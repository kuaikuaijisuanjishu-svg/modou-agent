"""证据关联图与保守失效列表（总计划 T10·方向 11）。

输入是 TaskService.get() 形状的任务 JSON（T01 已实现：``rounds`` 闭合轮次、
``active_round``、``criteria`` 的 target_mapping/prior_state/historical_outcome），
review 引用（origin_review_id / followup_review_id）与回执行状随任务 JSON 一并
携带。本模块只读消费，不修改任务、不回写 tasks.py、不产生任何成功结论。

两类输出：

- **关联图**：任务、轮次、验收项、目标行（file:line）、测试、实验（复审）、
  源码版本（snapshot_sha256）、要求版本（req-xxxx）之间的有向边。
- **失效列表**：给定当前源码快照 / 当前要求版本 / 变更文件集合，输出哪些
  结论过期。降级语义沿用 tasks.py：源码变 → ``historical_outcome``（历史证据
  保留可读，当前版本需重验）；要求变 → 旧轮次只读、旧依据不适用。

首版采用**保守影响范围**：绑定缺失、或无法证明不受影响的结论同样进入失效
列表（宁可过期，不可沿用）。``confirmed_by_diff`` 只是对"确实命中变更文件"
的标注，不改变失效判定本身。
"""
from __future__ import annotations

import json
import re

__all__ = [
    "EvidenceGraphError", "EvidenceGraph", "build_graph",
    "invalidations", "basis_expiry", "changed_files_from_diff",
    "GRAPH_SCHEMA_VERSION",
]

GRAPH_SCHEMA_VERSION = "master-2026-09-evidence-graph-v1"

NODE_KINDS = frozenset({"task", "round", "criterion", "target", "test",
                        "experiment", "source", "requirement"})
EDGE_KINDS = frozenset({"HAS_ROUND", "HAS_REQUIREMENT", "HAS_SNAPSHOT",
                        "CONTAINS", "TARGETS", "USES_TEST", "VERIFIED_BY",
                        "CONCLUDED_ON", "CONTINUES"})

# 可过期的"结论"：已形成判定（含历史降级前的原判定）。pending 族不算结论。
CONCLUSIVE_STATUSES = frozenset({"supported", "gap_remains", "accepted_risk",
                                 "inconclusive"})
PENDING_STATUSES = frozenset({"pending", "pending_recheck"})

_REQUIREMENT_RE = re.compile(r"req-[0-9a-zA-Z._-]{1,32}")


class EvidenceGraphError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _text(value, name, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID", name)
    return value


def _snapshot_sha(value) -> str:
    """接受快照 dict（repo snapshot 形状）、裸 sha 字符串或 None。"""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("snapshot_sha256", "") or "")
    if isinstance(value, str):
        return value.strip()
    raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                             "snapshot must be a dict or sha string")


def _target_files(criterion) -> list:
    files = []
    for row in criterion.get("origin_rows") or []:
        if isinstance(row, dict) and isinstance(row.get("file"), str):
            files.append(row["file"])
    for ref in criterion.get("finding_ids") or []:
        if isinstance(ref, str) and ":" in ref:
            files.append(ref.rsplit(":", 1)[0])
    return sorted(set(files))


def _binding_snapshot_sha(criterion, round_snapshot, task_snapshot) -> tuple:
    """结论的源码绑定：criterion 自身快照 → 轮次快照 → 任务快照。"""
    for level, snapshot in (("adopted", criterion.get("adopted_snapshot")),
                            ("experiment", criterion.get("experiment_snapshot")),
                            ("round", round_snapshot),
                            ("task", task_snapshot)):
        sha = _snapshot_sha(snapshot)
        if sha:
            return sha, level
    return "", "missing"


