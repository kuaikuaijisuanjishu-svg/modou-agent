"""Canonical ReviewBundle v2 construction and integrity helpers.

The review bundle is a public evidence boundary. Keep it independent of
server runtime state so local runs and replay use one stable wire format.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from .modes import (DEFAULT_DETERMINISTIC_SCHEDULER,
                    execution_mode_for)
from .sensitive import scan_text


SCHEMA_VERSION = "review-bundle-v2"
INTEGRITY_ALGORITHM = "sha256-canonical-json-v1"
_PERSONAL_PATH = re.compile(
    r"(?:/Users/[^/\s\"']+|/home/[^/\s\"']+)(?:/[^\s\"']+)*"
)
_ENCODED_PERSONAL_PATH = re.compile(
    r"-Users-[A-Za-z0-9._-]+(?:-Desktop-[^/\s\"']*)?"
)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.I,
)
_DROP_KEYS = frozenset({
    "argv", "command", "diff", "full_text", "messages", "model_trace",
    "patch", "private_trace", "prompt", "raw", "raw_model_response",
    "source_code", "source_text", "stderr", "stdout",
})
_ID_KEYS = frozenset({
    "delivery_id", "event_id", "evidence_run_id", "installation_id",
    "record_id", "repo_id", "repository_id", "review_id", "run_id",
    "unit_id",
})


def canonical_json(value: Any) -> bytes:
    """Return the UTF-8 canonical representation used by bundle hashes."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _public_id(value: Any, key: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    prefix = {
        "evidence_run_id": "public-run",
        "review_id": "public-review",
        "tool_commit": "public-runtime",
    }.get(key, "public-id")
    return f"{prefix}-{digest}"


def _redact_string(value: str) -> str:
    value = _PERSONAL_PATH.sub("artifact://private-path-redacted", value)
    value = _ENCODED_PERSONAL_PATH.sub("artifact-private-path-redacted", value)
    return _UUID.sub(lambda match: _public_id(match.group(0), "uuid"), value)


