"""Fail-closed preflight for repositories whose code may be executed.

The scan intentionally uses metadata-only Git commands and direct bounded file
reads.  It never checks out content, expands LFS objects, initializes
submodules, executes package scripts, or follows symlinks.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .safe_git import run_git


POLICY_VERSION = "repository-security-v1"
MAX_GIT_ENTRIES = 50_000
MAX_POLICY_FILE_BYTES = 1 << 20
MAX_PACKAGE_FILES = 200

_FILTER_ATTRIBUTE = re.compile(
    r"(?:^|\s)(?:filter|diff)\s*=\s*[^\s#]+", re.IGNORECASE)
_LIFECYCLE_SCRIPTS = frozenset({
    "preinstall", "install", "postinstall", "prepare", "prepublish",
    "prepublishOnly", "publish", "postpublish",
})
_DANGEROUS_CONFIG_PREFIXES = (
    "core.hookspath", "core.fsmonitor", "core.sparsecheckout",
    "core.sparsecheckoutcone", "core.attributesfile", "core.worktree",
    "extensions.partialclone", "filter.", "diff.", "include.", "includeif.",
)


class RepositorySecurityError(RuntimeError):
    def __init__(self, code: str, detail: str, report: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.report = report or {}


@dataclass(frozen=True)
class SecurityFinding:
    code: str
    severity: str
    path: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {key: value for key, value in {
            "code": self.code, "severity": self.severity,
            "path": self.path, "detail": self.detail,
        }.items() if value}


@dataclass(frozen=True)
class RepositorySecurityReport:
    findings: tuple[SecurityFinding, ...]
    tracked_entries: int
    untracked_entries: int

    @property
    def blockers(self) -> tuple[SecurityFinding, ...]:
        return tuple(item for item in self.findings if item.severity == "block")

    def as_dict(self) -> dict:
        return {
            "schema_version": POLICY_VERSION,
            "status": "blocked" if self.blockers else "accepted",
            "tracked_entries": self.tracked_entries,
            "untracked_entries": self.untracked_entries,
            "findings": [item.as_dict() for item in self.findings],
            "git_policy": {
                "hooks": "disabled",
                "global_config": "ignored",
                "credential_helpers": "disabled",
                "signing": "disabled",
                "submodule_recursion": "disabled",
                "interactive_prompts": "disabled",
            },
        }


def inspect_repository(root: Path) -> RepositorySecurityReport:
    """Inspect attack-bearing repository features without executing content."""
    root = Path(root).resolve(strict=True)
    top = _git_text(root, ["rev-parse", "--show-toplevel"]).strip()
    if not top or Path(top).resolve() != root:
        raise RepositorySecurityError("REPOSITORY_ROOT_INVALID",
                                      "repository must be registered at its Git root")

    findings: list[SecurityFinding] = []
    tracked = _tracked_entries(root)
    untracked = _untracked_entries(root)
    if len(tracked) + len(untracked) > MAX_GIT_ENTRIES:
        raise RepositorySecurityError(
            "REPOSITORY_SCAN_TOO_BROAD",
            f"repository exposes more than {MAX_GIT_ENTRIES} tracked/untracked entries")

    for mode, path in tracked:
        if mode == "160000":
            findings.append(SecurityFinding(
                "GIT_SUBMODULE_UNSUPPORTED", "block", path,
                "gitlink entries are not initialized or executed automatically"))
        elif mode == "120000":
            findings.append(SecurityFinding(
                "GIT_SYMLINK_UNSUPPORTED", "block", path,
                "tracked symlinks are refused until containment is independently verified"))

    for path in untracked:
        candidate = root / path
        try:
            if candidate.is_symlink():
                findings.append(SecurityFinding(
                    "GIT_SYMLINK_UNSUPPORTED", "block", path,
                    "untracked symlinks are not followed"))
        except OSError:
            findings.append(SecurityFinding(
                "REPOSITORY_PATH_UNREADABLE", "block", path,
                "path metadata could not be inspected"))

    all_paths = tuple(path for _, path in tracked) + tuple(untracked)
    path_set = set(all_paths)
    if ".gitmodules" in path_set:
        findings.append(SecurityFinding(
            "GIT_SUBMODULE_CONFIG_UNSUPPORTED", "block", ".gitmodules",
            "submodule configuration is not allowed in automated review"))
    if ".lfsconfig" in path_set:
        findings.append(SecurityFinding(
            "GIT_LFS_CONFIG_UNSUPPORTED", "block", ".lfsconfig",
            "repository-controlled LFS endpoints are not allowed"))

    for path in sorted(p for p in path_set if Path(p).name == ".gitattributes"):
        texts = [_bounded_text(root, path, findings),
                 _head_text(root, path, findings)]
        if any(text is not None and _FILTER_ATTRIBUTE.search(text)
               for text in texts):
            findings.append(SecurityFinding(
                "GIT_ATTRIBUTE_FILTER_UNSUPPORTED", "block", path,
                "filter/diff attributes may invoke repository-configured programs"))

    package_paths = sorted(p for p in path_set if Path(p).name == "package.json")
    if len(package_paths) > MAX_PACKAGE_FILES:
        findings.append(SecurityFinding(
            "PACKAGE_MANIFEST_SCAN_TOO_BROAD", "block", "",
            f"more than {MAX_PACKAGE_FILES} package.json files"))
    for path in package_paths[:MAX_PACKAGE_FILES]:
        text = _bounded_text(root, path, findings)
        if text is None:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            findings.append(SecurityFinding(
                "PACKAGE_JSON_INVALID", "observe", path,
                "invalid package.json was not evaluated"))
            continue
        scripts = raw.get("scripts") if isinstance(raw, dict) else None
        if isinstance(scripts, dict):
            names = sorted(name for name in scripts if name in _LIFECYCLE_SCRIPTS)
            if names:
                findings.append(SecurityFinding(
                    "PACKAGE_LIFECYCLE_SCRIPTS_PRESENT", "observe", path,
                    "detected but never executed: " + ", ".join(names)))

    findings.extend(_config_findings(root))
    findings.extend(_git_directory_findings(root))
    findings = sorted(findings, key=lambda item: (item.severity, item.code, item.path))
    return RepositorySecurityReport(tuple(findings), len(tracked), len(untracked))


def require_repository_safe(root: Path) -> RepositorySecurityReport:
    report = inspect_repository(root)
    if report.blockers:
        codes = ", ".join(sorted({item.code for item in report.blockers}))
        raise RepositorySecurityError(
            "REPOSITORY_SECURITY_BLOCKED",
            f"repository security preflight blocked execution: {codes}",
            report.as_dict())
    return report


def _git_text(root: Path, args: list[str], *, ok: tuple[int, ...] = (0,)) -> str:
    try:
        result = run_git(args, cwd=root, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RepositorySecurityError("REPOSITORY_GIT_INSPECTION_FAILED", str(exc)) from exc
    if result.returncode not in ok:
        detail = (result.stderr or "Git inspection failed").strip()[:300]
        raise RepositorySecurityError("REPOSITORY_GIT_INSPECTION_FAILED", detail)
    return result.stdout


def _tracked_entries(root: Path) -> tuple[tuple[str, str], ...]:
    index = _parse_entries(_git_text(root, ["ls-files", "--stage", "-z"]))
    head = _parse_entries(_git_text(
        root, ["ls-tree", "-r", "--full-tree", "-z", "HEAD"]))
    return tuple(sorted(set(index) | set(head), key=lambda row: (row[1], row[0])))


def _parse_entries(raw: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for item in raw.split("\0"):
        if not item:
            continue
        meta, separator, path = item.partition("\t")
        fields = meta.split()
        if not separator or len(fields) != 3:
            raise RepositorySecurityError("REPOSITORY_INDEX_INVALID",
                                          "could not parse git index entry")
        rows.append((fields[0], path))
    return tuple(rows)


def _untracked_entries(root: Path) -> tuple[str, ...]:
    raw = _git_text(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    return tuple(item for item in raw.split("\0") if item)


def _bounded_text(root: Path, relative: str,
                  findings: list[SecurityFinding]) -> str | None:
    path = root / relative
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_POLICY_FILE_BYTES:
            findings.append(SecurityFinding(
                "REPOSITORY_POLICY_FILE_TOO_LARGE", "block", relative,
                f"policy-bearing file exceeds {MAX_POLICY_FILE_BYTES} bytes"))
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        findings.append(SecurityFinding(
            "REPOSITORY_PATH_UNREADABLE", "block", relative,
            "file could not be inspected"))
        return None


def _head_text(root: Path, relative: str,
               findings: list[SecurityFinding]) -> str | None:
    size = _git_text(root, ["cat-file", "-s", f"HEAD:{relative}"], ok=(0, 128)).strip()
    if not size:
        return None
    try:
        parsed_size = int(size)
    except ValueError:
        findings.append(SecurityFinding(
            "REPOSITORY_POLICY_FILE_UNREADABLE", "block", relative,
            "HEAD policy-bearing object size is invalid"))
        return None
    if parsed_size > MAX_POLICY_FILE_BYTES:
        findings.append(SecurityFinding(
            "REPOSITORY_POLICY_FILE_TOO_LARGE", "block", relative,
            f"HEAD object exceeds {MAX_POLICY_FILE_BYTES} bytes"))
        return None
    return _git_text(root, ["show", f"HEAD:{relative}"])


def _config_findings(root: Path) -> list[SecurityFinding]:
    raw = _git_text(root, ["config", "--local", "--null", "--list"], ok=(0, 1))
    findings: list[SecurityFinding] = []
    for item in raw.split("\0"):
        if not item:
            continue
        key, _, value = item.partition("\n")
        lowered = key.lower()
        dangerous = (lowered.startswith(_DANGEROUS_CONFIG_PREFIXES)
                     or (lowered.startswith("remote.") and lowered.endswith(".promisor")))
        if not dangerous:
            continue
        if lowered in {"core.fsmonitor", "core.sparsecheckout",
                       "core.sparsecheckoutcone"} and value.lower() in {"", "false", "0", "no"}:
            continue
        findings.append(SecurityFinding(
            "GIT_LOCAL_CONFIG_UNSUPPORTED", "block", ".git/config",
            f"unsupported local Git setting: {key}"))
    return findings


def _git_directory_findings(root: Path) -> list[SecurityFinding]:
    findings: list[SecurityFinding] = []
    if (root / ".git").is_file():
        findings.append(SecurityFinding(
            "GIT_LINKED_WORKTREE_UNSUPPORTED", "block", ".git",
            "an externally linked Git worktree is not accepted as a source root"))
    common_raw = _git_text(root, ["rev-parse", "--git-common-dir"]).strip()
    common = Path(common_raw)
    if not common.is_absolute():
        common = (root / common).resolve()
    hooks = common / "hooks"
    try:
        active_hooks = sorted(
            path.name for path in hooks.iterdir()
            if path.is_file() and not path.name.endswith(".sample")) if hooks.is_dir() else []
    except OSError:
        active_hooks = ["<unreadable>"]
    if active_hooks:
        findings.append(SecurityFinding(
            "GIT_HOOKS_PRESENT", "observe", ".git/hooks",
            "disabled by control-plane Git policy: " + ", ".join(active_hooks[:20])))
    if (common / "objects" / "info" / "alternates").exists():
        findings.append(SecurityFinding(
            "GIT_OBJECT_ALTERNATES_UNSUPPORTED", "block", ".git/objects/info/alternates",
            "external object stores are not accepted"))
    if (common / "info" / "sparse-checkout").exists():
        findings.append(SecurityFinding(
            "GIT_SPARSE_CHECKOUT_UNSUPPORTED", "block", ".git/info/sparse-checkout",
            "sparse checkout can hide policy-bearing paths"))
    return findings
