"""预算调度器：固定顺序与 FIFO 对照（总计划 T10·方向 13）。

调度器不做执行、不创建作业；它把验收项集合（必选/可选、已知缺口、变更关联
测试标记）与可用预算变成**执行计划**：排序后的项、每项预算分配、被预算截断
的项。两种策略共用同一入口：

- ``fixed_order``（固定顺序）：必选项 → 已知缺口 → 变更关联测试 → 补充实验；
  同级按 ``(order_hint, item_id)`` 确定性排序，输入顺序不影响结果。
- ``fifo``：按投递顺序（``arrival_index``）排队，其余逻辑相同。

FIFO 对照纪律：两策略使用**同一冻结样本、同一预算总额**（各自独立但等额的
简化账本），保留全部结果；不预设固定顺序一定更优——对照输出只给逐项明细与
计数指标，结论由数据说话。

简化账本 :class:`SimpleBudgetLedger` 复刻 ``jobs.py`` 的预留-结算语义（创建时
预留、结束结算实际用量、失败/超支不重置已结算额度），但**不 import jobs.py**，
避免与公共作业服务耦合。作业执行期间若实际用量超出预留，超出部分计入已结算，
后续项的可预留额度随之收紧（预算累计）。

接线要求（集成人，接入 JobService）
1. 调度方先经 ``JobService.create_budget(total=...)`` 建立任务共享预算，得到
   ``budget_id``；再对本模块的计划调用 ``job_request_bodies(plan, ...)``。
2. 每个请求体按 jobs 契约投递：``POST /api/v2/jobs``，body 含
   ``{kind, payload, task_id, round_id, criterion_id, budget_id, dedupe_key}``；
   ``dedupe_key`` 已按 ``t10:<plan_digest>:<item_id>`` 生成，重复投递返回原作业。
3. 作业结束（含失败/取消）必须结算实际用量；不要按计划分配值结算。
4. 计划截断（``truncated``）的项不投递作业；如人工放宽预算，重新生成计划，
   不允许绕过预算创建影子作业（jobs 契约原文）。
"""
from __future__ import annotations

import hashlib
import json

__all__ = ["SCHEDULER_SCHEMA_VERSION", "TIERS", "SchedulerError",
           "SimpleBudgetLedger", "plan", "simulate", "compare_strategies",
           "job_request_bodies"]

SCHEDULER_SCHEMA_VERSION = "master-2026-09-scheduler-v1"

TIERS = ("mandatory", "known_gap", "change_linked", "supplementary")
DEFAULT_JOB_KIND = "domain_experiment"


class SchedulerError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _number(value, name, *, minimum=0.0):
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or value < minimum:
        raise SchedulerError("SCHEDULER_ITEM_INVALID", name)
    return float(value)


def _normalize(items):
    if not isinstance(items, list) or not items:
        raise SchedulerError("SCHEDULER_ITEM_INVALID", "items must be nonempty")
    seen = set()
    normalized = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise SchedulerError("SCHEDULER_ITEM_INVALID", "item must be a dict")
        item_id = raw.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip() \
                or len(item_id) > 100 or item_id in seen:
            raise SchedulerError("SCHEDULER_ITEM_INVALID", "item_id")
        seen.add(item_id)
        flags = {key: bool(raw.get(key, False))
                 for key in ("mandatory", "known_gap", "change_linked")}
        item = {
            "item_id": item_id,
            "text": str(raw.get("text", ""))[:300],
            "tier": ("mandatory" if flags["mandatory"] else
                     "known_gap" if flags["known_gap"] else
                     "change_linked" if flags["change_linked"] else
                     "supplementary"),
            **flags,
            "estimated_cost": _number(raw.get("estimated_cost"),
                                      "estimated_cost", minimum=1e-9),
            "order_hint": int(_number(raw.get("order_hint", 0), "order_hint")),
            "arrival_index": int(_number(raw.get("arrival_index", index),
                                         "arrival_index")),
            "actual_cost": _number(raw.get("actual_cost",
                                           raw.get("estimated_cost", 0.0) or 0.0),
                                   "actual_cost", minimum=0.0),
            "job_kind": str(raw.get("job_kind", DEFAULT_JOB_KIND))[:60],
            "criterion_id": str(raw.get("criterion_id", ""))[:100],
        }
        normalized.append(item)
    return normalized


