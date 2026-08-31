"""The ledger stores Fact, Experiment, and Claim records.

Claims require provenance from recorded observations.
这条不靠文案纪律维持，靠数据结构——`Claim` 没有非空 `provenance` 就构造不出来。

The three record types have distinct responsibilities:

- **Fact**：无干预的观测（测试状态向量、逐行 context、引用边、收集清单）。
- **Experiment**：一次干预 + 观测（删 AST 单元、还原 hunk、应用子集）。
  静态诊断增量属于这里，**不属于 Fact 层**——"删掉之后诊断变多了"是干预的结果。
- **Claim**：纯函数从 Fact / Experiment 推出，带 provenance。

`provenance` 只要求**至少一个**有效 ID，不机械要求两类都有：
纯 Fact 派生的主张（如「有静态引用」）未必有对应实验。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .anchors import Anchor, SourceLocation

SCHEMA_VERSION = 1

#: 记录类型。LEDGER_COMPLETE 是收尾标记——没有它就说明账本不完整。
FACT = "Fact"
EXPERIMENT = "Experiment"
CLAIM = "Claim"
LEDGER_COMPLETE = "LedgerComplete"

#: Fact 的种类
F_TEST_VECTOR = "TestVector"
F_LINE_CONTEXT = "LineContext"
F_STATIC_DIAG = "StaticDiag"
F_REF_EDGE = "RefEdge"
F_COLLECT_SET = "CollectSet"
F_HUNK_MAP = "HunkMap"
#: Facts derived from coverage, repository state, and probe outcomes.
F_GIT_PRESENCE = "GitPresence"      # 该路径在 base commit 里存不存在
F_FILE_KIND = "FileKind"            # 允不允许做删除探测，不允许的理由
F_PROBE_OUTCOME = "ProbeOutcome"    # 引擎为什么没能归因某些行（预算/超时/不合法）
F_PROBE_DECISION = "ProbeDecision"  # 确定性探测策略为何把某实验视为终局候选

#: Experiment 的种类
X_DELETE_UNIT = "DeleteUnit"
X_REVERT_HUNK = "RevertHunk"
X_APPLY_SUBSET = "ApplySubset"

#: Experiment 的结局。观测失败必须分型——账本记的是**真实发生了什么**，
#: "跑不完"和"JUnit 解析不了"在账本里长成一样，就是账本在撒谎。
COMPLETE = "COMPLETE"
TIMEOUT = "TIMEOUT"
INVALID = "INVALID"
DIRTY = "DIRTY"
ABORTED = "ABORTED"                  # 干预已施加，但还没跑测试就放弃了
JUNIT_UNUSABLE = "JUNIT_UNUSABLE"    # 跑了，但 XML 无法无歧义还原
OBSERVATION_FAILED = "OBSERVATION_FAILED"   # 其它测试运行协议错误

#: Claim 的种类。**没有任何一种表达"可以删除"**——账本里不存在支持那类主张的路径。
C_REQUIRED_BY_TEST = "RequiredByTest"
C_CONTRIBUTES_TO_FIX = "ContributesToFix"
C_PROTECTS_REGRESSION = "ProtectsRegression"
C_STATIC_DEPENDENCY = "StaticDependency"
C_STRUCTURALLY_REFERENCED = "StructurallyReferenced"

CLAIM_KINDS = frozenset({
    C_REQUIRED_BY_TEST, C_CONTRIBUTES_TO_FIX, C_PROTECTS_REGRESSION,
    C_STATIC_DEPENDENCY, C_STRUCTURALLY_REFERENCED,
})


class ProvenanceMissing(ValueError):
    """没有观测支持的主张。构造期就拒绝——这是「沉默 ≠ 反对」的结构实现。"""


def _rid(record_type: str, payload: dict) -> str:
    """内容寻址的 record_id。同样的观测重复写入不会产生两个身份。"""
    blob = json.dumps({"t": record_type, "p": payload},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Record:
    record_type: str
    payload: dict
    run_id: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def record_id(self) -> str:
        return _rid(self.record_type, self.payload)

    def to_json(self) -> dict:
        return {"schema_version": self.schema_version, "run_id": self.run_id,
                "record_id": self.record_id, "record_type": self.record_type,
                "payload": self.payload}


# ---------------------------------------------------------------- 构造

def fact(kind: str, *, anchor: Anchor | None = None,
         location: SourceLocation | None = None,
         payload: dict | None = None, observer: str = "",
         observed_at: float = 0.0, cost_s: float = 0.0,
         run_id: str = "") -> Record:
    if anchor is None and location is None:
        raise ValueError("Fact 必须挂在 Anchor 或 SourceLocation 上")
    return Record(FACT, {
        "kind": kind,
        "anchor": anchor.to_json() if anchor else None,
        "location": location.to_json() if location else None,
        "data": payload or {},
        "observer": observer, "observed_at": observed_at, "cost_s": cost_s,
    }, run_id=run_id)


def experiment(kind: str, *, anchor: Anchor, snapshot_base: str,
               intervention: dict, pre: list[str], post: list[str],
               status: str = COMPLETE, restored_clean: bool | None = None,
               cost_s: float = 0.0, run_id: str = "",
               failure_reason: str = "", phase: str = "") -> Record:
    return Record(EXPERIMENT, {
        "kind": kind,
        "anchor": anchor.to_json(),
        "snapshot_base": snapshot_base,
        "intervention": intervention,
        "pre_fact_ids": [p for p in pre if p],
        "post_fact_ids": [p for p in post if p],
        "status": status,
        "restored_clean": restored_clean,
        "cost_s": cost_s,
        # 失败了就要说清楚失败在哪一步、为什么。只记一个 status 不够。
        "failure_reason": failure_reason,
        "phase": phase,
    }, run_id=run_id)


def claim(kind: str, *, anchor: Anchor, provenance: list[str],
          scope: dict, data: dict | None = None, run_id: str = "") -> Record:
    """provenance 为空即拒绝构造。

    这是账本对「沉默 ≠ 反对」的结构保证：一个裁判说不出话，
    就没有 Observation，也就没有 ID 可以填进 provenance，Claim 根本立不起来。
    """
    if kind not in CLAIM_KINDS:
        raise ValueError(f"未知的主张类型：{kind}")
    prov = [p for p in provenance if p]
    if not prov:
        raise ProvenanceMissing(
            f"{kind} @ {anchor.path}:{anchor.line_start} 没有任何观测支持。"
            f"没有 Observation 就没有 Claim——这不是可以放宽的边界情况。")
    return Record(CLAIM, {
        "kind": kind,
        "anchor": anchor.to_json(),
        "provenance": prov,
        "scope": scope,
        "data": data or {},
    }, run_id=run_id)


def ledger_complete(run_id: str, counts: dict) -> Record:
    """收尾标记。没有它，一份语法合法的 JSONL 也可能是被中断的半份账本。"""
    return Record(LEDGER_COMPLETE, {"counts": counts}, run_id=run_id)
