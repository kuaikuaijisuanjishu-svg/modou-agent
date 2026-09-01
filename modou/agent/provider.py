"""模型接口与 deterministic provider。

**只有这个文件可以接触外部模型 API**。把 SDK import 散进实验引擎，
就等于让裁决路径依赖一个云服务的可用性和版本。

`DeterministicProvider` 不是占位符，它有三个真实用途：

1. **CI 与离线演示**：断网、没有 key、API 抖动时，整条链仍然走得通；
2. **可比较策略**：FIFO / cost-first / coverage-first 共享同一套接口，
   避免把实现差异误当成调度差异；
3. **失败回退**：模型不可用时退回它，而不是让运行失败。

它按 anchor_id 的自然序推进——也就是 FIFO 基线。**刻意不聪明**：
基线要是偷偷用了启发式，模型调度的增量就测不出来了。
"""
from __future__ import annotations

import json
import hashlib
import os
import time
from dataclasses import dataclass, field

from .models import (ActionKind, AgentAction, AgentLevel, AgentState, StopReason,
                     ToolCatalog)
from .policy import ProbePlanDraft
from .narrator import NarrationLayout


@dataclass(frozen=True)
class ProviderInfo:
    """写进 RunBundle 的 provider 元数据。**不含 key**。"""
    kind: str
    model_id: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    prompt_version: str = ""
    thinking_mode: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


class ModelProvider:
    """provider-neutral 接口。换模型只换实现，不改内核。

    `next_action` 拿到的是 `AgentState`（只读）和 `ToolCatalog`（白名单），
    返回一个已经过 `AgentAction.parse` 校验的动作。**实现不得**：
    直接执行工具、写账本、修改 state。它只负责"下一步做什么"。
    """

    info = ProviderInfo(kind="abstract")

    def next_action(self, state: AgentState,
                    catalog: ToolCatalog) -> AgentAction:
        raise NotImplementedError

    def draft_plan(self, *, goal: str, budget_seconds: float,
                   universe: tuple[str, ...]) -> ProbePlanDraft:
        from .policy import deterministic_draft
        return deterministic_draft(goal=goal, budget_seconds=budget_seconds,
                                   universe=universe)


class ProviderUnavailable(RuntimeError):
    pass


@dataclass
class ProviderMetrics:
    requests: int = 0
    json_parse_failures: int = 0
    schema_rejections: int = 0
    policy_rejections: int = 0
    policy_rejection_reasons: dict[str, int] = field(default_factory=dict)
    fallbacks: int = 0
    forbidden_requests: int = 0
    blocked_requests: int = 0
    executed_forbidden_actions: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict:
        ordered = sorted(self.latencies_ms)
        def pct(p: float):
            if not ordered:
                return None
            return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))], 1)
        n = max(1, self.requests)
        return {
            "requests": self.requests,
            "fallbacks": self.fallbacks,
            "policy_rejections": self.policy_rejections,
            "json_parse_failure_rate": self.json_parse_failures / n,
            "schema_rejection_rate": self.schema_rejections / n,
            "policy_gate_rejection_rate": self.policy_rejections / n,
            "policy_gate_rejection_reasons": dict(self.policy_rejection_reasons),
            "fallback_rate": self.fallbacks / n,
            "latency_ms_p50": pct(.5), "latency_ms_p95": pct(.95),
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "estimated_cost": None,
            "forbidden_requests": self.forbidden_requests,
            "blocked_requests": self.blocked_requests,
            "executed_forbidden_actions": self.executed_forbidden_actions,
        }


