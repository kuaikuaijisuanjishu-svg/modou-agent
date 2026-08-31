"""引擎 3：承重 / 惰性。HDD-inspired AST 层次化删除探测。

**刻意不叫 HDD。** 完整的 HDD 会在每一层做 ddmin 集合缩减（同层多个节点组合起来试），
这里只做"逐节点删除 + 失败后下钻"。少了那一步就不能借人家的名字——
这跟不许说"安全删除"是同一条纪律。

流程（方案 §6.3）：
  ① 先试较粗的节点
  ② 状态向量完全相同 → 该节点内符合条件的已覆盖行标惰性，**不再下钻**
  ③ 出现回归 → 恢复基线、再跑一次确认、下钻子节点
  ④ 没有更细的子节点了 → 对尚未归因的新增行做单行探测
  ⑤ 语法不合法 / 超时 / 预算耗尽 → 未标注（带具体原因）
  ⑥ 同一文件内，同一删除行集合只入队一次（去重，不是跨文件结果缓存）

「删除 → 跑 → 一律回滚」由 `modou/trial.py` 的 TrialRunner 统一执行，
本文件只负责层次化的下钻策略。回滚这种不能出错的动作只应该有一份实现。

红了之后必须"恢复 → 再跑 → 向量重新与基线逐项一致"才签承重证书；
对不回去就是 flaky_or_dirty_restore，这次实验作废——
"我以为回滚了"和"确实回滚了"是两回事。
"""
from __future__ import annotations

from pathlib import Path

from .. import astnodes
from ..astnodes import Node
from ..budget import Budget
from ..ledger import anchors, claim_builder, observe
from ..models import (EvidenceUnit, Label, TestVector, Transform, Unlabeled,
                      make_unit_id)
from ..trial import TIMEOUT, INVALID, PROJECTS_AS_PROBE_TIMEOUT


def _anchor(path: str, node: Node, src_lines: list[str]) -> anchors.Anchor:
    """用**真实源码切片**做摘要，用真实结构路径做身份。

    早先这里传的是 `f"{path}:{start}-{end}:{node_type}"` 合成串——
    那不是内容寻址，文件顶部插一行就会让同一个 AST 单元换身份，
    而"身份稳定"恰恰是 Anchor 存在的理由。
    """
    body = "\n".join(src_lines[node.start - 1:node.end])
    return anchors.unit_anchor(
        "S2", path, structural_path=node.structural_path or node.node_type,
        source=body, line_start=node.start, line_end=node.end)


