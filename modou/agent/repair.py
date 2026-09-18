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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .evidence import build_delivery_record
from . import syntax_guard
from .syntax_guard import SyntaxGuardError
from modou.safe_git import run_git


class RepairError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _syntax_gate(worktree: Path, rel_path: str) -> None:
    """改动文件过语法闸；拒绝一律翻成 RepairError 契约码。"""
    try:
        syntax_guard.check_file(worktree / rel_path, worktree=worktree)
    except SyntaxGuardError as exc:
        raise RepairError(exc.code, exc.detail) from exc


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


@dataclass(frozen=True)
class IsolatedRunResult:
    """One test run in a throwaway worktree. Nothing was committed."""

    returncode: int
    output: str


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
        # 语法闸：补丁边界与验证器都不证明改动文件语法成立。只查
        # 候选路径（用户树带来的 source patch 不归本次交付负责）。
        for rel in sorted(candidate_paths):
            if (worktree / rel).is_file():
                _syntax_gate(worktree, rel)
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


def run_isolated_new_test(*, repo: Path, source_patch: str,
                          new_file_path: str, new_file_code: str,
                          runner: Callable[[Path], subprocess.CompletedProcess],
                          scratch_root: Path | None = None) -> IsolatedRunResult:
    """Apply a candidate test in a throwaway worktree and run it once.

    This is the pre-delivery sandbox for model-proposed tests: the same
    worktree discipline as deliver_verified_patch (detached worktree at
    HEAD, source patch applied on top), but the run result is the product —
    Git never creates a commit, and the reviewer's checkout is asserted
    byte-for-byte unchanged at teardown. The runner receives the worktree
    path and owns executor choice, environment hygiene and timeouts.
    """
    repo = Path(repo).resolve(strict=True)
    _require_git_root(repo)
    if (not isinstance(new_file_path, str) or not new_file_path
            or new_file_path.startswith(("/", ".."))
            or Path(new_file_path).is_absolute()
            or ".." in Path(new_file_path).parts):
        raise RepairError("ISOLATED_RUN_PATH_INVALID",
                          "new file path escapes the worktree")
    _validate_path(new_file_path)
    if (not isinstance(new_file_code, str) or not new_file_code.strip()
            or "\x00" in new_file_code
            or len(new_file_code.encode("utf-8")) > 16 * 1024):
        raise RepairError("ISOLATED_RUN_CODE_INVALID",
                          "new file code is empty or oversized")
    with isolated_worktree(repo=repo, source_patch=source_patch,
                           scratch_root=scratch_root) as worktree:
        target = worktree / new_file_path
        if target.exists():
            raise RepairError("ISOLATED_RUN_PATH_EXISTS",
                              f"candidate path collides with a repository file: {new_file_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_file_code, encoding="utf-8")
        _syntax_gate(worktree, new_file_path)
        result = runner(worktree)
        output = (str(result.stdout or "") + str(result.stderr or ""))
        return IsolatedRunResult(returncode=int(result.returncode), output=output)


@contextmanager
def isolated_worktree(*, repo: Path, source_patch: str,
                      scratch_root: Path | None = None,
                      prefix: str = "shuimu-test-run-",
                      extra_patches: tuple[str, ...] = ()) -> Iterator[Path]:
    """一次性 detached 工作树：review source patch 之上，退出即拆。

    run_isolated_new_test、test_visa 与候选补丁验证共用的唯一一份实现
    ——「建了就一定拆、用户 checkout 字节不动」这条纪律不允许有第二份
    代码。extra_patches 在 source patch 之后依序应用（调用方自行先过
    validate_candidate_patch 的路径与规模闸）。进入时拒绝未跟踪源文件
    （沿用原口径），退出时无论成败都先断言用户 checkout 未被触碰，再
    强制移除工作树。
    """
    repo = Path(repo).resolve(strict=True)
    _require_git_root(repo)
    before_status = _git(repo, "status", "--porcelain=v1")
    if any(line.startswith("??") for line in before_status.splitlines()):
        raise RepairError("REPAIR_SOURCE_UNTRACKED",
                          "untracked source files require an explicit later workflow")
    base = _git(repo, "rev-parse", "HEAD")

    if scratch_root is not None:
        scratch_root = Path(scratch_root)
        scratch_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=prefix,
                                      dir=scratch_root))
    worktree = temp_root / "worktree"
    try:
        _run(repo, ["git", "worktree", "add", "--detach", str(worktree), base])
        if source_patch:
            _apply(worktree, source_patch, "review source patch")
        for extra in extra_patches:
            _apply(worktree, extra, "authorized extra patch")
        yield worktree
    finally:
        _assert_original_unchanged(repo, before_status)
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


