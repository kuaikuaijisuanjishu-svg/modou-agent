"""Provider-neutral repair proposals; never an execution or commit authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from modou.sensitive import scan_text
from .repair import RepairError, validate_candidate_patch


class RepairProposalProvider(Protocol):
    def propose_repair(self, payload: dict) -> dict: ...


@dataclass(frozen=True)
class RepairContext:
    review_id: str
    plan_sha256: str
    finding_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    snippets: tuple[dict, ...]
    max_files: int = 5
    max_changed_lines: int = 400

    def safe_payload(self) -> dict:
        if len(self.finding_ids) > 50 or len(self.allowed_paths) > 50:
            raise RepairError("REPAIR_CONTEXT_TOO_LARGE", "too many findings or paths")
        snippets: list[dict] = []
        total = 0
        for item in self.snippets:
            if not isinstance(item, dict) or set(item) != {"path", "start_line", "text"}:
                raise RepairError("REPAIR_CONTEXT_INVALID", "snippet shape is invalid")
            path, text = str(item["path"]), str(item["text"])
            if self.allowed_paths and not any(
                    path == rule or Path(path).match(rule)
                    for rule in self.allowed_paths):
                raise RepairError("REPAIR_PATH_NOT_APPROVED", path)
            total += len(text.encode("utf-8"))
            if total > 12 * 1024:
                raise RepairError("REPAIR_CONTEXT_TOO_LARGE", "snippets exceed 12 KiB")
            findings = scan_text(text, path=path)
            if findings:
                raise RepairError("REPAIR_CONTEXT_SENSITIVE",
                                  ",".join(sorted({item.rule for item in findings})))
            snippets.append({"path": path, "start_line": int(item["start_line"]),
                             "text": text})
        return {
            "schema_version": "repair-context-v1", "review_id": self.review_id,
            "plan_sha256": self.plan_sha256, "finding_ids": list(self.finding_ids),
            "allowed_paths": list(self.allowed_paths), "snippets": snippets,
            "limits": {"max_files": self.max_files,
                       "max_changed_lines": self.max_changed_lines},
        }


@dataclass(frozen=True)
class RepairCandidate:
    patch: str
    summary: str
    finding_ids: tuple[str, ...]
    generated_test_ids: tuple[str, ...] = ()
    schema_version: str = "repair-candidate-v1"

    @classmethod
    def parse(cls, raw: Any, context: RepairContext) -> "RepairCandidate":
        if not isinstance(raw, dict) or set(raw) != {
                "schema_version", "patch", "summary", "finding_ids", "generated_test_ids"}:
            raise RepairError("REPAIR_CANDIDATE_SCHEMA", "candidate fields are invalid")
        if raw.get("schema_version") != "repair-candidate-v1":
            raise RepairError("REPAIR_CANDIDATE_SCHEMA", "candidate schema is unsupported")
        finding_ids = raw.get("finding_ids")
        test_ids = raw.get("generated_test_ids")
        if (not isinstance(finding_ids, list) or not all(isinstance(x, str) for x in finding_ids)
                or not isinstance(test_ids, list) or not all(isinstance(x, str) for x in test_ids)):
            raise RepairError("REPAIR_CANDIDATE_SCHEMA", "ids must be string arrays")
        if not set(finding_ids).issubset(context.finding_ids):
            raise RepairError("REPAIR_FINDING_NOT_APPROVED", "candidate invented a finding")
        patch, summary = str(raw.get("patch") or ""), str(raw.get("summary") or "")
        if not summary or len(summary) > 300:
            raise RepairError("REPAIR_CANDIDATE_SCHEMA", "summary must be 1..300 characters")
        validate_candidate_patch(patch, allowed_paths=context.allowed_paths,
                                 max_files=context.max_files,
                                 max_changed_lines=context.max_changed_lines)
        return cls(patch, summary, tuple(finding_ids), tuple(test_ids))

    @property
    def patch_sha256(self) -> str:
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()

    def public_dict(self) -> dict:
        return {"schema_version": self.schema_version, "summary": self.summary,
                "finding_ids": list(self.finding_ids),
                "generated_test_ids": list(self.generated_test_ids),
                "patch_sha256": self.patch_sha256,
                "patch_bytes": len(self.patch.encode("utf-8"))}


def generate_candidate(provider: RepairProposalProvider, context: RepairContext) -> RepairCandidate:
    payload = context.safe_payload()
    raw = provider.propose_repair(payload)
    candidate = RepairCandidate.parse(raw, context)
    # Keep this second check explicit: parsing model JSON and authorizing its
    # patch are separate policy decisions even when they currently share code.
    validate_candidate_patch(candidate.patch, allowed_paths=context.allowed_paths,
                             max_files=context.max_files,
                             max_changed_lines=context.max_changed_lines)
    return candidate
