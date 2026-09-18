"""Fail-closed, one-command release pipeline for public award artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import capabilities as caps
from .baseline_manifest import (BaselineManifestError, build_manifest,
                                sha256_bytes, verify as verify_baseline)
from .bundle_v2 import verify_file
from .sensitive import scan_paths


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    seconds: float
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def public_json(self) -> dict:
        return {"name": self.name, "returncode": self.returncode,
                "seconds": round(self.seconds, 3),
                # Logs stay in checks/, not in the fact sheet or gate.
                "log": f"checks/{self.name}.log"}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _run(name: str, command: list[str], *, cwd: Path) -> CommandResult:
    start = time.monotonic()
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return CommandResult(name, tuple(command), proc.returncode,
                         time.monotonic() - start, proc.stdout, proc.stderr)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if proc.returncode:
        raise ReleaseError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _safe_source(repo: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise ReleaseError(f"public path escapes repository: {relative}")
    path = (repo / raw).resolve(strict=True)
    try:
        path.relative_to(repo.resolve())
    except ValueError as exc:
        raise ReleaseError(f"public path escapes repository: {relative}") from exc
    if path.is_symlink():
        raise ReleaseError(f"public path may not be a symlink: {relative}")
    return path


def _copy_public(repo: Path, names: list[str], destination: Path) -> list[Path]:
    copied: list[Path] = []
    for name in names:
        src = _safe_source(repo, name)
        dest = destination / Path(name)
        if src.is_dir():
            for item in src.rglob("*"):
                if not item.is_file() or item.is_symlink():
                    continue
                rel = item.relative_to(repo)
                target = destination / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                copied.append(target)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(dest)
    return copied


def _lookup(value: object, dotted: str) -> object:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ReleaseError(f"required evidence field is missing: {dotted}")
        current = current[part]
    return current


def _validate_required_evidence(repo: Path, requirements: Path) -> list[dict]:
    raw = json.loads(requirements.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "award-release-requirements-v1":
        raise ReleaseError("release requirements schema is unsupported")
    items = raw.get("items")
    if not isinstance(items, list) or not items:
        raise ReleaseError("release requirements must contain evidence items")
    results: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "path", "equals"}:
            raise ReleaseError("release requirement item shape is invalid")
        evidence_id = str(item["id"])
        relative = str(item["path"])
        raw_path = Path(relative)
        if raw_path.is_absolute() or ".." in raw_path.parts:
            raise ReleaseError(f"required evidence path is unsafe: {evidence_id}")
        if not (repo / raw_path).is_file():
            raise ReleaseError(f"required evidence is missing: {evidence_id}")
        path = _safe_source(repo, relative)
        if not path.is_file() or path.suffix.lower() != ".json":
            raise ReleaseError(f"required evidence must be a JSON file: {evidence_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        equals = item["equals"]
        if not isinstance(equals, dict) or not equals:
            raise ReleaseError(f"required evidence checks are empty: {evidence_id}")
        for dotted, expected in equals.items():
            observed = _lookup(payload, str(dotted))
            if observed != expected:
                raise ReleaseError(
                    f"required evidence gate failed: {evidence_id}.{dotted}")
        results.append({
            "id": evidence_id,
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "checks": len(equals),
        })
    return results


def _validate_baseline(repo: Path, *, config_path: Path, manifest_path: Path,
                       attestation_path: Path) -> dict:
    """Rebuild and verify the exact BaselineManifest bound to a release."""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        rebuilt = build_manifest(root=repo, config=config)
        result = verify_baseline(manifest, attestation)
    except (OSError, json.JSONDecodeError, BaselineManifestError) as exc:
        raise ReleaseError(f"BaselineManifest verification failed: {exc}") from exc
    if rebuilt != manifest:
        raise ReleaseError("BaselineManifest inputs drifted from the signed payload")
    if not result["ok"]:
        raise ReleaseError("BaselineManifest attestation is invalid")
    return {
        "status": "PASS",
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
        "payload_sha256": result["payload_sha256"],
        "assurance": result["assurance"],
    }


def _measured_result(path: Path, *, suite: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "modou-test-result-v1":
        raise ReleaseError(f"{suite} result has an unsupported schema")
    total = int(data.get("total", 0))
    passed = int(data.get("passed", 0))
    failed = int(data.get("failed", total - passed))
    counts = {key: int(data.get(key, 0)) for key in ("skipped", "xfailed", "xpassed")}
    collected = int(data.get("collected", total))
    cases = data.get("cases") or []
    if (total <= 0 or collected != total or len(cases) != total
            or passed + failed + sum(counts.values()) != total):
        raise ReleaseError(f"{suite} result count does not match its cases")
    return {"collected": collected, "total": total, "passed": passed, "failed": failed,
            **counts,
            "cases": cases}


def _server_count(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema_version") == "modou-test-result-v1":
            return value
    raise ReleaseError("server runner did not emit modou-test-result-v1 JSON")


def _frontend_count(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    total = int(data.get("numTotalTests", 0))
    passed = int(data.get("numPassedTests", 0))
    failed = int(data.get("numFailedTests", total - passed))
    skipped = int(data.get("numPendingTests", 0))
    xfailed = int(data.get("numTodoTests", 0))
    if total <= 0:
        raise ReleaseError("Vitest JSON contains no measured test cases")
    cases = []
    for file_result in data.get("testResults") or []:
        for item in file_result.get("assertionResults") or []:
            cases.append({"name": str(item.get("fullName") or item.get("title") or ""),
                          "passed": item.get("status") == "passed"})
    if len(cases) != total:
        raise ReleaseError("Vitest count does not match assertionResults")
    return {"collected": total, "total": total, "passed": passed, "failed": failed,
            "skipped": skipped, "xfailed": xfailed, "xpassed": 0,
            "cases": cases}


def _browser_count(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []

    def visit(suite: dict) -> None:
        for spec in suite.get("specs") or []:
            projects = [str(test.get("projectName") or "")
                        for test in spec.get("tests") or []]
            cases.append({"name": str(spec.get("title") or ""),
                          "project": projects[0] if projects else "",
                          "passed": bool(spec.get("ok"))})
        for child in suite.get("suites") or []:
            visit(child)

    for suite in data.get("suites") or []:
        visit(suite)
    total = len(cases)
    passed = sum(bool(case["passed"]) for case in cases)
    failed = total - passed
    if total <= 0:
        raise ReleaseError("Playwright JSON contains no measured test cases")
    return {"collected": total, "total": total, "passed": passed, "failed": failed,
            "skipped": 0, "xfailed": 0, "xpassed": 0,
            "cases": cases}


def _check_public_fact_consistency(public_dir: Path, tests: dict) -> list[str]:
    """Reject recognizable current test totals that disagree with measured facts."""
    patterns = {
        "python": re.compile(r"(?i)python\s*[*_`]*\s*(\d+)\s*/\s*(\d+)"),
        "server": re.compile(r"(?:HTTP|服务端)\s*[*_`]*\s*(\d+)\s*/\s*(\d+)", re.I),
        "frontend": re.compile(r"(?:前端|Vitest)\s*[*_`]*\s*(\d+)\s*/\s*(\d+)", re.I),
        "browser_e2e": re.compile(
            r"(?:浏览器(?:\s*E2E)?|Playwright)\s*[*_`]*\s*(\d+)\s*/\s*(\d+)", re.I),
    }
    problems: list[str] = []
    for path in sorted(public_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".html"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for suite, pattern in patterns.items():
            if suite not in tests:
                continue
            expected = (int(tests[suite]["passed"]), int(tests[suite]["total"]))
            for match in pattern.finditer(text):
                observed = (int(match.group(1)), int(match.group(2)))
                if observed != expected:
                    rel = path.relative_to(public_dir).as_posix()
                    problems.append(
                        f"{rel}: {suite} says {observed[0]}/{observed[1]}, "
                        f"measured {expected[0]}/{expected[1]}")
    return problems


def _archive(public_dir: Path, destination: Path) -> dict:
    expected = {
        path.relative_to(public_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(p for p in public_dir.rglob("*") if p.is_file())
    }
    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for path in sorted(p for p in public_dir.rglob("*") if p.is_file()):
            tf.add(path, arcname=path.relative_to(public_dir).as_posix(), recursive=False)
    with tarfile.open(destination, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        if not members:
            raise ReleaseError("public archive is empty")
        if any(Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members):
            raise ReleaseError("public archive contains an unsafe member path")
        if {m.name for m in members} != set(expected):
            raise ReleaseError("public archive member list differs from the allowlisted package")
        for member in members:
            stream = tf.extractfile(member)
            if stream is None or hashlib.sha256(stream.read()).hexdigest() != expected[member.name]:
                raise ReleaseError(f"public archive content mismatch: {member.name}")
    blob = destination.read_bytes()
    return {"file": destination.name, "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(), "files": len(members),
            "content_manifest_sha256": hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()}


def run_release(*, repo: Path, output: Path, bundles: list[Path],
                public_paths: list[str], requirements: Path | None = None,
                baseline: tuple[Path, Path, Path] | None = None) -> dict:
    """Execute tests/build/verification/scan/archive and publish one gate last."""
    repo = repo.resolve(strict=True)
    output = output.absolute()
    try:
        output.resolve(strict=False).relative_to(repo)
    except ValueError:
        pass
    else:
        raise ReleaseError("formal release output must be outside the Git worktree")
    if output.exists():
        raise ReleaseError(f"release output already exists: {output}")
    output.mkdir(parents=True)
    checks_dir = output / "checks"
    checks_dir.mkdir()
    status = {"schema_version": "release-status-v1", "status": "INCOMPLETE",
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "reason": ""}
    _write_json(output / "run_status.json", status)

    commands: list[CommandResult] = []
    facts: dict = {
        "schema_version": "release-facts-v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tests": {},
        "bundles": [],
        "security": {"ok": False, "findings": []},
    }
    try:
        dirty = _git(repo, "status", "--porcelain")
        if dirty:
            raise ReleaseError("release requires a clean Git worktree")
        commit = _git(repo, "rev-parse", "HEAD")
        if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ReleaseError("HEAD is not a full Git commit")
        facts["tool_commit"] = commit
        if requirements is not None:
            facts["required_evidence"] = _validate_required_evidence(
                repo, requirements.resolve(strict=True))
        if baseline is not None:
            facts["baseline"] = _validate_baseline(
                repo, config_path=baseline[0].resolve(strict=True),
                manifest_path=baseline[1].resolve(strict=True),
                attestation_path=baseline[2].resolve(strict=True))

        python = repo / ".venv-control" / "bin" / "python"
        if not python.is_file():
            raise ReleaseError("missing control interpreter .venv-control/bin/python")
        python_json = checks_dir / "python.json"
        commands.append(_run("python", [str(python), "tests/run.py",
                                         "--json-output", str(python_json)], cwd=repo))
        commands.append(_run("server", [str(python), "tests/run_server.py"], cwd=repo))
        vitest_json = checks_dir / "vitest.json"
        commands.append(_run("frontend", ["npm", "exec", "vitest", "--", "run",
                                           "--reporter=json", f"--outputFile={vitest_json}"],
                             cwd=repo / "web"))
        commands.append(_run("web_build", ["npm", "run", "build"], cwd=repo / "web"))
        package = json.loads((repo / "web" / "package.json").read_text())
        if "test:e2e" in (package.get("scripts") or {}):
            commands.append(_run("browser_e2e", ["npm", "run", "test:e2e"],
                                 cwd=repo / "web"))
        for result in commands:
            (checks_dir / f"{result.name}.log").write_text(
                result.stdout + ("\n[stderr]\n" + result.stderr if result.stderr else ""),
                encoding="utf-8")
            if not result.ok:
                raise ReleaseError(f"{result.name} failed with exit {result.returncode}")
        facts["tests"]["python"] = _measured_result(python_json, suite="Python")
        facts["tests"]["server"] = _server_count(commands[1].stdout)
        facts["tests"]["frontend"] = _frontend_count(vitest_json)
        browser_json = repo / "web" / "test-results" / "results.json"
        if any(result.name == "browser_e2e" for result in commands):
            facts["tests"]["browser_e2e"] = _browser_count(browser_json)
        facts["commands"] = [x.public_json() for x in commands]

        for path in bundles:
            result = verify_file(path)
            facts["bundles"].append({"path": path.name, **result.to_json()})
            if not result.ok:
                raise ReleaseError(f"Bundle v2 verification failed: {path}")

        public_dir = output / "public"
        _copy_public(repo, public_paths, public_dir)
        for item in facts.get("required_evidence", []):
            source = _safe_source(repo, item["path"])
            target = public_dir / "evidence" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        bundle_dir = public_dir / "bundles"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        if len({source.name for source in bundles}) != len(bundles):
            raise ReleaseError("Bundle filenames must be unique")
        for source in bundles:
            shutil.copy2(source, bundle_dir / source.name)
        fact_conflicts = _check_public_fact_consistency(public_dir, facts["tests"])
        _write_json(checks_dir / "fact_consistency.json", {
            "ok": not fact_conflicts, "problems": fact_conflicts})
        if fact_conflicts:
            raise ReleaseError("public document test totals disagree with measured facts")

        # Numbers are only half of what a package can overstate. The capability
        # registry gates the other half: a capability that has not cleared its
        # own frozen threshold may not be described as though it had.
        registry = caps.load()
        claim_problems = caps.missing_evidence(registry, repo=repo)
        claim_problems += caps.check_documents(public_dir, capabilities=registry)
        facts["capabilities"] = {
            **caps.public_registry(registry),
            "claims_checked": True,
            "violations": len(claim_problems),
        }
        _write_json(checks_dir / "capability_claims.json", {
            "ok": not claim_problems,
            "states": caps.public_registry(registry)["counts"],
            "violations": [x.to_json() for x in claim_problems]})
        if claim_problems:
            raise ReleaseError(
                "public material overstates a capability: "
                + "; ".join(str(x) for x in claim_problems[:3]))

        # The public package carries the same safe, machine-readable fact sheet.
        facts["security"] = {"ok": True, "findings": []}
        _write_json(output / "release_facts.json", facts)
        _write_json(public_dir / "release_facts.json", facts)
        findings = scan_paths([public_dir], root=public_dir)
        scan = {"ok": not findings, "findings": [x.to_json() for x in findings]}
        facts["security"] = scan
        _write_json(checks_dir / "sensitive_scan.json", scan)
        if findings:
            _write_json(output / "release_facts.json", facts)
            raise ReleaseError("sensitive information scan failed")

        _write_json(output / "release_facts.json", facts)
        _write_json(public_dir / "release_facts.json", facts)
        archive = _archive(public_dir, output / "modou-public.tar.gz")
        gate = {
            "schema_version": "release-gate-v1", "status": "PASS",
            "tool_commit": commit, "facts": "release_facts.json",
            "archive": archive, "bundle_count": len(bundles),
            "sensitive_scan": "PASS",
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        # The one and only success gate is deliberately the final write.
        _write_json(output / "release_gate.json", gate)
        status.update(status="COMPLETE", finished_at=gate["finished_at"])
        _write_json(output / "run_status.json", status)
        return gate
    except BaseException as exc:
        for result in commands:
            log = checks_dir / f"{result.name}.log"
            if not log.exists():
                log.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
        status.update(status="FAILED", reason=f"{type(exc).__name__}: {exc}"[:500],
                      finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        _write_json(output / "run_status.json", status)
        (output / "release_gate.json").unlink(missing_ok=True)
        raise
