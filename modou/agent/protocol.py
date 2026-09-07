"""Closed, capability-bound action and observation envelopes for the agent loop.

The language model never receives the signing secret and cannot mint a usable
capability.  A frozen controller plan issues a short-lived token, then every
post-approval tool dispatch is validated against that token and the closed tool
catalog before a handler is reached.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from modou.agent.models import ToolCatalog, ToolRisk


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_RESOURCE_KEYS = frozenset({
    "max_seconds", "max_output_bytes", "max_memory_bytes", "max_processes",
    "max_disk_bytes", "max_files",
})
_RISK_RANK = {ToolRisk.READ_ONLY: 0, ToolRisk.EXECUTES_REPO: 1, ToolRisk.MUTATES: 2}


class ProtocolError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ObservationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ActionRequest:
    review_id: str
    action_id: str
    tool: str
    args: dict
    risk: ToolRisk
    plan_sha256: str
    prerequisite_observation_ids: tuple[str, ...]
    reason_code: str
    reason: str
    idempotency_key: str
    timeout_seconds: float
    resource_budget: dict
    expected_artifacts: tuple[str, ...]
    capability_token: str = field(repr=False)
    target_commit: str = ""
    data_policy_sha256: str = ""
    kind: str = "call_tool"
    schema_version: str = "agent-action-v2"

    @classmethod
    def parse(cls, raw: Any, catalog: ToolCatalog) -> "ActionRequest":
        if not isinstance(raw, dict):
            raise ProtocolError("ACTION_NOT_OBJECT", "action must be an object")
        required = {
            "schema_version", "review_id", "action_id", "kind", "tool", "args",
            "risk", "plan_sha256", "prerequisite_observation_ids", "reason_code",
            "reason", "idempotency_key", "timeout_seconds", "resource_budget",
            "expected_artifacts", "capability_token",
        }
        allowed = required | {"target_commit", "data_policy_sha256"}
        extra = set(raw) - allowed
        missing = required - set(raw)
        if extra or missing:
            raise ProtocolError("ACTION_SCHEMA_INVALID",
                                f"missing={sorted(missing)} extra={sorted(extra)}")
        if not isinstance(raw.get("args"), dict):
            raise ProtocolError("ACTION_SCHEMA_INVALID", "args must be an object")
        for key in ("prerequisite_observation_ids", "expected_artifacts"):
            value = raw.get(key)
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ProtocolError("ACTION_SCHEMA_INVALID", f"{key} must be a string list")
        if not isinstance(raw.get("resource_budget"), dict):
            raise ProtocolError("ACTION_SCHEMA_INVALID", "resource_budget must be an object")
        try:
            action = cls(
                schema_version=str(raw["schema_version"]),
                review_id=str(raw["review_id"]), action_id=str(raw["action_id"]),
                kind=str(raw["kind"]), tool=str(raw["tool"]),
                args=dict(raw["args"]), risk=ToolRisk(raw["risk"]),
                plan_sha256=str(raw["plan_sha256"]),
                prerequisite_observation_ids=tuple(raw["prerequisite_observation_ids"]),
                reason_code=str(raw["reason_code"]), reason=str(raw["reason"]),
                idempotency_key=str(raw["idempotency_key"]),
                timeout_seconds=float(raw["timeout_seconds"]),
                resource_budget=dict(raw["resource_budget"]),
                expected_artifacts=tuple(raw["expected_artifacts"]),
                capability_token=str(raw["capability_token"]),
                target_commit=str(raw.get("target_commit") or ""),
                data_policy_sha256=str(raw.get("data_policy_sha256") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("ACTION_SCHEMA_INVALID", str(exc)) from exc
        action.validate(catalog)
        return action

    def validate(self, catalog: ToolCatalog) -> None:
        if self.schema_version != "agent-action-v2" or self.kind != "call_tool":
            raise ProtocolError("ACTION_KIND_NOT_ALLOWED", self.kind)
        if not _IDENTIFIER.fullmatch(self.review_id):
            raise ProtocolError("ACTION_REVIEW_ID_INVALID", self.review_id)
        if not _IDENTIFIER.fullmatch(self.action_id):
            raise ProtocolError("ACTION_ID_INVALID", self.action_id)
        if not _HEX64.fullmatch(self.plan_sha256):
            raise ProtocolError("ACTION_PLAN_HASH_INVALID", self.plan_sha256)
        if self.target_commit and not re.fullmatch(r"[0-9a-f]{40}", self.target_commit):
            raise ProtocolError("ACTION_TARGET_COMMIT_INVALID", self.target_commit)
        if self.data_policy_sha256 and not _HEX64.fullmatch(self.data_policy_sha256):
            raise ProtocolError("ACTION_DATA_POLICY_INVALID", self.data_policy_sha256)
        if not _IDENTIFIER.fullmatch(self.reason_code):
            raise ProtocolError("ACTION_REASON_CODE_INVALID", self.reason_code)
        if not self.reason or len(self.reason) > 300:
            raise ProtocolError("ACTION_REASON_INVALID", "reason must be 1..300 characters")
        if not _IDENTIFIER.fullmatch(self.idempotency_key):
            raise ProtocolError("ACTION_IDEMPOTENCY_INVALID", self.idempotency_key)
        if not math.isfinite(self.timeout_seconds) or not (0 < self.timeout_seconds <= 3600):
            raise ProtocolError("ACTION_TIMEOUT_INVALID", str(self.timeout_seconds))
        if set(self.resource_budget) - _RESOURCE_KEYS:
            raise ProtocolError("ACTION_RESOURCE_INVALID",
                                str(sorted(set(self.resource_budget) - _RESOURCE_KEYS)))
        for key, value in self.resource_budget.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ProtocolError("ACTION_RESOURCE_INVALID", key)
        for value in (*self.prerequisite_observation_ids, *self.expected_artifacts):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ProtocolError("ACTION_REFERENCE_INVALID", repr(value))
        if not self.capability_token or len(self.capability_token) > 8192:
            raise ProtocolError("ACTION_CAPABILITY_INVALID", "missing or oversized token")
        spec = catalog.check(self.tool, self.args)
        if spec.risk is not self.risk:
            raise ProtocolError("ACTION_RISK_MISMATCH", self.tool)

    def as_dict(self, *, redact_capability: bool = False) -> dict:
        raw = {
            "schema_version": self.schema_version, "review_id": self.review_id,
            "action_id": self.action_id, "kind": self.kind, "tool": self.tool,
            "args": self.args, "risk": self.risk.value,
            "plan_sha256": self.plan_sha256,
            "prerequisite_observation_ids": list(self.prerequisite_observation_ids),
            "reason_code": self.reason_code, "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "timeout_seconds": self.timeout_seconds,
            "resource_budget": self.resource_budget,
            "expected_artifacts": list(self.expected_artifacts),
            "capability_token": self.capability_token,
            "target_commit": self.target_commit,
            "data_policy_sha256": self.data_policy_sha256,
        }
        if redact_capability:
            raw.pop("capability_token")
            raw["capability_token_sha256"] = hashlib.sha256(
                self.capability_token.encode("utf-8")).hexdigest()
        return raw


@dataclass(frozen=True)
class ObservationRecord:
    review_id: str
    observation_id: str
    action_id: str
    status: ObservationStatus
    facts: dict
    artifacts: tuple[dict, ...]
    resource_usage: dict
    executor: dict
    duration_ms: int
    restore_proof: dict
    retryable: bool
    next_step_constraints: tuple[str, ...]
    error_code: str = ""
    schema_version: str = "agent-observation-v2"

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version, "review_id": self.review_id,
            "observation_id": self.observation_id, "action_id": self.action_id,
            "status": self.status.value, "facts": self.facts,
            "artifacts": list(self.artifacts), "resource_usage": self.resource_usage,
            "executor": self.executor, "duration_ms": self.duration_ms,
            "restore_proof": self.restore_proof, "retryable": self.retryable,
            "next_step_constraints": list(self.next_step_constraints),
            "error_code": self.error_code,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(self.as_dict())).hexdigest()


class CapabilityAuthority:
    """Process-local HMAC authority for short-lived execution capabilities."""

    def __init__(self, secret: bytes | None = None):
        self._secret = secret or secrets.token_bytes(32)
        self._revoked: set[str] = set()

    def issue(self, *, review_id: str, plan_sha256: str, repo_fingerprint: str,
              allowed_tools: tuple[str, ...], max_risk: ToolRisk,
              resource_budget: dict, ttl_seconds: int = 3600,
              target_commit: str = "", data_policy_sha256: str = "") -> str:
        now = int(time.time())
        claims = {
            "schema_version": "agent-capability-v1", "review_id": review_id,
            "plan_sha256": plan_sha256, "repo_fingerprint": repo_fingerprint,
            "allowed_tools": sorted(set(allowed_tools)), "max_risk": max_risk.value,
            "resource_budget": resource_budget, "issued_at": now,
            "target_commit": target_commit,
            "data_policy_sha256": data_policy_sha256,
            "expires_at": now + max(1, min(int(ttl_seconds), 7200)),
            "nonce": secrets.token_hex(16),
        }
        payload = _b64(_canonical(claims))
        signature = _b64(hmac.new(self._secret, payload.encode("ascii"),
                                  hashlib.sha256).digest())
        return f"{payload}.{signature}"

    def revoke(self, token: str) -> None:
        """Revoke one exact short-lived token without persisting its plaintext."""
        if token:
            self._revoked.add(hashlib.sha256(token.encode("utf-8")).hexdigest())

    def verify(self, action: ActionRequest, *, repo_fingerprint: str,
               now: int | None = None) -> dict:
        if hashlib.sha256(action.capability_token.encode("utf-8")).hexdigest() in self._revoked:
            raise ProtocolError("CAPABILITY_REVOKED", "capability was revoked")
        try:
            payload, signature = action.capability_token.split(".", 1)
            expected = _b64(hmac.new(self._secret, payload.encode("ascii"),
                                     hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                raise ProtocolError("CAPABILITY_SIGNATURE_INVALID", "signature mismatch")
            claims = json.loads(_unb64(payload).decode("utf-8"))
        except ProtocolError:
            raise
        except Exception as exc:
            raise ProtocolError("CAPABILITY_INVALID", str(exc)) from exc
        current = int(time.time()) if now is None else int(now)
        if claims.get("schema_version") != "agent-capability-v1":
            raise ProtocolError("CAPABILITY_SCHEMA_INVALID", "unsupported capability")
        bindings = {
            "review_id": action.review_id,
            "plan_sha256": action.plan_sha256,
            "repo_fingerprint": repo_fingerprint,
        }
        for key, expected_value in bindings.items():
            if not secrets.compare_digest(str(claims.get(key) or ""), expected_value):
                raise ProtocolError("CAPABILITY_BINDING_MISMATCH", key)
        for key, expected_value in {
                "target_commit": action.target_commit,
                "data_policy_sha256": action.data_policy_sha256}.items():
            claim = str(claims.get(key) or "")
            if claim or expected_value:
                if not secrets.compare_digest(claim, expected_value):
                    raise ProtocolError("CAPABILITY_BINDING_MISMATCH", key)
        if current < int(claims.get("issued_at", 0)) - 30 or current >= int(claims.get("expires_at", 0)):
            raise ProtocolError("CAPABILITY_EXPIRED", "capability is not currently valid")
        if action.tool not in set(claims.get("allowed_tools") or ()):
            raise ProtocolError("CAPABILITY_TOOL_NOT_ALLOWED", action.tool)
        try:
            maximum = ToolRisk(claims["max_risk"])
        except (KeyError, ValueError) as exc:
            raise ProtocolError("CAPABILITY_RISK_INVALID", "invalid risk claim") from exc
        if _RISK_RANK[action.risk] > _RISK_RANK[maximum]:
            raise ProtocolError("CAPABILITY_RISK_EXCEEDED", action.risk.value)
        ceilings = claims.get("resource_budget") or {}
        for key, value in action.resource_budget.items():
            if key not in ceilings or float(value) > float(ceilings[key]):
                raise ProtocolError("CAPABILITY_RESOURCE_EXCEEDED", key)
        return claims
