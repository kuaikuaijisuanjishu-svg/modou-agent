"""Expiring, cross-process repository leases.

Only opaque repository fingerprints are persisted.  A small advisory lock makes
inspection/replacement atomic across server processes; an expired lease is
reclaimed only when the recorded process identity is demonstrably gone.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


class LeaseError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _process_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
            text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return " ".join(result.stdout.split()) if result.returncode == 0 else ""


def _alive_with_identity(pid: int, identity: str) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    current = _process_identity(pid)
    if not identity or not current:
        return None
    return current == identity


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class RepositoryLease:
    repo_fingerprint: str
    review_id: str
    operation: str
    plan_sha256: str
    lease_id: str
    owner_id: str
    owner_pid: int
    owner_process_identity: str
    issued_at: float
    heartbeat_at: float
    expires_at: float
    schema_version: str = "repository-lease-v1"

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, raw: dict) -> "RepositoryLease":
        try:
            lease = cls(**raw)
        except (TypeError, ValueError) as exc:
            raise LeaseError("LEASE_RECORD_INVALID", str(exc)) from exc
        if lease.schema_version != "repository-lease-v1":
            raise LeaseError("LEASE_RECORD_INVALID", "unsupported schema")
        return lease


class RepositoryLeaseManager:
    def __init__(self, root: Path, *, owner_id: str | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id or uuid.uuid4().hex
        self.owner_pid = os.getpid()
        self.owner_process_identity = _process_identity(self.owner_pid)
        self._thread_lock = threading.RLock()

    def _paths(self, fingerprint: str) -> tuple[Path, Path]:
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise LeaseError("LEASE_FINGERPRINT_INVALID", fingerprint)
        return (self.root / f"{fingerprint}.json",
                self.root / f"{fingerprint}.lock")

    def _locked(self, lock_path: Path):
        """Return a file handle holding an exclusive advisory lock."""
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+")
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError) as exc:
            fh.close()
            raise LeaseError("LEASE_LOCK_UNAVAILABLE", str(exc)) from exc
        return fh

    @staticmethod
    def _unlock(fh) -> None:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    @staticmethod
    def _read(path: Path) -> RepositoryLease | None:
        if not path.exists():
            return None
        try:
            return RepositoryLease.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise LeaseError("LEASE_RECORD_INVALID", str(exc)) from exc

    def acquire(self, *, repo_fingerprint: str, review_id: str, operation: str,
                plan_sha256: str, ttl_seconds: float = 90) -> RepositoryLease:
        path, lock_path = self._paths(repo_fingerprint)
        ttl = max(5.0, min(float(ttl_seconds), 7200.0))
        with self._thread_lock:
            fh = self._locked(lock_path)
            try:
                now = time.time()
                existing = self._read(path)
                if existing is not None:
                    if (existing.owner_id == self.owner_id
                            and existing.review_id == review_id
                            and existing.operation == operation):
                        refreshed = RepositoryLease(
                            **{**existing.as_dict(), "heartbeat_at": now,
                               "expires_at": now + ttl})
                        _atomic_json(path, refreshed.as_dict())
                        return refreshed
                    if existing.expires_at > now:
                        raise LeaseError("REPOSITORY_LEASE_BUSY", existing.review_id)
                    alive = _alive_with_identity(existing.owner_pid,
                                                 existing.owner_process_identity)
                    if alive is not False:
                        raise LeaseError("LEASE_OWNER_UNCERTAIN", existing.review_id)
                lease = RepositoryLease(
                    repo_fingerprint=repo_fingerprint, review_id=review_id,
                    operation=operation, plan_sha256=plan_sha256,
                    lease_id=uuid.uuid4().hex, owner_id=self.owner_id,
                    owner_pid=self.owner_pid,
                    owner_process_identity=self.owner_process_identity,
                    issued_at=now, heartbeat_at=now, expires_at=now + ttl)
                _atomic_json(path, lease.as_dict())
                return lease
            finally:
                self._unlock(fh)

    def release(self, lease: RepositoryLease) -> bool:
        path, lock_path = self._paths(lease.repo_fingerprint)
        with self._thread_lock:
            fh = self._locked(lock_path)
            try:
                current = self._read(path)
                if current is None:
                    return False
                if current.lease_id != lease.lease_id or current.owner_id != self.owner_id:
                    raise LeaseError("LEASE_OWNERSHIP_MISMATCH", lease.review_id)
                path.unlink()
                return True
            finally:
                self._unlock(fh)

    def release_for_review(self, review_id: str) -> int:
        """Release only leases owned by this manager for the exact review."""
        count = 0
        for path in self.root.glob("*.json"):
            fingerprint = path.stem
            try:
                _, lock_path = self._paths(fingerprint)
                fh = self._locked(lock_path)
                try:
                    current = self._read(path)
                    if (current and current.review_id == review_id
                            and current.owner_id == self.owner_id):
                        path.unlink()
                        count += 1
                finally:
                    self._unlock(fh)
            except LeaseError:
                continue
        return count

    def inspect_review(self, review_id: str) -> tuple[RepositoryLease, ...]:
        found: list[RepositoryLease] = []
        for path in self.root.glob("*.json"):
            try:
                lease = self._read(path)
            except LeaseError:
                continue
            if lease and lease.review_id == review_id:
                found.append(lease)
        return tuple(found)

    def reclaim_stale_for_review(self, review_id: str) -> tuple[int, bool]:
        """Remove provably dead expired leases; report any uncertain owner."""
        removed = 0
        uncertain = False
        for lease in self.inspect_review(review_id):
            if lease.expires_at > time.time():
                uncertain = True
                continue
            alive = _alive_with_identity(lease.owner_pid,
                                         lease.owner_process_identity)
            if alive is not False:
                uncertain = True
                continue
            path, lock_path = self._paths(lease.repo_fingerprint)
            fh = self._locked(lock_path)
            try:
                current = self._read(path)
                if current and current.lease_id == lease.lease_id:
                    path.unlink()
                    removed += 1
            finally:
                self._unlock(fh)
        return removed, uncertain

    def cleanup_for_review(self, review_id: str) -> int:
        """Operator-authorized cleanup; refuses any live or unverifiable owner."""
        removed = self.release_for_review(review_id)
        for lease in self.inspect_review(review_id):
            alive = _alive_with_identity(lease.owner_pid,
                                         lease.owner_process_identity)
            if alive is not False:
                raise LeaseError("LEASE_OWNER_UNCERTAIN", lease.review_id)
            path, lock_path = self._paths(lease.repo_fingerprint)
            fh = self._locked(lock_path)
            try:
                current = self._read(path)
                if current and current.lease_id == lease.lease_id:
                    path.unlink()
                    removed += 1
            finally:
                self._unlock(fh)
        return removed
