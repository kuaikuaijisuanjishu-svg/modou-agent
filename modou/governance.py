"""Local review export, retention and deletion with traversal-safe boundaries."""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


class GovernanceError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def export_manifest(review_dir: Path) -> dict:
    root = _review_root(review_dir)
    artifacts = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise GovernanceError("EXPORT_SYMLINK_FORBIDDEN", path.name)
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        payload = path.read_bytes()
        artifacts.append({"path": rel, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()})
    return {"schema_version": "review-export-manifest-v1",
            "review_id": root.name, "artifacts": artifacts,
            "artifact_count": len(artifacts)}


def delete_review(review_dir: Path) -> dict:
    root = _review_root(review_dir)
    manifest = export_manifest(root)
    shutil.rmtree(root)
    return {"schema_version": "review-cleanup-result-v1",
            "review_id": manifest["review_id"], "deleted": True,
            "artifact_count": manifest["artifact_count"],
            "cleaned_at": datetime.now(timezone.utc).isoformat()}


def cleanup_expired(root: Path, *, now_epoch: float, log_days: int = 30,
                    failed_candidate_days: int = 7) -> dict:
    base = Path(root).resolve(strict=True)
    removed = []
    for review in sorted(base.iterdir()):
        if not review.is_dir() or review.name.startswith("_"):
            continue
        repair = review / "repair"
        candidate = repair / "candidate.json"
        if candidate.is_file() and now_epoch - candidate.stat().st_mtime >= failed_candidate_days * 86400:
            candidate.unlink()
            removed.append(f"{review.name}/repair/candidate.json")
        for log in review.rglob("*.log"):
            if not log.is_symlink() and now_epoch - log.stat().st_mtime >= log_days * 86400:
                log.unlink()
                removed.append(f"{review.name}/{log.relative_to(review).as_posix()}")
    return {"schema_version": "retention-cleanup-v1", "removed": removed,
            "removed_count": len(removed)}


def _review_root(path: Path) -> Path:
    root = Path(path).resolve(strict=True)
    if not root.is_dir() or not (root / "review_state.json").is_file():
        raise GovernanceError("REVIEW_DIRECTORY_INVALID", root.name)
    return root
