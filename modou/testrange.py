"""已声明测试范围：收集、执行、逐项比对。

JUnit XML 是证据原件（方案 §4）。若 XML 无法无歧义还原 nodeid，实例**失败关闭**，
不根据退出码降级判断——退出码只能说"有没有出错"，说不出"哪一项变了"。

还原 nodeid 的做法不靠猜：先拿 pytest 收集到的真实 nodeid 集合，
为每个 nodeid 算出它在 XML 里应有的 (classname, name)，反查即可。
classname 里哪一段是模块、哪一段是类，不需要我们判断。
"""
from __future__ import annotations

import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .models import TestStatus, TestVector


class TestRangeError(RuntimeError):
    pass


class JUnitUnusable(TestRangeError):
    """XML 缺失、损坏，或无法无歧义映射回 nodeid。"""


class ProbeTimeout(TestRangeError):
    """子进程超时。

    单独立一个类，是因为账本必须记下**真实发生的是什么**。
    以前所有 TestRangeError 都被上层当成超时，于是"JUnit 解析不了"
    和"跑了 300 秒没跑完"在账本里长得一模一样——那是账本在撒谎。
    旧的对外投影仍可把它们统一成 probe_timeout，但底层记录不行。
    """


@dataclass
class RunResult:
    vector: TestVector
    seconds: float
    returncode: int
    junit_path: Path


# ---------------------------------------------------------------- 子进程

def run_argv(argv: list[str], cwd: Path, timeout: float,
             env: dict | None = None) -> subprocess.CompletedProcess:
    """一律 argv 调用，并由 Review 线程绑定的统一 Executor 执行。"""
    from .executor import current_executor, sanitized_environment
    return current_executor().run(
        argv, cwd=Path(cwd), timeout=timeout,
        env=sanitized_environment(env))


# ---------------------------------------------------------------- 收集

def collect_nodeids(adapter, py: str, cwd: Path, targets: list[str],
                    timeout: float = 300) -> list[str]:
    """pytest --collect-only。收集出错即抛，不返回半份清单。"""
    argv = adapter.pytest_argv(py, targets, collect_only=True)
    try:
        r = run_argv(argv, cwd, timeout, env=adapter.test_env(cwd) or None)
    except subprocess.TimeoutExpired as e:
        raise TestRangeError(f"收集超时：{targets}") from e
    ids = [ln.strip() for ln in (r.stdout or "").splitlines()
           if "::" in ln and not ln.startswith(("=", "_", " ", "E "))]
    if r.returncode not in (0, 5) or not ids:
        tail = ((r.stdout or "") + (r.stderr or ""))[-600:]
        raise TestRangeError(f"收集失败 rc={r.returncode}：{tail}")
    if any(w in (r.stdout or "") for w in ("errors during collection",
                                           "ERROR collecting")):
        raise TestRangeError("收集期间报错，拒绝使用这份清单")
    return ids


# ---------------------------------------------------------------- JUnit

def _expected_xml_key(nodeid: str) -> tuple[str, str]:
    """nodeid -> 它在 JUnit XML 里应有的 (classname, name)。

    'a/b/c.py::Klass::test_x[1]' -> ('a.b.c.Klass', 'test_x[1]')
    """
    file_part, _, rest = nodeid.partition("::")
    mod = file_part[:-3] if file_part.endswith(".py") else file_part
    mod = mod.replace("/", ".")
    parts = [p for p in rest.split("::") if p]
    name = parts[-1] if parts else ""
    classname = ".".join([mod] + parts[:-1]) if len(parts) > 1 else mod
    return classname, name


def parse_junit(xml_path: Path, declared_nodeids: list[str]) -> TestVector:
    """解析 JUnit，映射回 nodeid，产出逐项状态向量。

    声明了但 XML 里没有的，记 MISSING —— 不是忽略。
    """
    if not xml_path.exists() or xml_path.stat().st_size == 0:
        raise JUnitUnusable(f"JUnit XML 缺失或为空：{xml_path}")
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        raise JUnitUnusable(f"JUnit XML 解析失败：{e}") from e

    want = {_expected_xml_key(n): n for n in declared_nodeids}
    if len(want) != len(declared_nodeids):
        raise JUnitUnusable("两个声明 nodeid 映射到同一个 XML 键，无法无歧义还原")

    seen: dict[str, TestStatus] = {}
    for tc in root.iter("testcase"):
        key = (tc.get("classname") or "", tc.get("name") or "")
        nodeid = want.get(key)
        if nodeid is None:
            continue                        # 声明范围之外的测试，不关心
        if tc.find("error") is not None:
            st = TestStatus.ERROR
        elif tc.find("failure") is not None:
            st = TestStatus.FAILED
        elif tc.find("skipped") is not None:
            st = TestStatus.SKIPPED
        else:
            st = TestStatus.PASSED
        if nodeid in seen and seen[nodeid] is not st:
            raise JUnitUnusable(f"同一 nodeid 在 XML 中出现多次且状态不一致：{nodeid}")
        seen[nodeid] = st

    return TestVector.of({n: seen.get(n, TestStatus.MISSING)
                          for n in declared_nodeids})


# ---------------------------------------------------------------- 执行

def run_declared(adapter, py: str, cwd: Path, nodeids: list[str],
                 junit_path: Path, deadline: float,
                 coverage_file: str | None = None,
                 coverage_rcfile: str | None = None,
                 coverage_source: str | None = None) -> RunResult:
    """跑已声明测试范围。可同时开覆盖率——基线那一次就该一次跑完两样。

    coverage_rcfile 用来压掉被测仓库自己的 [coverage:run] 配置
    （sphinx 的 setup.cfg 里 parallel/branch 都是 True，会让数据落不到我们指定的路径）。
    """
    if junit_path.exists():
        junit_path.unlink()
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(adapter.test_env(cwd))
    if coverage_file:
        env["COVERAGE_FILE"] = coverage_file
    if coverage_rcfile:
        env["COVERAGE_RCFILE"] = coverage_rcfile
    env = env or None
    argv = adapter.pytest_argv(py, nodeids, junit_xml=str(junit_path),
                               coverage_file=coverage_file,
                               coverage_source=coverage_source)
    t0 = time.time()
    try:
        r = run_argv(argv, cwd, timeout=max(deadline, 1), env=env)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        raise ProbeTimeout("probe_timeout")
    return RunResult(vector=parse_junit(junit_path, nodeids),
                     seconds=time.time() - t0, returncode=rc,
                     junit_path=junit_path)
