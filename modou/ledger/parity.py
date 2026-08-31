"""Check that ledger-derived projections match the displayed line results.

The check covers serialization, provenance, and derived-result consistency.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Label, LineResult, Unlabeled
from . import derive, store
from .legacy import F_LEGACY_LINE
from .records import FACT, Record


@dataclass
class ParityResult:
    ok: bool
    checked: int = 0
    diffs: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"projection_parity 通过（{self.checked} 行逐行一致）"
        head = f"projection_parity 失败：{len(self.diffs)} 处不一致"
        return head + "\n  · " + "\n  · ".join(self.diffs[:8])


def project(rows: list[dict]) -> list[LineResult]:
    """账本 → 逐行结果。**只做 join 与渲染，没有任何写入或推断。**"""
    out: list[LineResult] = []
    for r in rows:
        if r["record_type"] != FACT:
            continue
        p = r["payload"]
        if p.get("kind") != F_LEGACY_LINE:
            continue
        loc = p["location"]
        d = p["data"]
        out.append(LineResult(
            path=loc["path"], lineno=loc["lineno"],
            label=Label(d["label"]),
            reason=Unlabeled(d["reason"]) if d.get("reason") else None,
            unit_id=d.get("unit_id"), executable=d.get("executable", True)))
    return sorted(out, key=lambda x: (x.path, x.lineno))


def check(recs: list[Record], lines: list[LineResult]) -> ParityResult:
    """比对内存里的账本记录与旧输出。发布之前跑，不是发布之后。"""
    rows = [r.to_json() for r in recs]
    projected = project(rows)
    expected = sorted(lines, key=lambda x: (x.path, x.lineno))

    diffs: list[str] = []
    if len(projected) != len(expected):
        diffs.append(f"行数不同：账本 {len(projected)} ≠ 旧输出 {len(expected)}")
    for a, b in zip(projected, expected):
        if (a.path, a.lineno) != (b.path, b.lineno):
            diffs.append(f"位置错位：{a.path}:{a.lineno} ≠ {b.path}:{b.lineno}")
            break
        if a.label is not b.label:
            diffs.append(f"{a.path}:{a.lineno} 标签 {a.label.value} ≠ {b.label.value}")
        if a.reason is not b.reason:
            diffs.append(f"{a.path}:{a.lineno} 原因 {a.reason} ≠ {b.reason}")
        if a.unit_id != b.unit_id:
            diffs.append(f"{a.path}:{a.lineno} unit_id {a.unit_id} ≠ {b.unit_id}")
    return ParityResult(ok=not diffs, checked=len(expected), diffs=diffs)


def check_file(path, lines: list[LineResult]) -> ParityResult:
    """对已落盘的账本做同样的比对（离线核验用）。"""
    return check_rows(store.read(path), lines)


def check_rows(rows: list[dict], lines: list[LineResult]) -> ParityResult:
    projected = project(rows)
    expected = sorted(lines, key=lambda x: (x.path, x.lineno))
    diffs = []
    if len(projected) != len(expected):
        diffs.append(f"行数不同：账本 {len(projected)} ≠ 旧输出 {len(expected)}")
    for a, b in zip(projected, expected):
        if (a.path, a.lineno, a.label, a.reason, a.unit_id) != \
           (b.path, b.lineno, b.label, b.reason, b.unit_id):
            diffs.append(f"{a.path}:{a.lineno} 与旧输出不一致")
    return ParityResult(ok=not diffs, checked=len(expected), diffs=diffs)


# ---------------------------------------------------------------- M4

def check_derived(recs: list[Record], lines: list[LineResult], *,
                  three_state: bool = True) -> ParityResult:
    """**M4 的真正验收**：从账本推导出来的结论，与旧引擎是否一致。

    与 `check()` 的区别是本质性的：
    `check()` 比的是脚手架搬进去又搬出来有没有变形（round-trip 保真）；
    这里比的是**新推导**与旧引擎在同一批行上给不给出同一结论。
    对不上不是序列化 bug，是两套逻辑真的有分歧——那正是要学的东西。
    """
    rows = [r.to_json() for r in recs]
    derived = [d.as_line_result() for d in
               derive.derive(rows, three_state=three_state)]
    expected = sorted(lines, key=lambda r: (r.path, r.lineno))

    diffs: list[str] = []
    if len(derived) != len(expected):
        diffs.append(f"行数不同：推导 {len(derived)} ≠ 旧引擎 {len(expected)}")
    for a, b in zip(derived, expected):
        if (a.path, a.lineno) != (b.path, b.lineno):
            diffs.append(f"位置错位：{a.path}:{a.lineno} ≠ {b.path}:{b.lineno}")
            break
        if a.label is not b.label:
            diffs.append(
                f"{a.path}:{a.lineno} 推导 {a.label.value} ≠ 旧引擎 {b.label.value}")
        elif a.reason is not b.reason:
            diffs.append(
                f"{a.path}:{a.lineno} 原因 推导 {a.reason} ≠ 旧引擎 {b.reason}")
    return ParityResult(ok=not diffs, checked=len(expected), diffs=diffs)


def evidence_coverage(recs: list[Record], *, three_state: bool = True) -> dict:
    """有多少行的结论真的挂上了证据 ID。

    M2.1 时这个数是 4.1%——其余全靠脚手架。它是「证据编译」这句话
    能不能成立的直接度量，所以要能随时算出来。
    """
    rows = [r.to_json() for r in recs]
    ds = derive.derive(rows, three_state=three_state)
    n = len(ds)
    with_ev = sum(1 for d in ds if d.evidence_ids)
    labeled = [d for d in ds if d.label is not Label.UNLABELED]
    return {"lines": n, "with_evidence": with_ev,
            # 空补丁没有任何无证据行；完备覆盖在空集上成立。
            "ratio": (with_ev / n) if n else 1.0,
            "labeled": len(labeled),
            "labeled_with_evidence": sum(1 for d in labeled if d.evidence_ids)}
