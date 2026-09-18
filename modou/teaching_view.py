"""Teaching view: narrate a code-intervention experiment for learners.

The module explains what code was taken away, which named tests changed
behaviour, and why a conclusion is or is not warranted.  It deliberately
provides no scoring and no authorship detection: those are out of scope by
design and no such interface exists here.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

SCHEMA_VERSION = "teaching-view-v1"

_CONCLUSIVE = ("拿走目标代码后相关测试失败、恢复后重新通过："
               "这些测试确实覆盖该行为，实验可以下结论。")
_NOT_COVERING = ("拿走目标代码后相关测试仍然全部通过："
                 "这些测试没有覆盖该行为，不能声称有测试证据。")
_INVALID_COLLECTION = ("测试在收集阶段就失败了（例如 import 崩溃），"
                       "没有任何测试真正运行，本实验不产生证据。")
_BOUNDARY = ("教学叙述只解释本次干预实验；它不评价代码质量，"
             "也不构成对作业或作者的评价。")


def _digest(payload: dict[str, Any]) -> str:
    return "teach-" + hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                                  sort_keys=True).encode()).hexdigest()[:12]


def _required(raw, key, kind, name):
    value = raw.get(key)
    if not isinstance(value, kind):
        raise TeachingViewError("TEACHING_INPUT_INVALID", f"{name} must be {kind.__name__}")
    return value


def _statuses(rows, name) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("id", "")).strip():
            raise TeachingViewError("TEACHING_INPUT_INVALID", f"{name} entries need an id")
        out[str(row["id"])] = str(row.get("status", "unknown"))
    return out


class TeachingViewError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def narrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn one intervention run into the teaching narrative shape."""
    if not isinstance(raw, dict):
        raise TeachingViewError("TEACHING_INPUT_INVALID", "input must be an object")
    target = _required(raw, "target", dict, "target")
    took = {"file": str(target.get("file", "")),
            "lines": target.get("lines") if isinstance(target.get("lines"), list) else [],
            "excerpt": str(target.get("excerpt", ""))}
    before = _statuses(_required(raw, "tests_before", list, "tests_before"), "tests_before")
    after = _statuses(_required(raw, "tests_after", list, "tests_after"), "tests_after")
    related = [str(x) for x in _required(raw, "related_tests", list, "related_tests")]
    if not related:
        raise TeachingViewError("TEACHING_INPUT_INVALID", "related_tests must not be empty")

    collection_failed = any(status in ("error", "collection_error")
                            for status in after.values())
    tests_changed = [{"id": tid,
                      "before": before.get(tid, "missing"),
                      "after": after.get(tid, "missing")}
                     for tid in sorted(set(before) | set(after))
                     if before.get(tid) != after.get(tid)]
    related_after = {tid: after.get(tid, "missing") for tid in related}
    if collection_failed:
        outcome = "inconclusive_collection_failure"
        why = _INVALID_COLLECTION
    elif all(status == "failed" for status in related_after.values()):
        outcome = "conclusive"
        why = _CONCLUSIVE
    elif all(status == "passed" for status in related_after.values()):
        outcome = "not_covering"
        why = _NOT_COVERING
    else:
        outcome = "partially_covering"
        why = ("部分相关测试失败、部分仍通过：只有失败的测试覆盖了该行为，"
               "通过的部分不构成证据。")
    return {"schema_version": SCHEMA_VERSION, "narrative_id": _digest(raw),
            "took_away": took, "tests_changed": tests_changed,
            "related_tests": related, "outcome": outcome,
            "why_conclusive": why, "conclusion_boundary": _BOUNDARY,
            "no_scoring": True, "no_authorship_detection": True}


LESSON_CASES = {
    "lesson-covered": {
        "target": {"file": "cart.py", "lines": [40, 45],
                   "excerpt": "def total(): ..."},
        "tests_before": [{"id": "tests/test_cart.py::test_total", "status": "passed"}],
        "tests_after": [{"id": "tests/test_cart.py::test_total", "status": "failed"}],
        "related_tests": ["tests/test_cart.py::test_total"],
    },
    "lesson-not-covered": {
        "target": {"file": "cart.py", "lines": [40, 45], "excerpt": "def total(): ..."},
        "tests_before": [{"id": "tests/test_cart.py::test_total", "status": "passed"}],
        "tests_after": [{"id": "tests/test_cart.py::test_total", "status": "passed"}],
        "related_tests": ["tests/test_cart.py::test_total"],
    },
    "lesson-collection-failure": {
        "target": {"file": "cart.py", "lines": [1, 2], "excerpt": "import money"},
        "tests_before": [{"id": "tests/test_cart.py::test_total", "status": "passed"}],
        "tests_after": [{"id": "tests/test_cart.py::test_total", "status": "error"}],
        "related_tests": ["tests/test_cart.py::test_total"],
    },
}


def lesson(case_id: str) -> dict[str, Any]:
    """Return one of the three fixed teaching cases, narrated."""
    if case_id not in LESSON_CASES:
        raise TeachingViewError("LESSON_NOT_FOUND", case_id)
    return narrate(json.loads(json.dumps(LESSON_CASES[case_id])))


def lessons() -> list[str]:
    return sorted(LESSON_CASES)
