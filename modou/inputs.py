"""输入解析：把三种来源收敛成同一个 `ResolvedInput`。

在这之前，`tools/run_one.py` 里写着 `if fixture_root is not None: ... else: ...`——
两种来源各有一条分支，第三种（用户自己的仓库）没有地方可加。

**为什么必须先做这一层，而不是先做 Agent。** 输入边界的判断是：真正的风险
集中在输入契约、状态机、事件和 sandbox，不在模型。一个只能读 SWE-bench
元数据的引擎，外面套多少层 Agent 都还是 benchmark 脚本——
README 里那句"如果只是加一个聊天框总结已有 JSON，本立项视为失败"。

三种来源：

| 来源 | 用途 | 需要用户知道 |
| --- | --- | --- |
| `benchmark` | 冻结样本评测 | instance_id + scaffold |
| `fixture` | 回归验收、稳定录屏 | 一个目录 |
| `local` | **真实用户路径** | 自己的仓库路径与测试文件 |

`local` 是本次新增的那条。它不要求 instance_id，也不要求仓库被 clone 进
`paths.WORK`——直接在用户仓库上开 worktree。**不改用户工作树**：
`git worktree add --detach` 只读地引用对象库，用户的当前分支、暂存区和
未提交改动都不受影响。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .adapters import ADAPTERS, RepoAdapter, adapter_for
from .safe_git import run_git


class InputError(RuntimeError):
    """输入不合法。**一律 fail closed**：宁可拒绝，不要猜一个仓库出来跑。"""


@dataclass(frozen=True)
class ResolvedInput:
    """引擎真正需要的东西。三种来源解析之后长得一模一样。"""
    instance_id: str
    meta: dict
    ai_patch: str
    adapter: RepoAdapter
    test_files: tuple[str, ...]
    #: 运行目录前缀（历史上是 `scaffold[:12]`）。冻结样本口径依赖它，不能改。
    slug: str
    #: 写进 manifest 的完整 scaffold 名。本地仓库为 "local"。
    scaffold: str = ""
    #: 覆盖 `paths.WORK / adapter.clone_dir`。本地仓库时指向用户自己的仓库。
    repo_root: Path | None = None
    #: 覆盖 `paths.WORK / adapter.venv / bin / python`。
    python: str | None = None
    kind: str = "benchmark"
    #: 用户只给了测试**文件**、没点名具体测试项时为 True。
    #: 这时声明范围 = 这些文件收集到的全部 nodeid，由收集阶段展开。
    #: 它必须被记下来而不是猜：声明范围是三态判据的**分母**。
    declare_all_collected: bool = False

    def describe(self) -> str:
        return f"{self.kind}:{self.instance_id}"


# ---------------------------------------------------------------- benchmark

def from_benchmark(instance_id: str, scaffold: str) -> ResolvedInput:
    from . import instances
    meta = instances.load_meta().get(instance_id)
    if meta is None:
        raise InputError(f"元数据里没有这个实例：{instance_id}")
    adapter = adapter_for(meta["repo"])
    return ResolvedInput(
        instance_id=instance_id, meta=meta,
        ai_patch=instances.load_patch(instance_id, scaffold),
        adapter=adapter,
        test_files=tuple(instances.declared_test_files(instance_id, adapter, meta)),
        slug=scaffold[:12], scaffold=scaffold, kind="benchmark")


# ------------------------------------------------------------------ fixture

def from_fixture(root: Path, scaffold: str = "fixture") -> ResolvedInput:
    from . import fixture
    meta = fixture.meta_for(root)
    return ResolvedInput(
        instance_id=meta["instance_id"], meta=meta,
        ai_patch=fixture.patch_for(root),
        adapter=adapter_for(meta["repo"]),
        test_files=("tests/test_calc.py",),
        slug=scaffold[:12], scaffold=scaffold, kind="fixture")


# -------------------------------------------------------------- local repo

def _git(args: list[str], cwd: Path, ok: tuple[int, ...] = (0,)) -> str:
    r = run_git(args, cwd=cwd, timeout=120)
    if r.returncode not in ok:
        raise InputError(f"git {' '.join(args[:2])} 失败：{r.stderr.strip()[:200]}")
    return r.stdout


def _untracked_patch(repo: Path) -> str:
    """把未跟踪的新文件也变成 diff。

    **为什么必须做。** `git diff HEAD` 不含未跟踪文件，而"游离"判的恰恰
    就是新加的、没人引用的文件——漏掉它们，最能说明问题的那一类结论
    直接消失，而且不会有任何报错。

    **为什么不用 `git add -N`。** 那个常见技巧会往用户的索引里写
    intent-to-add 条目，也就是改了用户仓库的状态。水木验码的第一条纪律是
    不动用户仓库，这里不能为了省几行代码破例。
    `git diff --no-index` 不碰索引，实测退出码 1 表示"有差异"，属正常。
    """
    out = []
    listing = _git(["ls-files", "--others", "--exclude-standard"], repo)
    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel or (repo / rel).is_dir():
            continue
        d = _git(["diff", "--no-ext-diff", "--no-textconv", "--no-index",
                  "--", "/dev/null", rel], repo, ok=(0, 1))
        if d.strip():
            out.append(d)
    return "".join(out)


def _infer_package_root(repo: Path, test_files: tuple[str, ...]) -> str:
    """覆盖率 --source 用。宁可给仓库根，也不要猜错一个包名。

    猜错的后果不是报错，是"刚跑过的测试文件没有覆盖行"——
    那条自检会因为构造原因永远失败，而失败原因看起来完全无关。
    """
    tops = {p.split("/", 1)[0] for p in test_files if "/" in p}
    for cand in sorted(d.name for d in repo.iterdir()
                       if d.is_dir() and (d / "__init__.py").exists()):
        if not cand.startswith(".") and cand not in tops:
            return cand
    return "."


def from_local_repo(repo_path: Path, *, test_files: list[str],
                    declared_tests: list[str] | None = None,
                    patch_file: Path | None = None,
                    base_commit: str = "",
                    python: str | None = None,
                    name: str = "") -> ResolvedInput:
    """用户自己的仓库。**不需要 instance_id。**

    补丁来源两选一：
    - `patch_file`：一个 diff 文件，打在 `base_commit`（默认 `HEAD`）上；
    - 不给：取工作树里**未提交的改动**（`git diff HEAD`），base 就是 `HEAD`。
      这是"我刚让 Coding Agent 改完，帮我看看"的那条路径。
    """
    repo = Path(repo_path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise InputError(f"不是 Git 仓库：{repo}")
    if not test_files:
        raise InputError("必须指定至少一个 pytest 测试文件——"
                         "声明测试范围是三态判据的分母，不能由工具替用户猜")
    for tf in test_files:
        if not (repo / tf).exists():
            raise InputError(f"测试文件不存在：{tf}")

    if patch_file is not None:
        ai_patch = Path(patch_file).expanduser().read_text(
            encoding="utf-8", errors="surrogateescape")
        base = base_commit or _git(["rev-parse", "HEAD"], repo).strip()
    else:
        base = base_commit or _git(["rev-parse", "HEAD"], repo).strip()
        ai_patch = (_git(["diff", "--no-ext-diff", "--no-textconv", base], repo)
                    + _untracked_patch(repo))
        if not ai_patch.strip():
            raise InputError(
                f"{base[:8]} 与工作树之间没有差异。"
                f"水木验码审查的是补丁，没有补丁就没有可审查的新增行。")

    declared = [d.strip() for d in (declared_tests or []) if d.strip()]
    declare_all = not declared
    if declare_all:
        # 占位。真正的声明范围在收集阶段展开成该文件收集到的全部 nodeid——
        # 这里填文件路径只是为了让 meta 形状完整，引擎不会拿它去映射。
        declared = list(test_files)

    ident = name or repo.name          # run_id 会是 local__<ident>
    meta = {
        "instance_id": ident,
        "repo": f"local/{repo.name}",
        "base_commit": base,
        "test_patch": "",
        "FAIL_TO_PASS": json.dumps(declared),
        "PASS_TO_PASS": json.dumps([]),
        "version": "0",
    }
    # 本地仓库的 adapter 是即时构造的：clone_dir 与 venv 都会被 repo_root /
    # python 覆盖，留在这里只是为了让 RepoAdapter 的字段完整。
    adapter = RepoAdapter(repo=meta["repo"], clone_dir=repo.name,
                          package_root=_infer_package_root(repo, tuple(test_files)))
    ADAPTERS.setdefault(meta["repo"], adapter)
    return ResolvedInput(
        instance_id=ident, meta=meta, ai_patch=ai_patch, adapter=adapter,
        test_files=tuple(test_files), slug="local", scaffold="local",
        repo_root=repo, python=python, kind="local",
        declare_all_collected=declare_all)
