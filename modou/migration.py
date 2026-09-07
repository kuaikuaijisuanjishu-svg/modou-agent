"""Version detection and non-destructive v1/v2 migration helpers."""
from __future__ import annotations

import copy
from typing import Any

from .agent.spec import ReviewSpec
from .bundle_v2 import seal_bundle
from .modes import DEFAULT_DETERMINISTIC_SCHEDULER, execution_mode_for
from .review_bundle import public_safe
from .release_status import SCHEMA_VERSION, SCHEMA_VERSION_V2


class MigrationError(ValueError):
    pass


def detect_schema(value: Any) -> str:
    if not isinstance(value, dict):
        return "unknown"
    version = value.get("schema_version")
    if isinstance(version, str):
        return version
    if {"run_id", "records", "summary"} <= set(value):
        return "legacy-evidence-bundle"
    return "unknown"


def request_v1_to_spec_v2(request: dict) -> dict:
    if detect_schema(request) != "review-request-v1":
        raise MigrationError("expected review-request-v1")
    source = request.get("source") or {}
    raw = {
        "instruction": str(request.get("goal") or "审查补丁新增代码的证据边界"),
        "source": {"kind": source.get("kind"), "repo_id": source.get("repo_id")},
        "goal": str(request.get("goal") or "审查补丁新增代码的证据边界"),
        "scope": {"test_files": list(request.get("test_files") or [])},
        "constraints": {"budget_seconds": int(float(request.get("budget_seconds") or 300))},
        "autonomy_policy": {"model_provider": str(request.get("model_provider") or
                                                    "deterministic")},
        "manual_overrides": {"fields": [
            "scope.test_files", "constraints.budget_seconds",
            "autonomy_policy.model_provider"], "conflicts": []},
    }
    return ReviewSpec.parse(raw).as_dict()


def spec_v2_to_request_v1(spec: dict, *, review_id: str = "") -> dict:
    """Rollback projection; v2-only safety fields remain in the source artifact."""
    parsed = ReviewSpec.parse(spec)
    return {
        "schema_version": "review-request-v1", "review_id": review_id,
        "source": {"kind": parsed.source.kind, "repo_id": parsed.source.repo_id},
        "test_files": list(parsed.scope.test_files), "declared_tests": [],
        "goal": parsed.goal, "review_focus": parsed.review_focus,
        "budget_seconds": parsed.constraints.budget_seconds,
        "model_provider": parsed.autonomy_policy.model_provider,
        "execution_mode": "trusted_local",
        "migration": {"lossless": False, "source_schema": parsed.schema_version,
                      "preserved_source_required_for_forward_restore": True},
    }


def bundle_v1_to_v2(bundle: dict) -> dict:
    if detect_schema(bundle) != "review-bundle-v1":
        raise MigrationError("expected review-bundle-v1")
    out = public_safe(copy.deepcopy(bundle))
    # public_safe intentionally projects opaque identities, but the v2 event
    # protocol derives event IDs from the projected review ID.  Rebuild that
    # deterministic chain after projection so legacy events cannot leave a
    # stale/private ID that fails v2 verification.
    review_id = str(out.get("review_id") or "")
    for seq, event in enumerate(out.get("events") or [], 1):
        if isinstance(event, dict):
            event["review_id"] = review_id
            event["event_id"] = f"{review_id}:{seq:06d}"
    evidence_run_id = str(out.get("evidence_run_id") or "")
    evidence = out.get("evidence_bundle")
    if isinstance(evidence, dict) and evidence_run_id:
        # run_id is the join key between the top-level envelope and the
        # legacy evidence payload.  Rebind it after identity projection (and
        # do the same for legacy ledger rows) before sealing the v2 bundle.
        evidence["run_id"] = evidence_run_id
        for row in evidence.get("ledger") or []:
            if isinstance(row, dict):
                row["run_id"] = evidence_run_id
    request = out.get("request") or {}
    provider = out.get("provider") or {"kind": "deterministic"}
    isolation = str(request.get("execution_mode") or "trusted_local")
    execution = execution_mode_for(isolation)
    scheduler = ("model" if provider.get("kind") not in {None, "", "deterministic"}
                 else str(request.get("deterministic_scheduler") or
                          DEFAULT_DETERMINISTIC_SCHEDULER))
    evidence = out.get("evidence_bundle") or {}
    run_status = evidence.get("run_status") or {}
    complete = run_status.get("status") == "COMPLETE"
    out["scheduler_trace"] = [event for event in out.get("events") or []
                              if event.get("kind") in {"observation.recorded",
                                                       "model.action", "policy.rejected",
                                                       "scheduler.next"}]
    out["migration"] = {"source_schema": "review-bundle-v1",
                        "mode": "additive-envelope", "source_preserved": True}
    return seal_bundle(
        out, execution_mode=execution, scheduler_mode=scheduler,
        isolation_mode=isolation,
        restore_state={"verified": complete, "run_status": run_status.get("status", "UNKNOWN"),
                       "worktree_clean": complete, "protocol_version": "legacy-v1"},
        evaluation_context={"selection_outcome_blind": None, "sample_size_blind": None,
                            "historical_labels_viewed": None, "model_oracle_access": None,
                            "budget_semantics": "wall_clock",
                            "migration_source_schema": "review-bundle-v1"})


