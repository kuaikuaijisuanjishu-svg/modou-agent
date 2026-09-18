"""Commit-independent evidence manifests and post-commit delivery records.

An ``EvidenceManifest`` is frozen before Git creates a repair commit. A
``DeliveryRecord`` is written afterwards and may refer to that commit. Keeping
the objects separate avoids a circular hash between a commit and its bundle.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


MANIFEST_SCHEMA = "evidence-manifest-v1"
DELIVERY_SCHEMA = "delivery-record-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class EvidenceManifest:
    review_id: str
    plan_sha256: str
    source_commit: str
    patch_sha256: str
    test_scope: tuple[str, ...]
    test_results: dict
    restore_clean: bool
    generated_test_ids: tuple[str, ...] = ()
    tool_commit: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_scope", tuple(self.test_scope))
        object.__setattr__(self, "generated_test_ids", tuple(self.generated_test_ids))
        object.__setattr__(self, "test_results",
                           json.loads(json.dumps(self.test_results,
                                                 ensure_ascii=False,
                                                 sort_keys=True)))
        if not self.created_at:
            object.__setattr__(self, "created_at",
                               datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        out = asdict(self)
        out["schema_version"] = MANIFEST_SCHEMA
        out["test_scope"] = list(self.test_scope)
        out["generated_test_ids"] = list(self.generated_test_ids)
        out["created_at"] = self.created_at
        return out

    @property
    def sha256(self) -> str:
        return sha256_json(self.as_dict())


@dataclass(frozen=True)
class DeliveryRecord:
    review_id: str
    branch: str
    commit: str
    evidence_manifest_sha256: str
    delivered_at: str
    status: str = "DELIVERED"

    def as_dict(self) -> dict:
        out = asdict(self)
        out["schema_version"] = DELIVERY_SCHEMA
        return out


def build_delivery_record(*, review_id: str, branch: str, commit: str,
                          evidence_manifest_sha256: str,
                          delivered_at: str | None = None) -> DeliveryRecord:
    if not _SHA256.fullmatch(evidence_manifest_sha256):
        raise ValueError("evidence_manifest_sha256 must be a lowercase sha256")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("commit must be a hexadecimal Git object id")
    return DeliveryRecord(
        review_id=review_id, branch=branch, commit=commit,
        evidence_manifest_sha256=evidence_manifest_sha256,
        delivered_at=delivered_at or datetime.now(timezone.utc).isoformat(),
    )


def verify_manifest(manifest: dict, expected_sha256: str | None = None) -> list[str]:
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        problems.append("unsupported manifest schema")
    required = {"schema_version", "review_id", "plan_sha256", "source_commit",
                "patch_sha256", "test_scope", "test_results", "restore_clean",
                "generated_test_ids", "tool_commit", "created_at"}
    missing = sorted(required - set(manifest))
    if missing:
        problems.append("missing fields: " + ", ".join(missing))
    for field in ("plan_sha256", "patch_sha256"):
        if field in manifest and not _SHA256.fullmatch(str(manifest[field])):
            problems.append(f"{field} must be a lowercase sha256")
    if manifest.get("restore_clean") is not True:
        problems.append("restore_clean must be true before delivery")
    if not isinstance(manifest.get("test_scope"), list):
        problems.append("test_scope must be an array")
    if not isinstance(manifest.get("generated_test_ids"), list):
        problems.append("generated_test_ids must be an array")
    if any(key in manifest for key in ("commit", "branch", "delivery_record")):
        problems.append("manifest contains post-commit delivery fields")
    if expected_sha256 and expected_sha256 != sha256_json(manifest):
        problems.append("manifest sha256 mismatch")
    return problems