class OpenAICompatibleProvider(ModelProvider):
    """Minimal OpenAI-compatible JSON provider; only this class sees credentials."""

    def __init__(self, *, base_url: str, api_key: str, model_id: str,
                 timeout_s: float = 30.0, max_tokens: int = 1600,
                 thinking_mode: str = ""):
        if not base_url or not api_key or not model_id:
            raise ProviderUnavailable("model provider is not fully configured")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = float(timeout_s)
        self._max_tokens = int(max_tokens)
        if thinking_mode not in {"", "disabled", "enabled"}:
            raise ProviderUnavailable("thinking mode must be disabled or enabled")
        self._thinking_mode = thinking_mode
        self.info = ProviderInfo(kind="openai-compatible", model_id=model_id,
                                 temperature=0.0, max_tokens=max_tokens,
                                 prompt_version="planner-v1+stop-policy-v1+narrator-v1",
                                 thinking_mode=thinking_mode)
        self.metrics = ProviderMetrics()
        self.private_traces: list[dict] = []

    @classmethod
    def from_env(cls) -> "OpenAICompatibleProvider":
        return cls(
            base_url=os.environ.get("MODOU_MODEL_BASE_URL", ""),
            api_key=os.environ.get("MODOU_MODEL_API_KEY", ""),
            model_id=os.environ.get("MODOU_MODEL_ID", ""),
            timeout_s=float(os.environ.get("MODOU_MODEL_TIMEOUT_S", "30")),
            max_tokens=int(os.environ.get("MODOU_MODEL_MAX_TOKENS", "1600")),
            thinking_mode=os.environ.get("MODOU_MODEL_THINKING", ""),
        )

    def _request_json(self, *, system: str, user: dict) -> dict:
        try:
            import httpx
        except ImportError as exc:
            raise ProviderUnavailable("httpx is required for live model calls") from exc
        payload = {
            "model": self.info.model_id, "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        }
        if self._thinking_mode:
            # Reasoning tokens share max_tokens with the JSON answer on DeepSeek.
            payload["thinking"] = {"type": self._thinking_mode}
        request_sha256 = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        started = time.monotonic()
        status = 0
        raw_text = ""
        error_type = ""
        self.metrics.requests += 1
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload, timeout=self._timeout_s)
            status = response.status_code
            response.raise_for_status()
            body = response.json()
            raw_text = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
            self.metrics.input_tokens += int(usage.get("prompt_tokens") or 0)
            self.metrics.output_tokens += int(usage.get("completion_tokens") or 0)
            try:
                parsed = json.loads(raw_text)
            except (TypeError, json.JSONDecodeError) as exc:
                self.metrics.json_parse_failures += 1
                raise ProviderUnavailable(f"model returned invalid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                self.metrics.schema_rejections += 1
                raise ProviderUnavailable("model JSON must be an object")
            return parsed
        except Exception as exc:
            error_type = type(exc).__name__
            if isinstance(exc, ProviderUnavailable):
                raise
            raise ProviderUnavailable(f"model request failed: {type(exc).__name__}") from exc
        finally:
            latency = (time.monotonic() - started) * 1000
            self.metrics.latencies_ms.append(latency)
            # Private trace intentionally excludes URL, headers, prompts and key.
            self.private_traces.append({
                "http_status": status, "latency_ms": round(latency, 1),
                "response_chars": len(raw_text), "raw_response": raw_text,
                "request_sha256": request_sha256,
                "response_sha256": hashlib.sha256(
                    raw_text.encode("utf-8")).hexdigest(),
                "error_type": error_type,
            })

    def draft_plan(self, *, goal: str, budget_seconds: float,
                   universe: tuple[str, ...]) -> ProbePlanDraft:
        raw = self._request_json(
            system=("Return one concise JSON object and nothing else. Use exactly these "
                    "fields: goal, budget_seconds, scope, priorities, stop_rules, "
                    "requested_tools, prompt_version. Copy supplied arrays and values; "
                    "priorities may only reorder them. Example JSON: "
                    '{"goal":"g","budget_seconds":60,"scope":["a.py"],'
                    '"priorities":["a.py"],"stop_rules":["baseline_not_green",'
                    '"budget_exhausted","max_steps","restore_dirty"],'
                    '"requested_tools":["collect_test_scope","run_baseline",'
                    '"counterfactual_probe","finish_run"],'
                    '"prompt_version":"planner-v1"}'),
            user={"goal": goal, "budget_seconds": budget_seconds,
                  "scope": list(universe), "priorities": list(universe),
                  "mandatory_stop_rules": ["baseline_not_green", "restore_dirty",
                                           "budget_exhausted", "max_steps"],
                  "allowed_tools": ["collect_test_scope", "run_baseline",
                                    "counterfactual_probe", "finish_run"],
                  "prompt_version": "planner-v1"})
        try:
            return ProbePlanDraft.parse(raw)
        except ValueError:
            self.metrics.schema_rejections += 1
            raise

    def next_action(self, state: AgentState,
                    catalog: ToolCatalog) -> AgentAction:
        if not state.observations:
            raise ProviderUnavailable("stop policy requires at least one real observation")
        l2 = state.agent_level is AgentLevel.L2
        system = (
            "Return one concise JSON object and nothing else. The discriminator field is "
            "named kind, never action. Choose after the supplied real observation. "
            'Continue example: {"kind":"continue","reason":"继续检查剩余候选"}. '
            'Stop example: {"kind":"stop","stop_reason":"goal_satisfied",'
            '"reason":"目标证据已经出现"}. Continue has no anchor or order.')
        if l2:
            system += (
                ' Reprioritize example: {"kind":"reprioritize","order":["b","a"],'
                '"reason":"根据刚才观测先检查 b"}. order must be an exact permutation '
                "of every supplied remaining anchor, with no omission, addition or duplicate.")
        raw = self._request_json(
            system=system,
            user={
                "goal": state.goal, "budget_seconds": state.budget_seconds,
                "spent_seconds": state.spent_seconds, "step": state.step,
                "remaining": list(state.remaining),
                "remaining_candidates": [c.__dict__ for c in state.candidates
                                           if c.anchor_id in state.remaining],
                "last_observation": state.observations[-1].__dict__,
                "observation_branch": state.observations[-1].policy_branch,
                "allowed_actions": (["continue", "stop", "reprioritize"]
                                    if l2 else ["continue", "stop"]),
                "allowed_stop_reasons": [r.value for r in StopReason],
                "prompt_version": "scheduler-policy-v2" if l2 else "stop-policy-v1",
            })
        try:
            action = AgentAction.parse(raw, catalog)
        except ValueError:
            self.metrics.schema_rejections += 1
            raise
        allowed = ({ActionKind.CONTINUE, ActionKind.STOP, ActionKind.REPRIORITIZE}
                   if l2 else {ActionKind.CONTINUE, ActionKind.STOP})
        if action.kind not in allowed:
            self.metrics.forbidden_requests += 1
            self.metrics.blocked_requests += 1
            raise ProviderUnavailable(
                f"{state.agent_level.value} policy rejected {action.kind.value}")
        return action

    def draft_narration(self, *, claim_ids: tuple[str, ...]) -> NarrationLayout:
        raw = self._request_json(
            system=("Return only a JSON layout. You may order the supplied claim IDs "
                    "and choose concise style. Do not write factual claims."),
            user={"claim_ids": list(claim_ids),
                  "allowed_fields": ["claim_order", "style",
                                     "include_scope_first", "suggestions"],
                  "prompt_version": "narrator-v1"})
        try:
            return NarrationLayout.parse(raw, set(claim_ids))
        except ValueError:
            self.metrics.schema_rejections += 1
            raise


def coverage_first_order(anchors: tuple[str, ...],
                        candidates) -> tuple[tuple[str, ...], str]:
    """Rank candidates by how many of their added lines the baseline executed.

    Highest covered-added-lines first, with ties keeping the frozen order
    (`sorted` is stable). This makes the public default deterministic.

    Returns the order *and* the strategy actually applied. When a candidate has
    no summary there is nothing to rank on, and the frozen order is returned
    labelled `fifo` — reporting `coverage_first` in that case would put a claim
    in the bundle that the run did not earn.
    """
    features = {c.anchor_id: c for c in candidates}
    if not anchors or any(a not in features for a in anchors):
        return tuple(anchors), "fifo"
    ranked = sorted(anchors, key=lambda a: -features[a].covered_added_lines)
    return tuple(ranked), "coverage_first"


class DeterministicProvider(ModelProvider):
    """FIFO 基线：按冻结顺序逐个探测，预算耗尽或没有候选就停。

    没有模型、没有网络、没有随机数——同一个 state 永远给同一个动作。
    """

    info = ProviderInfo(kind="deterministic", prompt_version="fifo-v1")

    def next_action(self, state: AgentState,
                    catalog: ToolCatalog) -> AgentAction:
        if state.baseline_green is False:
            # 基线不绿就不能做反事实判定：分不清"删了才红"和"本来就红"。
            return AgentAction.parse(
                {"kind": "stop", "stop_reason": "baseline_not_green",
                 "reason": "基线未通过，反事实结论无法成立"}, catalog)
        if state.step >= state.max_steps:
            return AgentAction.parse(
                {"kind": "stop", "stop_reason": "max_steps",
                 "reason": "已达最大步数"}, catalog)
        if state.budget_left() <= 0:
            return AgentAction.parse(
                {"kind": "stop", "stop_reason": "budget_exhausted",
                 "reason": "预算耗尽"}, catalog)
        if not state.remaining:
            return AgentAction.parse(
                {"kind": "stop", "stop_reason": "no_eligible_left",
                 "reason": "冻结候选已全部处理"}, catalog)
        nxt = state.remaining[0]
        return AgentAction.parse(
            {"kind": "call_tool", "tool": "counterfactual_probe",
             "args": {"anchor_id": nxt},
             "reason": f"按冻结顺序处理下一项：{nxt}"}, catalog)


class RecordedProvider(ModelProvider):
    """回放一串事先录好的动作。演示与轨迹回归用。

    **必须标记为回放**（09 §… / 07 口径纪律）：用它录出来的画面不能冒充 live。
    """

    def __init__(self, actions: list[dict], info: ProviderInfo | None = None):
        self._raw = list(actions)
        self._i = 0
        self.info = info or ProviderInfo(kind="recorded")

    def next_action(self, state: AgentState,
                    catalog: ToolCatalog) -> AgentAction:
        if self._i >= len(self._raw):
            return AgentAction.parse(
                {"kind": "stop", "stop_reason": "no_eligible_left",
                 "reason": "录制的动作已用完"}, catalog)
        raw = self._raw[self._i]
        self._i += 1
        return AgentAction.parse(raw, catalog)