def ui_probe_v1_to_v2(probe: dict) -> dict:
    """Project a v1 probe into an explicitly non-gating legacy v2 record."""
    if detect_schema(probe) != "ui-comprehension-probe-v1":
        raise MigrationError("expected ui-comprehension-probe-v1")
    sessions = []
    for source in probe.get("sessions") or []:
        if not isinstance(source, dict):
            continue
        row = copy.deepcopy(source)
        row.setdefault("variant_class", "candidate")
        row.setdefault("validation_errors", [])
        sessions.append(row)
    candidate = sum(row.get("variant_class") == "candidate" for row in sessions)
    invalid = sum(bool(row.get("validation_errors")) or row.get("status") == "INVALIDATED" for row in sessions)
    return {
        "schema_version": "ui-comprehension-probe-v2", "name": "ui-comprehension-probe",
        "substitutes_human_study": False, "positive_claims_allowed": False,
        "evidence_class": "synthetic_counterexample_probe",
        "sample_scope": "legacy_no_positive_controls",
        "status": "PROBE_INSENSITIVE", "sessions": sessions,
        "batch": {"candidate_sessions": candidate, "positive_control_sessions": 0,
                   "positive_controls_detected": 0, "invalidated_sessions": invalid,
                   "environment_blocked_sessions": 0, "calibration_sessions": 0},
        "gate": {"sensitivity": False, "candidate_counterexamples": 0, "interpretable": False},
        "errors": ["legacy_v1_without_positive_controls"],
        "interpretation": "legacy record; cannot be used for the v2 sensitivity or release gate",
        "migration": {"source_schema": "ui-comprehension-probe-v1",
                       "legacy_no_positive_controls": True, "source_preserved": True},
    }


def public_matrix_v1_to_v2(matrix: dict) -> dict:
    """Wrap the legacy 20-repository matrix without changing its denominator."""
    source_schema = detect_schema(matrix)
    if source_schema not in {"public-repo-matrix-v1", "round1-seal-manifest-v1"}:
        raise MigrationError("expected public-repo-matrix-v1 or archived round1 seal manifest")
    source_matrix = matrix if source_schema == "public-repo-matrix-v1" else {
        "schema_version": "public-repo-matrix-v1", "status": "COMPLETE",
        "selection_seed": "shuimu-v0.2-public-matrix-v1",
        "targets": {"python_pytest": 10, "vitest": 5, "jest": 5},
    }
    return {"schema_version": "public-repo-matrix-v2",
            "sample_scope": "legacy_20_repo_matrix",
            "selection_seed": source_matrix.get("selection_seed", "shuimu-v0.2-public-matrix-v1"),
            "replacement_after_freeze": False,
            "targets": copy.deepcopy(source_matrix.get("targets") or {}),
            "repositories": [],
            "pre_freeze": {"required_runs": 3, "flake_table_frozen": False},
            "formal": {"required_runs": 2, "compatible": 0, "collected": 0,
                        "minimum_compatible": 0},
            "consistency_definition": "legacy denominator preserved; no v2 product-off/product-on conclusion",
            "evaluation_errors": ["legacy_matrix_requires_v2_reexecution"],
            "legacy_matrix": copy.deepcopy(matrix),
            "migration": {"source_schema": source_schema,
                           "source_denominator": 20, "source_minimum_consistent": 19,
                           "source_preserved": True},
            "status": "COMPLETE" if source_matrix.get("status") == "COMPLETE" else "PENDING",
            "machine_gate_eligible": False}


def release_status_v1_to_v2(status: dict) -> dict:
    if detect_schema(status) != SCHEMA_VERSION:
        raise MigrationError("expected v02-release-status-v1")
    # Internal v1 is losslessly projected to the internal v2 channel.  A
    # public prerelease can never be projected back to v1 (which means
    # INTERNAL_ONLY) without changing its semantics.
    if status.get("release_status") != "INTERNAL_ONLY":
        raise MigrationError("only INTERNAL_ONLY v1 status may migrate")
    source = str(status.get("source_commit") or "")
    tree = str((status.get("narrow_package_checks") or {}).get("public_tree_sha256") or "0" * 64)
    evidence = status.get("evidence") or {}
    notes = str((evidence.get("release_notes") or {}).get("sha256") or "0" * 64)
    out = {"schema_version": SCHEMA_VERSION_V2, "generated_at": status.get("generated_at", ""),
           "source_commit": source, "public_commit": source, "public_tree_sha256": tree,
           "notes_sha256": notes, "release_tag": "internal-v1",
           "release_status": "INTERNAL_ONLY", "release_channel": "internal",
           "stable_eligible": False,
           "stable_gates_pending": list(status.get("external_gates_pending") or []),
           "evidence": copy.deepcopy(evidence),
           "migration": {"source_schema": SCHEMA_VERSION, "source_preserved": True,
                          "public_projection_allowed": False},
           "legacy_v1": copy.deepcopy(status)}
    return out


def release_status_v2_to_v1(status: dict) -> dict:
    if detect_schema(status) != SCHEMA_VERSION_V2:
        raise MigrationError("expected v02-release-status-v2")
    if status.get("release_channel") != "internal":
        raise MigrationError("public prerelease v2 cannot project back to v1")
    legacy = status.get("legacy_v1")
    if not isinstance(legacy, dict) or detect_schema(legacy) != SCHEMA_VERSION:
        raise MigrationError("internal v2 status lacks preserved v1 source")
    return copy.deepcopy(legacy)
