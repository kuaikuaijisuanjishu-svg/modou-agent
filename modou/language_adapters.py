"""Deterministic registry for the v1 language evidence adapters."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .go_adapter import GoProject
from .java_adapter import JavaProject
from .python_adapter import PythonPytestAdapter
from .typescript_adapter import TypeScriptProject


class AdapterDiscoveryError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def supported_languages() -> tuple[str, ...]:
    return ("python", "typescript", "go", "java")


def detect_languages(root: Path) -> tuple[str, ...]:
    """Return marker-based candidates without executing project files."""
    root = Path(root).resolve(strict=True)
    candidates: list[str] = []
    if (root / "go.mod").is_file():
        candidates.append("go")
    if (root / "pom.xml").is_file():
        candidates.append("java")
    if (root / "package.json").is_file():
        candidates.append("typescript")
    if any((root / name).is_file() for name in ("pyproject.toml", "setup.py", "pytest.ini")):
        candidates.append("python")
    return tuple(candidates)


def discover_adapter(root: Path, *, language: str | None = None,
                     python: str | None = None):
    """Discover one adapter, failing closed on ambiguous repository markers."""
    root = Path(root).resolve(strict=True)
    normalized = (language or "").strip().lower()
    aliases = {"js": "typescript", "javascript": "typescript", "ts": "typescript",
               "golang": "go", "jvm": "java"}
    normalized = aliases.get(normalized, normalized)
    if normalized and normalized not in supported_languages():
        raise AdapterDiscoveryError("LANGUAGE_UNSUPPORTED", normalized)
    candidates = (normalized,) if normalized else detect_languages(root)
    if not candidates:
        raise AdapterDiscoveryError("LANGUAGE_NOT_DETECTED",
                                    "no supported project marker was found")
    if len(candidates) > 1:
        raise AdapterDiscoveryError("LANGUAGE_AMBIGUOUS", ",".join(candidates))
    selected = candidates[0]
    try:
        if selected == "go":
            return GoProject.discover(root)
        if selected == "java":
            return JavaProject.discover(root)
        if selected == "typescript":
            return TypeScriptProject.discover(root)
        interpreter = python or shutil.which("python") or sys.executable
        return PythonPytestAdapter(root, interpreter)
    except (ValueError, OSError) as exc:
        code = getattr(exc, "code", None) or "LANGUAGE_DISCOVERY_FAILED"
        raise AdapterDiscoveryError(code, str(exc)) from exc
