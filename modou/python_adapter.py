"""Python/pytest implementation of the shared shell-free adapter contract."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .executor import CommandExecutor, sanitized_environment
from .language_adapter import (AdapterCapabilities, AdapterRunResult, CommandSpec,
                               TestCaseFact, TestVector)


class PythonAdapterError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class PythonPytestAdapter:
    adapter_id = "python-pytest-v1"

    def __init__(self, root: Path, python: str):
        self.root = Path(root).resolve(strict=True)
        self.python = str(python)

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(self.adapter_id, "python", "pytest", True,
                                   True, True, True, True, True)

    def test_command(self, *, targets: tuple[str, ...], artifact_dir: Path) -> CommandSpec:
        if not targets:
            raise PythonAdapterError("PY_TEST_SCOPE_REQUIRED", "at least one test target is required")
        safe = []
        for value in targets:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise PythonAdapterError("PY_TEST_PATH_ESCAPE", value)
            resolved = (self.root / path).resolve(strict=True)
            try:
                resolved.relative_to(self.root)
            except ValueError as exc:
                raise PythonAdapterError("PY_TEST_PATH_ESCAPE", value) from exc
            safe.append(path.as_posix())
        report = Path(artifact_dir).resolve() / "pytest-report.json"
        return CommandSpec((self.python, "-m", "pytest", *safe, "-q",
                            f"--json-report-file={report}"), self.root, (report,))

    @staticmethod
    def parse_report(raw: dict, *, root: Path) -> TestVector:
        rows = []
        for item in raw.get("tests") or []:
            nodeid = str(item.get("nodeid") or "")
            if "::" not in nodeid:
                raise PythonAdapterError("PY_REPORT_INVALID", "nodeid")
            path, name = nodeid.split("::", 1)
            status = {"passed": "passed", "failed": "failed",
                      "skipped": "skipped"}.get(str(item.get("outcome") or ""))
            if status is None:
                raise PythonAdapterError("PY_REPORT_INVALID", "outcome")
            rows.append(TestCaseFact(nodeid, path, name, status,
                                     float(item.get("duration") or 0) * 1000))
        return TestVector(tuple(rows))

    def run_tests(self, executor: CommandExecutor, *, targets: tuple[str, ...],
                  artifact_dir: Path, timeout: float) -> AdapterRunResult:
        command = self.test_command(targets=targets, artifact_dir=artifact_dir)
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        result = executor.run(list(command.argv), cwd=self.root, timeout=timeout,
                              env=sanitized_environment())
        payload = command.artifacts[0].read_bytes()
        vector = self.parse_report(json.loads(payload), root=self.root)
        return AdapterRunResult(result.returncode, vector,
                                hashlib.sha256(payload).hexdigest(), command)
