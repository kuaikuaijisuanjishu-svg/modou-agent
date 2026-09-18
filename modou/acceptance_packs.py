"""Machine-readable, fail-closed acceptance-pack contracts.

An acceptance pack describes which *kind* of evidence a domain can consume and
what a caller must provide before an evidence task is created.  It is an intake
contract, not an evaluator: a successful assessment means only that the
request is shaped for a known pack.  It never turns user-supplied ``status``,
``verdict`` or ``outcome`` fields into evidence, and it never runs a command.

The catalog is deliberately small and versioned.  New domains can be added as
``planned`` entries without making the runtime pretend that an implementation
exists.  ``experimental`` entries may be selected, but their maturity is
returned with every public projection so a caller cannot silently present them
as the core Python evidence path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "shuimu-acceptance-pack-catalog-v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "configs" / "acceptance-packs.json"
MATURITIES = frozenset({"available", "experimental", "planned"})
MATURITY_LABELS = {
    "available": "可用",
    "experimental": "实验性",
    "planned": "计划中",
}

# The names are stable IDs used by the roadmap and are intentionally kept
# separate from prose shown in the UI.  Every current roadmap direction must
# belong to at least one pack; a missing direction is a catalog error.
ROADMAP_DIRECTIONS = (
    "multilingual_adapters",
    "full_stack_behavior",
    "interface_contracts",
    "requirements_contract",
    "rule_protection",
    "evidence_version_binding",
    "project_memory",
    "regression_protection",
    "task_triggers",
    "multi_agent_coordination",
    "budget_scheduling",
    "portable_receipts_sdk",
    "research_workflows",
    "sql",
    "hardware",
    "teaching",
    "agent_behavior",
    "experiment_methods",
    "domain_acceptance_packs",
    "browser_evidence",
)

# Criterion fields are the only fields this layer may inspect.  Runtime
# services still own path, authorization and execution validation.
CRITERION_FIELDS = frozenset({
    "text", "evidence_kind", "finding_ids", "profile_id", "language",
    "adapter_id", "test_targets", "adoption_mode",
})
# These fields are never accepted from a task creator.  They are deliberately
# listed even though they are not valid criterion fields, so an attempted
# self-attestation gets a specific, reviewable error.
SELF_ATTESTED_FIELDS = frozenset({
    "status", "outcome", "verdict", "result", "passed", "evidence",
    "receipt", "command", "argv", "exit_code",
})


class AcceptancePackError(ValueError):
    """The catalog or a requested pack is malformed."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AcceptancePack:
    id: str
    title: str
    domain: str
    maturity: str
    summary: str
    roadmap_directions: tuple[str, ...]
    evidence_kinds: tuple[str, ...]
    required_inputs: tuple[str, ...]
    optional_inputs: tuple[str, ...]
    human_gate: str
    boundary: str
    evidence: tuple[str, ...]

    @property
    def maturity_label(self) -> str:
        return MATURITY_LABELS[self.maturity]

    @property
    def allowed_inputs(self) -> frozenset[str]:
        return frozenset((*self.required_inputs, *self.optional_inputs))

    def public_json(self) -> dict[str, Any]:
        """Return the safe catalog projection exposed to local clients.

        The catalog does not expose an executor, command or a way to assert an
        outcome.  Repository-relative evidence paths are descriptive only;
        their existence is checked by ``missing_evidence`` before a release.
        """
        return {
            "id": self.id,
            "title": self.title,
            "domain": self.domain,
            "maturity": self.maturity,
            "maturity_label": self.maturity_label,
            "summary": self.summary,
            "roadmap_directions": list(self.roadmap_directions),
            "evidence_kinds": list(self.evidence_kinds),
            "required_inputs": list(self.required_inputs),
            "optional_inputs": list(self.optional_inputs),
            "human_gate": self.human_gate,
            "boundary": self.boundary,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class PackAssessment:
    """A structural intake result; no evidence conclusion is included."""

    pack_id: str
    intake_status: str
    maturity: str
    evidence_status: str
    missing_inputs: tuple[str, ...] = ()
    unsupported_fields: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "intake_status": self.intake_status,
            "maturity": self.maturity,
            "evidence_status": self.evidence_status,
            "missing_inputs": list(self.missing_inputs),
            "unsupported_fields": list(self.unsupported_fields),
            "reason_codes": list(self.reason_codes),
        }


def _bounded_string(value: object, *, field: str, maximum: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"{field} must be a bounded non-empty string")
    return value.strip()