def _public_safe(value: Any, *, context: str = "") -> Any:
    """Return a strict public projection without mutating the input."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = re.sub(r"[^a-z0-9_]", "", key.lower())
            if normalized in _DROP_KEYS:
                continue
            if normalized in _ID_KEYS or normalized.endswith("_uuid"):
                result[key] = _public_id(item, normalized)
                continue
            if normalized == "tool_commit":
                result[key] = _public_id(item, normalized)
                continue
            if normalized in {"endpoint", "base_url", "callback_url", "webhook_url"}:
                result[key] = "redacted://endpoint"
                continue
            if context in {"lines", "source_lines", "rendered_lines"} and normalized in {
                "content", "line_text", "source", "text",
            }:
                continue
            result[key] = _public_safe(item, context=normalized)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_safe(item, context=context) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _mode_fields(request: dict, provider: dict) -> tuple[str, str, str]:
    """Derive the three public mode labels from what the run actually did.

    `execution_mode` used to be the literal "live" for every bundle, so a
    replay run claimed it had executed repository code. It is not independent
    of isolation: replaying is precisely the case where nothing ran.
    """
    isolation = str(request.get("execution_mode") or "trusted_local")
    agent_level = str(request.get("agent_level") or "l1")
    provider_kind = str(provider.get("kind") or "deterministic")
    if agent_level == "l2" and provider_kind != "deterministic":
        scheduler = "model"
    else:
        scheduler = str(request.get("deterministic_scheduler")
                        or DEFAULT_DETERMINISTIC_SCHEDULER)
    return execution_mode_for(isolation), scheduler, isolation


def _resource_policy(*, request: dict, isolation_mode: str) -> dict:
    """Describe enforced and intentionally unenforced execution limits."""
    replay = isolation_mode == "replay"
    return {
        "schema_version": "resource-policy-v1",
        "budget_seconds": request.get("budget_seconds"),
        "budget_semantics": "wall_clock",
        "timeout_cleanup": "not_applicable" if replay else "process_group",
        "disk_limit": ("not_applicable" if replay else
                        "sandbox_file_size_only" if isolation_mode == "sandboxed"
                        else "not_enforced"),
        "memory_limit": "not_enforced",
        "process_count_limit": "not_enforced",
    }


def build_review_bundle_v2(*, review_id: str, request: dict, plan: dict,
                           events: list[dict], scheduler_trace: list[dict],
                           evidence_run_id: str, evidence_bundle: dict,
                           narration: dict, provider: dict,
                           model_metrics: dict, evidence_valid: bool,
                           evaluation_context: dict | None = None) -> dict:
    """Build and self-hash a ReviewBundle v2.

    ``payload_sha256`` hashes the complete bundle with that one value blanked.
    This avoids a self-referential digest while still authenticating every
    public field, including the event-chain digest and preserved run id.
    """
    request = _public_safe(request)
    plan = _public_safe(plan)
    events = _public_safe(events)
    scheduler_trace = _public_safe(scheduler_trace)
    evidence_bundle = _public_safe(evidence_bundle)
    narration = _public_safe(narration)
    provider = _public_safe(provider)
    model_metrics = _public_safe(model_metrics)
    evaluation_context = _public_safe(evaluation_context or {})
    execution_mode, scheduler_mode, isolation_mode = _mode_fields(request, provider)
    report = dict(evidence_bundle.get("report") or {})
    manifest = dict(report.get("manifest") or {})
    run_status = dict(evidence_bundle.get("run_status") or {})
    tool_commit = str(manifest.get("tool_commit") or "")
    event_chain_sha256 = content_sha256(events)
    context = {
        "selection_outcome_blind": None,
        "sample_size_blind": None,
        "historical_labels_viewed": None,
        "model_oracle_access": None,
        "budget_semantics": "wall_clock",
    }
    context.update(evaluation_context)
    # This is derived from the request and actual isolation mode, not caller
    # supplied prose, so the artifact cannot claim a stronger boundary than
    # the executor implements.
    context["resource_policy"] = _resource_policy(
        request=request, isolation_mode=isolation_mode)
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "review_id": review_id,
        "request": request,
        "plan": plan,
        "events": events,
        "scheduler_trace": scheduler_trace,
        "evidence_run_id": evidence_run_id,
        "evidence_bundle": evidence_bundle,
        "narration": narration,
        "provider": provider,
        "model_metrics": model_metrics,
        "execution_mode": execution_mode,
        "scheduler_mode": scheduler_mode,
        "isolation_mode": isolation_mode,
        "restore_state": {
            "verified": bool(evidence_valid),
            "run_status": run_status.get("status", "UNKNOWN"),
            "worktree_clean": bool(evidence_valid and run_status.get("status") == "COMPLETE"),
            "protocol_version": (report.get("summary") or {}).get("restore_protocol_version", ""),
        },
        "evaluation_context": context,
        "integrity": {
            "algorithm": INTEGRITY_ALGORITHM,
            "payload_sha256": "",
            "event_chain_sha256": event_chain_sha256,
            "event_count": len(events),
            "tool_commit": tool_commit,
            "tool_commit_clean": bool(tool_commit and not tool_commit.endswith("+dirty")),
        },
    }
    bundle["integrity"]["payload_sha256"] = payload_digest(bundle)
    findings = scan_text(json.dumps(bundle, ensure_ascii=False), path="review_bundle")
    if findings:
        rules = ", ".join(sorted({finding.rule for finding in findings}))
        raise ValueError(f"public ReviewBundle contains sensitive data shapes: {rules}")
    return bundle


def payload_digest(bundle: dict) -> str:
    candidate = deepcopy(bundle)
    integrity = candidate.setdefault("integrity", {})
    integrity["payload_sha256"] = ""
    return content_sha256(candidate)


def integrity_problems(bundle: dict) -> list[str]:
    """Validate the self-contained v2 integrity contract."""
    problems: list[str] = []
    if bundle.get("schema_version") != SCHEMA_VERSION:
        problems.append("unsupported review bundle schema")
    if not bundle.get("evidence_run_id"):
        problems.append("evidence_run_id is required")
    integrity = bundle.get("integrity") or {}
    if integrity.get("algorithm") != INTEGRITY_ALGORITHM:
        problems.append("unsupported integrity algorithm")
    if integrity.get("payload_sha256") != payload_digest(bundle):
        problems.append("payload sha256 mismatch")
    events = bundle.get("events") or []
    if integrity.get("event_count") != len(events):
        problems.append("event count mismatch")
    if integrity.get("event_chain_sha256") != content_sha256(events):
        problems.append("event chain sha256 mismatch")
    for expected, event in enumerate(events, 1):
        if event.get("seq") != expected:
            problems.append(f"event sequence is not contiguous at {expected}")
            break
    tool_commit = str(integrity.get("tool_commit") or "")
    if not tool_commit or tool_commit.endswith("+dirty"):
        problems.append("tool_commit is missing or dirty")
    return problems