class EvidenceGraph:
    """只读关联图：节点/边确定性排序，可整体序列化。"""

    def __init__(self):
        self._nodes: dict = {}
        self._edges: list = []
        self.conclusions: list = []  # 结论绑定记录（失效判定输入）

    def add_node(self, kind: str, node_id: str, **attrs):
        if kind not in NODE_KINDS:
            raise EvidenceGraphError("EVIDENCE_GRAPH_NODE_INVALID", kind)
        if not isinstance(node_id, str) or not node_id:
            raise EvidenceGraphError("EVIDENCE_GRAPH_NODE_INVALID", node_id)
        if node_id in self._nodes:
            # 幂等合并：同名节点只保留首个属性集（同一定位符只描述一个对象）。
            return
        self._nodes[node_id] = {"kind": kind, "node_id": node_id, **attrs}

    def add_edge(self, kind: str, src: str, dst: str, **attrs):
        if kind not in EDGE_KINDS:
            raise EvidenceGraphError("EVIDENCE_GRAPH_EDGE_INVALID", kind)
        edge = {"kind": kind, "src": src, "dst": dst, **attrs}
        if edge not in self._edges:
            self._edges.append(edge)

    def nodes(self, kind: str = "") -> list:
        nodes = [self._nodes[key] for key in sorted(self._nodes)]
        return [n for n in nodes if not kind or n["kind"] == kind]

    def edges(self, kind: str = "") -> list:
        return [e for e in self._edges if not kind or e["kind"] == kind]

    def as_dict(self) -> dict:
        return {"schema_version": GRAPH_SCHEMA_VERSION,
                "nodes": self.nodes(), "edges": self.edges(),
                "conclusions": [dict(c) for c in self.conclusions]}


def _iter_rounds(task):
    """产出 (round_meta, criteria, is_active)；无轮次旧任务合成 legacy 轮。"""
    active = task.get("active_round") or {}
    if active:
        yield active, task.get("criteria") or [], True
    for meta in task.get("rounds") or []:
        yield meta, meta.get("criteria") or [], False
    if not active and not task.get("rounds"):
        yield ({"round_id": "legacy", "round_no": 1, "status": "active",
                "supersede_reason": "legacy_unmigrated",
                "requirement_version": task.get("requirement_version", "req-0001"),
                "source_snapshot": task.get("source_snapshot") or {}},
               task.get("criteria") or [], True)