_NEW_FILE_SECTION = re.compile(r"^diff --git a/(.+) b/(.+)$", re.MULTILINE)


def validate_test_only_patch(patch: str) -> dict:
    """策略 4 的 test-only 白名单边界：只收"新增一个测试文件"的 diff。

    共同地基与交付边界完全一致（_validate_patch 的封禁前缀、封禁
    名单、绝对路径与越界拒绝、1-5 文件与 400 行上限），在这之上
    再收紧：
    - 恰好一个文件，且必须是 new file 模式——签证管线要求候选路径
      在基线里不存在，修改既有文件没有"新增保护"的语义；
    - 只允许 .py 且文件名以 test_ 开头（签证管线跑 pytest）；
    - 正文只允许加行：删除行、上下文行、第二个 hunk、缺结尾换行
      一律拒绝；
    - 提取出的源码必须过语法闸，语法坏了当场 fail-closed，绝不留
      到隔离工作树里才炸。
    返回 {"path", "code"}；code 可直接写盘交给签证管线。
    """
    paths = _validate_patch(patch)
    if len(paths) != 1:
        raise RepairError("REPAIR_TEST_PATCH_SCOPE",
                          "a test proposal adds exactly one file")
    sections = _NEW_FILE_SECTION.findall(patch)
    if len(sections) != 1 or sections[0][0] != sections[0][1]:
        raise RepairError("REPAIR_TEST_PATCH_INVALID",
                          "expected exactly one new-file diff section")
    rel_path = sections[0][1]
    if paths != {rel_path}:
        raise RepairError("REPAIR_TEST_PATCH_INVALID", rel_path)
    candidate = Path(rel_path)
    if (candidate.suffix.lower() != ".py"
            or not candidate.name.startswith("test_")
            or "new file mode" not in patch
            or "--- /dev/null" not in patch):
        raise RepairError("REPAIR_TEST_PATCH_INVALID",
                          "only new test_*.py files are accepted")
    code_lines: list[str] = []
    hunk_seen = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            if hunk_seen:
                raise RepairError("REPAIR_TEST_PATCH_INVALID",
                                  "a new-file diff has exactly one hunk")
            hunk_seen = True
            continue
        if line.startswith(("diff --git", "index ", "new file mode",
                            "--- ", "+++ ")):
            continue
        if not hunk_seen:
            continue
        if line.startswith("+"):
            code_lines.append(line[1:])
        elif line.startswith("-"):
            raise RepairError("REPAIR_TEST_PATCH_INVALID",
                              "a new-file diff has no deletions")
        elif line.startswith("\\"):
            raise RepairError("REPAIR_TEST_PATCH_INVALID",
                              "the test file must end with a newline")
        elif line.strip():
            raise RepairError("REPAIR_TEST_PATCH_INVALID",
                              "a new-file diff has no context lines")
    code = "\n".join(code_lines)
    if not code.strip():
        raise RepairError("REPAIR_TEST_PATCH_INVALID", "empty test file")
    try:
        syntax_guard.check_text(rel_path, code)
    except SyntaxGuardError as exc:
        raise RepairError(exc.code, exc.detail) from exc
    return {"path": rel_path, "code": code}


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
