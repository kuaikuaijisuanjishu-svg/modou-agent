"""账本级校验：引用完整性、一致性、完整性。

**为什么单靠 `claim()` 的构造期检查不够。**

`records.claim()` 只能看见自己那一条记录，所以它只能检查
"provenance 字段非空"。它拦不住这个：

    provenance=["not-a-record"]      # 构造成功，但那个 ID 根本不存在

于是「没有 Observation 就没有 Claim」在构造期只成立了一半——
准确说法是"没有非空 provenance 字段就不能构造 Claim"。
要让另一半成立，必须有人在**看得见整本账**的位置上检查引用真的落地。
那就是这里。

校验在 `LedgerComplete` 之前统一执行，**任一失败即不得发布**——
与 parity、coverage 自检同一条纪律：失败关闭，不降级。
"""
from __future__ import annotations

from .records import (CLAIM, COMPLETE, EXPERIMENT, FACT, LEDGER_COMPLETE,
                      SCHEMA_VERSION, Record, _rid)


class LedgerInvalid(RuntimeError):
    """账本自身不自洽。这次运行的账本不可发布，也不可采信。"""


def _rows(records: list) -> list[dict]:
    return [r.to_json() if isinstance(r, Record) else r for r in records]


def validate(records: list, *, run_id: str | None = None) -> list[str]:
    """返回问题清单。空列表 = 通过。

    不抛异常，一次把问题列全——一次只报一个会让人来回试。
    """
    rows = _rows(records)
    problems: list[str] = []
    by_id: dict[str, dict] = {}

    for r in rows:
        rid = r.get("record_id", "")
        if r.get("schema_version") != SCHEMA_VERSION:
            problems.append(
                f"{rid[:8]}：schema_version {r.get('schema_version')} "
                f"≠ 当前 {SCHEMA_VERSION}")
        # record_id 必须能由内容重算出来，否则它不是内容寻址，只是个标签
        want = _rid(r.get("record_type", ""), r.get("payload", {}))
        if rid != want:
            problems.append(f"{rid[:8]}：record_id 与内容不符（应为 {want[:8]}）")
        if rid in by_id:
            problems.append(f"{rid[:8]}：record_id 重复")
        by_id[rid] = r

    run_ids = {r.get("run_id") for r in rows}
    if len(run_ids) > 1:
        problems.append(f"同一份账本里混了多个 run_id：{sorted(run_ids)}")
    elif run_id is not None and run_ids and run_ids != {run_id}:
        problems.append(f"run_id 不符：账本 {run_ids} ≠ 预期 {run_id}")

    for r in rows:
        if r.get("record_type") != CLAIM:
            continue
        p = r["payload"]
        rid = r["record_id"]
        prov = p.get("provenance") or []
        if not prov:
            problems.append(f"{rid[:8]}：主张没有 provenance")
            continue
        anchor = p.get("anchor") or {}
        matched_anchor = False
        for pid in prov:
            src = by_id.get(pid)
            if src is None:
                problems.append(
                    f"{rid[:8]}：provenance 指向不存在的记录 {pid[:8]}")
                continue
            if src["record_type"] not in (FACT, EXPERIMENT):
                problems.append(
                    f"{rid[:8]}：provenance 指向 {src['record_type']}，"
                    f"只能指向 Fact 或 Experiment")
                continue
            # 实验没跑完就什么都没观测到。TIMEOUT/INVALID/ABORTED 的实验
            # 记在账本里是对的（真实发生了什么），但它撑不起任何主张——
            # 「删掉它，test_x 就红」这句话必须来自一次真的跑完的实验。
            # 构造期看不见这一点：claim() 只能检查 provenance 字段非空。
            if src["record_type"] == EXPERIMENT and \
                    src["payload"].get("status") != COMPLETE:
                problems.append(
                    f"{rid[:8]}：provenance 指向状态为 "
                    f"{src['payload'].get('status')} 的实验——"
                    f"没跑完的实验什么都没观测到，撑不起主张")
            if src.get("run_id") != r.get("run_id"):
                problems.append(
                    f"{rid[:8]}：provenance 跨了 run_id（{src.get('run_id')}）")
            sa = (src["payload"].get("anchor") or {})
            if sa.get("aid") and anchor.get("aid") and sa["aid"] == anchor["aid"]:
                matched_anchor = True
            # 快照必须一致：S1 上的观测撑不起 S2 上的主张
            if sa.get("snapshot_id") and anchor.get("snapshot_id") and \
                    sa["snapshot_id"] != anchor["snapshot_id"]:
                problems.append(
                    f"{rid[:8]}：来源快照 {sa['snapshot_id']} "
                    f"≠ 主张快照 {anchor['snapshot_id']}")
        if prov and not matched_anchor:
            problems.append(
                f"{rid[:8]}：没有任何来源挂在与主张相同的 Anchor 上，"
                f"主张与证据对不上")
        scope_snap = (p.get("scope") or {}).get("snapshot")
        if scope_snap and anchor.get("snapshot_id") and \
                scope_snap != anchor["snapshot_id"]:
            problems.append(
                f"{rid[:8]}：scope 快照 {scope_snap} ≠ Anchor 快照 "
                f"{anchor['snapshot_id']}")
    return problems


def check_complete(rows: list[dict]) -> list[str]:
    """校验收尾标记与计数。`rows` 是含 LedgerComplete 的完整清单。"""
    problems: list[str] = []
    if not rows or rows[-1].get("record_type") != LEDGER_COMPLETE:
        problems.append(f"账本没有 {LEDGER_COMPLETE} 收尾标记")
        return problems
    declared = rows[-1]["payload"].get("counts") or {}
    actual: dict[str, int] = {}
    for r in rows[:-1]:
        k = r.get("record_type", "?")
        actual[k] = actual.get(k, 0) + 1
    if declared != actual:
        problems.append(f"收尾计数与实际不符：声明 {declared} ≠ 实际 {actual}")
    return problems


def raise_if_bad(problems: list[str], what: str = "账本") -> None:
    if problems:
        raise LedgerInvalid(
            f"{what}校验未通过（{len(problems)} 项）：\n  · "
            + "\n  · ".join(problems[:10]))