def build_graph(task) -> EvidenceGraph:
    """从任务 JSON 建立关联图并抽取结论绑定记录。"""
    if not isinstance(task, dict) or not task.get("task_id"):
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID", "task_id")
    task_id = _text(task["task_id"], "task_id", 100)
    graph = EvidenceGraph()
    task_node = f"task:{task_id}"
    graph.add_node("task", task_node, title=str(task.get("title", ""))[:200],
                   origin_review_id=str(task.get("origin_review_id", "")))
    if task.get("origin_review_id"):
        graph.add_node("experiment", f"review:{task['origin_review_id']}",
                       role="origin_review")
        graph.add_edge("VERIFIED_BY", task_node, f"review:{task['origin_review_id']}")

    task_sha = _snapshot_sha(task.get("source_snapshot"))
    round_metas = []
    for meta, criteria, is_active in _iter_rounds(task):
        round_id = str(meta.get("round_id", ""))
        round_node = f"round:{round_id}"
        req = str(meta.get("requirement_version", "") or "req-0001")
        if not _REQUIREMENT_RE.fullmatch(req):
            raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                     f"requirement_version {req!r}")
        round_sha = _snapshot_sha(meta.get("source_snapshot"))
        graph.add_node("round", round_node, round_no=meta.get("round_no", 1),
                       status=meta.get("status", "active"),
                       supersede_reason=meta.get("supersede_reason", ""))
        graph.add_edge("HAS_ROUND", task_node, round_node)
        req_node = f"requirement:{req}"
        graph.add_node("requirement", req_node, version=req)
        graph.add_edge("HAS_REQUIREMENT", round_node, req_node)
        if round_sha:
            src_node = f"source:{round_sha[:16]}"
            graph.add_node("source", src_node, snapshot_sha256=round_sha)
            graph.add_edge("HAS_SNAPSHOT", round_node, src_node)
        round_metas.append((round_id, meta))
        for criterion in criteria:
            cid = str(criterion.get("criterion_id", ""))
            if not cid:
                raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                         "criterion_id")
            crit_node = f"criterion:{cid}@{round_id}"
            # 活动轮在源码变化后 outcome 被实时降级为 inconclusive，真正被
            # 记录的结论在 historical_outcome 里；而携带态的 pending_recheck
            # 仍以 outcome 为准（其 historical_outcome 只是上一轮的只读副本，
            # 该结论已由闭合轮自己的 criteria 计入）。
            outcome = criterion.get("outcome") or {}
            if str(outcome.get("status", "pending")) not in PENDING_STATUSES:
                outcome = criterion.get("historical_outcome") or outcome
            graph.add_node("criterion", crit_node, criterion_id=cid,
                           round_id=round_id,
                           evidence_kind=str(criterion.get("evidence_kind", "")),
                           outcome_status=str(outcome.get("status", "")))
            graph.add_edge("CONTAINS", round_node, crit_node)
            for target_file in _target_files(criterion):
                target_node = f"target:{target_file}"
                graph.add_node("target", target_node, file=target_file)
                graph.add_edge("TARGETS", crit_node, target_node)
            for test in criterion.get("test_targets") or []:
                if not isinstance(test, str) or not test:
                    raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                             "test_targets")
                test_node = f"test:{test}"
                graph.add_node("test", test_node, test_id=test)
                graph.add_edge("USES_TEST", crit_node, test_node)
            followup = str(criterion.get("followup_review_id", "") or "")
            if followup:
                exp_node = f"review:{followup}"
                graph.add_node("experiment", exp_node, role="followup_review")
                graph.add_edge("VERIFIED_BY", crit_node, exp_node)
            binding_sha, binding_level = _binding_snapshot_sha(
                criterion, meta.get("source_snapshot"), task.get("source_snapshot"))
            if binding_sha:
                src_node = f"source:{binding_sha[:16]}"
                graph.add_node("source", src_node, snapshot_sha256=binding_sha)
                graph.add_edge("CONCLUDED_ON", crit_node, src_node)
            status = str(outcome.get("status", "pending"))
            graph.conclusions.append({
                "conclusion_id": f"{cid}@{round_id}",
                "criterion_id": cid, "round_id": round_id,
                "round_no": meta.get("round_no", 1),
                "round_status": meta.get("status", "active"),
                "outcome_status": status,
                "degraded_view": bool(criterion.get("historical_outcome")),
                "requirement_version": req,
                "source_snapshot_sha256": binding_sha,
                "binding_level": binding_level,
                "target_files": _target_files(criterion),
                "state": "pending" if status in PENDING_STATUSES else "conclusive",
            })
    # 轮次延续边：闭合轮 → 后继轮（要求/快照演进链）。
    for meta in task.get("rounds") or []:
        if meta.get("superseded_by"):
            graph.add_edge("CONTINUES", f"round:{meta.get('superseded_by')}",
                           f"round:{meta['round_id']}",
                           supersede_reason=meta.get("supersede_reason", ""))
    graph.conclusions.sort(key=lambda c: (c["round_no"], c["criterion_id"]))
    return graph


