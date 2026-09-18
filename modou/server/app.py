"""Authenticated same-origin FastAPI control plane for local 水木验码 reviews."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import socket
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool

from ..agent.memory import (MemoryProposal, ReviewMemoryError,
                            confirm as confirm_review_memory,
                            delete as delete_review_memory,
                            export as export_review_memory,
                            set_status as set_review_memory_status)
from ..acceptance_packs import public_catalog as acceptance_pack_catalog
from ..capabilities import public_registry as capability_registry
from ..release_status import build_status, validate_status, ReleaseStatusError
from .control import IntakeError, ReviewManager, StandardModeViolation
from .eval_receipts import get_receipt, list_receipts
from .tasks import ReverifyInterrupted, TaskService
from .jobs import JobCancelled, JobBudgetExhausted, JobService
from .jobs import register as register_jobs


API_PREFIX = "/api/v1"


def _tree_sha256(root: Path) -> str:
    """Hash a build/skill tree deterministically without exposing its contents."""
    digest = hashlib.sha256()
    if root.is_file():
        digest.update(root.name.encode())
        digest.update(root.read_bytes())
        return digest.hexdigest()
    if not root.is_dir():
        return "0" * 64
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _version_info() -> dict[str, str]:
    project = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                                check=True, capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = "0" * 40
    return {
        "schema_version": "shuimu-version-v1",
        "rc_commit": commit if len(commit) == 40 else "0" * 40,
        "frontend_build_hash": _tree_sha256(project / "web" / "dist"),
        "skill_hash": _tree_sha256(project / "skills" / "ui-comprehension-probe" / "SKILL.md"),
        "scenario_hash": _tree_sha256(project / "skills" / "ui-comprehension-probe" / "references" / "scenarios.md"),
        "harness_version": "ui-comprehension-probe-harness-v1",
    }


def _lan_hosts() -> list[str]:
    """本机在局域网里的 IPv4（尽力而为）。

    UDP connect 不发任何包，只让协议栈替我们选一次路由，所以拿到的是
    "此刻要出门会从哪个地址走"。离线或没有局域网时返回空表。
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 9))
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        found.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    unique: list[str] = []
    for ip in found:
        if (ip and not ip.startswith("127.") and not ip.startswith("169.254.")
                and ip not in unique):
            unique.append(ip)
    return unique


def accepted_host_headers(host: str, port: int) -> list[str]:
    """这台服务认哪些 Host 头。

    绑定具体地址（默认 127.0.0.1）时只认它自己，与从前完全一致；
    绑定 0.0.0.0/::（展台三机拓扑）时额外认回环写法与公告的局域网地址。
    """
    if host in {"", "0.0.0.0", "::"}:
        names = ["127.0.0.1", "localhost", *_lan_hosts()]
        return [f"{name}:{port}" for name in dict.fromkeys(names)]
    return [f"{host}:{port}"]


def announcement_urls(host: str, port: int) -> list[str]:
    """启动横幅逐行打印的打开地址（不含 token 片段，由调用方拼接）。

    单独成函数是因为这里出过一次展台级回归：横幅在已经带端口的
    host 头后面又拼了一次端口（http://127.0.0.1:8765:8765/），
    双击启动器照抄这行就打不开页面。
    """
    return [f"http://{value}" for value in accepted_host_headers(host, port)]


def _is_event_stream_path(path: str) -> bool:
    return path.endswith("/events")


class _GZipExceptEventStreams(GZipMiddleware):
    """gzip 压 JSON 与静态资源，但放过 SSE：压缩器的缓冲会把实时进度挤成一段一段。"""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _is_event_stream_path(scope.get("path", "")):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


