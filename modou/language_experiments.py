"""Server-controlled, language-neutral test execution evidence.

This layer deliberately stops at test execution facts.  It does not create
Python line claims and it does not accept a command supplied by a browser.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from .executor import ResourceLimits, TrustedLocalExecutor
from .language_adapters import discover_adapter
from .source_identity import source_content_sha256


MAX_OUTPUT_BYTES = 32 << 20


class LanguageRuntimeUnavailable(RuntimeError):
    """The selected adapter is known, but its runner is not installed."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _classify(statuses: dict[str, str], returncode: int) -> tuple[str, str]:
    if not statuses:
        return "inconclusive", "NO_TESTS_COLLECTED"
    if any(value == "failed" for value in statuses.values()):
        return "failed", "ASSERTION_FAILED"
    if returncode:
        return "inconclusive", "RUNNER_ERROR"
    if any(value != "passed" for value in statuses.values()):
        return "inconclusive", "TESTS_NOT_EXECUTED"
    return "passed", "DECLARED_CHECKS_PASSED"


def run_language_tests(root: Path, *, language: str, targets: tuple[str, ...],
                       output_root: Path, timeout: float = 300,
                       executor=None) -> dict:
    """Run a discovered adapter and return a version-bound execution receipt."""
    root = Path(root).resolve(strict=True)
    output_root = Path(output_root).resolve()
    if output_root == root or root in output_root.parents:
        raise ValueError("ARTIFACTS_MUST_BE_OUTSIDE_REPOSITORY")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 900:
        raise ValueError("LANGUAGE_TIMEOUT_INVALID")
    adapter = discover_adapter(root, language=language)
    capabilities = adapter.capabilities()
    run_dir = output_root / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    before = source_content_sha256(root)
    started = time.time()
    receipt = {
        "schema_version": "shuimu-language-experiment-v1",
        "receipt_id": run_dir.name,
        "evidence_kind": "language_test_execution",
        "language": adapter.capabilities().language,
        "adapter_id": adapter.adapter_id,
        "capabilities": capabilities.as_dict(),
        "scope": list(targets),
        "status": "inconclusive",
        "reason": "",
        "tests": {},
        "report_sha256": "",
        "source_snapshot_sha256": before,
        "source_snapshot_after_sha256": "",
        "experimental": True,
        "started_at_epoch": started,
    }
    try:
        # Discoverability and execution are separate capabilities.  Returning
        # the adapter's stable reason here makes a missing Go/Maven runner an
        # explicit inconclusive receipt instead of an opaque FileNotFoundError.
        # An injected executor is an explicit test/integration seam and may
        # provide a fixture runner under a different executable name.  The
        # production default still fails closed from the capability probe.
        if not capabilities.execution and executor is None:
            raise LanguageRuntimeUnavailable(
                capabilities.unsupported_reasons[0]
                if capabilities.unsupported_reasons
                else "LANGUAGE_RUNTIME_UNAVAILABLE")
        runner = executor or TrustedLocalExecutor(ResourceLimits(max_memory_bytes=2 << 30))
        result = adapter.run_tests(runner, targets=targets, artifact_dir=run_dir,
                                   timeout=float(timeout))
        statuses = result.tests.as_status_map()
        receipt["tests"] = statuses
        receipt["report_sha256"] = result.report_sha256
        receipt["command"] = {"argv": list(result.command.argv),
                              "network": result.command.network, "shell": result.command.shell}
        receipt["returncode"] = result.returncode
        receipt["status"], receipt["reason"] = _classify(statuses, result.returncode)
    except Exception as exc:
        receipt["reason"] = getattr(exc, "code", None) or type(exc).__name__
        receipt["detail"] = str(exc)[:1000]
    after = source_content_sha256(root)
    receipt["source_snapshot_after_sha256"] = after
    if before != after:
        receipt["status"], receipt["reason"] = "inconclusive", "SUBJECT_CHANGED_DURING_RUN"
    refs = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "receipt.json":
            data = path.read_bytes()
            if len(data) > MAX_OUTPUT_BYTES:
                receipt["status"], receipt["reason"] = "inconclusive", "ARTIFACT_TOO_LARGE"
                continue
            refs.append({"path": path.relative_to(run_dir).as_posix(), "sha256": _digest(data)})
    receipt["artifact_refs"] = refs
    receipt["elapsed_seconds"] = round(time.time() - started, 3)
    receipt["receipt_sha256"] = _digest(json.dumps(receipt, ensure_ascii=False,
                                                   sort_keys=True, separators=(",", ":")).encode())
    (run_dir / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    receipt["receipt_path"] = str(run_dir / "receipt.json")
    return receipt
