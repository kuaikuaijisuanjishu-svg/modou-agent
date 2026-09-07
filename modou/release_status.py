"""Fail-closed v0.2 internal release status records.

The status deliberately separates machine checks from stable eligibility.  An
internal candidate can be useful without becoming a public-release claim.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .baseline_manifest import verify as verify_baseline_manifest


SCHEMA_VERSION = "v02-release-status-v1"
SCHEMA_VERSION_V2 = "v02-release-status-v2"
RELEASE_STATUS = "INTERNAL_ONLY"
PUBLIC_RELEASE_STATUS = "PUBLIC_PRERELEASE"
MACHINE_STATUSES = frozenset({"MACHINE_CHECKS_PASSED", "MACHINE_CHECKS_FAILED"})
EXTERNAL_GATES = (
    "independent_hidden_qa",
    "independent_linux_host",
    "authorized_private_repo_pilot",
    "real_developer_study",
    "independent_security_release_signoff",
)
NAMED_PENDING_GATES = ("public_repo_matrix",)
# 矩阵门禁在 round-2 之后要能表达真实结局，而不是永远 SUSPENDED。
# COMPLETE_PASS 只表示矩阵这一项达标，与 stable 无关：stable_eligible
# 仍由 external_gates_pending 决定，且在本版本恒为 false。
MATRIX_GATE_STATUSES = frozenset({
    "SUSPENDED", "NOT_RUN", "INCOMPLETE", "COMPLETE_FAIL", "COMPLETE_PASS",
})
MATRIX_GATE_INCOMPLETE = frozenset({
    "SUSPENDED", "NOT_RUN", "INCOMPLETE", "COMPLETE_FAIL",
})
MACHINE_EVIDENCE = (
    "automated_suites", "linux_isolation", "merge_compatibility", "public_matrix",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseStatusError(ValueError):
    """Raised when a release status attempts to overclaim or drift."""


def build_public_prerelease_status(*, source_commit: str, public_commit: str,
                                   public_tree_sha256: str, notes_sha256: str,
                                   release_tag: str,
                                   evidence: Mapping[str, Any],
                                   pending_stable_gates: list[str] | tuple[str, ...],
                                   generated_at: str | None = None) -> dict[str, Any]:
    """Build the additive v2 status used by the experimental public channel."""
    for field, value in (("source_commit", source_commit), ("public_commit", public_commit)):
        if not re.fullmatch(r"[0-9a-f]{40}", str(value)):
            raise ReleaseStatusError(f"{field} must be a full Git commit")
    for field, value in (("public_tree_sha256", public_tree_sha256), ("notes_sha256", notes_sha256)):
        _require_sha(str(value), field)
    if not re.fullmatch(r"v\d+\.\d+\.\d+-experimental\.\d+", str(release_tag)):
        raise ReleaseStatusError("release_tag must be an experimental prerelease")
    if not isinstance(evidence, Mapping):
        raise ReleaseStatusError("evidence must be an object")
    payload = {
        "schema_version": SCHEMA_VERSION_V2,
        "generated_at": generated_at or _now(),
        "source_commit": source_commit,
        "public_commit": public_commit,
        "public_tree_sha256": public_tree_sha256,
        "notes_sha256": notes_sha256,
        "release_tag": release_tag,
        "release_status": PUBLIC_RELEASE_STATUS,
        "release_channel": "experimental",
        "stable_eligible": False,
        "stable_gates_pending": list(pending_stable_gates),
        "evidence": {str(k): dict(v) if isinstance(v, Mapping) else v
                     for k, v in sorted(evidence.items())},
    }
    validate_public_prerelease_status(payload)
    return payload


def validate_public_prerelease_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("schema_version") != SCHEMA_VERSION_V2:
        raise ReleaseStatusError("unsupported public prerelease status schema")
    if payload.get("release_status") != PUBLIC_RELEASE_STATUS or payload.get("release_channel") != "experimental":
        raise ReleaseStatusError("public v2 status must be PUBLIC_PRERELEASE/experimental")
    if payload.get("stable_eligible") is not False:
        raise ReleaseStatusError("public experimental status cannot be stable eligible")
    for field in ("source_commit", "public_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get(field) or "")):
            raise ReleaseStatusError(f"{field} must be a full Git commit")
    for field in ("public_tree_sha256", "notes_sha256"):
        _require_sha(str(payload.get(field) or ""), field)
    if not re.fullmatch(r"v\d+\.\d+\.\d+-experimental\.\d+", str(payload.get("release_tag") or "")):
        raise ReleaseStatusError("invalid experimental release tag")
    pending = payload.get("stable_gates_pending")
    if not isinstance(pending, list) or not pending or any(not isinstance(x, str) or not x for x in pending):
        raise ReleaseStatusError("stable_gates_pending must be a non-empty list")
    if not isinstance(payload.get("evidence"), Mapping):
        raise ReleaseStatusError("evidence must be an object")
    if "READY" in json.dumps(payload, ensure_ascii=False).upper():
        raise ReleaseStatusError("public prerelease status may not contain READY")
    return dict(payload)


def validate_status_v2(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate either additive internal v2 or the public experimental v2."""
    if payload.get("release_status") == PUBLIC_RELEASE_STATUS:
        return validate_public_prerelease_status(payload)
    if payload.get("release_status") != RELEASE_STATUS or payload.get("release_channel") != "internal":
        raise ReleaseStatusError("invalid v2 release channel")
    for field in ("source_commit", "public_commit"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(payload.get(field) or "")):
            raise ReleaseStatusError(f"{field} must be a full Git commit")
    for field in ("public_tree_sha256", "notes_sha256"):
        _require_sha(str(payload.get(field) or ""), field)
    if not isinstance(payload.get("stable_gates_pending"), list):
        raise ReleaseStatusError("stable_gates_pending must be a list")
    return dict(payload)


