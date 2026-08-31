"""Planner schema and fail-closed Policy Gate for frozen probe plans."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .models import CATALOG_V1


PROMPT_VERSION = "planner-v1"
POLICY_VERSION = "probe-policy-v1"
MANDATORY_STOP_RULES = frozenset({
    "baseline_not_green", "restore_dirty", "budget_exhausted", "max_steps",
})


class PolicyError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ProbePlanDraft:
    goal: str
    budget_seconds: float
    scope: tuple[str, ...]
    priorities: tuple[str, ...]
    stop_rules: tuple[str, ...]
    requested_tools: tuple[str, ...]
    prompt_version: str = PROMPT_VERSION

    @classmethod
    def parse(cls, raw: dict) -> "ProbePlanDraft":
        if not isinstance(raw, dict):
            raise PolicyError("PLAN_NOT_OBJECT", "plan must be a JSON object")
        allowed = {"goal", "budget_seconds", "scope", "priorities", "stop_rules",
                   "requested_tools", "prompt_version"}
        extra = set(raw) - allowed
        if extra:
            raise PolicyError("PLAN_EXTRA_FIELDS", str(sorted(extra)))
        try:
            return cls(
                goal=str(raw.get("goal") or "")[:500],
                budget_seconds=float(raw["budget_seconds"]),
                scope=_strings(raw.get("scope"), "scope"),
                priorities=_strings(raw.get("priorities"), "priorities"),
                stop_rules=_strings(raw.get("stop_rules"), "stop_rules"),
                requested_tools=_strings(raw.get("requested_tools"), "requested_tools"),
                prompt_version=str(raw.get("prompt_version") or PROMPT_VERSION),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, PolicyError):
                raise
            raise PolicyError("PLAN_SCHEMA_INVALID", str(exc)) from exc

    def as_dict(self) -> dict:
        raw = asdict(self)
        for key in ("scope", "priorities", "stop_rules", "requested_tools"):
            raw[key] = list(raw[key])
        return raw


def _strings(value, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError("PLAN_SCHEMA_INVALID", f"{field} must be string array")
    if len(value) != len(set(value)):
        raise PolicyError("PLAN_DUPLICATE", field)
    return tuple(value)


@dataclass(frozen=True)
class FrozenProbePlan:
    plan: ProbePlanDraft
    plan_sha256: str
    policy_version: str = POLICY_VERSION

    def as_dict(self) -> dict:
        return {**self.plan.as_dict(), "plan_sha256": self.plan_sha256,
                "policy_version": self.policy_version}


def deterministic_draft(*, goal: str, budget_seconds: float,
                        universe: tuple[str, ...]) -> ProbePlanDraft:
    return ProbePlanDraft(
        goal=goal, budget_seconds=budget_seconds, scope=universe,
        priorities=universe, stop_rules=tuple(sorted(MANDATORY_STOP_RULES)),
        requested_tools=("collect_test_scope", "run_baseline",
                         "counterfactual_probe", "finish_run"),
        prompt_version="deterministic-fifo-v1")


def compile_plan(draft: ProbePlanDraft, *, universe: tuple[str, ...],
                 user_budget_seconds: float) -> FrozenProbePlan:
    if draft.prompt_version not in {PROMPT_VERSION, "deterministic-fifo-v1"}:
        raise PolicyError("PROMPT_VERSION_UNSUPPORTED", draft.prompt_version)
    expected = set(universe)
    if set(draft.scope) != expected or len(draft.scope) != len(universe):
        raise PolicyError("SCOPE_CHANGED", "scope must contain the frozen universe exactly")
    if set(draft.priorities) != expected or len(draft.priorities) != len(universe):
        raise PolicyError("CANDIDATES_CHANGED", "priorities may reorder but not add or remove anchors")
    if not (0 < draft.budget_seconds <= user_budget_seconds):
        raise PolicyError("BUDGET_EXCEEDED", f"{draft.budget_seconds} > {user_budget_seconds}")
    missing = MANDATORY_STOP_RULES - set(draft.stop_rules)
    if missing:
        raise PolicyError("STOP_RULE_MISSING", str(sorted(missing)))
    allowed_tools = {spec.name for spec in CATALOG_V1}
    forbidden = set(draft.requested_tools) - allowed_tools
    if forbidden:
        raise PolicyError("TOOL_NOT_ALLOWED", str(sorted(forbidden)))
    canonical = json.dumps(draft.as_dict(), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return FrozenProbePlan(plan=draft, plan_sha256=digest)
