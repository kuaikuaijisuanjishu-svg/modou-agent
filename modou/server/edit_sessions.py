"""编辑会话的纯逻辑层：状态机、ID、孤儿判定。

会话只是 repair.py 那条唯一写入边界外面的账本，不是第二条写入
路径：这里没有任何代码触碰用户 checkout。落盘格式、目录布局与
reverifications 完全同构（review 目录下一个 JSON 一条记录、原子
写、只追加不删除），宿主 control.py 负责真正读写。
"""
from __future__ import annotations

import os


EDIT_SESSION_SCHEMA_VERSION = "edit-session-v1"

#: 封闭状态集。前四个是活动态；delivered/abandoned/stale 是终态。
OPEN = "open"
PATCH_CANDIDATE = "patch_candidate"
VERIFIED = "verified"
AWAITING_DELIVERY = "awaiting_delivery"
DELIVERED = "delivered"
ABANDONED = "abandoned"
STALE = "stale"

TERMINAL_STATES = frozenset({DELIVERED, ABANDONED, STALE})
ACTIVE_STATES = frozenset({OPEN, PATCH_CANDIDATE, VERIFIED, AWAITING_DELIVERY})

# 与 RepairError/IntakeError 一样：错误码是契约的一部分，任何故障
# 都不允许塌缩成异常类名（R16 的教训）。
EXIT_REASONS = {
    "delivered": "repair_delivered",
    "abandoned": "orphan_owner_process_gone",
    "stale": "source_snapshot_changed",
}


def new_session_id(existing: list[str]) -> str:
    """按既有编号顺序取下一个 es-NNNNNN；不依赖时钟，重启可复现。"""
    highest = 0
    for session_id in existing:
        suffix = str(session_id).rsplit("-", 1)[-1]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"es-{highest + 1:06d}"


def can_transition(src: str, dst: str) -> bool:
    """open → patch_candidate → verified → awaiting_delivery → delivered。

    abandoned/stale 可从任何活动态进入（强杀收割、源快照变化）；
    终态不可再变。verified → awaiting_delivery 在真实交付里发生在
    验证器闭包内，这一步让"验证已过、提交未落"的崩溃窗口可见。
    """
    if src == dst:
        return False
    if src in TERMINAL_STATES:
        return False
    forward = {
        OPEN: {PATCH_CANDIDATE, ABANDONED, STALE},
        PATCH_CANDIDATE: {VERIFIED, ABANDONED, STALE},
        VERIFIED: {AWAITING_DELIVERY, ABANDONED, STALE},
        AWAITING_DELIVERY: {DELIVERED, ABANDONED, STALE},
    }
    return dst in forward.get(src, set())


def pid_alive(pid: int) -> bool:
    """os.kill(pid, 0)：进程不在了（或不是我们的）就算死。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_orphan(record: dict) -> bool:
    """活动态且 owner 进程已死 → 孤儿，可收割。"""
    if str(record.get("status") or "") not in ACTIVE_STATES:
        return False
    return not pid_alive(int(record.get("owner_pid") or 0))
