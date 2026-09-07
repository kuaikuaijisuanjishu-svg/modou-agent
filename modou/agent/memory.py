"""Versioned, user-confirmed repository review memory.

The file name is YAML for developer ergonomics, while its first version uses
the JSON subset of YAML 1.2 so the trusted control plane needs no permissive
YAML parser.  Rules are advisory context only; they never grant tool or
network permission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "shuimu-review-memory-v1"
RELATIVE_PATH = Path(".shuimu/review-memory.yaml")
_KINDS = frozenset({"review_preference", "known_baseline", "test_convention",
                    "architecture_constraint"})
_STATUSES = frozenset({"active", "deprecated", "revoked"})
_UNIX_PERSONAL_PREFIXES = ("/" + "Users" + "/", "/" + "home" + "/")
_PERSONAL_PATH = re.compile(
    r"(?:%s|%s|[A-Za-z]:\\\\)" % tuple(re.escape(x) for x in _UNIX_PERSONAL_PREFIXES))
_SECRET_SHAPE = re.compile(r"(?:sk-[A-Za-z0-9]{12,}|AKIA[0-9A-Z]{16})")


class ReviewMemoryError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _strings(value, field: str, *, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", field)
    if len(value) > limit or len(value) != len(set(value)):
        raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", field)
    return tuple(value)


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    rule: str
    applies_to: tuple[str, ...]
    kind: str
    source_review_id: str
    evidence_ids: tuple[str, ...]
    confirmed_at: str
    status: str = "active"

    @classmethod
    def parse(cls, raw: object) -> "MemoryRecord":
        if not isinstance(raw, dict) or set(raw) != {
                "memory_id", "rule", "applies_to", "kind", "source_review_id",
                "evidence_ids", "confirmed_at", "status"}:
            raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "record fields")
        record = cls(memory_id=str(raw["memory_id"]), rule=str(raw["rule"]),
                     applies_to=_strings(raw["applies_to"], "applies_to", limit=40),
                     kind=str(raw["kind"]), source_review_id=str(raw["source_review_id"]),
                     evidence_ids=_strings(raw["evidence_ids"], "evidence_ids", limit=80),
                     confirmed_at=str(raw["confirmed_at"]), status=str(raw["status"]))
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", record.memory_id):
            raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "memory_id")
        if record.kind not in _KINDS or record.status not in _STATUSES:
            raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "kind or status")
        if len(record.rule) > 500 or _PERSONAL_PATH.search(record.rule) or _SECRET_SHAPE.search(record.rule):
            raise ReviewMemoryError("MEMORY_CONTENT_FORBIDDEN", "rule")
        for path in record.applies_to:
            candidate = Path(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ReviewMemoryError("MEMORY_PATH_FORBIDDEN", path)
        return record

    def as_dict(self) -> dict:
        raw = asdict(self)
        raw["applies_to"] = list(self.applies_to)
        raw["evidence_ids"] = list(self.evidence_ids)
        return raw


@dataclass(frozen=True)
class MemorySnapshot:
    records: tuple[MemoryRecord, ...]
    sha256: str

    def active_rules(self) -> list[dict]:
        return [{"memory_id": item.memory_id, "rule": item.rule,
                 "applies_to": list(item.applies_to), "kind": item.kind}
                for item in self.records if item.status == "active"]


def load(repo: Path) -> MemorySnapshot:
    path = Path(repo) / RELATIVE_PATH
    if not path.exists():
        return MemorySnapshot(records=(), sha256="")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewMemoryError("MEMORY_FILE_INVALID", str(exc)) from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "records"}:
        raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "top-level fields")
    if raw["schema_version"] != SCHEMA_VERSION or not isinstance(raw["records"], list):
        raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "schema_version")
    records = tuple(MemoryRecord.parse(item) for item in raw["records"])
    ids = [item.memory_id for item in records]
    if len(ids) != len(set(ids)):
        raise ReviewMemoryError("MEMORY_SCHEMA_INVALID", "duplicate memory_id")
    canonical = json.dumps({"schema_version": SCHEMA_VERSION,
                            "records": [item.as_dict() for item in records]},
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return MemorySnapshot(records=records,
                          sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def render(records: tuple[MemoryRecord, ...]) -> str:
    """Render the safe JSON subset of YAML for a confirmed worktree update."""
    return json.dumps({"schema_version": SCHEMA_VERSION,
                       "records": [item.as_dict() for item in records]},
                      ensure_ascii=False, indent=2) + "\n"


@dataclass(frozen=True)
class MemoryProposal:
    """Unpersisted candidate. A proposal cannot become repository authority."""

    memory_id: str
    rule: str
    applies_to: tuple[str, ...]
    kind: str
    source_review_id: str
    evidence_ids: tuple[str, ...]

    def validate(self) -> None:
        MemoryRecord.parse({
            "memory_id": self.memory_id, "rule": self.rule,
            "applies_to": list(self.applies_to), "kind": self.kind,
            "source_review_id": self.source_review_id,
            "evidence_ids": list(self.evidence_ids),
            "confirmed_at": "pending", "status": "active",
        })


def confirm(repo: Path, proposal: MemoryProposal, *, confirmed_by: str) -> MemorySnapshot:
    """Persist one proposal only after an explicit, attributable confirmation."""
    if not isinstance(confirmed_by, str) or not confirmed_by.strip() or len(confirmed_by) > 100:
        raise ReviewMemoryError("MEMORY_CONFIRMATION_REQUIRED", "confirmed_by")
    proposal.validate()
    current = load(repo)
    if any(item.memory_id == proposal.memory_id for item in current.records):
        raise ReviewMemoryError("MEMORY_ID_EXISTS", proposal.memory_id)
    # The confirmation subject is deliberately audit-only and not placed in
    # the advisory rule file; the event/controller records it separately.
    record = MemoryRecord(
        proposal.memory_id, proposal.rule, proposal.applies_to, proposal.kind,
        proposal.source_review_id, proposal.evidence_ids,
        datetime.now(timezone.utc).isoformat(), "active")
    _write(repo, (*current.records, record))
    return load(repo)


def set_status(repo: Path, memory_id: str, status: str, *, confirmed_by: str) -> MemorySnapshot:
    if status not in {"deprecated", "revoked"}:
        raise ReviewMemoryError("MEMORY_STATUS_INVALID", status)
    if not confirmed_by.strip():
        raise ReviewMemoryError("MEMORY_CONFIRMATION_REQUIRED", "confirmed_by")
    current = load(repo)
    found = False
    records = []
    for item in current.records:
        if item.memory_id == memory_id:
            found = True
            item = MemoryRecord(**{**item.__dict__, "status": status})
        records.append(item)
    if not found:
        raise ReviewMemoryError("MEMORY_NOT_FOUND", memory_id)
    _write(repo, tuple(records))
    return load(repo)


def delete(repo: Path, memory_id: str, *, confirmed_by: str) -> MemorySnapshot:
    if not confirmed_by.strip():
        raise ReviewMemoryError("MEMORY_CONFIRMATION_REQUIRED", "confirmed_by")
    current = load(repo)
    records = tuple(item for item in current.records if item.memory_id != memory_id)
    if len(records) == len(current.records):
        raise ReviewMemoryError("MEMORY_NOT_FOUND", memory_id)
    _write(repo, records)
    return load(repo)


def export(repo: Path) -> dict:
    snapshot = load(repo)
    return {"schema_version": SCHEMA_VERSION, "sha256": snapshot.sha256,
            "records": [item.as_dict() for item in snapshot.records]}


def _write(repo: Path, records: tuple[MemoryRecord, ...]) -> None:
    root = Path(repo).resolve(strict=True)
    path = root / RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = render(records)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
