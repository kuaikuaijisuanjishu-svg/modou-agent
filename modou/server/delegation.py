"""持续委托：授权期内按事件维护一条验收要求的依据（职责层最小闭环）。

边界（docs/interfaces/delegation-loop-v1.md 冻结）：
- 不是持续扫描文件的守护进程。检查只由事件带来：任务完成、提交前、
  用户点击、页面/编程入口的显式 POST /delegations/check。GET 永不触发。
- 授权由用户显式创建，额度小而有限（默认"1小时、最多3次复验"）；次数
  上限不是预算替代物，每次投递仍走 JobService 的真实预算账本。
- "委托有效"不等于"验收通过"。作业完成=复验已执行；结论以任务回执为准。
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import threading
import time

from modou.server.control import IntakeError, _atomic_json, _read_json, _repo_snapshot
from modou.server.jobs import JobBudgetExhausted, JobCancelled, JobService
from modou.server.tasks import ReverifyInterrupted, TaskService, digest

SCHEMA_VERSION = "delegation-auth-v1"
AUTH_ID_RE = re.compile(r"auth-[a-f0-9]{24}")
TERMINAL_STATUSES = {"stopped", "needs_reconfirm", "expired", "exhausted"}
#: R2：取消/超预算后等待复验线程在协作边界停下的宽限秒数；超过即放弃
#: 等待，后台延续不再被消费（不进产物、不进授权记录）。
INTERRUPT_GRACE_SECONDS = 10.0


def _fail(code, detail):
    raise IntakeError(code, detail)


class DelegationService:
    """授权记录 + 检查事件 + 复验投递。复用 TaskService/JobService，不复制执行器。"""

    def __init__(self, tasks: TaskService, jobs: JobService):
        self.tasks = tasks
        self.jobs = jobs
        self.root = tasks.manager.root / "_delegations"
        self.root.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._recover()

    # ---- 持久化 ------------------------------------------------------

    def _path(self, auth_id):
        if not isinstance(auth_id, str) or not AUTH_ID_RE.fullmatch(auth_id):
            _fail("DELEGATION_AUTH_UNKNOWN", auth_id or "missing auth_id")
        return self.root / (auth_id + ".json")

    def _load(self, auth_id):
        record = _read_json(self._path(auth_id))
        if not record:
            _fail("DELEGATION_AUTH_UNKNOWN", auth_id)
        return record

    def _save(self, auth, event, details=None):
        events = auth.setdefault("events", [])
        entry = {"seq": len(events) + 1, "kind": event, "at": time.time(),
                 "details": details or {},
                 "previous_sha256": events[-1]["sha256"] if events else ""}
        entry["sha256"] = digest(entry)
        events.append(entry)
        auth["updated_at"] = entry["at"]
        _atomic_json(self._path(auth["auth_id"]), auth)

    def _recover(self):
        """重启恢复：过期授权固化、不复活；预算账本不动、旧回执不翻新。"""
        now = time.time()
        for path in self.root.glob("auth-*.json"):
            auth = _read_json(path)
            if not auth:
                continue
            if auth.get("status") == "active" and now >= auth.get("expires_at", 0):
                auth["status"] = "expired"
                auth["stop_reason"] = "expired"
                self._save(auth, "authorization_expired", {"recovered": True})

    # ---- 派生状态 ------------------------------------------------------

    def _derived(self, auth):
        """读取时按事实推导状态：过期与额度耗尽不等下次写入才成立；
        要求版本变化也只读推导——GET 不投递、不落事件，
        只是不再把"要求已变"的授权显示成仍然有效。"""
        now = time.time()
        status = auth.get("status", "active")
        if status == "active" and now >= auth.get("expires_at", 0):
            status = "expired"
        if status == "active" and auth.get("used_reverify", 0) >= auth.get("max_reverify", 0):
            status = "exhausted"
        requirement_changed = False
        if status == "active":
            try:
                current = self.tasks.get(auth["task_id"])
                if str(current.get("requirement_version", "")) != auth.get("requirement_version"):
                    status = "needs_reconfirm"
                    requirement_changed = True
            except IntakeError:
                pass
        out = dict(auth)
        out["derived_status"] = status
        if requirement_changed:
            out["stop_reason"] = auth.get("stop_reason") or "requirement_changed"
        out["remaining_reverify"] = max(0, auth.get("max_reverify", 0) - auth.get("used_reverify", 0))
        out["seconds_remaining"] = max(0.0, round(auth.get("expires_at", 0) - now, 3))
        if status == "active":
            try:
                budget = self.jobs.budget(auth["budget_id"])
                out["budget_remaining"] = round(
                    budget["total"] - budget["settled"] - budget["reserved"], 3)
            except IntakeError:
                out["budget_remaining"] = 0.0
        return out

    def _public(self, auth):
        public = self._derived(auth)
        public.pop("_lock", None)
        return public

    # ---- 授权（用户显式动作） -------------------------------------------

    def create(self, raw, idempotency_key=""):
        if not isinstance(raw, dict) or not isinstance(raw.get("task_id"), str):
            _fail("DELEGATION_REQUEST_INVALID", "task_id is required")
        duration = raw.get("duration_seconds", 3600)
        max_reverify = raw.get("max_reverify", 3)
        per_reverify = raw.get("per_reverify_seconds", 300)
        for name, value, bounds in (("duration_seconds", duration, (60, 3600)),
                                    ("max_reverify", max_reverify, (1, 3)),
                                    ("per_reverify_seconds", per_reverify, (0.1, 3600))):
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not bounds[0] <= float(value) <= bounds[1]:
                _fail("DELEGATION_REQUEST_INVALID",
                      f"{name} must be within {bounds[0]}..{bounds[1]}")
        task_id = raw["task_id"]
        with self._lock:
            if idempotency_key:
                for prior in self._scan(task_id=task_id):
                    if prior.get("create_key") == idempotency_key:
                        if prior.get("create_sha256") != digest(
                                {k: raw[k] for k in sorted(raw) if k != "task_id"}):
                            _fail("IDEMPOTENCY_CONFLICT",
                                  "delegation create key reused with different content")
                        return self._public(prior)
            for prior in self._scan(task_id=task_id):
                if prior.get("status") == "active" \
                        and time.time() < prior.get("expires_at", 0):
                    _fail("DELEGATION_ALREADY_ACTIVE",
                          f"task {task_id} already has authorization "
                          + prior.get("auth_id", ""))
            # 要求版本由服务端从任务读取，不接受客户端声明
            task = self.tasks.get(task_id)
            requirement_version = str(task.get("requirement_version", ""))
            requirement_round_id = str(task.get("active_round_id", ""))
            if not requirement_version:
                _fail("DELEGATION_REQUEST_INVALID",
                      "task has no requirement version to bind")
            auth_id = "auth-" + secrets.token_hex(12)
            now = time.time()
            budget_id = "deleg-" + auth_id
            # 次数不是预算替代物：授权即建立真实预算账本，作业投递走预留+结算
            self.jobs.create_budget(budget_id,
                                    total=round(float(max_reverify) * float(per_reverify), 3),
                                    currency="seconds")
            auth = {
                "schema_version": SCHEMA_VERSION, "auth_id": auth_id,
                "task_id": task_id,
                "requirement_version": requirement_version,
                "requirement_round_id": requirement_round_id,
                "allowed_actions": ["reverify"],
                "created_at": now, "expires_at": now + float(duration),
                "stopped_at": 0.0,
                "max_reverify": int(max_reverify), "used_reverify": 0,
                "per_reverify_seconds": float(per_reverify),
                "budget_id": budget_id,
                "status": "active", "stop_reason": "",
                "last_check": {}, "last_job_id": "", "last_receipt": {},
                "last_abort": {},
                "dispatched_snapshots": [],
                "create_key": idempotency_key,
                "create_sha256": digest({k: raw[k] for k in sorted(raw) if k != "task_id"}),
            }
            self._save(auth, "authorization.created",
                       {"duration_seconds": float(duration),
                        "max_reverify": int(max_reverify),
                        "requirement_version": requirement_version})
            return self._public(auth)

    def list(self, task_id=""):
        with self._lock:
            rows = [self._public(a) for a in self._scan(task_id=task_id)]
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
        return {"delegations": rows}

    def get(self, auth_id):
        with self._lock:
            return self._public(self._load(auth_id))

    def events(self, auth_id, since_seq=0):
        with self._lock:
            auth = self._load(auth_id)
        rows = [e for e in auth.get("events", []) if e.get("seq", 0) > since_seq]
        return {"auth_id": auth_id, "events": rows}

    def stop(self, auth_id, reason="user_stop"):
        # Do not hold the authorization lock while waiting for a running job.
        # The worker must be able to append ``reverify_aborted`` under the same
        # lock; holding it here would turn a cooperative cancellation into a
        # watchdog timeout and a false needs_recovery.
        with self._lock:
            auth = self._load(auth_id)
            if auth["status"] != "active":
                return self._public(auth)
            auth["status"] = "stopped"
            auth["stop_reason"] = reason if reason in {"user_stop"} else "user_stop"
            auth["stopped_at"] = time.time()
            self._save(auth, "authorization.stop_requested",
                       {"stop_reason": auth["stop_reason"]})
            cancelled = []
            for job in self.jobs.list(task_id=auth["task_id"]).get("jobs", []):
                if job.get("kind") != "delegation_check":
                    continue
                if str((job.get("payload") or {}).get("auth_id")) != auth_id:
                    continue
                if job.get("status") in {"queued", "running"}:
                    cancelled.append(job["job_id"])
        for job_id in cancelled:
            try:
                self.jobs.cancel(job_id)
            except IntakeError:
                pass
        with self._lock:
            auth = self._load(auth_id)
            self._save(auth, "authorization.stopped",
                       {"stop_reason": auth["stop_reason"], "cancelled": cancelled})
            return self._public(auth)

    # ---- 检查事件（唯一投递入口） ---------------------------------------

    def check(self, task_id, reason="event"):
        """检查事件：核验授权与身份，按状态机决定记录、投递或拒绝。

        全程持授权锁：排队时核验（授权/要求/额度/预算）与额度占用是
        原子的，并发重复事件不能绕过次数限制。
        """
        with self._lock:
            rows = sorted(self._scan(task_id=task_id),
                          key=lambda r: r.get("created_at", 0), reverse=True)
            if not rows:
                # 未授权：只可查看，不投递，也不留事件垃圾
                return {"task_id": task_id, "outcome": "no_authorization"}
            auth = rows[0]
            now = time.time()
            if auth.get("status") != "active":
                # 有授权但已终结：如实在 blocked 里给出原因（过期/耗尽/
                # 停止/要求已变），不与"从未授权"混为一谈
                reason_map = {"expired": "expired", "exhausted": "attempt_exhausted",
                              "needs_reconfirm": "requirement_changed",
                              "stopped": "user_stop"}
                return {"task_id": task_id, "auth_id": auth["auth_id"],
                        "outcome": "blocked",
                        "blocked_reason": reason_map.get(auth.get("status"),
                                                         auth.get("status"))}
            if now >= auth.get("expires_at", 0):
                auth["status"] = "expired"
                auth["stop_reason"] = "expired"
                self._save(auth, "authorization_expired", {"reason": reason})
                return {"task_id": task_id, "auth_id": auth["auth_id"],
                        "outcome": "blocked", "blocked_reason": "expired"}
            if auth.get("used_reverify", 0) >= auth.get("max_reverify", 0):
                auth["status"] = "exhausted"
                auth["stop_reason"] = "attempt_exhausted"
                self._save(auth, "attempts_exhausted", {"reason": reason})
                return {"task_id": task_id, "auth_id": auth["auth_id"],
                        "outcome": "blocked", "blocked_reason": "attempt_exhausted"}
            # 要求版本变化：原授权不得静默覆盖新目标。注意：复验会开新轮次
            # （round_id 变化），那不是要求变化；判定只看 requirement_version。
            task = self.tasks.get(task_id)
            current_req = str(task.get("requirement_version", ""))
            if current_req != auth.get("requirement_version"):
                auth["status"] = "needs_reconfirm"
                auth["stop_reason"] = "requirement_changed"
                self._save(auth, "requirement_changed",
                           {"from": auth.get("requirement_version"),
                            "to": current_req, "reason": reason})
                return {"task_id": task_id, "auth_id": auth["auth_id"],
                        "outcome": "blocked", "blocked_reason": "requirement_changed",
                        "requirement_version": current_req}
            needed = self.tasks.reverify_needed(task_id)
            snapshot = self.tasks_active_snapshot(task_id)
            if not needed["needed"]:
                auth["last_check"] = {"at": now,
                                      "snapshot_sha256": snapshot,
                                      "outcome": "no_change"}
                self._save(auth, "check_no_change",
                           {"snapshot_sha256": snapshot, "reason": reason})
                return {"task_id": task_id, "auth_id": auth["auth_id"],
                        "outcome": "no_change",
                        "snapshot_sha256": snapshot}
            # 去重身份含工作树内容快照（不只 HEAD）：同一内容重复事件只执行
            # 一次，也不重复消耗额度。授权记录里维护已投递快照表，检查全程
            # 持锁，顺序与并发重复事件都在这里被拦下；JobService 的内容级
            # 去重是第二道保险。
            dispatched = auth.setdefault("dispatched_snapshots", [])
            for row in dispatched:
                if row.get("snapshot_sha256") == snapshot:
                    return {"task_id": task_id, "auth_id": auth["auth_id"],
                            "outcome": "already_dispatched",
                            "job_id": row.get("job_id", ""),
                            "snapshot_sha256": snapshot}
            dedupe_key = f"delegation:{auth['auth_id']}:{snapshot}"
            auth["used_reverify"] = auth.get("used_reverify", 0) + 1
            # payload 不带 reason：同一快照的作业 payload 必须逐字节一致，
            # JobService 的内容级去重才总能命中而不是报冲突；reason 记在授权事件里。
            job = self.jobs.create(
                "delegation_check",
                {"task_id": task_id, "auth_id": auth["auth_id"],
                 "budget_seconds": auth["per_reverify_seconds"],
                 "snapshot_sha256": snapshot,
                 "requirement_version": auth["requirement_version"]},
                task_id=task_id, dedupe_key=dedupe_key,
                budget_id=auth["budget_id"],
                runner=self._runner)
            dispatched.append({"snapshot_sha256": snapshot,
                               "job_id": job["job_id"]})
            auth["last_check"] = {"at": now, "snapshot_sha256": snapshot,
                                  "outcome": "dispatched"}
            auth["last_job_id"] = job["job_id"]
            self._save(auth, "reverify_dispatched",
                       {"job_id": job["job_id"], "snapshot_sha256": snapshot,
                        "reason": reason,
                        "used_reverify": auth["used_reverify"]})
            return {"task_id": task_id, "auth_id": auth["auth_id"],
                    "outcome": "dispatched", "job_id": job["job_id"],
                    "job_status": job.get("status", ""),
                    "snapshot_sha256": snapshot,
                    "used_reverify": auth["used_reverify"],
                    "remaining_reverify": max(0, auth["max_reverify"] - auth["used_reverify"])}

    def tasks_active_snapshot(self, task_id):
        """服务端计算的当前工作树内容快照摘要（含 dirty 内容，不只 HEAD）。"""
        task = self.tasks._load(task_id)
        repo = self.tasks._repo(task)
        return _repo_snapshot(repo.path).get("snapshot_sha256", "")

    # ---- 作业体 --------------------------------------------------------

    def _runner(self, ctx):
        """delegation_check 作业体。

        执行前核对这份作业确实是授权服务亲自登记的
        派发记录（job_id 在授权的 dispatched_snapshots 里、payload 快照/
        要求与登记一致、绑定授权专属预算账本且有预留）——通用作业接口
        直创、伪造字段、未绑定预算的作业在这里全部失败关闭。
        R2：取消与剩余预算经 interrupt_check 传进真实复验；取消/超预算后
        不写回执产物、不记 receipt_recorded，作业落取消/预算终止。
        """
        payload = ctx.payload or {}
        claimed_task = str(payload.get("task_id", "") or "")
        task_id = str(ctx.task_id or "")
        if claimed_task and claimed_task != task_id:
            raise IntakeError("JOB_IDENTITY_MISMATCH",
                              "payload.task_id does not match the server-side job identity")
        if not task_id:
            raise IntakeError("TRIGGER_TASK_REQUIRED",
                              "delegation job requires the server-side task_id field")
        auth_id = str(payload.get("auth_id", "") or "")
        interrupt_check = self._interrupt_check(ctx)
        with self._lock:
            auth = self._load(auth_id)
            if auth.get("task_id") != task_id:
                raise IntakeError("JOB_IDENTITY_MISMATCH",
                                  "authorization does not belong to this task")
            # R1：派发登记核对——不接受未经授权预留事务登记的裸作业
            dispatched = next((row for row in auth.get("dispatched_snapshots", [])
                               if row.get("job_id") == ctx.job_id), None)
            if dispatched is None:
                raise IntakeError(
                    "JOB_IDENTITY_MISMATCH",
                    "this job was not dispatched by the delegation service "
                    "(no attempt reserved, no budget bound); use "
                    "POST /api/v2/delegations/check")
            if str(payload.get("snapshot_sha256", "")) != str(dispatched.get("snapshot_sha256", "")):
                raise IntakeError("JOB_IDENTITY_MISMATCH",
                                  "payload snapshot does not match the dispatched record")
            if str(payload.get("requirement_version", "")) != str(auth.get("requirement_version", "")):
                raise IntakeError("JOB_IDENTITY_MISMATCH",
                                  "payload requirement_version does not match the authorization")
            # R1：预算绑定核对——复验必须带着授权专属账本的预留执行
            if str(ctx.budget_id or "") != str(auth.get("budget_id", "")) \
                    or float(ctx.budget_reserved or 0.0) <= 0:
                raise IntakeError("JOB_BUDGET_UNBOUND",
                                  "delegation reverify must run on the authorization "
                                  "budget reservation, not an unbound job")
            now = time.time()
            if auth.get("status") != "active" or now >= auth.get("expires_at", 0):
                return {"status": "blocked", "auth_id": auth_id,
                        "blocked_reason": ("authorization_not_active" if auth.get("status") != "active"
                                           else "authorization_expired"),
                        "boundary": "执行前核验拒绝：授权已不在有效状态，实验未运行"}
            current = self.tasks.get(task_id)
            # 复验会开新轮次（round_id 变化），不是要求变化；只看 requirement_version
            if str(current.get("requirement_version", "")) != auth.get("requirement_version"):
                self._abort_event(auth_id, "requirement_changed", stage="before_reverify",
                                  job_id=ctx.job_id, finished=False)
                return {"status": "blocked", "auth_id": auth_id,
                        "blocked_reason": "requirement_changed",
                        "boundary": "执行前核验拒绝：验收要求已变化，原授权不覆盖新目标"}
            # The dispatch snapshot is a content identity, not a hint.  A job
            # can sit in the queue while the worktree changes; it must fail
            # closed before opening a new task round or starting an experiment.
            current_snapshot = self.tasks_active_snapshot(task_id)
            expected_snapshot = str(payload.get("snapshot_sha256", ""))
            if not expected_snapshot or current_snapshot != expected_snapshot:
                self._abort_event(auth_id, "source_snapshot_changed",
                                  stage="before_reverify", job_id=ctx.job_id,
                                  finished=False)
                raise IntakeError(
                    "DELEGATION_SOURCE_CHANGED",
                    "current source snapshot does not match the dispatched delegation")
        # R2：入口即核对——取消/预算在起步前就已触发时，不启动任何实验
        reason = interrupt_check()
        if reason:
            self._abort_event(auth_id, reason, stage="before_start", job_id=ctx.job_id)
            self._raise_interrupted(reason, "interrupted before reverify started")
        needed = self.tasks.reverify_needed(task_id)
        if not needed["needed"]:
            return {"status": "nothing_to_reverify", "auth_id": auth_id,
                    "task_id": task_id,
                    "pending_recheck": needed["pending_recheck"],
                    "snapshot_moved": needed["snapshot_moved"],
                    "receipt_status": self.tasks.receipt(task_id).get("status", ""),
                    "boundary": "无可复验项是事实陈述，不冒充复验成功"}
        started = time.monotonic()
        with ctx.phase("reverify_current"):
            outcome = self._reverify_watchdog(ctx, task_id, interrupt_check)
        elapsed = round(time.monotonic() - started, 3)
        if outcome.get("interrupted"):
            # 取消/超预算：无论复验是否已在中断前做完，都不作为正常交付
            self._abort_event(auth_id, outcome["reason"],
                              stage="during_reverify", job_id=ctx.job_id,
                              finished=bool(outcome.get("finished")))
            self._raise_interrupted(
                outcome["reason"],
                "reverify interrupted (%s); partial state saved in the task ledger"
                % outcome["reason"])
        receipt = self.tasks.receipt(task_id)
        # R2：产物与授权记账前的最后一道核对
        reason = interrupt_check()
        if reason:
            self._abort_event(auth_id, reason, stage="before_receipt",
                              job_id=ctx.job_id, finished=True)
            self._raise_interrupted(reason,
                                    "cancel/budget arrived before the receipt was recorded")
        receipt_bytes = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        ctx.write_artifact("task-receipt.json", receipt_bytes)
        if ctx.currency == "units":
            ctx.usage_units = elapsed
        with self._lock:
            auth = self._load(auth_id)
            auth["last_receipt"] = {"job_id": ctx.job_id,
                                    "receipt_status": receipt.get("status", ""),
                                    "receipt_sha256": receipt.get("receipt_sha256", "")}
            self._save(auth, "receipt_recorded",
                       {"job_id": ctx.job_id,
                        "receipt_status": receipt.get("status", ""),
                        "receipt_sha256": receipt.get("receipt_sha256", "")})
        return {"status": "reverified", "auth_id": auth_id, "task_id": task_id,
                "reverify_seconds": elapsed,
                "round_id": receipt.get("round_id", ""),
                "round_no": receipt.get("round_no", 0),
                "receipt_status": receipt.get("status", ""),
                "receipt_sha256": receipt.get("receipt_sha256", ""),
                "receipt_artifact": {"name": "task-receipt.json",
                                     "path": f"jobs/{ctx.job_id}/artifacts/task-receipt.json"},
                "boundary": "作业完成=复验已执行；验收结论以回执 status 为准"}

    # ---- R2：取消与预算的执行面 ---------------------------------------

    def _interrupt_check(self, ctx):
        """把作业的取消事件与预算探针折成一个可传入复验循环的探针。"""
        def check() -> str:
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                return "cancel_requested"
            if ctx.budget_exhausted is not None:
                try:
                    if ctx.budget_exhausted():
                        return "budget_exhausted"
                except Exception:  # 探针故障不伪装成预算耗尽
                    pass
            return ""
        return check

    def _reverify_watchdog(self, ctx, task_id, interrupt_check):
        """跑真实复验并盯取消/预算：中断即时生效（协作边界），复验线程在
        下一个中断点停下；宽限后仍未停的后台延续不再被消费（结果不进
        产物、不进授权记录）。"""
        box = {}

        def work():
            try:
                box["result"] = self.tasks.reverify_current(
                    task_id, {}, idempotency_key="delegation-job:" + ctx.job_id,
                    interrupt_check=interrupt_check)
            except BaseException as exc:  # noqa: BLE001 - 原样带回主线程
                box["error"] = exc

        worker = threading.Thread(target=work, daemon=True,
                                  name=f"delegation-reverify-{ctx.job_id}")
        worker.start()
        reason = ""
        grace_deadline = 0.0
        while worker.is_alive():
            trip = interrupt_check()
            if trip and not reason:
                reason = trip
                grace_deadline = time.monotonic() + INTERRUPT_GRACE_SECONDS
            if reason and time.monotonic() >= grace_deadline:
                break  # 协作边界未能及时停下：放弃等待，后台延续不被消费
            worker.join(timeout=0.1)
        finished = "result" in box or ("error" in box
                                       and not isinstance(box["error"], ReverifyInterrupted))
        if reason:
            return {"interrupted": True, "reason": reason,
                    "finished": finished,
                    "continuation_abandoned": worker.is_alive()}
        if "error" in box:
            if isinstance(box["error"], ReverifyInterrupted):
                # 复验循环内的协作边界先于看门狗轮询触发：同样按中断处理
                return {"interrupted": True, "reason": box["error"].reason,
                        "finished": False, "continuation_abandoned": False}
            raise box["error"]
        return {"interrupted": False, "result": box.get("result")}

    def _abort_event(self, auth_id, reason, *, stage, job_id="", finished=False):
        """取消/超预算如实入账：授权事件记录中止，绝不记 receipt_recorded。"""
        with self._lock:
            auth = self._load(auth_id)
            abort = {"job_id": job_id, "reason": reason, "stage": stage,
                     "at": time.time(),
                     "reverify_finished_before_abort": bool(finished),
                     "receipt_suppressed": True}
            auth["last_abort"] = abort
            self._save(auth, "reverify_aborted",
                       {**abort,
                        "boundary": "中止的复验不作为成功回执呈现；授权记录不更新"})

    def _raise_interrupted(self, reason, detail):
        if reason == "budget_exhausted":
            raise JobBudgetExhausted(detail)
        raise JobCancelled(detail)

    # ---- 内部 ----------------------------------------------------------

    def _scan(self, task_id=""):
        rows = []
        for path in self.root.glob("auth-*.json"):
            record = _read_json(path)
            if not record:
                continue
            if task_id and record.get("task_id") != task_id:
                continue
            rows.append(record)
        return rows
