"""统一后台作业服务（总计划 T03，契约 ``docs/interfaces/jobs.md``）。

候选生成、隔离验证、复验与领域实验统一通过 :class:`JobService` 创建后台作业：
立即返回作业编号；状态、事件与产物持久化在 manager root 的 ``jobs/`` 目录，
网页刷新与服务重启后可重新读取；服务重启时 running 作业先核对实际文件状态，
再标记 ``interrupted`` 或 ``needs_recovery``，禁止盲目重放写入。

设计纪律
- 存储参照 TaskService：每作业一个目录 + 原子 JSON（``_atomic_json``）+ 追加式事件日志。
- 进程组管理与崩溃恢复复用 ``modou/executor.py`` 的进程记录（executor-process-v1）
  与身份核对纪律；本模块按同 schema 写记录以便 ``recover_process_record`` 接管。
- 预算是任务共享账本（预留-结算）：失败/重试/取消不重置已结算额度，取消结算实际用量。
- HTTP 面只接受服务端注册的作业 kind；``runner`` 参数仅供服务端代码使用，不经 HTTP 暴露。

接线要求（集成人，接入 app.py）
1. ``from modou.server.jobs import JobService, register as register_jobs``；
   ``jobs = JobService(manager, runners={...})``；``register_jobs(app, jobs)``。
2. 建议在既有 IntakeError 处理器中把 ``JOB_NOT_FOUND`` 映射为 404。
3. ``run_subprocess_job`` 的 payload.argv 来自作业 payload：只可为服务端自行构造
   payload 的 kind 注册该 runner，不得把任意客户端命令直接映射到它（安全边界）。
4. 任务共享预算由任务侧（T01/T10）通过 ``create_budget`` 建立，作业携带 ``budget_id`` 归属。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

# executor 的进程记录/身份核对是既有纪律；这里按同 schema 复用（私有纯函数，
# 不复制实现以免两套口径漂移）。
from modou.executor import (_process_identity, _process_is_zombie,
                            _write_process_record, recover_process_record,
                            sanitized_environment)
from modou.server.control import IntakeError, _atomic_json, _read_json

__all__ = [
    "JobCancelled", "JobTimeout", "JobBudgetExhausted", "JobContext",
    "JobService", "run_subprocess_job", "build_router", "register",
]

SCHEMA_VERSION = "master-2026-09"
BUDGET_SCHEMA_VERSION = "master-2026-09-budget-v1"

# interrupted/needs_recovery 是重启后的等待处置终态；stopping 是取消请求已
# 写入、但归属工作尚未确认退出的过渡态，不能被展示成已停止。
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled",
                               "interrupted", "needs_recovery"})
# 去重只对在途作业生效；终态后同 key 重新投递创建新作业。
DEDUPE_ACTIVE = frozenset({"queued", "running"})
JOB_ID_RE = re.compile(r"job-[a-f0-9]{12}")
KIND_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
BUDGET_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

DEFAULT_CANCEL_GRACE_SECONDS = 5.0
DEFAULT_BUDGET_RESERVE = 60.0
MAX_PAYLOAD_BYTES = 128 * 1024
MAX_OUTPUT_CHARS = 4000

_SNAPSHOT_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv"})
_SNAPSHOT_MAX_FILES = 4000
_SNAPSHOT_MAX_FILE_BYTES = 8 << 20


class JobCancelled(Exception):
    """作业体收到取消请求并已终止其进程组。"""


class JobTimeout(Exception):
    """作业超过声明的 timeout 被终止。"""


class JobBudgetExhausted(Exception):
    """作业耗尽其预留预算被终止。"""


def _fail(code: str, detail: str):
    raise IntakeError(code, detail)


def _bounded_text(value, name: str, maximum: int = 200) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail("JOB_REQUEST_INVALID", f"{name} must be a nonempty bounded string")
    return value.strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_write_scope(roots) -> dict[str, str]:
    """对 write_scope 做内容快照（作业运行前），用于重启后核对部分写入痕迹。"""
    snapshot: dict[str, str] = {}
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            continue
        stack = [root]
        while stack and len(snapshot) < _SNAPSHOT_MAX_FILES:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                if len(snapshot) >= _SNAPSHOT_MAX_FILES:
                    break
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SNAPSHOT_SKIP_DIRS:
                            stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    if stat.st_size > _SNAPSHOT_MAX_FILE_BYTES:
                        snapshot[str(path)] = f"size:{stat.st_size}"
                    else:
                        snapshot[str(path)] = _sha256_file(path)
                except OSError:
                    continue
    return snapshot


def write_traces_changed(baseline: dict[str, str], roots) -> list[str]:
    """对比快照与当前文件状态；新增/变更/删除都算部分写入痕迹。"""
    current = snapshot_write_scope(roots)
    changed = [path for path, value in current.items() if baseline.get(path) != value]
    changed.extend(path for path in baseline if path not in current)
    return changed[:50]


class JobContext:
    """作业体的执行面：写入只经服务端的原子接口，取消经 cancel_event 协作。"""

    def __init__(self, service: "JobService", job_id: str, payload: dict,
                 artifact_dir: Path, cancel_event: threading.Event,
                 budget_exhausted: Callable[[], bool] | None, *,
                 task_id: str = "", round_id: str = "", criterion_id: str = "",
                 currency: str = "seconds", budget_id: str = "",
                 budget_reserved: float = 0.0):
        self.service = service
        self.job_id = job_id
        self.payload = payload
        self.artifact_dir = Path(artifact_dir)
        self.cancel_event = cancel_event
        self.budget_exhausted = budget_exhausted
        # 服务端确定并持久化的作业身份（N03）：作业体从这里取 task/round，
        # 不信任 payload 里客户端可伪造的同名字段。
        self.task_id = task_id
        self.round_id = round_id
        self.criterion_id = criterion_id
        self.currency = currency
        # 作业体可核对“这份作业真的绑定了它声称的预算账本”。
        self.budget_id = budget_id
        self.budget_reserved = budget_reserved
        # currency=units 时由作业体申报实际用量；seconds 由服务端计时。
        self.usage_units: float | None = None

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        status = "done"
        self.service._phase_start(self.job_id, name)
        try:
            yield
        except JobCancelled:
            status = "cancelled"
            raise
        except BaseException:
            status = "failed"
            raise
        finally:
            self.service._phase_end(self.job_id, name, status)

    def declare_artifact(self, name: str) -> None:
        self.service._declare_artifact(self.job_id, name)

    def write_artifact(self, name: str, data: bytes) -> None:
        target = self.artifact_dir / name
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        self.declare_artifact(name)


class JobService:
    """后台作业：创建/查询/事件/取消、状态机、持久化、去重串行与共享预算。"""

    _CREATE_FIELDS = frozenset({"kind", "payload", "task_id", "round_id",
                                "criterion_id", "dedupe_key", "budget_id",
                                "repo_write_lock", "trigger_source"})
    #: 服务内部作业类型：只能由拥有它的服务经授权预留事务投递，
    #: HTTP /api/v2/jobs 一律拒绝；create() 仍供服务端代码使用。
    INTERNAL_KINDS = frozenset({"delegation_check"})

    def __init__(self, manager=None, *, root=None, runners=None,
                 cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
                 default_budget_reserve: float = DEFAULT_BUDGET_RESERVE,
                 allowed_cwd_roots=None):
        self.manager = manager
        if root is None:
            if manager is None:
                _fail("JOB_SERVICE_INVALID", "root or manager is required")
            root = Path(manager.root) / "jobs"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._budgets_root = self.root / "_budgets"
        self._budgets_root.mkdir(exist_ok=True)
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.default_budget_reserve = float(default_budget_reserve)
        self.allowed_cwd_roots = (None if allowed_cwd_roots is None
                                  else tuple(Path(p) for p in allowed_cwd_roots))
        self._runners: dict[str, Callable[[JobContext], dict]] = dict(runners or {})
        # 仅本实例可解析的作业体（服务端代码直接传入的可调用）。
        self._inflight: dict[str, Callable[[JobContext], dict]] = {}
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._pending: list[str] = []
        self._active_locks: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        # 线程句柄不跨进程持久化；作业记录中的 execution_handle 是恢复时的
        # 权威事实，内存句柄只用于本实例在取消时等待归属 worker 退出。
        self._worker_threads: dict[str, threading.Thread] = {}
        self._recover_interrupted_jobs()
        self._dispatcher = threading.Thread(target=self._dispatch_loop,
                                            name="jobs-dispatcher", daemon=True)
        self._dispatcher.start()

    # ---- runner 注册 -------------------------------------------------

    def register_runner(self, kind: str, runner: Callable[[JobContext], dict]) -> None:
        self._runners[str(kind)] = runner

    def _resolve_runner(self, record: dict):
        runner = self._inflight.get(record.get("job_id", ""))
        if runner is not None:
            return runner
        return self._runners.get(record.get("runner_ref") or "")

    # ---- 预算账本 ----------------------------------------------------

    def create_budget(self, budget_id: str = "", *, total: float,
                      currency: str = "seconds") -> dict:
        if isinstance(total, bool) or not isinstance(total, (int, float)) \
                or not 0 < float(total) <= 10_000_000:
            _fail("JOB_BUDGET_INVALID", "total must be a positive number")
        if currency not in {"seconds", "units"}:
            _fail("JOB_BUDGET_INVALID", "currency must be seconds or units")
        if not budget_id:
            budget_id = "budget-" + secrets.token_hex(6)
        budget_id = str(budget_id)
        if not BUDGET_ID_RE.fullmatch(budget_id):
            _fail("JOB_BUDGET_INVALID", "budget_id must match [A-Za-z0-9._-]{1,64}")
        with self._lock:
            path = self._budgets_root / f"{budget_id}.json"
            existing = _read_json(path)
            if existing:
                if existing.get("total") != float(total) or existing.get("currency") != currency:
                    _fail("JOB_BUDGET_CONFLICT", "budget exists with a different envelope")
                return dict(existing)
            record = {"schema_version": BUDGET_SCHEMA_VERSION, "budget_id": budget_id,
                      "total": float(total), "reserved": 0.0, "settled": 0.0,
                      "currency": currency, "created_at": time.time(),
                      "updated_at": time.time()}
            _atomic_json(path, record)
            return dict(record)

    def budget(self, budget_id: str) -> dict:
        with self._lock:
            record = _read_json(self._budget_path(str(budget_id)))
        if not record:
            _fail("JOB_BUDGET_UNKNOWN", f"budget {budget_id} is not registered")
        return record

    def _budget_path(self, budget_id: str) -> Path:
        return self._budgets_root / f"{budget_id}.json"

    def _budget_try_reserve(self, budget_id: str, amount: float) -> bool:
        # 调用方持有 self._lock
        record = _read_json(self._budget_path(budget_id))
        if not record:
            return False
        available = record["total"] - record["settled"] - record["reserved"]
        if amount > available:
            return False
        record["reserved"] = round(record["reserved"] + amount, 3)
        record["updated_at"] = time.time()
        _atomic_json(self._budget_path(budget_id), record)
        return True

    def _budget_settle(self, budget_id: str, reserved_amount: float, actual: float) -> None:
        # 调用方持有 self._lock；结算实际用量并释放预留。
        record = _read_json(self._budget_path(budget_id))
        if not record:
            return
        record["reserved"] = round(max(0.0, record["reserved"] - reserved_amount), 3)
        record["settled"] = round(record["settled"] + max(0.0, actual), 3)
        record["updated_at"] = time.time()
        _atomic_json(self._budget_path(budget_id), record)

    # ---- 路径与持久化 ------------------------------------------------

    def _job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _events_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "events.jsonl"

    def _artifacts_dir(self, job_id: str) -> Path:
        directory = self._job_dir(job_id) / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _process_record_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "artifacts" / "process.runtime.json"

    def _persist(self, job_id: str, record: dict) -> None:
        _atomic_json(self._job_path(job_id), record)

    def _append_event(self, job_id: str, kind: str, details: dict) -> None:
        with self._lock:
            path = self._events_path(job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            last = 0
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        last = max(last, int(json.loads(line).get("seq", 0)))
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
            entry = {"seq": last + 1, "kind": kind, "at": time.time(),
                     "details": details if isinstance(details, dict) else {}}
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    @staticmethod
    def _public(record: dict) -> dict:
        return {key: value for key, value in record.items() if key != "write_baseline"}

    def _require_job_id(self, job_id: str) -> str:
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
            _fail("JOB_NOT_FOUND", "unknown job")
        return job_id

    # ---- 重启恢复 ----------------------------------------------------

    def _recover_interrupted_jobs(self) -> None:
        """启动扫描：running 先杀可核对的孤儿进程组、核对写入痕迹后落终态。

        只读持久记录并核对实际文件状态；绝不重放任何写入（恢复动作由上层
        依据 needs_recovery 记录单独发起）。
        """
        for job_dir in sorted(self.root.glob("job-*")):
            if not job_dir.is_dir():
                continue
            record = _read_json(job_dir / "job.json")
            if not record:
                continue
            status = record.get("status")
            if status in {"running", "stopping"}:
                process_record = job_dir / "artifacts" / "process.runtime.json"
                outcome = (recover_process_record(process_record)
                           if process_record.exists() else "none")
                traces = write_traces_changed(record.get("write_baseline") or {},
                                              record.get("write_scope") or [])
                record["recovery"] = {"scanned_at": time.time(),
                                      "required": bool(status == "stopping" or traces),
                                      "process_record_outcome": outcome,
                                      "partial_write_traces": traces}
                record["status"] = "needs_recovery" if (status == "stopping" or traces) else "interrupted"
                record["termination_state"] = "unconfirmed" if record["status"] == "needs_recovery" else "confirmed"
                record.setdefault("termination", {})["worker_alive"] = False
                record["ended_at"] = time.time()
                self._persist(record["job_id"], record)
                self._append_event(record["job_id"], record["status"],
                                   {"process_record_outcome": outcome,
                                    "partial_write_traces": traces})
            elif status == "queued":
                if record.get("runner_ref") in self._runners:
                    self._pending.append(record["job_id"])
                    self._append_event(record["job_id"], "restart.redispatched", {})
                else:
                    record["restart_note"] = ("runner unavailable in this instance; "
                                              "job stays queued")
                    self._persist(record["job_id"], record)
                    self._append_event(record["job_id"], "restart.deferred", {})

    # ---- 创建 --------------------------------------------------------

    def register_runner(self, kind: str, runner: Callable[[JobContext], dict]) -> None:
        """运行期注册作业体（构造顺序晚于 JobService 的服务，如持续委托）。

        同时把重启扫描时因 runner 未就绪而 deferred 的同类排队作业重新入队；
        执行前的授权核验由作业体自己负责，重排不越权。
        """
        kind = _bounded_text(kind, "kind", 64)
        with self._lock:
            self._runners[kind] = runner
            for path in sorted(self.root.glob("job-*/job.json")):
                record = _read_json(path)
                if (not record or record.get("kind") != kind
                        or record.get("status") != "queued"):
                    continue
                if record.pop("restart_note", None) is not None:
                    self._persist(record["job_id"], record)
                    self._pending.append(record["job_id"])
                    self._append_event(record["job_id"], "restart.redispatched", {})
            self._cv.notify_all()

    def create(self, kind, payload, *, task_id="", round_id="", criterion_id="",
               dedupe_key="", budget_id="", repo_write_lock="", trigger_source="",
               runner: Callable[[JobContext], dict] | None = None) -> dict:
        """创建作业并立即返回作业记录（含 job_id 与 status）。

        ``runner`` 只供服务端代码传入作业体；HTTP 面只能引用已注册的 kind。
        """
        kind = _bounded_text(kind, "kind", 64)
        if not kind or not KIND_RE.fullmatch(kind):
            _fail("JOB_REQUEST_INVALID", "kind must match [a-z][a-z0-9_.-]{0,63}")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            _fail("JOB_REQUEST_INVALID", "payload must be an object")
        try:
            blob = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            _fail("JOB_REQUEST_INVALID", "payload must be JSON-serializable")
        if len(blob.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            _fail("JOB_REQUEST_INVALID", "payload too large")
        task_id = _bounded_text(task_id, "task_id")
        round_id = _bounded_text(round_id, "round_id")
        criterion_id = _bounded_text(criterion_id, "criterion_id")
        dedupe_key = _bounded_text(dedupe_key, "dedupe_key")
        budget_id = _bounded_text(budget_id, "budget_id", 64)
        repo_write_lock = _bounded_text(repo_write_lock, "repo_write_lock")
        trigger_source = _bounded_text(trigger_source, "trigger_source")
        if kind == "adoption_apply" and not trigger_source:
            _fail("JOB_TRIGGER_SOURCE_REQUIRED",
                  "adoption_apply must carry trigger_source (loop suppression)")
        with self._lock:
            resolved = runner if runner is not None else self._runners.get(kind)
            if resolved is None:
                _fail("JOB_RUNNER_UNKNOWN", f"kind {kind} has no registered runner")
            # N03：去重身份由服务端绑定。客户端传来的 dedupe_key 一律追加
            # 服务端已校验的 task_id/round_id 再参与去重与落盘——相同 key
            # 但不同任务或不同轮次的投递不会错误合并，旧客户端也无法通过
            # 复用 key 把两个轮次的作业并成一个。
            effective_key = dedupe_key
            if dedupe_key and task_id:
                effective_key += f"|task:{task_id}"
            if dedupe_key and round_id:
                effective_key += f"|round:{round_id}"
            if dedupe_key:
                for existing in self._scan_dedupe(effective_key):
                    same = (existing.get("kind") == kind and
                            json.dumps(existing.get("payload"), ensure_ascii=False,
                                       sort_keys=True) == json.dumps(
                                           payload, ensure_ascii=False, sort_keys=True))
                    if not same:
                        _fail("JOB_DEDUPE_CONFLICT",
                              "dedupe_key reused with different content")
                    return self._public(existing)
            budget = {"budget_id": "", "reserved": 0.0, "settled": 0.0,
                      "currency": "seconds"}
            reserve_amount = 0.0
            exhausted = False
            if budget_id:
                ledger = _read_json(self._budget_path(budget_id))
                if not ledger:
                    _fail("JOB_BUDGET_UNKNOWN", f"budget {budget_id} is not registered")
                currency = ledger.get("currency", "seconds")
                estimate_key = "budget_seconds" if currency == "seconds" else "budget_units"
                estimate = payload.get(estimate_key, self.default_budget_reserve)
                if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) \
                        or not 0.1 <= float(estimate) <= 3600:
                    _fail("JOB_BUDGET_INVALID",
                          f"payload.{estimate_key} must be within 0.1..3600")
                reserve_amount = float(estimate)
                budget.update(budget_id=budget_id, reserved=reserve_amount,
                              currency=currency)
                exhausted = not self._budget_try_reserve(budget_id, reserve_amount)
            # 契约：job_id 形如 job-<hex12>。
            job_id = "job-" + secrets.token_hex(6)
            write_scope = self._validated_write_scope(payload)
            record = {
                "job_id": job_id, "schema_version": SCHEMA_VERSION, "kind": kind,
                "status": "queued", "created_at": time.time(),
                "started_at": 0.0, "ended_at": 0.0,
                "task_id": task_id, "round_id": round_id, "criterion_id": criterion_id,
                "budget": budget, "dedupe_key": effective_key,
                "repo_write_lock": repo_write_lock, "trigger_source": trigger_source,
                "phases": [], "stop_reason": "",
                "result_ref": f"jobs/{job_id}/result.json", "artifacts": [],
                "error": {"code": "", "detail": ""},
                "payload": payload,
                "runner_ref": kind if kind in self._runners else "",
                "write_scope": write_scope, "write_baseline": {},
                "cancel_requested": False, "cancel_result": {},
                "termination_state": "none",
                "termination": {"requested_at": 0.0, "observed_at": 0.0,
                                "terminated_at": 0.0, "worker_alive": False,
                                "process": {}},
                "execution_handle": {"worker_ident": 0,
                                      "worker_name": "",
                                      "process_record": str(self._process_record_path(job_id)),
                                      "started_at": 0.0},
                "recovery": {"required": False},
                "budget_settled_at": 0.0,
            }
            if runner is not None:
                self._inflight[job_id] = runner
            self._persist(job_id, record)
            self._append_event(job_id, "queued",
                               {"kind": kind, "repo_write_lock": repo_write_lock})
            if budget_id and not exhausted:
                self._append_event(job_id, "budget_reserved",
                                   {"budget_id": budget_id, "amount": reserve_amount})
            if exhausted:
                # 预算耗尽：作业进入 failed，stop_reason=budget_exhausted；
                # 不创建绕过账本的影子作业。
                record["status"] = "failed"
                record["stop_reason"] = "budget_exhausted"
                record["error"] = {"code": "JOB_BUDGET_EXHAUSTED",
                                   "detail": "task budget had no room to reserve"}
                record["ended_at"] = time.time()
                self._persist(job_id, record)
                self._append_event(job_id, "budget_exhausted",
                                   {"requested_reserve": reserve_amount})
                self._append_event(job_id, "failed",
                                   {"stop_reason": "budget_exhausted"})
            else:
                self._pending.append(job_id)
                self._cv.notify_all()
            return self._public(record)

    def create_from_request(self, raw) -> dict:
        """HTTP POST /api/v2/jobs 的入口：字段白名单校验后转 create。"""
        if not isinstance(raw, dict):
            _fail("JOB_REQUEST_INVALID", "request body must be an object")
        unknown = set(raw) - self._CREATE_FIELDS
        if "kind" not in raw or unknown:
            _fail("JOB_REQUEST_INVALID",
                  "create requires kind (+payload); unknown fields rejected")
        kind = str(raw.get("kind") or "")
        # 服务内部作业类型不接受 HTTP 直创。委托复验只能
        # 经 DelegationService.check 的授权预留事务投递——那里占用次数、绑定
        # 专属预算并登记快照身份；通用作业接口直创会绕过全部三项。
        if kind in self.INTERNAL_KINDS:
            _fail("JOB_KIND_INTERNAL",
                  f"kind {kind} is dispatched internally by its owning service; "
                  "use the service's own authorized entry")
        payload = raw.get("payload", {})
        return self.create(raw["kind"], payload, task_id=raw.get("task_id", ""),
                           round_id=raw.get("round_id", ""),
                           criterion_id=raw.get("criterion_id", ""),
                           dedupe_key=raw.get("dedupe_key", ""),
                           budget_id=raw.get("budget_id", ""),
                           repo_write_lock=raw.get("repo_write_lock", ""),
                           trigger_source=raw.get("trigger_source", ""))

    def _validated_write_scope(self, payload: dict) -> list[str]:
        scope = payload.get("write_scope")
        if scope in (None, []):
            return []
        if not isinstance(scope, list) or len(scope) > 4:
            _fail("JOB_REQUEST_INVALID",
                  "write_scope must be a list of at most 4 directory paths")
        resolved = []
        for entry in scope:
            path = Path(str(entry)).expanduser()
            if not path.is_dir():
                _fail("JOB_REQUEST_INVALID",
                      f"write_scope entry is not a directory: {entry}")
            resolved.append(str(path.resolve()))
        return resolved

    def _scan_dedupe(self, dedupe_key: str):
        # 调用方持有 self._lock
        for path in sorted(self.root.glob("job-*/job.json")):
            record = _read_json(path)
            if record and record.get("dedupe_key") == dedupe_key \
                    and record.get("status") in DEDUPE_ACTIVE:
                yield record

    # ---- 查询 --------------------------------------------------------

    def get(self, job_id: str) -> dict:
        job_id = self._require_job_id(job_id)
        with self._lock:
            record = _read_json(self._job_path(job_id))
        if not record:
            _fail("JOB_NOT_FOUND", job_id)
        return self._public(record)

    def list(self, task_id: str = "", status: str = "") -> dict:
        jobs = []
        with self._lock:
            for path in sorted(self.root.glob("job-*/job.json")):
                record = _read_json(path)
                if not record:
                    continue
                if task_id and record.get("task_id") != task_id:
                    continue
                if status and record.get("status") != status:
                    continue
                jobs.append(self._public(record))
        jobs.sort(key=lambda item: (item.get("created_at", 0), item.get("job_id", "")))
        return {"jobs": jobs}

    def events(self, job_id: str, since_seq: int = 0) -> dict:
        job_id = self._require_job_id(job_id)
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            _fail("JOB_EVENT_CURSOR_INVALID", "since_seq must be a non-negative integer")
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record:
                _fail("JOB_NOT_FOUND", job_id)
            entries = []
            path = self._events_path(job_id)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("seq", 0) > since_seq:
                        entries.append(entry)
        entries.sort(key=lambda item: item.get("seq", 0))
        return {"job_id": job_id, "status": record.get("status"),
                "events": entries,
                "last_seq": entries[-1]["seq"] if entries else since_seq}

    # ---- 调度与执行 --------------------------------------------------

    def _dispatch_loop(self) -> None:
        while True:
            with self._cv:
                picked = None
                picked_lock = ""
                while picked is None:
                    for job_id in list(self._pending):
                        record = _read_json(self._job_path(job_id))
                        lock = (record or {}).get("repo_write_lock") or ""
                        # 同 repo_write_lock 串行：创建顺序即同锁 FIFO。
                        if not lock or lock not in self._active_locks:
                            picked, picked_lock = job_id, lock
                            break
                    if picked is None:
                        self._cv.wait()
                self._pending.remove(picked)
                if picked_lock:
                    self._active_locks.add(picked_lock)
            threading.Thread(target=self._run_job, args=(picked,), daemon=True,
                             name=f"job-{picked}").start()

    def _run_job(self, job_id: str) -> None:
        self._worker_threads[job_id] = threading.current_thread()
        try:
            self._execute(job_id)
        except BaseException:  # 防御：作业体外的崩溃也必须落终态
            self._finalize(job_id, "failed", "error",
                           {"code": "JOB_WORKER_CRASH",
                            "detail": "job worker crashed outside the runner"})
        finally:
            with self._cv:
                record = _read_json(self._job_path(job_id))
                lock = (record or {}).get("repo_write_lock") or ""
                if lock:
                    self._active_locks.discard(lock)
                self._cancel_events.pop(job_id, None)
                self._inflight.pop(job_id, None)
                self._worker_threads.pop(job_id, None)
                self._cv.notify_all()

    def _execute(self, job_id: str) -> None:
        record = _read_json(self._job_path(job_id))
        if not record:
            return
        runner = self._resolve_runner(record)
        started = time.monotonic()
        cancel_event = threading.Event()
        with self._cv:
            self._cancel_events[job_id] = cancel_event
            if record.get("cancel_requested"):
                cancel_event.set()
            # A cancellation can win the race between dispatch and worker start.
            # Preserve stopping instead of resurrecting the job as running.
            if record.get("status") != "stopping":
                record["status"] = "running"
            record["started_at"] = time.time()
            record["execution_handle"] = {
                **(record.get("execution_handle") or {}),
                "worker_ident": threading.get_ident(),
                "worker_name": threading.current_thread().name,
                "process_record": str(self._process_record_path(job_id)),
                "started_at": record["started_at"],
            }
            record["termination"] = {
                **(record.get("termination") or {}),
                "worker_alive": True,
            }
            if record.get("write_scope"):
                record["write_baseline"] = snapshot_write_scope(record["write_scope"])
            self._persist(job_id, record)
        self._append_event(job_id, "running", {})
        if cancel_event.is_set():
            self._finalize(job_id, "cancelled", "user_cancel",
                           {"code": "JOB_CANCELLED", "detail": "cancelled before runner start"},
                           usage=0.0)
            return
        if runner is None:
            self._finalize(job_id, "failed", "error",
                           {"code": "JOB_RUNNER_UNAVAILABLE",
                            "detail": "runner missing in this instance"})
            return
        budget = record.get("budget") or {}
        currency = budget.get("currency", "seconds")
        reserved = float(budget.get("reserved") or 0.0)
        exhausted_probe = None
        if budget.get("budget_id") and currency == "seconds" and reserved > 0:
            # 预留额度同时是运行上限：超出即预算耗尽终止。
            exhausted_probe = (lambda started=started, reserved=reserved:
                               time.monotonic() - started > reserved)
        ctx = JobContext(self, job_id, record.get("payload") or {},
                         self._artifacts_dir(job_id), cancel_event, exhausted_probe,
                         task_id=record.get("task_id", ""),
                         round_id=record.get("round_id", ""),
                         criterion_id=record.get("criterion_id", ""),
                         currency=currency,
                         budget_id=str(budget.get("budget_id") or ""),
                         budget_reserved=reserved)
        try:
            result = runner(ctx)
            usage = (float(ctx.usage_units)
                     if currency == "units" and ctx.usage_units is not None
                     else time.monotonic() - started)
            # A cooperative runner may return just after cancellation.  The
            # cancellation event wins; do not publish a late success result.
            if cancel_event.is_set() or self._record_status(job_id) in {"stopping", "needs_recovery"}:
                self._finalize(job_id, "cancelled", "user_cancel",
                               {"code": "JOB_CANCELLED", "detail": "cancelled before result publication"},
                               usage=usage)
                return
            if isinstance(result, dict):
                _atomic_json(self._job_dir(job_id) / "result.json",
                             {"schema_version": SCHEMA_VERSION, "job_id": job_id,
                              "result": result})
            self._finalize(job_id, "completed", "", usage=usage)
        except JobCancelled as exc:
            self._finalize(job_id, "cancelled", "user_cancel",
                           {"code": "JOB_CANCELLED", "detail": str(exc)},
                           usage=time.monotonic() - started)
        except JobTimeout as exc:
            self._finalize(job_id, "failed", "timeout",
                           {"code": "JOB_TIMEOUT", "detail": str(exc)},
                           usage=time.monotonic() - started)
        except JobBudgetExhausted as exc:
            self._finalize(job_id, "failed", "budget_exhausted",
                           {"code": "JOB_BUDGET_EXHAUSTED", "detail": str(exc)},
                           usage=time.monotonic() - started)
        except Exception as exc:
            # 作业体抛出的带码异常（IntakeError 等）保留其业务码，便于上层
            # 按 code 而不是按异常类名解释失败原因。
            self._finalize(job_id, "failed", "error",
                           {"code": str(getattr(exc, "code", "") or type(exc).__name__),
                            "detail": str(exc)[:500]},
                           usage=time.monotonic() - started)

    def _finalize(self, job_id: str, status: str, stop_reason: str,
                  error: dict | None = None, usage: float | None = None) -> None:
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record or record.get("status") not in {"queued", "running", "stopping"}:
                return
            current_status = record.get("status")
            if current_status == "stopping" and status == "completed":
                status = "cancelled"
                stop_reason = record.get("stop_reason") or "user_cancel"
                error = error or {"code": "JOB_CANCELLED",
                                  "detail": "cancellation won the completion race"}
            if usage is None:
                usage = (max(0.0, time.time() - record["started_at"])
                         if record.get("started_at") else 0.0)
            record["status"] = status
            record["stop_reason"] = stop_reason
            if error:
                record["error"] = {"code": str(error.get("code", "")),
                                   "detail": str(error.get("detail", ""))[:500]}
            record["ended_at"] = time.time()
            record["termination_state"] = "confirmed"
            termination = record.setdefault("termination", {})
            termination["worker_alive"] = False
            termination["terminated_at"] = record["ended_at"]
            for phase in record.get("phases", []):
                if phase.get("status") == "running":
                    phase["status"] = ("cancelled" if status == "cancelled"
                                       else "failed" if status == "failed" else "done")
                    phase["ended_at"] = record["ended_at"]
            budget = record.get("budget") or {}
            if budget.get("budget_id") and not record.get("budget_settled_at"):
                self._budget_settle(budget["budget_id"],
                                    float(budget.get("reserved") or 0.0), usage)
                # 作业记录保留预留额（历史信息）；账本的 reserved 已释放。
                budget["settled"] = round(max(0.0, usage), 3)
                record["budget_settled_at"] = record["ended_at"]
            self._persist(job_id, record)
            self._append_event(job_id, status, {"stop_reason": stop_reason})
            if budget.get("budget_id"):
                self._append_event(job_id, "budget_settled",
                                   {"settled": budget["settled"],
                                    "budget_id": budget["budget_id"]})

    def _record_status(self, job_id: str) -> str:
        with self._lock:
            return str((_read_json(self._job_path(job_id)) or {}).get("status", ""))

    def _mark_needs_recovery(self, job_id: str, *, stop_reason: str,
                             termination: dict, usage: float | None = None) -> dict:
        """Persist an honest stop when the owned worker outlives the grace.

        This deliberately settles the measured usage but leaves a recovery
        record.  A late worker cannot call ``_finalize`` into a success state.
        """
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record:
                return {}
            if usage is None:
                usage = (max(0.0, time.time() - record.get("started_at", 0.0))
                         if record.get("started_at") else 0.0)
            record["status"] = "needs_recovery"
            record["stop_reason"] = stop_reason
            record["termination_state"] = "unconfirmed"
            record["termination"] = {**(record.get("termination") or {}), **termination,
                                      "worker_alive": bool(termination.get("worker_alive"))}
            record["recovery"] = {"required": True, "recorded_at": time.time(),
                                  "reason": "owned worker did not confirm exit"}
            record["ended_at"] = time.time()
            budget = record.get("budget") or {}
            if budget.get("budget_id") and not record.get("budget_settled_at"):
                self._budget_settle(budget["budget_id"],
                                    float(budget.get("reserved") or 0.0), usage)
                budget["settled"] = round(max(0.0, usage), 3)
                record["budget_settled_at"] = record["ended_at"]
            for phase in record.get("phases", []):
                if phase.get("status") == "running":
                    phase["status"] = "cancelled"
                    phase["ended_at"] = record["ended_at"]
            self._persist(job_id, record)
            self._append_event(job_id, "needs_recovery", {
                "stop_reason": stop_reason,
                "termination": termination,
            })
            if budget.get("budget_id"):
                self._append_event(job_id, "budget_settled",
                                   {"settled": budget["settled"],
                                    "budget_id": budget["budget_id"]})
            return self._public(record)

    # ---- 阶段与产物 --------------------------------------------------

    def _phase_start(self, job_id: str, name: str) -> None:
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record or record.get("status") not in {"running", "stopping"}:
                _fail("JOB_STATE_INVALID", "phases can only be recorded while running or stopping")
            record.setdefault("phases", []).append(
                {"name": str(name)[:100], "started_at": time.time(),
                 "ended_at": 0.0, "status": "running"})
            self._persist(job_id, record)
            self._append_event(job_id, "phase_started", {"phase": name})

    def _phase_end(self, job_id: str, name: str, status: str) -> None:
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record:
                return
            for phase in reversed(record.get("phases", [])):
                if phase.get("name") == name and phase.get("status") == "running":
                    phase["status"] = status
                    phase["ended_at"] = time.time()
                    break
            self._persist(job_id, record)
            self._append_event(job_id, "phase_ended", {"phase": name, "status": status})

    def _declare_artifact(self, job_id: str, name: str) -> None:
        with self._lock:
            path = self._job_dir(job_id) / "artifacts" / name
            if not path.is_file():
                _fail("JOB_ARTIFACT_MISSING", name)
            record = _read_json(self._job_path(job_id))
            if not record:
                return
            entry = {"name": name, "path": f"jobs/{job_id}/artifacts/{name}",
                     "sha256": _sha256_file(path)}
            artifacts = [item for item in record.get("artifacts", [])
                         if item.get("name") != name]
            artifacts.append(entry)
            record["artifacts"] = artifacts
            self._persist(job_id, record)

    # ---- 取消 --------------------------------------------------------

    def cancel(self, job_id: str, *, grace_seconds: float | None = None,
               force_timeout: float = 10.0) -> dict:
        """取消作业：queued 直接落终态；running 先 SIGTERM 进程组、宽限后 SIGKILL。"""
        job_id = self._require_job_id(job_id)
        grace = (self.cancel_grace_seconds if grace_seconds is None
                 else float(grace_seconds))
        with self._lock:
            record = _read_json(self._job_path(job_id))
            if not record:
                _fail("JOB_NOT_FOUND", job_id)
            if record.get("status") in TERMINAL_STATUSES:
                return self._public(record)
            if record.get("status") == "stopping":
                return self._public(record)
            if record.get("status") == "queued":
                if job_id in self._pending:
                    self._pending.remove(job_id)
                record["cancel_requested"] = True
                record["termination_state"] = "confirmed"
                record["termination"] = {**(record.get("termination") or {}),
                                          "requested_at": time.time(),
                                          "observed_at": time.time(),
                                          "terminated_at": time.time(),
                                          "worker_alive": False}
                self._persist(job_id, record)
                self._append_event(job_id, "cancel_requested", {"phase": "queued"})
                self._finalize(job_id, "cancelled", "user_cancel")
                return self._public(_read_json(self._job_path(job_id)))
            requested_at = time.time()
            record["cancel_requested"] = True
            record["status"] = "stopping"
            record["stop_reason"] = "user_cancel"
            record["termination_state"] = "requested"
            record["termination"] = {**(record.get("termination") or {}),
                                      "requested_at": requested_at,
                                      "worker_alive": True}
            self._persist(job_id, record)
            self._append_event(job_id, "cancel_requested", {"phase": "running"})
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        kill = self._terminate_process_group(self._process_record_path(job_id), grace)
        if kill.get("terminated"):
            # 进程组已核对终止，清理本作业的进程记录。
            try:
                self._process_record_path(job_id).unlink(missing_ok=True)
            except OSError:
                pass
        with self._lock:
            record = _read_json(self._job_path(job_id))
            # 作业体可能已先落终态；取消结果仍需补记（幂等）。
            if record and not record.get("cancel_result"):
                record["cancel_result"] = kill
                self._persist(job_id, record)
        deadline = time.monotonic() + max(0.1, grace) + max(0.0, force_timeout)
        while time.monotonic() < deadline:
            with self._lock:
                record = _read_json(self._job_path(job_id)) or {}
                status = record.get("status")
            worker = self._worker_threads.get(job_id)
            worker_alive = bool(worker and worker.is_alive())
            process_alive = bool((self._process_record_path(job_id)).exists())
            if status in TERMINAL_STATUSES:
                break
            if not worker_alive and not process_alive:
                break
            time.sleep(0.05)
        with self._lock:
            record = _read_json(self._job_path(job_id))
            worker = self._worker_threads.get(job_id)
            worker_alive = bool(worker and worker.is_alive())
            process_alive = bool((self._process_record_path(job_id)).exists())
            if record and record.get("status") == "stopping":
                record["cancel_result"] = {**kill, "forced": False}
                termination_record = record.setdefault("termination", {})
                termination_record["process"] = kill
                termination_record["worker_alive"] = worker_alive
                termination_record["observed_at"] = time.time()
                self._persist(job_id, record)
                if worker_alive or process_alive:
                    record = self._mark_needs_recovery(
                        job_id, stop_reason="user_cancel",
                        termination={"process": kill, "worker_alive": worker_alive,
                                     "process_alive": process_alive,
                                     "observed_at": time.time()})
                else:
                    self._finalize(job_id, "cancelled", "user_cancel",
                                   {"code": "JOB_CANCELLED", "detail": "worker terminated"})
                    record = _read_json(self._job_path(job_id))
        return self._public(record)

    def _terminate_process_group(self, record_path: Path, grace: float) -> dict:
        """SIGTERM 进程组 -> 宽限 -> SIGKILL；身份不符绝不误杀（复用 executor 纪律）。"""
        record = _read_json(Path(record_path))
        if record.get("schema_version") != "executor-process-v1":
            return {"terminated": False, "detail": "no live process record"}
        try:
            pid = int(record.get("pid") or 0)
            pgid = int(record.get("process_group_id") or pid)
        except (TypeError, ValueError):
            return {"terminated": False, "detail": "invalid process record"}
        if pid <= 0:
            return {"terminated": False, "detail": "invalid process record"}

        def gone() -> bool:
            if _process_is_zombie(pid):
                return True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return True
            return False

        if gone():
            return {"terminated": True, "already_exited": True, "pid": pid}
        recorded_identity = str(record.get("process_identity") or "")
        if recorded_identity:
            current_identity = _process_identity(pid)
            if not current_identity:
                # 进程在读取身份前退出（如作业体已响应取消先行终止）：
                # 复核存活后按已终止处理，不误报身份不符。
                if gone():
                    return {"terminated": True, "already_exited": True, "pid": pid}
            elif current_identity != recorded_identity:
                return {"terminated": False, "pid": pid,
                        "detail": "pid identity mismatch; not killed"}
        used_kill = False
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + max(0.1, grace)
        while time.monotonic() < deadline:
            if gone():
                return {"terminated": True, "used_sigkill": False, "pid": pid}
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
            used_kill = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if gone():
                return {"terminated": True, "used_sigkill": used_kill, "pid": pid}
            time.sleep(0.05)
        return {"terminated": False, "used_sigkill": used_kill, "pid": pid,
                "detail": "process group did not exit"}


def _graceful_terminate(process: subprocess.Popen, grace: float = 2.0) -> None:
    """对作业子进程执行 SIGTERM->宽限->SIGKILL（进程组，含后代）。"""
    if os.name == "nt":  # pragma: no cover - Windows 不支持进程组
        process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
    try:
        process.wait(timeout=max(0.2, grace))
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":  # pragma: no cover
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()


def run_subprocess_job(ctx: JobContext) -> dict:
    """通用子进程作业体：命令列表 + cwd + 超时 + 进程组终止（演示与测试载体）。

    安全边界：argv 来自作业 payload，因此该 runner 只能由服务端为「payload 由
    服务端构造」的 kind 注册；不得把任意客户端命令直接映射到它。可选的
    ``service.allowed_cwd_roots`` 进一步限制可执行目录。
    """
    payload = ctx.payload
    argv = payload.get("argv")
    if (not isinstance(argv, list) or not 1 <= len(argv) <= 64
            or any(not isinstance(item, str) or not item.strip() or "\x00" in item
                   for item in argv)):
        raise ValueError("JOB_ARGV_INVALID: payload.argv must be a bounded "
                         "list of nonempty strings")
    cwd = Path(str(payload.get("cwd") or ctx.artifact_dir)).expanduser()
    if not cwd.is_dir():
        raise ValueError(f"JOB_CWD_INVALID: {cwd} is not a directory")
    roots = getattr(ctx.service, "allowed_cwd_roots", None)
    if roots is not None and not any(
            cwd.resolve().is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError("JOB_CWD_FORBIDDEN: cwd is outside the allowed roots")
    timeout = payload.get("timeout", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not 1 <= float(timeout) <= 3600:
        raise ValueError("JOB_TIMEOUT_INVALID: payload.timeout must be within 1..3600")
    env = sanitized_environment(payload.get("env") or {})
    process_record = ctx.artifact_dir / "process.runtime.json"
    started = time.monotonic()
    termination = None
    stdout, stderr = b"", b""
    with ctx.phase("subprocess"):
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=False, start_new_session=os.name != "nt")
        _write_process_record(process_record, process, argv)
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    if ctx.cancel_event.is_set():
                        termination = "cancelled"
                        break
                    if time.monotonic() - started > timeout:
                        termination = "timeout"
                        break
                    if ctx.budget_exhausted is not None and ctx.budget_exhausted():
                        termination = "budget_exhausted"
                        break
            if termination is None and ctx.cancel_event.is_set():
                # 竞态兜底：进程组被取消路径终止且在本轮 50ms 轮询内退出时，
                # communicate() 会正常返回；以取消事件为准，不误判 completed。
                termination = "cancelled"
            if termination is not None:
                _graceful_terminate(process, grace=2.0)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=5)
        finally:
            # 取消路径不清理进程记录：取消方需核对进程组终止结果；
            # 其余路径（正常退出/超时/预算）由作业体自行清理。
            if termination != "cancelled":
                try:
                    process_record.unlink(missing_ok=True)
                except OSError:
                    pass
        elapsed = time.monotonic() - started
    stdout_text = (stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (stderr or b"").decode("utf-8", errors="replace")
    ctx.write_artifact("stdout.txt",
                       stdout_text[:1 << 18].encode("utf-8", errors="replace"))
    ctx.write_artifact("stderr.txt",
                       stderr_text[:1 << 18].encode("utf-8", errors="replace"))
    if termination == "cancelled":
        raise JobCancelled(f"process group terminated on request "
                           f"(rc={process.returncode})")
    if termination == "timeout":
        raise JobTimeout(f"timeout after {timeout}s (rc={process.returncode})")
    if termination == "budget_exhausted":
        raise JobBudgetExhausted(
            f"reserved budget exhausted after {elapsed:.1f}s")
    return {"argv": argv, "cwd": str(cwd), "returncode": process.returncode,
            "elapsed_seconds": round(elapsed, 3),
            "stdout": stdout_text[:MAX_OUTPUT_CHARS],
            "stderr": stderr_text[:MAX_OUTPUT_CHARS]}


def build_router(service: JobService) -> APIRouter:
    """契约 API 面：/api/v2/jobs（创建/查询/事件轮询/取消）。"""
    router = APIRouter(prefix="/api/v2/jobs")

    @router.post("", status_code=201)
    async def create_job(request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise IntakeError("JOB_REQUEST_INVALID", "invalid JSON") from exc
        return await run_in_threadpool(service.create_from_request, raw)

    @router.get("")
    async def list_jobs(task_id: str = "", status: str = ""):
        return await run_in_threadpool(service.list, task_id, status)

    @router.get("/{job_id}")
    async def get_job(job_id: str):
        return await run_in_threadpool(service.get, job_id)

    @router.get("/{job_id}/events")
    async def job_events(job_id: str, since_seq: int = 0):
        return await run_in_threadpool(service.events, job_id, since_seq)

    @router.post("/{job_id}/cancel")
    async def cancel_job(job_id: str):
        return await run_in_threadpool(service.cancel, job_id)

    return router


def register(app, service: JobService):
    """接线到 app.py：app.include_router(build_router(service)) 并暴露 state.jobs。"""
    app.include_router(build_router(service))
    app.state.jobs = service
    return app
