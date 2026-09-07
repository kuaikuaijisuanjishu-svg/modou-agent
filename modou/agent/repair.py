"""Safe delivery of a *previously verified* repair into a new local branch.

This module deliberately does not generate patches and never touches the
reviewer's checkout.  It is the deterministic delivery boundary between a
future patch-proposal agent and Git: callers must supply a verifier, and a
commit is impossible until that verifier succeeds.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .evidence import build_delivery_record
from modou.safe_git import run_git


class RepairError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RepairVerification:
    passed: bool
    test_scope: tuple[str, ...]
    summary: str = ""


@dataclass(frozen=True)
class RepairDelivery:
    branch: str
    commit: str
    patch_sha256: str
    test_scope: tuple[str, ...]
    evidence_manifest_sha256: str = ""
    delivery_record: dict | None = None


_PATCH_PATH = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$")
_FORBIDDEN_PREFIXES = (".git/", ".github/", "node_modules/", ".shuimu/")
_FORBIDDEN_NAMES = {
    ".gitattributes", ".gitmodules", ".lfsconfig", ".npmrc", ".pnpmfile.cjs",
    ".yarnrc", ".yarnrc.yml", "package.json", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "requirements.txt",
    "requirements-dev.txt", "pyproject.toml",
}


def deliver_verified_patch(*, repo: Path, review_id: str, plan_sha256: str,
                           patch: str,
                           verifier: Callable[[Path], RepairVerification],
                           allowed_paths: tuple[str, ...] = (),
                           max_files: int = 5,
                           max_changed_lines: int = 400,
                           scratch_root: Path | None = None,
                           bundle_sha256: str | None = None,
                           evidence_manifest_sha256: str | None = None,
                           evidence_manifest_builder: Callable[[RepairVerification], str] | None = None
                           ) -> RepairDelivery:
    """Commit a bounded repair to ``shuimu/review-<id>`` after verification.

    The original checkout may contain the uncommitted patch being reviewed. It
    is copied to the private worktree before the repair candidate is applied,
    so the resulting branch represents "current review target + repair" while
    the user's checkout remains byte-for-byte unchanged.
    """
    repo = Path(repo).resolve(strict=True)
    _require_git_root(repo)
    _validate_hash(plan_sha256, "plan_sha256")
    if (bundle_sha256 is None and evidence_manifest_sha256 is None
            and evidence_manifest_builder is None):
        raise RepairError("REPAIR_EVIDENCE_HASH_REQUIRED",
                          "a pre-commit evidence hash or builder is required")
    if bundle_sha256 is not None:
        _validate_hash(bundle_sha256, "bundle_sha256")
    if evidence_manifest_sha256 is not None:
        _validate_hash(evidence_manifest_sha256, "evidence_manifest_sha256")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", review_id):
        raise RepairError("REPAIR_ID_INVALID", "review_id is not branch-safe")
    candidate_paths = validate_candidate_patch(
        patch, allowed_paths=allowed_paths, max_files=max_files,
        max_changed_lines=max_changed_lines)
    before_status = _git(repo, "status", "--porcelain=v1")
    if any(line.startswith("??") for line in before_status.splitlines()):
        raise RepairError("REPAIR_SOURCE_UNTRACKED",
                          "untracked source files require an explicit later workflow")
    base = _git(repo, "rev-parse", "HEAD")
    source_patch = _git_text(repo, "diff", "--binary", "HEAD")
    source_paths = set(filter(None, _git(repo, "diff", "--name-only", "HEAD").splitlines()))
    branch = _unique_branch(repo, f"shuimu/review-{review_id[:12].lower()}")

    if scratch_root is not None:
        scratch_root = Path(scratch_root)
        scratch_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="shuimu-repair-",
                                     dir=scratch_root))
    worktree = temp_root / "worktree"
    try:
        _run(repo, ["git", "worktree", "add", "--detach", str(worktree), base])
        _run(worktree, ["git", "switch", "-c", branch])
        if source_patch:
            _apply(worktree, source_patch, "review source patch")
        _apply(worktree, patch, "repair patch")
        verification = verifier(worktree)
        if not isinstance(verification, RepairVerification) or not verification.passed:
            raise RepairError("REPAIR_VERIFICATION_FAILED",
                              getattr(verification, "summary", "verifier did not pass"))
        if evidence_manifest_sha256 is None and evidence_manifest_builder is not None:
            try:
                evidence_manifest_sha256 = str(evidence_manifest_builder(verification))
            except RepairError:
                raise
            except Exception as exc:
                raise RepairError("REPAIR_MANIFEST_INVALID", str(exc)[:300]) from exc
            _validate_hash(evidence_manifest_sha256, "evidence_manifest_sha256")
        _assert_only_expected_changes(worktree, source_paths | candidate_paths)
        _assert_original_unchanged(repo, before_status)
        changed = sorted(source_paths | candidate_paths)
        _run(worktree, ["git", "add", "-A", "--", *changed])
        message = (
            "shuimu: verified repair\n\n"
            f"Shuimu-Review-ID: {review_id}\n"
            f"Review-Plan-SHA256: {plan_sha256}\n"
            f"Verified-Test-Scope: {', '.join(verification.test_scope)}"
        )
        if evidence_manifest_sha256:
            message += f"\nEvidence-Manifest-SHA256: {evidence_manifest_sha256}"
        if bundle_sha256:
            # Compatibility for the pre-v2 caller. New callers should provide
            # the commit-independent EvidenceManifest hash instead.
            message += f"\nEvidence-Bundle-SHA256: {bundle_sha256}"
        _run(worktree, ["git", "commit", "-m", message])
        commit = _git(worktree, "rev-parse", "HEAD")
        _assert_original_unchanged(repo, before_status)
        manifest_hash = evidence_manifest_sha256 or bundle_sha256 or ""
        record = build_delivery_record(
            review_id=review_id, branch=branch, commit=commit,
            evidence_manifest_sha256=manifest_hash,
        ).as_dict()
        return RepairDelivery(branch=branch, commit=commit,
                              patch_sha256=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
                              test_scope=verification.test_scope,
                              evidence_manifest_sha256=manifest_hash,
                              delivery_record=record)
    finally:
        if worktree.exists():
            run_git(["worktree", "remove", "--force", str(worktree)],
                    cwd=repo)
        shutil.rmtree(temp_root, ignore_errors=True)


def _validate_patch(patch: str) -> set[str]:
    if not isinstance(patch, str) or not patch.strip() or "\x00" in patch:
        raise RepairError("REPAIR_PATCH_INVALID", "patch must be non-empty text")
    paths: set[str] = set()
    changed_lines = 0
    for line in patch.splitlines():
        match = _PATCH_PATH.match(line)
        if match:
            path = match.group(1)
            if path == "/dev/null":
                continue
            _validate_path(path)
            paths.add(path)
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            changed_lines += 1
    if not paths or len(paths) > 5:
        raise RepairError("REPAIR_PATCH_SCOPE", "repair may touch between 1 and 5 text files")
    if changed_lines > 400:
        raise RepairError("REPAIR_PATCH_SCOPE", "repair may change at most 400 lines")
    return paths


def validate_candidate_patch(patch: str, *, allowed_paths: tuple[str, ...] = (),
                             max_files: int = 5, max_changed_lines: int = 400) -> set[str]:
    """Public policy boundary shared by candidate generation and delivery."""
    paths = _validate_patch(patch)
    changed_lines = sum(1 for line in patch.splitlines()
                        if line.startswith(("+", "-")) and
                        not line.startswith(("+++", "---")))
    if len(paths) > max_files or changed_lines > max_changed_lines:
        raise RepairError("REPAIR_PATCH_SCOPE", "candidate exceeds the approved modification limit")
    approved = {path for path in paths if any(
        path == rule or Path(path).match(rule) for rule in allowed_paths)}
    if allowed_paths and approved != paths:
        raise RepairError("REPAIR_PATH_NOT_APPROVED",
                          ", ".join(sorted(paths - approved)))
    return paths


def _validate_path(path: str) -> None:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or path.startswith(_FORBIDDEN_PREFIXES):
        raise RepairError("REPAIR_PATH_FORBIDDEN", path)
    if candidate.name in _FORBIDDEN_NAMES or candidate.suffix.lower() in {".png", ".jpg", ".gif", ".zip", ".pdf"}:
        raise RepairError("REPAIR_PATH_FORBIDDEN", path)


def _require_git_root(repo: Path) -> None:
    top = _git(repo, "rev-parse", "--show-toplevel")
    if Path(top).resolve() != repo:
        raise RepairError("REPAIR_REPO_INVALID", "repo must be the Git root")


def _validate_hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RepairError("REPAIR_HASH_INVALID", field)


def _branch_exists(repo: Path, branch: str) -> bool:
    return run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                   cwd=repo).returncode == 0


def _unique_branch(repo: Path, base: str) -> str:
    if not _branch_exists(repo, base):
        return base
    for number in range(2, 100):
        candidate = f"{base}-{number}"
        if not _branch_exists(repo, candidate):
            return candidate
    raise RepairError("REPAIR_BRANCH_NAMESPACE_EXHAUSTED", base)


def _git(repo: Path, *args: str) -> str:
    return _git_text(repo, *args).strip()


def _git_text(repo: Path, *args: str) -> str:
    return _run(repo, ["git", *args]).stdout


def _run(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = (run_git(argv[1:], cwd=cwd)
                  if argv and argv[0] == "git"
                  else subprocess.run(argv, cwd=cwd, capture_output=True,
                                      text=True, check=False))
    except OSError as exc:
        raise RepairError("REPAIR_GIT_FAILED", str(exc)) from exc
    if result.returncode:
        raise RepairError("REPAIR_GIT_FAILED", result.stderr.strip() or " ".join(argv))
    return result


def _apply(worktree: Path, patch: str, label: str) -> None:
    result = run_git(["apply", "--check", "-"], cwd=worktree, input=patch)
    if result.returncode:
        raise RepairError("REPAIR_PATCH_REJECTED", f"{label}: {result.stderr.strip()}")
    result = run_git(["apply", "-"], cwd=worktree, input=patch)
    if result.returncode:
        raise RepairError("REPAIR_PATCH_REJECTED", f"{label}: {result.stderr.strip()}")


def _assert_only_expected_changes(worktree: Path, expected: set[str]) -> None:
    changed = {line for line in _git(worktree, "diff", "--name-only", "HEAD").splitlines() if line}
    untracked = [line for line in _git(worktree, "status", "--porcelain=v1").splitlines()
                 if line.startswith("??")]
    if untracked or not changed.issubset(expected):
        raise RepairError("REPAIR_WORKTREE_DIRTY", "verification wrote files outside the approved patch")


def _assert_original_unchanged(repo: Path, before_status: str) -> None:
    if _git(repo, "status", "--porcelain=v1") != before_status:
        raise RepairError("REPAIR_SOURCE_CHANGED", "original checkout changed during repair delivery")
