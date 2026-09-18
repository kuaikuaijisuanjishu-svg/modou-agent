"""Thin, authenticated client for a single running local control plane.

No ReviewManager, model credentials, approval or local Git writes live here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


class ClientError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClientError("SERVER_INVALID", "Invalid local server address") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not port or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ClientError("SERVER_INVALID", "Use an explicit loopback HTTP address and port")
    return value.rstrip("/")


def read_token(token_file: Path | None = None) -> str:
    token = os.environ.get("SHUIMU_LOCAL_TOKEN", "")
    if not token and token_file:
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ClientError("TOKEN_UNAVAILABLE", "Cannot read the configured token file") from exc
    if not token or any(ch.isspace() for ch in token):
        raise ClientError("TOKEN_REQUIRED", "Set SHUIMU_LOCAL_TOKEN or use --token-file")
    return token


class LocalClient:
    def __init__(self, server: str, token: str, *, timeout: float = 30):
        self.server = local_origin(server)
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, payload: dict | None = None,
                *, idempotency_key: str = "", timeout: float | None = None) -> dict:
        if not path.startswith("/api/") or "#" in path:
            raise ClientError("PATH_INVALID", "Only local API paths are accepted")
        headers = {"Authorization": "Bearer " + self.token,
                   "Origin": self.server, "Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(self.server + path, body, headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout if timeout is None else timeout) as response:
                value = json.loads(response.read(16_000_001))
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(100_000)).get("error", {})
                code, detail = error.get("code"), error.get("message")
            except (ValueError, AttributeError):
                code, detail = None, None
            raise ClientError(str(code or "HTTP_ERROR"),
                              str(detail or f"Local server returned HTTP {exc.code}")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClientError("SERVER_UNAVAILABLE", "The local server is not reachable") from exc
        except (ValueError, UnicodeError) as exc:
            raise ClientError("RESPONSE_INVALID", "Local server returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ClientError("RESPONSE_INVALID", "Local server response must be an object")
        return value

    def get(self, path: str) -> dict:
        """通用只读 GET。路径限制只有一个闸门——request 里的 /api/ 前缀检查；
        这里不新增白名单，否则两份清单迟早走散。"""
        return self.request("GET", path)

    def delegation_check(self, task_id: str, reason: str = "programming_entry") -> dict:
        """在既有有效授权下触发一次检查。

        与创建/续期授权是两件事：这里只能触发已授权范围内的检查与复验，
        服务的状态机决定投递、无变化还是拒绝（过期/停止/要求变化/额度用完
        都会被如实拒绝）。不提供任何创建、续期或扩大授权的入口。
        """
        return self.request("POST", "/api/v2/delegations/check",
                            {"task_id": task_id, "reason": reason})

    def task(self, task_id: str, *, receipt: bool = False) -> dict:
        return self.request("GET", "/api/v2/tasks/" + component(task_id)
                            + ("/receipt" if receipt else ""))

    def task_url(self, task_id: str, *, authenticated: bool = False) -> str:
        values = {"task": task_id}
        if authenticated:
            values["token"] = self.token
        return self.server + "/#" + urllib.parse.urlencode(values)

    def start(self, *, repo_id: str, goal: str, test_files: list[str] | None = None,
              budget_seconds: int = 120, idempotency_key: str = "", mode: str = "standard",
              language: str | None = None, test_targets: list[str] | None = None,
              adoption_mode: str = "manual") -> dict:
        raw = {"source": {"kind": "local", "repo_id": repo_id}, "goal": goal,
               "constraints": {"budget_seconds": budget_seconds}}
        if mode not in {"standard", "agent"}:
            raise ClientError("MODE_INVALID", "Mode must be standard or agent")
        if mode == "agent":
            raw["autonomy_policy"] = {"model_provider": "live", "allow_repair_branch": True}
            raw["data_policy"] = {"model_data_categories": ["selected_snippets"]}
        if test_files:
            raw["scope"] = {"test_files": test_files}
        review = self.request("POST", "/api/v2/reviews", raw,
                              idempotency_key=idempotency_key)
        review_id = review["review_id"]
        try:
            task_payload = {"review_id": review_id, "title": goal}
            if language or test_targets or adoption_mode != "manual":
                test_targets = list(test_targets or [])
                if any(not isinstance(v, str) or not v for v in test_targets):
                    raise ClientError("LANGUAGE_TARGETS_INVALID", "test targets must be nonempty strings")
                if adoption_mode not in {"manual", "preauthorized"}:
                    raise ClientError("ADOPTION_MODE_INVALID", "adoption mode must be manual or preauthorized")
                if language:
                    if language not in {"typescript", "go", "java"}:
                        raise ClientError("LANGUAGE_INVALID", "language must be typescript, go or java")
                    task_payload["criteria"] = [{"text": goal, "finding_ids": [],
                                                  "evidence_kind": "language_test_execution",
                                                  "language": language, "test_targets": test_targets,
                                                  "adoption_mode": adoption_mode}]
                elif test_targets:
                    raise ClientError("LANGUAGE_INVALID", "test targets require an explicit language")
                else:
                    task_payload["criteria"] = [{"text": goal, "finding_ids": [],
                                                  "adoption_mode": adoption_mode}]
            task = self.request("POST", "/api/v2/tasks", task_payload,
                                idempotency_key=idempotency_key)
        except ClientError as exc:
            raise ClientError(exc.code, f"Review {review_id} was created; task creation failed. "
                              "Attach it with the attach command. " + exc.detail) from exc
        return {"review": review, "task": task,
                "task_url": self.task_url(task["task_id"])}


def component(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ClientError("ID_INVALID", "A nonempty opaque identifier is required")
    return urllib.parse.quote(value, safe="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="水木验码：连接正在运行的本地后台")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("repos", help="查看已登记仓库")
    start = sub.add_parser("start", help="创建待确认审查与证据任务")
    start.add_argument("--repo-id", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--test-file", action="append")
    start.add_argument("--mode", choices=["standard", "agent"], default="standard",
                       help="agent 明确授权选定代码片段用于模型补测；计划仍需确认")
    start.add_argument("--budget", type=int, default=120)
    start.add_argument("--language", choices=["typescript", "go", "java"],
                       help="把任务声明为语言测试执行证据实验")
    start.add_argument("--test-target", action="append",
                       help="语言适配器的固定测试目标（可重复）")
    start.add_argument("--adoption-mode", choices=["manual", "preauthorized"], default="manual",
                       help="采用默认为人工确认；preauthorized 仅在服务端常驻授权匹配时可用")
    start.add_argument("--idempotency-key", default="")
    start.add_argument("--open", action="store_true")
    attach = sub.add_parser("attach", help="给已有审查建立任务")
    attach.add_argument("--review-id", required=True)
    attach.add_argument("--title", default="")
    delegation = sub.add_parser(
        "delegation", help="查看任务的持续委托授权（只读 GET，永不触发检查/投递）")
    delegation.add_argument("--task-id", required=True,
                            help="证据任务 id；行内含 derived_status、"
                                 "remaining_reverify 与 last_receipt")
    delegation_check = sub.add_parser(
        "delegation-check",
        help="在既有有效授权下触发一次检查（可能投递复验；不能创建/续期/扩大授权）")
    delegation_check.add_argument("--task-id", required=True)
    delegation_check.add_argument("--reason", default="programming_entry",
                                  help="检查来源说明，最长 200 字符")
    for command in ("status", "receipt", "open"):
        action = sub.add_parser(command)
        action.add_argument("task_id")
    args = parser.parse_args(argv)
    try:
        client = LocalClient(args.server, read_token(args.token_file))
        if args.command == "repos":
            value = client.request("GET", "/api/v1/repos")
        elif args.command == "start":
            value = client.start(repo_id=args.repo_id, goal=args.goal,
                                 test_files=args.test_file, budget_seconds=args.budget,
                                 idempotency_key=args.idempotency_key, mode=args.mode,
                                 language=args.language, test_targets=args.test_target,
                                 adoption_mode=args.adoption_mode)
            if args.open:
                webbrowser.open(client.task_url(value["task"]["task_id"], authenticated=True))
        elif args.command == "attach":
            payload = {"review_id": args.review_id}
            if args.title:
                payload["title"] = args.title
            value = client.request("POST", "/api/v2/tasks", payload)
        elif args.command == "delegation":
            value = client.get("/api/v2/delegations?task_id=" + component(args.task_id))
        elif args.command == "delegation-check":
            value = client.delegation_check(args.task_id, reason=args.reason[:200])
        else:
            value = client.task(args.task_id, receipt=args.command == "receipt")
            if args.command == "open":
                webbrowser.open(client.task_url(args.task_id, authenticated=True))
                value = {"task_id": args.task_id, "task_url": client.task_url(args.task_id)}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except ClientError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.detail}},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
