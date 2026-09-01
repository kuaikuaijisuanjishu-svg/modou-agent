"""Authenticated same-origin FastAPI control plane for local Shuimu Yanma reviews."""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..capabilities import public_registry as capability_registry
from .control import IntakeError, ReviewManager


API_PREFIX = "/api/v1"


def create_app(*, manager: ReviewManager, token: str,
               host: str = "127.0.0.1", port: int = 8765,
               web_dist: Path | None = None) -> FastAPI:
    expected_host = f"{host}:{port}"
    expected_origin = f"http://{expected_host}"
    app = FastAPI(title="水木验码本地控制面", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.manager = manager
    app.state.token = token

    @app.middleware("http")
    async def secure_boundary(request: Request, call_next):
        if request.url.path.startswith(API_PREFIX):
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
        if exc.code in {"LIVE_RUN_BUSY", "STALE_PLAN"}:
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
        return manager.create(raw)

    @app.get(f"{API_PREFIX}/reviews/{{review_id}}")
    async def get_review(review_id: str):
        return manager.describe(review_id)

    @app.post(f"{API_PREFIX}/reviews/{{review_id}}/approval")
    async def approve(review_id: str, request: Request):
        raw = await request.json()
        if not isinstance(raw, dict) or set(raw) != {"plan_sha256"}:
            raise IntakeError("APPROVAL_INVALID", "approval requires only plan_sha256")
        return manager.approve(review_id, str(raw["plan_sha256"]))

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
