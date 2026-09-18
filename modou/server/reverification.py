"""质疑原因驱动的复验策略表（A2 → M0 波次）。

七条质疑原因各自配一条专属策略，M0 波次把「只有默认重放」的五条
再补上四条：

- flaky_result_suspected：完整重放循环跑 K_RERUNS 遍，全部一致才算
  确认（台上口径：你说不稳？我跑两遍，两遍一样）；
- source_changed：只比工作区快照哈希，不跑测试；变了就扣留并写明，
  没变则原证据仍然精确绑定当前源码；
- test_did_not_execute_code：带 per-test 覆盖率重跑声明范围，用
  admissibility 判每条回归是否真的执行过被删的行——执行过才算
  行为证据（A），全部可解析但都没执行过只能降为间接证据（B）；
- collection_crash_suspected：只收集不执行。基线收集干净而干预后
  收集崩，失败是连带损坏（C）；两边都干净，失败是行为性的；
- test_scope_incomplete：确定性扩大收集范围重跑。原回归全部复现
  才确认，且扩大范围永远不改原主张等级，只作新 revision 佐证；
  新增测试数超过预算上限一律失败关闭，不许截断后继续；
- other_needs_explanation：不跑实验，诚实路由给人工判断。它是路由
  不是差异化实验，因此 differentiated 保持 False；
- new_test_available：质疑自带一个只碰测试文件的 unified diff，
  送进既有补测签证流水线跑完整三段验证。红线口径：签证 PASS 不
  升级原主张，只追加一条新 claim（该行现受用户提交的测试保护）；
  签证扣留原样透出，绝不圆场。

_settle_from_replay 仍是重放类策略的唯一结算判据表：恢复不净一律
扣留；向量一致才确认；出现分歧只以新 revision 修订并在事件里带上
evidence_delta，旧结论只追加不覆盖。不确定永远不升级——
ensure_no_promotion 把这条纪律也压到非重放策略的证据定级上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..admissibility import grade_one, resolve_context
from ..coverage import CoverageUnavailable
from ..models import ADMISSIBILITY_ORDER, TestStatus


K_RERUNS = 2
SCHEMA_VERSION = "review-reverification-v2"
# 重放兜底的诚实标注：策略 1-5 都有差异化实验后，它不再映射任何
# 质疑原因，仅作为回落实现保留（历史记录读取仍可能见到它的标注）。
REPLAY_ONLY_OPEN = ["not_implemented"]
# other_needs_explanation 的诚实标注：实验回答不了，路由给人工。
HUMAN_ROUTING_OPEN = ["requires_human_judgment"]
# scope_widen 的确定性预算：扩跑新增测试数上限。超限失败关闭，
# 绝不截断后继续——截断会让"全范围复现"变成一句无法兑现的话。
SCOPE_WIDEN_MAX_NEW_TESTS = 25


class CollectionUnsupported(RuntimeError):
    """宿主适配器不具备只收集能力。策略必须失败关闭，不得回落重放。"""


@dataclass(frozen=True)
class StrategyOutcome:
    """策略跑完后的机器结论；结算只认这里给出的字段。"""

    outcome: str                       # claim.confirmed/revised/withheld
    outcome_reason: str
    answered: bool                     # 差异化实验是否真的回答了质疑
    open_items: tuple[str, ...] = ()
    evidence_delta: dict | None = None
    needs_human: bool = False
    experiments_added: int = 0
    # 与既有 replay_observations 完全同形的最后一次循环观测。
    replay_observations: list[dict] = field(default_factory=list)
    # 每一次重放的 experiment.replayed 事件载荷（控制面负责入链）。
    replay_events: list[dict] = field(default_factory=list)


class ExperimentContext(Protocol):
    """策略对宿主的全部访问面：仍然是具名方法表，不是逃生舱。

    六个方法就是封闭集合——除了这张表上的动作，策略拿不到注释、
    账本写入或密钥等任何其他能力。新增能力只能通过给这张表加一个
    具名方法并同步这里的协议来完成，没有第二条路。
    """

    def replay_once(self, record: dict) -> list[dict]:
        """按既定计划在隔离工作树里完整重放一轮，返回观测列表。"""

    def source_snapshot_matches(self) -> dict[str, object]:
        """返回当前源码与计划快照的明确比较结果。"""

    def coverage_contexts(self, record: dict) -> dict:
        """带 per-test 覆盖率重跑声明范围，返回判定所需的上下文集。

        形如 {"path", "deleted_lines", "contexts_by_line", "regressions",
        "original_grade"}；覆盖率不可用直接上抛 CoverageUnavailable。
        """

    def collect_only(self, record: dict) -> dict:
        """在隔离工作树里只收集不执行，返回两侧收集结果。

        形如 {"baseline_collect_ok", "intervened_collect_ok", "detail"}；
        适配器不支持只收集则上抛 CollectionUnsupported。
        """

    def widen_declared_scope(self, record: dict) -> dict:
        """确定性扩大收集范围重跑，返回复现判定结果。

        形如 {"added_count", "capped",
        "widened_baseline_regens_original_regressions", "detail"}；
        超预算时只返回 capped=True，绝不截断后继续。
        """

    def submit_test_for_visa(self, record: dict) -> dict:
        """把补充测试送签证验证：走既有补测签证流水线，不开新路。

        宿主实现见 control.py 的 submit_test_for_visa——复用 propose_test
        的三段验证与隔离工作树；用户树在快照闸下只读，漂移即失败关闭。
        """


#: 封闭性检查用：协议上的具名方法就是这张表，一个都不能少，也
#: 不允许策略绕过表去摸宿主的其他能力。
EXPERIMENT_CONTEXT_METHODS = frozenset({
    "replay_once",
    "source_snapshot_matches",
    "coverage_contexts",
    "collect_only",
    "widen_declared_scope",
    "submit_test_for_visa",
})


class Strategy(Protocol):
    """一条质疑原因对应的实验策略。"""

    name: str
    differentiated: bool               # 是否有真的差异化实验

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        """执行实验并给出结论；实现不得改写记录或发出事件。"""


def _events_for_run(observations: list[dict], run_index: int,
                    reverification_id: str) -> list[dict]:
    events = []
    for obs in observations:
        events.append({
            "reverification_id": reverification_id,
            "experiment_id": obs["experiment_id"],
            "replay_experiment_id": obs["replay_experiment_id"],
            "vector_matches": obs["vector_matches"],
            "restored_clean": obs["restored_clean"],
            "run_index": run_index,
        })
    return events


def _settle_from_replay(runs: list[list[dict]], *, reverification_id: str,
                        confirmed_reason: str, answered: bool,
                        open_items: tuple[str, ...]) -> StrategyOutcome:
    """单点结算判据：恢复不净 → 扣留；一致 → 确认；分歧 → 修订。

    修订事件必带 evidence_delta；确认不新增证据，delta 保持 None；
    扣留是「不确定只能降级」的出口，任何策略不得从这里升级。
    """
    events = [event for index, run in enumerate(runs)
              for event in _events_for_run(run, index, reverification_id)]
    last = runs[-1]
    experiments_added = sum(len(run) for run in runs)
    if any(obs["restored_clean"] is not True for run in runs for obs in run):
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason=(
                "replay did not restore the workspace cleanly; uncertainty "
                "can only lower the claim"),
            answered=answered, open_items=open_items,
            experiments_added=experiments_added,
            replay_observations=last, replay_events=events)
    divergent = sorted({obs["experiment_id"] for run in runs
                        for obs in run if not obs["vector_matches"]})
    if divergent:
        return StrategyOutcome(
            outcome="claim.revised",
            outcome_reason=(
                "replay observed a different outcome; the original claim "
                "is revised as a new revision, never overwritten"),
            answered=answered, open_items=open_items,
            evidence_delta={"divergent_experiments": divergent,
                            "replay_runs": len(runs)},
            experiments_added=experiments_added,
            replay_observations=last, replay_events=events)
    return StrategyOutcome(
        outcome="claim.confirmed",
        outcome_reason=confirmed_reason,
        answered=answered, open_items=open_items,
        experiments_added=experiments_added,
        replay_observations=last, replay_events=events)


def ensure_no_promotion(original_grade: str, new_grade: str) -> str:
    """证据等级闸门：任何策略给出的等级不得强于原等级。

    ADMISSIBILITY_ORDER 由强到弱（A 行为证据 > B 间接 > C 连带 >
    D 环境）。新等级更强（排序更靠前）就拒绝、退回原等级；相等或
    更弱才放行。任何一端不是合法等级时失败关闭退回原等级——不确定
    只能降级，绝不让一次闸门调用把主张悄悄升级。
    """
    ranks = {level.value: rank
             for rank, level in enumerate(ADMISSIBILITY_ORDER)}
    original = str(original_grade)
    new = str(new_grade)
    if original not in ranks or new not in ranks:
        return original
    if ranks[new] < ranks[original]:
        return original
    return new


#: 证据等级的机器标签 ↔ 字母等级。策略对外只说标签，闸门只认字母。
_LABEL_TO_GRADE = {"behavioural": "A", "indirect": "B",
                   "collateral": "C", "environmental": "D"}
_GRADE_TO_LABEL = {grade: label for label, grade in _LABEL_TO_GRADE.items()}


def _admissibility_label(label: str, original_grade: str | None) -> str:
    """策略想给的证据等级标签，过 ensure_no_promotion 闸门再出口。

    拿不到原等级（None 或不在 A-D 里）按 "A" 宽松处理：A 是等级
    天花板，任何实验结论都不会强于它，闸门此时不该因为缺数据把
    诚实的结论拦下来。想给的标签映射不到字母时按 "B"（间接证据）
    处理——和 admissibility 的口径一致，不确定不能变成更强的主张。
    """
    wanted = _LABEL_TO_GRADE.get(str(label), "B")
    allowed = str(original_grade) if str(original_grade) in \
        _GRADE_TO_LABEL else "A"
    final = ensure_no_promotion(allowed, wanted)
    return _GRADE_TO_LABEL.get(final, "indirect")


class ReplayOnlyStrategy:
    """默认策略：单次重放并对比向量；差异化实验未实现。

    记录里如实保留 open=["not_implemented"]，UI 显示「这条理由的
    差异化实验排在会话能力之后，目前只做重放」——重放结论本身可信，
    但质疑没有被差异化实验回答。
    """

    name = "replay_only"
    differentiated = False
    plan_open: tuple[str, ...] = tuple(REPLAY_ONLY_OPEN)

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        runs = [ctx.replay_once(record)]
        return _settle_from_replay(
            runs, reverification_id=record["reverification_id"],
            confirmed_reason=(

                "replay reproduced the original observation vector "
                "and the workspace restored clean"),
            answered=False,
            open_items=self.plan_open)


class FlakyRerunStrategy:
    """结果不稳 → 完整重放循环跑 K_RERUNS 遍，全部一致才确认。"""

    name = "flaky_result_suspected"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        runs = [ctx.replay_once(record) for _ in range(K_RERUNS)]
        return _settle_from_replay(
            runs, reverification_id=record["reverification_id"],
            confirmed_reason=f"{K_RERUNS} full replay runs reproduced the "
                             "original observation vector consistently "
                             "and the workspace restored clean each time",
            answered=True, open_items=())


class SourceChangedStrategy:
    """代码已变 → 只比快照哈希，不跑测试。

    变了：密封证据不再绑定当前源码，扣留并写明，需要人重新审查；
    没变：质疑前提不成立，原证据仍精确绑定这份源码，直接确认。
    """

    name = "source_changed"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        comparison = ctx.source_snapshot_matches()
        matches = comparison["matches"] is True
        expected = str(comparison.get("expected_sha256") or "")
        observed = str(comparison.get("observed_sha256") or "")
        # snapshot_sha256 remains only as a v1 read-compatibility field. New
        # records must expose both sides so a changed source can be audited.
        delta = {
            "expected_snapshot_sha256": expected,
            "observed_snapshot_sha256": observed,
            "snapshot_matches": matches,
            "snapshot_sha256": observed,
        }
        if matches:
            return StrategyOutcome(
                outcome="claim.confirmed",
                outcome_reason=(

                    "workspace hash still equals the reviewed snapshot; "
                    "the sealed evidence binds this source exactly and "
                    "no rerun is needed"),
                answered=True, evidence_delta=delta)
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason=(

                "source changed since the review; the sealed experiments "
                "describe a snapshot that no longer matches this "
                "workspace"),
            answered=True, evidence_delta=delta, needs_human=True)


class CoverageCheckStrategy:
    """没执行代码？带 per-test 覆盖率核对每条回归执行过被删行吗。

    有回归执行过被删的行且真的失败：质疑前提成立——失败确实是这行
    的行为证据（A），原主张确认。全部可解析但没有任何回归执行过被
    删的行：失败与这行无关，只能算间接证据（B），原主张降级修订。
    覆盖率不可用或上下文解析不了：不确定，扣留并转人工，绝不回落
    成重放——重放回答不了「执行过吗」这个问题。
    """

    name = "coverage_check"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        try:
            report = ctx.coverage_contexts(record)
        except CoverageUnavailable as exc:
            return StrategyOutcome(
                outcome="claim.withheld",
                outcome_reason=f"coverage_unavailable: {exc}"[:300],
                answered=False, needs_human=True)
        original_grade = report.get("original_grade")
        deleted_lines = [int(n) for n in report.get("deleted_lines") or []]
        contexts_by_line = report.get("contexts_by_line") or {}
        regressions = report.get("regressions") or []
        touching: set[str] = set()
        known: set[str] = set()
        for line in deleted_lines:
            touching |= {str(n) for n in contexts_by_line.get(line, ())}
        for names in contexts_by_line.values():
            known |= {str(n) for n in names}
        hit_tests: list[str] = []
        for tid, before, after in regressions:
            try:
                before_status = TestStatus(str(before))
                after_status = TestStatus(str(after))
            except ValueError:
                # 账本状态字面量不合法：和覆盖率缺失一样不确定，
                # 失败关闭而不是挑一条看得懂的继续算。
                return StrategyOutcome(
                    outcome="claim.withheld",
                    outcome_reason="coverage_unavailable: illegal status "
                                   f"literal for {tid}"[:300],
                    answered=False, needs_human=True)
            resolved = resolve_context(str(tid), known)
            if resolved is None:
                # 上下文解析不了（歧义或没采到）：无法区分行为证据
                # 和间接证据，按不确定扣留，绝不猜。
                return StrategyOutcome(
                    outcome="claim.withheld",
                    outcome_reason="coverage_unavailable: cannot resolve "
                                   f"coverage context for {tid}"[:300],
                    answered=False, needs_human=True)
            executed = resolved in touching
            graded = grade_one(before_status, after_status,
                               executed_the_lines=executed)
            if executed and graded.value == "A":
                hit_tests.append(str(tid))
        if hit_tests:
            return StrategyOutcome(
                outcome="claim.confirmed",
                outcome_reason=(
                    "the failing tests executed the deleted lines in the "
                    "baseline coverage; the failure is behavioural "
                    "evidence for the claim"),
                answered=True,
                evidence_delta={
                    "hit_tests": hit_tests,
                    "admissibility": _admissibility_label(
                        "behavioural", original_grade),
                },
                experiments_added=1)
        return StrategyOutcome(
            outcome="claim.revised",
            outcome_reason=(
                "no failing test executed the deleted lines; the failure "
                "is indirect evidence and the claim is downgraded"),
            answered=True,
            evidence_delta={"admissibility": _admissibility_label(
                "indirect", original_grade)},
            experiments_added=1)


class CollectionCheckStrategy:
    """收集崩了？只收集不执行，把行为失败和连带损坏分开。

    基线收集干净而干预后收集崩：失败其实是收集崩溃（C 连带损坏），
    原主张修订。两边都干净：失败是行为性的，原主张确认。基线自己
    就崩或两边都崩：环境不确定，扣留并转人工。适配器不支持只收集：
    如实失败关闭，绝不回落重放——重放会把收集崩溃误读成行为失败。
    """

    name = "collection_check"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        try:
            report = ctx.collect_only(record)
        except CollectionUnsupported as exc:
            return StrategyOutcome(
                outcome="claim.withheld",
                outcome_reason=f"collection_unsupported: {exc}"[:300],
                answered=False, needs_human=True)
        baseline_ok = report.get("baseline_collect_ok") is True
        intervened_ok = report.get("intervened_collect_ok") is True
        detail = str(report.get("detail") or "")
        original_grade = report.get("original_grade")
        if baseline_ok and intervened_ok:
            return StrategyOutcome(
                outcome="claim.confirmed",
                outcome_reason=(
                    "collection stayed clean under the intervention; the "
                    "observed failure is behavioural, not a collection "
                    "crash"),
                answered=True,
                evidence_delta={"admissibility": _admissibility_label(
                    "behavioural", original_grade)},
                experiments_added=1)
        if baseline_ok and not intervened_ok:
            return StrategyOutcome(
                outcome="claim.revised",
                outcome_reason=(
                    "collecting under the intervention crashes; the "
                    "observed failure is collateral damage, not behaviour"),
                answered=True,
                evidence_delta={"admissibility": _admissibility_label(
                    "collateral", original_grade)},
                experiments_added=1)
        reason = ("baseline_collection_failed"
                  if not baseline_ok and intervened_ok
                  else "collection crashes in both baseline and intervened "
                       "states; the environment is uncertain")
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason=reason[:300],
            answered=True,
            evidence_delta={"detail": detail} if detail else None,
            needs_human=True,
            experiments_added=1)


class ScopeWidenStrategy:
    """范围不够？确定性扩大收集范围重跑，预算超限一律失败关闭。

    原回归在扩跑中全部复现：原主张在更大范围内仍然成立，确认，且
    扩大范围永远不改原主张等级，只作新 revision 佐证。原回归没能
    复现：结果不确定，扣留并转人工。新增测试数超过预算上限：确定性
    事实直接失败关闭，绝不截断后继续——截断过的「全范围复现」是句
    无法兑现的话。
    """

    name = "scope_widen"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        report = ctx.widen_declared_scope(record)
        added_count = int(report.get("added_count") or 0)
        if report.get("capped") is True:
            return StrategyOutcome(
                outcome="claim.withheld",
                outcome_reason=(
                    "scope_budget_exceeded: widening would add more "
                    f"tests than the budget allows ({added_count} > "
                    f"{SCOPE_WIDEN_MAX_NEW_TESTS}); refusing to truncate "
                    "and continue"),
                answered=False,
                evidence_delta={"widen_scope": {"added_count": added_count,
                                                "capped": True}})
        reproduced = report.get(
            "widened_baseline_regens_original_regressions") is True
        if reproduced:
            return StrategyOutcome(
                outcome="claim.confirmed",
                outcome_reason=(
                    "the original regressions all reproduce in the widened "
                    "scope; the original claim stands unchanged"),
                answered=True,
                evidence_delta={
                    "widen_scope": {"added_count": added_count},
                    "original_claim_grade_unchanged": True,
                },
                experiments_added=1)
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason=(
                "the original regressions did not reproduce in the "
                "widened scope; the result is uncertain and needs a "
                "human judgment"),
            answered=True, needs_human=True, experiments_added=1)


class HumanRoutingStrategy:
    """需要解释？不跑实验，诚实路由给人工判断。

    有意不把 differentiated 翻成 True：它没有差异化实验，只有一次
    路由。计划书第八节「断言翻面为七条全 differentiated」与本节
    策略 5 的设计（不跑实验、answered=False）矛盾，以策略 5 的具体
    设计为准——把路由算成实验，才是装了没做的事。
    """

    name = "human_routing"
    differentiated = False
    plan_open: tuple[str, ...] = tuple(HUMAN_ROUTING_OPEN)

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason="requires_human_judgment",
            answered=False,
            open_items=self.plan_open,
            needs_human=True)


class TestVisaStrategy:
    """我有一条新测试？送进既有签证流水线，不造第二条执行路径。

    质疑自带只碰测试文件的 unified diff（计划阶段已过 test-only
    白名单边界）；宿主的 submit_test_for_visa 复用
    test_visa.verify_candidate 跑完整三段签证：基线绿 → 干预后
    断言级失败 → 两次复跑一致 → 保留干预上仍复现。

    红线口径（与 ensure_no_promotion 的关系写死在这里）：
    - 签证 PASS 不升级原主张。outcome 是 claim.confirmed（原主张
      维持原判），evidence_delta 里只追加一条新 claim（"该行现受
      用户提交的测试保护"）并显式声明 original_claim_grade_unchanged；
      追加新主张不是升级原主张，两者不冲突。
    - 签证扣留（WITHHELD_SMALL_CASE / WITHHELD_NO_HOLDOUT /
      PASSED_NOT_EFFECTIVE / VISA_ERROR）原样透出：outcome_reason
      与 open_items 都携带真实签证状态，绝不圆场。
    """

    name = "test_visa"
    differentiated = True
    plan_open: tuple[str, ...] = ()

    def run(self, ctx: ExperimentContext, record: dict) -> StrategyOutcome:
        visa = ctx.submit_test_for_visa(record)
        status = str((visa or {}).get("status") or "VISA_ERROR")
        if status == "VERIFIED_EFFECTIVE":
            return StrategyOutcome(
                outcome="claim.confirmed",
                outcome_reason=(
                    "the user-submitted test earned the retest visa "
                    "(baseline green, assertion-level failure under "
                    "intervention, consistent reruns); the original "
                    "claim keeps its grade and a protected claim is "
                    "added alongside it"),
                answered=True, open_items=(),
                evidence_delta={
                    "new_claim": PROTECTED_CLAIM_TEXT,
                    "new_claim_id": protected_claim_id(
                        str(record.get("reverification_id") or "")),
                    "visa_status": status,
                    "original_claim_grade_unchanged": True},
                experiments_added=1)
        return StrategyOutcome(
            outcome="claim.withheld",
            outcome_reason=f"test_visa_withheld:{status}",
            answered=False,
            open_items=(f"test_visa_withheld:{status}",),
            evidence_delta=None,
            needs_human=True,
            experiments_added=1)


REPLAY_ONLY = ReplayOnlyStrategy()
FLAKY_RERUN = FlakyRerunStrategy()
SOURCE_CHANGED = SourceChangedStrategy()
COVERAGE_CHECK = CoverageCheckStrategy()
COLLECTION_CHECK = CollectionCheckStrategy()
SCOPE_WIDEN = ScopeWidenStrategy()
HUMAN_ROUTING = HumanRoutingStrategy()
TEST_VISA = TestVisaStrategy()

STRATEGIES: dict[str, Strategy] = {
    "test_did_not_execute_code": COVERAGE_CHECK,
    "collection_crash_suspected": COLLECTION_CHECK,
    "flaky_result_suspected": FLAKY_RERUN,
    "source_changed": SOURCE_CHANGED,
    "test_scope_incomplete": SCOPE_WIDEN,
    "new_test_available": TEST_VISA,
    "other_needs_explanation": HUMAN_ROUTING,
}


# 签证 PASS 追加的新主张：固定文本 + 确定性 ID，评论可以再质疑它
# （绑定面由控制面并入 _review_claim_surface）。
PROTECTED_CLAIM_TEXT = "该行现受用户提交的测试保护"


def protected_claim_id(reverification_id: str) -> str:
    """签证 PASS 追加的新主张 ID：<reverification_id>:protected。"""
    return f"{reverification_id}:protected"


def strategy_for(reason: str) -> Strategy:
    """按原因取策略；未知原因在入口已挡，这里再 fail closed。"""
    strategy = STRATEGIES.get(reason)
    if strategy is None:
        raise KeyError(f"no strategy registered for reason {reason!r}")
    return strategy


def plan_fields(reason: str) -> dict:
    """v2 记录在计划阶段的策略字段。

    open 列表按策略如实标注：human_routing 是
    requires_human_judgment；其余六条质疑原因（含 new_test_available
    的签证实验）都有差异化实验，没有挂起的口子。
    """
    strategy = strategy_for(reason)
    return {
        "strategy": strategy.name,
        "answered": False,
        "open": list(getattr(strategy, "plan_open", ())),
        "experiments_added": 0,
        "evidence_delta": None,
        "needs_human": False,
    }


def migrate_v1(record: dict) -> dict:
    """v1 记录读取时补默认值；只补内存视图，绝不重写盘上文件。"""
    merged = dict(record)
    merged.setdefault("strategy", "")
    merged.setdefault("answered", False)
    merged.setdefault("open", [])
    merged.setdefault("experiments_added", 0)
    merged.setdefault("evidence_delta", None)
    merged.setdefault("needs_human", False)
    return merged