def _string_list(value: object, *, field: str, maximum: int = 50,
                 item_maximum: int = 200, allow_empty: bool = False) -> tuple[str, ...]:
    if (not isinstance(value, list) or (not allow_empty and not value)
            or len(value) > maximum):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or not item.strip() or len(item) > item_maximum
           for item in value):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"{field} contains an invalid string")
    return tuple(item.strip() for item in value)


def _path_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"{field} must be repository-relative paths")
    paths: list[str] = []
    for item in value:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"{field} escapes repository: {item}")
        paths.append(item)
    return tuple(paths)


def _pack(raw: object) -> AcceptancePack:
    if not isinstance(raw, dict):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", "each acceptance pack must be an object")
    required = {
        "id", "title", "domain", "maturity", "summary", "roadmap_directions",
        "evidence_kinds", "required_inputs", "optional_inputs", "human_gate",
        "boundary", "evidence",
    }
    if set(raw) != required:
        missing = sorted(required - set(raw))
        extra = sorted(set(raw) - required)
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID",
                                  f"pack fields mismatch; missing={missing}, extra={extra}")
    identifier = _bounded_string(raw["id"], field="id", maximum=80)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", identifier):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"unstable pack id: {identifier!r}")
    maturity = _bounded_string(raw["maturity"], field=f"{identifier}.maturity", maximum=20)
    if maturity not in MATURITIES:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"unknown maturity: {maturity}")
    directions = _string_list(raw["roadmap_directions"], field=f"{identifier}.roadmap_directions",
                              maximum=len(ROADMAP_DIRECTIONS), item_maximum=80)
    if any(item not in ROADMAP_DIRECTIONS for item in directions):
        unknown = sorted(set(directions) - set(ROADMAP_DIRECTIONS))
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"unknown roadmap directions: {unknown}")
    evidence_kinds = _string_list(raw["evidence_kinds"], field=f"{identifier}.evidence_kinds",
                                  maximum=20, item_maximum=80)
    inputs = (*_string_list(raw["required_inputs"], field=f"{identifier}.required_inputs",
                            maximum=len(CRITERION_FIELDS), item_maximum=80),
              *_string_list(raw["optional_inputs"], field=f"{identifier}.optional_inputs",
                            maximum=len(CRITERION_FIELDS), item_maximum=80,
                            allow_empty=True))
    unknown_inputs = sorted(set(inputs) - CRITERION_FIELDS)
    if unknown_inputs:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"unknown criterion inputs: {unknown_inputs}")
    if len(set(inputs)) != len(inputs):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"duplicate criterion input in {identifier}")
    if not set(raw["required_inputs"]) <= set(inputs):  # defensive; already checked above
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"required input mismatch in {identifier}")
    evidence = _path_list(raw["evidence"], field=f"{identifier}.evidence")
    if maturity == "available" and not evidence:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"available pack lacks evidence: {identifier}")
    return AcceptancePack(
        id=identifier,
        title=_bounded_string(raw["title"], field=f"{identifier}.title"),
        domain=_bounded_string(raw["domain"], field=f"{identifier}.domain", maximum=100),
        maturity=maturity,
        summary=_bounded_string(raw["summary"], field=f"{identifier}.summary"),
        roadmap_directions=directions,
        evidence_kinds=evidence_kinds,
        required_inputs=tuple(raw["required_inputs"]),
        optional_inputs=tuple(raw["optional_inputs"]),
        human_gate=_bounded_string(raw["human_gate"], field=f"{identifier}.human_gate"),
        boundary=_bounded_string(raw["boundary"], field=f"{identifier}.boundary"),
        evidence=evidence,
    )


def load(path: Path | str | None = None) -> tuple[AcceptancePack, ...]:
    source = Path(path) if path is not None else DEFAULT_PATH
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_UNREADABLE", str(exc)) from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", "unsupported acceptance pack schema")
    packs_raw = raw.get("packs")
    if not isinstance(packs_raw, list) or not packs_raw:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", "acceptance pack catalog is empty")
    packs = tuple(_pack(item) for item in packs_raw)
    ids = [item.id for item in packs]
    if len(ids) != len(set(ids)):
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", "pack ids must be unique")
    covered = {direction for item in packs for direction in item.roadmap_directions}
    missing = sorted(set(ROADMAP_DIRECTIONS) - covered)
    if missing:
        raise AcceptancePackError("ACCEPTANCE_CATALOG_INVALID", f"roadmap directions are uncovered: {missing}")
    return packs


