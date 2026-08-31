"""Unified intervention, observation, and restore runner.

- `engines/hdd.py::_probe`：删若干行 → 跑声明范围 → 一律 revert；
- `engines/drift.py::evaluate`：把文件挪走 → 重新收集 → 跑声明范围 → 一律放回。

两处重复的是同一件事：**回滚必须在 finally 里，无论判成什么**。
重复本身不是大问题，问题是"回滚"这种不能出错的动作有两份实现，
就有两处可以各自出错、各自漂移。收敛之后只有一处。

Experiment records are written directly to the ledger,
而不是等 `ledger/legacy.py` 事后从 `EvidenceUnit` 反推。
so the ledger retains the observed execution and restore result.

`during` 钩子是给游离用的：它需要在文件被挪走的状态下先重新收集一次 nodeid，
清单不一致就直接放弃，连测试都不用跑。钩子返回原因字符串即中止，返回 None 继续。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import mutate
from .ledger import anchors, records
from .ledger.records import Record
from .models import TestVector
from .testrange import (run_declared, JUnitUnusable, ProbeTimeout,
                        TestRangeError)

#: 实验结局。账本必须记下**真实发生的是什么**，所以观测失败要分型：
#: "跑了 300 秒没跑完"、"JUnit 解析不了"、"还没跑测试就放弃了"
#: 是三件不同的事。旧的对外投影仍把它们统一成 probe_timeout，
#: 但账本底层不能撒谎。
RAN = "RAN"
INVALID = "INVALID"
TIMEOUT = "TIMEOUT"
ABORTED = "ABORTED"
JUNIT_UNUSABLE = "JUNIT_UNUSABLE"
OBSERVATION_FAILED = "OBSERVATION_FAILED"
DIRTY = "DIRTY"

_TO_LEDGER = {RAN: records.COMPLETE, INVALID: records.INVALID,
              TIMEOUT: records.TIMEOUT, ABORTED: records.ABORTED,
              JUNIT_UNUSABLE: records.JUNIT_UNUSABLE,
              OBSERVATION_FAILED: records.OBSERVATION_FAILED,
              DIRTY: records.DIRTY}

#: 对外投影：这些底层结局在旧的三态输出里统一呈现为 probe_timeout。
#: 账本分型，投影合并——两者不冲突，前者是事实，后者是口径。
PROJECTS_AS_PROBE_TIMEOUT = frozenset(
    {TIMEOUT, JUNIT_UNUSABLE, OBSERVATION_FAILED})


def _observation_failure(e: Exception) -> str:
    """把 testrange 的异常分型。不靠错误字符串判断。"""
    if isinstance(e, JUnitUnusable):
        return JUNIT_UNUSABLE
    if isinstance(e, ProbeTimeout):
        return TIMEOUT
    return OBSERVATION_FAILED


@dataclass
class Intervention:
    """做了什么。`describe` 直接进证书的「方法」栏，措辞受纪律约束。"""
    kind: str                                  # records.X_*
    path: str
    lines: tuple[int, ...] = ()
    file_removed: bool = False
    describe: str = ""

    def to_json(self) -> dict:
        return {"kind": self.kind, "path": self.path, "lines": list(self.lines),
                "file_removed": self.file_removed, "describe": self.describe}


@dataclass(frozen=True)
class DuringObservation:
    """干预仍生效时完成的额外观测，例如文件移除后的 pytest 收集集。"""
    reason: str = ""
    fact_ids: tuple[str, ...] = ()


class TrialRestoreDirty(RuntimeError):
    """一次 Trial 未能恢复到基线；实例必须立即失败关闭。"""

    def __init__(self, detail: str, outcome=None):
        super().__init__(detail)
        self.detail = detail
        self.outcome = outcome


@dataclass
class TrialOutcome:
    status: str
    vector: Optional[TestVector] = None
    restored: Optional[TestVector] = None
    mutation: Optional[mutate.Mutation] = None
    seconds: float = 0.0
    abort_reason: str = ""
    phase: str = ""                            # pre_test | test_run
    during_result: object = None
    during_fact_ids: tuple[str, ...] = ()
    experiment_id: str = ""
    fact_ids: tuple[str, ...] = ()

    @property
    def ran(self) -> bool:
        return self.status == RAN


@dataclass
class TrialRunner:
    """一个实例内所有干预实验的执行者。记录直接进 `collector`。"""
    ws: object
    adapter: object
    nodeids: list
    run_dir: Path
    baseline: TestVector
    run_id: str = ""
    collector: list = field(default_factory=list)

    # ------------------------------------------------------------ 观测

    def _run(self, junit_name: str, deadline: float) -> TestVector:
        rr = run_declared(self.adapter, self.ws.python, self.ws.path,
                          self.nodeids, self.run_dir / junit_name,
                          deadline=deadline)
        return rr.vector

    def _vector_fact(self, anchor: anchors.Anchor, which: str,
                     vector: TestVector | None) -> str:
        if vector is None:
            return ""
        f = records.fact(
            records.F_TEST_VECTOR, anchor=anchor,
            payload={"which": which,
                     "statuses": [[t, s.value] for t, s in vector.statuses],
                     # 让账本自己就能回答"回滚干不干净"，不必去 join 主张。
                     "identical_to_baseline": vector.identical_to(self.baseline)},
            observer="declared_tests", run_id=self.run_id)
        self.collector.append(f)
        return f.record_id

    def _record(self, anchor: anchors.Anchor, iv: Intervention,
                out: TrialOutcome, restored_clean: bool | None) -> TrialOutcome:
        pre = self._vector_fact(anchor, "baseline", self.baseline)
        post = self._vector_fact(anchor, "mutated", out.vector)
        back = self._vector_fact(anchor, "restored", out.restored)
        x = records.experiment(
            iv.kind, anchor=anchor, snapshot_base="S2",
            intervention=iv.to_json(),
            pre=[pre], post=[p for p in (post, back, *out.during_fact_ids) if p],
            status=_TO_LEDGER[out.status], restored_clean=restored_clean,
            cost_s=out.seconds, run_id=self.run_id,
            failure_reason=out.abort_reason, phase=out.phase)
        self.collector.append(x)
        out.experiment_id = x.record_id
        out.fact_ids = tuple(p for p in (pre, post, back) if p)
        return out

    def _restore(self, out: TrialOutcome) -> bool:
        """每次 Trial 都用同一协议恢复；失败信息先落 Experiment，再抛出。"""
        try:
            self.ws.restore()
            return True
        except Exception as e:  # WorkspaceError 及测试替身故障都必须失败关闭
            out.status = DIRTY
            out.abort_reason = f"{type(e).__name__}: {e}"[:200]
            out.phase = "restore"
            return False

    def _finish(self, anchor: anchors.Anchor, iv: Intervention,
                out: TrialOutcome, restored_clean: bool | None) -> TrialOutcome:
        out = self._record(anchor, iv, out, restored_clean)
        if out.status == DIRTY:
            raise TrialRestoreDirty(out.abort_reason, out)
        return out

    # ------------------------------------------------------------ 干预

    def observe(self, anchor: anchors.Anchor, which: str, *, deadline: float,
                junit_name: str) -> tuple[Optional[TestVector], str]:
        """不做干预，只观测一次当前状态。

        承重证书要求"回滚之后再跑一次、向量重新与基线逐项一致"，
        那一次跑发生在**还原之后**，所以它不是干预，是独立观测。
        把它硬塞进 delete_lines 会让预算记账的时序变掉。
        """
        try:
            try:
                v = self._run(junit_name, deadline)
            except TestRangeError:
                v = None
        finally:
            try:
                self.ws.restore()
            except Exception as e:
                raise TrialRestoreDirty(f"{type(e).__name__}: {e}") from e
        return v, self._vector_fact(anchor, which, v)

    def delete_lines(self, path: str, lines: tuple[int, ...], *,
                     anchor: anchors.Anchor, deadline: float,
                     junit_name: str,
                     on_spend: Callable[[], None] | None = None) -> TrialOutcome:
        """把若干行置空（保行号），跑声明范围，**一律还原**。"""
        t0 = time.time()
        try:
            m, _original = mutate.apply_to(self.ws, path, lines)
        except mutate.InvalidTransform:
            # 没有形成合法变换，连子进程都没起，不计入预算
            iv = Intervention(records.X_DELETE_UNIT, path, lines,
                              describe="删除后语法不合法，未执行")
            return self._finish(anchor, iv,
                                TrialOutcome(status=INVALID), None)
        iv = Intervention(records.X_DELETE_UNIT, path, tuple(m.deleted),
                          describe=Transform_describe(m))
        out = TrialOutcome(status=RAN, mutation=m)
        try:
            out.vector = self._run(junit_name, deadline)
        except TestRangeError as e:
            out.status = _observation_failure(e)
            out.abort_reason = str(e)[:150]
            out.phase = "test_run"
        finally:
            out.seconds = time.time() - t0
            if on_spend:
                on_spend()
        clean = self._restore(out)
        return self._finish(anchor, iv, out, clean)

    def remove_file(self, path: str, *, anchor: anchors.Anchor, deadline: float,
                    junit_name: str, stash_dir: Path,
                    during: Callable[[], str | DuringObservation | None] | None = None
                    ) -> TrialOutcome:
        """把整个文件临时挪走，跑声明范围，**无论判成什么都放回去**。

        `during` 在文件已挪走、测试尚未跑的时刻执行；返回非空字符串即中止。
        游离用它做"移除后收集清单必须完全一致"这一条判据。
        """
        t0 = time.time()
        src = self.ws.path / path
        iv = Intervention(records.X_DELETE_UNIT, path, file_removed=True,
                          describe="临时移除整个文件")
        if not src.exists():
            out = TrialOutcome(status=INVALID, abort_reason="工作树里找不到该文件")
            return self._finish(anchor, iv, out, None)

        src.unlink()
        out = TrialOutcome(status=RAN)
        try:
            if during is not None:
                observed = during()
                if isinstance(observed, DuringObservation):
                    out.during_result = observed
                    out.during_fact_ids = observed.fact_ids
                    reason = observed.reason
                else:
                    reason = observed or ""
                if reason:
                    # 中止：不跑测试，但**不能在这里就写账本**——
                    # 早先这里直接 return self._record(...)，于是记录在 finally
                    # 更新 seconds 之前就构造好了，账本里永远是 cost_s = 0.0。
                    # 返回的对象后来被 finally 改对了，所以外部看着正常，
                    # 账本却是错的。这正是最难发现的那类错误。
                    out.status, out.abort_reason = ABORTED, reason
                    out.phase = "pre_test"
            if out.status == RAN:
                try:
                    out.vector = self._run(junit_name, deadline)
                except TestRangeError as e:
                    out.status = _observation_failure(e)
                    out.abort_reason = str(e)[:150]
                    out.phase = "test_run"
        finally:
            out.seconds = time.time() - t0
        # 文件与行都只由下面这一条权威协议恢复；不先手工写回再 reset 两遍。
        clean = self._restore(out)
        return self._finish(anchor, iv, out, clean)


def Transform_describe(m: mutate.Mutation) -> str:
    s = f"删除 {len(m.deleted)} 行（置空，保持行号）"
    if m.pass_inserted_at is not None:
        s += f"；父代码块被清空，在第 {m.pass_inserted_at} 行补 pass"
    return s
