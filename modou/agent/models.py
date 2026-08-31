"""Agent 层的数据模型：状态、动作、工具目录、工具结果。

**为什么动作要是数据结构，不是字符串。**

一个 Agent 最容易出事的地方是"模型说了句什么，我们照着执行"。
只要动作能表示成自由文本，就一定有一天会执行到不该执行的东西——
不是因为模型恶意，而是因为提示注入、schema 漂移或者一次格式错误。

所以这里把动作做成封闭类型：`tool` 必须是 `ToolCatalog` 里已登记的名字，
`args` 必须是该工具声明过的字段。模型给不出合法动作，就是拒绝，
不是"尽量理解一下"。`AgentAction.parse` 是唯一入口。

**状态为什么是只读的。**

`AgentState` 里的 `observations` 记录已经发生过的实验。模型可以读它来决定
下一步，但不能改它——把失败实验说成成功、把超时从记录里删掉，
是 04 §五 明确要防的作弊。这里用 frozen dataclass + tuple 表达这条纪律，
而不是靠 prompt 里写一句"请不要修改"。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class ActionKind(str, Enum):
    """Closed vocabulary for model actions."""
    CALL_TOOL = "call_tool"          # 调用工具目录里的某个工具
    CONTINUE = "continue"            # 继续；下一锚点仍由确定性调度器选择
    REPRIORITIZE = "reprioritize"    # 重排剩余候选（不能增删）
    STOP = "stop"                    # 主动结束，必须给出 StopReason
    ASK_HUMAN = "ask_human"          # 升级人工


class AgentLevel(str, Enum):
    """Server-bound capability level; browser requests cannot change it."""
    L1 = "l1"
    L2 = "l2"


class StopReason(str, Enum):
    """停止必须有机器可读原因。"没有理由地停下"是 loop 检测不到的失败。"""
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_ELIGIBLE_LEFT = "no_eligible_left"
    GOAL_SATISFIED = "goal_satisfied"
    BASELINE_NOT_GREEN = "baseline_not_green"
    RESTORE_DIRTY = "restore_dirty"
    MAX_STEPS = "max_steps"
    HUMAN_REQUESTED = "human_requested"


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"      # 只读仓库，不执行仓库代码
    EXECUTES_REPO = "executes"   # 执行仓库代码——pytest 与 import 都算
    MUTATES = "mutates"          # 施加可逆干预


@dataclass(frozen=True)
class ToolSpec:
    """一个 typed tool 的声明。`args` 是白名单，多一个字段就是拒绝。"""
    name: str
    risk: ToolRisk
    args: frozenset[str] = frozenset()
    summary: str = ""


#: 初评开放的八个工具（02 §四）。**这里没有的东西模型就调不到**——
#: 尤其没有 shell、write_test、git_commit、git_push、delete_file_permanently。
#: 不提供它们比"提供了再拦住"安全一个数量级：拦截逻辑会有 bug，不存在的东西不会。
CATALOG_V1: tuple[ToolSpec, ...] = (
    ToolSpec("inspect_repo", ToolRisk.READ_ONLY, frozenset(),
             "读取 Git、语言、测试框架和文件树摘要"),
    ToolSpec("inspect_patch", ToolRisk.READ_ONLY, frozenset(),
             "解析 diff、新增行、hunk、候选 anchor"),
    ToolSpec("collect_test_scope", ToolRisk.EXECUTES_REPO, frozenset({"paths"}),
             "收集并规范化 pytest nodeid（会 import 仓库代码）"),
    ToolSpec("build_eligible_universe", ToolRisk.READ_ONLY, frozenset(),
             "按冻结规则建立合格实验全集"),
    ToolSpec("run_baseline", ToolRisk.EXECUTES_REPO, frozenset(),
             "运行声明测试与覆盖率"),
    ToolSpec("counterfactual_probe", ToolRisk.MUTATES, frozenset({"anchor_id"}),
             "临时干预、跑测试、恢复、校验"),
    ToolSpec("read_evidence", ToolRisk.READ_ONLY, frozenset({"evidence_id"}),
             "按 evidence_id 读取结构化证据"),
    ToolSpec("finish_run", ToolRisk.READ_ONLY, frozenset({"stop_reason"}),
             "封存状态、生成报告和 replay 模型"),
)


class ToolNotAllowed(ValueError):
    """模型请求了目录之外的工具，或给了未声明的参数。

    这是**请求**越权，不是**执行**越权——两者必须分开计数（04 §十一）。
    把被拦下的请求算成"模型没有越权意图"是在美化数字。
    """


@dataclass(frozen=True)
class ToolCatalog:
    specs: tuple[ToolSpec, ...] = CATALOG_V1

    def get(self, name: str) -> ToolSpec:
        for s in self.specs:
            if s.name == name:
                return s
        raise ToolNotAllowed(
            f"工具 {name!r} 不在目录里。可用：{[s.name for s in self.specs]}")

    def check(self, name: str, args: dict) -> ToolSpec:
        spec = self.get(name)
        extra = set(args) - set(spec.args)
        if extra:
            raise ToolNotAllowed(
                f"{name} 不接受参数 {sorted(extra)}；只声明了 {sorted(spec.args)}")
        return spec


@dataclass(frozen=True)
class AgentAction:
    """模型的一步。**只能由 `parse` 构造**，因为构造即校验。"""
    kind: ActionKind
    tool: str = ""
    args: dict = field(default_factory=dict)
    order: tuple[str, ...] = ()          # REPRIORITIZE 时的新顺序
    stop_reason: StopReason | None = None
    #: 展示给用户看的一句话理由。**不是思维链**——02 §七 要求它简短可展示。
    reason: str = ""

    @classmethod
    def parse(cls, raw: Any, catalog: ToolCatalog) -> "AgentAction":
        """把模型输出转成合法动作。**不合法就抛，不猜。**"""
        if not isinstance(raw, dict):
            raise ToolNotAllowed(f"动作必须是对象，收到 {type(raw).__name__}")
        try:
            kind = ActionKind(raw.get("kind"))
        except ValueError:
            raise ToolNotAllowed(
                f"未知动作 {raw.get('kind')!r}；只允许 "
                f"{[k.value for k in ActionKind]}")
        reason = str(raw.get("reason", ""))[:300]

        if kind is ActionKind.CALL_TOOL:
            name = raw.get("tool", "")
            args = raw.get("args") or {}
            if not isinstance(args, dict):
                raise ToolNotAllowed("args 必须是对象")
            catalog.check(name, args)
            return cls(kind, tool=name, args=dict(args), reason=reason)

        if kind is ActionKind.REPRIORITIZE:
            order = raw.get("order") or []
            if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
                raise ToolNotAllowed("order 必须是 anchor_id 字符串列表")
            return cls(kind, order=tuple(order), reason=reason)

        if kind is ActionKind.STOP:
            try:
                sr = StopReason(raw.get("stop_reason"))
            except ValueError:
                raise ToolNotAllowed(
                    f"停止必须给合法 stop_reason，收到 {raw.get('stop_reason')!r}")
            return cls(kind, stop_reason=sr, reason=reason)

        return cls(kind, reason=reason)          # CONTINUE / ASK_HUMAN


@dataclass(frozen=True)
class Observation:
    """一次实验之后模型能看到的东西。**没有原始 shell 输出，也没有源码。**

    给模型看什么是个安全决策：仓库内容可能带提示注入，把整份 stdout
    喂回去等于把注入面直接接上模型。这里只回结构化结局。
    """
    anchor_id: str
    status: str                    # COMPLETE / TIMEOUT / INVALID / ...
    identical_to_baseline: bool | None
    regressed_tests: tuple[str, ...] = ()
    cost_s: float = 0.0
    evidence_id: str = ""
    observation_id: str = ""

    @property
    def policy_branch(self) -> str:
        """Pre-registered scheduler branch derived only from bounded facts."""
        if self.status in {"TIMEOUT", "INVALID", "ERROR", "TOOL_FAILED"}:
            return "invalid_or_timeout"
        if self.regressed_tests or self.identical_to_baseline is False:
            return "strong_evidence"
        return "no_target_evidence"


@dataclass(frozen=True)
class CandidateSummary:
    """Bounded scheduling features; never raw source or command output."""
    anchor_id: str
    path: str
    added_lines: int
    new_file: bool
    covered_added_lines: int
    probe_pending_lines: int
    estimated_cost_s: float


class ReprioritizationRejected(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    tool: str
    data: dict = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class AgentState:
    """一次运行的全部可见状态。**frozen**：推进状态只能靠 `advance` 产生新值。

    这不是洁癖。可变状态意味着任何一处代码都能改预算、改已完成集合，
    而"模型不能删掉难实验来提高完成率"（02 §七）这条防作弊纪律
    就没有结构上的保证。
    """
    goal: str = ""
    budget_seconds: float = 0.0
    spent_seconds: float = 0.0
    step: int = 0
    max_steps: int = 32
    #: 运行前**冻结**的合格实验全集。模型只能重排，不能增删——
    #: 它是 H1′ 的分母，动了它整个调度评测就失去意义（04 §六）。
    frozen_universe: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()
    observations: tuple[Observation, ...] = ()
    baseline_green: bool | None = None
    agent_level: AgentLevel = AgentLevel.L1
    candidates: tuple[CandidateSummary, ...] = ()
    decided_observation_ids: tuple[str, ...] = ()

    def budget_left(self) -> float:
        return max(0.0, self.budget_seconds - self.spent_seconds)

    def exhausted(self) -> bool:
        return self.budget_left() <= 0 or self.step >= self.max_steps

    def advance(self, obs: Observation) -> "AgentState":
        """记一次观测。已完成的实验从剩余里移除，预算按实际耗时扣。"""
        if not obs.observation_id:
            obs = replace(obs, observation_id=f"obs:{self.step + 1}:{obs.anchor_id}")
        return replace(
            self,
            step=self.step + 1,
            spent_seconds=self.spent_seconds + obs.cost_s,
            remaining=tuple(a for a in self.remaining if a != obs.anchor_id),
            observations=self.observations + (obs,))

    def mark_decision(self, observation_id: str) -> "AgentState":
        if not self.observations or observation_id != self.observations[-1].observation_id:
            raise ReprioritizationRejected(
                "OBSERVATION_NOT_CURRENT", "decision must bind the latest observation")
        if observation_id in self.decided_observation_ids:
            raise ReprioritizationRejected(
                "OBSERVATION_ALREADY_DECIDED", observation_id)
        return replace(
            self, decided_observation_ids=self.decided_observation_ids + (observation_id,))

    def reorder(self, order: tuple[str, ...], observation_id: str = "") -> "AgentState":
        """Strict L2 permutation: invalid requests are rejected, never repaired."""
        if self.agent_level is not AgentLevel.L2:
            raise ReprioritizationRejected(
                "AGENT_LEVEL_FORBIDDEN", "reprioritize requires server-bound L2")
        decided = self.mark_decision(observation_id)
        if len(order) != len(self.remaining):
            raise ReprioritizationRejected(
                "ORDER_LENGTH_MISMATCH", "order must include every remaining anchor")
        if len(order) != len(set(order)):
            raise ReprioritizationRejected("ORDER_DUPLICATE", "order contains duplicates")
        if set(order) != set(self.remaining):
            raise ReprioritizationRejected(
                "ORDER_CANDIDATES_CHANGED", "order must be an exact remaining permutation")
        return replace(decided, remaining=tuple(order))
