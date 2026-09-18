"""Go test evidence adapter.

The adapter deliberately consumes ``go test -json`` instead of parsing human
terminal output.  It constructs a fixed argv and never evaluates repository
scripts or downloads dependencies.  A caller still chooses the executor
(trusted local or sandboxed), so the normal resource and process boundaries
remain in force.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .executor import CommandExecutor, sanitized_environment
from .language_adapter import AdapterCapabilities, AdapterRunResult, CommandSpec
from .language_adapter import TestCaseFact, TestVector


MAX_REPORT_BYTES = 32 << 20
_MODULE_RE = re.compile(r"(?m)^\s*module\s+([^\s]+)\s*$")
_ACTIONS = {"pass": "passed", "fail": "failed", "skip": "skipped"}


class GoAdapterError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class GoProject:
    """A single-module Go project with a deterministic test command."""

    adapter_id = "go-test-v1"

    def __init__(self, root: Path, module: str):
        self.root = Path(root).resolve(strict=True)
        self.module = module

    @classmethod
    def discover(cls, root: Path) -> "GoProject":
        root = Path(root).resolve(strict=True)
        go_mod = root / "go.mod"
        if go_mod.is_symlink() or not go_mod.is_file():
            raise GoAdapterError("GO_MOD_MISSING", "go.mod is required")
        if (root / "go.work").exists():
            raise GoAdapterError("GO_WORKSPACE_UNSUPPORTED",
                                  "go.work multi-module projects require explicit support")
        try:
            raw = go_mod.read_text(encoding="utf-8")
        except OSError as exc:
            raise GoAdapterError("GO_MOD_INVALID", str(exc)) from exc
        match = _MODULE_RE.search(raw)
        if not match or any(ch in match.group(1) for ch in "\x00\r\n"):
            raise GoAdapterError("GO_MOD_INVALID", "module declaration is missing")
        return cls(root, match.group(1))

    def capabilities(self) -> AdapterCapabilities:
        reasons: list[str] = []
        if shutil.which("go") is None:
            reasons.append("GO_RUNNER_NOT_INSTALLED")
        reasons.extend(("GO_COVERAGE_NOT_NORMALIZED", "GO_CHANGE_LINE_MAPPING_NOT_IMPLEMENTED"))
        return AdapterCapabilities(
            adapter_id=self.adapter_id, language="go", test_runner="go test",
            discovery=True, execution=shutil.which("go") is not None,
            stable_test_ids=True, coverage=False, change_line_mapping=False,
            experimental_units=False, unsupported_reasons=tuple(reasons))

    def test_command(self, *, targets: tuple[str, ...], artifact_dir: Path) -> CommandSpec:
        if not targets:
            raise GoAdapterError("GO_TEST_SCOPE_REQUIRED", "at least one package target is required")
        safe = tuple(self._target(value) for value in targets)
        scratch = Path(artifact_dir).resolve()
        try:
            scratch.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise GoAdapterError("GO_ARTIFACT_INSIDE_REPOSITORY",
                                  "evidence artifacts must be written to review-owned scratch")
        report = scratch / "go-test.jsonl"
        return CommandSpec(("go", "test", "-count=1", "-json", *safe),
                           self.root, (report,), network=False)

    @staticmethod
    def _target(value: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise GoAdapterError("GO_TEST_TARGET_INVALID", str(value))
        if value.startswith("-") or value.startswith("/") or ".." in Path(value).parts:
            raise GoAdapterError("GO_TEST_TARGET_INVALID", value)
        if any(ch.isspace() for ch in value) or "//" in value:
            raise GoAdapterError("GO_TEST_TARGET_INVALID", value)
        # Package patterns are intentionally the only scope language exposed.
        if not (value == "./..." or value.startswith("./") or value.startswith(".")):
            raise GoAdapterError("GO_TEST_TARGET_INVALID", value)
        return value

    @staticmethod
    def parse_report(raw: str | bytes) -> TestVector:
        if isinstance(raw, bytes):
            payload = raw
            text = raw.decode("utf-8", errors="strict")
        else:
            text = raw
            payload = raw.encode("utf-8")
        if len(payload) > MAX_REPORT_BYTES:
            raise GoAdapterError("GO_REPORT_TOO_LARGE", str(len(payload)))
        statuses: dict[str, tuple[str, str, float | None]] = {}
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GoAdapterError("GO_REPORT_INVALID", f"line {line_no}: {exc}") from exc
            if not isinstance(event, dict):
                raise GoAdapterError("GO_REPORT_INVALID", f"line {line_no}: event is not an object")
            action = str(event.get("Action") or "")
            status = _ACTIONS.get(action)
            test = event.get("Test")
            package = event.get("Package")
            if status is None or not test:
                continue
            if not isinstance(test, str) or not isinstance(package, str) or not package:
                raise GoAdapterError("GO_REPORT_INVALID", f"line {line_no}: test identity")
            if "\x00" in test or "\x00" in package or "::" in test or "::" in package:
                raise GoAdapterError("GO_REPORT_INVALID", f"line {line_no}: unsafe test identity")
            duration = event.get("Elapsed")
            if duration is not None and not isinstance(duration, (int, float)):
                raise GoAdapterError("GO_REPORT_INVALID", f"line {line_no}: duration")
            identity = f"{package}::{test}"
            if identity in statuses and statuses[identity][0] != status:
                # A retry with two outcomes is ambiguous evidence for v1.
                raise GoAdapterError("GO_REPORT_AMBIGUOUS", identity)
            statuses[identity] = (status, package,
                                  None if duration is None else float(duration) * 1000)
        if not statuses:
            raise GoAdapterError("GO_REPORT_EMPTY", "report contains no test events")
        rows = tuple(TestCaseFact(identity, data[1], identity.split("::", 1)[1], data[0], data[2])
                     for identity, data in sorted(statuses.items()))
        return TestVector(rows)

    def run_tests(self, executor: CommandExecutor, *, targets: tuple[str, ...],
                  artifact_dir: Path, timeout: float) -> AdapterRunResult:
        command = self.test_command(targets=targets, artifact_dir=artifact_dir)
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        result = executor.run(list(command.argv), cwd=self.root, timeout=timeout,
                              env=sanitized_environment())
        stdout = result.stdout or ""
        payload = stdout.encode("utf-8") if isinstance(stdout, str) else bytes(stdout)
        command.artifacts[0].write_bytes(payload)
        vector = self.parse_report(payload)
        return AdapterRunResult(result.returncode, vector,
                                hashlib.sha256(payload).hexdigest(), command)
