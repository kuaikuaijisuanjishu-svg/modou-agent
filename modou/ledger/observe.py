"""Record structured observations used to derive review conclusions.

The facts below are collected from existing execution results:

| 事实 | 来源 | 支撑什么结论 |
| --- | --- | --- |
| `LineContext` | `coverage.CoverageResult` | 无据（可执行但未执行）、未测量、非执行行 |
| `CollectSet` | `testrange.collect_nodeids` | 游离判据② |
| `GitPresence` | `workspace.exists_in_base` | 游离判据①（硬负向控制） |
| `RefEdge` | `drift.statically_referenced` | 游离判据③ |
| `ProbeOutcome` | `hdd` 的 unresolved 表 | 未标注的机器可读原因 |
| `FileKind` | `filekinds.probe_allowed` | 不做探测的文件为什么不做 |

Coverage facts are recorded per file rather than per line. A patch may touch a large file,
逐行写 Fact 会让账本膨胀而不增加信息——按文件带上行集合，
每一行的状态照样可推导，evidence_id 照样指得到。
"""
from __future__ import annotations

from . import anchors, records
from .records import Record

SNAPSHOT_S2 = "S2"


def line_context(path: str, added_lines: list[int], cov, *,
                 non_executable: set[int], run_id: str) -> Record:
    """一个文件的逐行覆盖观测，范围限定在本补丁的新增行。

    `measured=False` 与「行不可执行」是两件事，必须分开记——
    混淆它们会让一个没人 import 的新脚本被整片记成"非执行行"。
    """
    measured = path in cov.executable
    executable = sorted(l for l in added_lines if cov.is_executable(path, l))
    executed = sorted(l for l in added_lines if cov.is_executed(path, l))
    return records.fact(
        records.F_LINE_CONTEXT,
        anchor=anchors.file_anchor(SNAPSHOT_S2, path),
        payload={"measured": measured,
                 "added_lines": sorted(added_lines),
                 "executable": executable,
                 "executed": executed,
                 # 文本层面就看得出的非执行行（空行/注释/纯括号）。
                 # 它是充分判据：为 True 的一定非执行。
                 "textually_non_executable": sorted(non_executable)},
        observer="coverage", run_id=run_id)


def collect_set(nodeids: list[str], *, run_id: str, which: str = "baseline",
                anchor=None) -> Record:
    """一次 pytest 收集观测。

    baseline 挂在 S2 快照；文件移除期间的 mutated 收集集挂在该文件 Anchor，
    并由对应 Experiment 的 post_fact_ids 引用。推导层比较真实集合，不采信
    一个事后写入的 ``identical=True`` 布尔值。
    """
    return records.fact(
        records.F_COLLECT_SET,
        anchor=anchor or anchors.snapshot_anchor(SNAPSHOT_S2),
        payload={"which": which, "n": len(nodeids),
                 "nodeids": sorted(nodeids)},
        observer="pytest_collect", run_id=run_id)


def git_presence(path: str, *, exists_in_base: bool, base_commit: str,
                 run_id: str) -> Record:
    """游离判据①。**补丁前已存在的文件永不命中游离**——这是硬负向控制。"""
    return records.fact(
        records.F_GIT_PRESENCE,
        anchor=anchors.file_anchor(SNAPSHOT_S2, path),
        payload={"exists_in_base": exists_in_base, "base_commit": base_commit},
        observer="git", run_id=run_id)


def ref_edge(path: str, *, referenced_by: str | None, method: str,
             run_id: str) -> Record:
    """游离判据③。`referenced_by=None` 表示扫描后没有找到引用者。

    注意这是**扫描结果**，不是"不存在引用"的证明：
    反射、运行时字符串拼接、entry_points、仓库外脚本都排除不了。
    这一条限制写在游离证书的「未覆盖」栏里。
    """
    return records.fact(
        records.F_REF_EDGE,
        anchor=anchors.file_anchor(SNAPSHOT_S2, path),
        payload={"referenced_by": referenced_by, "method": method,
                 "found": referenced_by is not None},
        observer="ref_scan", run_id=run_id)


def file_kind(path: str, *, probe_allowed: bool, reason: str,
              run_id: str) -> Record:
    """探测禁区。测试文件、配置、文档、二进制不做删除探测——
    否则就是删测试刷绿。"""
    return records.fact(
        records.F_FILE_KIND,
        anchor=anchors.file_anchor(SNAPSHOT_S2, path),
        payload={"probe_allowed": probe_allowed, "reason": reason},
        observer="filekinds", run_id=run_id)


def probe_outcome(path: str, unresolved: dict, *, run_id: str) -> Record:
    """引擎为什么没能归因这些行。原因必须机器可读、必须能单独计数。"""
    return records.fact(
        records.F_PROBE_OUTCOME,
        anchor=anchors.file_anchor(SNAPSHOT_S2, path),
        payload={"unresolved": {str(ln): r.value if hasattr(r, "value") else r
                                for ln, r in sorted(unresolved.items())}},
        observer="hdd", run_id=run_id)


def probe_decision(anchor, *, experiment_id: str, run_id: str) -> Record:
    """HDD-inspired 策略到达最细可隔离回归节点的确定性决策。

    这是 ClaimBuilder 所需的策略来源；它不包含 verdict，也不能由模型写入。
    """
    return records.fact(
        records.F_PROBE_DECISION,
        anchor=anchor,
        payload={"policy_version": "hdd-inspired-v1",
                 "decision": "terminal_regression",
                 "experiment_id": experiment_id,
                 "reason": "no_finer_ast_child"},
        observer="hdd_policy", run_id=run_id)
