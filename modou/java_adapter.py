"""Offline Maven/Surefire adapter for Java test evidence."""
from __future__ import annotations

import hashlib
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from .executor import CommandExecutor, sanitized_environment
from .language_adapter import AdapterCapabilities, AdapterRunResult, CommandSpec
from .language_adapter import TestCaseFact, TestVector


MAX_REPORT_BYTES = 32 << 20


class JavaAdapterError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class JavaProject:
    adapter_id = "java-maven-surefire-v1"

    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)

    @classmethod
    def discover(cls, root: Path) -> "JavaProject":
        root = Path(root).resolve(strict=True)
        pom = root / "pom.xml"
        if pom.is_symlink() or not pom.is_file():
            raise JavaAdapterError("JAVA_POM_MISSING", "pom.xml is required")
        try:
            raw = pom.read_bytes()
            # ElementTree is used only after refusing the entity-bearing forms
            # that could otherwise expand unbounded input during parsing.
            upper = raw.upper()
            if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                raise JavaAdapterError("JAVA_POM_UNSAFE", "DOCTYPE/ENTITY is not accepted")
            ET.fromstring(raw)
        except JavaAdapterError:
            raise
        except (OSError, ET.ParseError) as exc:
            raise JavaAdapterError("JAVA_POM_INVALID", str(exc)) from exc
        if (root / "pom.xml").is_symlink():
            raise JavaAdapterError("JAVA_POM_UNSAFE", "pom.xml must not be a symlink")
        return cls(root)

    def capabilities(self) -> AdapterCapabilities:
        reasons: list[str] = []
        if shutil.which("mvn") is None:
            reasons.append("JAVA_MAVEN_NOT_INSTALLED")
        reasons.extend(("JAVA_COVERAGE_NOT_NORMALIZED", "JAVA_CHANGE_LINE_MAPPING_NOT_IMPLEMENTED"))
        return AdapterCapabilities(
            adapter_id=self.adapter_id, language="java", test_runner="maven-surefire",
            discovery=True, execution=shutil.which("mvn") is not None,
            stable_test_ids=True, coverage=False, change_line_mapping=False,
            experimental_units=False, unsupported_reasons=tuple(reasons))

    def test_command(self, *, targets: tuple[str, ...], artifact_dir: Path) -> CommandSpec:
        if targets:
            raise JavaAdapterError("JAVA_SCOPE_UNSUPPORTED",
                                   "v1 runs the declared Maven test suite as one scope")
        scratch = Path(artifact_dir).resolve()
        try:
            scratch.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise JavaAdapterError("JAVA_ARTIFACT_INSIDE_REPOSITORY",
                                   "evidence artifacts must be written to review-owned scratch")
        report_dir = scratch / "target" / "surefire-reports"
        argv = ("mvn", "-o", "-q", "-DskipTests=false",
                f"-Dproject.build.directory={scratch / 'target'}", "test")
        return CommandSpec(argv, self.root, (report_dir,), network=False)

    @staticmethod
    def parse_reports(reports: list[Path] | tuple[Path, ...]) -> TestVector:
        rows: list[TestCaseFact] = []
        seen: set[str] = set()
        total = 0
        for report in sorted((Path(path) for path in reports), key=lambda p: p.as_posix()):
            if report.is_symlink() or not report.is_file():
                raise JavaAdapterError("JAVA_REPORT_INVALID", str(report))
            payload = report.read_bytes()
            total += len(payload)
            if total > MAX_REPORT_BYTES:
                raise JavaAdapterError("JAVA_REPORT_TOO_LARGE", str(total))
            upper = payload.upper()
            if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                raise JavaAdapterError("JAVA_REPORT_UNSAFE", str(report))
            try:
                suite = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise JavaAdapterError("JAVA_REPORT_INVALID", str(report)) from exc
            for case in suite.findall(".//testcase"):
                classname = (case.attrib.get("classname") or report.stem).strip()
                name = (case.attrib.get("name") or "").strip()
                if not classname or not name or "\x00" in classname or "\x00" in name:
                    raise JavaAdapterError("JAVA_REPORT_INVALID", f"test identity in {report.name}")
                test_id = f"{classname}::{name}"
                if test_id in seen:
                    raise JavaAdapterError("JAVA_REPORT_AMBIGUOUS", test_id)
                seen.add(test_id)
                if case.find("skipped") is not None:
                    status = "skipped"
                elif case.find("failure") is not None or case.find("error") is not None:
                    status = "failed"
                else:
                    status = "passed"
                duration = case.attrib.get("time")
                try:
                    duration_ms = None if duration is None else float(duration) * 1000
                except ValueError as exc:
                    raise JavaAdapterError("JAVA_REPORT_INVALID", "duration") from exc
                rows.append(TestCaseFact(test_id, classname, name, status, duration_ms))
        if not rows:
            raise JavaAdapterError("JAVA_REPORT_EMPTY", "Surefire reports contain no testcases")
        return TestVector(tuple(rows))

    def run_tests(self, executor: CommandExecutor, *, targets: tuple[str, ...],
                  artifact_dir: Path, timeout: float) -> AdapterRunResult:
        command = self.test_command(targets=targets, artifact_dir=artifact_dir)
        Path(artifact_dir).mkdir(parents=True, exist_ok=True)
        result = executor.run(list(command.argv), cwd=self.root, timeout=timeout,
                              env=sanitized_environment())
        report_dir = command.artifacts[0]
        reports = tuple(report_dir.glob("TEST-*.xml")) if report_dir.is_dir() else ()
        vector = self.parse_reports(reports)
        digest = hashlib.sha256()
        for report in reports:
            digest.update(report.name.encode("utf-8"))
            digest.update(report.read_bytes())
        return AdapterRunResult(result.returncode, vector, digest.hexdigest(), command)
