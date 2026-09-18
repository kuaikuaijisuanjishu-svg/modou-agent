"""MCP stdio server: --connect shares one existing backend; standalone owns one manager.

以下进程内直连边界仅指旧独占模式：

为什么不走 HTTP：`modou/server/app.py` 的那层边界（Host/Origin 校验、Bearer
令牌、CSP）存在的理由是**浏览器**——一个同机的恶意页面能向 localhost 发请求。
MCP 客户端是用户自己启动的子进程，stdin/stdout 就是信道，没有第三方能挤进来。
再套一层 HTTP 只会多出一个端口和一个必须传递的令牌，并不多出一分隔离。
所以这里按 `modou/server/__main__.py` 和 `web/e2e/serve_real_backend.py` 的同一
套路，进程内实例化 RepoRegistry + ReviewManager。

`--allow-repo` 的语义与现有启动器完全一致：**没有被显式登记的仓库，一行代码都
不会被执行**。MCP 客户端只拿得到不透明的 repo_id。

工具面故意只读多写少：四个只读投影 + 一个创建。创建停在 AWAITING_APPROVAL，
批准仍然只能由人在 Cockpit 里做——模型不批准自己的计划。

`--connect` 模式另有一组任务只读投影（轮次/作业/回执/委托授权）。持续委托的
创建与停止**不在其中**：授权始终由用户在页面里显式给出，MCP 面连检查事件
入口都没有——GET 永不触发实验，委托有效也不等于验收通过。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

from modou.inputs import InputError
from modou.ledger import records as ledger_records
from modou.server.control import IntakeError, RepoRegistry, ReviewManager

from .credentials import live_provider
from modou.task_client import ClientError, LocalClient, component, read_token


SERVER_NAME = "shuimu-yanma"
SERVER_VERSION = "0.2.0-experimental"
#: 已知的协议版本。客户端报的版本在列表里就照回，否则回落到默认——
#: 版本协商失败时协议要求服务端给出自己支持的版本，而不是静默装作兼容。
KNOWN_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18",
                           "2026-07-28")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

_REVIEW_ID = {"type": "string", "description": "create_review 返回的 review_id"}


TOOLS = [
    {
        "name": "create_review",
        "description": (
            "在已登记的本地仓库上创建一次审查，等价于 POST /api/v2/reviews。"
            "创建后停在 AWAITING_APPROVAL：计划已冻结并给出 plan_sha256，但"
            "**不会自动批准、不会执行任何测试**。批准由人在 Cockpit 完成。"
            "instruction 与 goal 至少给一个——审查目标要由调用方说出来，"
            "不替你默认一个，它会进 review_spec 和证据包。"
            "省略 test_files 时由后端在仓库里自动发现 Python 测试文件。"
            "要让 propose_and_verify_test 在这次审查上可用，必须同时把"
            "allow_repair_branch 设为 true，并在 model_data_categories 里"
            "带上 selected_snippets——两者都是授权，默认都不给。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_id": {"type": "string",
                            "description": "list_repos 给出的不透明仓库 id"},
                "instruction": {"type": "string",
                                "description": "一句话说明要审什么；与 goal 至少给一个"},
                "goal": {"type": "string"},
                "test_files": {"type": "array", "items": {"type": "string"},
                               "description": "仓库内相对路径的声明测试文件；省略则自动发现"},
                "review_focus": {"type": "string"},
                "budget_seconds": {"type": "integer"},
                "model_provider": {"type": "string",
                                   "enum": ["deterministic", "live"]},
                "allow_repair_branch": {
                    "type": "boolean",
                    "description": "授权修复分支与测试提议；默认 false"},
                "model_data_categories": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("允许送进模型的数据类别；propose_and_verify_test "
                                    "需要其中含 selected_snippets")},
            },
            "required": ["repo_id"],
            "anyOf": [{"required": ["instruction"]}, {"required": ["goal"]}],
        },
    },
    {
        "name": "get_status",
        "description": (
            "读取一次审查的状态，投影自 GET /api/v1/reviews/{review_id}。"
            "默认省略冻结计划的正文，只保留 plan_sha256。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "review_id": _REVIEW_ID,
                "include_plan": {"type": "boolean",
                                 "description": "为真时附上冻结计划全文"},
            },
            "required": ["review_id"],
        },
    },
    {
        "name": "get_findings",
        "description": (
            "读取已完成审查的主张与叙述，投影自 GET /api/v1/reviews/{review_id}"
            "/bundle。注意：产品里没有名为 finding 的记录类型——这里返回的是"
            "证据账本中的 Claim 记录（每条都带 provenance）与由它们编译出的"
            "narration。审查尚未产出 bundle 时返回 BUNDLE_NOT_READY。"),
        "inputSchema": {
            "type": "object",
            "properties": {"review_id": _REVIEW_ID},
            "required": ["review_id"],
        },
    },
    {
        "name": "get_evidence",
        "description": (
            "按 record_id 取一条证据账本记录，等价于 GET /api/v1/reviews/"
            "{review_id}/evidence/{evidence_id}。record_id 来自 get_findings "
            "返回的 provenance。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "review_id": _REVIEW_ID,
                "evidence_id": {"type": "string",
                                "description": "账本记录的 record_id"},
            },
            "required": ["review_id", "evidence_id"],
        },
    },
    {
        "name": "list_repos",
        "description": "列出后台已明确登记的仓库，等价于 GET /api/v1/repos。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_and_verify_test",
        "description": (
            "让模型为证据缺口提议一条测试，并让它在隔离工作树里**挣到签证**："
            "先跑绿（最多两轮修订），再过三段验证——断言级失败、同一干预下 k=2 "
            "确定性复跑、在模型全程没见过的保留集干预上复现。"
            "status 是 PASSED / PASSED_WITH_REVISION / NEEDS_HUMAN；"
            "visa.status 是 VERIFIED_EFFECTIVE / PASSED_NOT_EFFECTIVE / "
            "WITHHELD_SMALL_CASE / WITHHELD_NO_HOLDOUT / VISA_ERROR，"
            "只有 VERIFIED_EFFECTIVE 才让 effective 为真。"
            "要求这次审查创建时授权过 allow_repair_branch 与 selected_snippets，"
            "且服务端以 --model 启动。全程只读用户 checkout：测试文件只存在于"
            "用完即弃的工作树，Git 不会为它建任何分支或 commit。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "review_id": _REVIEW_ID,
                "finding_ids": {
                    "type": "array", "items": {"type": "string"},
                    "maxItems": 20,
                    "description": ("要为哪些缺口提议，形如 `app.py:3`；"
                                    "省略或空表示交给后端按缺口排序挑选")},
            },
            "required": ["review_id"],
        },
    },
]


# ------------------------------------------------------------------ 工具实现

#: 扁平入参 → ReviewSpec 里嵌套对象的落点。字段名一律沿用 spec 自己的名字，
#: 扁平化只是为了让模型少写一层嵌套，不是为了另起一套词汇。
_SPEC_NESTING = {
    "test_files": ("scope", "test_files"),
    "budget_seconds": ("constraints", "budget_seconds"),
    "model_provider": ("autonomy_policy", "model_provider"),
    "allow_repair_branch": ("autonomy_policy", "allow_repair_branch"),
    "model_data_categories": ("data_policy", "model_data_categories"),
}


def _create_review(manager: ReviewManager, args: dict) -> dict:
    """把扁平入参装回 v2 review spec，校验全部留给 ReviewManager.create_v2。

    这里一行校验都不复述：复述出来的校验会和 REST 那份慢慢走散，而走散的那天
    没有任何测试会红。扁平化只针对嵌套层次，键名全部沿用 spec 的原名。

    走 v2 而不是 v1，是因为 v1 不带 review_spec，而没有 review_spec 就没有
    autonomy_policy——`propose_test` 要的授权根本无处表达，那个工具接线后也只会
    一直回 REPAIR_NOT_AUTHORIZED。v2 让授权成为调用方能明确给出的东西，
    且默认不给。
    """
    raw: dict = {"source": {"kind": "local",
                            "repo_id": str(args.get("repo_id") or "")}}
    for field in ("instruction", "goal", "review_focus"):
        if field in args:
            raw[field] = args[field]
    for field, (container, key) in _SPEC_NESTING.items():
        if field in args:
            raw.setdefault(container, {})[key] = args[field]
    return manager.create_v2(raw)


def _get_status(manager: ReviewManager, args: dict) -> dict:
    described = manager.describe(str(args.get("review_id") or ""))
    if args.get("include_plan"):
        return described
    # 收窄而非改写：plan 自己就带 plan_sha256，这里只是不把正文塞进对话。
    plan = described.get("plan")
    narrowed = dict(described)
    narrowed["plan"] = ({"plan_sha256": plan.get("plan_sha256", "")}
                        if isinstance(plan, dict) else plan)
    return narrowed


def _get_findings(manager: ReviewManager, args: dict) -> dict:
    review_id = str(args.get("review_id") or "")
    bundle = (manager.bundle(review_id) if isinstance(manager, ConnectedBackend) else
              json.loads(manager.review_bundle_path(review_id).read_text(encoding="utf-8")))
    rows = (bundle.get("evidence_bundle") or {}).get("ledger") or []
    # 字段名照抄 ReviewManager._narrate 已有的 claim 投影，一个不加：
    # 模型看到的主张口径必须和 Cockpit 看到的是同一个。
    claims = [{"record_id": row["record_id"],
               "claim_type": row["payload"]["kind"],
               "anchor": row["payload"]["anchor"],
               "provenance": row["payload"]["provenance"]}
              for row in rows
              if row.get("record_type") == ledger_records.CLAIM]
    return {"review_id": bundle.get("review_id", review_id),
            "schema_version": bundle.get("schema_version", ""),
            "claims": claims,
            "narration": bundle.get("narration") or {},
            "restore_state": bundle.get("restore_state") or {}}


def _get_evidence(manager: ReviewManager, args: dict) -> dict:
    return manager.evidence(str(args.get("review_id") or ""),
                            str(args.get("evidence_id") or ""))


def _list_repos(manager: ReviewManager, _args: dict) -> dict:
    return {"repos": manager.registry.public()}


def _propose_and_verify_test(manager: ReviewManager, args: dict) -> dict:
    """接到 ReviewManager.propose_test：提议、隔离执行、挣签证。

    `propose_test` 要求传入的字典**恰好**只有 finding_ids 一个键（多一个少一个
    都是 TEST_PROPOSAL_CONTEXT_INVALID），所以这里只装配那一个，省略时补成空
    列表——空列表是「交给后端挑」，不是「没有缺口」。review_id 是位置参数，
    不能混进去。

    返回的是产品自己的 test-proposal-record-v1，原样透出：签证结论会进证据，
    在这一层改写它等于让 MCP 和 Cockpit 对同一条测试给出两种说法。
    """
    finding_ids = args.get("finding_ids") or []
    return manager.propose_test(str(args.get("review_id") or ""),
                                {"finding_ids": finding_ids})


HANDLERS = {
    "create_review": _create_review,
    "get_status": _get_status,
    "get_findings": _get_findings,
    "get_evidence": _get_evidence,
    "list_repos": _list_repos,
    "propose_and_verify_test": _propose_and_verify_test,
}


class ConnectedBackend:
    """HTTP facade: deliberately never constructs a registry or ReviewManager."""
    def __init__(self, client: LocalClient):
        self.client = client
        self.registry = self

    def public(self):
        return self.client.request("GET", "/api/v1/repos")["repos"]

    def create_v2(self, raw):
        return self.client.request("POST", "/api/v2/reviews", raw)

    def describe(self, review_id):
        return self.client.request("GET", "/api/v1/reviews/" + component(review_id))

    def bundle(self, review_id):
        return self.client.request("GET", "/api/v1/reviews/" + component(review_id) + "/bundle")

    def evidence(self, review_id, evidence_id):
        return self.client.request("GET", "/api/v1/reviews/" + component(review_id)
                                   + "/evidence/" + component(evidence_id))

    def propose_test(self, review_id, raw):
        return self.client.request("POST", "/api/v2/reviews/" + component(review_id)
                                   + "/test-proposal", raw, timeout=600)


def _create_task(backend, args):
    raw = {key: args[key] for key in ("review_id", "title", "criteria") if key in args}
    return backend.client.request("POST", "/api/v2/tasks", raw)


def _task_action(backend, args):
    """Forward one explicitly named Task action to the shared local backend."""
    required = ("task_id", "action", "criterion_id")
    if any(not isinstance(args.get(key), str) or not args[key].strip() for key in required):
        raise ClientError("TASK_ACTION_INVALID", "task_id, action and criterion_id are required")
    raw = {key: args[key] for key in (
        "action", "criterion_id", "finding_ids", "disposition", "reason", "handled_by",
        "source", "expected_sha256", "budget_seconds", "plan_sha256") if key in args}
    digest = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    key = str(args.get("idempotency_key") or "mcp-task-action-" + digest)
    return backend.client.request(
        "POST", "/api/v2/tasks/" + component(args["task_id"]) + "/actions", raw,
        idempotency_key=key)


def _get_task(backend, args):
    return backend.client.task(str(args.get("task_id") or ""))


def _get_task_rounds(backend, args):
    return backend.client.get("/api/v2/tasks/"
                              + component(str(args.get("task_id") or "")) + "/rounds")


def _get_task_jobs(backend, args):
    return backend.client.get("/api/v2/jobs?task_id="
                              + component(str(args.get("task_id") or "")))


def _get_task_delegation(backend, args):
    return backend.client.get("/api/v2/delegations?task_id="
                              + component(str(args.get("task_id") or "")))


def _check_task_delegation(backend, args):
    # 在既有有效授权下触发检查事件。服务端状态机决定
    # 投递/无变化/拒绝；这里没有任何创建、续期或扩大授权的入口。
    return backend.client.delegation_check(str(args.get("task_id") or ""),
                                           str(args.get("reason") or "mcp"))


CONNECTED_HANDLERS = {
    "create_task": _create_task,
    "task_action": _task_action,
    "get_task": _get_task,
    "get_task_receipt": lambda backend, args: backend.client.task(
        str(args.get("task_id") or ""), receipt=True),
    "get_task_rounds": _get_task_rounds,
    "get_task_jobs": _get_task_jobs,
    "get_task_delegation": _get_task_delegation,
    "check_task_delegation": _check_task_delegation,
}
CONNECTED_TOOLS = [
    {"name": "create_task", "description": "将已有审查组织为证据任务；不会批准、采用或执行。",
     "inputSchema": {"type": "object", "properties": {
         "review_id": _REVIEW_ID, "title": {"type": "string"},
         "criteria": {"type": "array", "items": {"type": "object", "properties": {
             "text": {"type": "string"},
             "finding_ids": {"type": "array", "items": {"type": "string"}},
             "evidence_kind": {"type": "string", "enum": ["python_experiment", "test_execution", "browser_behavior", "language_test_execution"]},
             "profile_id": {"type": "string"}, "language": {"type": "string"},
             "adapter_id": {"type": "string"},
             "test_targets": {"type": "array", "items": {"type": "string"}},
             "adoption_mode": {"type": "string", "enum": ["manual", "preauthorized"]},
             "acceptance_pack_id": {"type": "string",
                                    "description": "可选的机器可读验收包；只校验输入契约，不自报结果"}},
             "required": ["text", "finding_ids"]}}}, "required": ["review_id"]}},
    {"name": "task_action", "description": "在同一已运行的本地后台执行一个服务端允许的任务动作；不接受任意命令，采用与复验仍受后台状态、快照和授权闸门约束。",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"},
         "action": {"type": "string", "enum": ["set_targets", "select_disposition", "propose_test", "bind_candidate", "prepare_adoption", "confirm_adoption", "auto_confirm_adoption", "retry_review", "recover_adoption", "run_experiment"]},
         "criterion_id": {"type": "string"},
         "finding_ids": {"type": "array", "items": {"type": "string"}},
         "disposition": {"type": "string", "enum": ["add_tests", "controlled_repair", "accept_risk"]},
         "reason": {"type": "string"}, "handled_by": {"type": "string"},
         "source": {"type": "string", "enum": ["test_proposal", "repair_candidate"]},
         "expected_sha256": {"type": "string"}, "budget_seconds": {"type": "number"},
         "plan_sha256": {"type": "string"}, "idempotency_key": {"type": "string"}},
         "required": ["task_id", "action", "criterion_id"]}},
    *[{"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}
      for name, description in (
        ("get_task", "读取任务状态、可用动作及关联版本。"),
        ("get_task_receipt",
         "读取任务的原始聚合回执（含 receipt_sha256）。工具调用成功只说明"
         "读到了回执；业务结论必须看回执里的 status 字段。"),
        ("get_task_rounds",
         "读取任务的要求版本轮次列表与活动轮次，投影自 "
         "GET /api/v2/tasks/{id}/rounds。轮次推进是复验留下的历史事实，"
         "不构成验收通过的声明。"),
        ("get_task_jobs",
         "读取任务的作业列表（含委托复验作业），投影自 GET /api/v2/jobs?"
         "task_id=。作业 completed 只说明实验跑完并结算了预算，结论以回执"
         " status 为准。"),
        ("get_task_delegation",
         "读取任务的持续委托授权列表（只读 GET，永不触发检查或投递）。"
         "边界：委托有效≠验收通过，作业完成≠验收成功；创建、续期与停止"
         "授权是用户在页面里的动作，本工具面没有这些入口。"))],
    {"name": "check_task_delegation", "description": (
        "在既有有效授权下触发一次持续委托检查（POST /api/v2/delegations/check）："
        "源码有变化且额度/期限允许时投递复验作业；无变化只记录检查；"
        "无授权/过期/停止/要求已变/额度用完都会被如实拒绝，outcome 字段"
        "逐一如实返回。边界：本工具不能创建、续期或扩大授权，也不能绕过"
        "次数与预算；投递成功只说明复验作业已排队，结论以回执 status 为准。"),
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"},
         "reason": {"type": "string", "description": "检查来源说明，可选"}},
         "required": ["task_id"]}},
]


# ------------------------------------------------------------------ 协议

def _text_result(payload: dict, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text",
                         "text": json.dumps(payload, ensure_ascii=False,
                                            sort_keys=True, indent=2)}],
            "isError": is_error}


def _call_tool(manager: ReviewManager, params: dict) -> dict:
    name = str(params.get("name") or "")
    handlers = dict(HANDLERS)
    if isinstance(manager, ConnectedBackend):
        handlers.update(CONNECTED_HANDLERS)
    handler = handlers.get(name)
    if handler is None:
        return _text_result({"code": "UNKNOWN_TOOL", "message": name},
                            is_error=True)
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _text_result({"code": "ARGUMENTS_INVALID",
                             "message": "arguments must be an object"},
                            is_error=True)
    try:
        return _text_result(handler(manager, arguments))
    except (InputError, IntakeError, ClientError) as exc:
        # 产品自己的错误码原样透出。翻译一遍只会让 MCP 侧和 REST 侧对同一个
        # 拒绝给出两个名字。InputError 没有 .code：它是输入层拒绝，统一给
        # INPUT_REJECTED，原文消息带走，绝不能落进 INTERNAL_ERROR——
        # "没有补丁可审查"是调用方的输入问题，不是服务端 bug。
        code = getattr(exc, "code", None) or "INPUT_REJECTED"
        return _text_result({"code": code, "message": exc.detail
                             if hasattr(exc, "detail") else str(exc)},
                            is_error=True)
    except Exception as exc:                       # noqa: BLE001
        return _text_result({"code": "INTERNAL_ERROR",
                             "message": f"{type(exc).__name__}: {exc}"},
                            is_error=True)


def dispatch(manager: ReviewManager, message: dict) -> dict | None:
    """处理一条 JSON-RPC 请求；通知（没有 id）返回 None 表示不回包。"""
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    message_id = message.get("id")
    if message_id is None:
        return None
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in KNOWN_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        result = {"protocolVersion": version,
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": SERVER_NAME,
                                 "version": SERVER_VERSION}}
    elif method == "tools/list":
        result = {"tools": TOOLS + (CONNECTED_TOOLS if isinstance(manager, ConnectedBackend) else [])}
    elif method == "tools/call":
        result = _call_tool(manager, params)
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": message_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def serve(manager: ReviewManager, stdin: io.TextIOBase,
          stdout: io.TextIOBase) -> int:
    """行分隔的 JSON-RPC 主循环。

    调用方必须先把进程的 `sys.stdout` 挪开（见 `main`）：stdout 上任何一个
    多余的 print 都会被客户端当成畸形协议帧，而分析流水线里有的是打印。
    """
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        if not isinstance(message, dict):
            continue
        response = dispatch(manager, message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m modou.mcp",
        description="水木验码 MCP stdio server")
    parser.add_argument("--allow-repo", action="append", default=[], type=Path,
                        help="明确允许执行测试的 Git 仓库（可重复）")
    parser.add_argument("--connect", help="连接既有本地后台；不会创建第二个审查管理器")
    parser.add_argument("--token-file", type=Path, help="连接令牌文件，也可用 SHUIMU_LOCAL_TOKEN")
    parser.add_argument("--repo-python", action="append", default=[],
                        metavar="REPO=PYTHON",
                        help="为登记仓库绑定其测试解释器；客户端不能覆盖")
    parser.add_argument("--reviews-root", type=Path,
                        default=None)
    parser.add_argument("--model", action="store_true",
                        help="启用真实模型：密钥取自钥匙串或环境，绝不读 MCP 配置文件")
    parser.add_argument("--base-url", default=None,
                        help="模型服务地址；默认用启动器里选中的那一家")
    parser.add_argument("--model-id", default=None,
                        help="模型 id；默认用启动器里选中的那一家")
    return parser


def _manager_from(parser: argparse.ArgumentParser,
                  args: argparse.Namespace) -> ReviewManager:
    if args.connect:
        if args.allow_repo or args.repo_python or args.reviews_root or args.model or args.base_url or args.model_id:
            parser.error("--connect cannot be combined with standalone repository/model settings")
        try:
            return ConnectedBackend(LocalClient(args.connect, read_token(args.token_file)))
        except ClientError as exc:
            parser.error(exc.detail)
    if not args.allow_repo:
        parser.error("standalone mode requires --allow-repo; use --connect for an existing backend")
    python_by_repo = {}
    for binding in args.repo_python:
        if "=" not in binding:
            parser.error("--repo-python 必须是 REPO=PYTHON")
        repo_text, python_text = binding.split("=", 1)
        python_by_repo[Path(repo_text)] = Path(python_text)
    registry = RepoRegistry(args.allow_repo, python_by_repo=python_by_repo)
    # 默认不带 provider：被客户端拉起的进程不该顺手持有凭据。要用模型就显式
    # 说一次 --model，而且拿不到密钥时当场报错，绝不静默降级成确定性——
    # 静默降级会让调用方以为模型在参与，而证据包里根本没有模型的份。
    provider = (live_provider(base_url=args.base_url,
                              model_id=args.model_id,
                              fail=parser.error)
                if args.model else None)
    return ReviewManager(registry, root=args.reviews_root or Path.home() / ".modou" / "reviews", provider=provider)


def build_manager(argv: list[str] | None = None) -> ReviewManager:
    parser = _parser()
    return _manager_from(parser, parser.parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    # 先解析参数，再夺 stdout：`--help` 和参数错误是说给人听的，该走真正的
    # stdout；此刻还没有客户端在管道那头等协议帧。
    parser = _parser()
    args = parser.parse_args(argv)
    protocol_out = sys.stdout
    # 唯一一次夺走 stdout：此后进程里任何 print 都去 stderr，协议帧只从
    # protocol_out 出。必须抢在仓库登记之前——登记期就会跑 git，而流水线里
    # 到处是打印；晚一行，第一个 print 就变成一帧畸形协议。
    sys.stdout = sys.stderr
    return serve(_manager_from(parser, args), sys.stdin, protocol_out)


if __name__ == "__main__":
    raise SystemExit(main())
