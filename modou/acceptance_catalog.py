"""Unified acceptance-pack catalog (T14 / direction 20).

One aggregated view over every acceptance-pack registration source: version,
input contract, environment conditions, methods, artifacts and maturity.
A pack that has not been through acceptance stays ``not_evaluated`` — the
catalog never promotes it to usable.
"""
from __future__ import annotations

import json
from pathlib import Path

from modou.acceptance_packs import load as load_acceptance_packs

SCHEMA_VERSION = "acceptance-catalog-v1"


class CatalogError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("CATALOG_SOURCE_UNREADABLE", f"{path}: {exc}") from exc


def build_catalog(config_dir: str | Path | None = None) -> dict:
    """Aggregate the machine-readable pack sources into one catalog."""
    base = (Path(config_dir) if config_dir
            else Path(__file__).resolve().parents[1] / "configs")
    entries = []
    for pack in load_acceptance_packs():
        public = pack.public_json()
        evidence = public.get("evidence", [])
        usable = public.get("maturity") == "available" and bool(evidence)
        entries.append({
            "pack_id": public["id"], "title": public.get("title", ""),
            "domain": public.get("domain", ""), "version": "shuimu-acceptance-pack-catalog-v1",
            "maturity": public.get("maturity", "planned"),
            "evidence_kinds": public.get("evidence_kinds", []),
            "input_contract": {"required": public.get("required_inputs", []),
                                "optional": public.get("optional_inputs", [])},
            "methods": public.get("roadmap_directions", []),
            "artifacts": evidence,
            "verdict_domain": public.get("domain", ""),
            "usable": bool(usable),
            "source_kind": "core",
            "boundary": public.get("boundary", ""),
        })
    for source_name, source_path in (("specialty", base / "specialty-packs.json"),
                                     ("domain", base / "domain-packs.json")):
        source = _load_json(source_path)
        for pack in source.get("packs", []):
            pack_id = pack.get("pack_id") or pack.get("id") or ""
            if not pack_id:
                raise CatalogError("CATALOG_PACK_ID_MISSING", source_name)
            artifacts = pack.get("artifacts",
                                 [f"cases:{case_id}" for case_id in
                                  (pack.get("cases") or {}).values()])
            usable = pack.get("maturity") == "available" and bool(artifacts)
            entries.append({
                "pack_id": pack_id, "title": pack.get("title", pack.get("name", pack_id)),
                "domain": pack.get("verdict_domain", ""),
                "version": source.get("schema_version", ""),
                "maturity": pack.get("maturity", "experimental"),
                "evidence_kinds": [pack.get("evidence_kind", "domain_experiment")],
                "input_contract": pack.get("input_contract", pack.get("frozen_rules", {})),
                "methods": pack.get("methods", []),
                "artifacts": artifacts,
                "verdict_domain": pack.get("verdict_domain", ""),
                "usable": bool(usable),
                "source_kind": source_name,
                "boundary": pack.get("conclusion_boundary", ""),
            })
    ids = [e["pack_id"] for e in entries]
    if len(ids) != len(set(ids)):
        raise CatalogError("CATALOG_DUPLICATE_PACK", str(sorted(
            {i for i in ids if ids.count(i) > 1})))
    for entry in entries:
        if entry["maturity"] == "available" and not entry["artifacts"]:
            raise CatalogError("CATALOG_UNEARNED_STATUS", entry["pack_id"])
    return {"schema_version": SCHEMA_VERSION, "packs": entries,
            "counts": {"total": len(entries),
                        "usable": sum(1 for e in entries if e["usable"]),
                        "experimental": sum(1 for e in entries
                                             if e["maturity"] == "experimental")},
            "note": ("未执行验收的包不标记可用；usable 只承认 maturity=available 且"
                     "有原始验收产物引用的包。")}
