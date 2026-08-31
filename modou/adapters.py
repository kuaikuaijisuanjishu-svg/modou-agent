"""仓库适配器：测试 ID 映射与 argv 白名单。

两条硬规则（方案 §4、§7）：

1. **不执行来自 eval.sh 的任意 shell 字符串。** 只从中提取符合白名单的测试文件路径，
   argv 由我们自己拼。eval.sh 里含 heredoc、git checkout、管道，直接 shell 跑既不安全也不可复现。
2. **测试 ID 映射缺失、重复或歧义时立即失败**，不猜测。SymPy 的声明多是裸函数名，
   Sphinx 多是完整 nodeid，两者都必须一一映射到 pytest 真实收集到的 nodeid。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from dataclasses import dataclass

ADAPTER_VERSION = "1"

#: 只允许这种形状的测试文件路径从 eval.sh 里被提取出来
_TEST_FILE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+tests?/(?:[\w.-]+/)*[\w.-]*test[\w.-]*\.py)")


class TestIdError(RuntimeError):
    """映射失败一律 fail closed，不降级。"""


class TestIdMissing(TestIdError):
    pass


class TestIdAmbiguous(TestIdError):
    pass


@dataclass(frozen=True)
class RepoAdapter:
    repo: str                  # "sympy/sympy"
    clone_dir: str             # scratch/work 下的目录名
    package_root: str          # 包目录树，如 "sympy"
    venv: str = ".venv39"
    version: str = ADAPTER_VERSION
    #: src 布局仓库的源码根（相对仓库根，如 "src"）。扁平布局留空。
    #: 为空时不设置任何环境变量——不猜，也不影响既有语料。
    source_path: str = ""

    # ------------------------------------------------------------ 测试文件

    def test_files(self, eval_sh: str) -> list[str]:
        """从 eval.sh 提取声明的测试文件。只取白名单形状，去重保序。"""
        out, seen = [], set()
        for line in eval_sh.splitlines():
            # 跳过 heredoc 里的补丁正文
            if line.startswith(("+", "-", "@@", "diff ", "index ")):
                continue
            for m in _TEST_FILE.finditer(line):
                p = m.group(1)
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    # ------------------------------------------------------------ argv

    def coverage_source(self, test_files: list[str] | None = None) -> str:
        """--source 必须同时包含包目录和测试目录。

        sympy 的测试在 `sympy/*/tests/` —— 包内；sphinx 的在 `tests/` —— 包外。
        只写包名的话，sphinx 的测试文件根本不被测量，
        于是"刚跑过的测试文件必须有覆盖行"这条自检会因为构造原因永远失败。
        """
        roots = {self.package_root}
        for tf in test_files or []:
            top = tf.split("/", 1)[0]
            if top and not top.endswith(".py"):
                roots.add(top)
        return ",".join(sorted(roots))

    def test_env(self, repo_root: "Path | None" = None) -> dict:
        """被测子进程需要的环境覆盖。只可能包含 PYTHONPATH，且只在 src 布局下。

        写成 adapter 的一部分而不是调用点的临时参数：布局是仓库的属性，
        每一个跑测试的地方都必须用同一份答案，否则收集和执行会读到两份代码。
        """
        if not self.source_path:
            return {}
        root = Path(repo_root) if repo_root is not None else None
        return {"PYTHONPATH": str(root / self.source_path) if root
                else self.source_path}

    def pytest_argv(self, py: str, targets: list[str], *,
                    junit_xml: str | None = None,
                    collect_only: bool = False,
                    coverage_file: str | None = None,
                    coverage_source: str | None = None) -> list[str]:
        """自己拼 argv。绝不 shell=True，绝不把 eval.sh 的字符串丢给 shell。"""
        argv = [py]
        if coverage_file:
            src = coverage_source or self.package_root
            argv += ["-m", "coverage", "run", f"--source={src}"]
        argv += ["-m", "pytest", "-p", "no:cacheprovider", "-p", "no:randomly", "-q"]
        if collect_only:
            argv += ["--collect-only"]
        if junit_xml:
            argv += [f"--junit-xml={junit_xml}", "-o", "junit_family=xunit2"]
        argv += list(targets)
        return argv


# ------------------------------------------------------------------ 声明测试

def declared_tests(meta: dict) -> list[str]:
    """FAIL_TO_PASS ∪ PASS_TO_PASS。兼容字段是 JSON 字符串或列表；去重但保序。"""
    out, seen = [], set()
    for key in ("FAIL_TO_PASS", "PASS_TO_PASS"):
        raw = meta.get(key) or []
        if isinstance(raw, str):
            raw = json.loads(raw)
        for t in raw:
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    if not out:
        raise TestIdMissing("声明测试范围为空")
    return out


def _func_part(nodeid: str) -> str:
    """nodeid 尾部的测试函数名（去掉参数化后缀）。"""
    tail = nodeid.rsplit("::", 1)[-1]
    return tail.split("[", 1)[0]


def map_test_ids(declared: list[str], collected: list[str]) -> dict[str, str]:
    """把声明 ID 一一映射到 pytest 实际收集到的 nodeid。

    缺失 -> TestIdMissing；多于一个候选 -> TestIdAmbiguous。两者都不猜。
    """
    collected_set = set(collected)
    by_func: dict[str, list[str]] = {}
    for nid in collected:
        by_func.setdefault(_func_part(nid), []).append(nid)

    mapping: dict[str, str] = {}
    for d in declared:
        # 1) 完全一致
        if d in collected_set:
            mapping[d] = d
            continue
        # 2) 声明里带 :: —— 按后缀唯一匹配
        if "::" in d:
            cands = [n for n in collected if n.endswith(d) or d.endswith(n)]
            cands = sorted(set(cands))
            if len(cands) == 1:
                mapping[d] = cands[0]
                continue
            if not cands:
                raise TestIdMissing(f"声明测试项未被收集到：{d}")
            raise TestIdAmbiguous(f"声明测试项匹配到多个 nodeid：{d} -> {cands[:5]}")
        # 3) 裸函数名 —— 在已收集集合里按函数名唯一匹配
        cands = sorted(set(by_func.get(d, [])))
        if len(cands) == 1:
            mapping[d] = cands[0]
            continue
        if not cands:
            raise TestIdMissing(f"声明测试项未被收集到：{d}")
        raise TestIdAmbiguous(
            f"裸函数名在多处出现，拒绝猜测：{d} -> {cands[:5]}")

    # 反向也必须唯一：两个声明项不能映射到同一个 nodeid
    rev: dict[str, list[str]] = {}
    for d, n in mapping.items():
        rev.setdefault(n, []).append(d)
    dup = {n: ds for n, ds in rev.items() if len(ds) > 1}
    if dup:
        raise TestIdAmbiguous(f"多个声明项映射到同一 nodeid：{list(dup.items())[:3]}")
    return mapping


# ------------------------------------------------------------------ 注册表

ADAPTERS: dict[str, RepoAdapter] = {
    "sympy/sympy": RepoAdapter(
        repo="sympy/sympy", clone_dir="sympy", package_root="sympy"),
    "sphinx-doc/sphinx": RepoAdapter(
        repo="sphinx-doc/sphinx", clone_dir="sphinx", package_root="sphinx",
        venv=".venv39_sphinx"),
    "pytest-dev/pytest": RepoAdapter(
        repo="pytest-dev/pytest", clone_dir="pytest", package_root="src",
        venv=".venv39_pytest"),
}


def adapter_for(repo: str) -> RepoAdapter:
    if repo not in ADAPTERS:
        raise KeyError(f"没有为 {repo} 注册适配器")
    return ADAPTERS[repo]
