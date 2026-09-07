"""Shared, shell-free contract for language-specific evidence adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


LANGUAGE_ADAPTER_VERSION = "1"
TEST_STATUSES = frozenset({"passed", "failed", "skipped", "todo"})


@dataclass(frozen=True)
class AdapterCapabilities:
    adapter_id: str
    language: str
    test_runner: str
    discovery: bool
    execution: bool
    stable_test_ids: bool
    coverage: bool
    change_line_mapping: bool
    experimental_units: bool
    unsupported_reasons: tuple[str, ...] = ()
    version: str = LANGUAGE_ADAPTER_VERSION

    def as_dict(self) -> dict:
        return {
            "adapter_id": self.adapter_id,
            "version": self.version,
            "language": self.language,
            "test_runner": self.test_runner,
            "discovery": self.discovery,
            "execution": self.execution,
            "stable_test_ids": self.stable_test_ids,
            "coverage": self.coverage,
            "change_line_mapping": self.change_line_mapping,
            "experimental_units": self.experimental_units,
            "unsupported_reasons": list(self.unsupported_reasons),
        }


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path
    artifacts: tuple[Path, ...]
    network: bool = False
    shell: bool = False

    def __post_init__(self) -> None:
        if not self.argv or any(not item or "\x00" in item for item in self.argv):
            raise ValueError("adapter command argv must contain non-empty strings")
        if self.shell:
            raise ValueError("language adapters may not request a shell")


@dataclass(frozen=True)
class TestCaseFact:
    test_id: str
    path: str
    name: str
    status: str
    duration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.status not in TEST_STATUSES:
            raise ValueError(f"unsupported test status: {self.status}")
        if not self.test_id or self.test_id != f"{self.path}::{self.name}":
            raise ValueError("test id must be the stable path::name identity")


@dataclass(frozen=True)
class TestVector:
    cases: tuple[TestCaseFact, ...]

    def __post_init__(self) -> None:
        identities = [case.test_id for case in self.cases]
        if not identities or len(identities) != len(set(identities)):
            raise ValueError("test vector must contain unique stable identities")

    def as_status_map(self) -> dict[str, str]:
        return {case.test_id: case.status for case in self.cases}


@dataclass(frozen=True)
class CoverageLine:
    path: str
    line: int
    hits: int


@dataclass(frozen=True)
class CoverageVector:
    lines: tuple[CoverageLine, ...]

    def as_hit_map(self) -> dict[str, int]:
        return {f"{item.path}:{item.line}": item.hits for item in self.lines}

    def map_changed_lines(self, changed: dict[str, tuple[int, ...]]) -> dict[str, int | None]:
        hits = self.as_hit_map()
        return {
            f"{path}:{line}": hits.get(f"{path}:{line}")
            for path in sorted(changed)
            for line in sorted(set(changed[path]))
        }


@dataclass(frozen=True)
class AdapterRunResult:
    returncode: int
    tests: TestVector
    report_sha256: str
    command: CommandSpec


class LanguageAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    def capabilities(self) -> AdapterCapabilities: ...

    def test_command(self, *, targets: tuple[str, ...],
                     artifact_dir: Path) -> CommandSpec: ...
