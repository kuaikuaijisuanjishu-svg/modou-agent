"""Conservative secret/private-trace scanner for public release artifacts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str

    def to_json(self) -> dict:
        return {"path": self.path, "line": self.line, "rule": self.rule,
                "excerpt": self.excerpt}


RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Mentions such as "we exclude private traces" are safe documentation;
    # reject serialized private-trace fields rather than the phrase itself.
    ("private_trace", re.compile(
        r"[\"']private[_ -]?(?:model[_ -]?)?trace[\"']\s*:", re.I)),
    ("mac_user_path", re.compile(r"/Users/[^/\s\"']+")),
    # Left-anchored on purpose so a real home-directory path is detected
    # without treating an unrelated embedded fragment as a user path.
    ("linux_home_path", re.compile(r"(?<![\w])/home/[^/\s\"']+")),
    ("openai_key_shape", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token_shape", re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{16,}\b", re.I)),
    ("secret_assignment", re.compile(
        r"\b(?:api[_-]?key|secret|access[_-]?token|password)\b[\"']?\s*[:=]\s*[\"'][^\"']{8,}[\"']",
        re.I)),
)

TEXT_SUFFIXES = frozenset({
    ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsonl", ".md",
    ".py", ".rst", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
})


def scan_text(text: str, *, path: str = "<memory>") -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, pattern in RULES:
            match = pattern.search(line)
            if match:
                # Never echo a possible credential into release logs.
                excerpt = line[max(0, match.start() - 24):match.start()] + "<redacted>"
                findings.append(Finding(path, lineno, name, excerpt[-80:]))
    return findings


def scan_paths(paths: Iterable[Path], *, root: Path | None = None,
               max_bytes: int = 5_000_000) -> list[Finding]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files.append(path)
    findings: list[Finding] = []
    for path in sorted(set(files)):
        lowered = [part.lower() for part in path.parts]
        if "private" in lowered and "trace" in path.name.lower():
            shown = path.as_posix() if root is None else path.relative_to(root).as_posix()
            findings.append(Finding(shown, 0, "private_trace_file", "<redacted>"))
            continue
        if path.stat().st_size > max_bytes or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            shown = path.relative_to(root).as_posix() if root else path.as_posix()
        except ValueError:
            shown = path.name
        findings.extend(scan_text(text, path=shown))
    return findings
