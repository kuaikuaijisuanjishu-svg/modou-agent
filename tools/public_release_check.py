#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FORBIDDEN_PARTS = {
    "runs", "experiments", "archive", "reports", "scratchpad", "private",
    "evaluation", "node_modules", "dist",
}
FORBIDDEN_NAMES = re.compile(
    r"(?:capture_|freeze|aggregate_|evaluate|validate|run_day|run_.*eval|"
    r"archive_formal|probe_model|github_app|real-backend|synthetic)", re.I)
TEXT_SUFFIXES = {
    ".css", ".html", ".ini", ".js", ".json", ".md", ".py", ".sh",
    ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
PUBLIC_SCAN_EXEMPTIONS = frozenset({
    Path("tools/public_release_check.py"),
    Path("modou/review_bundle.py"),
    Path("modou/sensitive.py"),
    Path("tests/test_public_smoke.py"),
})

# These are deliberately file-level exemptions in the internal RC only.  They
# are negative fixtures: removing the samples would make the scanner tests
# pass without testing the scanner.  The public scope never applies this map.
SCAN_EXEMPTIONS = {
    **{path: "scanner implementation contains its own detection regex or public negative fixture"
       for path in PUBLIC_SCAN_EXEMPTIONS},
    Path("tests/test_release.py"): (
        "negative sensitive-scanner fixtures: two provider-key shapes and two "
        "credential assignments (lines 16, 19, 23)"),
    Path("tests/test_repair_candidate.py"): (
        "negative repair-context fixture: provider-key and credential shapes "
        "must remain rejected (line 50)"),
    Path("tests/test_bundle_v2.py"): (
        "negative Bundle sanitization fixture: two personal-path occurrences "
        "must be rejected and redacted (lines 84, 88)"),
    Path("tests/test_github_app_receipt.py"): (
        "negative receipt fixture: a personal secret-storage path must be "
        "redacted (line 50)"),
    Path("tests/test_review_bundle.py"): (
        "negative Bundle export fixture: a personal artifact path must be "
        "redacted (line 84)"),
    Path("tests/test_review_memory.py"): (
        "negative memory fixture: a personal path in a rule must be rejected "
        "(line 30)"),
}

# A v0.2 RC contains research-only files that must not be mistaken for the
# public subset.  The internal entry keeps them present and scans them for
# secrets, while allowing the marker that identifies research material.
INTERNAL_RC_CONTENT_ALLOWLIST = {
    Path("configs/capabilities.json"): (
        "v0.2 capability registry intentionally names internal evidence; the "
        "public checkout has its own registry and is checked in public scope"),
}
INTERNAL_RC_FORCE_SCAN = frozenset({
    Path("modou/agent/memory.py"),
    Path("打开方式.md"),
})
INTERNAL_MARKER_RULE = "internal_research_marker"
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
# Release Notes are a separate public surface.  Keep this deny-list focused on
# positive overclaims so the required negative wording ("not stable", "no
# arbitrary-repository support") remains usable while accidental marketing
# language fails closed.
RELEASE_CLAIM_PATTERNS = (
    ("release_stable_claim", re.compile(
        r"(?i)(?:generally\s+available|production[- ]ready|stable\s+release|支持任意仓库|兼容任意仓库|真人研究已完成|真实开发者研究已完成)")),
    ("release_automation_claim", re.compile(
        r"(?i)(?:自动\s*(?:推送|合并|发布)|auto[- ]?(?:push|merge|publish))")),
    ("release_rate_claim", re.compile(r"(?i)(?:理解率|成功率|通过率|wilson|confidence\s+interval)")),
)
REQUIRED_PUBLIC_FILES = {
    Path("README.md"), Path("LICENSE"), Path("NOTICE"), Path("CHANGELOG.md"),
    Path("CONTRIBUTING.md"), Path("CODE_OF_CONDUCT.md"), Path("SECURITY.md"),
    Path("configs/capabilities.json"), Path("tests/run.py"),
    Path(".github/workflows/ci.yml"), Path(".github/workflows/release.yml"),
    Path("web/e2e/public-flow.spec.ts"), Path("web/e2e/serve_fixture.py"),
}


def files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, check=True, capture_output=True,
    )
    return sorted(ROOT / item.decode("utf-8")
                  for item in result.stdout.split(b"\0") if item)


