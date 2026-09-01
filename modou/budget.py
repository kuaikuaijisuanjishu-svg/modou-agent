"""统一的时间预算。

每个补丁使用 300 秒总 deadline，**不采用孤立的“40 次 × 每次独立超时”**，
避免单次限制叠加后突破总预算。

    剩余预算 = 300 - 已耗时 - 5（最终恢复/渲染保留）
    单次估计 = max(基线耗时 × 1.5, 1 秒)
    最大探测次数 = min(40, floor(剩余预算 / 单次估计))

到点立即停止新增探测，剩下的行记 未标注/budget_exhausted。
超时不把实例排除出统计，而是通过未标注行**降低已标注比例**——
把跑不完的实例悄悄丢掉会让指标虚高。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

TOTAL = 300.0
# 每个 Trial 已经强制 reset + clean，不再需要在末尾预留 20 秒做累计恢复。
# 保留 5 秒给第二道整次恢复、账本校验与原子发布；总预算仍严格为 300 秒。
RESERVE = 5.0
HARD_MAX_PROBES = 40


@dataclass
class Budget:
    started: float
    baseline_seconds: float
    total: float = TOTAL
    reserve: float = RESERVE
    probes_used: int = 0

    @classmethod
    def start(cls, baseline_seconds: float, started: float | None = None) -> "Budget":
        return cls(started=started if started is not None else time.time(),
                   baseline_seconds=baseline_seconds)

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.elapsed - self.reserve)

    @property
    def per_probe(self) -> float:
        return max(self.baseline_seconds * 1.5, 1.0)

    @property
    def max_probes(self) -> int:
        return min(HARD_MAX_PROBES, int(self.remaining // self.per_probe))

    def can_probe(self) -> bool:
        return self.probes_used < HARD_MAX_PROBES and \
            self.remaining >= self.per_probe

    def probe_deadline(self) -> float:
        """给子进程的超时：不超过剩余预算，也不至于卡死。"""
        return max(5.0, min(self.remaining, self.per_probe * 4))

    def spend(self) -> None:
        self.probes_used += 1