def invalidations(task, *, current_source_snapshot=None,
                  current_requirement_version: str = "",
                  changed_files=None) -> dict:
    """保守失效列表。

    - 源码快照变化 → 结论降级 ``historical_outcome``（含绑定缺失的保守失效）；
    - 要求版本变化 → 旧轮次只读（``read_only``），优先级高于源码降级；
    - ``changed_files`` 给定时仅用于标注 ``confirmed_by_diff``（确实命中
      变更文件），不放松失效判定——无法证明不受影响的结论同样失效。
    """
    graph = build_graph(task)
    current_sha = _snapshot_sha(current_source_snapshot)
    current_req = str(current_requirement_version or "")
    if current_req and not _REQUIREMENT_RE.fullmatch(current_req):
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                 f"requirement_version {current_req!r}")
    changed = sorted({str(f) for f in changed_files or []})
    invalidated, valid, pending = [], [], []
    for conclusion in graph.conclusions:
        if conclusion["state"] == "pending":
            pending.append(conclusion["conclusion_id"])
            continue
        reasons, confirmed = [], None
        if current_req and current_req != conclusion["requirement_version"]:
            reasons.append("requirement_changed")
        if current_sha:
            if not conclusion["source_snapshot_sha256"]:
                reasons.append("source_binding_missing")
            elif conclusion["source_snapshot_sha256"] != current_sha:
                reasons.append("source_changed")
                hit = bool(set(conclusion["target_files"]) & set(changed)) \
                    if changed else None
                confirmed = bool(hit) if changed else False
        if not reasons:
            valid.append(conclusion["conclusion_id"])
            continue
        invalidated.append({
            "conclusion_id": conclusion["conclusion_id"],
            "criterion_id": conclusion["criterion_id"],
            "round_id": conclusion["round_id"],
            "round_status": conclusion["round_status"],
            "outcome_status": conclusion["outcome_status"],
            "reasons": reasons,
            "confirmed_by_diff": confirmed,
            "downgrade": "read_only" if "requirement_changed" in reasons
                         else "historical_outcome",
            "binding": {
                "requirement_version": conclusion["requirement_version"],
                "source_snapshot_sha256": conclusion["source_snapshot_sha256"],
                "binding_level": conclusion["binding_level"],
            },
            "note": "保守失效：无法证明不受影响的结论同样进入失效列表"
                    if confirmed is False else "",
        })
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "task_id": task["task_id"],
        "evaluated_against": {
            "source_snapshot_sha256": current_sha,
            "requirement_version": current_req,
            "changed_files": changed,
        },
        "invalidated": invalidated,
        "valid_conclusion_ids": valid,
        "already_pending_ids": pending,
        "summary": {
            "conclusive": len(valid) + len(invalidated),
            "invalidated": len(invalidated),
            "valid": len(valid),
            "conservative": sum(1 for e in invalidated
                                if e["confirmed_by_diff"] is False),
            "pending": len(pending),
        },
    }


def basis_expiry(basis_version, *, current_source_snapshot=None,
                 current_requirement_version: str = "") -> dict:
    """记忆依据版本的保守过期判定（与结论失效同一套规则，供记忆模块调用）。

    声明了快照绑定且与当前快照不符 → ``source_changed``；调用方提供了当前
    快照而记忆未声明快照绑定 → ``source_binding_missing``（宁可过期）。
    要求版本维度对称处理。
    """
    if not isinstance(basis_version, dict):
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                 "basis_version must be a dict")
    basis_sha = str(basis_version.get("source_snapshot_sha256", "") or "")
    basis_req = str(basis_version.get("requirement_version", "") or "")
    current_sha = _snapshot_sha(current_source_snapshot)
    current_req = str(current_requirement_version or "")
    if current_req and not _REQUIREMENT_RE.fullmatch(current_req):
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID",
                                 f"requirement_version {current_req!r}")
    reasons = []
    if current_sha:
        if not basis_sha:
            reasons.append("source_binding_missing")
        elif basis_sha != current_sha:
            reasons.append("source_changed")
    if current_req:
        if not basis_req:
            reasons.append("requirement_binding_missing")
        elif basis_req != current_req:
            reasons.append("requirement_changed")
    return {"expired": bool(reasons), "reasons": reasons}


def changed_files_from_diff(diff_text: str) -> list:
    """从 unified diff 提取变更文件（b 侧路径），仅作影响标注输入。"""
    if not isinstance(diff_text, str):
        raise EvidenceGraphError("EVIDENCE_GRAPH_INPUT_INVALID", "diff text")
    files = []
    for line in diff_text.splitlines():
        match = re.match(r'^diff --git a/(.+?) b/(.+)$', line)
        if match:
            files.append(match.group(2))
            continue
        match = re.match(r'^\+\+\+ b/(.+)$', line)
        if match:
            files.append(match.group(1))
    return sorted(set(files))
