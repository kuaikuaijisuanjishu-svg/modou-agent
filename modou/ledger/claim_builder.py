"""Deterministic ClaimBuilder.

Claim 只能由已经落账的 Fact / Experiment 与确定性策略决策推出。旧引擎的
``EvidenceUnit.verdict`` 不在输入里；改变旧标签只能让 parity 失败，不能改变 Claim。
"""
from __future__ import annotations

from ..models import TestStatus, TestVector
from . import records
from .records import Record

DERIVATION_VERSION = "required-by-test-v1"


class ClaimDerivationError(RuntimeError):
    """证据链不足或不自洽；不能生成主张，也不能退回旧 verdict。"""


def _vector(rec: Record, which: str) -> TestVector:
    if rec.record_type != records.FACT or \
            rec.payload.get("kind") != records.F_TEST_VECTOR:
        raise ClaimDerivationError(f"{rec.record_id[:8]} 不是 TestVector Fact")
    data = rec.payload.get("data") or {}
    if data.get("which") != which:
        raise ClaimDerivationError(
            f"{rec.record_id[:8]} 是 {data.get('which')}，预期 {which}")
    return TestVector.of({tid: TestStatus(status)
                          for tid, status in data.get("statuses", [])})


def required_by_test(items: list[Record], *, decision_id: str,
                     restored_fact_id: str, run_id: str) -> Record:
    """从完整证据链生成 ``RequiredByTest``；任一条件不足即抛错。"""
    by_id = {r.record_id: r for r in items}
    decision = by_id.get(decision_id)
    restored = by_id.get(restored_fact_id)
    if decision is None or restored is None:
        raise ClaimDerivationError("ProbeDecision 或 restored TestVector 没有落账")
    if decision.record_type != records.FACT or \
            decision.payload.get("kind") != records.F_PROBE_DECISION:
        raise ClaimDerivationError("decision_id 没有指向 ProbeDecision")
    data = decision.payload.get("data") or {}
    if data.get("policy_version") != "hdd-inspired-v1" or \
            data.get("decision") != "terminal_regression" or \
            data.get("reason") != "no_finer_ast_child":
        raise ClaimDerivationError("ProbeDecision 不是受支持的终局策略决策")

    experiment_id = data.get("experiment_id", "")
    exp = by_id.get(experiment_id)
    if exp is None or exp.record_type != records.EXPERIMENT:
        raise ClaimDerivationError("ProbeDecision 指向的 Experiment 不存在")
    if exp.payload.get("status") != records.COMPLETE:
        raise ClaimDerivationError("未完成的 Experiment 不能支撑主张")
    if exp.payload.get("restored_clean") is not True:
        raise ClaimDerivationError("Experiment 没有通过 per-Trial 工作区恢复校验")
    if any(r.run_id != run_id for r in (decision, exp, restored)):
        raise ClaimDerivationError("证据链跨了 run_id")

    anchor = exp.payload.get("anchor") or {}
    decision_anchor = decision.payload.get("anchor") or {}
    restored_anchor = restored.payload.get("anchor") or {}
    aids = {a.get("aid") for a in (anchor, decision_anchor, restored_anchor)}
    snaps = {a.get("snapshot_id") for a in (anchor, decision_anchor, restored_anchor)}
    if len(aids) != 1 or None in aids or len(snaps) != 1 or None in snaps:
        raise ClaimDerivationError("ProbeDecision、Experiment 与恢复观测 Anchor 不一致")

    pre_ids = exp.payload.get("pre_fact_ids") or []
    post_ids = exp.payload.get("post_fact_ids") or []
    baseline_id = next((fid for fid in pre_ids if fid in by_id and
                        (by_id[fid].payload.get("data") or {}).get("which") == "baseline"), "")
    mutated_id = next((fid for fid in post_ids if fid in by_id and
                       (by_id[fid].payload.get("data") or {}).get("which") == "mutated"), "")
    if not baseline_id or not mutated_id:
        raise ClaimDerivationError("Experiment 缺 baseline 或 mutated TestVector")
    baseline_rec, mutated_rec = by_id[baseline_id], by_id[mutated_id]
    if any(r.run_id != run_id for r in (baseline_rec, mutated_rec)):
        raise ClaimDerivationError("测试向量跨了 run_id")
    for rec in (baseline_rec, mutated_rec):
        if (rec.payload.get("anchor") or {}).get("aid") != anchor.get("aid"):
            raise ClaimDerivationError("测试向量与 Experiment Anchor 不一致")

    baseline = _vector(baseline_rec, "baseline")
    mutated = _vector(mutated_rec, "mutated")
    restored_vector = _vector(restored, "restored")
    regressions = mutated.regressions(baseline)
    if not regressions:
        raise ClaimDerivationError("mutated TestVector 没有具名回归")
    if not restored_vector.identical_to(baseline):
        raise ClaimDerivationError("restored TestVector 没有回到 baseline")

    provenance = list(dict.fromkeys([
        decision_id, experiment_id, baseline_id, mutated_id, restored_fact_id]))
    from .anchors import from_json
    return records.claim(
        records.C_REQUIRED_BY_TEST,
        anchor=from_json(anchor), provenance=provenance,
        scope={"snapshot": anchor["snapshot_id"],
               "declared_size": len(baseline),
               "范围": "结论只在已声明测试范围（F2P ∪ P2P）内成立"},
        data={"derivation_version": DERIVATION_VERSION,
              "regressions": [{"test_id": tid, "before": before.value,
                               "after": after.value}
                              for tid, before, after in regressions],
              "restore_clean": True,
              "legacy_verdict_dependency": False},
        run_id=run_id)
