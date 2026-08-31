#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {
    "runs", "experiments", "archive", "reports", "scratchpad", "private",
    "evaluation", "e2e", "node_modules", "dist",
}
FORBIDDEN_NAMES = re.compile(
    r"(?:capture_|freeze|aggregate_|evaluate|validate|run_day|run_.*eval|"
    r"archive_formal|probe_model|github_app|real-backend|synthetic)", re.I)
TEXT_SUFFIXES = {
    ".css", ".html", ".ini", ".js", ".json", ".md", ".py", ".sh",
    ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
SCAN_EXEMPTIONS = {
    Path("tools/public_release_check.py"),
    Path("modou/review_bundle.py"),
    Path("modou/sensitive.py"),
    Path("tests/test_public_smoke.py"),
}
PATTERNS = (
    ("mac_user_path", re.compile(r"/Users/[^/\s\"']+")),
    ("linux_home_path", re.compile(r"(?<![\w])/home/[^/\s\"']+")),
    ("encoded_user_path", re.compile(r"(?:^|[=/_-])-Users-[A-Za-z0-9._-]+-Desktop-")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)),
    ("openai_key_shape", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token_shape", re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("credential_assignment", re.compile(r"\b(?:api[_-]?key|secret|access[_-]?token|password)\b[\"']?\s*[:=]\s*[\"'][^\"']{8,}[\"']", re.I)),
)


def files() -> list[Path]:
    return sorted(
        path for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "node_modules" not in path.parts
        and "dist" not in path.parts
    )


def main() -> int:
    findings: list[str] = []
    for path in files():
        relative = path.relative_to(ROOT)
        lowered_parts = {part.lower() for part in relative.parts}
        if lowered_parts & FORBIDDEN_PARTS or (
            relative.parts and relative.parts[0] == "tools"
            and FORBIDDEN_NAMES.search(path.name)
        ):
            findings.append(f"forbidden path: {relative}")
            continue
        if relative in SCAN_EXEMPTIONS:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(f"unreadable text: {relative}")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for rule, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{relative}:{line_number}: {rule}")
    if findings:
        print("公开发布检查失败：")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    print(f"公开发布检查通过：{len(files())} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
