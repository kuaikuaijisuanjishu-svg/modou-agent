"""Real full-stack demo project (T08 / direction 2).

Generates a small but genuine front/back project (FastAPI CRUD backend plus a
static HTML+JS front page served by the same process), boots it on an
ephemeral port and drives the create / refresh-keeps / modify / delete path
against real HTTP.  Every step is recorded into a trace; the front-end layer
is verified as served assets that call the API (no browser screenshot in
this tier — stated honestly in the trace).
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

SCHEMA_VERSION = "fullstack-trace-v1"

BACKEND_PY = '''"""Minimal TODO CRUD backend with a served front page (demo project)."""
import uuid
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI()
ITEMS = {}

class Item(BaseModel):
    title: str
    done: bool = False

@app.get("/app")
def page():
    return HTMLResponse("<html><body><script>fetch('/api/items').then(r=>r.json()).then(render)</script></body></html>")

@app.get("/api/items")
def listing():
    return [{"id": i, **v} for i, v in sorted(ITEMS.items())]

def _missing():
    return JSONResponse(status_code=404, content={"error": "not_found", "field": "id"})

@app.get("/api/items/{item_id}")
def read_one(item_id: str):
    if item_id not in ITEMS:
        return _missing()
    return {"id": item_id, **ITEMS[item_id]}

@app.post("/api/items", status_code=201)
def create(item: Item):
    item_id = uuid.uuid4().hex[:8]
    ITEMS[item_id] = item.model_dump()
    return {"id": item_id, **ITEMS[item_id]}

@app.patch("/api/items/{item_id}")
def modify(item_id: str, item: Item):
    if item_id not in ITEMS:
        return _missing()
    ITEMS[item_id].update(item.model_dump())
    return {"id": item_id, **ITEMS[item_id]}

@app.delete("/api/items/{item_id}", status_code=204)
def remove(item_id: str):
    if item_id not in ITEMS:
        return _missing()
    del ITEMS[item_id]
'''

def write_demo_project(root: Path) -> Path:
    root = Path(root)
    (root / "static").mkdir(parents=True, exist_ok=True)
    (root / "backend.py").write_text(BACKEND_PY.replace("import json\n", ""))
    (root / "frontend" / "index.html").parent.mkdir(exist_ok=True)
    (root / "frontend" / "index.html").write_text(
        "<html><body><script>fetch('/api/items').then(r=>r.json()).then(render)</script></body></html>")
    return root


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def drive_full_stack(root: Path, *, timeout: float = 30.0) -> dict:
    """Boot the demo backend and drive the four-step real path with traces.

    Some local proxies answer loopback ports with a transparent 503, so a
    probe only counts when the demo backend itself answers 200; anything
    else retries on a fresh port.
    """
    root = Path(root)
    if not (root / "backend.py").is_file():
        write_demo_project(root)
    steps: list[dict] = []
    # Fixed demo ports first: local transparent proxies on this machine
    # answer ephemeral loopback ports with a 503 before uvicorn binds.
    import os
    ports = [18770 + (os.getpid() + attempt) % 25 for attempt in range(3)] + [_free_port()]
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + timeout
            healthy = False
            hijacked = False
            while time.time() < deadline:
                try:
                    with httpx.Client(trust_env=False) as client:
                        probe = client.get(base + "/api/items", timeout=2)
                except httpx.HTTPError:
                    time.sleep(0.15)
                    continue
                if probe.status_code == 200:
                    healthy = True
                    break
                hijacked = True
                break
            if not healthy:
                if hijacked:
                    continue
                return {"schema_version": SCHEMA_VERSION, "status": "env_unavailable",
                        "steps": steps, "note": "backend failed to start"}
            with httpx.Client(base_url=base, trust_env=False) as client:
                page = client.get("/app")
                front_src = (root / "frontend" / "index.html").read_bytes()
                steps.append({"step": "front_assets", "status": page.status_code,
                              "frontend_calls_api": b"/api/items" in page.content,
                              "static_asset_ok": b"/api/items" in front_src})
                created = client.post("/api/items", json={"title": "buy milk"})
                item = created.json()
                steps.append({"step": "create", "status": created.status_code, "body": item})
                kept = client.get("/api/items")
                steps.append({"step": "refresh_keeps", "status": kept.status_code,
                              "kept": any(row["id"] == item["id"] for row in kept.json())})
                modified = client.patch(f"/api/items/{item['id']}", json={"title": "buy oat milk"})
                steps.append({"step": "modify", "status": modified.status_code,
                              "body": modified.json()})
                removed = client.request("DELETE", f"/api/items/{item['id']}")
                gone = client.get(f"/api/items/{item['id']}")
                steps.append({"step": "delete", "status": removed.status_code,
                              "after_status": gone.status_code,
                              "error_format": gone.json() if gone.content else None})
            ok = (steps[0]["frontend_calls_api"] and steps[0]["static_asset_ok"]
                  and steps[1]["status"] == 201 and steps[2]["kept"]
                  and steps[3]["status"] == 200 and steps[3]["body"]["title"] == "buy oat milk"
                  and steps[4]["status"] == 204 and steps[4]["after_status"] == 404
                  and steps[4]["error_format"] == {"error": "not_found", "field": "id"})
            return {"schema_version": SCHEMA_VERSION, "status": "pass" if ok else "fail",
                    "steps": steps, "trace_ref": "steps",
                    "boundary": ("这是登记演示项目的端到端行为证据；不是任意全栈项目的支持声明。"
                                 "本层验证了前端资源与API两层，没有浏览器截图。"),
                    "browser_layer": "not_run_no_screenshot"}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
