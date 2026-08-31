"""覆盖率采集与**强制自检**。

这里是上一版栽跟头的地方：`coverage run bin/test` 在 sympy 上直接 no-data-collected，
`measured_files()` 有一千多个文件但每个 `lines()` 都是空的。结果所有"删后仍绿"
被误判成"零覆盖"，指标恒为 0，差点被当成"产品不成立"的证据。

所以本模块的契约是：**任何一项自检不过就抛 CoverageUnavailable，
绝不返回空字典，绝不让调用方继续算无据标签。** 让它响，不让它静默。

另一条（方案 §5）：只有"可执行但未执行"的新增行才能标无据。
空行、注释、纯括号这些非执行行不在 coverage 的 executable statements 里，
不能因为"覆盖率中没有它"就推导为无据。所以这里必须同时给出三个集合。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .testrange import run_argv


class CoverageUnavailable(RuntimeError):
    """采集失败或自检不过。调用方必须让实例失败关闭，不得降级。"""


@dataclass
class CoverageResult:
    executable: dict[str, set[int]] = field(default_factory=dict)
    executed: dict[str, set[int]] = field(default_factory=dict)
    missing: dict[str, set[int]] = field(default_factory=dict)

    def is_executable(self, path: str, lineno: int) -> bool:
        return lineno in self.executable.get(path, ())

    def is_executed(self, path: str, lineno: int) -> bool:
        return lineno in self.executed.get(path, ())

    def unevidenced(self, path: str, lineno: int) -> bool:
        """可执行、但从未被执行 —— 这才是无据。"""
        return self.is_executable(path, lineno) and not self.is_executed(path, lineno)


_DUMP = r"""
import json, sys, coverage
data_file, root = sys.argv[1], sys.argv[2]
cov = coverage.Coverage(data_file=data_file)
cov.load()
d = cov.get_data()
out = {}
for f in d.measured_files():
    try:
        _, stmts, _excl, miss, _ = cov.analysis2(f)
    except Exception:
        continue
    executed = sorted(set(stmts) - set(miss))
    out[f] = {"stmts": sorted(stmts), "executed": executed, "missing": sorted(miss)}
print(json.dumps({"root": root, "files": out}))
"""


RCFILE_NAME = "modou.coveragerc"

#: 强制统一的采集口径，压掉仓库自己的 [coverage:run] 配置。
#: 为什么必须这么做：sphinx 的 setup.cfg 里写着 `parallel = True` 和 `branch = True`。
#: parallel 会让数据写成 `<file>.<主机名>.<pid>.<随机>` 而不是我们指定的路径，
#: branch 会让数据存成 arcs、于是 `CoverageData.lines()` 返回 None。
#: 采集口径必须由我们定，不能由被测仓库的 CI 配置决定。
_RCFILE_BODY = """[run]
branch = False
parallel = False
concurrency = thread
"""


def write_rcfile(run_dir: Path) -> Path:
    p = run_dir / RCFILE_NAME
    p.write_text(_RCFILE_BODY)
    return p


def _combine_if_parallel(py: str, worktree: Path, data_file: Path,
                         rcfile: Path | None) -> None:
    """万一仍然写成了并行分片，合并回精确路径。"""
    if data_file.exists():
        return
    shards = sorted(data_file.parent.glob(data_file.name + ".*"))
    if not shards:
        return
    env = {"COVERAGE_FILE": str(data_file)}
    if rcfile:
        env["COVERAGE_RCFILE"] = str(rcfile)
    run_argv([py, "-m", "coverage", "combine", *map(str, shards)],
             worktree, timeout=180, env=env)


def collect(py: str, worktree: Path, data_file: Path, run_started: float,
            declared_test_files: list[str],
            coverage_cmd_ok: bool, junit_ok: bool,
            rcfile: Path | None = None) -> CoverageResult:
    """把 coverage 数据读成三个集合，并跑完整自检。

    data_file 必须是本次运行专用的、跑之前已删干净的路径；
    run_started 是本次测试运行开始的时间戳。
    """
    if not coverage_cmd_ok:
        raise CoverageUnavailable("coverage 子命令执行失败")
    if not junit_ok:
        raise CoverageUnavailable("JUnit 基线无效，覆盖率不予采信")

    _combine_if_parallel(py, worktree, data_file, rcfile)

    if not data_file.exists():
        raise CoverageUnavailable(f"没有生成覆盖率数据文件：{data_file}")
    mtime = data_file.stat().st_mtime
    if mtime < run_started:
        raise CoverageUnavailable(
            f"覆盖率数据文件是陈旧的（mtime {mtime:.0f} < 运行开始 {run_started:.0f}）")

    env = {"COVERAGE_RCFILE": str(rcfile)} if rcfile else None
    r = run_argv([py, "-c", _DUMP, str(data_file), str(worktree)],
                 worktree, timeout=300, env=env)
    if r.returncode != 0 or not r.stdout.strip():
        raise CoverageUnavailable(
            f"读取覆盖率数据失败 rc={r.returncode}：{(r.stderr or '')[-400:]}")
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise CoverageUnavailable(f"覆盖率数据不是合法 JSON：{e}") from e

    root = str(worktree.resolve())
    res = CoverageResult()
    for abspath, sets in payload["files"].items():
        rel = _relativize(abspath, root)
        if rel is None:
            continue                       # 测量路径落在临时仓库之外，丢弃
        res.executable[rel] = set(sets["stmts"])
        res.executed[rel] = set(sets["executed"])
        res.missing[rel] = set(sets["missing"])

    if not res.executed:
        raise CoverageUnavailable("覆盖率数据里没有任何被执行的行（典型的 no-data-collected）")

    # 核心不变量：刚刚跑过的那些测试文件，自己必须有被执行到的行
    for tf in declared_test_files:
        hit = res.executed.get(tf)
        if not hit:
            raise CoverageUnavailable(
                f"自检失败：刚执行过的测试文件 {tf} 没有任何被覆盖的行，"
                f"说明 tracer 根本没生效")
    return res


def _relativize(abspath: str, root: str) -> str | None:
    p = str(Path(abspath).resolve())
    if p.startswith(root + "/"):
        return p[len(root) + 1:]
    return None


def fresh_data_file(run_dir: Path, tag: str) -> Path:
    """每次运行一个独立的数据文件，跑之前先删干净。"""
    f = run_dir / f".coverage.{tag}.{int(time.time()*1000)}"
    if f.exists():
        f.unlink()
    for stale in run_dir.glob(f".coverage.{tag}.*"):
        stale.unlink()
    return f