def _sort_key(item, strategy):
    if strategy == "fifo":
        return (item["arrival_index"], item["item_id"])
    return (TIERS.index(item["tier"]), item["order_hint"], item["item_id"])


def _sample_digest(normalized):
    # 指纹描述样本内容（按 item_id 归一排序），与输入列表顺序无关。
    ordered = sorted(normalized, key=lambda item: item["item_id"])
    canonical = json.dumps(ordered, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def plan(items, budget_total, *, strategy: str = "fixed_order") -> dict:
    """生成执行计划：排序列表 + 每项预算分配 + 被预算截断的项。"""
    if strategy not in {"fixed_order", "fifo"}:
        raise SchedulerError("SCHEDULER_STRATEGY_UNKNOWN", strategy)
    total = _number(budget_total, "budget_total")
    normalized = _normalize(items)
    ordered = sorted(normalized, key=lambda item: _sort_key(item, strategy))
    planned, truncated, allocated = [], [], 0.0
    exhausted = False
    for seq, item in enumerate(ordered, start=1):
        cost = item["estimated_cost"]
        if exhausted or allocated + cost > total:
            exhausted = True
            truncated.append({
                "item_id": item["item_id"], "tier": item["tier"],
                "reason": "budget_exhausted", "estimated_cost": cost,
                "shortfall": round(cost - max(total - allocated, 0.0), 6),
            })
            continue
        allocated = round(allocated + cost, 6)
        planned.append({
            "seq": seq, "item_id": item["item_id"], "tier": item["tier"],
            "text": item["text"], "budget_allocation": cost,
            "sort_key": list(_sort_key(item, strategy)),
        })
    return {
        "schema_version": SCHEDULER_SCHEMA_VERSION,
        "strategy": strategy, "budget_total": total,
        "sample_digest": _sample_digest(normalized),
        "planned": planned, "truncated": truncated,
        "total_allocated": round(allocated, 6),
        "total_estimated": round(sum(i["estimated_cost"] for i in ordered), 6),
    }


class SimpleBudgetLedger:
    """jobs.py 预留-结算语义的简化等价账本（独立实现，不 import jobs）。

    ``reserve`` 检查 ``settled + reserved + amount <= total``；``settle`` 释放
    预留并把**实际用量**计入已结算；失败/超支不回滚已结算额度（预算累计）。
    """

    def __init__(self, budget_id: str, total: float):
        if not isinstance(budget_id, str) or not budget_id.strip():
            raise SchedulerError("SCHEDULER_BUDGET_INVALID", "budget_id")
        self.budget_id = budget_id
        self.total = _number(total, "total")
        self.reserved = 0.0
        self.settled = 0.0

    def reserve(self, amount: float) -> bool:
        amount = _number(amount, "amount", minimum=1e-9)
        if self.settled + self.reserved + amount > self.total + 1e-9:
            return False
        self.reserved = round(self.reserved + amount, 6)
        return True

    def settle(self, reserved_amount: float, actual: float) -> None:
        reserved_amount = _number(reserved_amount, "reserved_amount")
        actual = _number(actual, "actual")
        self.reserved = round(max(self.reserved - reserved_amount, 0.0), 6)
        self.settled = round(self.settled + actual, 6)

    def state(self) -> dict:
        return {"budget_id": self.budget_id, "total": self.total,
                "reserved": self.reserved, "settled": self.settled,
                "available": round(max(self.total - self.settled - self.reserved,
                                       0.0), 6)}


def simulate(execution_plan: dict, ledger: SimpleBudgetLedger,
             *, actual_costs=None) -> dict:
    """按计划模拟执行：预留 → 结算实际用量；超支收紧后续额度。

    ``actual_costs`` 可覆盖每项实际用量（键为 item_id），缺省用冻结样本的
    ``actual_cost`` 字段（未提供时等于估计值）。
    """
    if not isinstance(execution_plan, dict) or not isinstance(ledger,
                                                              SimpleBudgetLedger):
        raise SchedulerError("SCHEDULER_INPUT_INVALID", "plan/ledger")
    costs = dict(actual_costs or {})
    executed, truncated = [], []
    for entry in execution_plan["planned"]:
        item_id = entry["item_id"]
        actual = _number(costs.get(item_id, entry["budget_allocation"]),
                         f"actual_cost[{item_id}]")
        if not ledger.reserve(entry["budget_allocation"]):
            truncated.append({"item_id": item_id, "tier": entry["tier"],
                              "reason": "budget_exhausted",
                              "estimated_cost": entry["budget_allocation"]})
            continue
        ledger.settle(entry["budget_allocation"], actual)
        executed.append({
            "item_id": item_id, "tier": entry["tier"], "status": "executed",
            "reserved": entry["budget_allocation"], "settled_actual": actual,
            "overrun": round(actual - entry["budget_allocation"], 6),
        })
    truncated.extend({"item_id": t["item_id"], "tier": t["tier"],
                      "reason": "budget_exhausted",
                      "estimated_cost": t["estimated_cost"]}
                     for t in execution_plan["truncated"])
    return {
        "strategy": execution_plan["strategy"],
        "budget_total": execution_plan["budget_total"],
        "executed": executed, "truncated": truncated,
        "ledger": ledger.state(),
        "metrics": _metrics(execution_plan, executed, truncated, ledger),
    }


def _metrics(execution_plan, executed, truncated, ledger) -> dict:
    tiers_planned = {tier: sum(1 for p in execution_plan["planned"]
                               if p["tier"] == tier) for tier in TIERS}
    tiers_executed = {tier: sum(1 for e in executed if e["tier"] == tier)
                      for tier in TIERS}
    return {
        "planned": len(execution_plan["planned"]),
        "executed": len(executed),
        "truncated": len(truncated),
        "overruns": sum(1 for e in executed if e["overrun"] > 0),
        "tiers_planned": tiers_planned,
        "tiers_executed": tiers_executed,
        "settled": ledger.settled,
        "budget_utilization": (round(ledger.settled / ledger.total, 6)
                               if ledger.total else 0.0),
        # 关键能力指标：必选项是否在预算内保全（不足即真实缺口，如实计数）。
        "mandatory_executed": tiers_executed["mandatory"],
        "mandatory_planned": tiers_planned["mandatory"],
    }


def compare_strategies(items, budget_total) -> dict:
    """同一冻结样本、同一预算总额下 fixed_order 与 FIFO 的完整对照。

    两策略各持等额独立账本（同一总额）；输出保留两套完整计划与模拟结果，
    ``comparison`` 只并列计数指标，不判定优劣。
    """
    normalized = _normalize(items)
    digest = _sample_digest(normalized)
    actual_costs = {item["item_id"]: item["actual_cost"] for item in normalized}
    by_strategy = {}
    for strategy in ("fixed_order", "fifo"):
        execution_plan = plan(normalized, budget_total, strategy=strategy)
        ledger = SimpleBudgetLedger(f"t10-compare-{strategy}-{digest[:8]}",
                                    budget_total)
        by_strategy[strategy] = {**execution_plan,
                                 "run": simulate(execution_plan, ledger,
                                                 actual_costs=actual_costs)}
    fixed, fifo = by_strategy["fixed_order"], by_strategy["fifo"]
    return {
        "schema_version": SCHEDULER_SCHEMA_VERSION,
        "sample_digest": digest, "budget_total": float(budget_total),
        "strategies": by_strategy,
        "comparison": {
            "metric": ["executed", "truncated", "overruns",
                       "mandatory_executed", "mandatory_planned",
                       "settled", "budget_utilization"],
            "fixed_order": fixed["run"]["metrics"],
            "fifo": fifo["run"]["metrics"],
            "note": "两策略同一冻结样本与预算总额；不预设任何策略更优。",
        },
    }


def job_request_bodies(execution_plan: dict, *, task_id: str, round_id: str,
                       budget_id: str) -> list:
    """把执行计划翻译成 jobs 契约的投递请求体（接线辅助；不执行）。"""
    if not isinstance(execution_plan, dict):
        raise SchedulerError("SCHEDULER_INPUT_INVALID", "plan")
    for name, value in (("task_id", task_id), ("round_id", round_id),
                        ("budget_id", budget_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise SchedulerError("SCHEDULER_INPUT_INVALID", name)
    digest = execution_plan.get("sample_digest", "")
    bodies = []
    for entry in execution_plan["planned"]:
        bodies.append({
            "kind": DEFAULT_JOB_KIND,
            "payload": {"item_id": entry["item_id"], "text": entry["text"],
                        "tier": entry["tier"], "seq": entry["seq"]},
            "task_id": task_id, "round_id": round_id,
            "criterion_id": entry["item_id"],
            "budget_id": budget_id,
            "dedupe_key": f"t10:{digest}:{entry['item_id']}",
        })
    return bodies