def run(ws, adapter, *, path: str, added: set[int], pending: set[int],
        baseline: TestVector, nodeids: list[str], run_dir: Path,
        covered: set[int], budget: Budget, runner
        ) -> tuple[list[EvidenceUnit], dict[int, Unlabeled]]:
    """对一个文件跑层次化探测。

    pending：还需要归因的行（被执行过、暂时挂 NOT_ISOLATED 的）。
    返回 (证据单元, {行号: 未标注原因})。
    """
    units: list[EvidenceUnit] = []
    unresolved: dict[int, Unlabeled] = {}
    seen: set[tuple[int, ...]] = set()

    def _bail(reason: Unlabeled):
        un = {ln: reason for ln in pending}
        if un:
            runner.collector.append(
                observe.probe_outcome(path, un, run_id=runner.run_id))
        return units, un

    try:
        source = ws.read(path)
    except OSError:
        return _bail(Unlabeled.NO_VALID_TRANSFORM)

    src_lines = source.splitlines()
    queue: list[Node] = astnodes.candidates(source, added)
    if not queue:
        return _bail(Unlabeled.NO_VALID_TRANSFORM)

    resolved: set[int] = set()

    def remaining() -> set[int]:
        return pending - resolved

    while queue and remaining():
        node = queue.pop(0)
        if not (set(node.span) & remaining()):
            continue                                   # 这块已经归因完了
        key = node.lines
        if key in seen:
            continue
        seen.add(key)

        if not budget.can_probe():
            break

        a = _anchor(path, node, src_lines)
        tag = f"{path.replace('/', '__')}__{key[0]}_{key[-1]}"
        out = runner.delete_lines(
            path, key, anchor=a, deadline=budget.probe_deadline(),
            junit_name=f"probe__{tag}.xml", on_spend=budget.spend)

        if out.status == INVALID:
            if node.children:
                queue = node.children + queue
            else:
                for ln in set(node.span) & remaining():
                    unresolved[ln] = Unlabeled.NO_VALID_TRANSFORM
                    resolved.add(ln)
            continue

        if out.status == TIMEOUT:
            for ln in set(node.span) & remaining():
                unresolved[ln] = Unlabeled.PROBE_TIMEOUT
                resolved.add(ln)
            continue

        if not out.ran:
            # JUnit 无法解析、观测协议失败等都没有产生可采信向量，不能把
            # ``vector=None`` 当成一次回归交给 ClaimBuilder。
            reason = (Unlabeled.PROBE_TIMEOUT
                      if out.status in PROJECTS_AS_PROBE_TIMEOUT
                      else Unlabeled.NO_VALID_TRANSFORM)
            for ln in set(node.span) & remaining():
                unresolved[ln] = reason
                resolved.add(ln)
            continue

        vector, m = out.vector, out.mutation
        unit = EvidenceUnit(
            unit_id=make_unit_id(path, key, node.node_type),
            path=path, line_start=node.start, line_end=node.end,
            node_type=node.node_type,
            transform=Transform(deleted_lines=m.deleted,
                                pass_inserted_at=m.pass_inserted_at),
            baseline=baseline, mutated=vector,
            covered_lines=tuple(sorted(set(node.span) & covered)),
            seconds=out.seconds,
            experiment_id=out.experiment_id, fact_ids=out.fact_ids,
            anchor_json=a.to_json())

        if vector is not None and vector.identical_to(baseline):
            # ② 未观察到变化 —— 该节点内已覆盖的行标惰性，不再下钻
            unit.verdict = Label.INERT
            units.append(unit)
            resolved |= set(node.span) & remaining()
            continue

        # ③ 出现回归：先确认回滚干净，再决定下钻还是定案
        if not budget.can_probe():
            for ln in set(node.span) & remaining():
                unresolved[ln] = Unlabeled.BUDGET_EXHAUSTED
                resolved.add(ln)
            break
        # 还原之后再跑一次确认——这不是干预，是独立观测，所以走 observe。
        unit.restored, back_fid = runner.observe(
            a, "restored", deadline=budget.probe_deadline(),
            junit_name="restore_check.xml")
        if back_fid:
            unit.fact_ids = unit.fact_ids + (back_fid,)
        budget.spend()

        if not unit.restore_is_clean():
            for ln in set(node.span) & remaining():
                unresolved[ln] = Unlabeled.FLAKY_OR_DIRTY_RESTORE
                resolved.add(ln)
            continue

        if node.children:
            queue = node.children + queue              # 下钻
            continue

        # ④ 已经是最细的 AST 节点：这就是最小已隔离单元
        decision = observe.probe_decision(
            a, experiment_id=out.experiment_id, run_id=runner.run_id)
        runner.collector.append(decision)
        claim = claim_builder.required_by_test(
            runner.collector, decision_id=decision.record_id,
            restored_fact_id=back_fid, run_id=runner.run_id)
        runner.collector.append(claim)
        unit.verdict = Label.LOAD_BEARING
        units.append(unit)
        resolved |= set(node.span) & remaining()

    # ⑤ 预算或队列耗尽后仍未归因的
    for ln in remaining():
        unresolved[ln] = (Unlabeled.BUDGET_EXHAUSTED if not budget.can_probe()
                          else Unlabeled.NOT_ISOLATED)
    if unresolved:
        runner.collector.append(
            observe.probe_outcome(path, unresolved, run_id=runner.run_id))
    return units, unresolved