def _git_text(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _materialize_ref(ref: str, destination: Path) -> list[Path]:
    """Materialize one tracked Git tree without switching either checkout."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", ref],
        cwd=ROOT, check=True, capture_output=True,
    )
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe path in {ref}: {relative}")
        blob = subprocess.run(
            ["git", "show", f"{ref}:{relative.as_posix()}"],
            cwd=ROOT, check=True, capture_output=True,
        ).stdout
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
        paths.append(target)
    return sorted(paths)


def _forbidden_path(relative: Path) -> bool:
    lowered_parts = {part.lower() for part in relative.parts}
    return bool(lowered_parts & FORBIDDEN_PARTS or (
        relative.parts and relative.parts[0] == "tools"
        and FORBIDDEN_NAMES.search(relative.name)))


def _public_subset_paths(ref: str = "main") -> set[Path]:
    """Return the path set of the selected public tree when it is available."""
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", ref],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {Path(item) for item in result.stdout.splitlines() if item}


def _pinned_public_ref(head: str, public_head: str) -> str:
    """Return the public parent bound into the latest RC merge.

    ``main`` may advance after an internal RC is frozen.  Scanning the mutable
    branch name would then validate a different public tree than the one named
    by the RC receipt.  The latest merge keeps that baseline as a direct parent;
    select the unique parent that belongs to public main's history.
    """
    if head == public_head:
        return public_head
    merge = _git_text("log", "--merges", "-1", "--format=%H", head)
    parents = _git_text("show", "-s", "--format=%P", merge).split()
    if public_head in parents:
        return public_head
    candidates = []
    for parent in parents:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", parent, public_head],
            cwd=ROOT, capture_output=True,
        )
        if result.returncode == 0:
            candidates.append(parent)
        elif result.returncode != 1:
            raise subprocess.CalledProcessError(result.returncode, result.args)
    if len(candidates) != 1:
        raise ValueError("cannot identify one public baseline parent")
    return candidates[0]


def _readable_patterns(scope: str, relative: Path,
                       public_subset: set[Path]) -> tuple[tuple[str, re.Pattern], ...]:
    patterns = PATTERNS
    if scope != "internal":
        return patterns
    if relative in INTERNAL_RC_CONTENT_ALLOWLIST:
        return tuple((rule, pattern) for rule, pattern in patterns
                     if rule != INTERNAL_MARKER_RULE)
    if public_subset and relative not in public_subset and relative not in INTERNAL_RC_FORCE_SCAN:
        # The internal tree is not a public claim.  Its research material is
        # intentionally left intact and is checked by its own release gates;
        # only the explicitly forced source paths above remain in this scan.
        return ()
    return patterns


def _scan_tree(*, scope: str, root: Path, all_files: list[Path],
               public_subset: set[Path]) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
    findings: list[str] = []
    attributed: dict[str, list[dict[str, str]]] = {"B": [], "C": []}
    present = {path.relative_to(root) for path in all_files}
    for required in sorted(REQUIRED_PUBLIC_FILES):
        if required not in present:
            findings.append(f"required public file missing: {required}")
    for path in all_files:
        relative = path.relative_to(root)
        if _forbidden_path(relative):
            if scope == "internal":
                attributed["B"].append({
                    "path": relative.as_posix(),
                    "reason": "正常内部 RC 组成；保留文件，公开子集检查时禁止出现",
                })
            else:
                findings.append(f"forbidden path: {relative}")
            continue
        if scope == "internal" and relative in SCAN_EXEMPTIONS:
            if relative not in PUBLIC_SCAN_EXEMPTIONS:
                attributed["C"].append({
                    "path": relative.as_posix(), "reason": SCAN_EXEMPTIONS[relative],
                })
            continue
        if scope == "public" and relative in PUBLIC_SCAN_EXEMPTIONS:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            findings.append(f"unreadable text: {relative}")
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for rule, pattern in _readable_patterns(scope, relative, public_subset):
                if pattern.search(line):
                    findings.append(f"{relative}:{line_number}: {rule}")
    try:
        from modou import capabilities
        registry = capabilities.load(root / "configs" / "capabilities.json")
        findings.extend(str(item) for item in capabilities.missing_evidence(
            registry, repo=root))
        # Scan exactly the files this check reports on. Left to its own
        # discovery, `check_documents` walks the whole tree and picks up
        # `web/node_modules`, so running the two commands the README prescribes
        # — `npm ci` then this check — reported a capability violation in a
        # vendored README.
        findings.extend(str(item) for item in capabilities.check_documents(
            root, capabilities=registry, paths=list(all_files)))
    except Exception as exc:
        findings.append(f"capability registry invalid: {type(exc).__name__}: {exc}")
    return findings, attributed


def _scan_materialized_root(root: Path) -> tuple[
        list[str], dict[str, list[dict[str, str]]], int]:
    """Zero-tolerance scan of an already-built directory.

    Without this the only public-scope path materialises the pinned public
    ref, so a freshly built v0.2 tree could never be the thing under test:
    the check would pass while describing a different tree entirely.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("public tree root must be a directory")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    if not files:
        raise ValueError("public tree root is empty")
    findings, attributed = _scan_tree(scope="public", root=root,
                                      all_files=files, public_subset=set())
    return findings, attributed, len(files)


def _scan(scope: str) -> tuple[list[str], dict[str, list[dict[str, str]]], int]:
    if scope == "public":
        try:
            head = _git_text("rev-parse", "HEAD")
            public_head = _git_text("rev-parse", "main")
            public_ref = _pinned_public_ref(head, public_head)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            return ([f"public ref unavailable: {type(exc).__name__}"],
                    {"B": [], "C": []}, 0)
        if head != public_head:
            # The internal RC contains overlapping private files, so scanning
            # current file contents would neither validate the public artifact
            # nor give the RC a meaningful zero-tolerance gate.  Read the exact
            # public Git tree into a temporary directory instead.  This keeps
            # both worktrees untouched and fails closed if any blob is missing.
            try:
                with tempfile.TemporaryDirectory(prefix="shuimu-public-check-") as raw:
                    root = Path(raw)
                    public_files = _materialize_ref(public_ref, root)
                    findings, attributed = _scan_tree(
                        scope=scope, root=root, all_files=public_files,
                        public_subset=set(),
                    )
                    return findings, attributed, len(public_files)
            except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError) as exc:
                return ([f"public tree unreadable: {type(exc).__name__}: {exc}"],
                        {"B": [], "C": []}, 0)
    public_subset = _public_subset_paths()
    current_files = files()
    findings, attributed = _scan_tree(
        scope=scope, root=ROOT, all_files=current_files,
        public_subset=public_subset,
    )
    return findings, attributed, len(current_files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the public checkout or attribute the internal RC tree")
    parser.add_argument(
        "--scope", choices=("public", "internal"), default="public",
        help="public is zero-tolerance; internal attributes approved RC-only B/C items")
    parser.add_argument(
        "--root", type=Path, default=None,
        help="scan this already-built public tree instead of the pinned public ref")
    parser.add_argument(
        "--notes-file", type=Path, default=None,
        help="also require and scan the exact frozen release Notes file")
    args = parser.parse_args(argv)
    if args.notes_file is not None:
        notes = args.notes_file.resolve()
        if args.root is not None:
            try:
                notes.relative_to(args.root.resolve())
            except ValueError:
                print("公开发布检查失败：Notes 必须位于被扫描的公开树内")
                return 1
        if not notes.is_file():
            print(f"公开发布检查失败：Notes 文件不存在：{notes}")
            return 1
        try:
            note_text = notes.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"公开发布检查失败：Notes 不可读：{exc}")
            return 1
        note_lines = note_text.splitlines()
        for line_number, line in enumerate(note_lines, 1):
            for rule, pattern in (*PATTERNS, *RELEASE_CLAIM_PATTERNS):
                # The frozen Notes intentionally contains explicit negative
                # statements ("not stable", "不支持任意仓库").  Treat a line
                # with a nearby negator as a disclaimer, not an overclaim.
                context = " ".join(note_lines[max(0, line_number - 2):line_number + 1])
                negated = re.search(r"(?i)(?:不是|不支持|未完成|not\b|no\b|without\b)", context)
                if pattern.search(line) and not (rule.startswith("release_") and negated):
                    print(f"公开发布检查失败（release Notes）：{notes}:{line_number}: {rule}")
                    return 1
    if args.root is not None:
        if args.scope != "public":
            raise SystemExit("--root is a public-scope zero-tolerance scan")
        try:
            findings, attributed, scanned_count = _scan_materialized_root(args.root)
        except (OSError, ValueError) as exc:
            print(f"公开发布检查失败（root）：{exc}")
            return 1
        label = f"root {args.root}"
    else:
        findings, attributed, scanned_count = _scan(args.scope)
        label = f"{args.scope} scope"
    if findings:
        print(f"公开发布检查失败（{label}）：")
        print("\n".join(f"- {finding}" for finding in findings))
        return 1
    if args.scope == "internal":
        print("内部 RC 检查通过：A 类未归因项 0；"
              f"B 类已归因 {len(attributed['B'])} 条；"
              f"C 类已豁免 {len(attributed['C'])} 个夹具文件")
    else:
        print(f"公开发布检查通过：{scanned_count} 个文件（{label}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
