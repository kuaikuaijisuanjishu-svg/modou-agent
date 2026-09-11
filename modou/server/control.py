"""Review orchestration and server-side repository capability registry."""
from __future__ import annotations

import json
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from modou.application import AnalysisRequest, ExecutionMode
from modou.executor import (DEFAULT_RESOURCE_LIMITS, SandboxedExecutor,
                            TrustedLocalExecutor, execution_scope,
                            recover_process_record, sanitized_environment)
from modou.agent.events import EventJournalError, EventStore
from modou.agent.models import (ActionKind, AgentLevel, AgentState, CATALOG_V1,
                                Observation as AgentObservation,
                                ReprioritizationRejected, StopReason, ToolCatalog,
                                ToolRisk)
from modou.agent.protocol import (ActionRequest, CapabilityAuthority,
                                  ObservationRecord, ObservationStatus,
                                  ProtocolError)
from modou.agent.lease import (LeaseError, RepositoryLease,
                               RepositoryLeaseManager)
from modou.agent.narrator import (NarrationLayout, NarrationRejected,
                                  compile_narration, deterministic_layout)
from modou.agent.policy import (PolicyError, compile_plan, deterministic_draft)
from modou.agent.spec import DEFAULT_CREATED_AT, ReviewSpec, ReviewSpecError
from modou.agent.memory import ReviewMemoryError, load as load_review_memory
from modou.agent.evidence import EvidenceManifest, verify_manifest
from modou.agent.repair import (RepairError, RepairVerification,
                                deliver_verified_patch)
from modou.agent.repair_candidate import RepairContext, generate_candidate
from modou.governance import (GovernanceError, delete_review as delete_review_data,
                              export_manifest as review_export_manifest)
from modou.agent.provider import (OpenAICompatibleProvider, ProviderUnavailable)
from modou.agent.recommendations import (RECOMMENDATION_PROMPT_VERSION,
                                         RecommendationRejected, build_input,
                                         failure_label, parse_recommendations,
                                         retry_hint)
from modou.agent.review import (ReviewStateError, ReviewStatus, ReviewStore,
                                TERMINAL)
from modou.agent.session import AnalysisSession, SessionError, ToolRouter
from modou.ledger import records as ledger_records, store as ledger_store
from modou.runroot import RunRoot, verify_bundle
from modou import capabilities as caps
from modou.ddmin import ProbeStrategy
from modou.agent.provider import coverage_first_order
from modou.review_bundle import (build_review_bundle_v2, content_sha256,
                                 payload_digest)
from modou.repository_security import (RepositorySecurityError,
                                       require_repository_safe)
from modou.safe_git import run_git

REVIEW_FOCUSES = frozenset({
    "evidence-boundary", "named-regression", "coverage-gap",
    "call-path", "budget-first",
})


class IntakeError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class RegisteredRepo:
    repo_id: str
    path: Path
    display_name: str
    python: str


@dataclass(frozen=True)
class ReviewPreset:
    """A safe, public alias for a pre-approved real review input."""

    preset_id: str
    display_name: str
    description: str
    repo_id: str
    test_files: tuple[str, ...]
    goal: str
    budget_seconds: int
    model_provider: str


class RepoRegistry:
    """Opaque capabilities for canonical, explicitly allowed Git repositories."""

    def __init__(self, paths: list[Path], python_by_repo: dict[Path, Path] | None = None,
                 presets: list[dict] | None = None):
        self._repos: dict[str, RegisteredRepo] = {}
        # Do not resolve interpreter symlinks: invoking a venv through its own
        # bin/python path is how Python discovers that environment.
        python_by_repo = {Path(k).expanduser().resolve(): Path(v).expanduser().absolute()
                          for k, v in (python_by_repo or {}).items()}
        seen: set[Path] = set()
        for raw in paths:
            path = Path(raw).expanduser().resolve(strict=True)
            if path in seen:
                continue
            py = python_by_repo.get(path, Path(sys.executable))
            self.add(path, python=py)
            seen.add(path)
        if not self._repos:
            raise IntakeError("NO_ALLOWED_REPOS", "at least one --allow-repo is required")
        self._presets = self._compile_presets(presets or [])

    def public(self) -> list[dict]:
        return [{"repo_id": r.repo_id, "display_name": r.display_name}
                for r in self._repos.values()]

    def add(self, raw_path: Path | str, *, python: Path | str | None = None) -> RegisteredRepo:
        """Authorize one local Git root for this server process.

        The browser receives only an opaque repository id. This mutation is
        protected by the same localhost origin and launch token as review
        creation, and lasts only until the local server exits.
        """
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise IntakeError("REPO_INVALID", f"repository path does not exist: {raw_path}") from exc
        for existing in self._repos.values():
            if existing.path == path:
                return existing
        try:
            check = run_git(["rev-parse", "--show-toplevel"], cwd=path,
                            timeout=10)
        except OSError as exc:
            raise IntakeError("REPO_INVALID", f"cannot inspect {path}: {exc}") from exc
        if check.returncode != 0 or Path(check.stdout.strip()).resolve() != path:
            raise IntakeError("REPO_INVALID", f"not a Git repository root: {path}")
        interpreter = Path(python or sys.executable).expanduser().absolute()
        if not interpreter.is_file():
            raise IntakeError("PYTHON_INVALID", f"interpreter not found for {path.name}: {interpreter}")
        repo_id = secrets.token_urlsafe(18)
        repo = RegisteredRepo(repo_id, path, path.name, str(interpreter))
        self._repos[repo_id] = repo
        return repo

    def presets_public(self) -> list[dict]:
        """Return presentation inputs without exposing a filesystem path."""
        return [{
            "preset_id": p.preset_id,
            "display_name": p.display_name,
            "description": p.description,
            "repo_id": p.repo_id,
            "test_files": list(p.test_files),
            "goal": p.goal,
            "budget_seconds": p.budget_seconds,
            "model_provider": p.model_provider,
        } for p in self._presets]

    def _compile_presets(self, raw_presets: list[dict]) -> tuple[ReviewPreset, ...]:
        by_name: dict[str, list[RegisteredRepo]] = {}
        for repo in self._repos.values():
            by_name.setdefault(repo.display_name, []).append(repo)
        compiled: list[ReviewPreset] = []
        seen: set[str] = set()
        allowed = {"preset_id", "display_name", "description", "repo_name",
                   "test_files", "goal", "budget_seconds", "model_provider"}
        for raw in raw_presets:
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise IntakeError("PRESET_INVALID", "preset has unsupported fields")
            preset_id = str(raw.get("preset_id") or "")
            if (not preset_id or preset_id in seen or len(preset_id) > 64
                    or not all(c.isalnum() or c in "-_" for c in preset_id)):
                raise IntakeError("PRESET_INVALID", "preset_id must be unique and URL-safe")
            matches = by_name.get(str(raw.get("repo_name") or ""), [])
            if len(matches) != 1:
                raise IntakeError("PRESET_INVALID", "repo_name must identify one allowed repository")
            repo = matches[0]
            tests = self.validate_test_files(repo, raw.get("test_files") or [])
            budget = int(raw.get("budget_seconds") or 300)
            provider = str(raw.get("model_provider") or "deterministic")
            if not 1 <= budget <= 3600 or provider not in {"deterministic", "live"}:
                raise IntakeError("PRESET_INVALID", "preset budget or provider is invalid")
            compiled.append(ReviewPreset(
                preset_id=preset_id,
                display_name=str(raw.get("display_name") or preset_id)[:100],
                description=str(raw.get("description") or "")[:240],
                repo_id=repo.repo_id,
                test_files=tests,
                goal=str(raw.get("goal") or "审查补丁新增代码的证据边界")[:500],
                budget_seconds=budget,
                model_provider=provider,
            ))
            seen.add(preset_id)
        return tuple(compiled)

    def get(self, repo_id: str) -> RegisteredRepo:
        try:
            return self._repos[repo_id]
        except KeyError as exc:
            raise IntakeError("UNKNOWN_REPO_ID", "repository is not registered") from exc

    def validate_test_files(self, repo: RegisteredRepo,
                            raw_paths: list[str]) -> tuple[str, ...]:
        if not raw_paths:
            raise IntakeError("TEST_SCOPE_REQUIRED", "at least one test file is required")
        out: list[str] = []
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                raise IntakeError("TEST_PATH_INVALID", "test path must be a non-empty string")
            p = Path(raw)
            if p.is_absolute() or ".." in p.parts:
                raise IntakeError("TEST_PATH_ESCAPE", raw)
            try:
                resolved = (repo.path / p).resolve(strict=True)
                resolved.relative_to(repo.path)
            except (OSError, ValueError) as exc:
                raise IntakeError("TEST_PATH_ESCAPE", raw) from exc
            if not resolved.is_file():
                raise IntakeError("TEST_PATH_INVALID", raw)
            rel = resolved.relative_to(repo.path).as_posix()
            if rel != p.as_posix().lstrip("./"):
                # Any symlink is rejected, even an in-repo one, so UI input and
                # executed path have exactly one identity.
                raise IntakeError("TEST_PATH_SYMLINK", raw)
            out.append(rel)
        return tuple(dict.fromkeys(out))

    def discover_python_test_files(self, repo: RegisteredRepo) -> tuple[str, ...]:
        """Return a bounded, deterministic local pytest-file suggestion.

        Discovery is intentionally only a convenience for v2 intake.  The
        resolved paths still go through ``validate_test_files`` before any
        repository code is run.
        """
        ignored = {".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache"}
        found: list[str] = []
        for path in repo.path.rglob("*.py"):
            relative = path.relative_to(repo.path)
            if ignored.intersection(relative.parts):
                continue
            name = path.name
            if name.startswith("test_") or name.endswith("_test.py"):
                found.append(relative.as_posix())
                if len(found) > 200:
                    raise IntakeError("TEST_DISCOVERY_TOO_BROAD", "more than 200 Python test files")
        if not found:
            raise IntakeError("TEST_DISCOVERY_EMPTY", "no Python test files found; provide scope.test_files")
        return self.validate_test_files(repo, sorted(found))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ReviewRuntime:
    def __init__(self, review_id: str, review_dir: Path):
        self.review_id = review_id
        self.review_dir = review_dir
        self.events = EventStore(review_dir, review_id)
        self.state = ReviewStore(review_dir, review_id)
        self.session: AnalysisSession | None = None
        self.router: ToolRouter | None = None
        self.frozen_plan = None
        self.request: dict = {}
        self.provider = None
        self.review_spec: ReviewSpec | None = None
        # Provider 是进程级单例，指标与 trace 会跨 Review 累积；每次 create
        # 记下基线，出包时只取本次增量——第一遍 4 次、第二遍 8 次的累计
        # 现象从这里根治。
        self.metrics_base: dict = {}
        self.trace_start: int = 0
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.capability_token = ""
        self.repo_fingerprint = ""
        self.resource_budget: dict = {}
        self.target_commit = ""
        self.data_policy_sha256 = ""
        self.action_seq = 0
        self.observation_ids: list[str] = []
        self.lease: RepositoryLease | None = None
        self.decision_event = threading.Event()
        self.pending_decision: dict = {}
        self.decision_answer = ""
        self.finished_event = threading.Event()
        receipts = _read_json(review_dir / "action_receipts.json")
        self.action_receipts: dict[str, dict] = receipts if isinstance(receipts, dict) else {}


