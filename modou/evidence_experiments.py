"""Opt-in, allow-listed dogfood evidence. Never issues Python claim grades.

Only the explicitly registered running product checkout is supported. Profiles
are fixed by the server; neither commands nor results come from the browser.
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .executor import TrustedLocalExecutor, ResourceLimits, sanitized_environment
from .typescript_adapter import TypeScriptProject, TypeScriptAdapterError
from .language_experiments import run_language_tests

PROFILES = {
    "ts-vitest-demo": ("test_execution", "src/conclusion-tools.test.ts"),
    "js-vitest-demo": ("test_execution", "evidence-probes/js.test.js"),
    "browser-memory-demo": ("browser_behavior", "e2e/memory-flow-real.spec.ts"),
}
PROJECT = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


from .source_identity import source_content_sha256 as snapshot


def build_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def classify_tests(tests: dict[str, str], returncode: int) -> tuple[str, str]:
    if not tests:
        return "inconclusive", "NO_TESTS_COLLECTED"
    if any(s == "failed" for s in tests.values()):
        return "failed", "ASSERTION_FAILED"
    if returncode:
        return "inconclusive", "RUNNER_ERROR"
    if any(s != "passed" for s in tests.values()):
        return "inconclusive", "TESTS_NOT_EXECUTED"
    return "passed", "DECLARED_CHECKS_PASSED"


def parse_browser_report(raw: dict) -> dict[str, str]:
    facts = {}
    def visit(suite: dict, ancestry: tuple[str, ...]):
        prefix = (*ancestry, str(suite.get("title", "")))
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                key = "::".join((*prefix, str(spec.get("title", "")),
                                 str(test.get("projectName", ""))))
                if key in facts:
                    raise ValueError("DUPLICATE_TEST_ID")
                results = test.get("results", [])
                facts[key] = str(results[-1].get("status")) if results else "not_run"
        for child in suite.get("suites", []):
            visit(child, prefix)
    for suite in raw.get("suites", []):
        visit(suite, ())
    if raw.get("errors"):
        facts["runner::global_error"] = "error"
    return facts


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _browser(root: Path, out: Path, executor, *, timeout: float):
    web = root / "web"
    if not (web / "dist/index.html").is_file():
        raise ValueError("FRONTEND_BUILD_MISSING")
    scenario = web / PROFILES["browser-memory-demo"][1]
    port, draft_port = _port(), _port()
    while draft_port == port:
        draft_port = _port()
    (out / "node_modules").symlink_to(web / "node_modules", target_is_directory=True)
    (out / "package.json").write_text('{"type":"module"}\n')
    source = scenario.read_text().replace("127.0.0.1:8788", f"127.0.0.1:{port}")
    (out / "memory.spec.ts").write_text(source)
    launcher = root / "tools/serve_evidence_dogfood.py"
    command = " ".join(__import__("shlex").quote(str(v)) for v in
                       (sys.executable, launcher, out, port, draft_port))
    config = {
        "testDir": str(out), "testMatch": "memory.spec.ts", "workers": 1,
        "retries": 0, "globalTimeout": int(timeout * 900),
        "reporter": [["json", {"outputFile": str(out / "playwright.json")}]],
        "outputDir": str(out / "traces"),
        "use": {"baseURL": f"http://127.0.0.1:{port}", "trace": "on",
                "headless": True, "viewport": {"width": 1920, "height": 1080}},
        "webServer": {"command": command, "url": f"http://127.0.0.1:{port}",
                      "reuseExistingServer": False, "timeout": 30000},
    }
    cfg = out / "playwright.config.cjs"
    cfg.write_text("module.exports = " + json.dumps(config) + ";\n")
    result = executor.run(["node", str(web / "node_modules/@playwright/test/cli.js"),
                           "test", "--config", str(cfg)], cwd=web, timeout=timeout,
                          env=sanitized_environment())
    report = out / "playwright.json"
    if not report.is_file() or report.is_symlink() or report.stat().st_size > 32 << 20:
        raise ValueError("BROWSER_REPORT_MISSING")
    raw = json.loads(report.read_text())
    return result.returncode, parse_browser_report(raw), {
        "scenario_sha256": digest(scenario.read_bytes()),
        "frontend_build_sha256": build_digest(web / "dist"),
        "real_backend": True, "model_calls": 0,
        "sample": "real_product_generated_isolated_repository",
        "browser_environment": "local_chromium",
        "restart_persistence_tested": False,
    }


def run_profile(root: Path, profile_id: str, output_root: Path, *,
                timeout: float = 360, executor=None) -> dict:
    if profile_id not in PROFILES:
        raise ValueError("UNKNOWN_EVIDENCE_PROFILE")
    root = root.resolve(strict=True)
    output_root = output_root.resolve()
    if output_root == root or root in output_root.parents:
        raise ValueError("ARTIFACTS_MUST_BE_OUTSIDE_REPOSITORY")
    out = output_root / uuid.uuid4().hex
    out.mkdir(parents=True)
    kind, target = PROFILES[profile_id]
    record = {"schema_version": "shuimu-experiment-receipt-v1",
              "receipt_id": out.name, "evidence_kind": kind,
              "profile_id": profile_id, "status": "inconclusive", "reason": "",
              "isolation_mode": "trusted_local", "experimental": True,
              "scope": [target], "coverage": None, "tests": {},
              "claim_grades": [], "started_at_epoch": time.time()}
    before = snapshot(root)
    build_before = build_digest(root / "web/dist") if kind == "browser_behavior" else ""
    record["subject_snapshot_sha256"] = before
    executor = executor or TrustedLocalExecutor(ResourceLimits(max_memory_bytes=2 << 30))
    try:
        if kind == "test_execution":
            adapter = TypeScriptProject.discover(root / "web")
            result = adapter.run_tests(executor, targets=(target,), artifact_dir=out,
                                       timeout=min(timeout, 120))
            tests = result.tests.as_status_map()
            record["runner"] = adapter.test_runner
            record["test_source_sha256"] = digest((root / "web" / target).read_bytes())
            record["report_sha256"] = result.report_sha256
            record["status"], record["reason"] = classify_tests(tests, result.returncode)
        else:
            code, tests, metadata = _browser(root, out, executor, timeout=timeout)
            record.update(metadata)
            record["status"], record["reason"] = classify_tests(tests, code)
        record["tests"] = tests
    except (TypeScriptAdapterError, ValueError, OSError, subprocess.SubprocessError,
            RuntimeError) as exc:
        record["status"] = "inconclusive"
        record["reason"] = getattr(exc, "code", None) or type(exc).__name__
        record["detail"] = str(exc)[:1000]
    after = snapshot(root)
    record["subject_snapshot_after_sha256"] = after
    if before != after or (build_before and build_before != build_digest(root / "web/dist")):
        record["status"], record["reason"] = "inconclusive", "SUBJECT_CHANGED_DURING_RUN"
    record["elapsed_seconds"] = round(time.time() - record["started_at_epoch"], 3)
    artifacts = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and not p.is_symlink() and "node_modules" not in p.parts:
            artifacts.append({"path": str(p), "sha256": digest(p.read_bytes())})
    record["artifact_refs"] = artifacts
    (out / "receipt.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    record["receipt_path"] = str(out / "receipt.json")
    return record


def make_runner(manager):
    def run(task: dict, criterion: dict) -> dict:
        from .server.control import _repo_snapshot
        review_id = task.get("origin_review_id") or task.get("review_id")
        request = manager.describe(review_id)["request"]
        repo = manager.registry.get(request["source"]["repo_id"])
        profile = criterion.get("profile_id", "")
        if criterion.get("evidence_kind") == "language_test_execution":
            if getattr(manager.execution_mode, "value", "") != "trusted_local":
                return {"status": "inconclusive", "reason": "LANGUAGE_PROFILE_REQUIRES_TRUSTED_LOCAL",
                        "artifact_refs": [], "evidence_kind": "language_test_execution",
                        "language": criterion.get("language", "")}
            language = criterion.get("language", "")
            if not isinstance(language, str) or not language:
                raise ValueError("LANGUAGE_REQUIRED")
            targets = criterion.get("test_targets", [])
            if not isinstance(targets, list) or any(not isinstance(value, str) for value in targets):
                raise ValueError("LANGUAGE_TARGETS_INVALID")
            constraints = request.get("constraints") if isinstance(request.get("constraints"), dict) else {}
            approved_budget = request.get("budget_seconds") or constraints.get("budget_seconds") or 60
            result = run_language_tests(repo.path, language=language, targets=tuple(targets),
                                        output_root=manager.root / "_language_experiment_runs",
                                        timeout=min(900.0, float(approved_budget)))
            result["source_snapshot_sha256"] = _repo_snapshot(repo.path)["snapshot_sha256"]
            if criterion.get("adapter_id") and result.get("adapter_id") != criterion["adapter_id"]:
                result["status"], result["reason"] = "inconclusive", "LANGUAGE_ADAPTER_MISMATCH"
            return result
        if repo.path.resolve() != PROJECT.resolve():
            return {"status": "inconclusive", "reason": "PROFILE_ONLY_FOR_REGISTERED_PRODUCT",
                    "artifact_refs": [], "profile_id": profile,
                    "evidence_kind": criterion.get("evidence_kind"),
                    "source_snapshot_sha256": _repo_snapshot(repo.path)["snapshot_sha256"]}
        if getattr(manager.execution_mode, "value", "") != "trusted_local":
            return {"status": "inconclusive", "reason": "PROFILE_REQUIRES_TRUSTED_LOCAL",
                    "artifact_refs": [], "profile_id": profile,
                    "evidence_kind": criterion.get("evidence_kind")}
        if profile not in PROFILES or PROFILES[profile][0] != criterion.get("evidence_kind"):
            raise ValueError("EVIDENCE_PROFILE_KIND_MISMATCH")
        constraints = request.get("constraints") if isinstance(request.get("constraints"), dict) else {}
        approved_budget = request.get("budget_seconds") or constraints.get("budget_seconds") or 60
        budget = min(360.0, float(approved_budget))
        result = run_profile(repo.path, profile, manager.root / "_experiment_runs", timeout=budget)
        result["source_snapshot_sha256"] = _repo_snapshot(repo.path)["snapshot_sha256"]
        return result
    return run


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run a fixed, trusted-local evidence experiment")
    parser.add_argument("profile", choices=list(PROFILES))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run_profile(PROJECT, args.profile, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
