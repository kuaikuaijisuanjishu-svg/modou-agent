"""Review Bundle v2 sealing and deterministic, model-free verification."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .modes import EXECUTION_MODES, ISOLATION_MODES, SCHEDULER_MODES
from .sensitive import scan_text

SCHEMA_VERSION = "review-bundle-v2"
REQUIRED = frozenset({
    "schema_version", "review_id", "request", "plan", "events",
    "scheduler_trace", "evidence_run_id", "evidence_bundle", "provider",
    "model_metrics", "integrity", "execution_mode", "scheduler_mode",
    "isolation_mode", "restore_state", "evaluation_context",
})
HEX_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$", re.I)
FORBIDDEN_CLAIM_KINDS = frozenset({
    "SafeToDelete", "CanDelete", "DeleteRecommended", "Inert", "Lazy",
    "惰性", "可以删除", "建议删除",
})


@dataclass(frozen=True)
class Problem:
    code: str
    location: str
    detail: str

    def to_json(self) -> dict:
        return {"code": self.code, "location": self.location,
                "detail": self.detail}


@dataclass(frozen=True)
class Verification:
    ok: bool
    bundle_sha256: str
    checks: int
    problems: tuple[Problem, ...]

    def to_json(self) -> dict:
        return {
            "schema_version": "bundle-verification-v1",
            "ok": self.ok,
            "bundle_sha256": self.bundle_sha256,
            "checks": self.checks,
            "problems": [p.to_json() for p in self.problems],
        }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def canonical_bundle_sha256(bundle: dict) -> str:
    """Hash a bundle while excluding only its self-referential digest field."""
    payload = copy.deepcopy(bundle)
    integrity = payload.get("integrity")
    if isinstance(integrity, dict):
        if "payload_sha256" in integrity:
            integrity["payload_sha256"] = ""
        else:  # compatibility for early v2 development bundles
            integrity.pop("bundle_sha256", None)
    return hashlib.sha256(_canonical(payload)).hexdigest()


def seal_bundle(bundle: dict, *, execution_mode: str, scheduler_mode: str,
                isolation_mode: str, restore_state: dict,
                evaluation_context: dict) -> dict:
    """Return a v2 copy with a canonical integrity seal; never mutates input."""
    out = copy.deepcopy(bundle)
    out["schema_version"] = SCHEMA_VERSION
    out["execution_mode"] = execution_mode
    out["scheduler_mode"] = scheduler_mode
    out["isolation_mode"] = isolation_mode
    out["restore_state"] = copy.deepcopy(restore_state)
    out["evaluation_context"] = copy.deepcopy(evaluation_context)
    out["integrity"] = {
        "algorithm": "sha256-canonical-json-v1",
        "event_count": len(out.get("events") or []),
        "event_chain_sha256": hashlib.sha256(_canonical(out.get("events") or [])).hexdigest(),
        "payload_sha256": "",
    }
    out["integrity"]["payload_sha256"] = canonical_bundle_sha256(out)
    return out


def _records(bundle: dict) -> list[dict]:
    evidence = bundle.get("evidence_bundle") or {}
    rows = evidence.get("ledger") or [] if isinstance(evidence, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def verify(bundle: dict) -> Verification:
    problems: list[Problem] = []
    checks = 0

    def require(ok: bool, code: str, location: str, detail: str) -> None:
        nonlocal checks
        checks += 1
        if not ok:
            problems.append(Problem(code, location, detail))

    require(isinstance(bundle, dict), "BUNDLE_TYPE", "$", "bundle must be an object")
    if not isinstance(bundle, dict):
        return Verification(False, "", checks, tuple(problems))
    require(bundle.get("schema_version") == SCHEMA_VERSION, "SCHEMA_VERSION",
            "schema_version", f"expected {SCHEMA_VERSION}")
    for key in sorted(REQUIRED):
        require(key in bundle, "FIELD_MISSING", key, "required v2 field is absent")

    review_id = bundle.get("review_id")
    events = bundle.get("events")
    require(isinstance(events, list) and bool(events), "EVENTS_EMPTY", "events",
            "events must be a non-empty array")
    if isinstance(events, list):
        for expected, event in enumerate(events, 1):
            loc = f"events[{expected - 1}]"
            require(isinstance(event, dict), "EVENT_TYPE", loc, "event must be an object")
            if not isinstance(event, dict):
                continue
            require(event.get("seq") == expected, "EVENT_SEQUENCE", f"{loc}.seq",
                    f"expected contiguous sequence {expected}")
            require(event.get("review_id") == review_id, "EVENT_REVIEW_ID",
                    f"{loc}.review_id", "event belongs to another review")
            expected_id = f"{review_id}:{expected:06d}"
            require(event.get("event_id") == expected_id, "EVENT_ID", f"{loc}.event_id",
                    f"expected {expected_id}")

    evidence = bundle.get("evidence_bundle")
    require(isinstance(evidence, dict), "EVIDENCE_TYPE", "evidence_bundle",
            "evidence bundle must be an object")
    run_id = bundle.get("evidence_run_id")
    if isinstance(evidence, dict):
        require(bool(run_id) and evidence.get("run_id") == run_id,
                "EVIDENCE_RUN_ID", "evidence_run_id",
                "top-level id and evidence bundle run_id must match")
        status = evidence.get("run_status") or {}
        require(isinstance(status, dict) and status.get("status") == "COMPLETE",
                "RUN_INCOMPLETE", "evidence_bundle.run_status.status",
                "formal evidence run must be COMPLETE")
        report = evidence.get("report") or {}
        manifest = report.get("manifest") or {} if isinstance(report, dict) else {}
        commit = manifest.get("tool_commit")
        require(isinstance(commit, str) and HEX_COMMIT.fullmatch(commit) is not None,
                "TOOL_COMMIT_DIRTY", "evidence_bundle.report.manifest.tool_commit",
                "tool_commit must be a clean full hexadecimal commit")

    rows = _records(bundle)
    if rows:
        from .ledger import validate as ledger_validate
        ledger_problems = ledger_validate.validate(rows, run_id=str(run_id or ""))
        checks += 1
        for detail in ledger_problems:
            problems.append(Problem("LEDGER_INVALID", "evidence_bundle.ledger", detail))
    ids = {row.get("record_id") for row in rows if row.get("record_id")}
    require(len(ids) == len(rows), "LEDGER_IDS", "evidence_bundle.ledger",
            "each ledger record needs a unique record_id")
    for i, row in enumerate(rows):
        loc = f"evidence_bundle.ledger[{i}]"
        require(row.get("run_id") == run_id, "LEDGER_RUN_ID", f"{loc}.run_id",
                "ledger record belongs to another evidence run")
        if row.get("record_type") != "Claim":
            continue
        payload = row.get("payload") or {}
        kind = payload.get("kind")
        require(kind not in FORBIDDEN_CLAIM_KINDS, "INERT_CLAIM_LEAK", f"{loc}.payload.kind",
                "withheld/inert or deletion recommendation cannot be a formal claim")
        provenance = payload.get("provenance") or []
        require(bool(provenance) and all(pid in ids for pid in provenance),
                "CLAIM_PROVENANCE", f"{loc}.payload.provenance",
                "claim provenance must resolve inside this ledger")
        if kind == "RequiredByTest":
            data = payload.get("data") or {}
            require(bool(data.get("regressions")), "CLAIM_REGRESSION",
                    f"{loc}.payload.data.regressions", "RequiredByTest needs a named regression")
            require(data.get("restore_clean") is True, "CLAIM_RESTORE",
                    f"{loc}.payload.data.restore_clean", "RequiredByTest needs verified restoration")

    narration = bundle.get("narration") or {}
    for i, block in enumerate(narration.get("blocks") or [] if isinstance(narration, dict) else []):
        loc = f"narration.blocks[{i}]"
        require(block.get("claim_id") in ids, "NARRATION_CLAIM", f"{loc}.claim_id",
                "narration claim must resolve in ledger")
        require(all(x in ids for x in block.get("evidence_ids") or []),
                "NARRATION_EVIDENCE", f"{loc}.evidence_ids",
                "narration evidence must resolve in ledger")

    restore = bundle.get("restore_state") or {}
    require(isinstance(restore, dict) and restore.get("verified") is True,
            "RESTORE_UNVERIFIED", "restore_state.verified", "restoration must be verified")
    clean = restore.get("worktree_clean", restore.get("working_tree_clean")) \
        if isinstance(restore, dict) else None
    require(clean is True,
            "WORKTREE_DIRTY", "restore_state.worktree_clean",
            "post-run worktree must be clean")
    require(bundle.get("execution_mode") in EXECUTION_MODES,
            "EXECUTION_MODE", "execution_mode", "mode must distinguish live from replay")
    require(bundle.get("scheduler_mode") in SCHEDULER_MODES,
            "SCHEDULER_MODE", "scheduler_mode", "unknown scheduler mode")
    require(bundle.get("isolation_mode") in ISOLATION_MODES,
            "ISOLATION_MODE", "isolation_mode", "unknown isolation mode")

    integrity = bundle.get("integrity") or {}
    calculated = canonical_bundle_sha256(bundle)
    require(isinstance(integrity, dict) and integrity.get("algorithm") == "sha256-canonical-json-v1",
            "INTEGRITY_ALGORITHM", "integrity.algorithm", "unknown canonicalization")
    require(isinstance(integrity, dict) and integrity.get("event_count") == len(events or []),
            "INTEGRITY_EVENT_COUNT", "integrity.event_count", "event count does not match")
    event_digest = hashlib.sha256(_canonical(events or [])).hexdigest()
    require(isinstance(integrity, dict) and integrity.get("event_chain_sha256") == event_digest,
            "INTEGRITY_EVENT_DIGEST", "integrity.event_chain_sha256",
            "canonical event digest mismatch")
    stored_digest = integrity.get("payload_sha256", integrity.get("bundle_sha256")) \
        if isinstance(integrity, dict) else None
    require(stored_digest == calculated,
            "INTEGRITY_DIGEST", "integrity.payload_sha256", "canonical bundle digest mismatch")

    text = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    for finding in scan_text(text, path="bundle"):
        problems.append(Problem("SENSITIVE_DATA", finding.path,
                                f"{finding.rule} at logical line {finding.line}"))
        checks += 1
    return Verification(not problems, calculated, checks, tuple(problems))


def verify_file(path: Path) -> Verification:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Verification(False, "", 1,
                            (Problem("JSON_INVALID", str(path), str(exc)),))
    return verify(payload)
