"""Multi-artifact integration (T08 / direction 9).

Two independently developed artifacts (unified-diff patches on a shared
baseline) are merged in an isolated temporary copy.  Text conflicts return
an explicit undecided verdict — never a guess.  The caller's baseline tree
is never modified.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

SCHEMA_VERSION = "merge-integration-v1"


class MergeIntegrationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def merge_artifacts(baseline: Path, patch_a: str, patch_b: str, *,
                    work_root: Path | None = None) -> dict:
    """Apply both patches to an isolated clone; conflicts stay undecided."""
    baseline = Path(baseline)
    if not baseline.is_dir():
        raise MergeIntegrationError("BASELINE_MISSING", str(baseline))
    if not _git(["rev-parse", "--is-inside-work-tree"], cwd=baseline,
                check=False).stdout.strip() == "true":
        raise MergeIntegrationError("BASELINE_NOT_GIT", str(baseline))
    work = Path(work_root or tempfile.mkdtemp(prefix="merge-int-"))
    work.mkdir(parents=True, exist_ok=True)
    sandbox = work / "merged"
    shutil.copytree(baseline, sandbox,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv*"))
    _git(["init", "-q"], cwd=sandbox)
    _git(["add", "-A"], cwd=sandbox)
    _git(["-c", "user.name=integration", "-c", "user.email=integration@example.invalid",
          "commit", "-qm", "baseline"], cwd=sandbox)
    applied, conflicts = [], []
    for name, patch in (("artifact-a", patch_a), ("artifact-b", patch_b)):
        patch_path = work / f"{name}.patch"
        patch_path.write_text(patch)
        result = _git(["apply", "--check", str(patch_path)], cwd=sandbox, check=False)
        if result.returncode != 0:
            conflicts.append({"artifact": name,
                              "files": sorted({line.split(":")[0].strip()
                                               for line in result.stderr.splitlines()
                                               if ":" in line}) or ["unknown"]})
            continue
        _git(["apply", str(patch_path)], cwd=sandbox)
        applied.append(name)
    if conflicts:
        return {"schema_version": SCHEMA_VERSION, "status": "conflict",
                "applied": applied, "conflicts": conflicts,
                "verdict": "not_executed_undecided",
                "note": "合并冲突：验收未执行、不可判定；不猜测合并结果",
                "merged_dir": "", "work_dir": str(work)}
    return {"schema_version": SCHEMA_VERSION, "status": "merged",
            "applied": applied, "conflicts": [], "verdict": "merged",
            "merged_dir": str(sandbox), "work_dir": str(work),
            "note": ("合并结果只存在于临时隔离目录；调用方基线未被修改。")}


def baseline_unchanged(baseline: Path, before: dict[str, str]) -> bool:
    """Guard used by tests/demos: byte-identity of the caller's tree."""
    current = {p.relative_to(baseline).as_posix(): p.read_bytes()
               for p in sorted(Path(baseline).rglob("*"))
               if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}
    return current == before


def snapshot(baseline: Path) -> dict[str, bytes]:
    baseline = Path(baseline)
    return {p.relative_to(baseline).as_posix(): p.read_bytes()
            for p in sorted(baseline.rglob("*"))
            if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts}
