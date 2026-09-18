"""Closed TypeScript test-runner discovery for the future language adapter.

It intentionally never evaluates ``package.json`` scripts.  A script is
repository-controlled text and can contain arbitrary shell commands; this
adapter selects only a known runner and constructs argv itself.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .executor import CommandExecutor, sanitized_environment
from .language_adapter import (AdapterCapabilities, AdapterRunResult,
                               CommandSpec, CoverageLine, CoverageVector,
                               TestCaseFact, TestVector)


MAX_REPORT_BYTES = 32 << 20


class TypeScriptAdapterError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TypeScriptProject:
    package_manager: str
    test_runner: str
    root: Path
    package_manager_version: str = ""

    @property
    def adapter_id(self) -> str:
        return f"typescript-{self.test_runner}-v1"

    @classmethod
    def discover(cls, root: Path) -> "TypeScriptProject":
        root = Path(root).resolve(strict=True)
        package_json = root / "package.json"
        if not package_json.is_file():
            raise TypeScriptAdapterError("TS_PACKAGE_JSON_MISSING", "package.json is required")
        try:
            raw = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TypeScriptAdapterError("TS_PACKAGE_JSON_INVALID", str(exc)) from exc
        if raw.get("workspaces") or (root / "pnpm-workspace.yaml").exists():
            raise TypeScriptAdapterError(
                "TS_WORKSPACE_UNSUPPORTED",
                "workspace/monorepo projects require a separately validated adapter")
        dependencies: set[str] = set()
        for field in ("dependencies", "devDependencies", "peerDependencies"):
            values = raw.get(field) or {}
            if not isinstance(values, dict) or not all(isinstance(key, str) for key in values):
                raise TypeScriptAdapterError("TS_PACKAGE_JSON_INVALID", field)
            dependencies.update(values)
        runners = [name for name in ("vitest", "jest") if name in dependencies]
        if len(runners) != 1:
            raise TypeScriptAdapterError("TS_RUNNER_AMBIGUOUS",
                                         "exactly one of vitest or jest must be declared")
        manager = _package_manager(root)
        manager_version = _declared_package_manager(raw, manager)
        return cls(package_manager=manager, test_runner=runners[0], root=root,
                   package_manager_version=manager_version)

    def ready(self) -> bool:
        """No implicit network install: the already-installed binary is required."""
        return (self.root / "node_modules" / ".bin" / self.test_runner).is_file()

    def capabilities(self) -> AdapterCapabilities:
        reasons: list[str] = []
        if not self.ready():
            reasons.append("TS_RUNNER_NOT_INSTALLED")
        coverage = self.coverage_ready()
        if not coverage:
            reasons.append("TS_COVERAGE_PROVIDER_MISSING")
        return AdapterCapabilities(
            adapter_id=self.adapter_id, language="typescript",
            test_runner=self.test_runner, discovery=True,
            execution=self.ready(), stable_test_ids=True, coverage=coverage,
            change_line_mapping=coverage,
            experimental_units=False,
            unsupported_reasons=tuple(reasons + ["TS_AST_UNITS_NOT_IMPLEMENTED"]),
        )

    def coverage_ready(self) -> bool:
        provider = (self.root / "node_modules" / "@vitest" / "coverage-v8")
        return ((self.test_runner == "vitest" and provider.is_dir())
                or (self.test_runner == "jest" and self.ready()))

    def test_command(self, *, targets: tuple[str, ...],
                     artifact_dir: Path) -> CommandSpec:
        artifact_dir = self._artifact_dir(artifact_dir)
        output_file = artifact_dir / f"{self.test_runner}-report.json"
        return CommandSpec(
            argv=tuple(self.test_argv(targets=targets, output_file=output_file)),
            cwd=self.root, artifacts=(output_file,), network=False)

    def test_argv(self, *, targets: tuple[str, ...], output_file: Path) -> list[str]:
        if not targets:
            raise TypeScriptAdapterError("TS_TEST_SCOPE_REQUIRED", "at least one test target is required")
        targets = tuple(self._test_target(target) for target in targets)
        prefix = _runner_prefix(self.package_manager, self.test_runner)
        if self.test_runner == "vitest":
            return [*prefix, "run", *targets, "--reporter=json", f"--outputFile={output_file}"]
        return [*prefix, *targets, "--json", f"--outputFile={output_file}", "--runInBand"]

    def full_suite_argv(self, *, output_file: Path) -> list[str]:
        """Run the repository's whole declared suite.

        ``test_argv`` requires an explicit scope because a product review is
        always scoped.  The matrix measures whole repositories instead, so it
        needs a first-class way to say "everything" rather than being forced
        to fabricate a target list or to hand-build the command.
        """
        prefix = _runner_prefix(self.package_manager, self.test_runner)
        if self.test_runner == "vitest":
            return [*prefix, "run", "--reporter=json", f"--outputFile={output_file}"]
        return [*prefix, "--json", f"--outputFile={output_file}", "--runInBand"]

    def coverage_command(self, *, targets: tuple[str, ...],
                         artifact_dir: Path) -> CommandSpec:
        if not self.coverage_ready():
            raise TypeScriptAdapterError(
                "TS_COVERAGE_PROVIDER_MISSING",
                "@vitest/coverage-v8 must already be installed from the lockfile")
        artifact_dir = self._artifact_dir(artifact_dir)
        report_dir = artifact_dir / "coverage"
        argv = self.test_argv(targets=targets,
                              output_file=artifact_dir / f"{self.test_runner}-report.json")
        if self.test_runner == "vitest":
            argv.extend(["--coverage.enabled", "--coverage.provider=v8",
                         "--coverage.reporter=json",
                         f"--coverage.reportsDirectory={report_dir}"])
        else:
            argv.extend(["--coverage", "--coverageReporters=json",
                         f"--coverageDirectory={report_dir}"])
        return CommandSpec(argv=tuple(argv), cwd=self.root,
                           artifacts=(artifact_dir / f"{self.test_runner}-report.json",
                                      report_dir / "coverage-final.json"),
                           network=False)

    def run_tests(self, executor: CommandExecutor, *, targets: tuple[str, ...],
                  artifact_dir: Path, timeout: float,
                  env: dict[str, str] | None = None) -> AdapterRunResult:
        if not self.ready():
            raise TypeScriptAdapterError("TS_RUNNER_NOT_INSTALLED",
                                         "implicit dependency installation is forbidden")
        child_env = env if env is not None else sanitized_environment()
        self._validate_package_manager_version(executor, timeout=timeout,
                                               env=child_env)
        artifact_dir = self._artifact_dir(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        command = self.test_command(targets=targets, artifact_dir=artifact_dir)
        result = executor.run(list(command.argv), cwd=command.cwd, timeout=timeout,
                              env=child_env)
        report = command.artifacts[0]
        if report.is_symlink() or not report.is_file():
            raise TypeScriptAdapterError("TS_REPORT_MISSING",
                                         "runner did not produce its declared JSON artifact")
        if report.stat().st_size > MAX_REPORT_BYTES:
            raise TypeScriptAdapterError("TS_REPORT_TOO_LARGE",
                                         f"JSON report exceeds {MAX_REPORT_BYTES} bytes")
        payload = report.read_bytes()
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TypeScriptAdapterError("TS_REPORT_INVALID", str(exc)) from exc
        vector = parse_test_vector(raw, runner=self.test_runner, root=self.root)
        return AdapterRunResult(
            returncode=result.returncode, tests=vector,
            report_sha256=hashlib.sha256(payload).hexdigest(), command=command)

    def _validate_package_manager_version(self, executor: CommandExecutor, *,
                                          timeout: float,
                                          env: dict[str, str]) -> None:
        if not self.package_manager_version:
            return
        result = executor.run([self.package_manager, "--version"], cwd=self.root,
                              timeout=min(timeout, 15), env=env)
        observed = (result.stdout or "").strip().splitlines()
        actual = observed[-1].strip() if observed else ""
        if result.returncode or actual != self.package_manager_version:
            raise TypeScriptAdapterError(
                "TS_PACKAGE_MANAGER_VERSION_MISMATCH",
                f"packageManager requires {self.package_manager}@{self.package_manager_version}; "
                f"observed {actual or 'unavailable'}")

    def _test_target(self, target: str) -> str:
        path = Path(target)
        if path.is_absolute() or ".." in path.parts:
            raise TypeScriptAdapterError("TS_TEST_PATH_ESCAPE", target)
        try:
            resolved = (self.root / path).resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise TypeScriptAdapterError("TS_TEST_PATH_INVALID", target) from exc
        if resolved.is_symlink() or not resolved.is_file():
            raise TypeScriptAdapterError("TS_TEST_PATH_INVALID", target)
        relative = resolved.relative_to(self.root).as_posix()
        if relative != path.as_posix().lstrip("./"):
            raise TypeScriptAdapterError("TS_TEST_PATH_SYMLINK", target)
        return relative

    def _artifact_dir(self, artifact_dir: Path) -> Path:
        resolved = Path(artifact_dir).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return resolved
        raise TypeScriptAdapterError(
            "TS_ARTIFACT_INSIDE_REPOSITORY",
            "runner reports must be written to review-owned scratch")


def _package_manager(root: Path) -> str:
    locks = [("pnpm", root / "pnpm-lock.yaml"), ("npm", root / "package-lock.json"),
             ("yarn", root / "yarn.lock")]
    found = [name for name, path in locks if path.is_file()]
    if len(found) != 1:
        raise TypeScriptAdapterError("TS_PACKAGE_MANAGER_AMBIGUOUS",
                                     "exactly one supported lockfile is required")
    return found[0]


def _declared_package_manager(package_json: dict, locked: str) -> str:
    declared = package_json.get("packageManager")
    if declared is None:
        return ""
    if not isinstance(declared, str) or "@" not in declared:
        raise TypeScriptAdapterError(
            "TS_PACKAGE_MANAGER_FIELD_INVALID", "packageManager must be name@version")
    name, version = declared.split("@", 1)
    if name != locked:
        raise TypeScriptAdapterError(
            "TS_PACKAGE_MANAGER_MISMATCH",
            f"packageManager declares {name}, lockfile requires {locked}")
    if not version or any(ch.isspace() for ch in version):
        raise TypeScriptAdapterError(
            "TS_PACKAGE_MANAGER_FIELD_INVALID", "package manager version is required")
    return version


def _runner_prefix(manager: str, runner: str) -> list[str]:
    # Every form avoids package download.  ``npm exec --no`` refuses a missing
    # local binary, unlike npx's historic implicit-install behaviour.
    return {
        "pnpm": ["pnpm", "exec", runner],
        "yarn": ["yarn", runner],
        "npm": ["npm", "exec", "--no", "--", runner],
    }[manager]


def locked_install_command(project: TypeScriptProject, *, registry: str,
                           download_limit_bytes: int) -> CommandSpec:
    """Describe a lockfile-only, lifecycle-disabled install; never execute it here."""
    if registry not in {"https://registry.npmjs.org", "https://registry.npmmirror.com"}:
        raise TypeScriptAdapterError("TS_REGISTRY_NOT_ALLOWED", registry)
    if not 1 <= int(download_limit_bytes) <= 1_000_000_000:
        raise TypeScriptAdapterError("TS_DOWNLOAD_LIMIT_INVALID", str(download_limit_bytes))
    argv = {
        "npm": ("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund", f"--registry={registry}"),
        "pnpm": ("pnpm", "install", "--frozen-lockfile", "--ignore-scripts", f"--registry={registry}"),
        "yarn": ("yarn", "install", "--immutable", "--mode=skip-build", f"--registry={registry}"),
    }[project.package_manager]
    return CommandSpec(argv, project.root, (), network=True)


def parse_test_vector(raw: Any, *, runner: str,
                      root: Path | None = None,
                      duplicate_identity_policy: str = "reject") -> TestVector:
    """Normalize JSON reporter output to stable repository-relative identities."""
    if runner not in {"vitest", "jest"} or not isinstance(raw, dict):
        raise TypeScriptAdapterError("TS_REPORT_INVALID", "unsupported report")
    if duplicate_identity_policy not in {"reject", "path_full_name_ordinal_v1"}:
        raise TypeScriptAdapterError(
            "TS_REPORT_INVALID", "unsupported duplicate identity policy")
    pending: list[tuple[str, str, str, float | None]] = []
    for file_result in raw.get("testResults") or []:
        if not isinstance(file_result, dict):
            raise TypeScriptAdapterError("TS_REPORT_INVALID", "test result is not an object")
        path = _report_path(str(file_result.get("name") or file_result.get("file") or ""),
                            root)
        for item in file_result.get("assertionResults") or []:
            if not isinstance(item, dict):
                raise TypeScriptAdapterError("TS_REPORT_INVALID", "assertion is not an object")
            title = str(item.get("fullName") or item.get("title") or "")
            if not path or not title:
                raise TypeScriptAdapterError("TS_REPORT_INVALID", "test identity is missing")
            status = {"passed": "passed", "failed": "failed",
                      "pending": "skipped", "skipped": "skipped",
                      "todo": "todo"}.get(str(item.get("status") or ""))
            if status is None:
                raise TypeScriptAdapterError("TS_REPORT_INVALID", "unsupported test status")
            duration = item.get("duration")
            if duration is not None and not isinstance(duration, (int, float)):
                raise TypeScriptAdapterError("TS_REPORT_INVALID", "duration must be numeric")
            pending.append((path, title, status,
                            None if duration is None else float(duration)))
    base_counts: dict[str, int] = {}
    for path, title, _, _ in pending:
        base = f"{path}::{title}"
        base_counts[base] = base_counts.get(base, 0) + 1
    if duplicate_identity_policy == "reject":
        duplicate = next((base for base, count in base_counts.items() if count > 1), None)
        if duplicate is not None:
            raise TypeScriptAdapterError("TS_REPORT_AMBIGUOUS", duplicate)
    ordinals: dict[str, int] = {}
    rows: list[TestCaseFact] = []
    seen: set[str] = set()
    for path, title, status, duration in pending:
        base = f"{path}::{title}"
        fact_name = title
        if base_counts[base] > 1:
            ordinals[base] = ordinals.get(base, 0) + 1
            fact_name = f"{title}::#shuimu-duplicate-ordinal={ordinals[base]}"
        identifier = f"{path}::{fact_name}"
        # A repository-controlled title could itself look like the suffix.
        # Refuse such a collision instead of silently merging two tests.
        if identifier in seen:
            raise TypeScriptAdapterError("TS_REPORT_AMBIGUOUS", identifier)
        seen.add(identifier)
        rows.append(TestCaseFact(identifier, path, fact_name, status, duration))
    if not rows:
        raise TypeScriptAdapterError("TS_REPORT_EMPTY", "report contains no assertions")
    declared_total = raw.get("numTotalTests")
    if declared_total is not None and declared_total != len(rows):
        raise TypeScriptAdapterError("TS_REPORT_COUNT_MISMATCH",
                                     f"declared {declared_total}, parsed {len(rows)}")
    return TestVector(tuple(rows))


def parse_test_report(raw: Any, *, runner: str) -> dict[str, bool]:
    """Compatibility projection used by the earlier TypeScript prototype."""
    vector = parse_test_vector(raw, runner=runner)
    return {case.test_id: case.status == "passed" for case in vector.cases}


def parse_istanbul_coverage(raw: Any, *, root: Path) -> CoverageVector:
    """Normalize Istanbul coverage-final.json to stable line hit facts."""
    if not isinstance(raw, dict):
        raise TypeScriptAdapterError("TS_COVERAGE_INVALID", "coverage must be an object")
    root = Path(root).resolve(strict=True)
    hits_by_line: dict[tuple[str, int], int] = {}
    for raw_path, file_data in raw.items():
        if not isinstance(raw_path, str) or not isinstance(file_data, dict):
            raise TypeScriptAdapterError("TS_COVERAGE_INVALID", "invalid file coverage entry")
        path = _report_path(str(file_data.get("path") or raw_path), root)
        statements = file_data.get("statementMap")
        counts = file_data.get("s")
        if not isinstance(statements, dict) or not isinstance(counts, dict):
            raise TypeScriptAdapterError("TS_COVERAGE_INVALID", path)
        if set(statements) != set(counts):
            raise TypeScriptAdapterError("TS_COVERAGE_COUNT_MISMATCH", path)
        for key, span in statements.items():
            count = counts[key]
            if (not isinstance(span, dict) or not isinstance(count, int)
                    or count < 0):
                raise TypeScriptAdapterError("TS_COVERAGE_INVALID", path)
            start = span.get("start") or {}
            line = start.get("line") if isinstance(start, dict) else None
            if not isinstance(line, int) or line < 1:
                raise TypeScriptAdapterError("TS_COVERAGE_INVALID", path)
            identity = (path, line)
            hits_by_line[identity] = max(hits_by_line.get(identity, 0), count)
    if not hits_by_line:
        raise TypeScriptAdapterError("TS_COVERAGE_EMPTY", "coverage contains no statements")
    return CoverageVector(tuple(
        CoverageLine(path, line, hits)
        for (path, line), hits in sorted(hits_by_line.items())))


def _report_path(value: str, root: Path | None) -> str:
    if not value:
        return ""
    path = Path(value)
    if root is None:
        if path.is_absolute():
            raise TypeScriptAdapterError("TS_REPORT_ROOT_REQUIRED",
                                         "absolute report paths require a repository root")
        if ".." in path.parts:
            raise TypeScriptAdapterError("TS_REPORT_PATH_ESCAPE", value)
        return path.as_posix().lstrip("./")
    root = Path(root).resolve(strict=True)
    resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise TypeScriptAdapterError("TS_REPORT_PATH_ESCAPE", value) from exc