#: Which registry capability each opt-in probe strategy actually turns on.
#: `hdd_inspired` is the always-on default path and has no separate switch.
_STRATEGY_CAPABILITY = {ProbeStrategy.DDMIN.value: "ddmin"}


class ReviewManager:
    def __init__(self, registry: RepoRegistry, *, root: Path,
                 provider: OpenAICompatibleProvider | None = None,
                 agent_level: AgentLevel | str = AgentLevel.L1,
                 execution_mode: ExecutionMode | str = ExecutionMode.TRUSTED_LOCAL):
        self.registry = registry
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.agent_level = AgentLevel(agent_level)
        self.execution_mode = ExecutionMode(execution_mode)
        # Loaded once, and loudly: a malformed registry must stop the server
        # rather than silently leave every capability ungated.
        self.capabilities = caps.load()
        if self.execution_mode not in {ExecutionMode.TRUSTED_LOCAL,
                                       ExecutionMode.SANDBOXED}:
            raise IntakeError("EXECUTION_MODE_INVALID", self.execution_mode.value)
        self._lock = threading.RLock()
        self._live_id: str | None = None
        self._runtimes: dict[str, ReviewRuntime] = {}
        self._idempotency: dict[str, dict] = self._load_idempotency()
        self._capability_authority = CapabilityAuthority()
        self._leases = RepositoryLeaseManager(self.root / "_leases")
        self._recover_interrupted()

    def _load_idempotency(self) -> dict[str, dict]:
        path = self.root / "idempotency.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            # A corrupt dedupe index must not make the server reuse an unknown
            # response.  Start empty; review state itself remains authoritative.
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for key, value in raw.items():
            if (isinstance(key, str) and isinstance(value, dict)
                    and isinstance(value.get("payload_sha256"), str)
                    and isinstance(value.get("response"), dict)):
                out[key] = value
        return out

    def _remember_idempotency(self, key: str, payload: dict, response: dict) -> None:
        if not key:
            return
        self._idempotency[key] = {
            "payload_sha256": _payload_sha256(payload),
            "response": json.loads(json.dumps(response, ensure_ascii=False)),
        }
        _atomic_json(self.root / "idempotency.json", self._idempotency)

    def _idempotent_result(self, key: str, payload: dict) -> dict | None:
        key = _validate_idempotency_key(key)
        if not key:
            return None
        existing = self._idempotency.get(key)
        if existing is None:
            return None
        digest = _payload_sha256(payload)
        if existing.get("payload_sha256") != digest:
            raise IntakeError("IDEMPOTENCY_CONFLICT",
                              "同一 Idempotency-Key 不能复用不同请求")
        return json.loads(json.dumps(existing["response"], ensure_ascii=False))

    @staticmethod
    def _repo_fingerprint(repo: RegisteredRepo) -> str:
        # The canonical path is used only inside this process; persisted lease
        # records contain the one-way fingerprint, never the local path.
        return hashlib.sha256(str(repo.path).encode("utf-8")).hexdigest()

    @staticmethod
    def _transition_store(events: EventStore, state: ReviewStore,
                          status: ReviewStatus, *, reason: str = "",
                          **kwargs):
        """Publish durable intent before changing the materialized state."""
        previous = state.snapshot.status
        intent = events.append("review.state_transition_intent", {
            "from": previous.value, "to": status.value, "reason": reason,
        })
        changed = {
            "intent_event_id": intent.event_id, "from": previous.value,
            "to": status.value, "reason": reason,
        }
        if status in TERMINAL:
            # Terminal state is the public completion marker.  Publish every
            # journal record first so a caller that observes terminal state can
            # safely remove/export the review directory without racing a final
            # event append from the worker.
            events.append("review.state_changed", changed)
            snapshot = state.transition(status, reason=reason, **kwargs)
        else:
            snapshot = state.transition(status, reason=reason, **kwargs)
            events.append("review.state_changed", changed)
        return snapshot

    def _transition(self, rt: ReviewRuntime, status: ReviewStatus, *,
                    reason: str = "", **kwargs):
        return self._transition_store(rt.events, rt.state, status,
                                      reason=reason, **kwargs)

    def _bind_execution_authority(self, rt: ReviewRuntime) -> RegisteredRepo:
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        rt.repo_fingerprint = self._repo_fingerprint(repo)
        limits = DEFAULT_RESOURCE_LIMITS
        review_spec = rt.review_spec
        if review_spec is None and isinstance(request.get("review_spec"), dict):
            try:
                review_spec = ReviewSpec.parse(request["review_spec"])
            except ReviewSpecError:
                review_spec = None
        constraints = review_spec.constraints if review_spec is not None else None
        rt.resource_budget = {
            "max_seconds": float(constraints.budget_seconds if constraints else
                                 request.get("budget_seconds") or 300),
            "max_output_bytes": limits.max_output_bytes,
            "max_memory_bytes": (constraints.max_memory_bytes if constraints else
                                 limits.max_memory_bytes),
            "max_processes": (constraints.max_processes if constraints else
                              limits.max_processes),
            "max_disk_bytes": (constraints.max_disk_bytes if constraints else
                               limits.max_disk_bytes),
            "max_files": limits.max_files,
        }
        plan_hash = (rt.frozen_plan.plan_sha256 if rt.frozen_plan is not None
                     else str(_read_json(rt.review_dir / "plan.frozen.json").get(
                         "plan_sha256") or ""))
        ttl = int(rt.resource_budget["max_seconds"] + 600)
        rt.target_commit = str((request.get("source_snapshot") or {}).get("head") or "")
        data_policy = review_spec.as_dict()["data_policy"] if review_spec is not None else {}
        rt.data_policy_sha256 = hashlib.sha256(json.dumps(
            data_policy, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        allowed_tools = (review_spec.autonomy_policy.allowed_tools
                         if review_spec is not None and
                         review_spec.autonomy_policy.allowed_tools else
                         tuple(item.name for item in CATALOG_V1))
        rt.capability_token = self._capability_authority.issue(
            review_id=rt.review_id, plan_sha256=plan_hash,
            repo_fingerprint=rt.repo_fingerprint,
            allowed_tools=tuple(allowed_tools),
            max_risk=ToolRisk.MUTATES,
            resource_budget=rt.resource_budget, ttl_seconds=ttl,
            target_commit=rt.target_commit,
            data_policy_sha256=rt.data_policy_sha256)
        return repo

    @staticmethod
    def _bounded_facts(result) -> dict:
        """Return structural facts only; never copy raw stdout or source text."""
        if hasattr(result, "anchor_id") and hasattr(result, "status"):
            return {
                "anchor_id": str(result.anchor_id), "status": str(result.status),
                "identical_to_baseline": result.identical_to_baseline,
                "regressed_test_count": len(result.regressed_tests),
                "evidence_id": str(result.evidence_id),
            }
        if not isinstance(result, dict):
            return {"result_type": type(result).__name__}
        facts: dict = {"result_keys": sorted(str(k) for k in result)[:40]}
        for key, value in result.items():
            if isinstance(value, bool) or value is None:
                facts[str(key)] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                facts[str(key)] = value
            elif isinstance(value, (list, tuple, dict, set)):
                facts[f"{key}_count"] = len(value)
        return facts

    @staticmethod
    def _artifact_records(rt: ReviewRuntime, result) -> tuple[dict, ...]:
        if not isinstance(result, dict):
            return ()
        records: list[dict] = []
        for key, value in result.items():
            if not isinstance(value, str):
                continue
            try:
                path = Path(value).resolve(strict=True)
                relative = path.relative_to(rt.review_dir.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            records.append({
                "artifact_id": str(key)[:80], "relative_ref": relative,
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            })
        return tuple(records[:20])

    def _call_tool(self, rt: ReviewRuntime, name: str, args: dict | None = None,
                   *, reason_code: str, reason: str,
                   expected_artifacts: tuple[str, ...] = (),
                   idempotency_key: str = ""):
        """The sole post-approval tool path: action event, dispatch, observation."""
        if not rt.capability_token or not rt.repo_fingerprint:
            raise ProtocolError("EXECUTION_AUTHORITY_MISSING", name)
        catalog = ToolCatalog()
        spec = catalog.get(name)
        plan_hash = (rt.frozen_plan.plan_sha256 if rt.frozen_plan is not None
                     else str(_read_json(rt.review_dir / "plan.frozen.json").get(
                         "plan_sha256") or ""))
        dedupe_key = idempotency_key or hashlib.sha256(json.dumps({
            "review_id": rt.review_id, "plan_sha256": plan_hash,
            "tool": name, "args": dict(args or {})}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        receipt = rt.action_receipts.get(dedupe_key)
        if receipt is not None:
            if receipt.get("status") == "COMPLETED":
                rt.events.append("agent.action.deduplicated", {
                    "idempotency_key_sha256": hashlib.sha256(
                        dedupe_key.encode("utf-8")).hexdigest(),
                    "action_id": receipt.get("action_id")})
                return _decode_action_result(receipt.get("result"))
            raise ProtocolError("ACTION_OUTCOME_UNCERTAIN",
                                "an unfinished action with this idempotency key exists")
        rt.action_seq += 1
        action_id = f"action:{rt.action_seq:06d}"
        action = ActionRequest(
            review_id=rt.review_id, action_id=action_id, tool=name,
            args=dict(args or {}), risk=spec.risk, plan_sha256=plan_hash,
            prerequisite_observation_ids=tuple(rt.observation_ids[-8:]),
            reason_code=reason_code, reason=reason,
            idempotency_key=f"{rt.review_id}:{dedupe_key[:24]}",
            timeout_seconds=float(rt.resource_budget["max_seconds"]),
            resource_budget=dict(rt.resource_budget),
            expected_artifacts=expected_artifacts,
            capability_token=rt.capability_token,
            target_commit=rt.target_commit,
            data_policy_sha256=rt.data_policy_sha256)
        action.validate(catalog)
        rt.action_receipts[dedupe_key] = {
            "schema_version": "action-receipt-v1", "status": "STARTED",
            "action_id": action_id, "tool": name, "plan_sha256": plan_hash,
            "args_sha256": hashlib.sha256(json.dumps(
                dict(args or {}), ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
        }
        _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
        rt.events.append("agent.action.accepted", action.as_dict(redact_capability=True))
        started = time.monotonic()
        observation_id = f"observation:{rt.action_seq:06d}"
        try:
            result = rt.router.execute_controlled(
                action, self._capability_authority,
                repo_fingerprint=rt.repo_fingerprint)
        except Exception as exc:
            code = getattr(exc, "code", getattr(exc, "stage", type(exc).__name__))
            observation = ObservationRecord(
                review_id=rt.review_id, observation_id=observation_id,
                action_id=action_id, status=ObservationStatus.FAILED,
                facts={"exception_type": type(exc).__name__}, artifacts=(),
                resource_usage={
                    "wall_seconds": round(time.monotonic() - started, 6),
                    "limits": dict(rt.resource_budget)},
                executor={"mode": self.execution_mode.value,
                          "control_plane": "trusted"},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                restore_proof={"required": spec.risk.value == "mutates",
                               "verified": False},
                retryable=code in {"TimeoutExpired", "timeout", "budget_exhausted"},
                next_step_constraints=("do_not_retry_without_policy_review",),
                error_code=str(code)[:120])
            rt.events.append("agent.observation.recorded", {
                **observation.as_dict(), "observation_sha256": observation.sha256})
            rt.observation_ids.append(observation_id)
            rt.action_receipts[dedupe_key].update(
                status="FAILED", observation=observation.as_dict(),
                error_code=str(code)[:120])
            _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
            raise
        recent = rt.events.since(max(0, rt.events.last_seq - 6))
        restored = any(event.kind == "restore.verified" for event in recent)
        observation = ObservationRecord(
            review_id=rt.review_id, observation_id=observation_id,
            action_id=action_id, status=ObservationStatus.SUCCEEDED,
            facts=self._bounded_facts(result),
            artifacts=self._artifact_records(rt, result),
            resource_usage={
                "wall_seconds": round(time.monotonic() - started, 6),
                "limits": dict(rt.resource_budget)},
            executor={"mode": self.execution_mode.value,
                      "control_plane": "trusted"},
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            restore_proof={"required": spec.risk.value == "mutates",
                           "verified": restored if spec.risk.value == "mutates" else True},
            retryable=False, next_step_constraints=())
        rt.events.append("agent.observation.recorded", {
            **observation.as_dict(), "observation_sha256": observation.sha256})
        rt.observation_ids.append(observation_id)
        rt.action_receipts[dedupe_key].update(
            status="COMPLETED", observation=observation.as_dict(),
            result=_encode_action_result(result))
        _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
        return result

    def _release_execution_lease(self, rt: ReviewRuntime) -> None:
        if rt.lease is None:
            return
        lease = rt.lease
        try:
            released = self._leases.release(lease)
            rt.events.append("repository.lease_released", {
                "operation": "execute", "lease_id": lease.lease_id,
                "released": released})
        finally:
            rt.lease = None

    def _recover_interrupted(self) -> None:
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "review_state.json").exists():
                continue
            try:
                state = ReviewStore(d, d.name)
                try:
                    events = EventStore(d, d.name)
                    process_results = {
                        "execution": recover_process_record(d / "runtime_process.json"),
                        "repair": recover_process_record(d / "repair" / "runtime_process.json"),
                    }
                    for operation, process_recovery in process_results.items():
                        if process_recovery == "none":
                            continue
                        events.append("executor.process_recovered", {
                            "operation": operation, "result": process_recovery})
                    all_events = events.all()
                    intents = {event.event_id for event in all_events
                               if event.kind == "review.state_transition_intent"}
                    completed = {str(event.data.get("intent_event_id") or "")
                                 for event in all_events
                                 if event.kind == "review.state_changed"}
                    unresolved_intent = bool(intents - completed)
                    temp_artifacts = tuple(d.rglob(".*.tmp"))
                    orphan_worktrees = tuple(
                        path for path in (d / "repair" / "worktrees").glob("shuimu-repair-*")
                        if path.is_dir())
                    _, uncertain_lease = self._leases.reclaim_stale_for_review(d.name)
                    process_uncertain = "uncertain" in process_results.values()
                    receipts = _read_json(d / "action_receipts.json")
                    if not isinstance(receipts, dict):
                        receipts = {}
                    uncertain_actions = [key for key, value in receipts.items()
                                         if isinstance(value, dict) and
                                         value.get("status") == "STARTED"]
                    if (unresolved_intent or temp_artifacts or orphan_worktrees or uncertain_lease
                            or process_uncertain or uncertain_actions):
                        reason = ("recovery_unmatched_transition" if unresolved_intent
                                  else "recovery_temporary_artifact"
                                  if temp_artifacts else "recovery_orphan_worktree"
                                  if orphan_worktrees else "recovery_lease_owner_uncertain"
                                  if uncertain_lease else "recovery_process_uncertain"
                                  if process_uncertain else "recovery_action_outcome_uncertain")
                        events.append("review.recovered", {
                            "previous_state": state.snapshot.status.value,
                            "resolution": "QUARANTINED", "reason": reason,
                            "temporary_artifact_count": len(temp_artifacts),
                            "orphan_worktree_count": len(orphan_worktrees),
                            "uncertain_action_count": len(uncertain_actions),
                        })
                        if state.snapshot.status is not ReviewStatus.QUARANTINED:
                            self._transition_store(events, state,
                                                   ReviewStatus.QUARANTINED,
                                                   reason=reason,
                                                   allow_recovery_terminal=True)
                        continue
                    if state.snapshot.status in TERMINAL:
                        continue
                    events.append("review.recovered", {
                        "previous_state": state.snapshot.status.value,
                        "resolution": "ABORTED",
                    })
                    if state.snapshot.status is not ReviewStatus.RECOVERING:
                        self._transition_store(events, state, ReviewStatus.RECOVERING,
                                               reason="process_restart")
                    self._transition_store(
                        events, state, ReviewStatus.ABORTED,
                        reason="review_aborted:process_restart")
                    for status_file in (d / "artifacts").glob("*/run_status.json"):
                        evidence = RunRoot(status_file.parent, official=False,
                                           unofficial_reason="review_evidence")
                        if evidence.status().get("status") == "INCOMPLETE":
                            evidence.finish(
                                False, reason="review_aborted:process_restart")
                except EventJournalError:
                    state.transition(
                        ReviewStatus.QUARANTINED, reason="event_journal_invalid",
                        allow_recovery_terminal=True)
                    for status_file in (d / "artifacts").glob("*/run_status.json"):
                        evidence = RunRoot(status_file.parent, official=False,
                                           unofficial_reason="review_evidence")
                        if evidence.status().get("status") == "INCOMPLETE":
                            evidence.finish(False, reason="event_journal_invalid")
            except ReviewStateError:
                continue

    def _runtime(self, review_id: str) -> ReviewRuntime:
        if review_id in self._runtimes:
            return self._runtimes[review_id]
        d = self.root / review_id
        if not d.is_dir():
            raise IntakeError("REVIEW_NOT_FOUND", review_id)
        rt = ReviewRuntime(review_id, d)
        self._runtimes[review_id] = rt
        return rt

    @property
    def live_id(self) -> str | None:
        with self._lock:
            return self._live_id

    def create_v2(self, raw: dict, *, idempotency_key: str = "") -> dict:
        """Compile a closed natural-language task into the proven v1 executor."""
        with self._lock:
            cached = self._idempotent_result(idempotency_key, raw)
            if cached is not None:
                return cached
        try:
            spec = ReviewSpec.parse(raw)
        except ReviewSpecError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if spec.language == "typescript":
            raise IntakeError("LANGUAGE_NOT_AVAILABLE",
                              "TypeScript adapters are not enabled in this build")
        repo = self.registry.get(spec.source.repo_id)
        source_snapshot = _repo_snapshot(repo.path)
        if not source_snapshot.get("head") or not source_snapshot.get("status_sha256"):
            raise IntakeError("SOURCE_SNAPSHOT_UNAVAILABLE",
                              "cannot bind the repository identity and workspace snapshot")
        target_ref = spec.source.target_ref or "HEAD"
        try:
            target_commit = run_git(["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
                                    cwd=repo.path, check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntakeError("SOURCE_TARGET_INVALID", target_ref) from exc
        if spec.source.target_commit and spec.source.target_commit != target_commit:
            raise IntakeError("SOURCE_TARGET_STALE",
                              "target_commit does not match the selected target_ref")
        if target_commit != source_snapshot["head"]:
            raise IntakeError("SOURCE_TARGET_REQUIRES_HUMAN",
                              "the current evidence engine requires target_ref to match HEAD")
        base_ref = spec.source.base_ref or "HEAD"
        spec = replace(
            spec,
            source=replace(spec.source, base_ref=base_ref, target_ref=target_ref,
                           target_commit=target_commit,
                           workspace_snapshot_sha256=source_snapshot["snapshot_sha256"]),
            created_at=(datetime.now(timezone.utc).isoformat()
                        if spec.created_at == DEFAULT_CREATED_AT else spec.created_at),
        )
        try:
            memory = load_review_memory(repo.path)
        except ReviewMemoryError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if spec.memory_snapshot_sha256 and spec.memory_snapshot_sha256 != memory.sha256:
            raise IntakeError("MEMORY_SNAPSHOT_STALE",
                              "review spec memory hash does not match the repository")
        if memory.sha256:
            spec = replace(spec, memory_snapshot_sha256=memory.sha256)
        tests = (self.registry.validate_test_files(repo, list(spec.scope.test_files))
                 if spec.scope.test_files else self.registry.discover_python_test_files(repo))
        created = self.create({
            "source": {"kind": "local", "repo_id": spec.source.repo_id},
            "test_files": list(tests), "declared_tests": [], "goal": spec.goal,
            "review_focus": spec.review_focus,
            "budget_seconds": spec.constraints.budget_seconds,
            "model_provider": spec.autonomy_policy.model_provider,
            "ui_mode": ("model_research" if spec.autonomy_policy.model_provider == "live"
                        else "research"),
            "probe_strategy": ProbeStrategy.HDD_INSPIRED.value,
        }, review_spec=spec, idempotency_key=idempotency_key)
        rt = self._runtime(created["review_id"])
        if memory.records:
            rt.request["review_memory"] = memory.active_rules()
            _atomic_json(rt.review_dir / "request.json", rt.request)
            rt.events.append("review_memory.loaded", {
                "memory_snapshot_sha256": memory.sha256,
                "active_records": len(memory.active_rules()),
            })
        response = self.describe(created["review_id"])
        with self._lock:
            self._remember_idempotency(idempotency_key, raw, response)
        return response

    def revise_v2(self, review_id: str, raw: dict) -> dict:
        """Supersede an unapproved v2 draft with a hash-linked new draft.

        A revision never mutates a plan that a user might have copied.  The old
        draft becomes a terminal audit record and the revised task receives a
        fresh review id and a fresh frozen-plan fingerprint.
        """
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_APPROVAL:
            raise IntakeError("PLAN_REVISION_NOT_ALLOWED", rt.state.snapshot.status.value)
        current = rt.review_spec
        if current is None:
            request = rt.request or _read_json(rt.review_dir / "request.json")
            spec_raw = request.get("review_spec") if isinstance(request, dict) else None
            try:
                current = ReviewSpec.parse(spec_raw)
            except ReviewSpecError as exc:
                raise IntakeError("PLAN_REVISION_NOT_ALLOWED", "review has no valid v2 spec") from exc
        if not isinstance(raw, dict) or set(raw) - {
                "instruction", "goal", "scope", "constraints", "autonomy_policy",
                "language", "review_focus", "revision_reason",
                "trigger_observation_id"}:
            raise IntakeError("PLAN_REVISION_INVALID", "unsupported revision fields")
        reason = str(raw.get("revision_reason") or "user_revision")
        trigger = str(raw.get("trigger_observation_id") or "")
        if len(reason) > 300 or len(trigger) > 160:
            raise IntakeError("PLAN_REVISION_INVALID", "revision metadata is too long")
        merged = current.as_dict()
        merged.pop("schema_version", None)
        merged.update({key: value for key, value in raw.items()
                       if key not in {"revision_reason", "trigger_observation_id"}})
        try:
            revised = ReviewSpec.parse(merged)
        except ReviewSpecError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if revised.sha256 == current.sha256:
            raise IntakeError("PLAN_REVISION_NO_CHANGE", "revision does not change the review spec")
        rt.events.append("plan.superseded", {
            "review_spec_sha256": current.sha256,
            "replacement_review_spec_sha256": revised.sha256,
            "changed_fields": sorted(key for key in merged
                                     if merged.get(key) != current.as_dict().get(key)),
            "revision_reason": reason,
            "trigger_observation_id": trigger,
        })
        self._transition(rt, ReviewStatus.ABORTED, reason="plan_superseded")
        with self._lock:
            if self._live_id == review_id:
                self._live_id = None
        created = self.create_v2(revised.as_dict())
        child = self._runtime(created["review_id"])
        child.request["plan_revision"] = {
            "parent_review_id": review_id,
            "parent_review_spec_sha256": current.sha256,
            "revision_reason": reason,
            "trigger_observation_id": trigger,
        }
        _atomic_json(child.review_dir / "request.json", child.request)
        child.events.append("plan.revised", dict(child.request["plan_revision"]))
        return created

    def plan_revisions(self, review_id: str) -> dict:
        """Return the immutable parent chain without exposing local paths."""
        chain: list[dict] = []
        current_id = review_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            rt = self._runtime(current_id)
            request = rt.request or _read_json(rt.review_dir / "request.json")
            chain.append({
                "review_id": current_id,
                "review_spec_sha256": str(request.get("review_spec_sha256") or ""),
                "status": rt.state.snapshot.status.value,
            })
            parent = request.get("plan_revision") or {}
            current_id = str(parent.get("parent_review_id") or "")
        return {"schema_version": "review-plan-revisions-v1", "revisions": list(reversed(chain))}

    def create(self, raw: dict, *, review_spec: ReviewSpec | None = None,
               idempotency_key: str = "") -> dict:
        with self._lock:
            cached = self._idempotent_result(idempotency_key, raw)
            if cached is not None:
                return cached
            if self._live_id:
                raise IntakeError("LIVE_RUN_BUSY", self._live_id)
            if set(raw) - {"source", "test_files", "declared_tests", "goal",
                          "review_focus", "budget_seconds", "model_provider", "probe_strategy",
                          "ui_mode"}:
                raise IntakeError("REQUEST_FIELDS_INVALID", "unsupported request field")
            source = raw.get("source") or {}
            if not isinstance(source, dict) or set(source) != {"kind", "repo_id"}:
                raise IntakeError("SOURCE_INVALID", "local source requires only kind and repo_id")
            if source.get("kind") != "local":
                raise IntakeError("SOURCE_NOT_REGISTERED", "HTTP live reviews currently accept registered local repos only")
            repo = self.registry.get(str(source.get("repo_id") or ""))
            tests = self.registry.validate_test_files(repo, raw.get("test_files") or [])
            declared = raw.get("declared_tests") or []
            if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
                raise IntakeError("DECLARED_TESTS_INVALID", "declared_tests must be strings")
            budget = float(raw.get("budget_seconds") or 300)
            if not (1 <= budget <= 3600):
                raise IntakeError("BUDGET_INVALID", "budget must be between 1 and 3600 seconds")
            goal = str(raw.get("goal") or "审查补丁新增代码的证据边界")[:500]
            review_focus = str(raw.get("review_focus") or "evidence-boundary")
            if review_focus not in REVIEW_FOCUSES:
                raise IntakeError("REVIEW_FOCUS_INVALID", review_focus)
            provider_name = str(raw.get("model_provider") or "deterministic")
            if provider_name not in {"deterministic", "live"}:
                raise IntakeError("PROVIDER_INVALID", provider_name)
            ui_mode = str(raw.get("ui_mode") or (
                "model_research" if provider_name == "live" else "research"))
            if ui_mode not in {"judge", "research", "model_research"}:
                raise IntakeError("UI_MODE_INVALID", ui_mode)
            if ui_mode != "model_research" and provider_name == "live":
                raise IntakeError(
                    "MODEL_MODE_MISMATCH",
                    "评委和普通研究员模式固定使用确定性调度；请切换到 AI 研究员模式",
                )
            if provider_name == "live" and self.provider is None:
                raise IntakeError("MODEL_UNAVAILABLE", "AI 研究员需要已配置且可用的模型服务")
            # ddmin is opt-in and costs extra experiments, so the default stays
            # hdd_inspired and an unknown value is refused rather than coerced.
            strategy = str(raw.get("probe_strategy")
                           or ProbeStrategy.HDD_INSPIRED.value)
            if strategy not in {x.value for x in ProbeStrategy}:
                raise IntakeError("PROBE_STRATEGY_INVALID", strategy)
            # A capability the registry has switched off must not be reachable
            # from the browser. Otherwise "disabled" is a word in a document
            # rather than a property of the product.
            capability = _STRATEGY_CAPABILITY.get(strategy)
            if capability and not caps.runtime_allowed(
                    capability, capabilities=self.capabilities):
                raise IntakeError("CAPABILITY_UNAVAILABLE", capability)
            # The repository scan is deliberately last among pure request
            # validation steps: malformed/unauthorized requests must be
            # rejected by their own stable boundary before repository state is
            # inspected.  It still runs before a review id, event, worktree or
            # repository-controlled process can be created.
            try:
                repository_security = require_repository_safe(repo.path)
            except RepositorySecurityError as exc:
                raise IntakeError(exc.code, exc.detail) from exc

            review_id = uuid.uuid4().hex
            review_dir = self.root / review_id
            rt = ReviewRuntime(review_id, review_dir)
            if self.provider is not None:
                rt.metrics_base = self.provider.metrics.counters()
                rt.trace_start = len(self.provider.private_traces)
            self._runtimes[review_id] = rt
            self._live_id = review_id
            public_request = {
                "schema_version": "review-request-v1", "review_id": review_id,
                "source": source, "test_files": list(tests),
                "declared_tests": declared, "goal": goal,
                "review_focus": review_focus,
                "budget_seconds": budget, "model_provider": provider_name,
                "ui_mode": ui_mode,
                "probe_strategy": strategy,
                "execution_mode": self.execution_mode.value,
                "agent_level": self.agent_level.value,
                "source_snapshot": _repo_snapshot(repo.path),
                "repository_security": repository_security.as_dict(),
            }
            if review_spec is not None:
                public_request["schema_version"] = "review-request-v2"
                public_request["review_spec"] = review_spec.as_dict()
                public_request["review_spec_sha256"] = review_spec.sha256
            rt.request = public_request
            rt.review_spec = review_spec
            _atomic_json(review_dir / "request.json", public_request)
            rt.events.append("review.created", {
                "execution_mode": self.execution_mode.value,
                "execution_mode_label": self.execution_mode.label,
                "repo_id": repo.repo_id,
            })
            if review_spec is not None:
                rt.events.append("review_spec.compiled", {
                    "schema_version": review_spec.schema_version,
                    "review_spec_sha256": review_spec.sha256,
                    "language": review_spec.language,
                    "autonomy": review_spec.autonomy_policy.model_provider,
                    "repair_branch_authorized": review_spec.autonomy_policy.allow_repair_branch,
                })
            self._transition(rt, ReviewStatus.INTAKE_VALIDATED)
            rt.events.append("intake.validated", {"test_files": list(tests)})
            rt.events.append("repository_security.accepted", {
                "schema_version": repository_security.as_dict()["schema_version"],
                "findings": len(repository_security.findings),
                "observations": len(tuple(
                    item for item in repository_security.findings
                    if item.severity == "observe")),
            })

            req = AnalysisRequest(
                repo_path=repo.path, test_files=tests,
                declared_tests=tuple(declared), python=repo.python,
                budget_seconds=budget, mode=self.execution_mode,
                goal=goal, model_provider=provider_name,
                out_root=review_dir / "artifacts", quiet=True)
            try:
                resolved = req.resolve()
                rt.session = AnalysisSession(
                    resolved, quiet=True, three_state=True,
                    out_root=req.out_root, total_budget=budget,
                    probe_strategy=strategy,
                    run_metadata={
                        "execution_mode": req.mode.value,
                        "execution_mode_label": req.mode.label,
                        "input_kind": resolved.kind,
                        "review_id": review_id,
                        "planner_prompt_version": "planner-v1",
                        "stop_policy_version": ("scheduler-policy-v3"
                                                if self.agent_level is AgentLevel.L2
                                                else "stop-policy-v1"),
                    }, event_sink=rt.events.append)
                rt.router = ToolRouter(rt.session, ToolCatalog())
                rt.router.call("inspect_repo")
                rt.router.call("inspect_patch")
                universe = tuple(rt.router.call("build_eligible_universe")["anchor_ids"])
                draft, fallback = self._draft_plan(rt, provider_name, goal, budget,
                                                   universe)
                try:
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget,
                                          review_spec_sha256=(review_spec.sha256
                                                              if review_spec else ""))
                except PolicyError as exc:
                    if self.provider:
                        self.provider.metrics.policy_rejections += 1
                        reasons = self.provider.metrics.policy_rejection_reasons
                        reasons[exc.code] = reasons.get(exc.code, 0) + 1
                    rt.events.append("policy.rejected", {"code": exc.code})
                    draft = deterministic_draft(goal=goal, budget_seconds=budget,
                                                universe=universe)
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget,
                                          review_spec_sha256=(review_spec.sha256
                                                              if review_spec else ""))
                    fallback = f"policy_rejected:{exc.code}"
                rt.frozen_plan = frozen
                _atomic_json(review_dir / "plan.draft.json", draft.as_dict())
                self._transition(rt, ReviewStatus.PLAN_DRAFTED)
                rt.events.append("plan.drafted", {
                    "prompt_version": draft.prompt_version,
                    "fallback_reason": fallback,
                })
                self._transition(rt, ReviewStatus.AWAITING_APPROVAL)
                rt.events.append("plan.awaiting_approval", {
                    "plan_sha256": frozen.plan_sha256,
                    "plan": frozen.as_dict(),
                })
                response = self.describe(review_id)
                self._remember_idempotency(idempotency_key, raw, response)
                return response
            except Exception as exc:
                self._fail(rt, exc)
                raise

    def _draft_plan(self, rt: ReviewRuntime, provider_name: str, goal: str,
                    budget: float, universe: tuple[str, ...]):
        if provider_name != "live" or self.provider is None:
            reason = "provider_unconfigured" if provider_name == "live" else ""
            if reason and self.provider:
                self.provider.metrics.fallbacks += 1
            return deterministic_draft(goal=goal, budget_seconds=budget,
                                       universe=universe), reason
        last = None
        for _ in range(2):
            rt.events.append("model.request.started", {"stage": "plan"})
            try:
                draft = self.provider.draft_plan(
                    goal=goal, budget_seconds=budget, universe=universe)
                rt.events.append("model.request.completed",
                                 {"stage": "plan", "ok": True})
                return draft, ""
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "plan", "ok": False,
                    "error_type": type(exc).__name__})
                last = exc
        self.provider.metrics.fallbacks += 1
        return deterministic_draft(goal=goal, budget_seconds=budget,
                                   universe=universe), f"model_failed:{type(last).__name__}"

    def approve(self, review_id: str, plan_sha256: str, *,
                idempotency_key: str = "") -> dict:
        payload = {"review_id": review_id, "plan_sha256": plan_sha256,
                   "action": "approve"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_APPROVAL:
            raise IntakeError("REVIEW_NOT_APPROVABLE", rt.state.snapshot.status.value)
        if rt.frozen_plan is None:
            raw = json.loads((rt.review_dir / "plan.draft.json").read_text(encoding="utf-8"))
            raise IntakeError("REVIEW_NOT_RESUMABLE", "restart requires a new review")
        if not secrets.compare_digest(plan_sha256, rt.frozen_plan.plan_sha256):
            raise IntakeError("STALE_PLAN", "approved plan hash is not current")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo_for_snapshot = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo_for_snapshot.path)
        if (current_snapshot.get("head") != expected_snapshot.get("head") or
                current_snapshot.get("snapshot_sha256") != expected_snapshot.get("snapshot_sha256")):
            raise IntakeError("STALE_APPROVAL",
                              "HEAD or workspace fingerprint changed after plan drafting")
        repo = self._bind_execution_authority(rt)
        try:
            rt.lease = self._leases.acquire(
                repo_fingerprint=rt.repo_fingerprint, review_id=review_id,
                operation="execute", plan_sha256=plan_sha256,
                ttl_seconds=float(rt.resource_budget["max_seconds"]) + 600)
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        _atomic_json(rt.review_dir / "plan.frozen.json", rt.frozen_plan.as_dict())
        rt.events.append("plan.approved", {
            "plan_sha256": plan_sha256,
            "repo_fingerprint": self._repo_fingerprint(repo),
            "lease_id": rt.lease.lease_id,
            "capability_token_sha256": hashlib.sha256(
                rt.capability_token.encode("utf-8")).hexdigest(),
            "target_commit": current_snapshot.get("head"),
            "workspace_snapshot_sha256": current_snapshot.get("snapshot_sha256"),
            "approval_expires_at_epoch": int(time.time()) + int(
                rt.resource_budget["max_seconds"] + 600),
        })
        try:
            self._transition(rt, ReviewStatus.PLAN_FROZEN)
        except Exception:
            self._leases.release(rt.lease)
            rt.lease = None
            raise
        thread = threading.Thread(target=self._execute, args=(rt,), daemon=True,
                                  name=f"modou-review-{review_id[:8]}")
        rt.thread = thread
        try:
            thread.start()
        except Exception:
            if rt.lease is not None:
                self._leases.release(rt.lease)
                rt.lease = None
            raise
        response = self.describe(review_id)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def cancel(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Request a cooperative cancellation; never kills an arbitrary PID.

        The execution loop observes the event between deterministic actions. If
        a repository subprocess is currently running it is allowed to finish or
        hit its own deadline, after which the run is finalized as ABORTED.
        """
        payload = {"review_id": review_id, "action": "cancel"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        status = rt.state.snapshot.status
        if status in TERMINAL:
            raise IntakeError("REVIEW_NOT_CANCELLABLE", status.value)
        if status in {ReviewStatus.CREATED, ReviewStatus.INTAKE_VALIDATED,
                      ReviewStatus.PLAN_DRAFTED, ReviewStatus.AWAITING_APPROVAL}:
            rt.events.append("review.cancel_requested", {"phase": status.value})
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_cancelled:user")
            with self._lock:
                if self._live_id == review_id:
                    self._live_id = None
            response = self.describe(review_id)
        elif status in {ReviewStatus.PLAN_FROZEN, ReviewStatus.BASELINE_RUNNING,
                        ReviewStatus.EXECUTING, ReviewStatus.VERIFYING_RESTORE,
                        ReviewStatus.SYNTHESIZING, ReviewStatus.CANCELLING,
                        ReviewStatus.REPLANNING, ReviewStatus.AWAITING_HUMAN,
                        ReviewStatus.PROPOSING_REPAIR,
                        ReviewStatus.VERIFYING_REPAIR,
                        ReviewStatus.DELIVERING_BRANCH}:
            rt.cancel_event.set()
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="review_cancelled:user")
            rt.events.append("review.cancel_requested", {"phase": status.value})
            response = self.describe(review_id)
        elif status in {ReviewStatus.RECOVERING, ReviewStatus.CLEANUP_REQUIRED,
                        ReviewStatus.QUARANTINED}:
            response = self.cleanup(review_id)
        else:
            raise IntakeError("REVIEW_NOT_CANCELLABLE", status.value)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def resume(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Create a new review only from a restart-aborted, unchanged snapshot."""
        payload = {"review_id": review_id, "action": "resume"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.ABORTED:
            raise IntakeError("REVIEW_NOT_RESUMABLE", rt.state.snapshot.status.value)
        if rt.state.snapshot.reason != "review_aborted:process_restart":
            raise IntakeError("REVIEW_NOT_RESUMABLE", "only restart-aborted reviews may resume")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        source = request.get("source") or {}
        repo = self.registry.get(str(source.get("repo_id") or ""))
        expected = (request.get("source_snapshot") or {}).get("head")
        current = _repo_snapshot(repo.path)
        snapshot = request.get("source_snapshot") or {}
        if not expected or not snapshot.get("status_sha256"):
            raise IntakeError("SOURCE_SNAPSHOT_MISSING", "缺少可验证的源仓库快照，不能自动恢复")
        if expected and (expected != current.get("head") or
                         snapshot.get("status_sha256") != current.get("status_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED", "仓库 HEAD 已变化，不能自动恢复")
        if request.get("schema_version") == "review-request-v2" and request.get("review_spec"):
            child = self.create_v2(request["review_spec"])
        else:
            fields = {key: request[key] for key in {
                "source", "test_files", "declared_tests", "goal", "review_focus",
                "budget_seconds", "model_provider", "probe_strategy", "ui_mode",
            } if key in request}
            child = self.create(fields)
        child_rt = self._runtime(child["review_id"])
        child_rt.request["resume_of"] = review_id
        _atomic_json(child_rt.review_dir / "request.json", child_rt.request)
        child_rt.events.append("review.resumed", {"parent_review_id": review_id})
        rt.events.append("review.resume_created", {"replacement_review_id": child["review_id"]})
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, child)
        return child

    def cleanup(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Bounded operator cleanup for recovery/quarantine and orphaned temps."""
        payload = {"review_id": review_id, "action": "cleanup"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.thread is not None and rt.thread.is_alive():
            rt.cancel_event.set()
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="manual_cleanup_requested")
            rt.events.append("review.cleanup_deferred", {"reason": "execution_active"})
            response = self.describe(review_id)
            with self._lock:
                self._remember_idempotency(idempotency_key, payload, response)
            return response
        rt.events.append("review.cleanup_requested", {
            "state": rt.state.snapshot.status.value})
        removed_temps = 0
        for path in tuple(rt.review_dir.rglob(".*.tmp")):
            try:
                path.unlink()
                removed_temps += 1
            except OSError as exc:
                if rt.state.snapshot.status is not ReviewStatus.QUARANTINED:
                    self._transition(rt, ReviewStatus.QUARANTINED,
                                     reason="manual_cleanup_failed")
                raise IntakeError("CLEANUP_FAILED", str(exc)[:200]) from exc
        removed_worktrees = 0
        request = rt.request or _read_json(rt.review_dir / "request.json")
        source = request.get("source") or {}
        try:
            repo = self.registry.get(str(source.get("repo_id") or ""))
        except IntakeError:
            repo = None
        for root in tuple((rt.review_dir / "repair" / "worktrees").glob("shuimu-repair-*")):
            worktree = root / "worktree"
            if repo is not None and worktree.exists():
                run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo.path)
            shutil.rmtree(root, ignore_errors=True)
            removed_worktrees += 1
        try:
            removed_leases = self._leases.cleanup_for_review(review_id)
        except LeaseError as exc:
            if rt.state.snapshot.status is not ReviewStatus.QUARANTINED:
                self._transition(rt, ReviewStatus.QUARANTINED, reason=exc.code)
            raise IntakeError(exc.code, exc.detail) from exc
        for status_file in (rt.review_dir / "artifacts").glob("*/run_status.json"):
            evidence = RunRoot(status_file.parent, official=False,
                               unofficial_reason="review_evidence")
            if evidence.status().get("status") == "INCOMPLETE":
                evidence.finish(False, reason="review_aborted:manual_cleanup")
        if rt.state.snapshot.status not in TERMINAL:
            if rt.state.snapshot.status is ReviewStatus.QUARANTINED:
                self._transition(rt, ReviewStatus.CLEANUP_REQUIRED,
                                 reason="manual_cleanup_verified")
            elif rt.state.snapshot.status is not ReviewStatus.CLEANUP_REQUIRED:
                self._transition(rt, ReviewStatus.RECOVERING,
                                 reason="manual_cleanup")
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_aborted:manual_cleanup")
        rt.events.append("review.cleanup_completed", {
            "temporary_artifacts_removed": removed_temps,
            "orphan_worktrees_removed": removed_worktrees,
            "leases_removed": removed_leases,
        })
        with self._lock:
            if self._live_id == review_id:
                self._live_id = None
        response = self.describe(review_id)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def decide(self, review_id: str, decision_id: str, decision: str,
               plan_sha256: str, *, idempotency_key: str = "") -> dict:
        """Answer one controller-created, plan-bound human escalation."""
        payload = {"review_id": review_id, "action": "decide",
                   "decision_id": decision_id, "decision": decision,
                   "plan_sha256": plan_sha256}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        if decision not in {"continue", "stop"}:
            raise IntakeError("DECISION_INVALID", decision)
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_HUMAN:
            raise IntakeError("DECISION_NOT_AWAITING", rt.state.snapshot.status.value)
        pending = rt.pending_decision or _read_json(
            rt.review_dir / "decisions" / "pending.json")
        if (str(pending.get("decision_id") or "") != decision_id
                or not secrets.compare_digest(
                    str(pending.get("plan_sha256") or ""), plan_sha256)):
            raise IntakeError("DECISION_STALE", "decision or plan hash is not current")
        if time.time() >= float(pending.get("deadline_epoch") or 0):
            raise IntakeError("DECISION_EXPIRED", decision_id)
        answer = {**pending, "decision": decision,
                  "answered_at_epoch": time.time(), "status": "ANSWERED"}
        _atomic_json(rt.review_dir / "decisions" / f"{decision_id}.json", answer)
        rt.events.append("decision.answered", {
            "decision_id": decision_id, "decision": decision,
            "plan_sha256": plan_sha256})
        rt.decision_answer = decision
        rt.decision_event.set()
        response = self.describe(review_id)
        response["decision"] = {"decision_id": decision_id,
                                "status": "ANSWERED", "decision": decision}
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def deliver_repair(self, review_id: str, patch: str, *,
                       idempotency_key: str = "") -> dict:
        """Verify and deliver an explicitly supplied patch to a local branch.

        This is deliberately a patch *delivery* boundary, not a model patch
        generator. The caller must opt in to repair delivery in the v2 spec;
        the verifier runs the declared tests in an isolated worktree before
        Git is allowed to create a commit.
        """
        patch_digest = hashlib.sha256(str(patch).encode("utf-8")).hexdigest()
        payload = {"review_id": review_id, "action": "deliver_repair",
                   "patch_sha256": patch_digest}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID", "evidence bundle did not verify")
        spec = rt.review_spec
        if spec is None:
            request = rt.request or _read_json(rt.review_dir / "request.json")
            spec_raw = request.get("review_spec") if isinstance(request, dict) else None
            try:
                spec = ReviewSpec.parse(spec_raw)
            except ReviewSpecError as exc:
                raise IntakeError("REPAIR_NOT_AUTHORIZED", "repair requires a v2 authorization") from exc
        if not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED", "计划未授权创建本地修复分支")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo.path)
        if (expected_snapshot.get("head") != current_snapshot.get("head") or
                expected_snapshot.get("snapshot_sha256") != current_snapshot.get("snapshot_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED", "仓库在审查后发生变化，不能交付旧证据对应的补丁")
        plan = rt.frozen_plan.as_dict() if rt.frozen_plan is not None else _read_json(rt.review_dir / "plan.frozen.json")
        plan_sha256 = str(plan.get("plan_sha256") or "")
        if not plan_sha256:
            raise IntakeError("REPAIR_PLAN_MISSING", "没有可验证的冻结计划")
        manifest_holder: dict[str, dict] = {}
        test_files = tuple(request.get("test_files") or ())
        budget = float(request.get("budget_seconds") or 300)

        def verifier(worktree: Path) -> RepairVerification:
            executor = (SandboxedExecutor(
                            worktree.parent / "sandbox",
                            process_record=rt.review_dir / "repair" / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=rt.review_dir / "repair" / "runtime_process.json"))
            env = sanitized_environment({"PYTHONWARNINGS": "ignore"})
            result = executor.run(
                [repo.python, "-m", "pytest", *test_files, "-q"],
                cwd=worktree, timeout=budget, env=env)
            passed = result.returncode == 0
            return RepairVerification(
                passed=passed, test_scope=test_files,
                summary=("declared tests passed" if passed
                          else f"declared tests failed (exit {result.returncode})"),
            )

        def build_manifest(verification: RepairVerification) -> str:
            manifest = EvidenceManifest(
                review_id=review_id, plan_sha256=plan_sha256,
                source_commit=str(expected_snapshot.get("head") or ""),
                patch_sha256=patch_digest, test_scope=verification.test_scope,
                test_results={"passed": verification.passed,
                              "summary": verification.summary},
                restore_clean=True, generated_test_ids=(),
                tool_commit=_tool_commit(),
            )
            raw = manifest.as_dict()
            problems = verify_manifest(raw, manifest.sha256)
            if problems:
                raise RepairError("REPAIR_MANIFEST_INVALID", "; ".join(problems))
            manifest_holder["manifest"] = raw
            return manifest.sha256

        fingerprint = self._repo_fingerprint(repo)
        try:
            repair_lease = self._leases.acquire(
                repo_fingerprint=fingerprint, review_id=review_id,
                operation="repair", plan_sha256=plan_sha256,
                ttl_seconds=budget + 600)
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        rt.events.append("repository.lease_acquired", {
            "operation": "repair", "repo_fingerprint": fingerprint,
            "lease_id": repair_lease.lease_id})
        try:
            try:
                delivery = deliver_verified_patch(
                    repo=repo.path, review_id=review_id, plan_sha256=plan_sha256,
                    patch=patch, verifier=verifier,
                    allowed_paths=tuple(spec.scope.include),
                    max_files=spec.scope.max_modified_files,
                    max_changed_lines=spec.scope.max_changed_lines,
                    scratch_root=rt.review_dir / "repair" / "worktrees",
                    evidence_manifest_builder=build_manifest)
            except RepairError as exc:
                raise IntakeError(exc.code, exc.detail) from exc
        finally:
            self._leases.release(repair_lease)
            rt.events.append("repository.lease_released", {
                "operation": "repair", "lease_id": repair_lease.lease_id})
        manifest = manifest_holder.get("manifest")
        if not manifest:
            raise IntakeError("REPAIR_MANIFEST_MISSING", "提交前证据清单未生成")
        repair_dir = rt.review_dir / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(repair_dir / "evidence_manifest.json", manifest)
        _atomic_json(repair_dir / "delivery_record.json", delivery.delivery_record or {})
        _atomic_text(repair_dir / "patch.diff", patch)
        rt.events.append("repair.delivered", {
            "branch": delivery.branch, "commit": delivery.commit,
            "evidence_manifest_sha256": delivery.evidence_manifest_sha256,
            "patch_sha256": delivery.patch_sha256,
        })
        bundle_path = rt.review_dir / "review_bundle.json"
        if bundle_path.exists():
            bundle = _read_json(bundle_path)
            bundle["repair"] = {
                "status": "DELIVERED",
                "evidence_manifest_sha256": delivery.evidence_manifest_sha256,
                "delivery_record": delivery.delivery_record or {},
            }
            bundle["events"] = [event.as_dict() for event in rt.events.all()]
            bundle["integrity"]["event_count"] = len(bundle["events"])
            bundle["integrity"]["event_chain_sha256"] = content_sha256(bundle["events"])
            bundle["integrity"]["payload_sha256"] = payload_digest(bundle)
            _atomic_json(bundle_path, bundle)
        response = self.describe(review_id)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def generate_repair(self, review_id: str, raw: dict, *,
                        idempotency_key: str = "") -> dict:
        """Generate a bounded candidate only; application and commit stay separate."""
        if not isinstance(raw, dict) or set(raw) != {"finding_ids", "snippets"}:
            raise IntakeError("REPAIR_CONTEXT_INVALID", "finding_ids and snippets are required")
        if self.provider is None:
            raise IntakeError("MODEL_PROVIDER_UNAVAILABLE", "repair generation requires a configured provider")
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID", "evidence bundle did not verify")
        spec = rt.review_spec
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED", "repair generation was not approved")
        if "selected_snippets" not in spec.data_policy.model_data_categories:
            raise IntakeError("REPAIR_DATA_NOT_AUTHORIZED", "selected_snippets was not approved for model transfer")
        plan = rt.frozen_plan.as_dict() if rt.frozen_plan else _read_json(rt.review_dir / "plan.frozen.json")
        context = RepairContext(
            review_id, str(plan.get("plan_sha256") or ""),
            tuple(raw.get("finding_ids") or ()), tuple(spec.scope.include),
            tuple(raw.get("snippets") or ()), spec.scope.max_modified_files,
            spec.scope.max_changed_lines)
        payload = {"review_id": review_id, "action": "generate_repair",
                   "context_sha256": hashlib.sha256(json.dumps(
                       context.safe_payload(), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        try:
            candidate = generate_candidate(self.provider, context)
        except RepairError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        directory = rt.review_dir / "repair"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_text(directory / "candidate.patch", candidate.patch)
        record = {**candidate.public_dict(), "status": "PROPOSED",
                  "retention_days": 7}
        _atomic_json(directory / "candidate.json", record)
        rt.events.append("repair.candidate_proposed", record)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def export_review(self, review_id: str) -> dict:
        try:
            return review_export_manifest(self._runtime(review_id).review_dir)
        except GovernanceError as exc:
            raise IntakeError(exc.code, exc.detail) from exc

    def delete_review(self, review_id: str, *, idempotency_key: str = "") -> dict:
        payload = {"review_id": review_id, "action": "delete_review"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in TERMINAL:
            raise IntakeError("REVIEW_DELETE_NOT_TERMINAL", rt.state.snapshot.status.value)
        try:
            result = delete_review_data(rt.review_dir)
        except GovernanceError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        self._runtimes.pop(review_id, None)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, result)
        return result

    def repair_status(self, review_id: str) -> dict:
        rt = self._runtime(review_id)
        directory = rt.review_dir / "repair"
        manifest = _read_json(directory / "evidence_manifest.json")
        delivery = _read_json(directory / "delivery_record.json")
        candidate = _read_json(directory / "candidate.json")
        if not manifest and not delivery and not candidate:
            return {"status": "NOT_DELIVERED"}
        return {"status": ("DELIVERED" if delivery else "PROPOSED"
                           if candidate else "INCOMPLETE"),
                "candidate": candidate, "evidence_manifest": manifest,
                "delivery_record": delivery}

    def _execute(self, rt: ReviewRuntime) -> None:
        failure: Exception | None = None
        try:
            executor = (SandboxedExecutor(
                            rt.session.run_dir,
                            process_record=rt.review_dir / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=rt.review_dir / "runtime_process.json"))
            rt.events.append("executor.bound", {
                "mode": executor.mode,
                "sandboxed_children": executor.mode == "sandboxed",
                "control_plane": "trusted",
            })
            with execution_scope(executor):
                self._execute_bound(rt)
        except Exception as exc:
            failure = exc
        finally:
            self._prepare_terminal(rt)
            if failure is not None:
                self._fail(rt, failure)
            rt.finished_event.set()

    def _prepare_terminal(self, rt: ReviewRuntime) -> None:
        """Finish cleanup before a terminal state becomes publicly visible."""
        if rt.session is not None:
            rt.session.cleanup()
            rt.session = None
        if rt.lease is not None:
            try:
                self._release_execution_lease(rt)
            except LeaseError:
                pass
        if rt.capability_token:
            self._capability_authority.revoke(rt.capability_token)
        with self._lock:
            if self._live_id == rt.review_id:
                self._live_id = None

    def _execute_bound(self, rt: ReviewRuntime) -> None:
        """一次 Review 的可信控制面；仓库子进程由当前 Executor 隔离。"""
        if rt.cancel_event.is_set():
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="review_cancelled:user")
            rt.events.append("review.cancellation_observed", {"phase": "pre_execution"})
            self._prepare_terminal(rt)
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_cancelled:user")
            return
        self._transition(rt, ReviewStatus.BASELINE_RUNNING,
                         evidence_run_id=rt.session.slug,
                         evidence_started=True)
        self._call_tool(
            rt, "collect_test_scope", {"paths": list(rt.session.test_files)},
            reason_code="freeze_test_scope",
            reason="Normalize the approved test scope before execution")
        baseline = self._call_tool(
            rt, "run_baseline", reason_code="establish_baseline",
            reason="Establish the frozen comparison baseline",
            expected_artifacts=("baseline_result",))
        cancelled = self._cancel_requested(rt)
        # Coverage ranking needs the baseline's coverage, which does not exist
        # when the plan is frozen — so the deterministic order is applied here,
        # after the baseline and before the first probe. The frozen universe is
        # untouched: the candidate set is identical, only the visiting order
        # differs, and the loop probes remaining[0], so ranking here is what
        # makes the product match the measured coverage_first baseline.
        summaries = rt.session.candidate_summaries()
        priorities = tuple(rt.frozen_plan.plan.priorities)
        ordered, applied = coverage_first_order(
            priorities, summaries, str(rt.request.get("review_focus") or "evidence-boundary"))
        rt.request["deterministic_scheduler"] = applied
        rt.events.append("scheduler.deterministic_order", {
                "strategy": applied, "frozen_order": list(priorities),
                "applied_order": list(ordered),
        })
        state = AgentState(
            goal=rt.request["goal"], budget_seconds=rt.request["budget_seconds"],
            frozen_universe=priorities,
            remaining=ordered,
            baseline_green=bool(baseline["all_passed"]),
            max_steps=max(1, len(priorities)),
            agent_level=self.agent_level,
            candidates=summaries)
        if not cancelled:
            self._transition(rt, ReviewStatus.EXECUTING)
        stop_reason = StopReason.NO_ELIGIBLE_LEFT
        while state.remaining and not cancelled:
            cancelled = self._cancel_requested(rt)
            if cancelled:
                break
            if state.exhausted():
                stop_reason = (StopReason.MAX_STEPS if state.step >= state.max_steps
                               else StopReason.BUDGET_EXHAUSTED)
                rt.events.append("policy.forced_stop", {"stop_reason": stop_reason.value})
                break
            anchor_id = state.remaining[0]
            rt.events.append("probe.started", {"anchor_id": anchor_id})
            self._transition(rt, ReviewStatus.VERIFYING_RESTORE)
            obs = self._call_tool(
                rt, "counterfactual_probe", {"anchor_id": anchor_id},
                reason_code="collect_counterfactual_evidence",
                reason="Run one approved counterfactual and verify restoration",
                expected_artifacts=("counterfactual_evidence", "restore_proof"))
            cancelled = self._cancel_requested(rt)
            if not cancelled:
                self._transition(rt, ReviewStatus.EXECUTING)
            state = state.advance(obs)
            rt.events.append("observation.recorded", {
                    "observation_id": obs.observation_id,
                    "policy_branch": obs.policy_branch,
                    "anchor_id": obs.anchor_id, "status": obs.status,
                    "identical_to_baseline": obs.identical_to_baseline,
                    "regressed_tests": list(obs.regressed_tests),
                    "evidence_id": obs.evidence_id,
            })
            if cancelled:
                stop_reason = StopReason.HUMAN_REQUESTED
                break
            if not state.remaining:
                stop_reason = StopReason.NO_ELIGIBLE_LEFT
                break
            action, fallback = self._next_action(rt, state)
            original_order = state.remaining
            requested_order = tuple(action.order)
            try:
                if action.kind is ActionKind.REPRIORITIZE:
                    state = state.reorder(action.order, obs.observation_id)
                else:
                    state = state.mark_decision(obs.observation_id)
            except ReprioritizationRejected as exc:
                if self.provider:
                    self.provider.metrics.policy_rejections += 1
                    self.provider.metrics.blocked_requests += 1
                    self.provider.metrics.fallbacks += 1
                    reasons = self.provider.metrics.policy_rejection_reasons
                    reasons[exc.code] = reasons.get(exc.code, 0) + 1
                rt.events.append("policy.rejected", {
                        "stage": "scheduler", "code": exc.code,
                        "observation_id": obs.observation_id,
                        "requested_order": list(requested_order),
                })
                fallback = f"policy_rejected:{exc.code}"
                action = _continue_action()
                state = state.mark_decision(obs.observation_id)
            rt.events.append("model.action", {
                    "observation_id": obs.observation_id,
                    "observation_branch": obs.policy_branch,
                    "kind": action.kind.value,
                    "stop_reason": action.stop_reason.value if action.stop_reason else None,
                    "reason": action.reason, "fallback": fallback,
                    "source": ("live_model" if not fallback
                               else ("deterministic"
                                     if fallback == "deterministic"
                                     else "deterministic_fallback")),
                    "requested_order": list(requested_order),
                    "original_order": list(original_order),
                    "actual_order": list(state.remaining),
                    # 决策上下文：目标、预算与最近观测随动作一并入档，
                    # 让"模型看到了什么再做的决定"可回放、可审计。
                    "goal": state.goal,
                    "budget_seconds": state.budget_seconds,
                    "spent_seconds": round(state.spent_seconds, 3),
                    "budget_left_seconds": round(state.budget_left(), 3),
                    "last_observation": obs.__dict__,
                    "remaining_candidates": [c.__dict__ for c in state.candidates
                                             if c.anchor_id in state.remaining],
                    "prompt_version": ("scheduler-policy-v3"
                                       if self.agent_level is AgentLevel.L2
                                       else "stop-policy-v1"),
            })
            if action.kind is ActionKind.ASK_HUMAN:
                human = self._await_human(
                    rt, action.reason, state.budget_left())
                if human != "continue":
                    stop_reason = StopReason.HUMAN_REQUESTED
                    break
            if action.kind is ActionKind.STOP:
                stop_reason = action.stop_reason or StopReason.GOAL_SATISFIED
                break
            rt.events.append("scheduler.next", {
                    "actual_next_anchor": state.remaining[0],
                    "previous_next_anchor": original_order[0],
                    "selection": ("model_reprioritized"
                                  if action.kind is ActionKind.REPRIORITIZE
                                  else f"frozen_{applied}"),
                    "priority_reason": action.reason,
            })
        cancelled = self._cancel_requested(rt) or cancelled
        if cancelled:
            stop_reason = StopReason.HUMAN_REQUESTED
        self._transition(rt, ReviewStatus.SYNTHESIZING)
        result = self._call_tool(
            rt, "finish_run", {"stop_reason": stop_reason.value},
            reason_code="seal_evidence",
            reason="Seal the approved run and publish its evidence artifacts",
            expected_artifacts=("report", "bundle", "run_status"))
        valid = not verify_bundle(Path(result["report"]).parent)
        narration = self._narrate(rt, Path(result["report"]).parent)
        # 建议挂在 narration 内部（ReviewBundle v2 不加破坏性顶层字段）：
        # 只读、不执行、不进三态结论，失败也不影响审查完成。
        narration["recommendations"] = self._recommend(
            rt, Path(result["report"]).parent, stop_reason.value)
        final = (ReviewStatus.ABORTED if cancelled else
                 ReviewStatus.PARTIAL
                 if result["summary"].get("analysis_completion") == "partial"
                 else ReviewStatus.COMPLETE)
        if cancelled:
            rt.events.append("review.cancelled", {"reason": "user"})
        rt.events.append("review.completed", {
                "review_status": final.value,
                "evidence_status": "COMPLETE" if valid else "FAILED",
                "stop_reason": stop_reason.value,
        })
        evidence_dir = Path(result["report"]).parent
        all_events = [e.as_dict() for e in rt.events.all()]
        provider = (self.provider.info.as_dict() if self.provider else
                    {"kind": "deterministic"})
        review_bundle = build_review_bundle_v2(
            review_id=rt.review_id,
            request=rt.request,
            plan=rt.frozen_plan.as_dict(),
            events=all_events,
            scheduler_trace=[
                e for e in all_events
                if e["kind"] in {"observation.recorded", "model.action",
                                 "policy.rejected", "scheduler.next"}
            ],
            evidence_run_id=rt.session.slug,
            evidence_bundle={
                "run_id": rt.session.slug,
                "report": _read_json(evidence_dir / "report.json"),
                "ledger": ledger_store.read(evidence_dir / ledger_store.FILENAME),
                "manifest": _read_json(evidence_dir / "bundle.json"),
                "run_status": _read_json(evidence_dir / "run_status.json"),
            },
            narration=narration,
            provider=provider,
            model_metrics=(self.provider.metrics.delta_since(rt.metrics_base)
                           if self.provider else {}),
            evidence_valid=valid,
        )
        _atomic_json(rt.review_dir / "review_bundle.json", review_bundle)
        traces = (self.provider.private_traces[rt.trace_start:]
                  if self.provider else [])
        if traces:
            _atomic_json(rt.review_dir / "private" / "model_trace.json", {
                "schema_version": "private-model-trace-v1",
                "traces": traces,
            })
        self._prepare_terminal(rt)
        self._transition(rt, final,
                         reason=("review_cancelled:user" if cancelled else ""),
                         valid_bundle=valid)

    def _cancel_requested(self, rt: ReviewRuntime) -> bool:
        if not rt.cancel_event.is_set():
            return False
        if rt.state.snapshot.status not in {ReviewStatus.CANCELLING,
                                            *TERMINAL}:
            self._transition(rt, ReviewStatus.CANCELLING,
                             reason="review_cancelled:user")
        rt.events.append("review.cancellation_observed", {})
        return True

    def _await_human(self, rt: ReviewRuntime, reason: str,
                     budget_left_seconds: float) -> str:
        decision_id = uuid.uuid4().hex
        plan_hash = rt.frozen_plan.plan_sha256
        wait_seconds = max(1.0, min(300.0, float(budget_left_seconds)))
        pending = {
            "schema_version": "human-decision-v1", "decision_id": decision_id,
            "review_id": rt.review_id, "plan_sha256": plan_hash,
            "question": (reason or "Continue this approved review?")[:300],
            "allowed_decisions": ["continue", "stop"],
            "default_decision": "stop", "status": "PENDING",
            "deadline_epoch": time.time() + wait_seconds,
        }
        rt.pending_decision = pending
        rt.decision_answer = ""
        rt.decision_event.clear()
        _atomic_json(rt.review_dir / "decisions" / "pending.json", pending)
        rt.events.append("decision.requested", {
            "decision_id": decision_id, "plan_sha256": plan_hash,
            "deadline_epoch": pending["deadline_epoch"],
            "default_decision": "stop"})
        self._transition(rt, ReviewStatus.AWAITING_HUMAN,
                         reason="human_decision_required")
        while time.time() < pending["deadline_epoch"]:
            if rt.cancel_event.is_set():
                return "stop"
            if rt.decision_event.wait(timeout=.2):
                answer = rt.decision_answer
                if answer == "continue":
                    self._transition(rt, ReviewStatus.EXECUTING,
                                     reason="human_decision:continue")
                    return answer
                return "stop"
        rt.events.append("decision.defaulted", {
            "decision_id": decision_id, "decision": "stop",
            "reason": "deadline_expired"})
        return "stop"

    def _next_action(self, rt: ReviewRuntime, state: AgentState):
        if rt.request["model_provider"] == "live" and self.provider is not None:
            rt.events.append("model.request.started", {"stage": "scheduling"})
            try:
                action = self.provider.next_action(state, ToolCatalog())
                rt.events.append("model.request.completed",
                                 {"stage": "scheduling", "ok": True})
                return action, ""
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "scheduling", "ok": False,
                    "error_type": type(exc).__name__})
                self.provider.metrics.fallbacks += 1
                rt.events.append("model.fallback", {
                    "stage": "next_action", "reason": type(exc).__name__,
                    "action": "continue",
                })
                return _continue_action(), type(exc).__name__
        return _continue_action(), "deterministic"

    def _narrate(self, rt: ReviewRuntime, bundle_dir: Path) -> dict:
        rows = ledger_store.read(bundle_dir / ledger_store.FILENAME)
        claims = []
        evidence_ids = {r["record_id"] for r in rows}
        for row in rows:
            if row["record_type"] != ledger_records.CLAIM:
                continue
            p = row["payload"]
            claims.append({
                "record_id": row["record_id"], "claim_type": p["kind"],
                "anchor": p["anchor"], "provenance": p["provenance"],
            })
        claim_ids = [c["record_id"] for c in claims]
        layout = deterministic_layout(claim_ids)
        layout_fallback = False
        if (rt.request.get("model_provider") == "live" and self.provider is not None
                and claim_ids):
            rt.events.append("model.request.started", {"stage": "narration"})
            try:
                layout = self.provider.draft_narration(claim_ids=tuple(claim_ids))
                rt.events.append("model.request.completed",
                                 {"stage": "narration", "ok": True})
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "narration", "ok": False,
                    "error_type": type(exc).__name__})
                self.provider.metrics.fallbacks += 1
                layout_fallback = True
                rt.events.append("model.fallback", {
                    "stage": "narrator", "reason": type(exc).__name__,
                    "action": "deterministic_template",
                })
        try:
            narration = compile_narration(
                claims=claims, evidence_ids=evidence_ids, layout=layout)
            rt.events.append("narrator.compiled", {
                "template_version": narration["template_version"],
                "fact_blocks": len(narration["blocks"]),
                "fallback": layout_fallback,
            })
            return narration
        except NarrationRejected as exc:
            rt.events.append("narrator.fallback", {"reason": str(exc)[:100]})
            return {"template_version": "evidence-narrator-v1",
                    "scope_note": "结论仅适用于本次声明测试范围。", "blocks": []}

    def _recommend(self, rt: ReviewRuntime, bundle_dir: Path,
                   stop_reason: str) -> dict:
        """调度结束后的证据约束型只读建议；仅 live 运行自动追加一次。

        输入封顶 80 行 / 12KB，代码行包在不可信边界内。校验失败重试一次，
        仍失败则整体降级为 failure_reason——建议从不阻塞审查完成，也从不
        进入三态结论。
        """
        base = {
            "schema_version": RECOMMENDATION_PROMPT_VERSION,
            "generated": False,
            "items": [],
            "model": (self.provider.info.model_id if self.provider else ""),
            "prompt_version": RECOMMENDATION_PROMPT_VERSION,
        }
        if (rt.request.get("model_provider") != "live"
                or self.provider is None):
            return {**base, "skipped_reason": "deterministic_run"}
        report = _read_json(bundle_dir / "report.json")
        lines = ((report.get("render_model") or {}).get("lines") or [])
        rows = ledger_store.read(bundle_dir / ledger_store.FILENAME)
        built = build_input(goal=str(rt.request.get("goal") or ""),
                            stop_reason=stop_reason, lines=lines,
                            evidence_rows=rows)
        if not built["allowed"]["evidence_ids"] or not built["allowed"]["line_refs"]:
            return {**base, "skipped_reason": "no_relevant_evidence"}
        last_error = ""
        last_code = ""
        prompt = built["prompt"]
        for attempt in range(2):
            rt.events.append("model.request.started",
                             {"stage": "recommendation",
                              "attempt": attempt + 1})
            try:
                raw = self.provider.recommendation_request(prompt)
                items = parse_recommendations(raw, allowed=built["allowed"])
            except RecommendationRejected as exc:
                rt.events.append("model.request.completed", {
                    "stage": "recommendation", "ok": False,
                    "error_type": type(exc).__name__, "error_code": exc.code})
                self.provider.metrics.schema_rejections += 1
                last_error = failure_label(exc.code)
                last_code = exc.code
                # 重试请求必须携带脱敏错误码与定向指引，否则第二次
                # 与第一次完全相同，模型几乎必然重复同一格式错误。
                prompt = {**built["prompt"],
                          "previous_attempt": retry_hint(exc)}
                continue
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "recommendation", "ok": False,
                    "error_type": type(exc).__name__})
                last_error = type(exc).__name__
                last_code = type(exc).__name__
                continue
            rt.events.append("model.request.completed",
                             {"stage": "recommendation", "ok": True})
            rt.events.append("recommendation.generated", {
                "count": len(items),
                "prompt_version": RECOMMENDATION_PROMPT_VERSION,
                "model": self.provider.info.model_id,
            })
            return {**base, "generated": True,
                    "items": [item.as_dict() for item in items],
                    **({} if items else
                       {"skipped_reason": "insufficient_evidence"})}
        rt.events.append("recommendation.failed", {"reason": last_error,
                                                   "code": last_code})
        return {**base, "failure_reason": last_error, "failure_code": last_code}

    def _fail(self, rt: ReviewRuntime, exc: Exception) -> None:
        reason = (f"{exc.stage}:{exc.detail}" if isinstance(exc, SessionError)
                  else f"{type(exc).__name__}:{str(exc)[:200]}")
        try:
            rt.events.append("review.failed", {"reason": reason})
        except Exception:
            pass
        try:
            if rt.state.snapshot.status not in TERMINAL:
                self._transition(rt, ReviewStatus.FAILED, reason=reason,
                                 allow_recovery_terminal=True)
        except Exception:
            pass
        with self._lock:
            if self._live_id == rt.review_id:
                self._live_id = None

    def describe(self, review_id: str) -> dict:
        rt = self._runtime(review_id)
        plan = None
        plan_path = rt.review_dir / "plan.frozen.json"
        draft_path = rt.review_dir / "plan.draft.json"
        if rt.frozen_plan is not None:
            plan = rt.frozen_plan.as_dict()
        elif plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        elif draft_path.exists():
            plan = json.loads(draft_path.read_text(encoding="utf-8"))
        return {"review_id": review_id, "state": rt.state.snapshot.as_dict(),
                "request": rt.request or _read_json(rt.review_dir / "request.json"),
                "plan": plan, "repair": self.repair_status(review_id),
                "last_seq": rt.events.last_seq}

    def events(self, review_id: str) -> EventStore:
        return self._runtime(review_id).events

    def evidence(self, review_id: str, evidence_id: str) -> dict:
        rt = self._runtime(review_id)
        if rt.session is not None:
            try:
                return rt.session.read_evidence(evidence_id)
            except SessionError:
                pass
        artifact = _find_evidence_bundle(rt.review_dir)
        for row in ledger_store.read(artifact / ledger_store.FILENAME):
            if row.get("record_id") == evidence_id:
                return row
        raise IntakeError("EVIDENCE_NOT_FOUND", evidence_id)

    def review_bundle_path(self, review_id: str) -> Path:
        path = self._runtime(review_id).review_dir / "review_bundle.json"
        if not path.exists():
            raise IntakeError("BUNDLE_NOT_READY", review_id)
        return path

    def model_transcript(self, review_id: str) -> dict:
        """Return the complete credential-free model conversation for local UI."""
        rt = self._runtime(review_id)
        path = rt.review_dir / "private" / "model_trace.json"
        if not path.exists():
            return {"schema_version": "local-model-transcript-v1", "calls": []}
        raw = _read_json(path)
        calls = []
        for index, trace in enumerate(raw.get("traces") or [], start=1):
            calls.append({
                "call": index,
                "stage": str(trace.get("stage") or "unknown"),
                "messages": trace.get("messages") or [],
                "raw_response": str(trace.get("raw_response") or ""),
                "http_status": int(trace.get("http_status") or 0),
                "latency_ms": trace.get("latency_ms"),
                "error_type": str(trace.get("error_type") or ""),
                "request_sha256": str(trace.get("request_sha256") or ""),
                "response_sha256": str(trace.get("response_sha256") or ""),
            })
        return {"schema_version": "local-model-transcript-v1", "calls": calls}

    def provider_public(self) -> dict:
        return {
            "configured": self.provider is not None,
            "display_name": "OpenAI-compatible" if self.provider else "Deterministic fallback",
            "model_id": self.provider.info.model_id if self.provider else "",
            "live_available": self.provider is not None,
            "agent_level": self.agent_level.value,
            "execution_mode": self.execution_mode.value,
            "prompt_versions": ["planner-v1",
                                ("scheduler-policy-v3"
                                 if self.agent_level is AgentLevel.L2
                                 else "stop-policy-v1"), "narrator-v1",
                                RECOMMENDATION_PROMPT_VERSION],
        }


def _continue_action():
    from modou.agent.models import AgentAction
    return AgentAction.parse({"kind": "continue", "reason": "继续冻结顺序"}, ToolCatalog())


def _encode_action_result(result) -> dict:
    if isinstance(result, AgentObservation):
        return {"kind": "agent-observation", "value": {
            "anchor_id": result.anchor_id, "status": result.status,
            "identical_to_baseline": result.identical_to_baseline,
            "regressed_tests": list(result.regressed_tests), "cost_s": result.cost_s,
            "evidence_id": result.evidence_id,
            "observation_id": result.observation_id}}
    if isinstance(result, dict):
        return {"kind": "json-object", "value": _json_safe(result)}
    raise ProtocolError("ACTION_RESULT_NOT_PERSISTABLE", type(result).__name__)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "to_json"):
        return _json_safe(value.to_json())
    raise ProtocolError("ACTION_RESULT_NOT_PERSISTABLE", type(value).__name__)


def _decode_action_result(raw):
    if not isinstance(raw, dict):
        raise ProtocolError("ACTION_RECEIPT_INVALID", "missing persisted result")
    if raw.get("kind") == "json-object" and isinstance(raw.get("value"), dict):
        return json.loads(json.dumps(raw["value"], ensure_ascii=False))
    if raw.get("kind") == "agent-observation" and isinstance(raw.get("value"), dict):
        value = raw["value"]
        return AgentObservation(
            anchor_id=str(value.get("anchor_id") or ""),
            status=str(value.get("status") or ""),
            identical_to_baseline=value.get("identical_to_baseline"),
            regressed_tests=tuple(value.get("regressed_tests") or ()),
            cost_s=float(value.get("cost_s") or 0),
            evidence_id=str(value.get("evidence_id") or ""),
            observation_id=str(value.get("observation_id") or ""))
    raise ProtocolError("ACTION_RECEIPT_INVALID", "unsupported persisted result")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _validate_idempotency_key(value: str) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    if len(value) > 128 or any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in value):
        raise IntakeError("IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 必须是 128 字符以内的可打印字符串")
    return value


def _payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repo_snapshot(repo: Path) -> dict:
    """Capture the source identity needed for a safe resume.

    `git status --porcelain` alone is not enough: editing an already-modified
    file leaves the status line byte-identical, so a snapshot built only from
    it cannot tell "the review target I planned against" from "someone edited
    that file again since". Both the approval gate and repair delivery rely on
    this digest to refuse stale work, so the working-tree *content* has to be
    part of it: the tracked diff against HEAD, plus the bytes of every
    untracked file the status lists.
    """
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=repo, check=True,
                       timeout=10).stdout.strip()
        status = run_git(["status", "--porcelain=v1", "-uall"], cwd=repo,
                         check=True, timeout=10).stdout
        tracked_diff = run_git(["diff", "--binary", "HEAD"], cwd=repo,
                               check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {"head": "", "dirty": True, "status_sha256": "",
                "content_sha256": "", "snapshot_sha256": ""}
    status_sha256 = hashlib.sha256(status.encode("utf-8")).hexdigest()
    content = hashlib.sha256()
    content.update(tracked_diff.encode("utf-8", errors="surrogateescape"))
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        # `-uall` lists untracked files individually, so each entry is a real
        # path; quoted names come back from core.quotepath and are hashed as
        # written rather than unquoted, which is fine — the digest only has to
        # change when the tree changes.
        relative = line[3:].strip().strip('"')
        try:
            blob = (repo / relative).read_bytes()
        except OSError:
            blob = b"<unreadable>"
        content.update(relative.encode("utf-8"))
        content.update(hashlib.sha256(blob).digest())
    content_sha256 = content.hexdigest()
    snapshot_sha256 = hashlib.sha256(json.dumps(
        {"head": head, "status_sha256": status_sha256,
         "content_sha256": content_sha256}, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"head": head, "dirty": bool(status), "status_sha256": status_sha256,
            "content_sha256": content_sha256, "snapshot_sha256": snapshot_sha256}


def _tool_commit() -> str:
    """Return this checkout's commit when available, without failing delivery."""
    try:
        root = Path(__file__).resolve().parents[2]
        return run_git(["rev-parse", "HEAD"], cwd=root, check=True,
                       timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _find_evidence_bundle(review_dir: Path) -> Path:
    candidates = list((review_dir / "artifacts").glob("*/report.json"))
    if not candidates:
        raise IntakeError("BUNDLE_NOT_READY", review_dir.name)
    return candidates[0].parent
