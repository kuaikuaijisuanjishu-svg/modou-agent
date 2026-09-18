"""Version-bound evidence tasks. Human adoption is a new, explicit authority.

A task organizes evidence; it does not certify that a development task is done.
Only the original review engine can produce experiment results. Candidate visas
never substitute for a new review of the adopted source and declared tests.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

from modou.agent.repair import (RepairError, isolated_worktree,
                                validate_candidate_patch, validate_test_only_patch)
from modou.server.auto_adoption import AutoAdoptionPolicy
from modou.candidate_intake import (CandidateIntakeError, ManualCandidateStore,
                                    content_sha256, parse_test_list,
                                    rule_protection)
from modou.language_adapters import discover_adapter
from modou.models import TestStatus
from modou.agent.spec import ReviewSpec, ReviewSpecError
from modou.agent.lease import LeaseError
from modou.application import ExecutionMode
from modou.acceptance_packs import (AcceptancePackError, assess_criterion,
                                     load as load_acceptance_packs)
from modou.executor import SandboxedExecutor, TrustedLocalExecutor, sanitized_environment, recover_process_record
from modou.safe_git import run_git
from modou.source_identity import source_content_sha256
from modou.target_mapping import (TargetMappingError, apply_confirmation,
                                  map_targets)
from modou.task_schema import TASK_RECEIPT_SCHEMA_VERSION
from modou.task_receipt import receipt_sha256
from modou.server.control import (IntakeError, _atomic_json, _read_json,
                                  _repo_snapshot, _find_evidence_bundle)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fail(code, detail):
    raise IntakeError(code, detail)


def closed(raw, required=(), optional=()):
    if not isinstance(raw, dict) or not set(required) <= set(raw) or set(raw) - set(required) - set(optional):
        fail("TASK_REQUEST_INVALID", "unsupported or missing fields")


def short_text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail("TASK_REQUEST_INVALID", name + " must be a nonempty bounded string")
    return value.strip()


def rows_for(rt):
    try:
        return _read_json(_find_evidence_bundle(rt.review_dir) / "report.json").get("render_model", {}).get("lines", [])
    except (IntakeError, OSError):
        return []


class ReverifyInterrupted(Exception):
    """复验被取消/预算终止打断。

    部分状态（新轮次、已复验项）已在任务账本落盘后才抛出；作业体据此把
    作业落成取消/预算终止，且不得把本次结果当作成功回执呈现。
    """

    def __init__(self, reason: str, round_id: str, rechecked: list):
        super().__init__(reason)
        self.reason = reason
        self.round_id = round_id
        self.rechecked = list(rechecked)


def ref(row):
    return f"{row.get('file')}:{row.get('line')}"


def new_test_patch(path, code):
    # The proposal parser has already bounded path/code; still validate the
    # generated diff with the same test-only boundary before exposing it.
    lines = code.splitlines(keepends=True)
    patch = (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
             f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
             + "".join("+" + line for line in lines))
    validate_test_only_patch(patch)
    return patch


def safe_file(repo, relative):
    path = repo / relative
    ancestors = []
    parent = path.parent
    while parent != repo and parent != parent.parent:
        ancestors.append(parent)
        parent = parent.parent
    if path.is_symlink() or any(parent.is_symlink() for parent in ancestors):
        fail("TASK_PATH_UNSAFE", relative)
    if not path.resolve().is_relative_to(repo.resolve()):
        fail("TASK_PATH_UNSAFE", relative)
    if path.exists() and (not path.is_file() or path.stat().st_size > 512 * 1024):
        fail("TASK_PATH_UNSAFE", relative)
    return path


def blob(path):
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def _blob_text(value):
    """blob() 的逆操作：base64 → 文本；缺失文件（新增/删除）为 None。"""
    if value is None:
        return None
    return base64.b64decode(value).decode("utf-8", "replace")


def index_digest(repo):
    # Compare logical index content, not the stat cache rewritten by git status.
    return text_hash(run_git(["ls-files", "--stage", "-z"], cwd=repo, check=True).stdout)


class TaskService:
    def __init__(self, manager, experiment_runner=None):
        self.manager = manager
        self.experiment_runner = experiment_runner
        # Load the catalog once per control plane.  A malformed catalog must
        # stop task intake instead of silently falling back to an older shape.
        self.acceptance_packs = load_acceptance_packs()
        self.root = manager.root / "_tasks"
        self.root.mkdir(exist_ok=True)
        # Manual candidates share the exact isolation/adoption pipeline; only
        # their origin differs (candidates contract, rule 1).
        self.candidate_store = ManualCandidateStore(self.root / "_candidates")
        # Preauthorized auto adoption is off unless a human writes the config
        # flag; the ledger itself only ever holds human-registered grants.
        self.auto_adoption_policy = AutoAdoptionPolicy(
            self.root / "auto-adoption-ledger.json",
            enabled=os.environ.get("SHUIMU_AUTO_ADOPTION_ENABLED", "") == "1")
        self._lock = threading.RLock()
        for record in self.root.glob("task-*/*/verification.runtime.json"):
            recover_process_record(record)

    def _path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"task-[a-f0-9]{24}", task_id):
            fail("TASK_NOT_FOUND", "unknown task")
        return self.root / (task_id + ".json")

    def _load(self, task_id):
        task = _read_json(self._path(task_id))
        if not task:
            fail("TASK_NOT_FOUND", task_id)
        return task

    def _save(self, task, event, details=None):
        events = task.setdefault("events", [])
        entry = {"seq": len(events) + 1, "kind": event, "at": time.time(),
                 "details": details or {}, "previous_sha256": events[-1]["sha256"] if events else ""}
        entry["sha256"] = digest(entry)
        events.append(entry)
        task["updated_at"] = entry["at"]
        _atomic_json(self._path(task["task_id"]), task)

    def _repo(self, task):
        for repo in self.manager.registry.registered():
            if self.manager._repo_fingerprint(repo) == task["repo_fingerprint"]:
                return repo
        fail("UNKNOWN_REPO_ID", "task repository is not registered")

    def _ready(self, task):
        rt = self.manager._runtime(task["origin_review_id"])
        if rt.state.snapshot.status.value not in {"COMPLETE", "PARTIAL"}:
            fail("TASK_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            fail("TASK_EVIDENCE_INVALID", "original evidence bundle is not verified")
        return rt

    def _targets(self, rt, finding_ids):
        if not isinstance(finding_ids, list) or len(finding_ids) > 20 or any(not isinstance(v, str) for v in finding_ids):
            fail("TASK_TARGET_INVALID", "finding_ids must be at most 20 actual review references")
        known = {ref(row): row for row in rows_for(rt)}
        if any(v not in known for v in finding_ids):
            fail("TASK_TARGET_INVALID", "a target is not in this review")
        return [copy.deepcopy(known[v]) for v in dict.fromkeys(finding_ids)]

    def create(self, raw, idempotency_key=""):
        closed(raw, ("review_id",), ("title", "criteria"))
        with self._lock:
            for path in self.root.glob("task-*.json"):
                prior = _read_json(path)
                if idempotency_key and prior.get("create_key") == idempotency_key:
                    if prior.get("create_sha256") != digest(raw):
                        fail("IDEMPOTENCY_CONFLICT", "task create key reused with different content")
                    return self.get(prior["task_id"])
            rt = self.manager._runtime(str(raw["review_id"]))
            request = rt.request or _read_json(rt.review_dir / "request.json")
            repo = self.manager._resolve_review_repo(request)
            title = short_text(raw.get("title") or request.get("goal") or "测试证据任务", "title")
            criteria = raw.get("criteria", [{"text": title, "finding_ids": []}])
            if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
                fail("TASK_REQUEST_INVALID", "criteria must contain 1-20 entries")
            records = [self._criterion_record(rt, i, item)
                       for i, item in enumerate(criteria)]
            task_id = "task-" + secrets.token_hex(12)
            task = {"schema_version": "evidence-task-v1", "task_id": task_id,
                    "origin_review_id": rt.review_id, "title": title, "criteria": records,
                    "repo_fingerprint": self.manager._repo_fingerprint(repo),
                    "source_snapshot": request.get("source_snapshot", {}),
                    "base_commit": (request.get("review_spec", {}).get("source", {}).get("base_ref")
                                    or request.get("source_snapshot", {}).get("head")),
                    "created_at": time.time(), "create_key": idempotency_key, "create_sha256": digest(raw),
                    "actions": {}, "receipt_url": f"/api/v2/tasks/{task_id}/receipt",
                    "rounds": [], "active_round": self._new_round_meta("created",
                               source_snapshot=copy.deepcopy(request.get("source_snapshot", {})))}
            self._save(task, "task.created")
            return self.get(task_id)

    def _criterion_record(self, rt, i, item, *, requirement_fields=False):
        optional = ("finding_ids", "evidence_kind", "profile_id", "language",
                    "test_targets", "adapter_id", "adoption_mode", "acceptance_pack_id")
        if requirement_fields:
            optional += ("origin", "expected_behavior", "mandatory")
        closed(item, ("text",), optional)
        kind = item.get("evidence_kind", "python_experiment")
        if kind not in {"python_experiment", "test_execution", "browser_behavior",
                        "language_test_execution"}:
            fail("TASK_REQUEST_INVALID", "unknown evidence_kind")
        profile = item.get("profile_id", "")
        if not isinstance(profile, str) or len(profile) > 100:
            fail("TASK_REQUEST_INVALID", "invalid profile_id")
        language = item.get("language", "")
        if language and (not isinstance(language, str) or len(language) > 30):
            fail("TASK_REQUEST_INVALID", "invalid language")
        targets = item.get("test_targets", [])
        if not isinstance(targets, list) or len(targets) > 50 or any(
                not isinstance(value, str) or not value or len(value) > 300
                for value in targets):
            fail("TASK_REQUEST_INVALID", "invalid test_targets")
        adapter_id = item.get("adapter_id", "")
        if adapter_id and (not isinstance(adapter_id, str) or len(adapter_id) > 100):
            fail("TASK_REQUEST_INVALID", "invalid adapter_id")
        adoption_mode = item.get("adoption_mode", "manual")
        if adoption_mode not in {"manual", "preauthorized"}:
            fail("TASK_REQUEST_INVALID", "invalid adoption_mode")
        if kind == "language_test_execution" and not language:
            fail("TASK_REQUEST_INVALID", "language is required for language evidence")
        acceptance_pack_id = item.get("acceptance_pack_id", "")
        acceptance_contract = None
        if acceptance_pack_id:
            if (not isinstance(acceptance_pack_id, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]*", acceptance_pack_id)):
                fail("TASK_REQUEST_INVALID", "invalid acceptance_pack_id")
            try:
                # The pack selector chooses the contract; it is not a
                # criterion input that the pack itself needs to list.
                contract_input = {key: value for key, value in item.items()
                                  if key != "acceptance_pack_id"}
                acceptance_contract = assess_criterion(
                    acceptance_pack_id, contract_input,
                    packs=self.acceptance_packs)
            except AcceptancePackError as exc:
                fail(exc.code, exc.detail)
            if acceptance_contract.intake_status != "ready":
                code = ("ACCEPTANCE_PACK_UNAVAILABLE"
                        if acceptance_contract.intake_status == "unsupported"
                        else "ACCEPTANCE_CRITERION_INVALID")
                fail(code, "; ".join(acceptance_contract.reason_codes)
                     or acceptance_contract.intake_status)
        record = {"criterion_id": f"criterion-{i+1}", "text": short_text(item["text"], "text"),
            "finding_ids": [], "origin_rows": [],
            "evidence_kind": kind, "profile_id": profile, "language": language,
            "test_targets": list(item.get("test_targets", [])), "adapter_id": adapter_id,
            "adoption_mode": adoption_mode,
            "acceptance_pack_id": acceptance_pack_id,
            "acceptance_contract": (acceptance_contract.as_dict()
                                    if acceptance_contract is not None else None),
            "status": "pending", "disposition": "",
            "candidate": None, "adoption_plan": None, "followup_review_id": "",
            "outcome": {"status": "pending", "reason": "尚未形成采用后的目标证据"}}
        if requirement_fields:
            record["origin"] = short_text(item.get("origin"), "origin")
            record["expected_behavior"] = short_text(item.get("expected_behavior"), "expected_behavior")
            record["mandatory"] = item.get("mandatory", True)
            if not isinstance(record["mandatory"], bool):
                fail("TASK_REQUEST_INVALID", "mandatory must be boolean")
        targets = self._targets(rt, item.get("finding_ids", []))
        record["finding_ids"] = [ref(row) for row in targets]
        record["origin_rows"] = targets
        return record

    def list(self, review_id=""):
        return {"tasks": [self.get(path.stem) for path in sorted(self.root.glob("task-*.json"))
                          if not review_id or _read_json(path).get("origin_review_id") == review_id]}

    def get(self, task_id):
        with self._lock:
            task = self._load(task_id)
            try:
                observed_snapshot = _repo_snapshot(self._repo(task).path)
            except IntakeError:
                observed_snapshot = {}
            for criterion in task["criteria"]:
                expected = criterion.get("adopted_snapshot") or criterion.get("experiment_snapshot") or task["source_snapshot"]
                criterion["source_snapshot_matches"] = bool(observed_snapshot) and observed_snapshot == expected
                child_id = criterion.get("followup_review_id")
                if child_id and criterion["status"] in {"reviewing", "adopted"}:
                    child = self.manager._runtime(child_id)
                    state = child.state.snapshot.status.value
                    criterion["followup_status"] = state
                    if state in {"COMPLETE", "PARTIAL"}:
                        criterion["outcome"] = self._compare(criterion, child)
                        criterion["status"] = "reviewed"
                    elif state in {"FAILED", "ABORTED"}:
                        criterion["status"] = "adopted"
                        criterion["outcome"] = {"status": "inconclusive", "reason": "已采用；新审查没有完成"}
                elif child_id:
                    # Historical followup of a carried/pending_recheck item:
                    # readable as context, never a live status override.
                    criterion["followup_status"] = self.manager._runtime(child_id).state.snapshot.status.value
                if (criterion["outcome"].get("status") == "supported"
                        and not criterion["source_snapshot_matches"]):
                    criterion["historical_outcome"] = criterion["outcome"]
                    criterion["outcome"] = {"status": "inconclusive",
                        "reason": "代码版本已变化；历史证据保留，当前版本需要重新审查"}
                criterion["allowed_actions"] = self._allowed(criterion)
                if criterion.get("adoption_mode") != "preauthorized":
                    criterion["allowed_actions"] = [a for a in criterion["allowed_actions"]
                                                     if a != "auto_confirm_adoption"]
                # Never return rollback bytes, internal request dedupe, or repository paths.
                if criterion.get("adoption_plan"):
                    criterion["adoption_plan"] = {k: v for k, v in criterion["adoption_plan"].items()
                                                   if k not in {"before", "after", "child_spec", "index_sha256"}}
            task.pop("actions", None)
            task.pop("create_key", None)
            task.pop("create_sha256", None)
            task["rounds_summary"] = self._rounds_view(task)
            active = task.get("active_round") or {}
            task["active_round_id"] = active.get("round_id", "")
            task["round_no"] = active.get("round_no", 1)
            task["requirement_version"] = active.get("requirement_version", "req-0001")
            return task

    def receipt(self, task_id):
        task = self.get(task_id)
        statuses = {c["outcome"]["status"] for c in task["criteria"]}
        status = ("supported" if statuses == {"supported"} else
                  "gap_remains" if "gap_remains" in statuses else
                  "accepted_risk" if statuses == {"accepted_risk"} else
                  "pending" if statuses and statuses <= {"pending", "pending_recheck"} else "inconclusive")
        if status == "supported" and not all(c.get("source_snapshot_matches") for c in task["criteria"]):
            status = "inconclusive"
        snapshots = [c.get("adopted_snapshot") or c.get("experiment_snapshot", {}) for c in task["criteria"]]
        coherent = snapshots and all(s == snapshots[0] and s for s in snapshots)
        result = {**task, "receipt_schema_version": TASK_RECEIPT_SCHEMA_VERSION, "status": status,
                "round_id": task.get("active_round_id", ""),
                "round_no": task.get("round_no", 1),
                "requirement_version": task.get("requirement_version", "req-0001"),
                "source_head": snapshots[0].get("head", "") if coherent else "",
                "source_snapshot_sha256": snapshots[0].get("snapshot_sha256", "") if coherent else "",
                "source_is_clean": not snapshots[0].get("dirty", True) if coherent else None,
                "evidence_kinds": sorted({str(c.get("evidence_kind") or "")
                                           for c in task["criteria"] if c.get("evidence_kind")}),
                "languages": sorted({str(c.get("language") or "")
                                      for c in task["criteria"] if c.get("language")}),
                "boundary": "测试证据记录；不等于通用任务完成认证。签证不等于目标缺口已补齐。",
                }
        # Keep the server seal byte-for-byte identical to the portable verifier
        # used by local clients and CI tooling.
        result["receipt_sha256"] = receipt_sha256(result)
        return result

    # ---- version rounds (T01) -------------------------------------------

    @staticmethod
    def _new_round_meta(reason, source_snapshot=None, round_no=1, requirement_seq=1):
        return {"round_id": "round-" + secrets.token_hex(6), "round_no": round_no,
                "status": "active", "supersede_reason": reason,
                "requirement_version": f"req-{requirement_seq:04d}",
                "requirement_seq": requirement_seq,
                "source_snapshot": copy.deepcopy(source_snapshot or {}),
                "created_at": time.time(), "closed_at": None, "superseded_by": ""}

    def _ensure_rounds(self, task):
        """Lazily upgrade a legacy task on its first mutating operation.

        Round 1 describes the state the legacy task already had; no outcome is
        rewritten and old receipts stay readable untouched.
        """
        if task.get("active_round"):
            return
        task["rounds"] = []
        task["active_round"] = self._new_round_meta("resumed_legacy",
                                                    source_snapshot=task.get("source_snapshot", {}))

    def _carry_criteria(self, criteria, reason, keep_status_for=()):
        """Conservative carry-over: checked items become pending_recheck.

        Bindings (candidate, adoption plan, followup review) are preserved for
        context and re-verification; staleness is still enforced by the
        per-action source guards. Nothing may inherit a success conclusion.
        keep_status_for carries criteria unchanged (the adoption trigger that
        is itself transitioning into its fresh review in the new round).
        """
        carried = []
        reasons = {"adoption_applied": "另一验收项的采用改变了源码版本；本项需在当前版本重新核验",
                   "reverify_new_snapshot": "源码快照已变化；本项需在当前版本重新核验",
                   "requirement_changed": "要求已修改；旧依据不适用于新的要求版本"}
        for c in criteria:
            fresh = copy.deepcopy(c)
            if c["criterion_id"] in keep_status_for:
                carried.append(fresh)
                continue
            fresh["prior_state"] = {"status": c["status"],
                                    "disposition": c.get("disposition", ""),
                                    "outcome": copy.deepcopy(c.get("outcome")),
                                    "followup_review_id": c.get("followup_review_id", "")}
            fresh["historical_outcome"] = copy.deepcopy(c.get("outcome"))
            fresh["status"] = "pending_recheck"
            fresh["outcome"] = {"status": "pending_recheck", "reason": reasons.get(reason, reason)}
            carried.append(fresh)
        return carried

    def _open_round(self, task, reason, *, criteria=None, source_snapshot=None,
                    requirement_seq=None, keep_status_for=()):
        """Close the active round read-only and install a fresh active round.

        The caller re-fetches criterion references afterwards because
        task["criteria"] is replaced by carried copies.
        """
        # The requirement version bumps only on requirement_changed; adoption
        # and reverify rounds advance round_no under the same requirements.
        new_meta = self._new_round_meta(
            reason, source_snapshot=source_snapshot or task.get("source_snapshot", {}),
            round_no=(task["active_round"]["round_no"] + 1) if task.get("active_round") else 1,
            requirement_seq=(requirement_seq if requirement_seq is not None
                             else (task["active_round"].get("requirement_seq", 1))
                             if task.get("active_round") else 1))
        active = task.get("active_round")
        if active:
            frozen_criteria = copy.deepcopy(task["criteria"])
            # Seal in-flight followup conclusions at freeze time so the
            # read-only round shows the outcome that was actually reached.
            for fc in frozen_criteria:
                if fc["status"] == "reviewing" and fc.get("followup_review_id"):
                    child = self.manager._runtime(fc["followup_review_id"])
                    state = child.state.snapshot.status.value
                    if state in {"COMPLETE", "PARTIAL"}:
                        fc["outcome"] = self._compare(fc, child)
                        fc["status"] = "reviewed"
                    elif state in {"FAILED", "ABORTED"}:
                        fc["status"] = "adopted"
                        fc["outcome"] = {"status": "inconclusive", "reason": "已采用；新审查没有完成"}
            frozen = {**active, "status": "superseded", "supersede_reason": reason,
                      "closed_at": time.time(), "superseded_by": new_meta["round_id"],
                      "criteria": frozen_criteria}
            task.setdefault("rounds", []).append(frozen)
        task["active_round"] = new_meta
        task["criteria"] = (criteria if criteria is not None
                            else self._carry_criteria(task["criteria"], reason,
                                                      keep_status_for=keep_status_for))
        if source_snapshot is not None:
            task["source_snapshot"] = copy.deepcopy(source_snapshot)

    def _rounds_view(self, task):
        rows = []
        for r in task.get("rounds", []):
            rows.append({"round_id": r["round_id"], "round_no": r["round_no"],
                         "status": r["status"], "supersede_reason": r["supersede_reason"],
                         "superseded_by": r.get("superseded_by", ""),
                         "requirement_version": r["requirement_version"],
                         "source_snapshot_sha256": r.get("source_snapshot", {}).get("snapshot_sha256", ""),
                         "created_at": r["created_at"], "closed_at": r.get("closed_at"),
                         "criteria_outcomes": [{"criterion_id": c["criterion_id"],
                                                "status": c["status"],
                                                "outcome_status": c.get("outcome", {}).get("status", "")}
                                               for c in r.get("criteria", [])]})
        active = task.get("active_round")
        if active:
            rows.append({"round_id": active["round_id"], "round_no": active["round_no"],
                         "status": "active", "supersede_reason": active.get("supersede_reason", ""),
                         "superseded_by": "", "requirement_version": active["requirement_version"],
                         "source_snapshot_sha256": active.get("source_snapshot", {}).get("snapshot_sha256", ""),
                         "created_at": active["created_at"], "closed_at": None,
                         "criteria_outcomes": [{"criterion_id": c["criterion_id"],
                                                "status": c["status"],
                                                "outcome_status": c.get("outcome", {}).get("status", "")}
                                               for c in task["criteria"]]})
        else:
            rows.append({"round_id": "", "round_no": 1, "status": "active",
                         "supersede_reason": "legacy_unmigrated", "superseded_by": "",
                         "requirement_version": "req-0001",
                         "source_snapshot_sha256": task.get("source_snapshot", {}).get("snapshot_sha256", ""),
                         "created_at": task.get("created_at", 0), "closed_at": None,
                         "criteria_outcomes": [{"criterion_id": c["criterion_id"],
                                                "status": c["status"],
                                                "outcome_status": c.get("outcome", {}).get("status", "")}
                                               for c in task.get("criteria", [])],
                         "legacy": True})
        return rows

    def rounds(self, task_id):
        with self._lock:
            task = self._load(task_id)
            return {"task_id": task["task_id"], "active_round_id": task.get("active_round", {}).get("round_id", ""),
                    "rounds": self._rounds_view(task)}

    def round_detail(self, task_id, round_id):
        with self._lock:
            task = self._load(task_id)
            active = task.get("active_round") or {}
            if active and round_id == active.get("round_id"):
                return {"task_id": task["task_id"], **copy.deepcopy(active),
                        "criteria": copy.deepcopy(task["criteria"])}
            for r in task.get("rounds", []):
                if r["round_id"] == round_id:
                    return {"task_id": task["task_id"], **copy.deepcopy(r)}
            fail("TASK_ROUND_NOT_FOUND", "unknown round for this task")

    def create_requirement_version(self, task_id, raw, idempotency_key=""):
        closed(raw, ("criteria",), ("reason", "source", "title"))
        if raw.get("reason", "requirement_changed") != "requirement_changed":
            fail("TASK_ROUND_REASON_INVALID", "external round creation supports requirement_changed only")
        criteria = raw["criteria"]
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20:
            fail("TASK_REQUEST_INVALID", "criteria must contain 1-20 entries")
        with self._lock:
            task = self._load(task_id)
            key = "rounds:" + (idempotency_key or "")
            if idempotency_key and key in task["actions"]:
                if task["actions"][key] != digest(raw):
                    fail("IDEMPOTENCY_CONFLICT", "round create key reused with different content")
                return self.rounds(task_id)
            self._ensure_rounds(task)
            rt = self._ready(task)
            repo = self._repo(task)
            records = [self._criterion_record(rt, i, item, requirement_fields=True)
                       for i, item in enumerate(criteria)]
            observed = _repo_snapshot(repo.path)
            self._open_round(task, "requirement_changed", criteria=records,
                             source_snapshot=observed,
                             requirement_seq=task["active_round"]["requirement_seq"] + 1)
            if raw.get("title"):
                task["title"] = short_text(raw["title"], "title")
            if idempotency_key:
                task["actions"][key] = digest(raw)
            self._save(task, "round.requirement_changed",
                       {"round_id": task["active_round"]["round_id"],
                        "requirement_version": task["active_round"]["requirement_version"],
                        "source": raw.get("source", "")})
            return self.rounds(task_id)

    def reverify_needed(self, task_id) -> dict:
        """触发作业的入口判定（N02）：什么时候确实有复验工作可做。

        两类情况需要复验：存在 pending_recheck 的验收项；或源快照相对
        当前轮次已经移动（证据可能失效——跨版本场景，正是复验存在的原因）。
        两者都不满足才是真正的无可执行项。
        """
        with self._lock:
            task = self._load(task_id)
            self._ensure_rounds(task)
            repo = self._repo(task)
            observed = _repo_snapshot(repo.path)
            active = task.get("active_round") or {}
            moved = observed.get("snapshot_sha256") != \
                active.get("source_snapshot", {}).get("snapshot_sha256")
            pending = [c["criterion_id"] for c in task["criteria"]
                       if c.get("status") == "pending_recheck"]
            return {"needed": bool(pending) or moved,
                    "pending_recheck": pending, "snapshot_moved": moved}

    def reverify_current(self, task_id, raw=None, idempotency_key="",
                         interrupt_check=None):
        """interrupt_check：作业体注入的协作中断探针（R2）。在每项复验工作
        开始前调用；返回非空原因字符串即保存部分状态并抛 ReverifyInterrupted，
        让取消/预算真正终止后续实验，而不是只记录一个事后取消标记。"""
        if raw is None:
            raw = {}
        with self._lock:
            task = self._load(task_id)
            key = "reverify:" + (idempotency_key or "")
            if idempotency_key and key in task["actions"]:
                if task["actions"][key] != digest(raw):
                    fail("IDEMPOTENCY_CONFLICT", "reverify key reused with different content")
                return self.get(task_id)
            closed(raw, (), ())
            self._ensure_rounds(task)
            rt = self._ready(task)
            repo = self._repo(task)
            observed = _repo_snapshot(repo.path)
            active = task["active_round"]
            round_opened = ""
            if observed.get("snapshot_sha256") != active["source_snapshot"].get("snapshot_sha256"):
                self._open_round(task, "reverify_new_snapshot", source_snapshot=observed)
                round_opened = task["active_round"]["round_id"]
                for c in task["criteria"]:
                    # Re-verification reviews the current source; the adoption
                    # transaction itself is long finished.
                    if c.get("adopted_snapshot"):
                        c["adopted_snapshot"] = copy.deepcopy(observed)
                    self._refresh_target_mapping(task, c, repo)
            rechecked, blocked = [], []

            def _interrupt_point(next_criterion):
                if interrupt_check is None:
                    return
                reason = interrupt_check()
                if not reason:
                    return
                task["last_reverify"] = {
                    "status": "aborted",
                    "reason": reason,
                    "round_id": round_opened or active["round_id"],
                    "rechecked": list(rechecked),
                    "next_criterion": next_criterion,
                    "receipt_suppressed": True,
                    "at": time.time(),
                }
                self._save(task, "task.reverify_interrupted",
                           {"round_id": round_opened or active["round_id"],
                            "rechecked": rechecked, "reason": reason,
                            "next_criterion": next_criterion})
                raise ReverifyInterrupted(reason, round_opened or active["round_id"],
                                          rechecked)

            for c in task["criteria"]:
                if c["status"] != "pending_recheck":
                    continue
                _interrupt_point(c["criterion_id"])
                if c["evidence_kind"] in {"test_execution", "browser_behavior",
                                          "language_test_execution"}:
                    self._run_experiment(task, c, repo)
                    rechecked.append(c["criterion_id"])
                elif c.get("adoption_plan") and c.get("adopted_snapshot"):
                    # The adoption transaction itself is long finished; the
                    # followup review must run against the current source.
                    c["adopted_snapshot"] = copy.deepcopy(observed)
                    self._start_review(task, c, repo, force_new_generation=True)
                    rechecked.append(c["criterion_id"])
                else:
                    blocked.append(c["criterion_id"])
            if idempotency_key:
                task["actions"][key] = digest(raw)
            self._save(task, "task.reverify_current",
                       {"round_id": round_opened or active["round_id"],
                        "round_opened": round_opened, "rechecked": rechecked,
                        "still_pending": blocked})
            return self.get(task_id)

    @staticmethod
    def _allowed(c):
        if c["status"] == "pending_recheck":
            actions = ["set_targets", "select_disposition", "run_experiment"]
            if c.get("adopted_snapshot"):
                actions.append("retry_review")
            if isinstance(c.get("target_mapping"), dict):
                actions.append("confirm_target_mapping")
            return actions
        if c["status"] == "pending":
            return ["set_targets", "select_disposition", "run_experiment"]
        if c["status"] == "selected":
            return ["set_targets", "select_disposition", "propose_test", "bind_candidate", "run_experiment"]
        if c["status"] == "candidate_ready":
            return ["prepare_adoption", "bind_candidate"]
        if c["status"] == "prepared":
            return ["prepare_adoption", "confirm_adoption", "auto_confirm_adoption"]
        if c["status"] in {"applying", "recovery_required"}:
            return ["recover_adoption"]
        if c["status"] == "adopted":
            return ["retry_review"]
        return []

    def action(self, task_id, raw, idempotency_key=""):
        try:
            return self._action(task_id, raw, idempotency_key)
        except (RepairError, ReviewSpecError, LeaseError) as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        except subprocess.TimeoutExpired as exc:
            raise IntakeError("TASK_VERIFICATION_TIMEOUT", "candidate verification exhausted its budget") from exc

    def _action(self, task_id, raw, idempotency_key=""):
        closed(raw, ("action", "criterion_id"), ("finding_ids", "disposition", "reason", "handled_by", "source",
                "expected_sha256", "budget_seconds", "plan_sha256", "mapping_id", "confirmed",
                "file", "line", "operator_note", "candidate_id"))
        name = raw["action"]
        fields = {"set_targets": {"finding_ids"}, "select_disposition": {"disposition", "reason", "handled_by"},
                  "propose_test": set(), "bind_candidate": {"source", "expected_sha256", "candidate_id"},
                  "prepare_adoption": {"budget_seconds"}, "confirm_adoption": {"plan_sha256"},
                  "auto_confirm_adoption": set(), "retry_review": set(),
                  "recover_adoption": set(), "run_experiment": set(),
                  "confirm_target_mapping": {"mapping_id", "confirmed", "file", "line", "operator_note"}}
        if name not in fields or set(raw) - {"action", "criterion_id"} - fields[name]:
            fail("TASK_ACTION_INVALID", "unsupported action fields")
        with self._lock:
            task = self._load(task_id)
            if idempotency_key in task["actions"]:
                if task["actions"][idempotency_key] != digest(raw):
                    fail("IDEMPOTENCY_CONFLICT", "action key reused with different content")
                return self.get(task_id)
            self._ensure_rounds(task)
            c = next((c for c in task["criteria"] if c["criterion_id"] == raw["criterion_id"]), None)
            if c is None:
                fail("TASK_CRITERION_NOT_FOUND", "unknown criterion")
            if (name == "confirm_adoption" and c.get("confirmed_at")
                    and c.get("adoption_plan", {}).get("plan_sha256") == raw.get("plan_sha256")
                    and c["status"] != "prepared"):
                return self.get(task_id)
            if c.get("followup_review_id") and c["status"] == "reviewing":
                child = self.manager._runtime(c["followup_review_id"])
                if child.state.snapshot.status.value in {"FAILED", "ABORTED"}:
                    c["status"] = "adopted"
            if name not in self._allowed(c):
                fail("TASK_STATE_INVALID", "action is unavailable in this state")
            rt = self._ready(task)
            repo = self._repo(task)
            if name == "set_targets":
                c["origin_rows"] = self._targets(rt, raw.get("finding_ids"))
                c["finding_ids"] = [ref(row) for row in c["origin_rows"]]
            elif name == "select_disposition":
                choice = raw.get("disposition")
                if choice not in {"add_tests", "controlled_repair", "accept_risk"}:
                    fail("TASK_DISPOSITION_INVALID", "unknown disposition")
                c["disposition"] = choice
                c["status"] = "selected"
                if choice == "accept_risk":
                    c["risk_acceptance"] = {"reason": short_text(raw.get("reason"), "reason"),
                        "handled_by": short_text(raw.get("handled_by"), "handled_by", 100),
                        "snapshot_sha256": task["source_snapshot"].get("snapshot_sha256"), "at": time.time()}
                    c["status"] = "accepted_risk"
                    c["outcome"] = {"status": "accepted_risk", "reason": "人工接受风险；原测试证据缺口未改变"}
            elif name == "propose_test":
                if c["disposition"] != "add_tests" or not c["finding_ids"]:
                    fail("TASK_TARGET_REQUIRED", "select concrete targets and add_tests first")
                proposal = self.manager.propose_test(rt.review_id, {"finding_ids": c["finding_ids"]},
                                                     idempotency_key=idempotency_key)
                c["proposal_attempt"] = {k: proposal.get(k) for k in ("status", "effective", "path", "code_sha256", "question")}
                c["proposal_attempt"]["visa_status"] = proposal.get("visa", {}).get("status", "")
                if proposal.get("visa", {}).get("status") == "VERIFIED_EFFECTIVE":
                    self._bind(task, c, rt, repo, {"source": "test_proposal", "expected_sha256": proposal["code_sha256"]})
            elif name == "bind_candidate":
                self._bind(task, c, rt, repo, raw)
            elif name == "prepare_adoption":
                self._prepare(task, c, rt, repo, raw)
            elif name == "confirm_adoption":
                self._confirm(task, c, rt, repo, raw)
            elif name == "auto_confirm_adoption":
                self._auto_confirm(task, c, rt, repo)
            elif name == "retry_review":
                if c["status"] == "pending_recheck" and _repo_snapshot(repo.path) != c.get("adopted_snapshot"):
                    fail("TASK_ROUND_RECHECK_REQUIRED", "source version moved; call reverify_current to rebind this item to the current version")
                self._start_review(task, c, repo)
            elif name == "confirm_target_mapping":
                self._confirm_mapping(task, c, raw)
            elif name == "recover_adoption":
                self._recover(task, c, repo)
            elif name == "run_experiment":
                self._run_experiment(task, c, repo)
            if idempotency_key:
                task["actions"][idempotency_key] = digest(raw)
            self._save(task, "task." + name, {"criterion_id": c["criterion_id"]})
            return self.get(task_id)

    def _source_guard(self, task, repo):
        if _repo_snapshot(repo.path).get("snapshot_sha256") != task["source_snapshot"].get("snapshot_sha256"):
            fail("SOURCE_SNAPSHOT_CHANGED", "reviewed source changed; start a new review")

    def _refresh_target_mapping(self, task, c, repo):
        """Map origin rows across a source rewrite (T02 wiring).

        The mapping is an assist for target identity, never a gate: when the
        computation itself fails the legacy exact-text comparison semantics
        remain in force.
        """
        rows = [r for r in c.get("origin_rows", [])
                if isinstance(r, dict) and isinstance(r.get("line"), int)
                and isinstance(r.get("file"), str) and r.get("file")]
        if not rows:
            return
        base = task.get("base_commit") or ""
        try:
            patch = ""
            if base:
                patch = run_git(["diff", "--find-renames=50%", base], cwd=repo.path,
                                timeout=60).stdout
            old_tree, new_tree = {}, {}
            for f in sorted({r["file"] for r in rows}):
                if base:
                    shown = run_git(["show", f"{base}:{f}"], cwd=repo.path, timeout=30)
                    if shown.returncode == 0 and shown.stdout:
                        old_tree[f] = shown.stdout
                path = repo.path / f
                if path.is_file() and path.stat().st_size < 2 * 1024 * 1024:
                    new_tree[f] = path.read_text(encoding="utf-8")
            report = map_targets(rows, old_tree=old_tree, new_tree=new_tree or None,
                                 patch=patch or None)
            c["target_mapping"] = report.as_dict()
        except (TargetMappingError, OSError, UnicodeDecodeError,
                subprocess.SubprocessError):
            c.pop("target_mapping", None)

    def _confirm_mapping(self, task, c, raw):
        record = c.get("target_mapping") if isinstance(c.get("target_mapping"), dict) else None
        if not record:
            fail("TASK_MAPPING_NOT_FOUND", "criterion has no recorded target mapping")
        target = next((m for m in record.get("mappings", [])
                       if m.get("mapping_id") == raw.get("mapping_id")), None)
        if target is None:
            fail("TASK_MAPPING_NOT_FOUND", "unknown mapping_id for this criterion")
        try:
            updated = apply_confirmation(target, confirmed=bool(raw.get("confirmed")),
                                         file=raw.get("file") if isinstance(raw.get("file"), str) else None,
                                         line=raw.get("line") if isinstance(raw.get("line"), int) else None,
                                         operator_note=str(raw.get("operator_note", "")))
        except TargetMappingError as exc:
            fail("TASK_MAPPING_CONFIRM_INVALID", str(exc))
        record["mappings"] = [updated if m.get("mapping_id") == raw["mapping_id"] else m
                              for m in record["mappings"]]
        c["target_mapping"] = record
        # Confirmation only fixes target identity: recompute the conclusion
        # from an already-completed followup when one exists. It never
        # fabricates a success without that review evidence.
        child_id = c.get("followup_review_id")
        if child_id:
            child = self.manager._runtime(child_id)
            if (child.state.snapshot.status.value in {"COMPLETE", "PARTIAL"}
                    and child.state.snapshot.valid_bundle):
                outcome = self._compare(c, child)
                if outcome["status"] in {"supported", "gap_remains"}:
                    c["outcome"] = outcome
                    c["status"] = "reviewed"
                else:
                    c["outcome"] = outcome

    def _run_experiment(self, task, c, repo):
        self._source_guard(task, repo)
        runner = self.experiment_runner or getattr(self.manager, "evidence_experiment_runner", None)
        if runner is None:
            fail("TASK_EXPERIMENT_UNAVAILABLE", "no server-controlled experiment runner configured")
        if c["evidence_kind"] not in {"test_execution", "browser_behavior",
                                       "language_test_execution"}:
            fail("TASK_EXPERIMENT_KIND_INVALID", "experimental runner cannot produce Python attribution")
        c["experiment"] = runner(copy.deepcopy(task), copy.deepcopy(c))
        observed = _repo_snapshot(repo.path)
        experiment = c["experiment"]
        bound = (observed == task["source_snapshot"]
                 and experiment.get("source_snapshot_sha256") == observed.get("snapshot_sha256")
                 and experiment.get("evidence_kind") == c["evidence_kind"])
        supported = bound and experiment.get("status") == "passed"
        c["experiment_snapshot"] = observed
        c["outcome"] = {"status": "supported" if supported else "inconclusive",
            "reason": "独立行为/执行证据，不代表Python逐行证据", "experiment": experiment}
        if supported:
            c["status"] = "reviewed"

    def _bind(self, task, c, rt, repo, raw):
        self._source_guard(task, repo)
        if not c["finding_ids"]:
            fail("TASK_TARGET_REQUIRED", "select concrete targets")
        spec = self.manager._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            fail("REPAIR_NOT_AUTHORIZED", "candidate work was not authorized")
        source = raw.get("source")
        if source == "test_proposal" and c["disposition"] == "add_tests":
            record = _read_json(rt.review_dir / "test_proposal" / "proposal.json")
            if record.get("visa", {}).get("status") != "VERIFIED_EFFECTIVE":
                fail("TASK_CANDIDATE_NOT_EFFECTIVE", "a passing test alone is not an effective-test visa")
            if not set(c["finding_ids"]) <= set(record.get("finding_ids", [])):
                fail("TASK_CANDIDATE_TARGET_MISMATCH", "candidate was generated for different targets")
            code = record.get("code", "")
            if text_hash(code) != record.get("code_sha256") or raw.get("expected_sha256") != record.get("code_sha256"):
                fail("TASK_CANDIDATE_CHANGED", "candidate code hash changed")
            patch = new_test_patch(record["path"], code)
            verification = {"kind": "effective_test", "status": "VERIFIED_EFFECTIVE", "visa": record["visa"],
                            "target_improvement_proven": False}
            extras = {"path": record["path"], "code_sha256": record["code_sha256"]}
        elif source == "repair_candidate" and c["disposition"] == "controlled_repair":
            path = rt.review_dir / "repair" / "candidate.patch"
            if not path.is_file():
                fail("REPAIR_CANDIDATE_MISSING", "generate a repair candidate first")
            patch = path.read_text()
            if text_hash(patch) != raw.get("expected_sha256"):
                fail("TASK_CANDIDATE_CHANGED", "candidate patch hash changed")
            verification = {"kind": "declared_tests", "status": "pending", "target_improvement_proven": False}
            extras = {}
        elif source == "manual_candidate" and c["disposition"] == "add_tests":
            try:
                record = self.candidate_store.load(str(raw.get("candidate_id", "")),
                                                   with_content=True)
            except CandidateIntakeError as exc:
                fail("TASK_CANDIDATE_INVALID", str(exc))
            target = record.get("target") or {}
            if target.get("criterion_id") and target["criterion_id"] != c["criterion_id"]:
                fail("TASK_CANDIDATE_TARGET_MISMATCH", "manual candidate was sealed for another criterion")
            content = record["sealed_content"]["content"]
            sealed_hash = record["sealed_content"]["content_sha256"]
            if (content_sha256(content) != sealed_hash
                    or raw.get("expected_sha256") != sealed_hash):
                fail("TASK_CANDIDATE_CHANGED", "manual candidate content hash changed")
            path = record["sealed_content"]["path"]
            patch = new_test_patch(path, content)
            verification = {"kind": "manual_test", "status": "pending", "origin": "manual",
                            "upload_source": (record.get("origin_detail") or {}).get("source", ""),
                            "target_improvement_proven": False}
            extras = {"path": path, "code_sha256": sealed_hash,
                      "candidate_id": record["candidate_id"]}
        else:
            fail("TASK_CANDIDATE_INVALID", "candidate source does not match disposition")
        paths = validate_candidate_patch(patch, allowed_paths=tuple(spec.scope.include),
                    max_files=spec.scope.max_modified_files, max_changed_lines=spec.scope.max_changed_lines)
        if any(line.startswith(("old mode ", "new mode ", "rename from ", "rename to ", "GIT binary patch"))
               or (line.startswith("new file mode ") and line != "new file mode 100644") for line in patch.splitlines()):
            fail("TASK_PATCH_MODE_UNSUPPORTED", "adoption accepts regular text edits only")
        if c["disposition"] == "controlled_repair" and any(
                p in (rt.request.get("test_files") or []) or Path(p).name.startswith("test_")
                or "tests" in Path(p).parts for p in paths):
            fail("TASK_TEST_WEAKENING_FORBIDDEN", "controlled repair cannot modify existing test expectations")
        for p in paths:
            safe_file(repo.path, p)
        candidate = {"source": source, "patch": patch, "patch_sha256": text_hash(patch),
                     "paths": sorted(paths), "verification": verification, **extras}
        c.setdefault("candidate_history", []).append(copy.deepcopy(candidate))
        c["candidate"] = candidate
        c["adoption_plan"] = None
        c["status"] = "candidate_ready"

    def _prepare(self, task, c, rt, repo, raw):
        self._source_guard(task, repo)
        candidate = c["candidate"]
        if text_hash(candidate["patch"]) != candidate["patch_sha256"]:
            fail("TASK_CANDIDATE_CHANGED", "sealed candidate changed")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        maximum = float(request.get("budget_seconds") or 300)
        budget = raw.get("budget_seconds", maximum)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1 <= budget <= maximum:
            fail("TASK_BUDGET_INVALID", "budget must be within the original approved maximum")
        spec = self.manager._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            fail("REPAIR_NOT_AUTHORIZED", "candidate preparation not authorized")
        paths = sorted(validate_candidate_patch(candidate["patch"], allowed_paths=tuple(spec.scope.include),
            max_files=spec.scope.max_modified_files, max_changed_lines=spec.scope.max_changed_lines))
        before = {p: blob(safe_file(repo.path, p)) for p in paths}
        tests = list(dict.fromkeys([*(request.get("test_files") or []), *([candidate["path"]] if candidate.get("path") else [])]))
        # The isolation helper applies tracked diffs only. Refuse to verify a
        # different source tree by silently dropping pre-existing new files.
        if run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo.path, check=True).stdout:
            fail("TASK_UNTRACKED_SOURCE", "stage reviewed new files before starting a new review and adoption")
        source_patch = run_git(["diff", "--binary", "HEAD"], cwd=repo.path, check=True).stdout
        lease = self.manager._leases.acquire(repo_fingerprint=self.manager._repo_fingerprint(repo),
            review_id=rt.review_id, operation="prepare_adoption", plan_sha256=candidate["patch_sha256"],
            ttl_seconds=self.manager._lease_ttl_seconds(budget))
        scratch = self.root / task["task_id"] / c["criterion_id"]
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with isolated_worktree(repo=repo.path, source_patch=source_patch, scratch_root=scratch,
                                   extra_patches=(candidate["patch"],)) as worktree:
                expected_content = source_content_sha256(worktree)
                # N01: 源码维度的规则保护原料。before 是基线（仓库当前内容，
                # 新文件为 None），after 是候选隔离工作树内容；逐路径送给
                # rule_protection 的 source_diff，删断言/恒真/配置弱化在
                # 统一入口被识别，而不只靠执行状态计数。
                after = {p: blob(safe_file(worktree, p)) for p in paths}
                source_files = {}
                for p in paths:
                    if p.endswith((".py", ".ini", ".cfg", ".toml")):
                        source_files[p] = {
                            "baseline": _blob_text(before.get(p)),
                            "candidate": _blob_text(after.get(p))}
                source_diff = {"files": source_files} if source_files else None
                executor = (SandboxedExecutor(scratch / "sandbox", process_record=scratch / "verification.runtime.json")
                            if self.manager.execution_mode is ExecutionMode.SANDBOXED
                            else TrustedLocalExecutor(process_record=scratch / "verification.runtime.json"))
                if c.get("language"):
                    # T07: non-Python criteria go through the language adapter
                    # (fixed argv, no shell) instead of the pytest-only path.
                    # The baseline is a second isolated worktree without the
                    # candidate so rule protection compares like with like.
                    adapter = discover_adapter(Path(worktree), language=c["language"])
                    with isolated_worktree(repo=repo.path, source_patch=source_patch,
                                           scratch_root=scratch / "baseline",
                                           extra_patches=()) as base_worktree:
                        base_adapter = discover_adapter(Path(base_worktree), language=c["language"])
                        declared = tuple(t for t in tests if t != candidate.get("path"))
                        base_run = base_adapter.run_tests(executor, targets=declared,
                            artifact_dir=scratch / "baseline-artifacts", timeout=budget)
                    cand_run = adapter.run_tests(executor, targets=tuple(tests),
                        artifact_dir=scratch / "candidate-artifacts", timeout=budget)
                    base_pairs = [{"test_id": tid, "status": status.value}
                                  for tid, status in base_run.tests.statuses]
                    cand_pairs = [{"test_id": tid, "status": status.value}
                                  for tid, status in cand_run.tests.statuses]
                    protection = rule_protection(parse_test_list(base_pairs),
                                                 parse_test_list(cand_pairs),
                                                 {"baseline_scope": list(request.get("test_files") or []),
                                                  "candidate_scope": list(tests)},
                                                 source_diff=source_diff)
                    if protection.get("verdict") == "blocked":
                        fail("TASK_TEST_WEAKENING_FORBIDDEN",
                             "; ".join(protection.get("blocking_codes", []))
                             or "rule protection blocked adoption")
                    bad = [tid for tid, status in cand_run.tests.statuses
                           if status in (TestStatus.FAILED, TestStatus.ERROR, TestStatus.SKIPPED)]
                    if bad:
                        fail("TASK_TEST_NOT_EXECUTED", "candidate contains skipped or failed tests")
                    base_ids = {tid for tid, _ in base_run.tests.statuses}
                    new_cases = sorted(tid for tid, status in cand_run.tests.statuses
                                       if status is TestStatus.PASSED and tid not in base_ids)
                    if candidate.get("path") and not new_cases:
                        fail("TASK_TEST_NOT_EXECUTED", "candidate test was not collected/executed")
                    cases = cand_run.tests.statuses
                else:
                    result = executor.run([repo.python, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider",
                                           "--junitxml=" + str(scratch / "verification.xml")],
                        cwd=worktree, timeout=budget, env=sanitized_environment({"PYTHONWARNINGS": "ignore", "PYTHONDONTWRITEBYTECODE": "1"}))
                    if result.returncode != 0:
                        fail("TASK_VERIFICATION_FAILED", "declared tests and candidate must pass in isolation")
                    from xml.etree import ElementTree as ET
                    xml = ET.parse(scratch / "verification.xml")
                    cases = list(xml.iter("testcase"))
                    key = lambda case: case.attrib.get("classname", "") + "::" + case.attrib.get("name", "")
                    passed = lambda case: not any(x.tag in {"skipped", "failure", "error"} for x in case)
                    original_xml = _find_evidence_bundle(rt.review_dir) / "baseline.xml"
                    if not original_xml.is_file():
                        fail("TASK_BASELINE_MISSING", "declared-test baseline is required for adoption")
                    original_passed = {key(case) for case in ET.parse(original_xml).iter("testcase") if passed(case)}
                    now_passed = {key(case) for case in cases if passed(case)}
                    if not cases or not original_passed <= now_passed:
                        fail("TASK_TEST_NOT_EXECUTED", "original passing tests disappeared, skipped, or failed")
                    # Rule protection (T05): a green run must not have weakened
                    # the rules — dropped assertions, new skips or a shrunken
                    # scope are refused before an adoption plan can exist.
                    try:
                        protection = rule_protection(
                            original_xml.read_text(encoding="utf-8"),
                            (scratch / "verification.xml").read_text(encoding="utf-8"),
                            {"baseline_scope": list(request.get("test_files") or []),
                             "candidate_scope": list(tests)},
                            source_diff=source_diff)
                    except CandidateIntakeError:
                        protection = {"verdict": "undetermined",
                                      "reason": "protection comparator unavailable"}
                    if protection.get("verdict") == "blocked":
                        fail("TASK_TEST_WEAKENING_FORBIDDEN",
                             "; ".join(protection.get("blocking_codes", []))
                             or "rule protection blocked adoption")
                    module = candidate.get("path", "").removesuffix(".py").replace("/", ".")
                    generated = [case for case in cases if module and (case.attrib.get("classname", "") == module
                                 or case.attrib.get("classname", "").startswith(module + "."))]
                    new_cases = [key(case) for case in generated if passed(case)]
                    if any(not passed(case) for case in generated):
                        fail("TASK_TEST_NOT_EXECUTED", "candidate contains skipped or failed tests")
                    if candidate.get("path") and not new_cases:
                        fail("TASK_TEST_NOT_EXECUTED", "candidate test was not collected/executed")
                if source_content_sha256(worktree) != expected_content:
                    fail("TASK_TEST_MUTATED_SOURCE", "verification changed the source snapshot")
        finally:
            self.manager._leases.release(lease)
        self._source_guard(task, repo)
        child_spec = spec.as_dict()
        child_spec["source"] = {**child_spec["source"], "repo_id": repo.repo_id,
            "base_ref": task["base_commit"], "target_ref": "HEAD", "target_commit": "", "workspace_snapshot_sha256": ""}
        child_spec["scope"]["test_files"] = tests
        child_spec["constraints"]["budget_seconds"] = budget
        # Fresh review uses deterministic evidence execution: no additional model
        # transfer or autonomous write permission is implied by an adoption.
        child_spec["autonomy_policy"]["model_provider"] = "deterministic"
        child_spec["autonomy_policy"]["allow_repair_branch"] = False
        child_spec["autonomy_policy"]["allow_local_commit"] = False
        child_spec["autonomy_policy"]["allow_dependency_install"] = False
        child_spec["constraints"]["allow_dependency_install"] = False
        child_spec["output_policy"]["local_branch"] = False
        child_spec["model_policy"]["provider"] = "deterministic"
        child_spec["product_mode"] = "standard"
        child_spec["created_at"] = "1970-01-01T00:00:00+00:00"
        child_spec = ReviewSpec.parse(child_spec).as_dict()
        plan = {"source_snapshot_sha256": task["source_snapshot"]["snapshot_sha256"],
                "patch_sha256": candidate["patch_sha256"], "paths": paths, "test_files": tests,
                "budget_seconds": budget, "expires_at": time.time() + 900, "status": "prepared",
                "before": before, "after": after, "index_sha256": index_digest(repo.path),
                "expected_content_sha256": expected_content,
                "child_spec": child_spec, "generated_test_ids": new_cases,
                "child_spec_sha256": digest(child_spec), "verification": {"passed": True, "test_count": len(cases)},
                "rule_protection": protection}
        plan["plan_sha256"] = digest(plan)
        c["adoption_plan"] = plan
        c["status"] = "prepared"
        candidate["verification"]["declared_tests"] = plan["verification"]
        # The auto-adoption policy (T04) refuses to gate on a missing verdict.
        candidate["rule_protection"] = protection

    def _auto_confirm(self, task, c, rt, repo):
        """Apply a prepared candidate under an explicit standing authorization.

        This is opt-in and intentionally narrower than manual adoption: the
        review must be L3, the human-issued authorization must cover the exact
        repository and budget, and the candidate remains a bounded text patch.
        The normal prepare/confirm transaction and fresh review are reused.
        """
        if c.get("adoption_mode") != "preauthorized":
            fail("TASK_AUTO_ADOPTION_NOT_ENABLED", "criterion did not opt into preauthorized adoption")
        auth = getattr(self.manager, "standing_authorization", None)
        if auth is None or getattr(getattr(self.manager, "agent_level", None), "value", "") != "l3":
            fail("TASK_AUTO_ADOPTION_NOT_AUTHORIZED", "L3 standing authorization is required")
        if not auth.covers_repo(repo.path) or auth.expired():
            fail("TASK_AUTO_ADOPTION_NOT_AUTHORIZED", "authorization does not cover this repository or is expired")
        plan = c.get("adoption_plan") or {}
        budget = plan.get("budget_seconds")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget > auth.budget_seconds_max:
            fail("TASK_AUTO_ADOPTION_NOT_AUTHORIZED", "adoption budget exceeds standing authorization")
        candidate = c.get("candidate") or {}
        paths = candidate.get("paths") or []
        if len(paths) > 3 or sum(1 for line in candidate.get("patch", "").splitlines()
                                if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))) > 120:
            fail("TASK_AUTO_ADOPTION_SCOPE_TOO_LARGE", "preauthorized adoption is limited to a small text patch")
        # Count and journal the write on the original review's durable ledger
        # before touching the user's files.  The ordinary transaction then
        # performs the same snapshot, index and recovery checks as manual mode.
        assert_budget = getattr(self.manager, "_assert_write_budget", None)
        if callable(assert_budget):
            assert_budget(rt)
        rt.events.append("autonomy.task_adoption", {
            "task_id": task["task_id"], "criterion_id": c["criterion_id"],
            "authorization_sha256": auth.source_sha256,
            "mode": "preauthorized", "paths": list(paths),
            "semantics": "human-issued standing authorization; not agent self-approval",
        })
        # T04: persistent, cross-task ledger gates the write itself. The
        # reservation counts as an attempt regardless of the outcome below.
        attempt = self.auto_adoption_policy.assert_allowed(
            task_id=task["task_id"],
            round_id=str(task.get("active_round", {}).get("round_id", "")),
            criterion_id=c["criterion_id"],
            repo_fingerprint=self.manager._repo_fingerprint(repo),
            paths=list(paths),
            budget_seconds=budget,
            trigger_source=f"auto:{task['origin_review_id']}:{c['criterion_id']}",
            basis=c.get("behavioral_basis"),
            rule_protection=(c.get("candidate") or {}).get("rule_protection"))
        c["auto_attempt_id"] = attempt["attempt_id"]
        self._confirm(task, c, rt, repo,
                      {"plan_sha256": plan.get("plan_sha256", "")})

    def _confirm(self, task, c, rt, repo, raw):
        plan = c["adoption_plan"]
        if raw.get("plan_sha256") != plan["plan_sha256"] or digest({k: v for k, v in plan.items() if k != "plan_sha256"}) != plan["plan_sha256"]:
            fail("TASK_ADOPTION_PLAN_STALE", "adoption plan fingerprint does not match")
        if time.time() > plan["expires_at"]:
            fail("TASK_ADOPTION_PLAN_EXPIRED", "prepare a new adoption plan")
        self._source_guard(task, repo)
        if index_digest(repo.path) != plan["index_sha256"]:
            fail("TASK_INDEX_CHANGED", "index changed since preview")
        if self.manager._live_id:
            fail("TASK_REVIEW_BUSY", "wait for the active review before adoption")
        if text_hash(c["candidate"]["patch"]) != plan["patch_sha256"]:
            fail("TASK_CANDIDATE_CHANGED", "confirmed candidate does not match preview")
        lease = self.manager._leases.acquire(repo_fingerprint=self.manager._repo_fingerprint(repo),
            review_id=rt.review_id, operation="adopt_candidate", plan_sha256=plan["plan_sha256"], ttl_seconds=120)
        try:
            self._source_guard(task, repo)
            if any(blob(safe_file(repo.path, p)) != plan["before"][p] for p in plan["paths"]):
                fail("SOURCE_SNAPSHOT_CHANGED", "a candidate target changed")
            c["status"] = "applying"
            c["confirmed_at"] = time.time()
            self._save(task, "adoption.confirmed", {"criterion_id": c["criterion_id"], "plan_sha256": plan["plan_sha256"]})
            # Bounded per-file write-ahead transaction; no git index/ref mutation.
            for p in plan["paths"]:
                path = safe_file(repo.path, p)
                if blob(path) != plan["before"][p]:
                    fail("SOURCE_SNAPSHOT_CHANGED", "candidate target changed during apply")
                value = plan["after"][p]
                if value is None:
                    path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temp = path.with_name(path.name + ".shuimu-" + secrets.token_hex(6))
                    try:
                        with temp.open("xb") as handle:
                            handle.write(base64.b64decode(value)); handle.flush(); os.fsync(handle.fileno())
                        if path.exists():
                            os.chmod(temp, path.stat().st_mode)
                        os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
            if index_digest(repo.path) != plan["index_sha256"]:
                fail("TASK_INDEX_CHANGED", "concurrent index modification; inspect recovery record")
            c["adopted_snapshot"] = _repo_snapshot(repo.path)
            if source_content_sha256(repo.path) != plan["expected_content_sha256"]:
                fail("SOURCE_SNAPSHOT_CHANGED", "source changed outside the authorized patch during adoption")
            c["status"] = "adopted"
            self._save(task, "adoption.applied", {"criterion_id": c["criterion_id"]})
            self._settle_auto_attempt(c, "applied")
        except Exception:
            if c["status"] == "applying":
                c["status"] = "recovery_required"
                self._save(task, "adoption.interrupted", {"criterion_id": c["criterion_id"]})
                self._settle_auto_attempt(c, "failed")
            raise
        finally:
            self.manager._leases.release(lease)
        # Successful adoption opens an immutable round: the applied criterion
        # proceeds to its fresh review inside the new round, every other
        # checked item is conservatively marked pending_recheck.
        self._open_round(task, "adoption_applied",
                         source_snapshot=copy.deepcopy(c["adopted_snapshot"]),
                         keep_status_for=(c["criterion_id"],))
        c = next(x for x in task["criteria"] if x["criterion_id"] == c["criterion_id"])
        for carried in task["criteria"]:
            # The applied patch itself may shift target lines; rebase identity.
            self._refresh_target_mapping(task, carried, repo)
        self._save(task, "round.opened", {"reason": "adoption_applied",
                   "round_id": task["active_round"]["round_id"],
                   "round_no": task["active_round"]["round_no"]})
        self._start_review(task, c, repo)

    def _settle_auto_attempt(self, c, status):
        attempt_id = c.get("auto_attempt_id")
        if attempt_id:
            try:
                self.auto_adoption_policy.settle(attempt_id, status=status)
            except IntakeError:
                # The reservation already counted the attempt; a settlement
                # error must not corrupt the adoption transaction itself.
                pass

    def _recover(self, task, c, repo):
        plan = c["adoption_plan"]
        observed = {p: blob(safe_file(repo.path, p)) for p in plan["paths"]}
        if (observed == plan["after"] and index_digest(repo.path) == plan["index_sha256"]
                and source_content_sha256(repo.path) == plan["expected_content_sha256"]):
            c["status"] = "adopted"
            c["adopted_snapshot"] = _repo_snapshot(repo.path)
            self._save(task, "adoption.recovered_applied", {"criterion_id": c["criterion_id"]})
            self._settle_auto_attempt(c, "recovered")
            self._open_round(task, "adoption_applied",
                             source_snapshot=copy.deepcopy(c["adopted_snapshot"]),
                             keep_status_for=(c["criterion_id"],))
            c = next(x for x in task["criteria"] if x["criterion_id"] == c["criterion_id"])
            self._save(task, "round.opened", {"reason": "adoption_applied",
                       "round_id": task["active_round"]["round_id"],
                       "round_no": task["active_round"]["round_no"]})
            self._start_review(task, c, repo)
        elif observed == plan["before"]:
            c["status"] = "candidate_ready"
            c["adoption_plan"] = None
        else:
            fail("TASK_RECOVERY_MANUAL_REQUIRED", "partial application or concurrent edits; preserve files and inspect recorded diff")

    def _start_review(self, task, c, repo, force_new_generation=False):
        if _repo_snapshot(repo.path) != c["adopted_snapshot"]:
            fail("TASK_ADOPTED_SOURCE_CHANGED", "adopted snapshot changed; a fresh task is required")
        spec = copy.deepcopy(c["adoption_plan"]["child_spec"])
        spec["source"]["repo_id"] = repo.repo_id
        # create_v2 freezes the actual new snapshot; approve uses its own source
        # guard. The composite human confirmation authorized exactly this spec.
        generation = int(c.get("review_generation", 0))
        if c.get("followup_review_id"):
            previous = self.manager._runtime(c["followup_review_id"])
            if force_new_generation or previous.state.snapshot.status.value in {"FAILED", "ABORTED"}:
                generation += 1
                c["review_generation"] = generation
        child_key = task["task_id"] + ":" + c["criterion_id"] + ":followup:" + str(generation)
        child = self.manager.create_v2(spec, idempotency_key=child_key)
        child_id = child["review_id"]
        c["followup_review_id"] = child_id
        self._save(task, "followup.created", {"criterion_id": c["criterion_id"], "review_id": child_id})
        runtime = self.manager._runtime(child_id)
        runtime.request["evidence_task"] = {"task_id": task["task_id"], "criterion_id": c["criterion_id"],
            "origin_review_id": task["origin_review_id"], "adoption_plan_sha256": c["adoption_plan"]["plan_sha256"]}
        _atomic_json(runtime.review_dir / "request.json", runtime.request)
        if child["state"]["status"] == "AWAITING_APPROVAL":
            self.manager.approve(child_id, child["plan"]["plan_sha256"],
                                 idempotency_key=child_key + ":approve")
        c["status"] = "reviewing"
        self._save(task, "followup.started", {"criterion_id": c["criterion_id"], "review_id": child_id})

    @staticmethod
    def _compare(c, child):
        old = c.get("origin_rows", [])
        new = rows_for(child)
        result = {"status": "inconclusive", "reason": "目标对应证据不足，不能自动宣称补齐",
                  "origin_rows": old, "followup_rows": [], "improved": False,
                  "followup_review_id": child.review_id}
        if not child.state.snapshot.valid_bundle or not old:
            return result
        record = c.get("target_mapping") if isinstance(c.get("target_mapping"), dict) else None
        by_origin = {}
        for m in (record or {}).get("mappings", []):
            origin = m.get("origin") or {}
            by_origin[(origin.get("file"), origin.get("line"))] = m
        for row in old:
            m = by_origin.get((row.get("file"), row.get("line")))
            if m and m.get("status") not in {"mapped", "confirmed"}:
                # Ambiguity is never guessed away: a rewrite the mapper could
                # not resolve uniquely requires explicit human confirmation.
                result["reason"] = ("源码已改写，目标映射需人工确认（" +
                                    (m.get("reason") or m.get("status") or "ambiguous") + "）；"
                                    "确认目标身份后仍以真实复验结论为准")
                result["target_mapping_pending"] = m.get("mapping_id", "")
                return result
            loc = (m.get("mapped") or {}) if m else {}
            want_file = loc.get("file") or row.get("file")
            want_line = loc.get("line")
            matches = []
            if want_line is not None:
                matches = [n for n in new if n.get("file") == want_file
                           and n.get("line") == want_line]
            if len(matches) != 1:
                matches = [n for n in new if n.get("file") == want_file
                           and n.get("text") == row.get("text")]
            if len(matches) != 1:
                return result
            result["followup_rows"].append(matches[0])
        supported = lambda r: r.get("label") == "承重" and r.get("admissibility") == "A"
        if all(supported(row) for row in result["followup_rows"]):
            result.update(status="supported", reason="所选目标在采用后的新版本中具有A级测试证据",
                          improved=not all(supported(row) for row in old))
        else:
            result.update(status="gap_remains", reason="采用后的目标仍有证据不足；候选签证不替代目标复验")
        return result
