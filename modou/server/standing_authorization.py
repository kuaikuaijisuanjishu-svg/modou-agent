"""Standing authorization: a human-issued, scoped pre-approval file.

standing-authorization-v1 is written by a person before any unattended run.
This module only loads and validates it; it grants nothing on its own.  Every
consumer must re-run the deterministic match conditions at use time (level,
repo whitelist, spec isomorphism, expiry, budget, lease ceiling) and must
keep the ordinary approval gates.  The agent can never write, extend, or
re-issue this file — that separation is the whole point.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modou.agent.spec import (AutonomyPolicy, DataPolicy, ModelPolicy,
                              ResourceConstraints, ReviewScope,
                              ReviewSpecError)


SCHEMA_VERSION = "standing-authorization-v1"

# The axes a human may pin.  Per-review identity (source repo id, workspace
# snapshot hash, memory snapshot hash) is deliberately absent: repo identity
# is covered by repo_whitelist and snapshot identity by the STALE_APPROVAL
# fingerprint re-check inside approve().
_SPEC_AXIS_PARSERS = {
    "scope": ReviewScope.parse,
    "constraints": ResourceConstraints.parse,
    "autonomy_policy": AutonomyPolicy.parse,
    "data_policy": DataPolicy.parse,
    "model_policy": ModelPolicy.parse,
}

_TOP_LEVEL_FIELDS = {"schema_version", "repo_whitelist", "budget_seconds_max",
                     "valid_until", "authorized_by", "spec_shape",
                     "max_writes_per_review", "max_writes_per_hour"}


class StandingAuthorizationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StandingAuthorization:
    schema_version: str
    repo_whitelist: tuple[Path, ...]
    budget_seconds_max: float
    valid_until: datetime
    authorized_by: str
    spec_shape: dict
    max_writes_per_review: int
    max_writes_per_hour: int
    source_sha256: str

    @classmethod
    def from_path(cls, path: Path) -> "StandingAuthorization":
        """Load and fully validate; any defect refuses the file."""
        path = Path(path)
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise StandingAuthorizationError(
                "STANDING_AUTHORIZATION_UNREADABLE", str(exc)) from exc
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StandingAuthorizationError(
                "STANDING_AUTHORIZATION_MALFORMED", str(exc)) from exc
        if not isinstance(raw, dict) or set(raw) != _TOP_LEVEL_FIELDS:
            raise StandingAuthorizationError(
                "STANDING_AUTHORIZATION_FIELDS_INVALID",
                "expected exactly: " + ", ".join(sorted(_TOP_LEVEL_FIELDS)))
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise StandingAuthorizationError(
                "STANDING_AUTHORIZATION_VERSION_INVALID",
                f"schema_version must be {SCHEMA_VERSION}")
        whitelist_raw = raw.get("repo_whitelist")
        if (not isinstance(whitelist_raw, list) or not whitelist_raw
                or not all(isinstance(item, str) and item.strip()
                           for item in whitelist_raw)):
            raise StandingAuthorizationError(
                "REPO_WHITELIST_INVALID",
                "repo_whitelist must be a non-empty array of absolute paths")
        repos: list[Path] = []
        for entry in whitelist_raw:
            candidate = Path(entry)
            if not candidate.is_absolute():
                raise StandingAuthorizationError(
                    "REPO_WHITELIST_INVALID",
                    f"repository path must be absolute: {entry}")
            repos.append(candidate.expanduser().resolve())
        if len(set(repos)) != len(repos):
            raise StandingAuthorizationError(
                "REPO_WHITELIST_INVALID", "repo_whitelist contains duplicates")
        budget = raw.get("budget_seconds_max")
        if (isinstance(budget, bool) or not isinstance(budget, (int, float))
                or not 1 <= float(budget) <= 86400):
            raise StandingAuthorizationError(
                "BUDGET_CEILING_INVALID",
                "budget_seconds_max must be a number between 1 and 86400")
        try:
            valid_until = datetime.fromisoformat(str(raw.get("valid_until") or ""))
        except ValueError as exc:
            raise StandingAuthorizationError(
                "VALID_UNTIL_INVALID",
                "valid_until must be an ISO 8601 timestamp") from exc
        if valid_until.tzinfo is None:
            raise StandingAuthorizationError(
                "VALID_UNTIL_INVALID", "valid_until must carry a timezone")
        authorized_by = str(raw.get("authorized_by") or "").strip()
        if not authorized_by or len(authorized_by) > 120:
            raise StandingAuthorizationError(
                "AUTHORIZED_BY_INVALID",
                "authorized_by must be a non-empty string of at most 120 chars")
        shape = raw.get("spec_shape")
        if (not isinstance(shape, dict) or not shape
                or set(shape) - set(_SPEC_AXIS_PARSERS)):
            raise StandingAuthorizationError(
                "SPEC_SHAPE_INVALID",
                "spec_shape must pin at least one of: "
                + ", ".join(sorted(_SPEC_AXIS_PARSERS)))
        for axis, parser in _SPEC_AXIS_PARSERS.items():
            if axis in shape:
                try:
                    parser(shape[axis])
                except ReviewSpecError as exc:
                    raise StandingAuthorizationError(
                        "SPEC_SHAPE_INVALID", f"{axis}: {exc.detail}") from exc

        def _cap(key: str) -> int:
            value = raw.get(key)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 1 <= value <= 1000):
                raise StandingAuthorizationError(
                    "WRITE_CAP_INVALID",
                    f"{key} must be an integer between 1 and 1000")
            return value

        return cls(
            schema_version=SCHEMA_VERSION,
            repo_whitelist=tuple(repos),
            budget_seconds_max=float(budget),
            valid_until=valid_until,
            authorized_by=authorized_by,
            spec_shape={axis: dict(value) for axis, value in shape.items()},
            max_writes_per_review=_cap("max_writes_per_review"),
            max_writes_per_hour=_cap("max_writes_per_hour"),
            source_sha256=hashlib.sha256(raw_bytes).hexdigest())

    def covers_repo(self, repo: Path) -> bool:
        return Path(repo).expanduser().resolve() in self.repo_whitelist

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now >= self.valid_until

    def pinned_axes(self) -> dict[str, Any]:
        """Parsed axis values, ready for dataclasses.replace on a ReviewSpec."""
        return {axis: _SPEC_AXIS_PARSERS[axis](value)
                for axis, value in self.spec_shape.items()}
