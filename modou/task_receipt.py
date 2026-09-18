"""Portable integrity checks for version-bound evidence-task receipts.

The helper deliberately validates *integrity and shape* only.  A valid hash is
not a claim that the underlying task was correct, and callers must still apply
the receipt's status and source binding rules before presenting acceptance.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .task_schema import TASK_RECEIPT_SCHEMA_VERSION


_TASK_ID = re.compile(r"^task-[a-f0-9]{24}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_STATUSES = frozenset({"supported", "gap_remains", "accepted_risk", "inconclusive", "pending"})


class TaskReceiptError(ValueError):
    """A receipt is malformed or its integrity digest does not match."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TaskReceiptView:
    """The small, safe projection clients need after integrity validation."""

    task_id: str
    status: str
    source_head: str
    source_is_clean: bool | None
    receipt_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": True,
            "integrity": "sha256_verified",
            "meaning": "integrity_only_not_acceptance",
            "task_id": self.task_id,
            "status": self.status,
            "source_head": self.source_head,
            "source_is_clean": self.source_is_clean,
            "receipt_sha256": self.receipt_sha256,
        }


def canonical_payload(receipt: Mapping[str, Any]) -> bytes:
    """Return the exact bytes covered by ``receipt_sha256``.

    The digest field is excluded; all other fields, including nested task
    criteria and event history, are covered with deterministic JSON encoding.
    """

    if not isinstance(receipt, Mapping):
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "receipt must be an object")
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "receipt contains non-JSON data") from exc


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 without trusting a supplied digest."""

    return hashlib.sha256(canonical_payload(receipt)).hexdigest()


def validate_task_receipt(receipt: Mapping[str, Any], *, expected_head: str | None = None,
                          require_clean: bool = False) -> TaskReceiptView:
    """Validate a task receipt and return a safe integrity projection.

    ``expected_head`` and ``require_clean`` are optional caller-side binding
    checks.  They never upgrade a ``pending`` or ``accepted_risk`` status.
    """

    if not isinstance(receipt, Mapping):
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "receipt must be an object")
    if receipt.get("receipt_schema_version") != TASK_RECEIPT_SCHEMA_VERSION:
        raise TaskReceiptError("TASK_RECEIPT_SCHEMA_INVALID", "unsupported receipt schema")
    task_id = receipt.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "task_id is invalid")
    status = receipt.get("status")
    if status not in _STATUSES:
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "status is invalid")
    digest = receipt.get("receipt_sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise TaskReceiptError("TASK_RECEIPT_HASH_MISSING", "receipt_sha256 is invalid")
    computed = receipt_sha256(receipt)
    if not hmac.compare_digest(digest, computed):
        raise TaskReceiptError("TASK_RECEIPT_HASH_MISMATCH", "receipt_sha256 does not match payload")

    source_head = receipt.get("source_head", "")
    if not isinstance(source_head, str) or (source_head and not _HEAD.fullmatch(source_head)):
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "source_head is invalid")
    source_is_clean = receipt.get("source_is_clean")
    if source_is_clean is not None and not isinstance(source_is_clean, bool):
        raise TaskReceiptError("TASK_RECEIPT_INVALID", "source_is_clean must be boolean or null")
    if expected_head is not None:
        if not isinstance(expected_head, str) or not _HEAD.fullmatch(expected_head):
            raise TaskReceiptError("TASK_RECEIPT_HEAD_INVALID", "expected_head is invalid")
        if source_head != expected_head:
            raise TaskReceiptError("TASK_RECEIPT_HEAD_MISMATCH", "receipt is bound to another source head")
    if require_clean and source_is_clean is not True:
        raise TaskReceiptError("TASK_RECEIPT_NOT_CLEAN", "receipt is not bound to a clean source")
    return TaskReceiptView(task_id, status, source_head, source_is_clean, digest)


def verify_task_receipt(receipt: Mapping[str, Any], *, expected_head: str | None = None,
                        require_clean: bool = False) -> dict[str, Any]:
    """Return a JSON-friendly result instead of raising for CLI/SDK callers."""

    try:
        return validate_task_receipt(receipt, expected_head=expected_head,
                                     require_clean=require_clean).as_dict()
    except TaskReceiptError as exc:
        return {"valid": False, "integrity": "rejected", "code": exc.code,
                "message": exc.detail, "meaning": "integrity_only_not_acceptance"}
