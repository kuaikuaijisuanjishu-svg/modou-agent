#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
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
    ("internal_research_marker", re.compile(
        r"(?:0[234]\s*§|configs/evals/|A[123]\s+(?:oracle|rerun|measured)|"
        r"(?:12-task|12-unit|30 real patches|18 non-degenerate).{0,20}(?:eval|task))",
        re.I)),
)
REQUIRED_PUBLIC_FILES = {
    Path("README.md"), Path("LICENSE"), Path("NOTICE"), Path("CHANGELOG.md"),
    Path("CONTRIBUTING.md"), Path("CODE_OF_CONDUCT.md"), Path("SECURITY.md"),
    Path("configs/capabilities.json"), Path("tests/run.py"),
    Path(".github/workflows/ci.yml"), Path(".github/workflows/release.yml"),
}


def files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    return sorted(ROOT / item.decode("utf-8")
                  for item in result.stdout.split(b"\0") if item)


def main() -> int:
    findings: list[str] = []
    present = {path.relative_to(ROOT) for path in files()}
    for required in sorted(REQUIRED_PUBLIC_FILES):
        if required not in present:
            findings.append(f"required public file missing: {required}")
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
    try:
        from modou import capabilities
        registry = capabilities.load(ROOT / "configs" / "capabilities.json")
        findings.extend(str(item) for item in capabilities.missing_evidence(
            registry, repo=ROOT))
        findings.extend(str(item) for item in capabilities.check_documents(
            ROOT, capabilities=registry))
    except Exception as exc:
        findings.append(f"capability registry invalid: {type(exc).__name__}: {exc}")
    if findings:
        print("公开发布检查失败：")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    print(f"公开发布检查通过：{len(files())} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
