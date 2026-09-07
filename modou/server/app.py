"""Authenticated same-origin FastAPI control plane for local 水木验码 reviews."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..capabilities import public_registry as capability_registry
from ..release_status import build_status, validate_status, ReleaseStatusError
from .control import IntakeError, ReviewManager


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


def create_app(*, manager: ReviewManager, token: str,
               host: str = "127.0.0.1", port: int = 8765,
               web_dist: Path | None = None,
               release_status_path: Path | None = None) -> FastAPI:
    expected_host = f"{host}:{port}"
    expected_origin = f"http://{expected_host}"
    app = FastAPI(title="水木验码本地控制面", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.manager = manager
    app.state.token = token
    app.state.release_status_path = release_status_path
    app.state.version_info = _version_info()

    def current_release_status() -> dict:
        path = app.state.release_status_path
        if path and Path(path).is_file():
            try:
                return validate_status(json.loads(Path(path).read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ReleaseStatusError):
                pass
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                    cwd=Path(__file__).resolve().parents[2],
                                    check=True, capture_output=True, text=True).stdout.strip()
        except Exception:
            commit = "0" * 40
        return build_status(machine_status="MACHINE_CHECKS_FAILED", source_commit=commit,
                            baseline_manifest_payload_sha256="0" * 64,
                            evidence={"status": {"state": "not_generated"}})

    @app.middleware("http")
    async def secure_boundary(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if request.headers.get("host", "") != expected_host:
                return _error(403, "HOST_FORBIDDEN", "Host 不属于本次本地服务")
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "")
            # Browsers commonly omit Origin on same-origin GET. Unsafe requests
            # require it exactly; safe requests reject any mismatch and, when
            # browser metadata exists, require same-origin.
            if request.method not in {"GET", "HEAD"}:
                if origin != expected_origin:
                    return _error(403, "ORIGIN_FORBIDDEN", "Origin 不属于本次本地服务")
            elif ((origin and origin != expected_origin) or
                  (fetch_site and fetch_site != "same-origin")):
                return _error(403, "ORIGIN_FORBIDDEN", "Origin 不属于本次本地服务")
            auth = request.headers.get("authorization", "")
            prefix = "Bearer "
            candidate = auth[len(prefix):] if auth.startswith(prefix) else ""
            if not candidate or not secrets.compare_digest(candidate, token):
                return _error(401, "AUTH_REQUIRED", "启动令牌缺失或无效")
            if request.method not in {"GET", "HEAD"}:
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
        status = 404 if exc.code in {"REVIEW_NOT_FOUND", "EVIDENCE_NOT_FOUND"} else 400
        if exc.code in {"LIVE_RUN_BUSY", "STALE_PLAN", "IDEMPOTENCY_CONFLICT",
                        "REVIEW_NOT_CANCELLABLE", "REVIEW_NOT_RESUMABLE",
                        "SOURCE_SNAPSHOT_CHANGED", "SOURCE_SNAPSHOT_MISSING",
                        "DECISION_NOT_AWAITING", "DECISION_STALE",
                        "DECISION_EXPIRED"}:
            status = 409
        if exc.code == "LIVE_RUN_BUSY":
            return JSONResponse(status_code=409, content={
                "code": exc.code, "current_review_id": exc.detail,
                "message": "已有审查正在进行", "retry_after_s": None,
            })
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
        return {"repo_id": repo.repo_id, "display_name": repo.display_name}

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

    @app.get("/api/v2/reviews/{review_id}/repair")
    async def repair_status_v2(review_id: str):
        return manager.repair_status(review_id)

    @app.post("/api/v2/reviews/{review_id}/repair")
    async def deliver_repair_v2(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"patch"}:
            raise IntakeError("REPAIR_REQUEST_INVALID", "修复交付请求只接受 patch")
        return manager.deliver_repair(
            review_id, str(raw["patch"]),
            idempotency_key=request.headers.get("idempotency-key", ""))

    @app.post("/api/v2/reviews/{review_id}/repair/candidate")
    async def generate_repair_candidate_v2(review_id: str, request: Request):
        raw = await request.json()
        return manager.generate_repair(
            review_id, raw,
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

    @app.get("/api/v2/capabilities")
    async def capabilities_v2():
        return capability_registry(manager.capabilities)

    @app.get("/api/v2/release-status")
    async def release_status():
        return current_release_status()

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
