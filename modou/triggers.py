"""Explicit trigger entries (T13 / direction 8, task-completed boundary).

Three human- or tool-initiated triggers share one background-job protocol:
task completion, pre-commit and PR update.  Dedupe, queueing, budget and
cancellation live in the JobService; this module only builds the dispatch
bodies and enforces loop suppression — a head whose last write came from the
auto-adoption path is never re-triggered by an automatic entry.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

TRIGGER_KINDS = ("task_completed", "pre_commit", "pr_updated")


class TriggerError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def dispatch_body(kind: str, *, task_id: str, round_id: str, budget_id: str,
                  head: str = "", dirty_digest: str = "",
                  payload: dict[str, Any] | None = None) -> dict:
    """Build the POST /api/v2/jobs body for one trigger entry.

    N03：去重键包含 round 与工作树状态。只按 task+head 去重会把"相同
    HEAD、不同轮次"以及"两次不同脏工作树"的投递错误合并成一个作业；
    这里把 round_id 与 dirty 摘要编进键里。服务端还会用自己校验过的
    task_id/round_id 再绑定一次身份（JobService.create），客户端伪造
    键无法跨轮次合并。
    """
    if kind not in TRIGGER_KINDS:
        raise TriggerError("TRIGGER_KIND_INVALID", kind)
    if not task_id or not round_id or not budget_id:
        raise TriggerError("TRIGGER_REQUEST_INVALID", "task_id/round_id/budget_id required")
    if head:
        head_part = head
    elif dirty_digest:
        head_part = f"dirty-{dirty_digest[:16]}"
    else:
        head_part = "dirty"
    body = {
        "kind": "trigger_dispatch",
        "task_id": task_id,
        "round_id": round_id,
        "budget_id": budget_id,
        "dedupe_key": f"trigger:{kind}:{task_id}:{round_id}:{head_part}",
        "trigger_source": f"trigger:{kind}:{task_id}",
        "payload": {"entry": kind, "head": head, "task_id": task_id,
                     "round_id": round_id, **(payload or {})},
    }
    body["payload"]["dispatch_sha256"] = hashlib.sha256(json.dumps(
        body["payload"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return body


def loop_suppression(last_write_trigger: str | None, entry: str) -> dict:
    """A head last written by the auto path must not re-trigger itself.

    Human re-dispatch (explicit pre_commit by the user after seeing the
    receipt) stays possible and is not suppressed — only same-origin loops
    are: an `auto:` write cannot be followed by another `auto:` write from
    the same chain, and trigger entries do not adopt patches themselves.
    """
    if not last_write_trigger:
        return {"dispatched": True, "reason": ""}
    if last_write_trigger.startswith("auto:") and entry in ("task_completed",):
        return {"dispatched": False, "reason": "loop_suppressed",
                "note": "最近一次写入来自自动采用路径；同一版本不再自动触发"}
    return {"dispatched": True, "reason": ""}
