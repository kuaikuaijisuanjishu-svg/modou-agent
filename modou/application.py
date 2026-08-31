"""Stable application entrypoints for the local UI and command line.

The public package exposes three entrypoints:

```text
analyze_patch(AnalysisRequest) -> RunHandle
stream_events(RunHandle)       -> Iterator[RunEvent]
load_run_bundle(run_id)        -> RunBundle
```

The module validates inputs, selects an execution mode, invokes the engine,
and returns serializable results.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Iterator

from . import inputs, paths
from .inputs import InputError, ResolvedInput


class ExecutionMode(str, Enum):
    """执行模式必须显式，且必须写进产物。

    07 的口径纪律：沙箱没有真正生效时，界面必须写「受信任本地仓库模式」，
    不能写「安全执行任意仓库」。把模式做成枚举而不是布尔，
    是为了让"没想过这件事"变成一个类型错误，而不是一个默认值。
    """
    REPLAY = "replay"                  # 只读已有 RunBundle，不执行任何仓库代码
    TRUSTED_LOCAL = "trusted_local"    # 直接执行仓库代码。**只适用于你信任的仓库**
    SANDBOXED = "sandboxed"            # seatbelt 子进程隔离；控制面仍可信

    @property
    def label(self) -> str:
        return {"replay": "离线回放",
                "trusted_local": "受信任本地仓库模式",
                "sandboxed": "沙箱模式"}[self.value]


class RunStatus(str, Enum):
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class AnalysisRequest:
    """一次审查请求。**可序列化**——UI 提交的就是这个，不是一堆位置参数。

    三种来源互斥，`resolve()` 会检查：

    - `repo_path`：用户自己的 Git 仓库（真实用户路径）；
    - `fixture_root`：自足 fixture（回归与录屏）；
    - `instance_id` + `scaffold`：保留字段，仅用于兼容旧调用方。
    """
    # ---- 来源（三选一）
    repo_path: Path | None = None
    patch_file: Path | None = None
    base_commit: str = ""
    fixture_root: Path | None = None
    instance_id: str = ""
    scaffold: str = ""

    # ---- 测试范围。**不由工具替用户猜**：它是三态判据的分母。
    test_files: tuple[str, ...] = ()
    declared_tests: tuple[str, ...] = ()
    python: str = ""

    # ---- 预算与模式
    budget_seconds: float = 300.0
    mode: ExecutionMode = ExecutionMode.TRUSTED_LOCAL

    # ---- Optional agent settings
    goal: str = ""
    model_provider: str = "deterministic"

    # ---- 产物
    out_root: Path | None = None
    three_state: bool = True
    quiet: bool = True

    def _sources(self) -> list[str]:
        return [n for n, v in (("repo_path", self.repo_path),
                               ("fixture_root", self.fixture_root),
                               ("instance_id", self.instance_id)) if v]

    def resolve(self) -> ResolvedInput:
        """把请求变成引擎认识的输入。**不合法就抛，绝不挑一个默认来源跑。**"""
        got = self._sources()
        if len(got) != 1:
            raise InputError(
                f"必须且只能给一种输入来源，收到 {got or '零个'}："
                f"repo_path（你的仓库）/ fixture_root（自足样例）/ instance_id（兼容字段）")
        if self.repo_path:
            return inputs.from_local_repo(
                self.repo_path, test_files=list(self.test_files),
                declared_tests=list(self.declared_tests),
                patch_file=self.patch_file, base_commit=self.base_commit,
                python=self.python or None)
        if self.fixture_root:
            return inputs.from_fixture(Path(self.fixture_root),
                                       self.scaffold or "fixture")
        raise InputError("公开展示包只支持本地仓库或自足 fixture 输入")

    def as_dict(self) -> dict:
        d = {k: (str(v) if isinstance(v, Path) else v)
             for k, v in self.__dict__.items() if v not in (None, "", ())}
        d["mode"] = self.mode.value
        return d


@dataclass(frozen=True)
class RunHandle:
    """一次运行的结果句柄。**失败不会被伪装成成功**——失败带 stage 和原因。"""
    run_id: str
    status: RunStatus
    mode: ExecutionMode
    request: AnalysisRequest
    run_dir: Path | None = None
    report_path: Path | None = None
    summary: dict = field(default_factory=dict)
    failure_stage: str = ""
    failure_detail: str = ""
    _payload: dict = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.COMPLETE

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "status": self.status.value,
                "mode": self.mode.value, "mode_label": self.mode.label,
                "report": str(self.report_path) if self.report_path else "",
                "summary": self.summary,
                "failure": ({"stage": self.failure_stage,
                             "detail": self.failure_detail}
                            if not self.ok else None)}


# ------------------------------------------------------------------ 入口 1

def analyze_patch(req: AnalysisRequest) -> RunHandle:
    """Run one local review synchronously.

    引擎的失败关闭纪律在这里被保留而不是被吞掉：`InstanceFailed` 变成
    `status=FAILED` 的句柄，带上失败阶段。返回一个"看起来正常"的空结果，
    比抛异常危险得多。
    """
    from modou.analysis import InstanceFailed, analyze_resolved
    from .executor import (SandboxedExecutor, TrustedLocalExecutor,
                           execution_scope)

    resolved = req.resolve()          # 不合法直接抛，不进引擎
    run_id = f"{resolved.slug}__{resolved.instance_id}"
    run_dir = (Path(req.out_root) if req.out_root is not None else paths.RUNS) / run_id
    executor = (SandboxedExecutor(run_dir)
                if req.mode is ExecutionMode.SANDBOXED
                else TrustedLocalExecutor())
    try:
        with execution_scope(executor):
            out = analyze_resolved(
                resolved, quiet=req.quiet, three_state=req.three_state,
                out_root=req.out_root, total_budget=req.budget_seconds,
                run_metadata={"execution_mode": req.mode.value,
                              "execution_mode_label": req.mode.label,
                              "input_kind": resolved.kind})
    except InstanceFailed as e:
        return RunHandle(run_id=run_id, status=RunStatus.FAILED, mode=req.mode,
                         request=req, failure_stage=e.stage, failure_detail=e.detail)

    summary = dict(out["summary"])
    report = Path(out["report"])
    payload = json.loads(report.read_text(encoding="utf-8"))
    return RunHandle(run_id=run_id, status=RunStatus.COMPLETE, mode=req.mode,
                     request=req, run_dir=report.parent, report_path=report,
                     summary=summary, _payload=payload)


# ------------------------------------------------------------------ 入口 2

@dataclass(frozen=True)
class RunEvent:
    """One event emitted by a review run.

    刻意不做成自由字典：UI「不读取任意日志猜状态，只根据结构化事件渲染」
    （02 §十），字典会让这条纪律无声地退化。
    """
    kind: str
    run_id: str
    data: dict = field(default_factory=dict)


def stream_events(source: "RunHandle | RunBundle") -> Iterator[RunEvent]:
    """只从序列化报告重建事件；Handle 与离线 Bundle 必须逐项一致。

    The event stream is suitable for replay by the local UI.
    """
    rid = source.run_id
    if isinstance(source, RunHandle) and not source.ok:
        yield RunEvent("run.created", rid, {"mode": source.mode.value,
                                            "mode_label": source.mode.label,
                                            "input_kind": ""})
        yield RunEvent("run.failed", rid, {"stage": source.failure_stage,
                                           "detail": source.failure_detail})
        return
    payload = source._payload if isinstance(source, RunHandle) else source.payload
    s = payload.get("summary") or {}
    yield RunEvent("run.created", rid, {"mode": s.get("execution_mode", ""),
                                        "mode_label": s.get("execution_mode_label", ""),
                                        "input_kind": s.get("input_kind", "")})
    yield RunEvent("baseline.completed", rid,
                   {"seconds": s.get("baseline_seconds"),
                    "declared_tests": s.get("declared_tests")})
    for unit in (payload.get("render_model") or {}).get("units", []):
        location = unit.get("location") or {}
        yield RunEvent("trial.completed", rid,
                       {"unit_id": unit.get("unit_id", ""),
                        "path": location.get("file", ""),
                        "verdict": unit.get("verdict")})
    yield RunEvent("restore.verified", rid, {})
    report = ((source.report_path if isinstance(source, RunHandle)
               else source.run_dir / "report.json"))
    yield RunEvent("run.completed", rid,
                   {"by_label": s.get("by_label"), "h1": s.get("h1"),
                    "seconds": s.get("seconds"), "report": str(report)})


# ------------------------------------------------------------------ 入口 3

@dataclass(frozen=True)
class RunBundle:
    """一份可离线复查的产物。UI 的回放模式读的就是它。"""
    run_id: str
    run_dir: Path
    payload: dict
    ledger_rows: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return self.payload.get("summary", {})

    @property
    def render_model(self) -> dict:
        return self.payload.get("render_model") or {}

    def evidence(self, evidence_id: str) -> dict | None:
        """按 evidence_id 取一条记录。Agent 的 `read_evidence` 工具会走这里。"""
        for r in self.ledger_rows:
            if r.get("record_id") == evidence_id:
                return r
        return None


def load_run_bundle(run_id: str, *, root: Path | None = None) -> RunBundle:
    """按 run_id 或直接按目录加载。**不完整的 bundle 直接拒绝。**"""
    d = Path(run_id)
    if not d.is_dir():
        d = (root or paths.RUNS) / run_id
    if not d.is_dir():
        raise InputError(f"找不到运行目录：{d}")
    report = d / "report.json"
    if not report.exists():
        raise InputError(f"{d.name} 没有 report.json——这不是一份完整产物")

    from . import runroot
    problems = runroot.verify_bundle(d)
    if problems:
        raise InputError(f"{d.name} 的 bundle 校验未通过：{problems[:2]}")

    payload = json.loads(report.read_text(encoding="utf-8"))
    rows: list[dict] = []
    from .ledger import store
    led = d / store.FILENAME
    if led.exists():
        rows = store.read(led)
    return RunBundle(run_id=d.name, run_dir=d, payload=payload, ledger_rows=rows)
