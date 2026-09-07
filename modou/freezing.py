"""正式冻结与校验的**唯一真相**。

为什么单独一个模块：冻结端（`tools/freeze.py`）与校验端（`tools/evaluate.py`）
如果各自实现一遍源码枚举，算法一旦漂移，校验就会假通过或假失败——
而一个假通过的校验比没有校验更坏，它会给不可复现的跑数盖上"已冻结"的章。

三条关键设计，每条都是踩过的坑：

**① 不能用 `source_tar_sha256` 做校验。**
它是 tar **文件**的哈希，而 tar 内嵌 mtime / uid / gid。源码内容一个字节没改、
只是文件被 touch 过，重算出来的 tar 就不是同一份字节，校验会假失败。
所以另立 `source_content_sha256`：只看路径与内容，与 mtime 无关。
`source_tar_sha256` 保留不动，否则 `runs/run2/`、`runs/submission-20260827/`
的既有记录会失效。

**② 源码用 `git ls-files` 枚举，不用 `rglob`。**
rglob 会把 ignored / generated 的 `.py` 一起算进去，那些文件不属于
"这次跑数实际用的源码"，把它们算进摘要会让摘要无谓地抖动。

**③ 联合摘要用长度前缀。**
直接拼字符串有边界歧义：`("ab", "c")` 和 `("a", "bc")` 拼出来一样。

另外——**光校验工具源码是不够的**。评测还依赖冻结清单和 scratch 里的补丁缓存。
替换掉缓存里的 `patch.diff`，源码摘要一点不变，跑数照样会被算作正式运行。
所以 `verify_sample()` 必须逐个运行单元比对实际加载的补丁哈希。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .adapters import ADAPTER_VERSION
from .gitinfo import current_tool_commit, is_dirty

#: 进源码摘要的目录。与 tools/freeze.py 的 SRC_DIRS 保持一致。
SRC_DIRS = ("modou", "tools", "tests")

#: freeze.json 里必须有这个字段才能用于正式校验；老冻结没有，只能记为不可校验。
CONTENT_KEY = "source_content_sha256"


class FreezeUnusable(RuntimeError):
    """冻结记录缺失、损坏，或不足以定义这次运行。调用方必须失败关闭。"""


# ---------------------------------------------------------------- 摘要

def _h(*chunks: bytes) -> str:
    """长度前缀的联合摘要。不用字符串拼接——那有边界歧义。"""
    h = hashlib.sha256()
    for c in chunks:
        h.update(str(len(c)).encode())
        h.update(b"\x00")
        h.update(c)
    return h.hexdigest()


def source_files(root: Path | None = None) -> list[str]:
    """本次跑数的源码清单。用 git ls-files，不用 rglob。

    非 git 环境下退回到 rglob 并在 verify 里明说——那种情况下摘要只能算参考。
    """
    root = root or paths.PROJECT
    # 未跟踪的 .py 不进摘要——它们不属于"已冻结的源码"。这不是漏洞：
    # `git status --porcelain` 会因为未跟踪文件把 commit 标成 +dirty，
    # 于是正式冻结与校验都会拒绝，方向是失败关闭。
    r = subprocess.run(["git", "ls-files", "--", *SRC_DIRS],
                       cwd=str(root), capture_output=True, text=True, timeout=60)
    if r.returncode == 0 and r.stdout.strip():
        return sorted(p for p in r.stdout.splitlines()
                      if p.endswith(".py") and "__pycache__" not in p)
    return sorted(
        str(f.relative_to(root))
        for d in SRC_DIRS if (root / d).exists()
        for f in (root / d).rglob("*.py") if "__pycache__" not in f.parts)


def source_content_sha256(root: Path | None = None) -> str:
    """与 mtime 无关的源码内容摘要。"""
    root = root or paths.PROJECT
    chunks: list[bytes] = []
    for rel in source_files(root):
        p = root / rel
        body = p.read_bytes() if p.exists() else b"<missing>"
        chunks.append(rel.encode())
        chunks.append(hashlib.sha256(body).hexdigest().encode())
    return _h(*chunks)


def deps_sha() -> str:
    """三份依赖清单的联合哈希（SPEC v2.2 §1.4 的 deps_sha）。

    放在这里而不是 tools/setup.py，是因为冻结、校验、体检三处都要用同一个数。
    """
    env = paths.CONFIGS / "env"
    chunks: list[bytes] = []
    for f in sorted(env.glob("*.txt")) if env.exists() else []:
        chunks.append(f.name.encode())
        chunks.append(f.read_bytes())
    return _h(*chunks) if chunks else "(无依赖清单)"


def manifest_sha256(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 校验

@dataclass
class FreezeStatus:
    run_root: Path
    freeze_sha256: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def raise_if_bad(self) -> "FreezeStatus":
        if not self.ok:
            raise FreezeUnusable(
                f"{self.run_root} 的冻结不足以定义这次运行：\n  · "
                + "\n  · ".join(self.problems))
        return self


def load(run_root: Path) -> dict:
    p = run_root / "freeze.json"
    if not p.exists():
        raise FreezeUnusable(f"没有冻结记录：{p}（先跑 tools/freeze.py --run-root）")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise FreezeUnusable(f"冻结记录不是合法 JSON：{p}: {e}") from e


def verify(run_root: Path, *, manifest_path: Path | None = None,
           root: Path | None = None) -> FreezeStatus:
    """这份冻结是否真的定义了「现在这套软件 + 现在这份清单」。

    不抛异常，把全部问题一次列清——一次只报一个问题会让人来回试。
    """
    root = root or paths.PROJECT
    info = load(run_root)
    st = FreezeStatus(run_root=run_root,
                      freeze_sha256=info.get(CONTENT_KEY, ""))

    want = info.get(CONTENT_KEY)
    if not want:
        st.problems.append(
            f"冻结记录里没有 {CONTENT_KEY}（run1/run2 时代的老格式），"
            f"不能用于正式校验")
    else:
        now = source_content_sha256(root)
        if now != want:
            st.problems.append(
                f"源码内容摘要不符：冻结 {want[:16]} ≠ 当前 {now[:16]}")

    commit = current_tool_commit(root, refresh=True)
    if not commit:
        st.problems.append("当前不是 git 仓库，tool_commit 为空，无法绑定")
    else:
        if is_dirty(commit):
            st.problems.append(f"工作树有未提交修改（{commit}），正式跑数拒绝启动")
        frozen = info.get("tool_commit") or ""
        if frozen and frozen.split("+")[0] != commit.split("+")[0]:
            st.problems.append(
                f"tool_commit 不符：冻结 {frozen[:12]} ≠ 当前 {commit[:12]}")

    if info.get("adapter_version") != ADAPTER_VERSION:
        st.problems.append(
            f"adapter 版本不符：冻结 {info.get('adapter_version')} ≠ 当前 {ADAPTER_VERSION}")

    frozen_deps = info.get("deps_sha")
    if frozen_deps:
        now_deps = deps_sha()
        if now_deps != frozen_deps:
            st.problems.append(
                f"依赖清单摘要不符：冻结 {frozen_deps[:16]} ≠ 当前 {now_deps[:16]}")

    if manifest_path is not None:
        frozen_m = info.get("evaluation_manifest_sha256")
        if not frozen_m:
            st.problems.append("冻结记录里没有 evaluation_manifest_sha256")
        elif not manifest_path.exists():
            st.problems.append(f"冻结清单不存在：{manifest_path}")
        else:
            now_m = manifest_sha256(manifest_path)
            if now_m != frozen_m:
                st.problems.append(
                    f"冻结清单摘要不符：冻结 {frozen_m[:16]} ≠ 当前 {now_m[:16]}")
    return st


def verify_sample(manifest: dict, *, loader=None, meta=None) -> list[str]:
    """逐个运行单元比对**实际加载的**补丁与元数据是否命中清单哈希。

    这一步不能省：源码摘要对 scratch 缓存一无所知，
    把 `patch.diff` 换掉，源码摘要一个比特都不会变。

    loader / meta 可注入，便于测试；默认走 modou.instances。
    """
    from . import instances
    from .models import sha256_text

    loader = loader or instances.load_patch
    meta = meta if meta is not None else instances.load_meta()

    problems: list[str] = []
    for row in manifest.get("instances", []):
        inst, sc = row["instance_id"], row["scaffold"]
        m = meta.get(inst)
        if m is None:
            problems.append(f"{inst}：元数据里没有这个实例")
            continue
        if m.get("repo") != row.get("repo"):
            problems.append(f"{inst}：repo 不符（{m.get('repo')} ≠ {row.get('repo')}）")
        if m.get("base_commit") != row.get("base_commit"):
            problems.append(f"{inst}：base_commit 不符")
        try:
            got = sha256_text(loader(inst, sc))
        except FileNotFoundError as e:
            problems.append(f"{inst}/{sc}：补丁缺失（{e}）")
            continue
        if got != row.get("patch_sha256"):
            problems.append(
                f"{inst}/{sc}：补丁哈希不符（{got[:12]} ≠ {str(row.get('patch_sha256'))[:12]}）")
        tp = sha256_text(m.get("test_patch") or "")
        if tp != row.get("test_patch_sha256"):
            problems.append(f"{inst}：test_patch 哈希不符")
    return problems