def by_id(packs: tuple[AcceptancePack, ...]) -> dict[str, AcceptancePack]:
    return {item.id: item for item in packs}


def get(pack_id: str, *, packs: tuple[AcceptancePack, ...] | None = None) -> AcceptancePack:
    if not isinstance(pack_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id):
        raise AcceptancePackError("ACCEPTANCE_PACK_NOT_FOUND", "unknown acceptance pack")
    item = by_id(packs if packs is not None else load()).get(pack_id)
    if item is None:
        raise AcceptancePackError("ACCEPTANCE_PACK_NOT_FOUND", pack_id)
    return item


def missing_evidence(packs: tuple[AcceptancePack, ...], *, repo: Path) -> list[str]:
    """Return missing catalog evidence paths without changing pack maturity."""
    root = Path(repo).resolve()
    missing: list[str] = []
    for item in packs:
        for relative in item.evidence:
            candidate = root / relative
            if not candidate.is_file():
                missing.append(relative)
    return sorted(set(missing))


def assess_criterion(pack_id: str, criterion: Mapping[str, Any], *,
                     packs: tuple[AcceptancePack, ...] | None = None) -> PackAssessment:
    """Assess only whether a criterion fits a pack's input contract.

    ``intake_status=ready`` is intentionally weaker than task support: the
    server still verifies repository identity, authorization, target paths and
    actual execution.  Every result explicitly carries
    ``evidence_status=not_evaluated``.
    """
    pack = get(pack_id, packs=packs)
    if not isinstance(criterion, Mapping):
        raise AcceptancePackError("ACCEPTANCE_CRITERION_INVALID", "criterion must be an object")
    keys = {str(key) for key in criterion}
    self_attested = sorted(keys & SELF_ATTESTED_FIELDS)
    unsupported = sorted(keys - pack.allowed_inputs - SELF_ATTESTED_FIELDS)
    if self_attested:
        return PackAssessment(pack.id, "invalid", pack.maturity, "not_evaluated",
                              unsupported_fields=tuple(self_attested),
                              reason_codes=("SELF_ATTESTATION_FORBIDDEN",))
    if unsupported:
        return PackAssessment(pack.id, "invalid", pack.maturity, "not_evaluated",
                              unsupported_fields=tuple(unsupported),
                              reason_codes=("CRITERION_FIELD_UNSUPPORTED",))
    missing = tuple(field for field in pack.required_inputs
                    if field not in criterion or criterion[field] in (None, "", []))
    if missing:
        return PackAssessment(pack.id, "incomplete", pack.maturity, "not_evaluated",
                              missing_inputs=missing,
                              reason_codes=("REQUIRED_INPUT_MISSING",))
    kind = criterion.get("evidence_kind")
    if not isinstance(kind, str) or kind not in pack.evidence_kinds:
        return PackAssessment(pack.id, "unsupported", pack.maturity, "not_evaluated",
                              reason_codes=("EVIDENCE_KIND_UNSUPPORTED",))
    for field in ("text", "language", "profile_id", "adapter_id"):
        value = criterion.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 2000):
            return PackAssessment(pack.id, "invalid", pack.maturity, "not_evaluated",
                                  reason_codes=(f"{field.upper()}_INVALID",))
    for field in ("finding_ids", "test_targets"):
        value = criterion.get(field)
        if value is not None and (not isinstance(value, list) or len(value) > 50
                                  or any(not isinstance(item, str) or not item.strip() or len(item) > 300
                                         for item in value)):
            return PackAssessment(pack.id, "invalid", pack.maturity, "not_evaluated",
                                  reason_codes=(f"{field.upper()}_INVALID",))
    if pack.maturity == "planned":
        return PackAssessment(pack.id, "unsupported", pack.maturity, "not_evaluated",
                              reason_codes=("PACK_NOT_IMPLEMENTED",))
    return PackAssessment(pack.id, "ready", pack.maturity, "not_evaluated",
                          reason_codes=("INPUT_CONTRACT_ACCEPTED",))


def public_catalog(packs: tuple[AcceptancePack, ...] | None = None) -> dict[str, Any]:
    items = packs if packs is not None else load()
    counts: dict[str, int] = {}
    for item in items:
        counts[item.maturity] = counts.get(item.maturity, 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "packs": [item.public_json() for item in items],
        "counts": dict(sorted(counts.items())),
        "evidence_boundary": "结构适配检查不等于证据产生、任务通过或领域能力已实现。",
    }
