"""Review orchestration and server-side repository capability registry."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from modou.application import AnalysisRequest, ExecutionMode
from modou.executor import (SandboxedExecutor, TrustedLocalExecutor,
                            execution_scope)
from modou.agent.events import EventJournalError, EventStore
from modou.agent.models import (ActionKind, AgentLevel, AgentState, CATALOG_V1,
                                ReprioritizationRejected, StopReason, ToolCatalog)
from modou.agent.narrator import (NarrationLayout, NarrationRejected,
                                  compile_narration, deterministic_layout)
from modou.agent.policy import (PolicyError, compile_plan, deterministic_draft)
from modou.agent.provider import (OpenAICompatibleProvider, ProviderUnavailable)
from modou.agent.review import (ReviewStateError, ReviewStatus, ReviewStore,
                                TERMINAL)
from modou.agent.session import AnalysisSession, SessionError, ToolRouter
from modou.ledger import records as ledger_records, store as ledger_store
from modou.runroot import RunRoot, verify_bundle
from modou import capabilities as caps
from modou.ddmin import ProbeStrategy
from modou.agent.provider import coverage_first_order
from modou.review_bundle import build_review_bundle_v2


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
            try:
                check = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"], cwd=path,
                    capture_output=True, text=True, timeout=10)
            except OSError as exc:
                raise IntakeError("REPO_INVALID", f"cannot inspect {path}: {exc}") from exc
            if check.returncode != 0 or Path(check.stdout.strip()).resolve() != path:
                raise IntakeError("REPO_INVALID", f"not a Git repository root: {path}")
            repo_id = secrets.token_urlsafe(18)
            py = python_by_repo.get(path, Path(sys.executable))
            if not py.is_file():
                raise IntakeError("PYTHON_INVALID", f"interpreter not found for {path.name}: {py}")
            self._repos[repo_id] = RegisteredRepo(repo_id, path, path.name, str(py))
            seen.add(path)
        if not self._repos:
            raise IntakeError("NO_ALLOWED_REPOS", "at least one --allow-repo is required")
        self._presets = self._compile_presets(presets or [])

    def public(self) -> list[dict]:
        return [{"repo_id": r.repo_id, "display_name": r.display_name}
                for r in self._repos.values()]

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


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
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
        self._recover_interrupted()

    def _recover_interrupted(self) -> None:
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "review_state.json").exists():
                continue
            try:
                state = ReviewStore(d, d.name)
                if state.snapshot.status in TERMINAL:
                    continue
                try:
                    events = EventStore(d, d.name)
                    events.append("review.recovered", {
                        "previous_state": state.snapshot.status.value,
                        "resolution": "ABORTED",
                    })
                    state.transition(
                        ReviewStatus.ABORTED,
                        reason="review_aborted:process_restart",
                        allow_recovery_terminal=True)
                    for status_file in (d / "artifacts").glob("*/run_status.json"):
                        evidence = RunRoot(status_file.parent, official=False,
                                           unofficial_reason="review_evidence")
                        if evidence.status().get("status") == "INCOMPLETE":
                            evidence.finish(
                                False, reason="review_aborted:process_restart")
                except EventJournalError:
                    state.transition(
                        ReviewStatus.FAILED, reason="event_journal_invalid",
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

    def create(self, raw: dict) -> dict:
        with self._lock:
            if self._live_id:
                raise IntakeError("LIVE_RUN_BUSY", self._live_id)
            if set(raw) - {"source", "test_files", "declared_tests", "goal",
                          "budget_seconds", "model_provider", "probe_strategy"}:
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
            provider_name = str(raw.get("model_provider") or "deterministic")
            if provider_name not in {"deterministic", "live"}:
                raise IntakeError("PROVIDER_INVALID", provider_name)
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

            review_id = uuid.uuid4().hex
            review_dir = self.root / review_id
            rt = ReviewRuntime(review_id, review_dir)
            self._runtimes[review_id] = rt
            self._live_id = review_id
            public_request = {
                "schema_version": "review-request-v1", "review_id": review_id,
                "source": source, "test_files": list(tests),
                "declared_tests": declared, "goal": goal,
                "budget_seconds": budget, "model_provider": provider_name,
                "probe_strategy": strategy,
                "execution_mode": self.execution_mode.value,
                "agent_level": self.agent_level.value,
            }
            rt.request = public_request
            _atomic_json(review_dir / "request.json", public_request)
            rt.events.append("review.created", {
                "execution_mode": self.execution_mode.value,
                "execution_mode_label": self.execution_mode.label,
                "repo_id": repo.repo_id,
            })
            rt.state.transition(ReviewStatus.INTAKE_VALIDATED)
            rt.events.append("intake.validated", {"test_files": list(tests)})

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
                        "stop_policy_version": ("scheduler-policy-v2"
                                                if self.agent_level is AgentLevel.L2
                                                else "stop-policy-v1"),
                    }, event_sink=rt.events.append)
                rt.router = ToolRouter(rt.session, ToolCatalog())
                rt.router.call("inspect_repo")
                rt.router.call("inspect_patch")
                universe = tuple(rt.router.call("build_eligible_universe")["anchor_ids"])
                draft, fallback = self._draft_plan(provider_name, goal, budget, universe)
                try:
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget)
                except PolicyError as exc:
                    if self.provider:
                        self.provider.metrics.policy_rejections += 1
                        reasons = self.provider.metrics.policy_rejection_reasons
                        reasons[exc.code] = reasons.get(exc.code, 0) + 1
                    rt.events.append("policy.rejected", {"code": exc.code})
                    draft = deterministic_draft(goal=goal, budget_seconds=budget,
                                                universe=universe)
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget)
                    fallback = f"policy_rejected:{exc.code}"
                rt.frozen_plan = frozen
                _atomic_json(review_dir / "plan.draft.json", draft.as_dict())
                rt.state.transition(ReviewStatus.PLAN_DRAFTED)
                rt.events.append("plan.drafted", {
                    "prompt_version": draft.prompt_version,
                    "fallback_reason": fallback,
                })
                rt.state.transition(ReviewStatus.AWAITING_APPROVAL)
                rt.events.append("plan.awaiting_approval", {
                    "plan_sha256": frozen.plan_sha256,
                    "plan": frozen.as_dict(),
                })
                return self.describe(review_id)
            except Exception as exc:
                self._fail(rt, exc)
                raise

    def _draft_plan(self, provider_name: str, goal: str, budget: float,
                    universe: tuple[str, ...]):
        if provider_name != "live" or self.provider is None:
            reason = "provider_unconfigured" if provider_name == "live" else ""
            if reason and self.provider:
                self.provider.metrics.fallbacks += 1
            return deterministic_draft(goal=goal, budget_seconds=budget,
                                       universe=universe), reason
        last = None
        for _ in range(2):
            try:
                return self.provider.draft_plan(
                    goal=goal, budget_seconds=budget, universe=universe), ""
            except Exception as exc:
                last = exc
        self.provider.metrics.fallbacks += 1
        return deterministic_draft(goal=goal, budget_seconds=budget,
                                   universe=universe), f"model_failed:{type(last).__name__}"

    def approve(self, review_id: str, plan_sha256: str) -> dict:
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_APPROVAL:
            raise IntakeError("REVIEW_NOT_APPROVABLE", rt.state.snapshot.status.value)
        if rt.frozen_plan is None:
            raw = json.loads((rt.review_dir / "plan.draft.json").read_text(encoding="utf-8"))
            raise IntakeError("REVIEW_NOT_RESUMABLE", "restart requires a new review")
        if not secrets.compare_digest(plan_sha256, rt.frozen_plan.plan_sha256):
            raise IntakeError("STALE_PLAN", "approved plan hash is not current")
        _atomic_json(rt.review_dir / "plan.frozen.json", rt.frozen_plan.as_dict())
        rt.state.transition(ReviewStatus.PLAN_FROZEN)
        rt.events.append("plan.approved", {"plan_sha256": plan_sha256})
        thread = threading.Thread(target=self._execute, args=(rt,), daemon=True,
                                  name=f"modou-review-{review_id[:8]}")
        thread.start()
        return self.describe(review_id)

    def _execute(self, rt: ReviewRuntime) -> None:
        try:
            executor = (SandboxedExecutor(rt.session.run_dir)
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor())
            rt.events.append("executor.bound", {
                "mode": executor.mode,
                "sandboxed_children": executor.mode == "sandboxed",
                "control_plane": "trusted",
            })
            with execution_scope(executor):
                self._execute_bound(rt)
        except Exception as exc:
            self._fail(rt, exc)
        finally:
            if rt.session is not None:
                rt.session.cleanup()
            with self._lock:
                if self._live_id == rt.review_id:
                    self._live_id = None

    def _execute_bound(self, rt: ReviewRuntime) -> None:
        """一次 Review 的可信控制面；仓库子进程由当前 Executor 隔离。"""
        rt.state.transition(ReviewStatus.BASELINE_RUNNING,
                            evidence_run_id=rt.session.slug,
                            evidence_started=True)
        rt.router.call("collect_test_scope", {"paths": list(rt.session.test_files)})
        baseline = rt.router.call("run_baseline")
        # Coverage ranking needs the baseline's coverage, which does not exist
        # when the plan is frozen — so the deterministic order is applied here,
        # after the baseline and before the first probe. The frozen universe is
        # untouched: the candidate set is identical, only the visiting order
        # differs, and the loop probes remaining[0], so ranking here is what
        # makes the product match the coverage_first baseline A2 measured.
        summaries = rt.session.candidate_summaries()
        priorities = tuple(rt.frozen_plan.plan.priorities)
        ordered, applied = coverage_first_order(priorities, summaries)
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
        rt.state.transition(ReviewStatus.EXECUTING)
        stop_reason = StopReason.NO_ELIGIBLE_LEFT
        while state.remaining:
            if state.exhausted():
                stop_reason = (StopReason.MAX_STEPS if state.step >= state.max_steps
                               else StopReason.BUDGET_EXHAUSTED)
                rt.events.append("policy.forced_stop", {"stop_reason": stop_reason.value})
                break
            anchor_id = state.remaining[0]
            rt.events.append("probe.started", {"anchor_id": anchor_id})
            rt.state.transition(ReviewStatus.VERIFYING_RESTORE)
            obs = rt.router.call("counterfactual_probe", {"anchor_id": anchor_id})
            rt.state.transition(ReviewStatus.EXECUTING)
            state = state.advance(obs)
            rt.events.append("observation.recorded", {
                    "observation_id": obs.observation_id,
                    "policy_branch": obs.policy_branch,
                    "anchor_id": obs.anchor_id, "status": obs.status,
                    "identical_to_baseline": obs.identical_to_baseline,
                    "regressed_tests": list(obs.regressed_tests),
                    "evidence_id": obs.evidence_id,
            })
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
                               else "deterministic_fallback"),
                    "requested_order": list(requested_order),
                    "original_order": list(original_order),
                    "actual_order": list(state.remaining),
                    "prompt_version": ("scheduler-policy-v2"
                                       if self.agent_level is AgentLevel.L2
                                       else "stop-policy-v1"),
            })
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
        rt.state.transition(ReviewStatus.SYNTHESIZING)
        result = rt.router.call("finish_run", {"stop_reason": stop_reason.value})
        valid = not verify_bundle(Path(result["report"]).parent)
        narration = self._narrate(rt, Path(result["report"]).parent)
        final = (ReviewStatus.PARTIAL
                 if result["summary"].get("analysis_completion") == "partial"
                 else ReviewStatus.COMPLETE)
        rt.state.transition(final, valid_bundle=valid)
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
            model_metrics=(self.provider.metrics.as_dict()
                           if self.provider else {}),
            evidence_valid=valid,
        )
        _atomic_json(rt.review_dir / "review_bundle.json", review_bundle)
        if self.provider and self.provider.private_traces:
            _atomic_json(rt.review_dir / "private" / "model_trace.json", {
                "schema_version": "private-model-trace-v1",
                "traces": self.provider.private_traces,
            })

    def _next_action(self, rt: ReviewRuntime, state: AgentState):
        if rt.request["model_provider"] == "live" and self.provider is not None:
            try:
                return self.provider.next_action(state, ToolCatalog()), ""
            except Exception as exc:
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
            try:
                layout = self.provider.draft_narration(claim_ids=tuple(claim_ids))
            except Exception as exc:
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

    def _fail(self, rt: ReviewRuntime, exc: Exception) -> None:
        reason = (f"{exc.stage}:{exc.detail}" if isinstance(exc, SessionError)
                  else f"{type(exc).__name__}:{str(exc)[:200]}")
        try:
            rt.events.append("review.failed", {"reason": reason})
        except Exception:
            pass
        try:
            if rt.state.snapshot.status not in TERMINAL:
                rt.state.transition(ReviewStatus.FAILED, reason=reason,
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
                "plan": plan, "last_seq": rt.events.last_seq}

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

    def provider_public(self) -> dict:
        return {
            "configured": self.provider is not None,
            "display_name": "OpenAI-compatible" if self.provider else "Deterministic fallback",
            "model_id": self.provider.info.model_id if self.provider else "",
            "live_available": self.provider is not None,
            "agent_level": self.agent_level.value,
            "execution_mode": self.execution_mode.value,
            "prompt_versions": ["planner-v1",
                                ("scheduler-policy-v2"
                                 if self.agent_level is AgentLevel.L2
                                 else "stop-policy-v1"), "narrator-v1"],
        }


def _continue_action():
    from modou.agent.models import AgentAction
    return AgentAction.parse({"kind": "continue", "reason": "继续冻结顺序"}, ToolCatalog())


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _find_evidence_bundle(review_dir: Path) -> Path:
    candidates = list((review_dir / "artifacts").glob("*/report.json"))
    if not candidates:
        raise IntakeError("BUNDLE_NOT_READY", review_dir.name)
    return candidates[0].parent