def create_app(*, manager: ReviewManager, token: str,
               host: str = "127.0.0.1", port: int = 8765,
               web_dist: Path | None = None,
               release_status_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="水木验码本地控制面", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.manager = manager
    from modou.evidence_experiments import make_runner
    tasks = TaskService(manager, experiment_runner=make_runner(manager))
    app.state.tasks = tasks
    # Trigger entries (T13) run as background jobs whose real body is the
    # server-side re-verification of the addressed task — never a client
    # command, and never an adoption by itself. N02：作业体执行真实的
    # reverify_current，把任务回执落成作业产物并在 result 里引用其哈希；
    # 不再把 usage_units 伪造成 0，也不再返回只有 task 标识的空结果。
    def _run_trigger(ctx):
        payload = ctx.payload or {}
        claimed_task = str(payload.get("task_id", "") or "")
        task_id = str(ctx.task_id or "")
        # N03：身份由服务端字段确定；payload 同名字段不一致即拒绝，
        # 客户端不能借 payload 把作业导向另一个任务。
        if claimed_task and claimed_task != task_id:
            raise IntakeError("JOB_IDENTITY_MISMATCH",
                              "payload.task_id does not match the server-side job identity")
        if not task_id:
            raise IntakeError("TRIGGER_TASK_REQUIRED",
                              "trigger job requires the server-side task_id field")
        # 取消与预算探针传入真实复验循环，触发作业与
        # 委托作业同一纪律——中断后不再启动后续实验，不把中止当成功。
        def _interrupt_check():
            if ctx.cancel_event is not None and ctx.cancel_event.is_set():
                return "cancel_requested"
            if ctx.budget_exhausted is not None:
                try:
                    if ctx.budget_exhausted():
                        return "budget_exhausted"
                except Exception:
                    pass
            return ""

        needed = tasks.reverify_needed(task_id)
        if not needed["needed"]:
            # 无可执行项：如实报告，不制造"复验成功"。
            return {"triggered_task": task_id, "status": "nothing_to_reverify",
                    "reason": ("no criterion is pending_recheck and the source "
                               "snapshot is unchanged; reverify would be a no-op"),
                    "pending_recheck": needed["pending_recheck"],
                    "snapshot_moved": needed["snapshot_moved"],
                    "receipt_status": tasks.receipt(task_id).get("status", ""),
                    "boundary": "作业完成不等于验收成功；无可复验项是事实陈述"}
        started = time.monotonic()
        with ctx.phase("reverify_current"):
            try:
                tasks.reverify_current(task_id, {}, idempotency_key="trigger-job:" + ctx.job_id,
                                       interrupt_check=_interrupt_check)
            except ReverifyInterrupted as exc:
                # 中断如实落终态：不写回执产物，不把中止当成功交付。
                if exc.reason == "budget_exhausted":
                    raise JobBudgetExhausted(
                        f"reverify interrupted by budget at criterion boundary; "
                        f"rechecked={exc.rechecked}") from exc
                raise JobCancelled(
                    f"reverify interrupted by cancel at criterion boundary; "
                    f"rechecked={exc.rechecked}") from exc
        elapsed = round(time.monotonic() - started, 3)
        receipt = tasks.receipt(task_id)
        receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                   indent=2).encode("utf-8")
        ctx.write_artifact("task-receipt.json", receipt_bytes)
        # units 预算按实测墙钟秒申报（在 result 里明示口径）；seconds 由
        # 服务端按墙钟结算。无论哪种货币都不再伪造成 0。
        if ctx.currency == "units":
            ctx.usage_units = elapsed
        return {"triggered_task": task_id, "status": "reverified",
                "pending_recheck": needed["pending_recheck"],
                "snapshot_moved": needed["snapshot_moved"],
                "reverify_seconds": elapsed,
                "round_id": receipt.get("round_id", ""),
                "round_no": receipt.get("round_no", 0),
                "receipt_status": receipt.get("status", ""),
                "receipt_sha256": receipt.get("receipt_sha256", ""),
                "receipt_artifact": {"name": "task-receipt.json",
                                     "path": f"jobs/{ctx.job_id}/artifacts/task-receipt.json"},
                "usage_note": ("units 按实测复验墙钟秒结算" if ctx.currency == "units"
                               else "seconds 由服务端按墙钟结算"),
                "boundary": "作业完成=复验已执行；验收结论以回执 status 为准，"
                            "回执是测试证据记录，不是通用任务完成认证"}

    jobs = JobService(manager, runners={"trigger_dispatch": _run_trigger})
    app.state.jobs = jobs
    register_jobs(app, jobs)
    # 持续委托（职责层最小闭环）：授权/检查/投递复用 TaskService 与 JobService，
    # 作业体在 DelegationService 内注册（执行前二次核验授权）。
    from modou.server.delegation import DelegationService
    delegations = DelegationService(tasks, jobs)
    app.state.delegations = delegations
    # 注册后，重启时 deferred 的排队复验作业可重新入队；执行前仍会二次核验授权
    jobs.register_runner("delegation_check", delegations._runner)
    from modou.project_registry import ProjectRegistry, ProjectRegistryError
    registry = ProjectRegistry()
    app.state.projects = registry
    app.state.token = token
    app.state.release_status_path = release_status_path
    app.state.version_info = _version_info()
    # Host/Origin 仍是白名单精确比较，只是从单值变成集合：绑定 0.0.0.0
    # （展台三机拓扑）时额外认回环写法与公告的局域网地址，其余一律 403。
    expected_hosts = set(accepted_host_headers(host, port))
    expected_origins = {f"http://{value}" for value in expected_hosts}
    # gzip 压 JSON 与静态资源（Monaco 首开 4.42MB 的问题）；/events 是 SSE，
    # 单独放行，理由见 _GZipExceptEventStreams。
    app.add_middleware(_GZipExceptEventStreams, minimum_size=1024)

    def current_release_status() -> dict:
        path = app.state.release_status_path
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    cwd=Path(__file__).resolve().parents[2],
                                    check=True, capture_output=True, text=True).stdout.strip()
        except Exception:
            commit = "0" * 40
        if path and Path(path).is_file():
            try:
                payload = validate_status(json.loads(Path(path).read_text(encoding="utf-8")))
                # 回证纪律：收据必须自证它描述的是哪一个提交。服务端把当前
                # HEAD 一并带上；两者不一致时标记 receipt_expired，让前端
                # 显示“收据已过期，需重算”，而不是照常渲染一份过期声明。
                payload["serving_commit"] = commit
                payload["receipt_expired"] = payload.get("source_commit") != commit
                return payload
            except (OSError, json.JSONDecodeError, ReleaseStatusError):
                pass
        # 无收据或收据损坏：not_generated 兜底同样带 serving_commit；
        # 兜底记录不描述任何外部声明，谈不上过期，expired 恒为 False。
        fallback = build_status(machine_status="MACHINE_CHECKS_FAILED", source_commit=commit,
                                baseline_manifest_payload_sha256="0" * 64,
                                evidence={"status": {"state": "not_generated"}})
        fallback["serving_commit"] = commit
        fallback["receipt_expired"] = False
        return fallback

    @app.middleware("http")
    async def secure_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if request.headers.get("host", "") not in expected_hosts:
                return _error(403, "HOST_FORBIDDEN", "Host 不属于本次本地服务")
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "")
            # Browsers commonly omit Origin on same-origin GET. Unsafe requests
            # require it exactly; safe requests reject any mismatch and, when
            # browser metadata exists, require same-origin.
            if request.method not in {"GET", "HEAD"}:
                if origin not in expected_origins:
                    return _error(403, "ORIGIN_FORBIDDEN", "Origin 不属于本次本地服务")
            elif ((origin and origin not in expected_origins) or
                  (fetch_site and fetch_site != "same-origin")):
                return _error(403, "ORIGIN_FORBIDDEN", "Origin 不属于本次本地服务")
            auth = request.headers.get("authorization", "")
            prefix = "Bearer "
            candidate = auth[len(prefix):] if auth.startswith(prefix) else ""
            if not candidate or not secrets.compare_digest(candidate, token):
                return _error(401, "AUTH_REQUIRED", "启动令牌缺失或无效")
            # DELETE 在本 API 里不带 body（归因走查询参数），豁免 JSON
            # content-type 检查；Origin 与令牌校验对它仍然全覆盖。
            if request.method not in {"GET", "HEAD", "DELETE"}:
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    return _error(415, "JSON_REQUIRED", "只接受 application/json")
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.exception_handler(IntakeError)
    async def intake_error(_request: Request, exc: IntakeError):
        status = 404 if exc.code in {"REVIEW_NOT_FOUND", "EVIDENCE_NOT_FOUND",
                                     "EVENT_NOT_FOUND", "MEMORY_NOT_FOUND",
                                     "SOURCE_PATH_NOT_FOUND",
                                     "COMMENT_NOT_FOUND", "TASK_NOT_FOUND",
                                     "TASK_ROUND_NOT_FOUND", "JOB_NOT_FOUND",
                                     "DELEGATION_AUTH_UNKNOWN"} else 400
        if exc.code in {"LIVE_RUN_BUSY", "STALE_PLAN", "IDEMPOTENCY_CONFLICT",
                        "MEMORY_ID_EXISTS", "REVIEW_NOT_CANCELLABLE",
                        "REVIEW_NOT_RESUMABLE",
                        "SOURCE_SNAPSHOT_CHANGED", "SOURCE_SNAPSHOT_MISSING",
                        "DECISION_NOT_AWAITING", "DECISION_STALE",
                        "DECISION_EXPIRED", "DISPOSITION_ALREADY_RECORDED",
                        "COMMENT_REVISION_CONFLICT", "JOB_DEDUPE_CONFLICT",
                        "DELEGATION_ALREADY_ACTIVE",
                        "JOB_NOT_CANCELLABLE"}:
            status = 409
        if exc.code == "LIVE_RUN_BUSY":
            return JSONResponse(status_code=409, content={
                "code": exc.code, "current_review_id": exc.detail,
                "message": "已有审查正在进行", "retry_after_s": None,
            })
        return _error(status, exc.code, exc.detail)

    @app.exception_handler(ProjectRegistryError)
    async def registry_error(_request: Request, exc: ProjectRegistryError):
        status = 404 if exc.code == "PROJECT_NOT_FOUND" else 400
        return _error(status, exc.code, exc.detail)

    @app.get(f"{API_PREFIX}/repos")
    async def repos():
        return {"repos": manager.registry.public()}

    @app.post(f"{API_PREFIX}/repos", status_code=201)
    async def add_repo(request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"path"}:
            raise IntakeError("REPO_REQUEST_INVALID", "只需要填写仓库根目录 path")
        repo = manager.registry.add(str(raw.get("path") or ""))
        return {"repo_id": repo.repo_id, "display_name": repo.display_name,
                "technical_name": repo.technical_name}

    # ---- C1 仓库审查记忆：确认 / 撤销 / 删除 --------------------------
    # memory.py 里的域逻辑（校验、原子写、状态机）早已齐备，这里只做
    # HTTP 接线。ReviewMemoryError 一律转成 IntakeError（code 原样透传），
    # 让前端拿到与其它写入端点一致的错误形状。

    def _with_memory(repo_id: str, action):
        repo = manager.registry.get(repo_id)
        try:
            return action(repo.path)
        except ReviewMemoryError as exc:
            raise IntakeError(exc.code, exc.detail) from exc

    @app.get("/api/v2/repos/{repo_id}/memory")
    async def read_repo_memory(repo_id: str):
        return _with_memory(repo_id, export_review_memory)

    @app.post("/api/v2/repos/{repo_id}/memory", status_code=201)
    async def remember_repo_memory(repo_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict):
            raise IntakeError("MEMORY_REQUEST_INVALID",
                              "memory request must be an object")
        applies_to = raw.get("applies_to") or []
        evidence_ids = raw.get("evidence_ids") or []
        if not isinstance(applies_to, list) or not isinstance(evidence_ids, list):
            raise IntakeError("MEMORY_REQUEST_INVALID",
                              "applies_to 与 evidence_ids 必须是数组")
        memory_id = str(raw.get("memory_id") or "").strip()
        if not memory_id:
            # 调用方没必要自己想一个合法 id：服务器生成短 slug。
            memory_id = "rule-" + secrets.token_hex(4)
        proposal = MemoryProposal(
            memory_id=memory_id, rule=str(raw.get("rule") or ""),
            applies_to=tuple(str(item) for item in applies_to),
            kind=str(raw.get("kind") or ""),
            source_review_id=str(raw.get("source_review_id") or ""),
            evidence_ids=tuple(str(item) for item in evidence_ids))
        _with_memory(repo_id, lambda path: confirm_review_memory(
            path, proposal, confirmed_by=str(raw.get("confirmed_by") or "")))
        return _with_memory(repo_id, export_review_memory)

    @app.patch("/api/v2/repos/{repo_id}/memory/{memory_id}")
    async def revise_repo_memory(repo_id: str, memory_id: str,
                                 request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"status", "confirmed_by"}:
            raise IntakeError("MEMORY_REQUEST_INVALID",
                              "状态修订只接受 status 和 confirmed_by")
        _with_memory(repo_id, lambda path: set_review_memory_status(
            path, memory_id, str(raw["status"]),
            confirmed_by=str(raw["confirmed_by"])))
        return _with_memory(repo_id, export_review_memory)

    @app.delete("/api/v2/repos/{repo_id}/memory/{memory_id}")
    async def forget_repo_memory(repo_id: str, memory_id: str,
                                 confirmed_by: str = ""):
        if not confirmed_by.strip():
            raise IntakeError("MEMORY_CONFIRMATION_REQUIRED",
                              "删除需要 confirmed_by 查询参数")
        _with_memory(repo_id, lambda path: delete_review_memory(
            path, memory_id, confirmed_by=confirmed_by))
        return _with_memory(repo_id, export_review_memory)

    @app.get(f"{API_PREFIX}/presets")
    async def presets():
        # Presets contain only opaque repository IDs and validated relative test
        # paths.  Local paths and interpreter bindings never cross this boundary.
        return {"presets": manager.registry.presets_public()}

    @app.get(f"{API_PREFIX}/capabilities")
    async def capabilities():
        # The cockpit renders these badges from the same registry the release
        # pipeline enforces, so the screen cannot claim more than the package.
        return capability_registry(manager.capabilities)

    @app.post(f"{API_PREFIX}/reviews", status_code=201)
    async def create_review(request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(raw, dict):
            raise IntakeError("REQUEST_INVALID", "request body must be an object")
        return manager.create(raw, idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews", status_code=201)
    async def create_review_v2(request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(raw, dict):
            raise IntakeError("SPEC_NOT_OBJECT", "review spec must be an object")
        return manager.create_v2(raw, idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/draft-from-text")
    async def draft_review_from_text(request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(raw, dict) or set(raw) - {"text", "product_mode"}:
            raise IntakeError("DRAFT_REQUEST_INVALID",
                              "draft request requires only text")
        try:
            return manager.draft_from_text(
                str(raw.get("text") or ""),
                product_mode=str(raw.get("product_mode") or "standard"))
        except StandardModeViolation as exc:
            raise IntakeError("STANDARD_MODE_MODEL_PATH_BLOCKED",
                              str(exc)) from exc

    @app.post("/api/v2/locate-code")
    async def locate_code(request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        if not isinstance(raw, dict):
            raise IntakeError("LOCATE_REQUEST_INVALID",
                              "locate request must be an object")
        return manager.locate_code(raw)

    @app.get("/api/v2/reviews/{review_id}")
    async def get_review_v2(review_id: str):
        return manager.describe(review_id)

    @app.post("/api/v2/reviews/{review_id}/approval")
    async def approve_v2(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"plan_sha256"}:
            raise IntakeError("APPROVAL_INVALID", "approval requires only plan_sha256")
        return manager.approve(review_id, str(raw["plan_sha256"]),
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/messages")
    async def revise_review_v2(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict):
            raise IntakeError("PLAN_REVISION_INVALID", "revision must be an object")
        return manager.revise_v2(review_id, raw)

    @app.post("/api/v2/reviews/{review_id}/cancel")
    async def cancel_review_v2(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("CANCEL_INVALID", "取消请求不需要 body")
        return manager.cancel(review_id,
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/resume")
    async def resume_review_v2(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("RESUME_INVALID", "恢复请求不需要 body")
        return manager.resume(review_id,
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/cleanup")
    async def cleanup_review_v2(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("CLEANUP_INVALID", "清理请求不需要 body")
        return manager.cleanup(review_id,
                               idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/decisions/{decision_id}")
    async def answer_decision_v2(review_id: str, decision_id: str,
                                 request: Request):
        raw = await request.json()
        if (not isinstance(raw, dict)
                or set(raw) != {"decision", "plan_sha256"}):
            raise IntakeError(
                "DECISION_INVALID", "人工决策只接受 decision 和 plan_sha256")
        return manager.decide(
            review_id, decision_id, str(raw["decision"]),
            str(raw["plan_sha256"]),
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/tasks", status_code=201)
    async def create_evidence_task(request: Request):
        return await run_in_threadpool(tasks.create, await request.json(), request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/tasks")
    async def list_evidence_tasks(review_id: str = ""):
        return await run_in_threadpool(tasks.list, review_id)

    @app.get("/api/v2/tasks/{task_id}")
    async def get_evidence_task(task_id: str):
        return await run_in_threadpool(tasks.get, task_id)

    @app.get("/api/v2/tasks/{task_id}/receipt")
    async def get_evidence_task_receipt(task_id: str):
        return await run_in_threadpool(tasks.receipt, task_id)

    @app.post("/api/v2/tasks/{task_id}/actions")
    async def act_on_evidence_task(task_id: str, request: Request):
        return await run_in_threadpool(tasks.action, task_id, await request.json(), request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/tasks/{task_id}/rounds")
    async def list_task_rounds(task_id: str):
        return await run_in_threadpool(tasks.rounds, task_id)

    @app.get("/api/v2/tasks/{task_id}/rounds/{round_id}")
    async def get_task_round(task_id: str, round_id: str):
        return await run_in_threadpool(tasks.round_detail, task_id, round_id)

    @app.post("/api/v2/tasks/{task_id}/rounds", status_code=201)
    async def create_task_round(task_id: str, request: Request):
        return await run_in_threadpool(tasks.create_requirement_version, task_id,
                                       await request.json(),
                                       request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/tasks/{task_id}/reverify")
    async def reverify_task_current(task_id: str, request: Request):
        raw = await _optional_json(request)
        return await run_in_threadpool(tasks.reverify_current, task_id,
                                       raw if raw is not None else {},
                                       request.headers.get("idempotency-key", ""))

    # ---- 持续委托（职责层最小闭环）：GET 全部只读，检查只走显式 POST ----
    @app.post("/api/v2/delegations", status_code=201)
    async def create_delegation(request: Request):
        raw = await request.json()
        return await run_in_threadpool(delegations.create, raw,
                                       request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/delegations")
    async def list_delegations(task_id: str = ""):
        return await run_in_threadpool(delegations.list, task_id)

    @app.get("/api/v2/delegations/{auth_id}")
    async def get_delegation(auth_id: str):
        return await run_in_threadpool(delegations.get, auth_id)

    @app.get("/api/v2/delegations/{auth_id}/events")
    async def list_delegation_events(auth_id: str, since_seq: int = 0):
        return await run_in_threadpool(delegations.events, auth_id, since_seq)

    @app.post("/api/v2/delegations/{auth_id}/stop")
    async def stop_delegation(auth_id: str, request: Request):
        await _optional_json(request)
        return await run_in_threadpool(delegations.stop, auth_id)

    @app.post("/api/v2/delegations/check")
    async def delegation_check(request: Request):
        raw = await _optional_json(request)
        if raw is None:
            raw = {}
        if not isinstance(raw, dict) or set(raw) - {"task_id", "reason"}:
            raise IntakeError("DELEGATION_REQUEST_INVALID",
                              "check requires only task_id and reason")
        task_id = str(raw.get("task_id") or "")
        if not task_id:
            raise IntakeError("DELEGATION_REQUEST_INVALID", "task_id is required")
        return await run_in_threadpool(delegations.check, task_id,
                                       str(raw.get("reason") or "user_check"))

    # ---- T07: registered projects (server-side run configs; the web client
    # only selects a configuration, it never submits free-form commands).
    @app.get("/api/v2/projects")
    async def list_registered_projects():
        return {"projects": await run_in_threadpool(registry.list_projects)}

    @app.get("/api/v2/projects/{project_id}")
    async def get_registered_project(project_id: str):
        return await run_in_threadpool(lambda: registry.get(project_id).as_dict())

    @app.get("/api/v2/projects/{project_id}/diagnose")
    async def diagnose_registered_project(project_id: str):
        return await run_in_threadpool(registry.diagnose, project_id)

    @app.get("/api/v2/projects/{project_id}/run-plan")
    async def plan_registered_project(project_id: str, test_ids: str = ""):
        ids = [t for t in test_ids.split(",") if t.strip()]
        return await run_in_threadpool(registry.run_plan, project_id, test_ids=ids)

    @app.post("/api/v2/projects/{project_id}/prepare")
    async def prepare_registered_project(project_id: str):
        return await run_in_threadpool(registry.prepare_dependencies, project_id)

    @app.post("/api/v2/projects/{project_id}/execute")
    async def execute_registered_project(project_id: str, request: Request):
        raw = await _optional_json(request)
        ids = [t for t in (raw or {}).get("test_ids", []) if isinstance(t, str)]
        return await run_in_threadpool(registry.execute_tests, project_id, ids)

    @app.get("/api/v2/capabilities")
    async def declared_capabilities():
        return await run_in_threadpool(registry.declare_capabilities)

    @app.get("/api/v2/teaching/lessons")
    async def teaching_lessons():
        """T12 教学视图的真实数据源：三个固定教学案例经服务端 narrate() 计算。

        这是教学叙述的唯一服务端入口；前端 TeachingView 组件只渲染这里的
        输出，不做评分、不做代写检测（模块边界照旧）。
        """
        from .. import teaching_view
        def _build():
            return {"schema_version": teaching_view.SCHEMA_VERSION,
                    "lessons": [{"case_id": case_id,
                                 "narration": teaching_view.lesson(case_id)}
                                for case_id in teaching_view.lessons()],
                    "boundary": "教学叙述只解释干预实验；不评价代码质量或作者。"}
        return await run_in_threadpool(_build)

    @app.get("/api/v2/reviews/{review_id}/repair")
    async def repair_status_v2(review_id: str):
        return manager.repair_status(review_id)

    @app.post("/api/v2/reviews/{review_id}/repair")
    async def deliver_repair_v2(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or "patch" not in raw or (
                set(raw) - {"patch", "session_id", "intent"}):
            raise IntakeError(
                "REPAIR_REQUEST_INVALID",
                "修复交付请求接受 patch，可选 session_id 与 intent")
        session_id = raw.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise IntakeError("REPAIR_REQUEST_INVALID", "session_id 必须是字符串")
        intent = raw.get("intent", "")
        if not isinstance(intent, str):
            raise IntakeError("REPAIR_REQUEST_INVALID", "intent 必须是字符串")
        return manager.deliver_repair(
            review_id, str(raw["patch"]),
            idempotency_key=request.headers.get("idempotency-key", ""),
            session_id=session_id, intent=intent)

    @app.post("/api/v2/reviews/{review_id}/edit-sessions", status_code=201)
    async def open_edit_session_v2(review_id: str, request: Request):
        raw = await _optional_json(request)
        return manager.open_edit_session(
            review_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/edit-sessions")
    async def list_edit_sessions_v2(review_id: str):
        return manager.list_edit_sessions(review_id)

    @app.post("/api/v2/reviews/{review_id}/edit-sessions/"
              "{session_id}/abandon")
    async def abandon_edit_session_v2(review_id: str, session_id: str,
                                      request: Request):
        raw = await _optional_json(request)
        return manager.abandon_edit_session(review_id, session_id, raw)

    @app.post("/api/v2/reviews/{review_id}/edit-sessions/"
              "{session_id}/delivery-approval")
    async def approve_delivery_v2(review_id: str, session_id: str,
                                  request: Request):
        raw = await _optional_json(request)
        return manager.approve_delivery(review_id, session_id, raw)

    @app.post("/api/v2/reviews/{review_id}/edit-sessions/"
              "{session_id}/delivery-export")
    async def export_delivery_v2(review_id: str, session_id: str,
                                 request: Request):
        raw = await _optional_json(request)
        return manager.export_delivery_package(review_id, session_id, raw)

    @app.post("/api/v2/reviews/{review_id}/repair/candidate")
    async def generate_repair_candidate_v2(review_id: str, request: Request):
        raw = await request.json()
        return manager.generate_repair(
            review_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/test-proposal")
    async def test_proposal_status_v2(review_id: str):
        return manager.test_proposal_status(review_id)

    @app.post("/api/v2/reviews/{review_id}/test-proposal")
    async def propose_test_v2(review_id: str, request: Request):
        raw = await _optional_json(request)
        return manager.propose_test(
            review_id, raw or {},
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/export")
    async def export_review_v2(review_id: str):
        return manager.export_review(review_id)

    @app.delete("/api/v2/reviews/{review_id}")
    async def delete_review_v2(review_id: str, request: Request):
        return manager.delete_review(
            review_id, idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/plan-revisions")
    async def plan_revisions_v2(review_id: str):
        return manager.plan_revisions(review_id)

    @app.post("/api/v2/reviews/{review_id}/dispositions", status_code=201)
    async def record_disposition_v2(review_id: str, request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        return manager.record_disposition(
            review_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/dispositions")
    async def dispositions_v2(review_id: str):
        return manager.dispositions(review_id)

    @app.get("/api/v2/reviews/{review_id}/events")
    async def events_v2(review_id: str, request: Request):
        return await events(review_id, request)

    @app.get("/api/v2/reviews/{review_id}/evidence/{evidence_id}")
    async def evidence_v2(review_id: str, evidence_id: str):
        return manager.evidence(review_id, evidence_id)

    @app.get("/api/v2/reviews/{review_id}/bundle")
    async def bundle_v2(review_id: str):
        return FileResponse(manager.review_bundle_path(review_id),
                            media_type="application/json",
                            filename=f"shuimu-yanma-review-{review_id}.json")

    @app.get("/api/v2/reviews/{review_id}/model-transcript")
    async def model_transcript_v2(review_id: str):
        return manager.model_transcript(review_id)

    @app.get("/api/v2/reviews/{review_id}/source/tree")
    async def source_tree_v2(review_id: str):
        return manager.source_tree(review_id)

    @app.get("/api/v2/reviews/{review_id}/source/file")
    async def source_file_v2(review_id: str, request: Request, path: str = ""):
        payload = manager.source_file(review_id, path)
        return JSONResponse(payload, headers={"ETag": f'"{payload["etag"]}"'})

    @app.post("/api/v2/reviews/{review_id}/comments", status_code=201)
    async def create_comment_v2(review_id: str, request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        return manager.create_comment(
            review_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/comments")
    async def list_comments_v2(review_id: str, path: str | None = None):
        return manager.list_comments(review_id, path=path)

    @app.post("/api/v2/reviews/{review_id}/comments/{comment_id}/replies",
              status_code=201)
    async def reply_comment_v2(review_id: str, comment_id: str,
                               request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        return manager.reply_comment(
            review_id, comment_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.patch("/api/v2/reviews/{review_id}/comments/{comment_id}")
    async def update_comment_v2(review_id: str, comment_id: str,
                                request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        return manager.update_comment(
            review_id, comment_id, raw,
            if_match=request.headers.get("if-match", ""))

    @app.post("/api/v2/reviews/{review_id}/reverifications", status_code=201)
    async def request_reverification_v2(review_id: str, request: Request):
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid JSON") from exc
        return manager.request_reverification(
            review_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get("/api/v2/reviews/{review_id}/reverifications")
    async def list_reverifications_v2(review_id: str):
        return manager.list_reverifications(review_id)

    @app.post("/api/v2/reviews/{review_id}/reverifications/"
              "{reverification_id}/approval")
    async def approve_reverification_v2(review_id: str, reverification_id: str,
                                        request: Request):
        raw = await _optional_json(request)
        return manager.approve_reverification(
            review_id, reverification_id, raw,
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/reverifications/"
              "{reverification_id}/rejection")
    async def reject_reverification_v2(review_id: str, reverification_id: str,
                                       request: Request):
        raw = await _optional_json(request)
        return manager.reject_reverification(review_id, reverification_id, raw)

    @app.get("/api/v2/reviews/{review_id}/annotations")
    async def annotations_v2(review_id: str):
        package = manager.annotations_package(review_id)
        return Response(
            content=json.dumps(package, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="shuimu-yanma-annotations-{review_id}.json"'})

    @app.get("/api/v2/capabilities")
    async def capabilities_v2():
        return capability_registry(manager.capabilities)

    @app.get("/api/v2/acceptance-packs")
    async def acceptance_packs_v2():
        """Read-only intake catalog; it never reports a task outcome."""
        return acceptance_pack_catalog(tasks.acceptance_packs)

    @app.get("/api/v2/release-status")
    async def release_status():
        return current_release_status()

    @app.get(f"{API_PREFIX}/standing-authorization")
    async def standing_authorization_status():
        """常驻授权状态卡（只读）：L3 无人值守的授权面公开可查。"""
        auth = manager.standing_authorization
        if auth is None:
            return {"loaded": False}
        return {
            "loaded": True,
            "authorized_by": auth.authorized_by,
            "source_sha256_prefix": auth.source_sha256[:12],
            "valid_until": auth.valid_until.isoformat(),
            "expired": auth.expired(),
            "repo_count": len(auth.repo_whitelist),
            "repo_whitelist": [p.name for p in auth.repo_whitelist],
            "budget_seconds_max": auth.budget_seconds_max,
            "max_writes_per_review": auth.max_writes_per_review,
            "max_writes_per_hour": auth.max_writes_per_hour,
        }

    @app.get(f"{API_PREFIX}/eval-receipts")
    async def eval_receipts_index():
        """评测回执索引（只读证据）：verdict 与判据计数先行，明细按 id 取。"""
        return {"receipts": list_receipts()}

    @app.get(f"{API_PREFIX}/eval-receipts/{{receipt_id}}")
    async def eval_receipt_detail(receipt_id: str):
        receipt = get_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(404, "eval receipt not found")
        return receipt

    @app.get("/api/v2/version")
    async def version():
        return dict(app.state.version_info)

    @app.get("/api/v2/providers")
    async def providers_v2():
        return manager.provider_public()

    @app.get("/api/v2/health")
    async def health_v2():
        return {"status": "ok", "live_review_id": manager.live_id,
                "execution_mode": manager.execution_mode.value}

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}")
    async def get_review(review_id: str):
        return manager.describe(review_id)

    @app.post(f"{API_PREFIX}/reviews/{{review_id}}/approval")
    async def approve(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"plan_sha256"}:
            raise IntakeError("APPROVAL_INVALID", "approval requires only plan_sha256")
        return manager.approve(review_id, str(raw["plan_sha256"]),
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post(f"{API_PREFIX}/reviews/{{review_id}}/cancel")
    async def cancel_review(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("CANCEL_INVALID", "取消请求不需要 body")
        return manager.cancel(review_id,
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post(f"{API_PREFIX}/reviews/{{review_id}}/resume")
    async def resume_review(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("RESUME_INVALID", "恢复请求不需要 body")
        return manager.resume(review_id,
                              idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post(f"{API_PREFIX}/reviews/{{review_id}}/cleanup")
    async def cleanup_review(review_id: str, request: Request):
        raw = await _optional_json(request)
        if raw not in ({}, None):
            raise IntakeError("CLEANUP_INVALID", "清理请求不需要 body")
        return manager.cleanup(review_id,
                               idempotency_key=request.headers.get("idempotency-key", ""))

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}/repair")
    async def repair_status(review_id: str):
        return manager.repair_status(review_id)

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}/events")
    async def events(review_id: str, request: Request):
        raw_cursor = request.headers.get("last-event-id", "0")
        try:
            cursor = int(raw_cursor.rsplit(":", 1)[-1])
        except ValueError as exc:
            raise IntakeError("EVENT_CURSOR_INVALID", raw_cursor) from exc
        store = manager.events(review_id)

        async def generate():
            current = cursor
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                batch = store.since(current)
                if batch:
                    for event in batch:
                        payload = json.dumps(event.as_dict(), ensure_ascii=False)
                        yield (f"id: {event.event_id}\n"
                               f"event: {event.kind}\n"
                               f"data: {payload}\n\n")
                        current = event.seq
                    state = manager.describe(review_id)["state"]["status"]
                    if state in {"COMPLETE", "PARTIAL", "FAILED", "ABORTED"}:
                        return
                await asyncio.sleep(.25)
                now = asyncio.get_running_loop().time()
                if not store.since(current) and now - last_heartbeat >= 15:
                    # Comment heartbeats have no event id and consume no seq.
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store",
                                          "X-Accel-Buffering": "no"})

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}/evidence/{{evidence_id}}")
    async def evidence(review_id: str, evidence_id: str):
        return manager.evidence(review_id, evidence_id)

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}/bundle")
    async def bundle(review_id: str):
        return FileResponse(manager.review_bundle_path(review_id),
                            media_type="application/json",
                            filename=f"shuimu-yanma-review-{review_id}.json")

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}/model-transcript")
    async def model_transcript(review_id: str):
        # Local-only transparency surface. The trace contains full prompts and
        # raw responses, but never the provider URL, headers or API key.
        return manager.model_transcript(review_id)

    @app.post(f"{API_PREFIX}/replays")
    async def replay(request: Request):
        raw = await request.json()
        review_id = str((raw or {}).get("review_id") or "")
        path = manager.review_bundle_path(review_id)
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get(f"{API_PREFIX}/providers")
    async def providers():
        return manager.provider_public()

    if web_dist and (web_dist / "index.html").exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        brand = web_dist / "brand"
        if brand.exists():
            app.mount("/brand", StaticFiles(directory=brand), name="brand")

        @app.get("/")
        async def index():
            # index.html 绝不能被缓存：它引用的是带哈希的 assets 文件名，
            # 一旦浏览器留着旧 index，`npm run build` 之后拿到的仍是旧应用——
            # 而且**看不出来**，因为页面照常渲染。演示前重新构建正是最容易踩的时候。
            return FileResponse(web_dist / "index.html",
                                headers={"Cache-Control": "no-store"})

    else:
        @app.get("/")
        async def index_missing():
            return Response("水木验码 Cockpit 尚未构建。请在 web/ 执行 npm run build。",
                            media_type="text/plain; charset=utf-8")
    return app


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


async def _optional_json(request: Request):
    try:
        return await request.json()
    except json.JSONDecodeError:
        return {}
