"""Evidence-compiled narration: models select layout, never factual prose."""
from __future__ import annotations

from dataclasses import dataclass


TEMPLATE_VERSION = "evidence-narrator-v1"
BANNED = ("安全删除", "语义等价", "已证明无用", "可以放心删除", "完整 HDD")


class NarrationRejected(ValueError):
    pass


@dataclass(frozen=True)
class NarrationLayout:
    claim_order: tuple[str, ...]
    style: str = "concise"
    include_scope_first: bool = True

    @classmethod
    def parse(cls, raw: dict, valid_claim_ids: set[str]) -> "NarrationLayout":
        if not isinstance(raw, dict):
            raise NarrationRejected("layout must be an object")
        allowed = {"claim_order", "style", "include_scope_first", "suggestions"}
        if set(raw) - allowed:
            raise NarrationRejected("layout contains unsupported fields")
        order = raw.get("claim_order") or []
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            raise NarrationRejected("claim_order must be a string array")
        if len(order) != len(set(order)) or set(order) != valid_claim_ids:
            raise NarrationRejected("claim_order must contain every valid claim exactly once")
        suggestions = raw.get("suggestions") or []
        if not isinstance(suggestions, list) or not all(isinstance(x, str) for x in suggestions):
            raise NarrationRejected("suggestions must be a string array")
        for text in suggestions:
            if any(term in text for term in BANNED):
                raise NarrationRejected("suggestion contains a forbidden claim")
        return cls(tuple(order), str(raw.get("style") or "concise")[:30],
                   bool(raw.get("include_scope_first", True)))


def deterministic_layout(claim_ids: list[str]) -> NarrationLayout:
    return NarrationLayout(tuple(sorted(claim_ids)))


def compile_narration(*, claims: list[dict], evidence_ids: set[str],
                      layout: NarrationLayout | None = None,
                      scope_note: str = "结论仅适用于本次声明测试范围。") -> dict:
    claim_by_id = {str(c.get("record_id") or ""): c for c in claims}
    if "" in claim_by_id or len(claim_by_id) != len(claims):
        raise NarrationRejected("claim records need unique record_id")
    for claim in claims:
        provenance = claim.get("provenance") or []
        if not provenance or any(p not in evidence_ids for p in provenance):
            raise NarrationRejected("claim provenance is missing from evidence")
    selected = layout or deterministic_layout(list(claim_by_id))
    if set(selected.claim_order) != set(claim_by_id):
        raise NarrationRejected("layout references unavailable claims")
    blocks = []
    for claim_id in selected.claim_order:
        claim = claim_by_id[claim_id]
        claim_type = str(claim.get("claim_type") or claim.get("type") or "")
        anchor = claim.get("anchor") or {}
        path = anchor.get("path") or claim.get("path") or "未知路径"
        if claim_type == "RequiredByTest":
            text = f"在声明测试范围内，干预 {path} 后观察到具名测试回归。"
        else:
            text = f"证据账本记录了 {path} 的 {claim_type or '结构化'} 主张。"
        if any(term in text for term in BANNED):
            raise NarrationRejected("compiled template contains forbidden phrase")
        blocks.append({"claim_id": claim_id, "evidence_ids": claim["provenance"],
                       "text": text, "template_version": TEMPLATE_VERSION})
    return {"template_version": TEMPLATE_VERSION, "scope_note": scope_note,
            "style": selected.style, "blocks": blocks}
