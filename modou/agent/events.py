"""Durable review event journal shared by live streaming and replay."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = "review-event-v1"


class EventJournalError(RuntimeError):
    """The on-disk journal is not a contiguous, trustworthy sequence."""


@dataclass(frozen=True)
class ReviewEvent:
    schema_version: str
    review_id: str
    event_id: str
    seq: int
    kind: str
    occurred_at: str
    data: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "ReviewEvent":
        try:
            return cls(
                schema_version=str(raw["schema_version"]),
                review_id=str(raw["review_id"]),
                event_id=str(raw["event_id"]),
                seq=int(raw["seq"]),
                kind=str(raw["kind"]),
                occurred_at=str(raw["occurred_at"]),
                data=dict(raw.get("data") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EventJournalError(f"invalid event record: {exc}") from exc

    def as_dict(self) -> dict:
        return asdict(self)


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class EventStore:
    """Append-only, atomically published event files with restart-safe seq."""

    def __init__(self, review_dir: Path, review_id: str):
        self.review_dir = Path(review_dir)
        self.review_id = review_id
        self.events_dir = self.review_dir / "events"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._events = self._load_and_validate()
        self._next_seq = len(self._events) + 1

    def _load_and_validate(self) -> list[ReviewEvent]:
        files = sorted(self.events_dir.glob("*.json"))
        out: list[ReviewEvent] = []
        for expected, path in enumerate(files, 1):
            if path.name != f"{expected:06d}.json":
                raise EventJournalError(
                    f"event gap or unexpected filename: expected {expected:06d}.json, got {path.name}")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EventJournalError(f"cannot read {path.name}: {exc}") from exc
            event = ReviewEvent.from_dict(raw)
            if event.schema_version != SCHEMA_VERSION:
                raise EventJournalError(f"unsupported event schema: {event.schema_version}")
            if event.review_id != self.review_id:
                raise EventJournalError(
                    f"review id mismatch at seq {expected}: {event.review_id}")
            if event.seq != expected:
                raise EventJournalError(
                    f"event seq mismatch: expected {expected}, got {event.seq}")
            expected_id = f"{self.review_id}:{expected:06d}"
            if event.event_id != expected_id:
                raise EventJournalError(
                    f"event id mismatch: expected {expected_id}, got {event.event_id}")
            out.append(event)
        return out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._next_seq - 1

    def append(self, kind: str, data: dict | None = None) -> ReviewEvent:
        with self._changed:
            seq = self._next_seq
            event = ReviewEvent(
                schema_version=SCHEMA_VERSION,
                review_id=self.review_id,
                event_id=f"{self.review_id}:{seq:06d}",
                seq=seq,
                kind=kind,
                occurred_at=datetime.now(timezone.utc).isoformat(),
                data=dict(data or {}),
            )
            _atomic_json(self.events_dir / f"{seq:06d}.json", event.as_dict())
            self._events.append(event)
            self._next_seq += 1
            self._changed.notify_all()
            return event

    def since(self, cursor: int = 0) -> tuple[ReviewEvent, ...]:
        if cursor < 0:
            raise EventJournalError("cursor must be non-negative")
        with self._lock:
            return tuple(e for e in self._events if e.seq > cursor)
    def all(self) -> tuple[ReviewEvent, ...]:
        return self.since(0)

    def wait_since(self, cursor: int, timeout: float = 15.0) -> tuple[ReviewEvent, ...]:
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.last_seq <= cursor:
                left = deadline - time.monotonic()
                if left <= 0:
                    return ()
                self._changed.wait(left)
            return tuple(e for e in self._events if e.seq > cursor)