def _read_evidence_entry(name: str, entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path = entry.get("path")
    digest = entry.get("sha256")
    if not isinstance(path, str) or not path:
        return None, f"{name}:path_missing"
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return None, f"{name}:sha256_missing"
    file_path = Path(path)
    try:
        blob = file_path.read_bytes()
        actual = hashlib.sha256(blob).hexdigest()
        value = json.loads(blob.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"{name}:unreadable:{type(exc).__name__}"
    if actual != digest:
        return None, f"{name}:sha256_mismatch"
    if not isinstance(value, dict):
        return None, f"{name}:not_object"
    return value, None


def validate_machine_evidence_records(
    evidence: Mapping[str, Mapping[str, Any]], *,
    baseline_manifest: Path | None = None,
    baseline_attestation: Path | None = None,
) -> list[str]:
    """Return errors that prevent a machine-pass status from being issued.

    This is intentionally stricter than :func:`build_status`, which remains a
    backwards-compatible shape builder for tests and server fallbacks. The
    release CLI calls this gate before accepting ``MACHINE_CHECKS_PASSED``.
    """
    errors: list[str] = []
    values: dict[str, dict[str, Any]] = {}
    for name in MACHINE_EVIDENCE:
        entry = evidence.get(name)
        if not isinstance(entry, Mapping):
            errors.append(f"missing:{name}")
            continue
        value, error = _read_evidence_entry(name, entry)
        if error:
            errors.append(error)
        elif value is not None:
            values[name] = value

    automated = values.get("automated_suites")
    if automated is not None:
        if automated.get("status") != "PASS":
            errors.append("automated_suites:not_pass")
        tests = automated.get("tests")
        if not isinstance(tests, Mapping) or not tests:
            errors.append("automated_suites:tests_missing")
        else:
            for suite, fact in tests.items():
                if not isinstance(fact, Mapping):
                    errors.append(f"automated_suites:{suite}:invalid_fact")
                    continue
                for key in ("collected", "total", "passed", "failed", "skipped", "xfailed", "xpassed"):
                    if key not in fact:
                        errors.append(f"automated_suites:{suite}:{key}_missing")
                counts = {key: fact.get(key) for key in
                          ("collected", "total", "passed", "failed", "skipped", "xfailed", "xpassed")}
                if (any(type(value) is not int or value < 0 for value in counts.values())
                        or counts["collected"] != counts["total"]
                        or counts["passed"] != counts["total"]
                        or any(counts[key] != 0 for key in ("failed", "skipped", "xfailed", "xpassed"))):
                    errors.append(f"automated_suites:{suite}:not_all_pass")

    linux = values.get("linux_isolation")
    if linux is not None:
        if linux.get("environment_status") != "PASS":
            errors.append("linux_isolation:not_pass")
        probes = linux.get("probes")
        if not isinstance(probes, list) or not probes:
            errors.append("linux_isolation:probes_missing")
        else:
            for probe in probes:
                if (not isinstance(probe, Mapping)
                        or probe.get("asserted_by") != "probe"
                        or probe.get("passed") is not True):
                    errors.append("linux_isolation:probe_not_pass")
        if linux.get("probe_passed") != len(probes):
            errors.append("linux_isolation:probe_count_mismatch")
        if linux.get("probe_failed") != 0:
            errors.append("linux_isolation:probe_failed")
        same_image = linux.get("same_image_tests")
        if not isinstance(same_image, Mapping) or same_image.get("ok") is not True:
            errors.append("linux_isolation:same_image_tests_not_pass")
        elif (same_image.get("collected") != same_image.get("total")
              or same_image.get("passed") != same_image.get("total")
              or any(same_image.get(key) != 0 for key in ("failed", "skipped", "xfailed", "xpassed"))):
            errors.append("linux_isolation:same_image_tests_not_all_pass")

    merge = values.get("merge_compatibility")
    if merge is not None:
        checks = merge.get("checks")
        if not isinstance(checks, list) or not checks:
            errors.append("merge_compatibility:checks_missing")
        else:
            for check in checks:
                result = check.get("status") if isinstance(check, Mapping) else None
                if not isinstance(result, Mapping) or result.get("passed") is not True:
                    errors.append("merge_compatibility:check_not_pass")

    matrix = values.get("public_matrix")
    if matrix is not None:
        if matrix.get("schema_version") == "public-repo-matrix-evaluation-v1":
            if matrix.get("status") != "COMPLETE" or matrix.get("machine_gate_eligible") is not True:
                errors.append("public_matrix:not_complete")
            formal = matrix.get("formal")
            try:
                consistent = int(formal.get("consistent", -1)) if isinstance(formal, Mapping) else -1
            except (TypeError, ValueError):
                consistent = -1
            if not isinstance(formal, Mapping) or formal.get("collected") != 20 or consistent < 19:
                errors.append("public_matrix:formal_gate_not_met")
        elif matrix.get("schema_version") == "public-repo-matrix-v1":
            if matrix.get("status") != "COMPLETE" or matrix.get("selection_complete") is not True:
                errors.append("public_matrix:not_frozen_complete")
        elif matrix.get("schema_version") == "public-repo-matrix-evaluation-v2":
            if (matrix.get("sample_scope") != "fixed_six_public_repos"
                    or matrix.get("status") != "COMPLETE"
                    or matrix.get("machine_gate_eligible") is not True):
                errors.append("public_matrix:v2_not_complete")
            formal = matrix.get("formal")
            if (not isinstance(formal, Mapping)
                    or formal.get("collected") != 6
                    or formal.get("compatible") != 6
                    or formal.get("minimum_compatible") != 6):
                errors.append("public_matrix:v2_formal_gate_not_met")
        else:
            errors.append("public_matrix:unsupported_schema")

    if baseline_manifest is None or baseline_attestation is None:
        errors.append("baseline_manifest:paths_required")
    else:
        try:
            manifest = json.loads(baseline_manifest.read_text(encoding="utf-8"))
            attestation = json.loads(baseline_attestation.read_text(encoding="utf-8"))
            result = verify_baseline_manifest(manifest, attestation)
            if not result.get("ok"):
                errors.append("baseline_manifest:verification_failed")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            errors.append("baseline_manifest:unreadable")
    return sorted(set(errors))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_sha(value: str, field: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ReleaseStatusError(f"{field} must be a lowercase SHA-256 digest")
    return value


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def build_status(*, machine_status: str, source_commit: str,
                 baseline_manifest_payload_sha256: str,
                 evidence: Mapping[str, Mapping[str, Any]] | None = None,
                 external_gates_pending: list[str] | tuple[str, ...] | None = None,
                 machine_failure_reasons: list[str] | tuple[str, ...] | None = None,
                 pending_gates: Mapping[str, Mapping[str, Any]] | None = None,
                 ui_probe_status: str = "PENDING_REPREFLIGHT",
                 narrow_package_checks: Mapping[str, Any] | None = None,
                 generated_at: str | None = None) -> dict[str, Any]:
    """Build the only status shape the v0.2 UI and release tools may expose."""
    if machine_status not in MACHINE_STATUSES:
        raise ReleaseStatusError("unsupported machine status")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ReleaseStatusError("source_commit must be a full Git commit")
    _require_sha(baseline_manifest_payload_sha256,
                 "baseline_manifest_payload_sha256")
    pending = list(external_gates_pending or EXTERNAL_GATES)
    if not pending or any(item not in EXTERNAL_GATES for item in pending):
        raise ReleaseStatusError("external_gates_pending must name known gates")
    failures = list(machine_failure_reasons or [])
    if any("ui_probe" in item.lower() for item in failures):
        raise ReleaseStatusError("UI probe status is not a machine failure reason")
    if machine_status == "MACHINE_CHECKS_FAILED" and not failures:
        failures = ["machine checks failed; inspect evidence"]
    named_pending = {
        key: dict(value) for key, value in (pending_gates or {
            "public_repo_matrix": {
                "status": "SUSPENDED",
                "reason": "公开仓库矩阵未执行",
                "resume_condition": "最终 RC 提交确定且分母只生成一次后恢复",
            }
        }).items()
    }
    matrix_status = named_pending.get("public_repo_matrix", {}).get("status")
    if machine_status == "MACHINE_CHECKS_PASSED" and matrix_status != "COMPLETE_PASS":
        # 之前默认门禁恒为 SUSPENDED，于是"机器全过 + 矩阵挂起"是可构造的，
        # 状态卡会自相矛盾。要声明机器通过，就必须显式给出达标的矩阵门禁。
        raise ReleaseStatusError(
            "MACHINE_CHECKS_PASSED requires an explicit COMPLETE_PASS matrix gate")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _now(),
        "source_commit": source_commit,
        "machine_status": machine_status,
        "release_status": RELEASE_STATUS,
        "stable_eligible": False,
        "external_gates_pending": pending,
        "machine_failure_reasons": failures,
        "ui_probe_status": ui_probe_status,
        "pending_gates": named_pending,
        **({"narrow_package_checks": dict(narrow_package_checks)}
           if narrow_package_checks is not None else {}),
        "baseline_manifest_payload_sha256": baseline_manifest_payload_sha256,
        "evidence": {
            str(key): dict(value) for key, value in sorted((evidence or {}).items())
        },
    }
    validate_status(payload)
    return payload


def _validate_narrow_package_checks(value: Any) -> None:
    """窄口径候选包的自检结果自成一档，不得借用也不得弱化 stable 机器门禁。

    它只描述"这个脱敏公开包自身的构建/测试/许可证/敏感扫描是否通过"，
    与 machine_status、stable_eligible 完全解耦：包检查全绿也不推进任何门禁。
    """
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ReleaseStatusError("narrow_package_checks must be an object")
    for key in ("build", "tests", "license_audit", "sbom", "sensitive_scan"):
        if key not in value:
            raise ReleaseStatusError(f"narrow_package_checks missing {key}")
        if not isinstance(value[key], bool):
            raise ReleaseStatusError(f"narrow_package_checks.{key} must be boolean")
    tree = value.get("public_tree_sha256")
    if not isinstance(tree, str) or not re.fullmatch(r"[0-9a-f]{64}", tree):
        raise ReleaseStatusError("narrow_package_checks needs the checked tree digest")
    for forbidden in ("stable_eligible", "release_status", "machine_status"):
        if forbidden in value:
            raise ReleaseStatusError(
                f"narrow_package_checks may not restate {forbidden}")


def validate_status(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate status and reject values that can be screenshot-misread as ready."""
    if not isinstance(payload, Mapping):
        raise ReleaseStatusError("status must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseStatusError("unsupported release status schema")
    if payload.get("release_status") != RELEASE_STATUS:
        raise ReleaseStatusError("v0.2 status must remain INTERNAL_ONLY")
    if payload.get("stable_eligible") is not False:
        raise ReleaseStatusError("stable_eligible must be false in this release")
    machine = payload.get("machine_status")
    if machine not in MACHINE_STATUSES:
        raise ReleaseStatusError("invalid machine_status")
    source = str(payload.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ReleaseStatusError("source_commit must be a full Git commit")
    _require_sha(str(payload.get("baseline_manifest_payload_sha256") or ""),
                 "baseline_manifest_payload_sha256")
    pending = payload.get("external_gates_pending")
    if not isinstance(pending, list) or not pending:
        raise ReleaseStatusError("external_gates_pending must be non-empty")
    if len(set(pending)) != len(pending) or any(x not in EXTERNAL_GATES for x in pending):
        raise ReleaseStatusError("external_gates_pending contains an unknown or duplicate gate")
    failures = payload.get("machine_failure_reasons")
    if not isinstance(payload.get("ui_probe_status"), str) or not payload.get("ui_probe_status"):
        raise ReleaseStatusError("ui_probe_status must be explicit")
    if isinstance(failures, list) and any("ui_probe" in str(item).lower() for item in failures):
        raise ReleaseStatusError("UI probe status is not a machine failure reason")
    if (machine == "MACHINE_CHECKS_FAILED"
            and (not isinstance(failures, list) or not failures
                 or not all(isinstance(x, str) and x for x in failures))):
        raise ReleaseStatusError("failed machine status needs failure reasons")
    named_pending = payload.get("pending_gates")
    if not isinstance(named_pending, Mapping) or set(named_pending) != set(NAMED_PENDING_GATES):
        raise ReleaseStatusError("pending_gates must name the public matrix gate")
    matrix_gate = named_pending.get("public_repo_matrix")
    if (not isinstance(matrix_gate, Mapping)
            or matrix_gate.get("status") not in MATRIX_GATE_STATUSES
            or not isinstance(matrix_gate.get("reason"), str)
            or not matrix_gate.get("reason")):
        raise ReleaseStatusError("public_repo_matrix pending gate is invalid")
    matrix_status = matrix_gate.get("status")
    # 未达标的门禁必须写明恢复条件；已达标的没有可恢复的东西。
    if matrix_status in MATRIX_GATE_INCOMPLETE and not isinstance(
            matrix_gate.get("resume_condition"), str):
        raise ReleaseStatusError("an unfinished matrix gate needs a resume condition")
    # 机器整体通过与矩阵未达标不能并存，否则状态卡会自相矛盾。
    if machine == "MACHINE_CHECKS_PASSED" and matrix_status != "COMPLETE_PASS":
        raise ReleaseStatusError(
            "machine checks cannot pass while the public matrix gate has not")
    _validate_narrow_package_checks(payload.get("narrow_package_checks"))
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ReleaseStatusError("evidence must be an object")
    if "READY" in json.dumps(payload, ensure_ascii=False).upper():
        raise ReleaseStatusError("status may not contain a READY claim")
    return dict(payload)


def status_digest(payload: Mapping[str, Any]) -> str:
    validate_status(payload)
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def write_status(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a validated status atomically, never partially."""
    validate_status(payload)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
