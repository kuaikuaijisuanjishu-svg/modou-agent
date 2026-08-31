"""Adapt established line-level observations into the local evidence ledger."""
from __future__ import annotations

from ..models import EvidenceUnit, LineResult
from . import anchors, records
from .records import Record

#: Snapshot label used by source anchors within one local review.
SNAPSHOT_S2 = "S2"

#: Compatibility record kinds retained for the public evidence format.
F_LEGACY_LINE = "LegacyLine"
F_LEGACY_VERDICT = "LegacyVerdict"


def _unit_anchor(u: EvidenceUnit) -> anchors.Anchor:
    """Prefer the anchor captured when the evidence unit was observed."""
    if u.anchor_json:
        return anchors.from_json(u.anchor_json)
    if u.node_type == "file":
        return anchors.file_anchor(SNAPSHOT_S2, u.path, line_end=u.line_end)
    return anchors.unit_anchor(
        SNAPSHOT_S2, u.path, structural_path=u.node_type,
        source=f"{u.path}:{u.line_start}-{u.line_end}:{u.node_type}",
        line_start=u.line_start, line_end=u.line_end)


def mirror(*, run_id: str, units: list[EvidenceUnit],
           lines: list[LineResult]) -> list[Record]:
    """Build compatibility records in memory for the public evidence format."""
    out: list[Record] = []

    for u in units:
        a = _unit_anchor(u)

        # Preserve the observed verdict for the compatibility projection.
        out.append(records.fact(
            F_LEGACY_VERDICT, anchor=a,
            payload={"verdict": u.verdict.value if u.verdict else None,
                     "unit_id": u.unit_id, "note": u.note,
                     "covered_lines": list(u.covered_lines)},
            observer="legacy_engine", run_id=run_id))

    # Preserve line observations as source locations.
    for r in lines:
        out.append(records.fact(
            F_LEGACY_LINE,
            location=anchors.SourceLocation(SNAPSHOT_S2, r.path, r.lineno),
            payload={"label": r.label.value,
                     "reason": r.reason.value if r.reason else None,
                     "unit_id": r.unit_id, "executable": r.executable},
            observer="legacy_engine", run_id=run_id))

    return out
