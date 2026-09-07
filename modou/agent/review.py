"""Product workflow state, deliberately separate from evidence run state."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class ReviewStateError(RuntimeError):
    pass


class ReviewStatus(str, Enum):
    CREATED = "CREATED"
    DRAFTING = "DRAFTING"
    INTAKE_VALIDATED = "INTAKE_VALIDATED"
    PLAN_DRAFTED = "PLAN_DRAFTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PLAN_FROZEN = "PLAN_FROZEN"
    BASELINE_RUNNING = "BASELINE_RUNNING"
    EXECUTING = "EXECUTING"
    REPLANNING = "REPLANNING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    VERIFYING_RESTORE = "VERIFYING_RESTORE"
    PROPOSING_REPAIR = "PROPOSING_REPAIR"
    VERIFYING_REPAIR = "VERIFYING_REPAIR"
    DELIVERING_BRANCH = "DELIVERING_BRANCH"
    SYNTHESIZING = "SYNTHESIZING"
    CANCELLING = "CANCELLING"
    RECOVERING = "RECOVERING"
    CLEANUP_REQUIRED = "CLEANUP_REQUIRED"
    QUARANTINED = "QUARANTINED"
    COMPLETE = "COMPLETE"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({ReviewStatus.COMPLETE, ReviewStatus.PARTIAL,
                      ReviewStatus.FAILED, ReviewStatus.ABORTED,
                      ReviewStatus.COMPLETED, ReviewStatus.CANCELLED})

_NEXT = {
    ReviewStatus.CREATED: {ReviewStatus.INTAKE_VALIDATED, ReviewStatus.FAILED,
                           ReviewStatus.ABORTED, ReviewStatus.DRAFTING},
    ReviewStatus.DRAFTING: {ReviewStatus.PLAN_DRAFTED,
                            ReviewStatus.AWAITING_APPROVAL,
                            ReviewStatus.FAILED, ReviewStatus.ABORTED},
    ReviewStatus.INTAKE_VALIDATED: {ReviewStatus.PLAN_DRAFTED, ReviewStatus.FAILED,
                                    ReviewStatus.ABORTED},
    ReviewStatus.PLAN_DRAFTED: {ReviewStatus.AWAITING_APPROVAL, ReviewStatus.FAILED,
                               ReviewStatus.ABORTED},
    ReviewStatus.AWAITING_APPROVAL: {ReviewStatus.PLAN_FROZEN, ReviewStatus.FAILED,
                                    ReviewStatus.ABORTED},
    ReviewStatus.PLAN_FROZEN: {ReviewStatus.BASELINE_RUNNING, ReviewStatus.FAILED,
                              ReviewStatus.ABORTED, ReviewStatus.CANCELLING},
    ReviewStatus.BASELINE_RUNNING: {ReviewStatus.EXECUTING, ReviewStatus.FAILED,
                                    ReviewStatus.ABORTED, ReviewStatus.CANCELLING},
    ReviewStatus.EXECUTING: {ReviewStatus.VERIFYING_RESTORE,
                             ReviewStatus.REPLANNING, ReviewStatus.AWAITING_HUMAN,
                             ReviewStatus.PROPOSING_REPAIR,
                             ReviewStatus.SYNTHESIZING, ReviewStatus.FAILED,
                             ReviewStatus.ABORTED, ReviewStatus.CANCELLING},
    ReviewStatus.REPLANNING: {ReviewStatus.AWAITING_APPROVAL,
                              ReviewStatus.EXECUTING,
                              ReviewStatus.AWAITING_HUMAN,
                              ReviewStatus.SYNTHESIZING, ReviewStatus.FAILED,
                              ReviewStatus.ABORTED},
    ReviewStatus.AWAITING_HUMAN: {ReviewStatus.EXECUTING,
                                  ReviewStatus.REPLANNING,
                                  ReviewStatus.SYNTHESIZING,
                                  ReviewStatus.FAILED, ReviewStatus.ABORTED},
    ReviewStatus.VERIFYING_RESTORE: {ReviewStatus.EXECUTING,
                                     ReviewStatus.SYNTHESIZING,
                                     ReviewStatus.FAILED, ReviewStatus.ABORTED,
                                     ReviewStatus.CANCELLING},
    ReviewStatus.PROPOSING_REPAIR: {ReviewStatus.VERIFYING_REPAIR,
                                    ReviewStatus.AWAITING_HUMAN,
                                    ReviewStatus.SYNTHESIZING,
                                    ReviewStatus.FAILED, ReviewStatus.ABORTED},
    ReviewStatus.VERIFYING_REPAIR: {ReviewStatus.DELIVERING_BRANCH,
                                    ReviewStatus.PROPOSING_REPAIR,
                                    ReviewStatus.AWAITING_HUMAN,
                                    ReviewStatus.SYNTHESIZING,
                                    ReviewStatus.FAILED, ReviewStatus.ABORTED},
    ReviewStatus.DELIVERING_BRANCH: {ReviewStatus.SYNTHESIZING,
                                     ReviewStatus.COMPLETE,
                                     ReviewStatus.PARTIAL,
                                     ReviewStatus.FAILED, ReviewStatus.ABORTED},
    ReviewStatus.SYNTHESIZING: {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL,
                                ReviewStatus.FAILED, ReviewStatus.ABORTED,
                                ReviewStatus.CANCELLING},
    ReviewStatus.CANCELLING: {ReviewStatus.SYNTHESIZING, ReviewStatus.ABORTED,
                              ReviewStatus.FAILED, ReviewStatus.CLEANUP_REQUIRED,
                              ReviewStatus.QUARANTINED},
    ReviewStatus.RECOVERING: {ReviewStatus.PLAN_FROZEN, ReviewStatus.CANCELLING,
                              ReviewStatus.ABORTED, ReviewStatus.FAILED,
                              ReviewStatus.QUARANTINED},
    ReviewStatus.CLEANUP_REQUIRED: {ReviewStatus.ABORTED, ReviewStatus.FAILED,
                                    ReviewStatus.QUARANTINED},
    ReviewStatus.QUARANTINED: {ReviewStatus.CLEANUP_REQUIRED, ReviewStatus.ABORTED,
                               ReviewStatus.FAILED},
}

# Recovery/cancellation are control-plane escape hatches, not model-selected
# progress.  Every non-terminal state must therefore have a durable path into
# recovery, quarantine, or cancellation after a crash or operator request.
for _status in tuple(ReviewStatus):
    if _status not in TERMINAL:
        _NEXT.setdefault(_status, set()).update(
            {ReviewStatus.RECOVERING, ReviewStatus.QUARANTINED,
             ReviewStatus.CANCELLING} - {_status})


def evidence_status_for(review: ReviewStatus, *, evidence_started: bool,
                        valid_bundle: bool = False) -> str | None:
    """Map product workflow to frozen runroot vocabulary without adding ABORTED."""
    if review.value in {"CREATED", "DRAFTING", "INTAKE_VALIDATED", "PLAN_DRAFTED",
                        "AWAITING_APPROVAL", "PLAN_FROZEN"}:
        return None
    if review in {ReviewStatus.COMPLETE, ReviewStatus.COMPLETED}:
        return "COMPLETE" if valid_bundle else "FAILED"
    if review is ReviewStatus.PARTIAL:
        return "COMPLETE" if valid_bundle else "FAILED"
    if review in {ReviewStatus.FAILED, ReviewStatus.ABORTED,
                  ReviewStatus.CANCELLED,
                  ReviewStatus.CANCELLING, ReviewStatus.RECOVERING,
                  ReviewStatus.CLEANUP_REQUIRED, ReviewStatus.QUARANTINED}:
        return "FAILED" if evidence_started else None
    return "INCOMPLETE"


@dataclass(frozen=True)
class ReviewSnapshot:
    review_id: str
    status: ReviewStatus
    updated_at: str
    reason: str = ""
    evidence_run_id: str = ""
    evidence_started: bool = False
    valid_bundle: bool = False

    def as_dict(self) -> dict:
        return {
            "schema_version": "review-state-v1",
            "review_id": self.review_id,
            "status": self.status.value,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "evidence_run_id": self.evidence_run_id,
            "evidence_started": self.evidence_started,
            "valid_bundle": self.valid_bundle,
            "evidence_status": evidence_status_for(
                self.status, evidence_started=self.evidence_started,
                valid_bundle=self.valid_bundle),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewStore:
    def __init__(self, review_dir: Path, review_id: str):
        self.review_dir = Path(review_dir)
        self.review_id = review_id
        self.path = self.review_dir / "review_state.json"
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot = self._load() if self.path.exists() else ReviewSnapshot(
            review_id=review_id, status=ReviewStatus.CREATED, updated_at=_now())
        if not self.path.exists():
            self._write(self._snapshot)

    def _load(self) -> ReviewSnapshot:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != "review-state-v1":
                raise ReviewStateError("unsupported review state schema")
            if raw.get("review_id") != self.review_id:
                raise ReviewStateError("review id mismatch")
            return ReviewSnapshot(
                review_id=self.review_id,
                status=ReviewStatus(raw["status"]),
                updated_at=str(raw["updated_at"]),
                reason=str(raw.get("reason") or ""),
                evidence_run_id=str(raw.get("evidence_run_id") or ""),
                evidence_started=bool(raw.get("evidence_started")),
                valid_bundle=bool(raw.get("valid_bundle")),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewStateError(f"invalid review_state.json: {exc}") from exc

    def _write(self, snapshot: ReviewSnapshot) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(snapshot.as_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    @property
    def snapshot(self) -> ReviewSnapshot:
        return self._snapshot

    def transition(self, status: ReviewStatus, *, reason: str = "",
                   evidence_run_id: str | None = None,
                   evidence_started: bool | None = None,
                   valid_bundle: bool | None = None,
                   allow_recovery_terminal: bool = False) -> ReviewSnapshot:
        current = self._snapshot.status
        allowed = _NEXT.get(current, set())
        if status not in allowed and not (
                allow_recovery_terminal and (
                    (current not in TERMINAL and status in TERMINAL) or
                    status is ReviewStatus.QUARANTINED)):
            raise ReviewStateError(f"illegal transition {current.value} -> {status.value}")
        self._snapshot = ReviewSnapshot(
            review_id=self.review_id,
            status=status,
            updated_at=_now(),
            reason=reason,
            evidence_run_id=(self._snapshot.evidence_run_id
                             if evidence_run_id is None else evidence_run_id),
            evidence_started=(self._snapshot.evidence_started
                              if evidence_started is None else evidence_started),
            valid_bundle=(self._snapshot.valid_bundle
                          if valid_bundle is None else valid_bundle),
        )
        self._write(self._snapshot)
        return self._snapshot
