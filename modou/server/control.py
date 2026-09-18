"""Review orchestration and server-side repository capability registry."""
from __future__ import annotations

import json
import hashlib
import errno
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from modou.application import AnalysisRequest, ExecutionMode
from modou.adapters import RepoAdapter
from modou.agent.escalation import material_changes
from modou.safety_events import SCOPE_WIDENING_CHANGES, emit_safe
from modou.executor import (DEFAULT_RESOURCE_LIMITS, SandboxedExecutor,
                            TrustedLocalExecutor, execution_scope,
                            recover_process_record, sanitized_environment)
from modou.agent.events import EventJournalError, EventStore
from modou.agent.models import (ActionKind, AgentLevel, AgentState, CATALOG_V1,
                                NO_PROGRESS_WINDOW, Observation as AgentObservation,
                                ReprioritizationRejected, StopReason, ToolCatalog,
                                ToolRisk)
from modou.agent.protocol import (ActionRequest, CapabilityAuthority,
                                  ObservationRecord, ObservationStatus,
                                  ProtocolError)
from modou.agent.lease import (LeaseError, RepositoryLease,
                               LEASE_TTL_CEILING_SECONDS,
                               RepositoryLeaseManager)
from modou.agent.narrator import (NarrationLayout, NarrationRejected,
                                  compile_narration, deterministic_layout)
from modou.agent.policy import (PolicyError, compile_plan, deterministic_draft)
from modou.agent.spec import (DEFAULT_CREATED_AT, SCHEMA_VERSION, ReviewSpec,
                              ReviewSpecError)
from modou.locate import (SCHEMA_VERSION as LOCATE_SCHEMA_VERSION, dedupe_paths,
                          extract_tokens, search_repo)
from modou.models import TestVector
from modou.coverage import (CoverageUnavailable, fresh_data_file,
                            read_contexts, write_rcfile)
from modou.testrange import collect_nodeids, run_argv, run_declared
from modou.server import reverification
from modou.server import edit_sessions
from modou.agent.memory import ReviewMemoryError, load as load_review_memory
from modou.agent.evidence import EvidenceManifest, verify_manifest
from modou.agent import delivery_ceremony
from modou.agent.repair import (RepairError, RepairVerification,
                                _assert_original_unchanged,
                                _syntax_gate, deliver_verified_patch,
                                isolated_worktree, run_isolated_new_test,
                                validate_candidate_patch,
                                validate_test_only_patch)
from modou.agent.repair_candidate import (REPAIR_CANDIDATE_SCHEMA_VERSION,
                                          RepairContext, generate_candidate)
from modou.server.standing_authorization import (StandingAuthorization,
                                                 StandingAuthorizationError)
from modou.governance import (GovernanceError, delete_review as delete_review_data,
                              export_manifest as review_export_manifest)
from modou.agent.provider import (OpenAICompatibleProvider, ProviderUnavailable)
from modou.agent.draft import (DEFAULT_BUDGET_SECONDS, DRAFT_PROMPT_VERSION,
                               DraftRejected,
                               build_input as build_draft_input,
                               failure_label as draft_failure_label,
                               parse_draft, retry_hint as draft_retry_hint)
from modou.agent.recommendations import (RECOMMENDATION_PROMPT_VERSION,
                                         RecommendationRejected, build_input,
                                         failure_label, parse_recommendations,
                                         retry_hint)
from modou.agent.test_proposal import (TEST_PROPOSAL_PROMPT_VERSION,
                                       TestProposalContext,
                                       TestProposalRejected,
                                       UNTRUSTED_BEGIN, UNTRUSTED_END,
                                       UNTRUSTED_NOTICE,
                                       build_input as build_test_input,
                                       failure_excerpt as test_failure_excerpt,
                                       failure_label as test_failure_label,
                                       parse_proposal,
                                       retry_hint as test_retry_hint)
from modou.agent.test_visa import (VISA_PROTOCOL_VERSION, Intervention,
                                   VisaError, assert_no_holdout_leak,
                                   generation_feedback, verify_candidate)
from modou.agent.review import (ReviewStateError, ReviewStatus, ReviewStore,
                                TERMINAL)
from modou.agent.session import AnalysisSession, SessionError, ToolRouter
from modou.ledger import records as ledger_records, store as ledger_store
from modou.runroot import RunRoot, verify_bundle
from modou import capabilities as caps
from modou.ddmin import ProbeStrategy
from modou.agent.provider import coverage_first_order
from modou.review_bundle import (build_review_bundle_v2, content_sha256,
                                 payload_digest)
from modou.repository_security import (RepositorySecurityError,
                                       require_repository_safe)
from modou import mutate
from modou.safe_git import run_git
from modou.modes import (DEFAULT_DETERMINISTIC_SCHEDULER, PRODUCT_MODE_PROVIDER,
                         PRODUCT_MODES, execution_mode_for, product_mode_for)

REVIEW_FOCUSES = frozenset({
    "evidence-boundary", "named-regression", "coverage-gap",
    "call-path", "budget-first",
})

# 人工处置的答案按事件类型封闭：与各升级事件自己声明的 allowed_answers
# 保持同一词表，处置留痕不允许发明新答案。
_DISPOSITION_ANSWERS = {
    "test.needs_human": frozenset({"write_manually",
                                   "retry_different_approach",
                                   "accept_gap"}),
    "decision.requested": frozenset({"continue", "stop"}),
}

# ---- 证据工作台的快照绑定源码浏览（P3） -------------------------------
# 浏览器里的代码视图只能读"这次审查当时所见"的源码：接口先核对工作区
# 快照哈希，再按封闭的白名单路径出内容。任何穿越、二进制、超大文件
# 都用稳定错误码拒绝，绝不退化成"读到了什么就展示什么"。
SOURCE_FILE_MAX_BYTES = 512 * 1024
SOURCE_TREE_MAX_ENTRIES = 20_000
_SOURCE_READ_ONLY_REASON = "evidence_read_only"

# ---- P5/P6：独立于机器证据的追加式人工评论与注释包 -------------------
# 评论是「人后来如何理解证据」的通道：它只落在 comments/ 与独立的
# annotations 哈希链里，绝不追加进已密封的 ReviewBundle 事件流。
COMMENT_KINDS = frozenset({
    "note", "question", "challenge", "evidence_request", "change_request",
})
COMMENT_STATUSES = frozenset({"open", "resolved", "withdrawn", "outdated"})
COMMENT_BODY_MAX_CHARS = 2000
COMMENT_AUTHOR_MAX_CHARS = 100
_ANNOTATION_CHAIN_ZERO = "0" * 64

# ---- P7：质疑—复验闭环 -----------------------------------------------
# 用户质疑一条机器主张时，系统只能用**新的实验**回应：重放原主张所凭的
# 干预，把新旧观测摆在同一张桌上。人发起质疑，实验重新签字；模型在这
# 条路径上没有角色。所有事件只进 annotations 哈希链，绝不改写已密封的
# 机器证据包。
REVERIFICATION_REASONS = frozenset({
    "test_did_not_execute_code",   # 测试没有真正执行这段代码
    "collection_crash_suspected",  # 失败可能是导入或收集崩溃
    "flaky_result_suspected",      # 结果可能不稳定
    "source_changed",              # 代码已经变化
    "test_scope_incomplete",       # 测试范围不完整
    "new_test_available",          # 我有一条新的测试
    "other_needs_explanation",     # 其他，需要人工说明
})

# 策略 4 的签证预算：与 verify_candidate 默认一致，不开放用户旋钮
# ——签证跑多久的决定权不留给质疑发起方。
TEST_VISA_BUDGET_SECONDS = 60.0

REVERIFICATION_STATUSES = frozenset({
    "planned",     # 复验计划已生成，等待用户确认
    "approved",    # 用户已确认，实验即将重放
    "replaying",   # 干预正在重放
    "settled",     # 新旧结果已对比，主张已确认/修订/扣留
    "rejected",    # 用户否决了复验计划
    "failed",      # 重放本身失败（预算、恢复、环境）——只能扣留，不能确认
})
REVERIFICATION_OUTCOMES = frozenset({
    "claim.confirmed", "claim.revised", "claim.withheld",
})

_SOURCE_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown", ".markdown": "markdown",
    ".txt": "text", ".rst": "rst",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml",
    ".css": "css", ".html": "html", ".ini": "ini", ".sh": "shell",
}


class IntakeError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class StandardModeViolation(RuntimeError):
    """标准审查触碰了可能调用模型的路径。

    零模型是三层独立不变量（intake、运行前断言、证据包校验）；这个
    异常属于第二层——它在任何模型请求发出之前终止运行，并先把拦截
    事件落盘，让审计能看到"想调但被拦下"的记录。
    """


def _gate_model_path_for_mode(product_mode: str, stage: str) -> None:
    """无 ReviewRuntime 的模型路径（如一句话起草）共享的同一道门卫。

    没有事件账本可写，所以这里只保证两条硬边界：稳定错误码，以及
    在任何模型请求发出之前终止。默认按标准模式解释（计划 6.1.4）：
    旧客户端不带 product_mode 时宁可拒绝，也不猜测。
    """
    if product_mode != "standard":
        return
    raise StandardModeViolation(
        f"STANDARD_MODE_MODEL_PATH_BLOCKED: {stage} 阶段试图调用模型")


@dataclass(frozen=True)
class RegisteredRepo:
    repo_id: str
    path: Path
    display_name: str
    technical_name: str
    python: str


@dataclass(frozen=True)
class ReviewPreset:
    """A safe, public alias for a pre-approved real review input."""

    preset_id: str
    display_name: str
    description: str
    repo_id: str
    test_files: tuple[str, ...]
    goal: str
    budget_seconds: int
    model_provider: str


class RepoRegistry:
    """Opaque capabilities for canonical, explicitly allowed Git repositories."""

    # 内置演示仓库的固定中文名。目录名（technical_name）保持英文 slug，
    # 供前端做目标适配和旧响应回退；display_name 面向中文用户。
    DEMO_DISPLAY_NAMES = {
        "retry_demo": "重试机制演示仓库",
        "uncovered_demo": "覆盖缺口演示仓库",
        "orphan_demo": "游离代码演示仓库",
        "tri_state_demo": "三态综合演示仓库",
        "dependency_demo": "多文件依赖演示仓库",
        "scheduler_demo": "调度对比演示仓库",
    }

    def __init__(self, paths: list[Path], python_by_repo: dict[Path, Path] | None = None,
                 presets: list[dict] | None = None):
        self._repos: dict[str, RegisteredRepo] = {}
        # Do not resolve interpreter symlinks: invoking a venv through its own
        # bin/python path is how Python discovers that environment.
        python_by_repo = {Path(k).expanduser().resolve(): Path(v).expanduser().absolute()
                          for k, v in (python_by_repo or {}).items()}
        seen: set[Path] = set()
        for raw in paths:
            path = Path(raw).expanduser().resolve(strict=True)
            if path in seen:
                continue
            py = python_by_repo.get(path, Path(sys.executable))
            self.add(path, python=py)
            seen.add(path)
        if not self._repos:
            raise IntakeError("NO_ALLOWED_REPOS", "at least one --allow-repo is required")
        self._presets = self._compile_presets(presets or [])

    def public(self) -> list[dict]:
        return [{"repo_id": r.repo_id, "display_name": r.display_name,
                 "technical_name": r.technical_name}
                for r in self._repos.values()]

    def registered(self) -> tuple[RegisteredRepo, ...]:
        """All explicitly allow-listed repositories, registration order."""
        return tuple(self._repos.values())

    def add(self, raw_path: Path | str, *, python: Path | str | None = None) -> RegisteredRepo:
        """Authorize one local Git root for this server process.

        The browser receives only an opaque repository id. This mutation is
        protected by the same localhost origin and launch token as review
        creation, and lasts only until the local server exits.
        """
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise IntakeError("REPO_INVALID", f"repository path does not exist: {raw_path}") from exc
        for existing in self._repos.values():
            if existing.path == path:
                return existing
        try:
            check = run_git(["rev-parse", "--show-toplevel"], cwd=path,
                            timeout=10)
        except OSError as exc:
            raise IntakeError("REPO_INVALID", f"cannot inspect {path}: {exc}") from exc
        if check.returncode != 0 or Path(check.stdout.strip()).resolve() != path:
            raise IntakeError("REPO_INVALID", f"not a Git repository root: {path}")
        interpreter = Path(python or sys.executable).expanduser().absolute()
        if not interpreter.is_file():
            raise IntakeError("PYTHON_INVALID", f"interpreter not found for {path.name}: {interpreter}")
        repo_id = secrets.token_urlsafe(18)
        technical_name = path.name
        repo = RegisteredRepo(
            repo_id, path, self.DEMO_DISPLAY_NAMES.get(technical_name, technical_name),
            technical_name, str(interpreter))
        self._repos[repo_id] = repo
        return repo

    def presets_public(self) -> list[dict]:
        """Return presentation inputs without exposing a filesystem path."""
        return [{
            "preset_id": p.preset_id,
            "display_name": p.display_name,
            "description": p.description,
            "repo_id": p.repo_id,
            "test_files": list(p.test_files),
            "goal": p.goal,
            "budget_seconds": p.budget_seconds,
            "model_provider": p.model_provider,
        } for p in self._presets]

    def _compile_presets(self, raw_presets: list[dict]) -> tuple[ReviewPreset, ...]:
        # 预设的 repo_name 指的是**目录名**，不是面向用户的展示名。
        # display_name 现在可能是中文（DEMO_DISPLAY_NAMES），拿它当索引会让
        # configs/review-presets.example.json 里的每一条都匹配不上，服务器
        # 在启动时就抛 PRESET_INVALID——展台上表现为"双击启动，什么都没起来"。
        by_name: dict[str, list[RegisteredRepo]] = {}
        for repo in self._repos.values():
            by_name.setdefault(repo.technical_name, []).append(repo)
        compiled: list[ReviewPreset] = []
        seen: set[str] = set()
        allowed = {"preset_id", "display_name", "description", "repo_name",
                   "test_files", "goal", "budget_seconds", "model_provider"}
        for raw in raw_presets:
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise IntakeError("PRESET_INVALID", "preset has unsupported fields")
            preset_id = str(raw.get("preset_id") or "")
            if (not preset_id or preset_id in seen or len(preset_id) > 64
                    or not all(c.isalnum() or c in "-_" for c in preset_id)):
                raise IntakeError("PRESET_INVALID", "preset_id must be unique and URL-safe")
            matches = by_name.get(str(raw.get("repo_name") or ""), [])
            if len(matches) != 1:
                raise IntakeError("PRESET_INVALID", "repo_name must identify one allowed repository")
            repo = matches[0]
            tests = self.validate_test_files(repo, raw.get("test_files") or [])
            budget = int(raw.get("budget_seconds") or 300)
            provider = str(raw.get("model_provider") or "deterministic")
            if not 1 <= budget <= 3600 or provider not in {"deterministic", "live"}:
                raise IntakeError("PRESET_INVALID", "preset budget or provider is invalid")
            compiled.append(ReviewPreset(
                preset_id=preset_id,
                display_name=str(raw.get("display_name") or preset_id)[:100],
                description=str(raw.get("description") or "")[:240],
                repo_id=repo.repo_id,
                test_files=tests,
                goal=str(raw.get("goal") or "审查补丁新增代码的证据边界")[:500],
                budget_seconds=budget,
                model_provider=provider,
            ))
            seen.add(preset_id)
        return tuple(compiled)

    def get(self, repo_id: str) -> RegisteredRepo:
        try:
            return self._repos[repo_id]
        except KeyError as exc:
            raise IntakeError("UNKNOWN_REPO_ID", "repository is not registered") from exc

    def validate_test_files(self, repo: RegisteredRepo,
                            raw_paths: list[str]) -> tuple[str, ...]:
        if not raw_paths:
            raise IntakeError("TEST_SCOPE_REQUIRED", "at least one test file is required")
        out: list[str] = []
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                raise IntakeError("TEST_PATH_INVALID", "test path must be a non-empty string")
            p = Path(raw)
            if p.is_absolute() or ".." in p.parts:
                raise IntakeError("TEST_PATH_ESCAPE", raw)
            try:
                resolved = (repo.path / p).resolve(strict=True)
                resolved.relative_to(repo.path)
            except (OSError, ValueError) as exc:
                raise IntakeError("TEST_PATH_ESCAPE", raw) from exc
            if not resolved.is_file():
                raise IntakeError("TEST_PATH_INVALID", raw)
            rel = resolved.relative_to(repo.path).as_posix()
            if rel != p.as_posix().lstrip("./"):
                # Any symlink is rejected, even an in-repo one, so UI input and
                # executed path have exactly one identity.
                raise IntakeError("TEST_PATH_SYMLINK", raw)
            out.append(rel)
        return tuple(dict.fromkeys(out))

    def discover_python_test_files(self, repo: RegisteredRepo) -> tuple[str, ...]:
        """Return a bounded, deterministic local pytest-file suggestion.

        Discovery is intentionally only a convenience for v2 intake.  The
        resolved paths still go through ``validate_test_files`` before any
        repository code is run.
        """
        ignored = {".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache"}
        found: list[str] = []
        for path in repo.path.rglob("*.py"):
            relative = path.relative_to(repo.path)
            if ignored.intersection(relative.parts):
                continue
            name = path.name
            if name.startswith("test_") or name.endswith("_test.py"):
                found.append(relative.as_posix())
                if len(found) > 200:
                    raise IntakeError("TEST_DISCOVERY_TOO_BROAD", "more than 200 Python test files")
        if not found:
            raise IntakeError("TEST_DISCOVERY_EMPTY", "no Python test files found; provide scope.test_files")
        return self.validate_test_files(repo, sorted(found))


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(value)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    """字节级原子写：导出包必须与它的内容哈希清单逐字节一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _merge_spec_patch(current: dict, patch: dict) -> dict:
    """增量修订的合并规则：对象按键深合并，数组和标量整值替换。

    调用方“只说改动点”：constraints 只给 budget_seconds 就只改预算，其余
    约束原样继承。嵌套对象递归合并，未点名的键不动；数组没有“按元素
    合并”的语义——给整表就按整表替换。键的白名单仍由 revise_v2 顶层的
    字段校验与 ReviewSpec.parse 把守，这里不做二次校验。
    """
    merged = dict(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_spec_patch(merged[key], value)
        else:
            merged[key] = value
    return merged


class ReviewRuntime:
    def __init__(self, review_id: str, review_dir: Path):
        self.review_id = review_id
        self.review_dir = review_dir
        self.events = EventStore(review_dir, review_id)
        self.state = ReviewStore(review_dir, review_id)
        self.session: AnalysisSession | None = None
        self.router: ToolRouter | None = None
        self.frozen_plan = None
        self.request: dict = {}
        self.provider = None
        self.review_spec: ReviewSpec | None = None
        # Provider 是进程级单例，指标与 trace 会跨 Review 累积；每次 create
        # 记下基线，出包时只取本次增量——第一遍 4 次、第二遍 8 次的累计
        # 现象从这里根治。
        self.metrics_base: dict = {}
        self.trace_start: int = 0
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.capability_token = ""
        self.repo_fingerprint = ""
        self.resource_budget: dict = {}
        self.target_commit = ""
        self.data_policy_sha256 = ""
        self.action_seq = 0
        self.observation_ids: list[str] = []
        self.lease: RepositoryLease | None = None
        self.decision_event = threading.Event()
        self.pending_decision: dict = {}
        self.decision_answer = ""
        self.finished_event = threading.Event()
        receipts = _read_json(review_dir / "action_receipts.json")
        self.action_receipts: dict[str, dict] = receipts if isinstance(receipts, dict) else {}


#: Which registry capability each opt-in probe strategy actually turns on.
#: `hdd_inspired` is the always-on default path and has no separate switch.
_STRATEGY_CAPABILITY = {ProbeStrategy.DDMIN.value: "ddmin"}


class _ReplayStrategyContext:
    """复验策略对宿主的窄访问面：六个具名方法，不是逃生舱。

    策略模块（reverification.py）不反向导入控制面，全靠这个结构
    协议对接；策略拿不到注释、账本写入或密钥等任何其他能力。
    新增能力只能给这张表加一个具名方法并同步协议，没有第二条路。
    """

    def __init__(self, manager: "ReviewManager", rt: ReviewRuntime):
        self._manager = manager
        self._rt = rt

    def replay_once(self, record: dict) -> list[dict]:
        return self._manager._replay_claim_experiments(self._rt, record)

    def source_snapshot_matches(self) -> dict[str, object]:
        request = (self._rt.request
                   or _read_json(self._rt.review_dir / "request.json"))
        expected = str((request.get("source_snapshot") or {})
                       .get("snapshot_sha256") or "")
        repo_id = str((request.get("source") or {}).get("repo_id") or "")
        repo = self._manager.registry.get(repo_id)
        observed = str(_repo_snapshot(repo.path).get("snapshot_sha256") or "")
        return {
            "matches": bool(expected) and observed == expected,
            "expected_sha256": expected,
            "observed_sha256": observed,
        }

    # ------------------------------------------------------------------
    # M0 波次的四个新数据面。共同纪律：先经 _source_context 校验快照，
    # 数据只从账本原实验与隔离 worktree 里来，全部工作在
    # execution_scope 内完成，runner 用完即毁；策略拿到的是 plain
    # dict，摸不到注释、账本写入或密钥。

    def _planned_experiment(self, rt2: ReviewRuntime,
                            record: dict) -> tuple[dict, dict, list[dict]]:
        """复验计划首条实验的 (原实验行, 干预, 账本行)。"""
        ledger_rows = _review_ledger_rows(rt2.review_dir, strict=False)
        item = record["plan"]["replay_experiments"][0]
        original = _review_experiment_row(rt2.review_dir,
                                          item["experiment_id"])
        return original, item["intervention"], ledger_rows

    def coverage_contexts(self, record: dict) -> dict:
        """带 per-test 覆盖率重跑声明范围（coverage_check 的数据面）。

        采集口径由 write_rcfile 强制统一（dynamic_context=
        test_function），仓库自己的 coverage 配置被压掉。覆盖率不可
        用时上抛 CoverageUnavailable，由策略失败关闭，绝不回落重放。
        """
        rt2, repo, _snapshot = self._manager._source_context(
            self._rt.review_id)
        original, intervention, ledger_rows = \
            self._planned_experiment(rt2, record)
        declared = _experiment_declared_nodeids(original, ledger_rows)
        path = str(intervention.get("path") or "")
        lines = [int(n) for n in intervention.get("lines") or []]
        if (not path or intervention.get("file_removed") or not lines
                or path.startswith("/")
                or ".." in PurePosixPath(path).parts):
            raise CoverageUnavailable(
                "coverage check needs a line intervention on a repo file")
        runner = _ReverificationRunner(repo=repo.path, python=repo.python,
                                       nodeids=declared)
        try:
            with execution_scope(self._manager._replay_executor(rt2,
                                                                runner)):
                data_file, rcfile = runner.run_baseline_coverage()
                contexts_all = read_contexts(
                    runner.python, runner.ensure_worktree(), data_file,
                    run_argv=run_argv, rcfile=rcfile)
        finally:
            runner.close()
        file_contexts = contexts_all.get(path)
        if not file_contexts:
            raise CoverageUnavailable(
                f"no per-test coverage context was collected for {path}")
        regressions = _experiment_regressions(original, ledger_rows)
        if not regressions:
            raise CoverageUnavailable(
                "the original experiment has no named regression to grade")
        return {
            "path": path,
            "deleted_lines": lines,
            # 全文件的行 → 上下文名集：known 集合必须覆盖没碰被删行
            # 的测试，否则它们会被误判成"无法解析"而不是"没命中"。
            "contexts_by_line": {int(line): sorted(names)
                                 for line, names in file_contexts.items()},
            "regressions": [[tid, before.value, after.value]
                            for tid, before, after in regressions],
            "original_grade": _claim_grade(
                rt2.review_dir, str(record.get("claim_id") or "")),
        }

    def collect_only(self, record: dict) -> dict:
        """两侧只收集不执行（collection_check 的数据面）。

        同一隔离 worktree：先基线收集，再应用干预收集，还原后重跑
        声明范围核对基线。适配器不支持只收集时
        _ReverificationRunner 会翻译成 CollectionUnsupported。
        """
        rt2, repo, _snapshot = self._manager._source_context(
            self._rt.review_id)
        original, intervention, ledger_rows = \
            self._planned_experiment(rt2, record)
        declared = _experiment_declared_nodeids(original, ledger_rows)
        runner = _ReverificationRunner(repo=repo.path, python=repo.python,
                                       nodeids=declared)
        try:
            with execution_scope(self._manager._replay_executor(rt2,
                                                                runner)):
                runner.run_baseline()
                baseline_ok, baseline_detail = runner.collect_probe()
                target, backup = runner.stage_intervention(intervention)
                try:
                    intervened_ok, intervened_detail = \
                        runner.collect_probe()
                finally:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(backup)
                if runner.restore_and_verify() is not True:
                    raise IntakeError(
                        "REVERIFICATION_RESTORE_FAILED",
                        "collect-only could not restore the worktree")
        finally:
            runner.close()
        return {
            "baseline_collect_ok": baseline_ok,
            "intervened_collect_ok": intervened_ok,
            "detail": (baseline_detail + " || " + intervened_detail)[:800],
            "original_grade": _claim_grade(
                rt2.review_dir, str(record.get("claim_id") or "")),
        }

    def widen_declared_scope(self, record: dict) -> dict:
        """确定性扩大收集范围重跑（scope_widen 的数据面）。

        先在基线 worktree 上确定性全仓收集，新增数超预算只返回
        capped=True，绝不截断后继续；预算内才建第二个 runner 用扩大
        的 nodeid 集跑基线+干预，对比原回归是否全部复现。
        """
        rt2, repo, _snapshot = self._manager._source_context(
            self._rt.review_id)
        original, intervention, ledger_rows = \
            self._planned_experiment(rt2, record)
        declared = _experiment_declared_nodeids(original, ledger_rows)
        regressions = _experiment_regressions(original, ledger_rows)
        if not regressions:
            raise IntakeError("REVERIFICATION_ORIGINAL_VECTOR_MISSING",
                              str(original.get("record_id") or ""))
        probe = _ReverificationRunner(repo=repo.path, python=repo.python,
                                      nodeids=declared)
        try:
            with execution_scope(self._manager._replay_executor(rt2,
                                                                probe)):
                collected = collect_nodeids(
                    _REPLAY_ADAPTER, probe.python, probe.ensure_worktree(),
                    [], timeout=probe.deadline)
        finally:
            probe.close()
        added = sorted(set(collected) - set(declared))
        grade = _claim_grade(rt2.review_dir,
                             str(record.get("claim_id") or ""))
        if len(added) > reverification.SCOPE_WIDEN_MAX_NEW_TESTS:
            return {
                "added_count": len(added), "capped": True,
                "widened_baseline_regens_original_regressions": False,
                "detail": (f"widening would add {len(added)} tests; "
                           "budget cap is "
                           f"{reverification.SCOPE_WIDEN_MAX_NEW_TESTS}"),
                "original_grade": grade,
            }
        widened = sorted(set(collected))
        runner = _ReverificationRunner(repo=repo.path, python=repo.python,
                                       nodeids=widened)
        try:
            with execution_scope(self._manager._replay_executor(rt2,
                                                                runner)):
                runner.run_baseline()
                replayed = runner.replay(intervention)
                if replayed["restored_clean"] is not True:
                    raise IntakeError(
                        "REVERIFICATION_RESTORE_FAILED",
                        "scope widen did not restore the worktree")
        finally:
            runner.close()
        replay_map = dict(replayed["vector"].statuses)
        reproduced = all(replay_map.get(tid) is after
                         for tid, _before, after in regressions)
        return {
            "added_count": len(added), "capped": False,
            "widened_baseline_regens_original_regressions": reproduced,
            "detail": (f"widened scope ran {len(widened)} tests "
                       f"({len(added)} new); original regressions "
                       + ("reproduced" if reproduced
                          else "did not reproduce")),
            "original_grade": grade,
        }

    def submit_test_for_visa(self, record: dict) -> dict:
        """补测签证（策略 4 数据面）：送进既有签证流水线，不开新路。

        复用 propose_test 的同一套装置——verify_candidate 三段验证、
        repair.isolated_worktree 隔离工作树、按 execution_mode 选执行
        器；用户树在 _source_context 的快照闸下只读（漂移即 fail-
        closed，与 SourceChangedStrategy 同一段比较）。
    """
        rt2, repo, _snapshot = self._manager._source_context(
            self._rt.review_id)
        proposal = record.get("test_proposal") or {}
        patch_text = str(proposal.get("patch") or "")
        if (not isinstance(proposal, dict) or not patch_text
                or not isinstance(proposal.get("patch_sha256"), str)
                or hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                != proposal["patch_sha256"]):
            raise IntakeError("REVERIFICATION_TEST_PATCH_INVALID",
                              "the reverification record carries no "
                              "intact test patch")
        try:
            candidate = validate_test_only_patch(patch_text)
        except RepairError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        directory = rt2.review_dir / "test-visa"
        directory.mkdir(parents=True, exist_ok=True)
        source_patch = run_git(["diff", "--binary", "HEAD"], cwd=repo.path,
                               check=True, timeout=30).stdout
        budget = TEST_VISA_BUDGET_SECONDS

        def visa_run_argv(argv, cwd, timeout, env=None):
            executor = (SandboxedExecutor(
                            Path(cwd).parent / "sandbox",
                            process_record=directory / "runtime_process.json")
                        if self._manager.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=directory / "runtime_process.json"))
            merged = {"PYTHONWARNINGS": "ignore"}
            merged.update({str(k): str(v) for k, v in (env or {}).items()})
            return executor.run([str(a) for a in argv], cwd=Path(cwd),
                                timeout=float(timeout),
                                env=sanitized_environment(merged))

        try:
            return verify_candidate(
                repo=repo.path, source_patch=source_patch,
                candidate_path=candidate["path"],
                candidate_code=candidate["code"],
                cited_files=[self._visa_cited_file(rt2, record)],
                py=repo.python, run_argv=visa_run_argv,
                scratch_root=directory / "worktrees", timeout=budget)
        except VisaError as exc:
            # 工具故障是观测结果不是内部错误：按 VISA_ERROR 如实落档，
            # 由策略原样透出扣留，绝不塌缩成"出了点问题"。
            return {"schema_version": "test-visa-v1",
                    "protocol_version": VISA_PROTOCOL_VERSION,
                    "status": "VISA_ERROR", "code": exc.code,
                    "error": exc.detail}

    def _visa_cited_file(self, rt: ReviewRuntime, record: dict) -> str:
        """签证引用文件 = 被质疑主张锚定的源文件；解析不出就拒绝。"""
        unit = _render_unit(rt.review_dir, str(record.get("claim_id") or ""))
        path = str((((unit or {}).get("location")) or {}).get("file") or "")
        if path:
            return path
        for item in (record.get("plan") or {}).get("replay_experiments") or []:
            path = str(((item.get("intervention") or {}).get("path")) or "")
            if path:
                return path
        raise IntakeError("REVERIFICATION_VISA_TARGET_UNKNOWN",
                          "cannot resolve which source file the "
                          "submitted test protects")


_LEASE_OVERHEAD_SECONDS = 600.0
_STANDING_MATCH_CONDITIONS = ("agent_level_l3", "spec_present",
                              "repo_whitelisted", "authorization_current",
                              "budget_within_authorization",
                              "lease_within_ceiling", "spec_isomorphic")
# 自主写入的预算计数依据是事件账本，不是内存。路由事件与候选事件同格：
# 一次派发无论落到哪个仪器，都占一次写入额度。
_SELF_WRITE_EVENT_KINDS = frozenset({"repair.candidate_proposed",
                                     "repair.candidate_verified",
                                     "autonomy.write_routed",
                                     "autonomy.task_adoption"})


def _ref_in_test_file(ref: str) -> bool:
    """覆盖缺口里的测试文件行：自主写入不把它们当仪器入口。

    与评测工具层（tools/run_abc_evaluation.py 的 _is_test_ref）同一条
    口径：补丁常常连带改测试，那些行留在缺口里只会把路由带偏。
    """
    rel = str(ref).rpartition(":")[0] or str(ref)
    path = PurePosixPath(rel)
    return bool({"tests", "test"} & set(path.parts)) or path.name.startswith("test_")


class ReviewManager:
    def __init__(self, registry: RepoRegistry, *, root: Path,
                 provider: OpenAICompatibleProvider | None = None,
                 agent_level: AgentLevel | str = AgentLevel.L1,
                 execution_mode: ExecutionMode | str = ExecutionMode.TRUSTED_LOCAL,
                 standing_authorization: StandingAuthorization | None = None):
        self.registry = registry
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.agent_level = AgentLevel(agent_level)
        self.execution_mode = ExecutionMode(execution_mode)
        # Human-issued, scoped pre-approval (or None for interactive-only
        # operation).  Loaded and validated once, before any review runs;
        # the match conditions are re-checked deterministically at every use.
        self.standing_authorization = standing_authorization
        # Loaded once, and loudly: a malformed registry must stop the server
        # rather than silently leave every capability ungated.
        self.capabilities = caps.load()
        if self.execution_mode not in {ExecutionMode.TRUSTED_LOCAL,
                                       ExecutionMode.SANDBOXED}:
            raise IntakeError("EXECUTION_MODE_INVALID", self.execution_mode.value)
        self._lock = threading.RLock()
        self._live_id: str | None = None
        self._runtimes: dict[str, ReviewRuntime] = {}
        self._idempotency: dict[str, dict] = self._load_idempotency()
        self._capability_authority = CapabilityAuthority()
        self._leases = RepositoryLeaseManager(self.root / "_leases")
        self._recover_interrupted()
        self._sweep_startup_edit_sessions()

    def _load_idempotency(self) -> dict[str, dict]:
        path = self.root / "idempotency.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            # A corrupt dedupe index must not make the server reuse an unknown
            # response.  Start empty; review state itself remains authoritative.
            return {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for key, value in raw.items():
            if (isinstance(key, str) and isinstance(value, dict)
                    and isinstance(value.get("payload_sha256"), str)
                    and isinstance(value.get("response"), dict)):
                out[key] = value
        return out

    def _remember_idempotency(self, key: str, payload: dict, response: dict) -> None:
        if not key:
            return
        self._idempotency[key] = {
            "payload_sha256": _payload_sha256(payload),
            "response": json.loads(json.dumps(response, ensure_ascii=False)),
        }
        _atomic_json(self.root / "idempotency.json", self._idempotency)

    def _idempotent_result(self, key: str, payload: dict) -> dict | None:
        key = _validate_idempotency_key(key)
        if not key:
            return None
        existing = self._idempotency.get(key)
        if existing is None:
            return None
        digest = _payload_sha256(payload)
        if existing.get("payload_sha256") != digest:
            raise IntakeError("IDEMPOTENCY_CONFLICT",
                              "同一 Idempotency-Key 不能复用不同请求")
        return json.loads(json.dumps(existing["response"], ensure_ascii=False))

    @staticmethod
    def _repo_fingerprint(repo: RegisteredRepo) -> str:
        # The canonical path is used only inside this process; persisted lease
        # records contain the one-way fingerprint, never the local path.
        return hashlib.sha256(str(repo.path).encode("utf-8")).hexdigest()

    @staticmethod
    def _transition_store(events: EventStore, state: ReviewStore,
                          status: ReviewStatus, *, reason: str = "",
                          **kwargs):
        """Publish durable intent before changing the materialized state."""
        previous = state.snapshot.status
        intent = events.append("review.state_transition_intent", {
            "from": previous.value, "to": status.value, "reason": reason,
        })
        changed = {
            "intent_event_id": intent.event_id, "from": previous.value,
            "to": status.value, "reason": reason,
        }
        if status in TERMINAL:
            # Terminal state is the public completion marker.  Publish every
            # journal record first so a caller that observes terminal state can
            # safely remove/export the review directory without racing a final
            # event append from the worker.
            events.append("review.state_changed", changed)
            snapshot = state.transition(status, reason=reason, **kwargs)
        else:
            snapshot = state.transition(status, reason=reason, **kwargs)
            events.append("review.state_changed", changed)
        return snapshot

    def _transition(self, rt: ReviewRuntime, status: ReviewStatus, *,
                    reason: str = "", **kwargs):
        return self._transition_store(rt.events, rt.state, status,
                                      reason=reason, **kwargs)

    def _bind_execution_authority(self, rt: ReviewRuntime) -> RegisteredRepo:
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        rt.repo_fingerprint = self._repo_fingerprint(repo)
        limits = DEFAULT_RESOURCE_LIMITS
        review_spec = rt.review_spec
        if review_spec is None and isinstance(request.get("review_spec"), dict):
            try:
                review_spec = ReviewSpec.parse(request["review_spec"])
            except ReviewSpecError:
                review_spec = None
        constraints = review_spec.constraints if review_spec is not None else None
        rt.resource_budget = {
            "max_seconds": float(constraints.budget_seconds if constraints else
                                 request.get("budget_seconds") or 300),
            "max_output_bytes": limits.max_output_bytes,
            "max_memory_bytes": (constraints.max_memory_bytes if constraints else
                                 limits.max_memory_bytes),
            "max_processes": (constraints.max_processes if constraints else
                              limits.max_processes),
            "max_disk_bytes": (constraints.max_disk_bytes if constraints else
                               limits.max_disk_bytes),
            "max_files": limits.max_files,
        }
        plan_hash = (rt.frozen_plan.plan_sha256 if rt.frozen_plan is not None
                     else str(_read_json(rt.review_dir / "plan.frozen.json").get(
                         "plan_sha256") or ""))
        ttl = int(self._lease_ttl_seconds(rt.resource_budget["max_seconds"]))
        rt.target_commit = str((request.get("source_snapshot") or {}).get("head") or "")
        data_policy = review_spec.as_dict()["data_policy"] if review_spec is not None else {}
        rt.data_policy_sha256 = hashlib.sha256(json.dumps(
            data_policy, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest()
        allowed_tools = (review_spec.autonomy_policy.allowed_tools
                         if review_spec is not None and
                         review_spec.autonomy_policy.allowed_tools else
                         tuple(item.name for item in CATALOG_V1))
        rt.capability_token = self._capability_authority.issue(
            review_id=rt.review_id, plan_sha256=plan_hash,
            repo_fingerprint=rt.repo_fingerprint,
            allowed_tools=tuple(allowed_tools),
            max_risk=ToolRisk.MUTATES,
            resource_budget=rt.resource_budget, ttl_seconds=ttl,
            target_commit=rt.target_commit,
            data_policy_sha256=rt.data_policy_sha256)
        return repo

    @staticmethod
    def _bounded_facts(result) -> dict:
        """Return structural facts only; never copy raw stdout or source text."""
        if hasattr(result, "anchor_id") and hasattr(result, "status"):
            return {
                "anchor_id": str(result.anchor_id), "status": str(result.status),
                "identical_to_baseline": result.identical_to_baseline,
                "regressed_test_count": len(result.regressed_tests),
                "evidence_id": str(result.evidence_id),
            }
        if not isinstance(result, dict):
            return {"result_type": type(result).__name__}
        facts: dict = {"result_keys": sorted(str(k) for k in result)[:40]}
        for key, value in result.items():
            if isinstance(value, bool) or value is None:
                facts[str(key)] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                facts[str(key)] = value
            elif isinstance(value, (list, tuple, dict, set)):
                facts[f"{key}_count"] = len(value)
        return facts

    @staticmethod
    def _artifact_records(rt: ReviewRuntime, result) -> tuple[dict, ...]:
        if not isinstance(result, dict):
            return ()
        records: list[dict] = []
        for key, value in result.items():
            if not isinstance(value, str):
                continue
            try:
                path = Path(value).resolve(strict=True)
                relative = path.relative_to(rt.review_dir.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            records.append({
                "artifact_id": str(key)[:80], "relative_ref": relative,
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            })
        return tuple(records[:20])

    def _call_tool(self, rt: ReviewRuntime, name: str, args: dict | None = None,
                   *, reason_code: str, reason: str,
                   expected_artifacts: tuple[str, ...] = (),
                   idempotency_key: str = ""):
        """The sole post-approval tool path: action event, dispatch, observation."""
        if not rt.capability_token or not rt.repo_fingerprint:
            raise ProtocolError("EXECUTION_AUTHORITY_MISSING", name)
        catalog = ToolCatalog()
        spec = catalog.get(name)
        plan_hash = (rt.frozen_plan.plan_sha256 if rt.frozen_plan is not None
                     else str(_read_json(rt.review_dir / "plan.frozen.json").get(
                         "plan_sha256") or ""))
        dedupe_key = idempotency_key or hashlib.sha256(json.dumps({
            "review_id": rt.review_id, "plan_sha256": plan_hash,
            "tool": name, "args": dict(args or {})}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        receipt = rt.action_receipts.get(dedupe_key)
        if receipt is not None:
            if receipt.get("status") == "COMPLETED":
                rt.events.append("agent.action.deduplicated", {
                    "idempotency_key_sha256": hashlib.sha256(
                        dedupe_key.encode("utf-8")).hexdigest(),
                    "action_id": receipt.get("action_id")})
                return _decode_action_result(receipt.get("result"))
            raise ProtocolError("ACTION_OUTCOME_UNCERTAIN",
                                "an unfinished action with this idempotency key exists")
        rt.action_seq += 1
        action_id = f"action:{rt.action_seq:06d}"
        action = ActionRequest(
            review_id=rt.review_id, action_id=action_id, tool=name,
            args=dict(args or {}), risk=spec.risk, plan_sha256=plan_hash,
            prerequisite_observation_ids=tuple(rt.observation_ids[-8:]),
            reason_code=reason_code, reason=reason,
            idempotency_key=f"{rt.review_id}:{dedupe_key[:24]}",
            timeout_seconds=float(rt.resource_budget["max_seconds"]),
            resource_budget=dict(rt.resource_budget),
            expected_artifacts=expected_artifacts,
            capability_token=rt.capability_token,
            target_commit=rt.target_commit,
            data_policy_sha256=rt.data_policy_sha256)
        action.validate(catalog)
        rt.action_receipts[dedupe_key] = {
            "schema_version": "action-receipt-v1", "status": "STARTED",
            "action_id": action_id, "tool": name, "plan_sha256": plan_hash,
            "args_sha256": hashlib.sha256(json.dumps(
                dict(args or {}), ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
        }
        _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
        rt.events.append("agent.action.accepted", action.as_dict(redact_capability=True))
        started = time.monotonic()
        observation_id = f"observation:{rt.action_seq:06d}"
        try:
            result = rt.router.execute_controlled(
                action, self._capability_authority,
                repo_fingerprint=rt.repo_fingerprint)
        except Exception as exc:
            code = getattr(exc, "code", getattr(exc, "stage", type(exc).__name__))
            observation = ObservationRecord(
                review_id=rt.review_id, observation_id=observation_id,
                action_id=action_id, status=ObservationStatus.FAILED,
                facts={"exception_type": type(exc).__name__}, artifacts=(),
                resource_usage={
                    "wall_seconds": round(time.monotonic() - started, 6),
                    "limits": dict(rt.resource_budget)},
                executor={"mode": self.execution_mode.value,
                          "control_plane": "trusted"},
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                restore_proof={"required": spec.risk.value == "mutates",
                               "verified": False},
                retryable=code in {"TimeoutExpired", "timeout", "budget_exhausted"},
                next_step_constraints=("do_not_retry_without_policy_review",),
                error_code=str(code)[:120])
            rt.events.append("agent.observation.recorded", {
                **observation.as_dict(), "observation_sha256": observation.sha256})
            rt.observation_ids.append(observation_id)
            rt.action_receipts[dedupe_key].update(
                status="FAILED", observation=observation.as_dict(),
                error_code=str(code)[:120])
            _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
            raise
        recent = rt.events.since(max(0, rt.events.last_seq - 6))
        restored = any(event.kind == "restore.verified" for event in recent)
        observation = ObservationRecord(
            review_id=rt.review_id, observation_id=observation_id,
            action_id=action_id, status=ObservationStatus.SUCCEEDED,
            facts=self._bounded_facts(result),
            artifacts=self._artifact_records(rt, result),
            resource_usage={
                "wall_seconds": round(time.monotonic() - started, 6),
                "limits": dict(rt.resource_budget)},
            executor={"mode": self.execution_mode.value,
                      "control_plane": "trusted"},
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            restore_proof={"required": spec.risk.value == "mutates",
                           "verified": restored if spec.risk.value == "mutates" else True},
            retryable=False, next_step_constraints=())
        rt.events.append("agent.observation.recorded", {
            **observation.as_dict(), "observation_sha256": observation.sha256})
        rt.observation_ids.append(observation_id)
        rt.action_receipts[dedupe_key].update(
            status="COMPLETED", observation=observation.as_dict(),
            result=_encode_action_result(result))
        _atomic_json(rt.review_dir / "action_receipts.json", rt.action_receipts)
        return result

    def _release_execution_lease(self, rt: ReviewRuntime) -> None:
        if rt.lease is None:
            return
        lease = rt.lease
        try:
            released = self._leases.release(lease)
            rt.events.append("repository.lease_released", {
                "operation": "execute", "lease_id": lease.lease_id,
                "released": released})
        finally:
            rt.lease = None

    def _recover_interrupted(self) -> None:
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or not (d / "review_state.json").exists():
                continue
            try:
                state = ReviewStore(d, d.name)
                try:
                    events = EventStore(d, d.name)
                    process_results = {
                        "execution": recover_process_record(d / "runtime_process.json"),
                        "repair": recover_process_record(d / "repair" / "runtime_process.json"),
                    }
                    for operation, process_recovery in process_results.items():
                        if process_recovery == "none":
                            continue
                        events.append("executor.process_recovered", {
                            "operation": operation, "result": process_recovery})
                    all_events = events.all()
                    intents = {event.event_id for event in all_events
                               if event.kind == "review.state_transition_intent"}
                    completed = {str(event.data.get("intent_event_id") or "")
                                 for event in all_events
                                 if event.kind == "review.state_changed"}
                    unresolved_intent = bool(intents - completed)
                    temp_artifacts = tuple(d.rglob(".*.tmp"))
                    orphan_worktrees = tuple(
                        path for path in (d / "repair" / "worktrees").glob("shuimu-repair-*")
                        if path.is_dir())
                    _, uncertain_lease = self._leases.reclaim_stale_for_review(d.name)
                    process_uncertain = "uncertain" in process_results.values()
                    receipts = _read_json(d / "action_receipts.json")
                    if not isinstance(receipts, dict):
                        receipts = {}
                    uncertain_actions = [key for key, value in receipts.items()
                                         if isinstance(value, dict) and
                                         value.get("status") == "STARTED"]
                    if (unresolved_intent or temp_artifacts or orphan_worktrees or uncertain_lease
                            or process_uncertain or uncertain_actions):
                        reason = ("recovery_unmatched_transition" if unresolved_intent
                                  else "recovery_temporary_artifact"
                                  if temp_artifacts else "recovery_orphan_worktree"
                                  if orphan_worktrees else "recovery_lease_owner_uncertain"
                                  if uncertain_lease else "recovery_process_uncertain"
                                  if process_uncertain else "recovery_action_outcome_uncertain")
                        events.append("review.recovered", {
                            "previous_state": state.snapshot.status.value,
                            "resolution": "QUARANTINED", "reason": reason,
                            "temporary_artifact_count": len(temp_artifacts),
                            "orphan_worktree_count": len(orphan_worktrees),
                            "uncertain_action_count": len(uncertain_actions),
                        })
                        if state.snapshot.status is not ReviewStatus.QUARANTINED:
                            self._transition_store(events, state,
                                                   ReviewStatus.QUARANTINED,
                                                   reason=reason,
                                                   allow_recovery_terminal=True)
                        continue
                    if state.snapshot.status in TERMINAL:
                        continue
                    events.append("review.recovered", {
                        "previous_state": state.snapshot.status.value,
                        "resolution": "ABORTED",
                    })
                    if state.snapshot.status is not ReviewStatus.RECOVERING:
                        self._transition_store(events, state, ReviewStatus.RECOVERING,
                                               reason="process_restart")
                    self._transition_store(
                        events, state, ReviewStatus.ABORTED,
                        reason="review_aborted:process_restart")
                    for status_file in (d / "artifacts").glob("*/run_status.json"):
                        evidence = RunRoot(status_file.parent, official=False,
                                           unofficial_reason="review_evidence")
                        if evidence.status().get("status") == "INCOMPLETE":
                            evidence.finish(
                                False, reason="review_aborted:process_restart")
                except EventJournalError:
                    state.transition(
                        ReviewStatus.QUARANTINED, reason="event_journal_invalid",
                        allow_recovery_terminal=True)
                    for status_file in (d / "artifacts").glob("*/run_status.json"):
                        evidence = RunRoot(status_file.parent, official=False,
                                           unofficial_reason="review_evidence")
                        if evidence.status().get("status") == "INCOMPLETE":
                            evidence.finish(False, reason="event_journal_invalid")
            except ReviewStateError:
                continue

    def _runtime(self, review_id: str) -> ReviewRuntime:
        if review_id in self._runtimes:
            return self._runtimes[review_id]
        d = self.root / review_id
        if not d.is_dir():
            raise IntakeError("REVIEW_NOT_FOUND", review_id)
        rt = ReviewRuntime(review_id, d)
        self._runtimes[review_id] = rt
        return rt

    @property
    def live_id(self) -> str | None:
        with self._lock:
            return self._live_id

    def create_v2(self, raw: dict, *, idempotency_key: str = "") -> dict:
        """Compile a closed natural-language task into the proven v1 executor."""
        with self._lock:
            cached = self._idempotent_result(idempotency_key, raw)
            if cached is not None:
                return cached
        try:
            spec = ReviewSpec.parse(raw)
        except ReviewSpecError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if spec.language == "typescript":
            raise IntakeError("LANGUAGE_NOT_AVAILABLE",
                              "TypeScript adapters are not enabled in this build")
        repo = self.registry.get(spec.source.repo_id)
        source_snapshot = _repo_snapshot(repo.path)
        if not source_snapshot.get("head") or not source_snapshot.get("status_sha256"):
            raise IntakeError("SOURCE_SNAPSHOT_UNAVAILABLE",
                              "cannot bind the repository identity and workspace snapshot")
        target_ref = spec.source.target_ref or "HEAD"
        try:
            target_commit = run_git(["rev-parse", "--verify", f"{target_ref}^{{commit}}"],
                                    cwd=repo.path, check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntakeError("SOURCE_TARGET_INVALID", target_ref) from exc
        if spec.source.target_commit and spec.source.target_commit != target_commit:
            raise IntakeError("SOURCE_TARGET_STALE",
                              "target_commit does not match the selected target_ref")
        if target_commit != source_snapshot["head"]:
            raise IntakeError("SOURCE_TARGET_REQUIRES_HUMAN",
                              "the current evidence engine requires target_ref to match HEAD")
        base_ref = spec.source.base_ref or "HEAD"
        try:
            base_ref = run_git(["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
                               cwd=repo.path, check=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntakeError("SOURCE_BASE_INVALID", base_ref) from exc
        spec = replace(
            spec,
            source=replace(spec.source, base_ref=base_ref, target_ref=target_ref,
                           target_commit=target_commit,
                           workspace_snapshot_sha256=source_snapshot["snapshot_sha256"]),
            created_at=(datetime.now(timezone.utc).isoformat()
                        if spec.created_at == DEFAULT_CREATED_AT else spec.created_at),
        )
        try:
            memory = load_review_memory(repo.path)
        except ReviewMemoryError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if spec.memory_snapshot_sha256 and spec.memory_snapshot_sha256 != memory.sha256:
            raise IntakeError("MEMORY_SNAPSHOT_STALE",
                              "review spec memory hash does not match the repository")
        if memory.sha256:
            spec = replace(spec, memory_snapshot_sha256=memory.sha256)
        tests = (self.registry.validate_test_files(repo, list(spec.scope.test_files))
                 if spec.scope.test_files else self.registry.discover_python_test_files(repo))
        created = self.create({
            "source": {"kind": "local", "repo_id": spec.source.repo_id},
            "test_files": list(tests), "declared_tests": [], "goal": spec.goal,
            "review_focus": spec.review_focus,
            "budget_seconds": spec.constraints.budget_seconds,
            "model_provider": spec.autonomy_policy.model_provider,
            "ui_mode": ("model_research" if spec.autonomy_policy.model_provider == "live"
                        else "research"),
            "probe_strategy": spec.probe_strategy,
        }, review_spec=spec, idempotency_key=idempotency_key)
        response = self.describe(created["review_id"])
        with self._lock:
            self._remember_idempotency(idempotency_key, raw, response)
        return response

    def revise_v2(self, review_id: str, raw: dict) -> dict:
        """Supersede an unapproved v2 draft with a hash-linked new draft.

        A revision never mutates a plan that a user might have copied.  The old
        draft becomes a terminal audit record and the revised task receives a
        fresh review id and a fresh frozen-plan fingerprint.
        """
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_APPROVAL:
            raise IntakeError("PLAN_REVISION_NOT_ALLOWED", rt.state.snapshot.status.value)
        current = rt.review_spec
        if current is None:
            request = rt.request or _read_json(rt.review_dir / "request.json")
            spec_raw = request.get("review_spec") if isinstance(request, dict) else None
            try:
                current = ReviewSpec.parse(spec_raw)
            except ReviewSpecError as exc:
                raise IntakeError("PLAN_REVISION_NOT_ALLOWED", "review has no valid v2 spec") from exc
        if not isinstance(raw, dict) or set(raw) - {
                "instruction", "goal", "scope", "constraints", "autonomy_policy",
                "language", "review_focus", "revision_reason",
                "trigger_observation_id"}:
            raise IntakeError("PLAN_REVISION_INVALID", "unsupported revision fields")
        reason = str(raw.get("revision_reason") or "user_revision")
        trigger = str(raw.get("trigger_observation_id") or "")
        if len(reason) > 300 or len(trigger) > 160:
            raise IntakeError("PLAN_REVISION_INVALID", "revision metadata is too long")
        merged = current.as_dict()
        merged.pop("schema_version", None)
        merged = _merge_spec_patch(
            merged,
            {key: value for key, value in raw.items()
             if key not in {"revision_reason", "trigger_observation_id"}})
        try:
            revised = ReviewSpec.parse(merged)
        except ReviewSpecError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        if revised.sha256 == current.sha256:
            raise IntakeError("PLAN_REVISION_NO_CHANGE", "revision does not change the review spec")
        rt.events.append("plan.superseded", {
            "review_spec_sha256": current.sha256,
            "replacement_review_spec_sha256": revised.sha256,
            "changed_fields": sorted(key for key in merged
                                     if merged.get(key) != current.as_dict().get(key)),
            "revision_reason": reason,
            "trigger_observation_id": trigger,
        })
        self._transition(rt, ReviewStatus.ABORTED, reason="plan_superseded")
        # The plan drifted and the run was aborted, so the replacement has to be
        # approved again rather than inheriting the old approval. Recorded for
        # the comprehension probe, which otherwise has nothing to observe: it
        # scores machine codes, and a correct refusal deliberately carries none.
        drift = material_changes(current, revised)
        emit_safe(rt.events.append, "approval.reexecuted_after_plan_drift", {
            "review_id": review_id,
            "review_spec_sha256": current.sha256,
            "replacement_review_spec_sha256": revised.sha256,
            "material_changes": list(drift),
        })
        widened = sorted(set(drift) & SCOPE_WIDENING_CHANGES)
        if widened:
            # The revision widened what the review may touch, reach or send,
            # and the run was aborted rather than carried on under the old
            # approval. Classified from material_changes, never from the
            # model's free-text reason.
            emit_safe(rt.events.append, "scope.expansion_cancelled", {
                "review_id": review_id,
                "widened": widened,
                "review_spec_sha256": current.sha256,
            })
        with self._lock:
            if self._live_id == review_id:
                self._live_id = None
        created = self.create_v2(revised.as_dict())
        child = self._runtime(created["review_id"])
        child.request["plan_revision"] = {
            "parent_review_id": review_id,
            "parent_review_spec_sha256": current.sha256,
            "revision_reason": reason,
            "trigger_observation_id": trigger,
        }
        _atomic_json(child.review_dir / "request.json", child.request)
        child.events.append("plan.revised", dict(child.request["plan_revision"]))
        return created

    def plan_revisions(self, review_id: str) -> dict:
        """Return the immutable parent chain without exposing local paths."""
        chain: list[dict] = []
        current_id = review_id
        seen: set[str] = set()
        while current_id and current_id not in seen:
            seen.add(current_id)
            rt = self._runtime(current_id)
            request = rt.request or _read_json(rt.review_dir / "request.json")
            parent = request.get("plan_revision") or {}
            chain.append({
                "review_id": current_id,
                "review_spec_sha256": str(request.get("review_spec_sha256") or ""),
                "status": rt.state.snapshot.status.value,
                # 每一稿**为什么**被改，就写在它自己的 plan_revision 里。不透出来
                # 的话，界面只能显示「第几稿」，而那恰恰是最没用的一半信息。
                # 第一稿没有父链，理由为空——不是缺字段，是它本来就没被谁改出来。
                "revision_reason": str(parent.get("revision_reason") or ""),
            })
            current_id = str(parent.get("parent_review_id") or "")
        return {"schema_version": "review-plan-revisions-v1", "revisions": list(reversed(chain))}

    def create(self, raw: dict, *, review_spec: ReviewSpec | None = None,
               idempotency_key: str = "") -> dict:
        with self._lock:
            cached = self._idempotent_result(idempotency_key, raw)
            if cached is not None:
                return cached
            if self._live_id:
                raise IntakeError("LIVE_RUN_BUSY", self._live_id)
            if set(raw) - {"source", "test_files", "declared_tests", "goal",
                          "review_focus", "budget_seconds", "model_provider", "probe_strategy",
                          "ui_mode"}:
                raise IntakeError("REQUEST_FIELDS_INVALID", "unsupported request field")
            source = raw.get("source") or {}
            if not isinstance(source, dict) or set(source) != {"kind", "repo_id"}:
                raise IntakeError("SOURCE_INVALID", "local source requires only kind and repo_id")
            if source.get("kind") != "local":
                raise IntakeError("SOURCE_NOT_REGISTERED", "HTTP live reviews currently accept registered local repos only")
            repo = self.registry.get(str(source.get("repo_id") or ""))
            tests = self.registry.validate_test_files(repo, raw.get("test_files") or [])
            declared = raw.get("declared_tests") or []
            if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
                raise IntakeError("DECLARED_TESTS_INVALID", "declared_tests must be strings")
            budget = float(raw.get("budget_seconds") or 300)
            if not (1 <= budget <= 3600):
                raise IntakeError("BUDGET_INVALID", "budget must be between 1 and 3600 seconds")
            goal = str(raw.get("goal") or "审查补丁新增代码的证据边界")[:500]
            review_focus = str(raw.get("review_focus") or "evidence-boundary")
            if review_focus not in REVIEW_FOCUSES:
                raise IntakeError("REVIEW_FOCUS_INVALID", review_focus)
            provider_name = str(raw.get("model_provider") or "deterministic")
            if provider_name not in {"deterministic", "live"}:
                raise IntakeError("PROVIDER_INVALID", provider_name)
            ui_mode = str(raw.get("ui_mode") or (
                "model_research" if provider_name == "live" else "research"))
            if ui_mode not in {"judge", "research", "model_research"}:
                raise IntakeError("UI_MODE_INVALID", ui_mode)
            if ui_mode != "model_research" and provider_name == "live":
                raise IntakeError(
                    "MODEL_MODE_MISMATCH",
                    "评委和普通研究员模式固定使用确定性调度；请切换到 AI 研究员模式",
                )
            if provider_name == "live" and self.provider is None:
                raise IntakeError("MODEL_UNAVAILABLE", "AI 研究员需要已配置且可用的模型服务")
            # v1 兼容层只负责映射：judge/research → standard，
            # model_research → agent；v2 走 review_spec 的显式声明。
            # 映射之后照样过同一条产品模式闸门——旧入口不是绕过口。
            if review_spec is not None and getattr(review_spec, "product_mode", ""):
                product_mode = review_spec.product_mode
            else:
                product_mode = ("agent" if ui_mode == "model_research"
                                else "standard")
            if PRODUCT_MODE_PROVIDER[product_mode] != provider_name:
                raise IntakeError(
                    "PRODUCT_MODE_PROVIDER_MISMATCH",
                    f"{product_mode} 审查必须使用 "
                    f"{PRODUCT_MODE_PROVIDER[product_mode]} 模型提供方")
            # ddmin is opt-in and costs extra experiments, so the default stays
            # hdd_inspired and an unknown value is refused rather than coerced.
            strategy = str(raw.get("probe_strategy")
                           or ProbeStrategy.HDD_INSPIRED.value)
            if strategy not in {x.value for x in ProbeStrategy}:
                raise IntakeError("PROBE_STRATEGY_INVALID", strategy)
            # A capability the registry has switched off must not be reachable
            # from the browser. Otherwise "disabled" is a word in a document
            # rather than a property of the product.
            capability = _STRATEGY_CAPABILITY.get(strategy)
            if capability and not caps.runtime_allowed(
                    capability, capabilities=self.capabilities):
                raise IntakeError("CAPABILITY_UNAVAILABLE", capability)
            # The repository scan is deliberately last among pure request
            # validation steps: malformed/unauthorized requests must be
            # rejected by their own stable boundary before repository state is
            # inspected.  It still runs before a review id, event, worktree or
            # repository-controlled process can be created.
            try:
                repository_security = require_repository_safe(repo.path)
            except RepositorySecurityError as exc:
                raise IntakeError(exc.code, exc.detail) from exc

            review_id = uuid.uuid4().hex
            review_dir = self.root / review_id
            rt = ReviewRuntime(review_id, review_dir)
            if self.provider is not None:
                rt.metrics_base = self.provider.metrics.counters()
                rt.trace_start = len(self.provider.private_traces)
            self._runtimes[review_id] = rt
            self._live_id = review_id
            public_request = {
                "schema_version": "review-request-v1", "review_id": review_id,
                "source": source, "test_files": list(tests),
                "declared_tests": declared, "goal": goal,
                "review_focus": review_focus,
                "budget_seconds": budget, "model_provider": provider_name,
                "ui_mode": ui_mode,
                "product_mode": product_mode,
                "probe_strategy": strategy,
                "execution_mode": self.execution_mode.value,
                "isolation_mode": self.execution_mode.value,
                "agent_level": self.agent_level.value,
                "source_snapshot": _repo_snapshot(repo.path),
                "repository_security": repository_security.as_dict(),
            }
            if review_spec is not None:
                public_request["schema_version"] = "review-request-v2"
                public_request["review_spec"] = review_spec.as_dict()
                public_request["review_spec_sha256"] = review_spec.sha256
            rt.request = public_request
            rt.review_spec = review_spec
            _atomic_json(review_dir / "request.json", public_request)
            rt.events.append("review.created", {
                # 见证词表用 v2 的语义：execution_mode 只说 live/replay，
                # 隔离方式单独走 isolation_mode。老版本把 trusted_local
                # 写进 execution_mode，交叉校验因此永远匹配不上。
                "execution_mode": execution_mode_for(self.execution_mode.value),
                "repo_id": repo.repo_id,
                # 模式旁证独立于请求字段落盘：发布前交叉验证不能只相信
                # request 里的声明，得看运行自己的账本怎么说。
                "product_mode": product_mode,
                "model_provider": provider_name,
                # L1 也让模型做 continue/stop 的调度决策（_next_action 不分
                # 级别），所以只要 provider 是 live，见证就必须写 model；
                # 写确定性会与调度阶段的模型请求事件自相矛盾。
                "scheduler_mode": ("model" if provider_name == "live"
                                   else DEFAULT_DETERMINISTIC_SCHEDULER),
                "isolation_mode": self.execution_mode.value,
            })
            if review_spec is not None:
                rt.events.append("review_spec.compiled", {
                    "schema_version": review_spec.schema_version,
                    "review_spec_sha256": review_spec.sha256,
                    "language": review_spec.language,
                    "autonomy": review_spec.autonomy_policy.model_provider,
                    "repair_branch_authorized": review_spec.autonomy_policy.allow_repair_branch,
                })
            self._transition(rt, ReviewStatus.INTAKE_VALIDATED)
            rt.events.append("intake.validated", {"test_files": list(tests)})
            # 已确认的仓库记忆在 intake 时一并读出（v1/v2 同一入口）：
            # 评委模式的 preset 走 v1，同样要能看到记忆已生效。记忆是
            # 上下文不是授权；装载失败按输入错误拒绝，绝不静默跳过。
            try:
                memory = load_review_memory(repo.path)
            except ReviewMemoryError as exc:
                raise IntakeError(exc.code, exc.detail) from exc
            if memory.records:
                rt.request["review_memory"] = memory.active_rules()
                _atomic_json(review_dir / "request.json", rt.request)
                rt.events.append("review_memory.loaded", {
                    "memory_snapshot_sha256": memory.sha256,
                    "active_records": len(memory.active_rules()),
                })
            rt.events.append("repository_security.accepted", {
                "schema_version": repository_security.as_dict()["schema_version"],
                "findings": len(repository_security.findings),
                "observations": len(tuple(
                    item for item in repository_security.findings
                    if item.severity == "observe")),
            })

            req = AnalysisRequest(
                repo_path=repo.path, test_files=tests,
                base_commit=(review_spec.source.base_ref if review_spec else ""),
                declared_tests=tuple(declared), python=repo.python,
                budget_seconds=budget, mode=self.execution_mode,
                goal=goal, model_provider=provider_name,
                out_root=review_dir / "artifacts", quiet=True)
            try:
                resolved = req.resolve()
                rt.session = AnalysisSession(
                    resolved, quiet=True, three_state=True,
                    out_root=req.out_root, total_budget=budget,
                    probe_strategy=strategy,
                    run_metadata={
                        "execution_mode": req.mode.value,
                        "execution_mode_label": req.mode.label,
                        "isolation_mode": req.mode.value,
                        "input_kind": resolved.kind,
                        "review_id": review_id,
                        "planner_prompt_version": "planner-v1",
                        "stop_policy_version": ("scheduler-policy-v3"
                                                if self.agent_level in
                                                {AgentLevel.L2, AgentLevel.L3}
                                                else "stop-policy-v1"),
                    }, event_sink=rt.events.append)
                rt.router = ToolRouter(rt.session, ToolCatalog())
                rt.router.call("inspect_repo")
                rt.router.call("inspect_patch")
                universe = tuple(rt.router.call("build_eligible_universe")["anchor_ids"])
                draft, fallback = self._draft_plan(rt, provider_name, goal, budget,
                                                   universe)
                try:
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget,
                                          review_spec_sha256=(review_spec.sha256
                                                              if review_spec else ""))
                except PolicyError as exc:
                    if self.provider:
                        self.provider.metrics.policy_rejections += 1
                        reasons = self.provider.metrics.policy_rejection_reasons
                        reasons[exc.code] = reasons.get(exc.code, 0) + 1
                    rt.events.append("policy.rejected", {"code": exc.code})
                    draft = deterministic_draft(goal=goal, budget_seconds=budget,
                                                universe=universe)
                    frozen = compile_plan(draft, universe=universe,
                                          user_budget_seconds=budget,
                                          review_spec_sha256=(review_spec.sha256
                                                              if review_spec else ""))
                    fallback = f"policy_rejected:{exc.code}"
                rt.frozen_plan = frozen
                _atomic_json(review_dir / "plan.draft.json", draft.as_dict())
                self._transition(rt, ReviewStatus.PLAN_DRAFTED)
                rt.events.append("plan.drafted", {
                    "prompt_version": draft.prompt_version,
                    "fallback_reason": fallback,
                })
                self._transition(rt, ReviewStatus.AWAITING_APPROVAL)
                rt.events.append("plan.awaiting_approval", {
                    "plan_sha256": frozen.plan_sha256,
                    "plan": frozen.as_dict(),
                })
                self._maybe_standing_approve(rt, frozen)
                response = self.describe(review_id)
                self._remember_idempotency(idempotency_key, raw, response)
                return response
            except Exception as exc:
                self._fail(rt, exc)
                raise

    def _gate_model_path(self, rt: ReviewRuntime, stage: str) -> None:
        """标准审查的第二层门卫：进入任何模型调用路径之前终止。

        intake 已拒绝过一次 provider 不匹配；这里防的是 intake 之后、
        运行期间的状态漂移——request 被改写、provider 被替换都过不去。
        事件先落盘再抛错，审计里留下的是"想调但被拦下"，不是静默降级。
        """
        if str(rt.request.get("product_mode") or "standard") != "standard":
            return
        rt.events.append("standard_mode.model_path_blocked", {"stage": stage})
        _gate_model_path_for_mode("standard", stage)

    def _draft_plan(self, rt: ReviewRuntime, provider_name: str, goal: str,
                    budget: float, universe: tuple[str, ...]):
        if provider_name != "live" or self.provider is None:
            reason = "provider_unconfigured" if provider_name == "live" else ""
            if reason and self.provider:
                self.provider.metrics.fallbacks += 1
            return deterministic_draft(goal=goal, budget_seconds=budget,
                                       universe=universe), reason
        self._gate_model_path(rt, "plan")
        last = None
        for _ in range(2):
            rt.events.append("model.request.started", {"stage": "plan"})
            try:
                draft = self.provider.draft_plan(
                    goal=goal, budget_seconds=budget, universe=universe)
                rt.events.append("model.request.completed",
                                 {"stage": "plan", "ok": True})
                return draft, ""
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "plan", "ok": False,
                    "error_type": type(exc).__name__})
                last = exc
        self.provider.metrics.fallbacks += 1
        return deterministic_draft(goal=goal, budget_seconds=budget,
                                   universe=universe), f"model_failed:{type(last).__name__}"

    def approve(self, review_id: str, plan_sha256: str, *,
                idempotency_key: str = "") -> dict:
        payload = {"review_id": review_id, "plan_sha256": plan_sha256,
                   "action": "approve"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_APPROVAL:
            raise IntakeError("REVIEW_NOT_APPROVABLE", rt.state.snapshot.status.value)
        if rt.frozen_plan is None:
            raw = json.loads((rt.review_dir / "plan.draft.json").read_text(encoding="utf-8"))
            raise IntakeError("REVIEW_NOT_RESUMABLE", "restart requires a new review")
        if not secrets.compare_digest(plan_sha256, rt.frozen_plan.plan_sha256):
            raise IntakeError("STALE_PLAN", "approved plan hash is not current")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo_for_snapshot = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo_for_snapshot.path)
        if (current_snapshot.get("head") != expected_snapshot.get("head") or
                current_snapshot.get("snapshot_sha256") != expected_snapshot.get("snapshot_sha256")):
            raise IntakeError("STALE_APPROVAL",
                              "HEAD or workspace fingerprint changed after plan drafting")
        repo = self._bind_execution_authority(rt)
        try:
            rt.lease = self._leases.acquire(
                repo_fingerprint=rt.repo_fingerprint, review_id=review_id,
                operation="execute", plan_sha256=plan_sha256,
                ttl_seconds=self._lease_ttl_seconds(
                    rt.resource_budget["max_seconds"]))
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        _atomic_json(rt.review_dir / "plan.frozen.json", rt.frozen_plan.as_dict())
        rt.events.append("plan.approved", {
            "plan_sha256": plan_sha256,
            "repo_fingerprint": self._repo_fingerprint(repo),
            "lease_id": rt.lease.lease_id,
            "capability_token_sha256": hashlib.sha256(
                rt.capability_token.encode("utf-8")).hexdigest(),
            "target_commit": current_snapshot.get("head"),
            "workspace_snapshot_sha256": current_snapshot.get("snapshot_sha256"),
            "approval_expires_at_epoch": int(time.time()) + int(
                self._lease_ttl_seconds(rt.resource_budget["max_seconds"])),
        })
        try:
            self._transition(rt, ReviewStatus.PLAN_FROZEN)
        except Exception:
            self._leases.release(rt.lease)
            rt.lease = None
            raise
        thread = threading.Thread(target=self._execute, args=(rt,), daemon=True,
                                  name=f"modou-review-{review_id[:8]}")
        rt.thread = thread
        try:
            thread.start()
        except Exception:
            if rt.lease is not None:
                self._leases.release(rt.lease)
                rt.lease = None
            raise
        response = self.describe(review_id)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def _lease_ttl_seconds(self, budget_seconds: float) -> float:
        """Deterministic lease TTL: budget plus fixed teardown overhead.

        The 7200s ceiling inside RepositoryLeaseManager.acquire is a safety
        property this server deliberately does not raise.  A plan whose lease
        would be clamped below the run it must cover is refused by the
        standing-authorization match instead of silently under-leased.
        """
        return float(budget_seconds) + _LEASE_OVERHEAD_SECONDS

    def _review_spec(self, rt: "ReviewRuntime") -> ReviewSpec | None:
        """In-memory spec, or the durable one re-parsed from the request.

        进程重启会丢掉内存里的 spec，而 request.json 一直带着它。每个查
        spec 的闸都必须仍然看到那份被授权的契约——否则重启之后同一条
        授权路径会以「未授权」拒绝自己，长跑中途重启就再也走不动。
        """
        if rt.review_spec is not None:
            return rt.review_spec
        request = rt.request or _read_json(rt.review_dir / "request.json")
        raw = request.get("review_spec") if isinstance(request, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            return ReviewSpec.parse(raw)
        except ReviewSpecError:
            return None

    def _standing_authorization_match(
            self, rt: "ReviewRuntime") -> tuple[dict | None, str]:
        """Decide deterministically whether the standing authorization covers this review.

        Returns (matched_conditions, "") on a full match and (None, reason)
        otherwise.  The seven material-change classes in
        escalation.material_changes remain the authority for "did the plan's
        contract change"; any pinned axis drifting returns to a human.
        """
        auth = self.standing_authorization
        if auth is None:
            return None, "not_configured"
        if self.agent_level is not AgentLevel.L3:
            return None, "agent_level_not_l3"
        spec = self._review_spec(rt)
        if spec is None:
            return None, "spec_required"
        request = rt.request or _read_json(rt.review_dir / "request.json")
        try:
            repo = self._resolve_review_repo(request)
        except IntakeError as exc:
            return None, f"repo_not_resolvable:{exc.code}"
        if not auth.covers_repo(repo.path):
            return None, "repo_not_whitelisted"
        if auth.expired():
            return None, "authorization_expired"
        # 自动批准发生在 _bind_execution_authority 之前，此刻
        # rt.resource_budget 还是空的；预算必须从与装配处同源的
        # 契约推导，否则每条计划都会以"预算超授权"被误判为 miss。
        # 两者读的是同一份 spec.constraints，批准后不会漂移。
        budget_raw = rt.resource_budget.get("max_seconds")
        if budget_raw is None:
            budget_raw = (spec.constraints.budget_seconds if spec is not None
                          else request.get("budget_seconds") or 0.0)
        budget = float(budget_raw or 0.0)
        if budget <= 0 or budget > auth.budget_seconds_max:
            return None, "budget_over_authorization"
        if self._lease_ttl_seconds(budget) > LEASE_TTL_CEILING_SECONDS:
            return None, "lease_ceiling_exceeded"
        pinned = auth.pinned_axes()
        expected = replace(spec, **pinned)
        changes = material_changes(expected, spec)
        if changes:
            return None, "spec_material_changes:" + ",".join(changes)
        matched = {
            "conditions": list(_STANDING_MATCH_CONDITIONS),
            "repo_fingerprint": self._repo_fingerprint(repo),
            "budget_seconds": budget,
            "pinned_axes": sorted(pinned),
        }
        return matched, ""

    def _maybe_standing_approve(self, rt: "ReviewRuntime", frozen) -> None:
        """Auto-approve a fresh plan only when a standing authorization covers it.

        The authorization was issued in advance by a human with explicit
        scope; this is not the agent approving its own plan.  Any miss keeps
        the review in AWAITING_APPROVAL for a human, with the first failing
        condition journaled so unattended runs stay auditable.
        """
        auth = self.standing_authorization
        if auth is None:
            return
        matched, reason = self._standing_authorization_match(rt)
        if matched is None:
            rt.events.append("approval.standing_authorization_missed", {
                "authorization_sha256": auth.source_sha256,
                "reason": reason,
            })
            return
        rt.events.append("approval.standing_authorization_used", {
            "authorization_sha256": auth.source_sha256,
            "matched_conditions": matched,
            "semantics": ("standing authorization was issued in advance by "
                          "a human with explicit scope; it is not agent "
                          "self-approval"),
        })
        # Reuse approve() wholesale: STALE_PLAN, the STALE_APPROVAL HEAD +
        # workspace fingerprint re-check, the lease, and plan.approved all
        # still apply to the autonomous path.
        self.approve(rt.review_id, frozen.plan_sha256)

    def _self_writes(self, rt: "ReviewRuntime") -> tuple[int, int]:
        """Count autonomous write attempts from the durable event ledger."""
        if self.standing_authorization is None:
            return 0, 0
        now = datetime.now(timezone.utc)
        total = hourly = 0
        for event in rt.events.all():
            if event.kind not in _SELF_WRITE_EVENT_KINDS:
                continue
            total += 1
            try:
                occurred = datetime.fromisoformat(event.occurred_at)
            except ValueError:
                occurred = now
            if (now - occurred).total_seconds() < 3600.0:
                hourly += 1
        return total, hourly

    def _assert_write_budget(self, rt: "ReviewRuntime") -> None:
        """Rate and cumulative caps for autonomous writes; fail closed."""
        auth = self.standing_authorization
        if auth is None or self.agent_level is not AgentLevel.L3:
            return
        total, hourly = self._self_writes(rt)
        if total >= auth.max_writes_per_review:
            raise IntakeError(
                "WRITE_BUDGET_EXCEEDED",
                f"per-review self-write cap reached "
                f"({total}/{auth.max_writes_per_review})")
        if hourly >= auth.max_writes_per_hour:
            raise IntakeError(
                "WRITE_BUDGET_EXCEEDED",
                f"hourly self-write cap reached "
                f"({hourly}/{auth.max_writes_per_hour})")

    def cancel(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Request a cooperative cancellation; never kills an arbitrary PID.

        The execution loop observes the event between deterministic actions. If
        a repository subprocess is currently running it is allowed to finish or
        hit its own deadline, after which the run is finalized as ABORTED.
        """
        payload = {"review_id": review_id, "action": "cancel"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        status = rt.state.snapshot.status
        if status in TERMINAL:
            raise IntakeError("REVIEW_NOT_CANCELLABLE", status.value)
        if status in {ReviewStatus.CREATED, ReviewStatus.INTAKE_VALIDATED,
                      ReviewStatus.PLAN_DRAFTED, ReviewStatus.AWAITING_APPROVAL}:
            rt.events.append("review.cancel_requested", {"phase": status.value})
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_cancelled:user")
            with self._lock:
                if self._live_id == review_id:
                    self._live_id = None
            response = self.describe(review_id)
        elif status in {ReviewStatus.PLAN_FROZEN, ReviewStatus.BASELINE_RUNNING,
                        ReviewStatus.EXECUTING, ReviewStatus.VERIFYING_RESTORE,
                        ReviewStatus.SYNTHESIZING, ReviewStatus.CANCELLING,
                        ReviewStatus.REPLANNING, ReviewStatus.AWAITING_HUMAN,
                        ReviewStatus.PROPOSING_REPAIR,
                        ReviewStatus.VERIFYING_REPAIR,
                        ReviewStatus.DELIVERING_BRANCH}:
            rt.cancel_event.set()
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="review_cancelled:user")
            rt.events.append("review.cancel_requested", {"phase": status.value})
            response = self.describe(review_id)
        elif status in {ReviewStatus.RECOVERING, ReviewStatus.CLEANUP_REQUIRED,
                        ReviewStatus.QUARANTINED}:
            response = self.cleanup(review_id)
        else:
            raise IntakeError("REVIEW_NOT_CANCELLABLE", status.value)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def resume(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Create a new review only from a restart-aborted, unchanged snapshot."""
        payload = {"review_id": review_id, "action": "resume"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.ABORTED:
            raise IntakeError("REVIEW_NOT_RESUMABLE", rt.state.snapshot.status.value)
        if rt.state.snapshot.reason != "review_aborted:process_restart":
            raise IntakeError("REVIEW_NOT_RESUMABLE", "only restart-aborted reviews may resume")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        source = request.get("source") or {}
        repo = self.registry.get(str(source.get("repo_id") or ""))
        expected = (request.get("source_snapshot") or {}).get("head")
        current = _repo_snapshot(repo.path)
        snapshot = request.get("source_snapshot") or {}
        if not expected or not snapshot.get("status_sha256"):
            raise IntakeError("SOURCE_SNAPSHOT_MISSING", "缺少可验证的源仓库快照，不能自动恢复")
        if expected and (expected != current.get("head") or
                         snapshot.get("status_sha256") != current.get("status_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED", "仓库 HEAD 已变化，不能自动恢复")
        if request.get("schema_version") == "review-request-v2" and request.get("review_spec"):
            child = self.create_v2(request["review_spec"])
        else:
            fields = {key: request[key] for key in {
                "source", "test_files", "declared_tests", "goal", "review_focus",
                "budget_seconds", "model_provider", "probe_strategy", "ui_mode",
            } if key in request}
            child = self.create(fields)
        child_rt = self._runtime(child["review_id"])
        child_rt.request["resume_of"] = review_id
        _atomic_json(child_rt.review_dir / "request.json", child_rt.request)
        child_rt.events.append("review.resumed", {"parent_review_id": review_id})
        rt.events.append("review.resume_created", {"replacement_review_id": child["review_id"]})
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, child)
        return child

    def cleanup(self, review_id: str, *, idempotency_key: str = "") -> dict:
        """Bounded operator cleanup for recovery/quarantine and orphaned temps."""
        payload = {"review_id": review_id, "action": "cleanup"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.thread is not None and rt.thread.is_alive():
            rt.cancel_event.set()
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="manual_cleanup_requested")
            rt.events.append("review.cleanup_deferred", {"reason": "execution_active"})
            response = self.describe(review_id)
            with self._lock:
                self._remember_idempotency(idempotency_key, payload, response)
            return response
        rt.events.append("review.cleanup_requested", {
            "state": rt.state.snapshot.status.value})
        removed_temps = 0
        for path in tuple(rt.review_dir.rglob(".*.tmp")):
            try:
                path.unlink()
                removed_temps += 1
            except OSError as exc:
                if rt.state.snapshot.status is not ReviewStatus.QUARANTINED:
                    self._transition(rt, ReviewStatus.QUARANTINED,
                                     reason="manual_cleanup_failed")
                raise IntakeError("CLEANUP_FAILED", str(exc)[:200]) from exc
        removed_worktrees = 0
        request = rt.request or _read_json(rt.review_dir / "request.json")
        source = request.get("source") or {}
        try:
            repo = self.registry.get(str(source.get("repo_id") or ""))
        except IntakeError:
            repo = None
        for root in tuple((rt.review_dir / "repair" / "worktrees").glob("shuimu-repair-*")):
            worktree = root / "worktree"
            if repo is not None and worktree.exists():
                run_git(["worktree", "remove", "--force", str(worktree)], cwd=repo.path)
            shutil.rmtree(root, ignore_errors=True)
            removed_worktrees += 1
        try:
            removed_leases = self._leases.cleanup_for_review(review_id)
        except LeaseError as exc:
            if rt.state.snapshot.status is not ReviewStatus.QUARANTINED:
                self._transition(rt, ReviewStatus.QUARANTINED, reason=exc.code)
            raise IntakeError(exc.code, exc.detail) from exc
        for status_file in (rt.review_dir / "artifacts").glob("*/run_status.json"):
            evidence = RunRoot(status_file.parent, official=False,
                               unofficial_reason="review_evidence")
            if evidence.status().get("status") == "INCOMPLETE":
                evidence.finish(False, reason="review_aborted:manual_cleanup")
        if rt.state.snapshot.status not in TERMINAL:
            if rt.state.snapshot.status is ReviewStatus.QUARANTINED:
                self._transition(rt, ReviewStatus.CLEANUP_REQUIRED,
                                 reason="manual_cleanup_verified")
            elif rt.state.snapshot.status is not ReviewStatus.CLEANUP_REQUIRED:
                self._transition(rt, ReviewStatus.RECOVERING,
                                 reason="manual_cleanup")
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_aborted:manual_cleanup")
        rt.events.append("review.cleanup_completed", {
            "temporary_artifacts_removed": removed_temps,
            "orphan_worktrees_removed": removed_worktrees,
            "leases_removed": removed_leases,
        })
        with self._lock:
            if self._live_id == review_id:
                self._live_id = None
        response = self.describe(review_id)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def decide(self, review_id: str, decision_id: str, decision: str,
               plan_sha256: str, *, idempotency_key: str = "") -> dict:
        """Answer one controller-created, plan-bound human escalation."""
        payload = {"review_id": review_id, "action": "decide",
                   "decision_id": decision_id, "decision": decision,
                   "plan_sha256": plan_sha256}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        if decision not in {"continue", "stop"}:
            raise IntakeError("DECISION_INVALID", decision)
        rt = self._runtime(review_id)
        if rt.state.snapshot.status is not ReviewStatus.AWAITING_HUMAN:
            raise IntakeError("DECISION_NOT_AWAITING", rt.state.snapshot.status.value)
        pending = rt.pending_decision or _read_json(
            rt.review_dir / "decisions" / "pending.json")
        if (str(pending.get("decision_id") or "") != decision_id
                or not secrets.compare_digest(
                    str(pending.get("plan_sha256") or ""), plan_sha256)):
            raise IntakeError("DECISION_STALE", "decision or plan hash is not current")
        if time.time() >= float(pending.get("deadline_epoch") or 0):
            raise IntakeError("DECISION_EXPIRED", decision_id)
        answer = {**pending, "decision": decision,
                  "answered_at_epoch": time.time(), "status": "ANSWERED"}
        _atomic_json(rt.review_dir / "decisions" / f"{decision_id}.json", answer)
        rt.events.append("decision.answered", {
            "decision_id": decision_id, "decision": decision,
            "plan_sha256": plan_sha256})
        rt.decision_answer = decision
        rt.decision_event.set()
        response = self.describe(review_id)
        response["decision"] = {"decision_id": decision_id,
                                "status": "ANSWERED", "decision": decision}
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def record_disposition(self, review_id: str, raw: dict, *,
                           idempotency_key: str = "") -> dict:
        """Persist one attributable human disposition for an escalation event.

        处置留痕是审计通道，不是控制通道：记录处置不改变审查状态，也从不
        代替 decide()。needs_human 事件的人工结论（谁处理、怎么处理）落在
        review 目录下，与事件日志互为印证。
        """
        payload = {"review_id": review_id, "action": "record_disposition",
                   "body": raw}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        required = {"event_id", "answer", "handled_by"}
        if (not isinstance(raw, dict) or not required <= set(raw)
                or set(raw) - (required | {"note"})
                or not all(isinstance(raw.get(key), str)
                           for key in required)
                or not isinstance(raw.get("note", ""), str)):
            raise IntakeError("DISPOSITION_REQUEST_INVALID",
                              "disposition requires string event_id, answer "
                              "and handled_by, plus an optional string note")
        event_id = raw["event_id"]
        answer = raw["answer"]
        handled_by = raw["handled_by"].strip()
        note = raw.get("note", "")
        if not 1 <= len(handled_by) <= 100:
            raise IntakeError("DISPOSITION_HANDLER_INVALID",
                              "handled_by must be 1-100 characters after trim")
        if len(note) > 500:
            raise IntakeError("DISPOSITION_REQUEST_INVALID",
                              "note must be at most 500 characters")
        rt = self._runtime(review_id)
        event = next((item for item in rt.events.all()
                      if item.event_id == event_id), None)
        if event is None:
            raise IntakeError("EVENT_NOT_FOUND", event_id)
        if event.kind not in _DISPOSITION_ANSWERS:
            raise IntakeError("DISPOSITION_EVENT_KIND_INVALID", event.kind)
        if answer not in _DISPOSITION_ANSWERS[event.kind]:
            raise IntakeError("DISPOSITION_ANSWER_INVALID", answer)
        path = rt.review_dir / "dispositions" / f"{event.seq:06d}.json"
        with self._lock:
            if path.exists():
                raise IntakeError("DISPOSITION_ALREADY_RECORDED", event_id)
            record = {
                "schema_version": "human-disposition-v1",
                "review_id": review_id,
                "event_id": event_id,
                "event_kind": event.kind,
                "answer": answer,
                "handled_by": handled_by,
                "note": note,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(path, record)
        rt.events.append("human.disposition.recorded", dict(record))
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def dispositions(self, review_id: str) -> dict:
        """List recorded human dispositions for audit, oldest first."""
        rt = self._runtime(review_id)
        directory = rt.review_dir / "dispositions"
        records = []
        if directory.is_dir():
            records = [_read_json(path)
                       for path in sorted(directory.glob("*.json"))]
        return {"schema_version": "human-dispositions-v1",
                "dispositions": records}

    # ---------------------------------------------------------- P5/P6 评论
    def create_comment(self, review_id: str, raw: dict, *,
                       idempotency_key: str = "") -> dict:
        """在审查快照上锚定一条人工评论；锚点字段全部由服务端计算。"""
        payload = {"review_id": review_id, "action": "create_comment",
                   "body": raw}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        allowed = {"author", "body", "kind", "path", "start_line", "end_line",
                   "claim_id", "evidence_ids"}
        required = {"author", "body", "kind", "path", "start_line", "end_line"}
        if (not isinstance(raw, dict) or not required <= set(raw)
                or set(raw) - allowed):
            raise IntakeError("COMMENT_REQUEST_INVALID",
                              "comment requires author, body, kind, path, "
                              "start_line and end_line; unknown fields are rejected")
        author, body, kind = _validate_comment_text(raw)
        claim_id, evidence_ids = _validate_comment_reference(raw)
        rt, repo, snapshot_sha = self._source_context(review_id)
        self._validate_claim_binding(rt.review_dir, claim_id, evidence_ids)
        anchor = self._build_comment_anchor(
            rt, repo, snapshot_sha, raw["path"],
            raw["start_line"], raw["end_line"])
        with self._lock:
            record = self._create_comment_record(
                rt, author=author, body=body, kind=kind, anchor=anchor,
                claim_id=claim_id, evidence_ids=evidence_ids,
                parent_comment_id=None, supersedes_comment_id=None)
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def list_comments(self, review_id: str, *, path: str | None = None) -> dict:
        """列出评论；读取时惰性检测锚点是否已被源码变化甩在身后。

        列表不要求工作区仍是审查快照——证据可以离线翻阅。但 open 状态
        的评论会与当前文件内容比对，不一致就落盘为 outdated 并追加事件，
        锚点本身绝不迁移到"看起来相似"的新行。
        """
        rt = self._runtime(review_id)
        records = _comment_records(rt.review_dir)
        repo = self._comment_repo_best_effort(rt)
        if repo is not None:
            blobs: dict[str, str | None] = {}
            touched = False
            for record in records:
                if record.get("status") != "open":
                    continue
                anchor = record.get("anchor") or {}
                rel = str(anchor.get("path") or "")
                if not rel:
                    continue
                if rel not in blobs:
                    target = repo.path.joinpath(*PurePosixPath(rel).parts)
                    try:
                        data = target.read_bytes()
                        blobs[rel] = (hashlib.sha256(data).hexdigest()
                                      if len(data) <= SOURCE_FILE_MAX_BYTES
                                      else "oversized")
                    except OSError:
                        blobs[rel] = None
                if blobs[rel] != anchor.get("blob_sha256"):
                    with self._lock:
                        self._mark_comment_outdated(rt, record)
                    touched = True
            if touched:
                records = _comment_records(rt.review_dir)
        comments = records
        if path is not None:
            wanted = str(path)
            comments = [record for record in records
                        if str((record.get("anchor") or {}).get("path") or "")
                        == wanted]
        request = rt.request or _read_json(rt.review_dir / "request.json")
        return {
            "schema_version": "review-comments-v1",
            "review_id": review_id,
            "source_snapshot_sha256": str(
                (request.get("source_snapshot") or {}).get("snapshot_sha256")
                or ""),
            "counts": {
                "total": len(comments),
                "open": sum(1 for r in comments if r.get("status") == "open"),
                "resolved": sum(1 for r in comments
                                if r.get("status") == "resolved"),
                "withdrawn": sum(1 for r in comments
                                  if r.get("status") == "withdrawn"),
                "outdated": sum(1 for r in comments
                                if r.get("status") == "outdated"),
            },
            "comments": comments,
        }

    def reply_comment(self, review_id: str, comment_id: str, raw: dict, *,
                      idempotency_key: str = "") -> dict:
        """在一条评论下回复：继承父评论锚定，不重新绑定源码。"""
        payload = {"review_id": review_id, "action": "reply_comment",
                   "comment_id": comment_id, "body": raw}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        allowed = {"author", "body", "kind"}
        required = {"author", "body", "kind"}
        if (not isinstance(raw, dict) or not required <= set(raw)
                or set(raw) - allowed):
            raise IntakeError("COMMENT_REQUEST_INVALID",
                              "reply requires author, body and kind only")
        author, body, kind = _validate_comment_text(raw)
        rt = self._runtime(review_id)
        with self._lock:
            parent = self._find_comment(rt, comment_id)
            if parent.get("status") == "withdrawn":
                raise IntakeError("COMMENT_PARENT_INVALID",
                                  "withdrawn threads cannot be extended")
            record = self._create_comment_record(
                rt, author=author, body=body, kind=kind,
                anchor=dict(parent.get("anchor") or {}),
                claim_id=parent.get("claim_id"),
                evidence_ids=list(parent.get("evidence_ids") or []),
                parent_comment_id=comment_id, supersedes_comment_id=None)
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def update_comment(self, review_id: str, comment_id: str, raw: dict, *,
                       if_match: str = "") -> dict:
        """按 revision 或 If-Match 更新评论状态；撤回只留痕不删除。

        重新锚定（action="reanchor"）不搬动旧记录：它创建一条
        supersedes_comment_id 指向旧评论的新评论，旧锚点原样保留。
        """
        if not isinstance(raw, dict):
            raise IntakeError("COMMENT_REQUEST_INVALID",
                              "update body must be an object")
        rt = self._runtime(review_id)
        with self._lock:
            record = self._find_comment(rt, comment_id)
            expected = str(record.get("revision") or 1)
            body_revision = raw.get("revision")
            if (isinstance(body_revision, bool)
                    or not isinstance(body_revision, int)):
                body_revision = None
            header_revision = str(if_match or "").strip().strip('"')
            if body_revision is None and not header_revision:
                raise IntakeError("COMMENT_REVISION_REQUIRED",
                                  "comment updates require revision or If-Match")
            if (body_revision is not None and header_revision
                    and str(body_revision) != header_revision):
                raise IntakeError("COMMENT_REVISION_CONFLICT",
                                  "body revision and If-Match disagree")
            for candidate in {str(body_revision) if body_revision is not None else "",
                              header_revision} - {""}:
                if candidate != expected:
                    raise IntakeError("COMMENT_REVISION_CONFLICT",
                                      f"expected revision {expected}")
            action = raw.get("action")
            if action == "reanchor":
                return self._reanchor_comment(rt, record, raw)
            if action not in (None, ""):
                raise IntakeError("COMMENT_REQUEST_INVALID",
                                  f"unsupported action {action!r}")
            allowed_keys = {"revision", "status"}
            if set(raw) - allowed_keys or "status" not in raw:
                raise IntakeError("COMMENT_REQUEST_INVALID",
                                  "update requires status, optionally revision")
            status = raw["status"]
            if status not in COMMENT_STATUSES or status == "outdated":
                raise IntakeError("COMMENT_STATUS_INVALID",
                                  "outdated is system-set; pick open/resolved/withdrawn")
            transitions = {"open": {"resolved", "withdrawn"},
                           "resolved": {"open"},
                           "outdated": {"resolved", "withdrawn"},
                           "withdrawn": set()}
            current = str(record.get("status") or "open")
            if status not in transitions[current]:
                raise IntakeError("COMMENT_TRANSITION_INVALID",
                                  f"{current} -> {status} is not allowed")
            updated = dict(record)
            updated["status"] = status
            updated["revision"] = int(record.get("revision") or 1) + 1
            updated["updated_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_json(rt.review_dir / "comments" / f"{comment_id}.json",
                         updated)
            event_kind = {"resolved": "human.comment.resolved",
                          "withdrawn": "human.comment.withdrawn",
                          "open": "human.comment.reopened"}[status]
            _append_annotation_event(rt.review_dir, event_kind,
                                     {"comment_id": comment_id,
                                      "from": current, "to": status})
            return updated

    def annotations_package(self, review_id: str) -> dict:
        """按需构建独立注释包：机器包回答"当时观察到什么"，这里回答
        "人后来如何理解、质疑和处理"。导出前重新校验哈希链。"""
        rt = self._runtime(review_id)
        bundle_sha = hashlib.sha256(
            self.review_bundle_path(review_id).read_bytes()).hexdigest()
        comments = _comment_records(rt.review_dir)
        events = _read_annotation_events(rt.review_dir)
        reverifications = _reverification_records(rt.review_dir)
        return {
            "schema_version": "review-annotations-v1",
            "review_id": review_id,
            "source_bundle_sha256": bundle_sha,
            "annotation_schema": "review-annotations-v1",
            "comment_records": comments,
            "reverification_records": reverifications,
            "event_chain": events,
            "integrity": {
                "algorithm": "sha256",
                "entries": len(events),
                "head_sha256": (events[-1]["entry_sha256"] if events
                                else _ANNOTATION_CHAIN_ZERO),
                "chain_verified": True,
            },
        }

    def _find_comment(self, rt: ReviewRuntime, comment_id: str) -> dict:
        if not isinstance(comment_id, str) or not comment_id:
            raise IntakeError("COMMENT_REQUEST_INVALID", "comment_id required")
        record = next((item for item in _comment_records(rt.review_dir)
                       if item.get("comment_id") == comment_id), None)
        if record is None:
            raise IntakeError("COMMENT_NOT_FOUND", comment_id)
        return record

    def _create_comment_record(self, rt: ReviewRuntime, *, author: str,
                               body: str, kind: str, anchor: dict,
                               claim_id: str | None,
                               evidence_ids: list[str],
                               parent_comment_id: str | None,
                               supersedes_comment_id: str | None) -> dict:
        """落一条新评论并追加注释链事件；调用方必须已持有 self._lock。"""
        records = _comment_records(rt.review_dir)
        comment_id = f'cmt-{_next_comment_sequence(records):06d}'
        now = datetime.now(timezone.utc).isoformat()
        record = {
            "schema_version": "review-comment-v1",
            "comment_id": comment_id,
            "review_id": rt.review_id,
            "author": author,
            "body": body,
            "kind": kind,
            "claim_id": claim_id,
            "anchor": anchor,
            "evidence_ids": evidence_ids,
            "parent_comment_id": parent_comment_id,
            "supersedes_comment_id": supersedes_comment_id,
            "status": "open",
            "created_at": now,
            "updated_at": now,
            "revision": 1,
        }
        _atomic_json(rt.review_dir / "comments" / f"{comment_id}.json", record)
        _append_annotation_event(rt.review_dir, "human.comment.created", {
            "comment_id": comment_id,
            "kind": kind,
            "path": anchor.get("path"),
            "start_line": anchor.get("start_line"),
            "end_line": anchor.get("end_line"),
            "claim_id": claim_id,
            "parent_comment_id": parent_comment_id,
            "supersedes_comment_id": supersedes_comment_id,
        })
        return record

    def _reanchor_comment(self, rt: ReviewRuntime, record: dict,
                          raw: dict) -> dict:
        anchor_raw = raw.get("anchor")
        if (not isinstance(anchor_raw, dict)
                or not {"path", "start_line", "end_line"} <= set(anchor_raw)
                or set(anchor_raw) - {"path", "start_line", "end_line", "body"}):
            raise IntakeError("COMMENT_REQUEST_INVALID",
                              "reanchor requires anchor.path/start_line/end_line")
        body = record.get("body")
        if "body" in anchor_raw:
            if not isinstance(anchor_raw["body"], str):
                raise IntakeError("COMMENT_BODY_INVALID",
                                  "reanchor body must be a string")
            _author, body, _kind = _validate_comment_text(
                {"author": record.get("author") or "author",
                 "body": anchor_raw["body"],
                 "kind": record.get("kind") or "note"})
        _rt, repo, snapshot_sha = self._source_context(rt.review_id)
        new_anchor = self._build_comment_anchor(
            rt, repo, snapshot_sha, anchor_raw["path"],
            anchor_raw["start_line"], anchor_raw["end_line"])
        replacement = self._create_comment_record(
            rt, author=str(record.get("author") or ""),
            body=str(body or ""), kind=str(record.get("kind") or "note"),
            anchor=new_anchor,
            claim_id=record.get("claim_id"),
            evidence_ids=list(record.get("evidence_ids") or []),
            parent_comment_id=record.get("parent_comment_id"),
            supersedes_comment_id=str(record.get("comment_id") or ""))
        updated = dict(record)
        updated["superseded_by"] = replacement["comment_id"]
        updated["revision"] = int(record.get("revision") or 1) + 1
        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        old_id = str(record.get("comment_id") or "")
        _atomic_json(rt.review_dir / "comments" / f"{old_id}.json", updated)
        _append_annotation_event(rt.review_dir, "human.comment.superseded", {
            "comment_id": old_id,
            "superseded_by": replacement["comment_id"],
            "old_path": (record.get("anchor") or {}).get("path"),
            "new_path": new_anchor.get("path"),
        })
        return {"comment": replacement, "superseded": updated}

    def _build_comment_anchor(self, rt: ReviewRuntime, repo: RegisteredRepo,
                              snapshot_sha: str, raw_path: object,
                              start_line: object, end_line: object) -> dict:
        """服务端计算锚点：客户端只报 path 和行号，哈希与提交自证。"""
        if (not isinstance(start_line, int) or isinstance(start_line, bool)
                or not isinstance(end_line, int) or isinstance(end_line, bool)):
            raise IntakeError("COMMENT_ANCHOR_INVALID",
                              "start_line/end_line must be integers")
        served = self.source_file(rt.review_id, raw_path)
        if not 1 <= start_line <= end_line <= served["line_count"]:
            raise IntakeError("COMMENT_ANCHOR_INVALID",
                              f"line range must be within 1..{served['line_count']}")
        text_lines = served["content"].splitlines()
        context = "\n".join(text_lines[start_line - 1:end_line])
        request = rt.request or _read_json(rt.review_dir / "request.json")
        head = str((request.get("source_snapshot") or {}).get("head") or "")
        return {
            "source_snapshot_sha256": snapshot_sha,
            "target_commit": head,
            "path": served["path"],
            "side": "base",
            "start_line": start_line,
            "end_line": end_line,
            "blob_sha256": served["blob_sha256"],
            "context_sha256": hashlib.sha256(
                context.encode("utf-8")).hexdigest(),
        }

    def _validate_claim_binding(self, review_dir: Path, claim_id: str | None,
                                evidence_ids: list[str]) -> None:
        """评论只能绑定这次审查真实发出过的机器主张与证据记录。"""
        if claim_id is None and not evidence_ids:
            return
        claims, evidence = _review_claim_surface(review_dir)
        if not claims:
            raise IntakeError("EVIDENCE_LEDGER_UNAVAILABLE",
                              "this review has no machine claims to bind")
        if claim_id is not None and claim_id not in claims:
            raise IntakeError("EVIDENCE_CLAIM_NOT_FOUND", claim_id)
        missing = [item for item in evidence_ids if item not in evidence]
        if missing:
            raise IntakeError("EVIDENCE_NOT_FOUND", ", ".join(missing[:3]))

    def _mark_comment_outdated(self, rt: ReviewRuntime, record: dict) -> dict:
        """调用方须持锁：把被源码变化甩下的 open 评论落盘为 outdated。"""
        updated = dict(record)
        updated["status"] = "outdated"
        updated["revision"] = int(record.get("revision") or 1) + 1
        updated["updated_at"] = datetime.now(timezone.utc).isoformat()
        comment_id = str(record.get("comment_id") or "")
        _atomic_json(rt.review_dir / "comments" / f"{comment_id}.json", updated)
        _append_annotation_event(rt.review_dir, "human.comment.outdated", {
            "comment_id": comment_id,
            "reason": "anchor_blob_mismatch",
            "path": (record.get("anchor") or {}).get("path"),
        })
        return updated

    # ------------------------------------------------------------ P7 复验

    def request_reverification(self, review_id: str, raw: dict, *,
                               idempotency_key: str = "") -> dict:
        """从一条 challenge 评论出发生成复验计划；人确认前不跑任何实验。

        计划是封闭的：被质疑的主张必须真实存在于这次审查的账本，且必须
        拿得出至少一条可重放的实验记录。找不到凭据的主张不进入复验——
        它已经没有可复验的实验内容。
        """
        payload = {"review_id": review_id, "action": "request_reverification",
                   "body": raw}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        allowed = {"comment_id", "reason", "explanation", "test_patch"}
        if (not isinstance(raw, dict) or "comment_id" not in raw
                or "reason" not in raw or set(raw) - allowed):
            raise IntakeError("REVERIFICATION_REQUEST_INVALID",
                              "request requires comment_id and reason; "
                              "only explanation is optional")
        reason = raw["reason"]
        explanation = raw.get("explanation", "")
        if reason not in REVERIFICATION_REASONS:
            raise IntakeError("REVERIFICATION_REASON_INVALID",
                              "reason must be one of the closed options")
        test_patch = raw.get("test_patch")
        if reason == "new_test_available":
            # 策略 4：质疑必须自带一个只碰测试文件的 unified diff。
            # 计划阶段就过 test-only 白名单边界，run 阶段按同一哈希
            # 复验——两头都 fail-closed，中间没有"先收下再补"的状态。
            if not isinstance(test_patch, str) or not test_patch.strip():
                raise IntakeError(
                    "REVERIFICATION_TEST_PATCH_REQUIRED",
                    "new_test_available requires a unified diff that "
                    "adds one test file")
            if len(test_patch) > 65536 or "\x00" in test_patch:
                raise IntakeError(
                    "REVERIFICATION_TEST_PATCH_INVALID",
                    "test_patch must be at most 64KB of text without NUL")
            try:
                validate_test_only_patch(test_patch)
            except RepairError as exc:
                raise IntakeError(exc.code, exc.detail) from exc
        elif test_patch is not None:
            raise IntakeError("REVERIFICATION_REQUEST_INVALID",
                              "test_patch only applies to new_test_available")
        if explanation is None:
            explanation = ""
        if not isinstance(explanation, str) or len(explanation) > 2000:
            raise IntakeError("REVERIFICATION_EXPLANATION_INVALID",
                              "explanation must be at most 2000 chars")
        if reason == "other_needs_explanation" and not explanation.strip():
            raise IntakeError("REVERIFICATION_EXPLANATION_REQUIRED",
                              "choosing 'other' requires a human explanation")
        rt = self._runtime(review_id)
        with self._lock:
            comment = self._find_comment(rt, str(raw["comment_id"]))
            if comment.get("kind") != "challenge":
                raise IntakeError("REVERIFICATION_COMMENT_INVALID",
                                  "only challenge comments can request "
                                  "reverification")
            if not comment.get("claim_id"):
                raise IntakeError("REVERIFICATION_CLAIM_MISSING",
                                  "the challenge comment is not bound to a "
                                  "machine claim")
            experiments = self._claim_replayable_experiments(
                rt, str(comment["claim_id"]))
            if not experiments:
                raise IntakeError("REVERIFICATION_EXPERIMENTS_MISSING",
                                  "the claim has no replayable experiment "
                                  "records; nothing to re-run")
            existing = _reverification_records(rt.review_dir)
            rid = f"rvf-{len(existing) + 1:06d}"
            record = {
                "schema_version": reverification.SCHEMA_VERSION,
                "reverification_id": rid,
                "review_id": review_id,
                "comment_id": comment["comment_id"],
                "claim_id": comment["claim_id"],
                "reason": reason,
                "explanation": explanation,
                "test_proposal": ({"patch": test_patch,
                                   "patch_sha256": hashlib.sha256(
                                       test_patch.encode("utf-8"))
                                   .hexdigest()}
                                  if reason == "new_test_available"
                                  else None),
                **reverification.plan_fields(reason),
                "status": "planned",
                "plan": {
                    "replay_experiments": [
                        {"experiment_id": row["record_id"],
                         "intervention": (row.get("payload") or {})
                         .get("intervention")}
                        for row in experiments],
                    "declared_tests": self._review_declared_tests(rt),
                    "comparison": "vector_diff_against_original_experiment",
                },
                "original_observations": [
                    {"experiment_id": row["record_id"],
                     "intervention": (row.get("payload") or {}).get("intervention")}
                    for row in experiments],
                "replay_observations": [],
                "outcome": None,
                "outcome_reason": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": record_ts(),
            }
            _atomic_json(rt.review_dir / "reverifications" / f"{rid}.json",
                         record)
            _append_annotation_event(rt.review_dir, "human.claim.challenged", {
                "comment_id": comment["comment_id"],
                "claim_id": comment["claim_id"], "reason": reason,
                "reverification_id": rid,
            })
            _append_annotation_event(rt.review_dir,
                                     "review.reverification.requested", {
                                         "reverification_id": rid,
                                         "claim_id": comment["claim_id"],
                                         "reason": reason,
                                         "experiments":
                                             len(record["plan"]
                                                 ["replay_experiments"]),
                                     })
            self._remember_idempotency(idempotency_key, payload, record)
            return record

    def approve_reverification(self, review_id: str, reverification_id: str,
                               raw: dict, *, idempotency_key: str = "") -> dict:
        """用户确认后立即重放干预并对比新旧结果，一次调用走到 settled。

        这不是异步任务：一次复验只重放一个主张所凭的实验记录，预算有
        上界。审批与重放分开成两步是为了把"用户明确确认"写进哈希链——
        没有这次确认，任何实验都不应被再次执行。
        """
        payload = {"review_id": review_id, "action": "approve_reverification",
                   "reverification_id": reverification_id, "body": raw}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        if raw not in ({}, None):
            raise IntakeError("REVERIFICATION_APPROVAL_INVALID",
                              "approval takes an empty body")
        rt = self._runtime(review_id)
        with self._lock:
            record = self._find_reverification(rt, reverification_id)
            if record["status"] != "planned":
                raise IntakeError("REVERIFICATION_STATE_INVALID",
                                  f"cannot approve from status "
                                  f"{record['status']}")
            record = dict(record)
            record["status"] = "approved"
            record["updated_at"] = record_ts()
            _append_annotation_event(rt.review_dir,
                                     "review.reverification.approved", {
                                         "reverification_id": reverification_id,
                                         "claim_id": record["claim_id"],
                                     })
            _atomic_json(rt.review_dir / "reverifications"
                         / f"{reverification_id}.json", record)
        # 重放在锁外执行：它要跑真实的测试子进程，不能拿全局锁当路障。
        settled = self._run_reverification_locked(rt, record)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, settled)
        return settled

    def reject_reverification(self, review_id: str, reverification_id: str,
                              raw: dict) -> dict:
        """用户否决复验计划；质疑保留，主张维持原判。"""
        if raw in (None, {}):
            raw = {}
        if not isinstance(raw, dict) or set(raw) - {"note"}:
            raise IntakeError("REVERIFICATION_REJECTION_INVALID",
                              "rejection takes at most a note string")
        note = raw.get("note", "") if isinstance(raw, dict) else ""
        if not isinstance(note, str) or len(note) > 2000:
            raise IntakeError("REVERIFICATION_REJECTION_INVALID",
                              "note must be at most 2000 chars")
        rt = self._runtime(review_id)
        with self._lock:
            record = self._find_reverification(rt, reverification_id)
            if record["status"] != "planned":
                raise IntakeError("REVERIFICATION_STATE_INVALID",
                                  f"cannot reject from status "
                                  f"{record['status']}")
            record = dict(record)
            record["status"] = "rejected"
            record["outcome"] = "claim.withheld"
            record["outcome_reason"] = "reverification_rejected_by_user"
            record["claim_revision"] = _next_claim_revision(
                rt.review_dir, record["claim_id"])
            record["updated_at"] = record_ts()
            _atomic_json(rt.review_dir / "reverifications"
                         / f"{reverification_id}.json", record)
            _append_annotation_event(rt.review_dir, "claim.withheld", {
                "reverification_id": reverification_id,
                "claim_id": record["claim_id"],
                "claim_revision": record["claim_revision"],
                "outcome_reason": "reverification_rejected_by_user",
            })
            return record

    def list_reverifications(self, review_id: str) -> dict:
        """列出复验记录；质疑历史本身就是审查证据的一部分。"""
        rt = self._runtime(review_id)
        return {
            "schema_version": "review-reverifications-v1",
            "review_id": review_id,
            "reverifications": _reverification_records(rt.review_dir),
        }

    # ------------------------------------------------------------------
    # M1 · edit-session-v1：会话身份、源快照绑定、逐条补丁入账。
    # 纪律：repair.deliver_verified_patch 仍是唯一写入边界；会话只在
    # 边界外围记账（谁开的、冻的哪份快照、每个候选差异去了哪里），
    # 这里没有任何代码直接写用户 checkout。

    def open_edit_session(self, review_id: str, raw: dict, *,
                          idempotency_key: str = "") -> dict:
        """开一个编辑会话：冻结源快照，登记身份，写进注释链。"""
        if raw in (None, {}):
            raw = {}
        if not isinstance(raw, dict) or set(raw) - {"intent"}:
            raise IntakeError("EDIT_SESSION_REQUEST_INVALID",
                              "opening an edit session takes at most an intent string")
        intent = raw.get("intent", "")
        if not isinstance(intent, str) or len(intent) > 2000:
            raise IntakeError("EDIT_SESSION_INTENT_INVALID",
                              "intent must be a string of at most 2000 chars")
        payload = {"review_id": review_id, "action": "open_edit_session",
                   "body": {"intent": intent}}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        # 快照核对与冻结必须同源：_source_context 先确认工作区仍是
        # 审查时那份，然后我们冻结此刻的快照作为会话的锚。
        rt2, repo, _review_sha = self._source_context(review_id)
        with self._lock:
            self._sweep_orphan_edit_sessions(rt2, repo)
            records = _edit_session_records(rt2.review_dir)
            session_id = edit_sessions.new_session_id(
                [str(r.get("session_id") or "") for r in records])
            current = _repo_snapshot(repo.path)
            record = {
                "schema_version": edit_sessions.EDIT_SESSION_SCHEMA_VERSION,
                "session_id": session_id,
                "review_id": review_id,
                "status": edit_sessions.OPEN,
                "intent": intent,
                "owner_pid": os.getpid(),
                "snapshot_sha256": current.get("snapshot_sha256") or "",
                "snapshot_head": current.get("head") or "",
                "candidates": [],
                "delivery": None,
                "transitions": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": record_ts(),
            }
            _ensure_disk_space(rt2.review_dir)
            _write_session_record(
                record, rt2.review_dir / "edit-sessions" / f"{session_id}.json")
            _append_annotation_event(rt2.review_dir, "edit_session.opened", {
                "session_id": session_id,
                "snapshot_sha256": record["snapshot_sha256"],
                "intent": intent,
            })
            self._remember_idempotency(idempotency_key, payload, record)
            return record

    def abandon_edit_session(self, review_id: str, session_id: str,
                             raw: dict) -> dict:
        """显式放弃一个活动会话；终态会话不可再变。"""
        if raw in (None, {}):
            raw = {}
        if not isinstance(raw, dict) or set(raw) - {"note"}:
            raise IntakeError("EDIT_SESSION_REQUEST_INVALID",
                              "abandoning takes at most a note string")
        rt = self._runtime(review_id)
        with self._lock:
            record = self._find_edit_session(rt, session_id)
            record = self._transition_edit_session(
                rt, record, edit_sessions.ABANDONED,
                reason="abandoned_by_user")
            return record

    def list_edit_sessions(self, review_id: str) -> dict:
        """列出编辑会话与每个候选补丁的入账情况。"""
        rt = self._runtime(review_id)
        return {
            "schema_version": edit_sessions.EDIT_SESSION_SCHEMA_VERSION,
            "review_id": review_id,
            "edit_sessions": _edit_session_records(rt.review_dir),
        }

    def _find_edit_session(self, rt: ReviewRuntime,
                           session_id: str) -> dict:
        if not isinstance(session_id, str) or not session_id:
            raise IntakeError("EDIT_SESSION_NOT_FOUND", "session_id required")
        record = next((item for item in
                       _edit_session_records(rt.review_dir)
                       if item.get("session_id") == session_id), None)
        if record is None:
            raise IntakeError("EDIT_SESSION_NOT_FOUND", session_id)
        return record

    def _transition_edit_session(self, rt: ReviewRuntime, record: dict,
                                 dst: str, *, reason: str = "") -> dict:
        """状态机迁移：非法迁移失败关闭，迁移与理由都进注释链。"""
        src = str(record.get("status") or "")
        if not edit_sessions.can_transition(src, dst):
            raise IntakeError("EDIT_SESSION_STATE_INVALID",
                              f"cannot transition {src} -> {dst}")
        record = dict(record)
        record["transitions"] = list(record.get("transitions") or []) + [{
            "from": src, "to": dst, "at": record_ts(), "reason": reason}]
        record["status"] = dst
        record["updated_at"] = record_ts()
        _write_session_record(
            record, rt.review_dir / "edit-sessions"
            / f'{record["session_id"]}.json')
        _append_annotation_event(rt.review_dir, "edit_session.transitioned", {
            "session_id": record["session_id"],
            "from": src, "to": dst, "reason": reason,
        })
        return record

    def _sweep_orphan_edit_sessions(self, rt: ReviewRuntime,
                                    repo=None) -> list[str]:
        """孤儿会话收割：owner 进程已死 → abandoned/stale。

        冻结快照仍与用户树一致 → abandoned（强杀没留下未知写入）；
        不一致 → stale（源已动，会话作废，fail-closed）。收割后跑
        一次 _assert_original_unchanged 作为字节完整性闸。
        """
        swept: list[str] = []
        for record in _edit_session_records(rt.review_dir):
            if not edit_sessions.is_orphan(record):
                continue
            stale = False
            frozen = str(record.get("snapshot_sha256") or "")
            if repo is not None and frozen:
                observed = str(_repo_snapshot(repo.path)
                               .get("snapshot_sha256") or "")
                stale = observed != frozen
            record = self._transition_edit_session(
                rt, record, edit_sessions.STALE if stale
                else edit_sessions.ABANDONED,
                reason=edit_sessions.EXIT_REASONS[
                    "stale" if stale else "abandoned"])
            swept.append(record["session_id"])
        if swept and repo is not None:
            before = run_git(["status", "--porcelain=v1"], cwd=repo.path,
                             check=True, timeout=10).stdout
            try:
                _assert_original_unchanged(repo.path, before)
            except RepairError as exc:
                raise IntakeError(exc.code, exc.detail) from exc
        return swept

    def _sweep_startup_edit_sessions(self) -> None:
        """进程启动时的全量收割；仓库解析不了就跳过，绝不阻塞启动。"""
        for review_dir in sorted(self.root.glob("*/edit-sessions")):
            rt_stub = ReviewRuntime(review_dir.parent.name, review_dir.parent)
            request_path = review_dir.parent / "request.json"
            try:
                request = _read_json(request_path)
                repo_id = str((request.get("source") or {}).get("repo_id") or "")
                repo = self.registry.get(repo_id)
            except (IntakeError, OSError):
                repo = None
            try:
                self._sweep_orphan_edit_sessions(rt_stub, repo)
            except IntakeError:
                # 收集期的快照漂移由下一次显式操作 fail-closed 兜底；
                # 启动路径不能因为一个旧审查把整个服务拖死。
                continue
    def _find_reverification(self, rt: ReviewRuntime,
                             reverification_id: str) -> dict:
        if not isinstance(reverification_id, str) or not reverification_id:
            raise IntakeError("REVERIFICATION_NOT_FOUND",
                              "reverification_id required")
        record = next((item for item in
                       _reverification_records(rt.review_dir)
                       if item.get("reverification_id") == reverification_id),
                      None)
        if record is None:
            raise IntakeError("REVERIFICATION_NOT_FOUND", reverification_id)
        return record

    def _run_reverification_locked(self, rt: ReviewRuntime,
                                   record: dict) -> dict:
        """按质疑原因分派策略并结算；异常路径一律扣留原主张。"""
        with self._lock:
            current = self._find_reverification(
                rt, record["reverification_id"])
            if current["status"] not in ("approved", "replaying"):
                return current
            current = dict(current)
            current["status"] = "replaying"
            current["updated_at"] = record_ts()
            _atomic_json(rt.review_dir / "reverifications" / f'{current["reverification_id"]}.json', current)
        strategy = reverification.strategy_for(str(record.get("reason")))
        ctx = _ReplayStrategyContext(self, rt)
        result: reverification.StrategyOutcome | None = None
        failure = ""
        try:
            result = strategy.run(ctx, record)
        except IntakeError as exc:
            if exc.code == "REVERIFICATION_ORIGINAL_VECTOR_MISSING":
                # 账本里拿不到原观测向量：唯一诚实的结论是扣留，且
                # 原因要写明"没证据"，绝不能落进"向量不等 → 修订"
                # 的分支把没证据说成被推翻。
                failure = "original_vector_missing"
            else:
                # R16 的教训：异常塌缩成类名会把真因一起埋掉。这里
                # 保留稳定错误码，扣留记录才能对着口径追责。
                failure = f"{exc.code}: {exc}"[:300]
        except Exception as exc:  # 预算、恢复、环境——失败只能扣留
            failure = f"{type(exc).__name__}: {exc}"[:300]
        with self._lock:
            settled = dict(current)
            settled["updated_at"] = record_ts()
            for event in (result.replay_events if result is not None else []):
                _append_annotation_event(rt.review_dir, "experiment.replayed",
                                         event)
            # 结算统一走单点判据表：确认 / 修订 / 扣留只由策略结果决定，
            # 恢复不净或拿不到答案在这里永远只能扣留。
            settled["claim_revision"] = _next_claim_revision(
                rt.review_dir, record["claim_id"])
            if result is None:
                settled["status"] = "failed"
                settled["outcome"] = "claim.withheld"
                settled["outcome_reason"] = failure
            else:
                settled["status"] = "settled"
                settled["outcome"] = result.outcome
                settled["outcome_reason"] = result.outcome_reason
                settled["replay_observations"] = result.replay_observations
                settled["answered"] = result.answered
                settled["open"] = list(result.open_items)
                settled["evidence_delta"] = result.evidence_delta
                settled["needs_human"] = result.needs_human
                settled["experiments_added"] = result.experiments_added
            event_name = {"claim.confirmed": "claim.confirmed",
                          "claim.revised": "claim.revised",
                          "claim.withheld": "claim.withheld"}[settled["outcome"]]
            event_payload = {
                "reverification_id": record["reverification_id"],
                "claim_id": record["claim_id"],
                "claim_revision": settled["claim_revision"],
                "outcome_reason": settled["outcome_reason"],
            }
            if settled["outcome"] == "claim.revised":
                # 不变量：修订以新版本改变结论，事件必须带上证据增量。
                delta = (result.evidence_delta if result is not None else None) or {}
                event_payload["evidence_delta"] = delta
                event_payload["divergent_experiments"] = delta.get(
                    "divergent_experiments") or []
            _append_annotation_event(rt.review_dir, event_name, event_payload)
            visa_delta = ((result.evidence_delta
                           if result is not None else None) or {})
            if (settled["outcome"] == "claim.confirmed"
                    and visa_delta.get("new_claim")):
                # 策略 4 红线：签证 PASS 不升级原主张——原主张以
                # confirmed 维持原判，这里只追加一条受保护的新主张。
                # 追加不是升级，与 ensure_no_promotion 不冲突。
                _append_annotation_event(rt.review_dir,
                                         "claim.protected.added", {
                     "reverification_id": record["reverification_id"],
                     "claim_id": record["claim_id"],
                     "new_claim_id": (visa_delta.get("new_claim_id")
                                      or reverification.protected_claim_id(
                                          record["reverification_id"])),
                     "text": visa_delta["new_claim"],
                     "visa_status": str(visa_delta.get("visa_status") or ""),
                     "original_claim_grade_unchanged": bool(
                         visa_delta.get("original_claim_grade_unchanged")),
                 })
            _atomic_json(rt.review_dir / "reverifications" / f'{record["reverification_id"]}.json', settled)
            return settled

    def _claim_replayable_experiments(self, rt: ReviewRuntime,
                                      claim_id: str) -> list[dict]:
        """找到这条主张所凭、且账本里真实存在的实验记录。

        三条封闭路径：claim_id 本身是实验记录、是账本 Claim（从
        provenance 反查）、或是渲染模型的 unit_id（按锚点行区间匹配
        实验）。都拿不出实验记录就返回空——没有可重放的实验就没有复验。

        三条路径都套用同一条资格闸门（_terminal_complete_experiments）：
        只重放 COMPLETE 终端实验、干预非空；且最多重放一条（cap 1，
        RequiredByTest 主张的 provenance 首位就是探测策略选中的终局
        实验）。复验预算有上界，逐条多跑不改变"确认/修订"的判据。
        """
        rows = _review_ledger_rows(rt.review_dir, strict=False)
        experiments = {row["record_id"]: row
                       for row in rows
                       if row.get("record_type") == "Experiment"}
        usable = {row["record_id"]
                  for row in _terminal_complete_experiments(experiments)}
        if rows:
            if claim_id in experiments:
                return [experiments[claim_id]] if claim_id in usable else []
            for row in rows:
                if (row.get("record_type") == "Claim"
                        and row.get("record_id") == claim_id):
                    wanted = [pid for pid in
                              (row.get("payload") or {}).get("provenance") or []
                              if pid in usable]
                    if wanted:
                        return [experiments[pid] for pid in wanted][:1]
            # 渲染模型的 unit_id：按锚点行区间把单元匹配回实验。
            unit = _render_unit(rt.review_dir, claim_id)
            if unit is not None:
                loc = unit.get("location") or {}
                path, start, end = (str(loc.get("file") or ""),
                                    int(loc.get("start") or 0),
                                    int(loc.get("end") or 0))
                matched = [row for row in experiments.values()
                           if row["record_id"] in usable
                           and _anchor_overlaps(row, path, start, end)]
                if matched:
                    return sorted(matched,
                                  key=lambda row: row["record_id"])[:1]
        return []

    def _review_declared_tests(self, rt: ReviewRuntime) -> list[str]:
        """复验计划里声明的测试范围：只能重跑原审查冻结过的范围。"""
        request = rt.request or _read_json(rt.review_dir / "request.json")
        return [str(item) for item in request.get("test_files") or []]

    def _replay_claim_experiments(self, rt: ReviewRuntime,
                                  record: dict) -> list[dict]:
        """在隔离 worktree 里逐条重放干预，然后逐项对比观测向量。

        原则与主审查完全一致：干预 → 观测 → 一律还原；还原不干净
        立即停止后续重放。测试范围 = 原基线向量的键（声明 nodeid），
        跑法与主审查同一条管线（Executor + pytest_argv + JUnit XML
        → TestVector），浏览器永远无权指定。
        """
        rt2, repo, snapshot_sha = self._source_context(rt.review_id)
        ledger_rows = _review_ledger_rows(rt2.review_dir, strict=False)
        planned = record["plan"]["replay_experiments"]
        originals = {item["experiment_id"]:
                     _review_experiment_row(rt2.review_dir,
                                            item["experiment_id"])
                     for item in planned}
        # 声明范围在原基线向量里冻结。一次审查只有一个基线；各实验
        # 的声明范围不一致说明账本被污染，fail closed。
        nodeids: list[str] = []
        for item in planned:
            declared = _experiment_declared_nodeids(
                originals[item["experiment_id"]], ledger_rows)
            if nodeids and declared != nodeids:
                raise IntakeError("REVERIFICATION_SCOPE_CONFLICT",
                                  item["experiment_id"])
            nodeids = declared
        runner = _ReverificationRunner(repo=repo.path, python=repo.python,
                                       nodeids=nodeids)
        try:
            with execution_scope(self._replay_executor(rt2, runner)):
                baseline = runner.run_baseline()
                observations: list[dict] = []
                for item in planned:
                    original = originals[item["experiment_id"]]
                    replayed = runner.replay(item["intervention"])
                    original_vector = _experiment_statuses(original,
                                                           ledger_rows)
                    vector = replayed["vector"]
                    observations.append({
                        "experiment_id": item["experiment_id"],
                        "replay_experiment_id": replayed["run_id"],
                        "intervention": item["intervention"],
                        "baseline_green": baseline.all_passed,
                        "statuses": [[t, s.value]
                                     for t, s in vector.statuses],
                        "vector_matches":
                            vector.identical_to(original_vector),
                        "restored_clean": replayed["restored_clean"],
                    })
                return observations
        finally:
            runner.close()

    def _replay_executor(self, rt: ReviewRuntime,
                         runner: "_ReverificationRunner"):
        """复验走与原审查同一档 Executor：配置是沙箱就沙箱。"""
        process_record = (rt.review_dir / "reverifications"
                          / "runtime_process.json")
        if self.execution_mode is ExecutionMode.SANDBOXED:
            return SandboxedExecutor(runner.sandbox_dir,
                                     process_record=process_record)
        return TrustedLocalExecutor(process_record=process_record)

    def _comment_repo_best_effort(self, rt: ReviewRuntime) -> RegisteredRepo | None:
        """列表期的仓库尽力解析：解析失败不阻塞离线翻阅评论。"""
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo_id = str((request.get("source") or {}).get("repo_id") or "")
        if not repo_id:
            return None
        try:
            return self.registry.get(repo_id)
        except IntakeError:
            return None

    def _resolve_review_repo(self, request: dict) -> RegisteredRepo:
        """Resolve the repository a review is bound to, across process boundaries.

        repo_id 是创建进程的随机令牌：同进程创建的审查直接命中第一分支。
        跨进程（审查由 Cockpit 或预跑脚本跑完、MCP 面只继承 reviews-root）
        时改按审查落盘的源快照在 --allow-repo 白名单里找内容一致的仓库。
        错误口径沿用既有的两个：HEAD 对得上但内容变了报
        SOURCE_SNAPSHOT_CHANGED；白名单里根本没有那个仓库报 UNKNOWN_REPO_ID。
        提议测试与自主写入两条面共用这一份解析，不许各写一份。
        """
        try:
            return self.registry.get(
                str((request.get("source") or {}).get("repo_id") or ""))
        except IntakeError:
            expected = request.get("source_snapshot") or {}
            head = str(expected.get("head") or "")
            digest = str(expected.get("snapshot_sha256") or "")
            if not head or not digest:
                raise
            head_seen = False
            for repo in self.registry.registered():
                current = _repo_snapshot(repo.path)
                if current.get("head") != head:
                    continue
                head_seen = True
                if current.get("snapshot_sha256") == digest:
                    return repo
            if head_seen:
                raise IntakeError(
                    "SOURCE_SNAPSHOT_CHANGED",
                    "仓库在审查后发生变化，不能基于旧证据继续写")
            raise IntakeError("UNKNOWN_REPO_ID",
                              "no allow-listed repository matches the reviewed snapshot")

    def deliver_repair(self, review_id: str, patch: str, *,
                       idempotency_key: str = "",
                       session_id: str | None = None,
                       intent: str = "") -> dict:
        """Verify and deliver an explicitly supplied patch to a local branch.

        This is deliberately a patch *delivery* boundary, not a model patch
        generator. The caller must opt in to repair delivery in the v2 spec;
        the verifier runs the declared tests in an isolated worktree before
        Git is allowed to create a commit.

        可选的 session_id 把这次交付绑进一个编辑会话：交付前重比会话
        冻结的源快照（变了置 stale 并失败关闭）、交付前预检磁盘、
        每个候选差异（含被拒的）都追加进注释链账本。
        """
        patch_digest = hashlib.sha256(str(patch).encode("utf-8")).hexdigest()
        payload = {"review_id": review_id, "action": "deliver_repair",
                   "patch_sha256": patch_digest,
                   "session_id": session_id}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID", "evidence bundle did not verify")
        session_record: dict | None = None
        if session_id is not None:
            with self._lock:
                session_record = self._find_edit_session(rt, session_id)
                if str(session_record.get("status")) not in {
                        edit_sessions.OPEN, edit_sessions.PATCH_CANDIDATE}:
                    raise IntakeError("EDIT_SESSION_STATE_INVALID",
                                      f"session is {session_record.get('status')}; "
                                      "only an open session can take a candidate")
        spec = self._repair_delivery_spec(rt)
        if not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED", "计划未授权创建本地修复分支")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        if session_record is not None:
            # 会话级源快照绑定：每次 apply 之前重比冻结快照。语义与
            # SourceChangedStrategy 完全一致——只比哈希，不猜"看起来
            # 差不多"；用户树动了就置 stale 并 fail-closed。
            frozen = str(session_record.get("snapshot_sha256") or "")
            observed = str(_repo_snapshot(repo.path).get("snapshot_sha256") or "")
            if not frozen or observed != frozen:
                with self._lock:
                    self._transition_edit_session(
                        rt, session_record, edit_sessions.STALE,
                        reason=edit_sessions.EXIT_REASONS["stale"])
                raise IntakeError("SOURCE_SNAPSHOT_CHANGED",
                                  "用户树在会话开启后发生变化；会话已置 stale，"
                                  "冻结补丁拒绝交付")
            # 幂等键 = sha256(patch)：会话里已有同一补丁的结局就原样
            # 返回首次结果，不重跑验证、不重开工作树。
            for entry in session_record.get("candidates") or []:
                if entry.get("patch_sha256") != patch_digest:
                    continue
                if entry.get("outcome_kind") == "delivered":
                    return dict(entry.get("response") or {})
                if entry.get("outcome_kind") == "rejected":
                    raise IntakeError(str(entry.get("error_code")),
                                      str(entry.get("error_detail")))
            _ensure_disk_space(rt.review_dir)
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo.path)
        if (expected_snapshot.get("head") != current_snapshot.get("head") or
                expected_snapshot.get("snapshot_sha256") != current_snapshot.get("snapshot_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED", "仓库在审查后发生变化，不能交付旧证据对应的补丁")
        plan = rt.frozen_plan.as_dict() if rt.frozen_plan is not None else _read_json(rt.review_dir / "plan.frozen.json")
        plan_sha256 = str(plan.get("plan_sha256") or "")
        if not plan_sha256:
            raise IntakeError("REPAIR_PLAN_MISSING", "没有可验证的冻结计划")
        manifest_holder: dict[str, dict] = {}
        verification_holder: dict[str, str] = {}
        test_files = tuple(request.get("test_files") or ())
        budget = float(request.get("budget_seconds") or 300)
        if session_record is not None:
            with self._lock:
                if str(session_record.get("status")) == edit_sessions.OPEN:
                    session_record = self._transition_edit_session(
                        rt, session_record, edit_sessions.PATCH_CANDIDATE,
                        reason=f"candidate:{patch_digest[:12]}")
                else:
                    session_record = self._find_edit_session(rt, str(session_id))
                self._append_session_candidate(
                    rt, str(session_id), patch_digest=patch_digest,
                    intent=intent, verdict="pending", test_result="not_run")

        def verifier(worktree: Path) -> RepairVerification:
            executor = (SandboxedExecutor(
                            worktree.parent / "sandbox",
                            process_record=rt.review_dir / "repair" / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=rt.review_dir / "repair" / "runtime_process.json"))
            env = sanitized_environment({"PYTHONWARNINGS": "ignore"})
            result = executor.run(
                [repo.python, "-m", "pytest", *test_files, "-q"],
                cwd=worktree, timeout=budget, env=env)
            passed = result.returncode == 0
            verification_holder["summary"] = (
                "declared tests passed" if passed
                else f"declared tests failed (exit {result.returncode})")
            if session_record is not None and passed:
                # 真实崩溃窗口：验证已过、提交未落。先把会话推进到
                # awaiting_delivery，中途强杀也能从账本看出卡在哪。
                with self._lock:
                    refreshed = self._find_edit_session(rt, str(session_id))
                    if str(refreshed.get("status")) == edit_sessions.PATCH_CANDIDATE:
                        refreshed = self._transition_edit_session(
                            rt, refreshed, edit_sessions.VERIFIED,
                            reason="declared tests passed")
                    if str(refreshed.get("status")) == edit_sessions.VERIFIED:
                        self._transition_edit_session(
                            rt, refreshed, edit_sessions.AWAITING_DELIVERY,
                            reason="commit_pending")
            return RepairVerification(
                passed=passed, test_scope=test_files,
                summary=verification_holder["summary"])

        def build_manifest(verification: RepairVerification) -> str:
            manifest = EvidenceManifest(
                review_id=review_id, plan_sha256=plan_sha256,
                source_commit=str(expected_snapshot.get("head") or ""),
                patch_sha256=patch_digest, test_scope=verification.test_scope,
                test_results={"passed": verification.passed,
                              "summary": verification.summary},
                restore_clean=True, generated_test_ids=(),
                tool_commit=_tool_commit(),
            )
            raw = manifest.as_dict()
            problems = verify_manifest(raw, manifest.sha256)
            if problems:
                raise RepairError("REPAIR_MANIFEST_INVALID", "; ".join(problems))
            manifest_holder["manifest"] = raw
            return manifest.sha256

        fingerprint = self._repo_fingerprint(repo)
        try:
            repair_lease = self._leases.acquire(
                repo_fingerprint=fingerprint, review_id=review_id,
                operation="repair", plan_sha256=plan_sha256,
                ttl_seconds=self._lease_ttl_seconds(budget))
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        rt.events.append("repository.lease_acquired", {
            "operation": "repair", "repo_fingerprint": fingerprint,
            "lease_id": repair_lease.lease_id})
        try:
            try:
                delivery = deliver_verified_patch(
                    repo=repo.path, review_id=review_id, plan_sha256=plan_sha256,
                    patch=patch, verifier=verifier,
                    allowed_paths=tuple(spec.scope.include),
                    max_files=spec.scope.max_modified_files,
                    max_changed_lines=spec.scope.max_changed_lines,
                    scratch_root=rt.review_dir / "repair" / "worktrees",
                    evidence_manifest_builder=build_manifest)
            except RepairError as exc:
                if session_record is not None:
                    with self._lock:
                        self._append_session_candidate(
                            rt, str(session_id), patch_digest=patch_digest,
                            intent=intent, verdict=f"rejected:{exc.code}",
                            test_result=verification_holder.get(
                                "summary", "not_run"),
                            outcome_kind="rejected", error_code=exc.code,
                            error_detail=exc.detail)
                raise IntakeError(exc.code, exc.detail) from exc
        finally:
            self._leases.release(repair_lease)
            rt.events.append("repository.lease_released", {
                "operation": "repair", "lease_id": repair_lease.lease_id})
        manifest = manifest_holder.get("manifest")
        if not manifest:
            raise IntakeError("REPAIR_MANIFEST_MISSING", "提交前证据清单未生成")
        repair_dir = rt.review_dir / "repair"
        repair_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(repair_dir / "evidence_manifest.json", manifest)
        _atomic_json(repair_dir / "delivery_record.json", delivery.delivery_record or {})
        _atomic_text(repair_dir / "patch.diff", patch)
        rt.events.append("repair.delivered", {
            "branch": delivery.branch, "commit": delivery.commit,
            "evidence_manifest_sha256": delivery.evidence_manifest_sha256,
            "patch_sha256": delivery.patch_sha256,
        })
        bundle_path = rt.review_dir / "review_bundle.json"
        if bundle_path.exists():
            bundle = _read_json(bundle_path)
            bundle["repair"] = {
                "status": "DELIVERED",
                "evidence_manifest_sha256": delivery.evidence_manifest_sha256,
                "delivery_record": delivery.delivery_record or {},
            }
            bundle["events"] = [event.as_dict() for event in rt.events.all()]
            bundle["integrity"]["event_count"] = len(bundle["events"])
            bundle["integrity"]["event_chain_sha256"] = content_sha256(bundle["events"])
            bundle["integrity"]["payload_sha256"] = payload_digest(bundle)
            _atomic_json(bundle_path, bundle)
        response = self.describe(review_id)
        if session_record is not None:
            with self._lock:
                self._append_session_candidate(
                    rt, str(session_id), patch_digest=patch_digest,
                    intent=intent, verdict="accepted",
                    test_result=verification_holder.get("summary", ""),
                    outcome_kind="delivered", response=response,
                    delivery={
                        "branch": delivery.branch, "commit": delivery.commit,
                        "evidence_manifest_sha256":
                            delivery.evidence_manifest_sha256,
                        "patch_sha256": delivery.patch_sha256})
                refreshed = self._find_edit_session(rt, str(session_id))
                self._transition_edit_session(
                    rt, refreshed, edit_sessions.DELIVERED,
                    reason=edit_sessions.EXIT_REASONS["delivered"])
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def _append_session_candidate(self, rt: ReviewRuntime, session_id: str, *,
                                  patch_digest: str, intent: str,
                                  verdict: str, test_result: str,
                                  outcome_kind: str = "", **extra) -> None:
        """逐条补丁入账：候选也算，追加进注释链既有哈希链。"""
        record = self._find_edit_session(rt, session_id)
        record = dict(record)
        entry: dict = {
            "patch_sha256": patch_digest, "intent": intent,
            "verdict": verdict, "test_result": test_result,
            "at": record_ts(),
        }
        if outcome_kind:
            entry["outcome_kind"] = outcome_kind
        entry.update(extra)
        record["candidates"] = list(record.get("candidates") or []) + [entry]
        record["updated_at"] = record_ts()
        _write_session_record(
            record, rt.review_dir / "edit-sessions" / f"{session_id}.json")
        event_data = {key: entry[key] for key in
                      ("patch_sha256", "intent", "verdict", "test_result",
                       "at") if key in entry}
        if outcome_kind:
            event_data["outcome_kind"] = outcome_kind
        event_data["session_id"] = session_id
        _append_annotation_event(rt.review_dir, "repair.candidate.recorded",
                                 event_data)

    # ── M2：五指纹批准仪式与受控交付 ────────────────────────────
    # deliver_verified_patch 仍是唯一写 Git 的边界；这里在它交付完成后
    # 加一道出库闸：批准冻结五指纹基线，导出前五个全部重算、逐一
    # 比对，任何一对不上就拒绝交付。原则搬自 tools/verify_release_evidence.py
    # 的"重算，不采信"。

    def _repair_delivery_spec(self, rt: ReviewRuntime) -> ReviewSpec:
        """与 deliver_repair 同一口径解析授权 spec，别无第二份语义。"""
        spec = self._review_spec(rt)
        if spec is None:
            raise IntakeError("REPAIR_NOT_AUTHORIZED",
                              "repair requires a v2 authorization")
        return spec

    def _delivered_session(self, rt: ReviewRuntime, session_id: str) -> dict:
        """仪式会话闸：只认账本里走完 awaiting_delivery → delivered 的会话。

        这是"交付必须走 M1 会话状态机、不绕开账本"的机器化：没有
        会话的交付没有仪式入口；会话没走完整条链的同样没有。
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise IntakeError("DELIVERY_SESSION_REQUIRED",
                              "session_id is required for the delivery ceremony")
        record = self._find_edit_session(rt, session_id)
        if str(record.get("status")) != edit_sessions.DELIVERED:
            raise IntakeError(
                "DELIVERY_SESSION_NOT_DELIVERED",
                f"session is {record.get('status') or 'unknown'}; the ceremony "
                "requires a session that reached delivered")
        steps = [(str(step.get("from")), str(step.get("to")))
                 for step in record.get("transitions") or []]
        for src, dst in (("verified", "awaiting_delivery"),
                         ("awaiting_delivery", "delivered")):
            if (src, dst) not in steps:
                raise IntakeError(
                    "DELIVERY_SESSION_NOT_DELIVERED",
                    f"session ledger is missing the {src} -> {dst} transition")
        return record

    def _recompute_five_fingerprints(self, rt: ReviewRuntime, repo,
                                     spec: ReviewSpec) -> tuple[dict, dict]:
        """批准与交付共用的唯一取指纹入口：全部从活体仓库与工件重算。

        权限扩张先于哈希比对被钉死：补丁文本重新过候选边界
        （allowed_paths/禁区前缀/封禁名单/规模上限），就算攻击者把
        批准记录里的哈希一起改写，语义拒绝依然独立成立。
        """
        repair_dir = rt.review_dir / "repair"
        paths = {"patch.diff": repair_dir / "patch.diff",
                 "evidence_manifest.json": repair_dir / "evidence_manifest.json",
                 "delivery_record.json": repair_dir / "delivery_record.json"}
        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            raise IntakeError("DELIVERY_ARTIFACT_MISSING", ", ".join(missing))
        patch_text = paths["patch.diff"].read_text(encoding="utf-8")
        try:
            validate_candidate_patch(
                patch_text,
                allowed_paths=tuple(spec.scope.include),
                max_files=spec.scope.max_modified_files,
                max_changed_lines=spec.scope.max_changed_lines)
        except RepairError as exc:
            raise IntakeError("DELIVERY_SCOPE_EXPANDED",
                              f"{exc.code}: {exc.detail}") from exc
        manifest = _read_json(paths["evidence_manifest.json"])
        problems = verify_manifest(manifest)
        if problems:
            raise IntakeError("DELIVERY_ARTIFACT_INVALID", "; ".join(problems))
        delivery_record = _read_json(paths["delivery_record.json"])
        branch = str(delivery_record.get("branch") or "")
        commit = str(delivery_record.get("commit") or "")
        if not branch or not delivery_ceremony.is_hex_oid(commit):
            raise IntakeError("DELIVERY_ARTIFACT_INVALID",
                              "delivery record is malformed")
        # 目标分支：从活体仓库重解 ref；分支没了或指到别处都是移动。
        resolved = run_git(["rev-parse", f"refs/heads/{branch}"],
                           cwd=repo.path, check=False)
        if resolved.returncode != 0:
            raise IntakeError(
                "DELIVERY_BRANCH_MOVED",
                f"target ref no longer resolves: refs/heads/{branch}")
        target_ref = resolved.stdout.strip()
        if target_ref != commit:
            raise IntakeError(
                "DELIVERY_BRANCH_MOVED",
                f"refs/heads/{branch} moved to {target_ref[:12]}; "
                f"delivery record says {commit[:12]}")
        snapshot = _repo_snapshot(repo.path)
        snapshot_sha256 = str(snapshot.get("snapshot_sha256") or "")
        if not snapshot_sha256:
            raise IntakeError("DELIVERY_SNAPSHOT_INVALID",
                              "repo snapshot could not be recomputed")
        five = delivery_ceremony.FiveFingerprints(
            snapshot_sha256=snapshot_sha256,
            patch_sha256=delivery_ceremony.patch_fingerprint(patch_text),
            test_sha256=delivery_ceremony.test_fingerprint(
                manifest.get("test_scope"), manifest.get("test_results")),
            evidence_manifest_sha256=delivery_ceremony.evidence_fingerprint(manifest),
            target_ref=target_ref)
        problems = five.problems()
        if problems:
            raise IntakeError("DELIVERY_ARTIFACT_INVALID", "; ".join(problems))
        return five.as_dict(), {"branch": branch, "commit": commit}

    def approve_delivery(self, review_id: str, session_id: str,
                         raw: dict) -> dict:
        """五指纹批准仪式：算一遍并冻结成比对基线，全程入账。

        批准不是信任的开始，只是比对的基线：交付侧五个全部重算
        （见 export_delivery_package），任何一对不上就拒绝交付。
        """
        if not isinstance(raw, dict) or set(raw) - {"note"}:
            raise IntakeError("DELIVERY_APPROVAL_REQUEST_INVALID",
                              "body accepts only an optional note")
        note = str(raw.get("note") or "")
        if len(note) > 200:
            raise IntakeError("DELIVERY_APPROVAL_REQUEST_INVALID",
                              "note must be at most 200 characters")
        rt = self._runtime(review_id)
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        spec = self._repair_delivery_spec(rt)
        with self._lock:
            record = self._delivered_session(rt, session_id)
            fingerprints, delivery = self._recompute_five_fingerprints(rt, repo, spec)
            try:
                approval = delivery_ceremony.build_approval_record(
                    review_id=review_id, session_id=session_id,
                    fingerprints=fingerprints,
                    branch=delivery["branch"], commit=delivery["commit"],
                    note=note, created_at=record_ts())
            except ValueError as exc:
                raise IntakeError("DELIVERY_APPROVAL_INVALID",
                                  str(exc)[:300]) from exc
            repair_dir = rt.review_dir / "repair"
            _ensure_disk_space(repair_dir)
            _atomic_json(repair_dir / "delivery_approval.json", approval)
            record = dict(record)
            record["delivery_approval"] = {
                "fingerprints": dict(fingerprints),
                "branch": delivery["branch"],
                "commit": delivery["commit"],
                "at": approval["created_at"],
            }
            record["updated_at"] = record_ts()
            _write_session_record(
                record, rt.review_dir / "edit-sessions" / f"{session_id}.json")
            _append_annotation_event(rt.review_dir, "repair.delivery_approved", {
                "session_id": session_id,
                "fingerprints": dict(fingerprints),
                "branch": delivery["branch"], "commit": delivery["commit"],
            })
        return {"status": "APPROVED", "approval": approval}

    def export_delivery_package(self, review_id: str, session_id: str,
                                raw: dict | None = None) -> dict:
        """受控交付：五指纹重算、逐一比对、全对才落导出包；绝不 push。

        红线：绝不采信批准记录里的值——每个指纹都从活体仓库与工件
        重算，批准记录（连同把它钉进注释链的事件）只提供比对基线。
        导出物是内容哈希清单，非安全签署。
        """
        raw = raw or {}
        if not isinstance(raw, dict) or raw:
            raise IntakeError("DELIVERY_EXPORT_REQUEST_INVALID",
                              "export takes no request fields")
        rt = self._runtime(review_id)
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self.registry.get(str((request.get("source") or {}).get("repo_id") or ""))
        spec = self._repair_delivery_spec(rt)
        with self._lock:
            self._delivered_session(rt, session_id)
            approval_path = rt.review_dir / "repair" / "delivery_approval.json"
            if not approval_path.is_file():
                raise IntakeError("DELIVERY_NOT_APPROVED",
                                  "approve_delivery must run before export")
            approval = _read_json(approval_path)
            problems = delivery_ceremony.approval_problems(approval)
            if (str(approval.get("session_id") or "") != session_id
                    or str(approval.get("review_id") or "") != review_id):
                problems.append("approval belongs to another review/session")
            chained = _latest_delivery_approval_event(rt.review_dir, session_id)
            if chained is None:
                problems.append("approval is not pinned in the annotation chain")
            if problems:
                raise IntakeError("DELIVERY_APPROVAL_INVALID", "; ".join(problems))
            approved = dict(approval.get("fingerprints") or {})
            # 红线：重算，不采信。批准文件还要与注释链里的钉子一致，
            # 单改文件不改链（或反过来）都在这里失败关闭。
            if (chained.get("fingerprints") != approved
                    or str(chained.get("branch") or "") != str(approval.get("branch") or "")
                    or str(chained.get("commit") or "") != str(approval.get("commit") or "")):
                raise IntakeError("DELIVERY_APPROVAL_INVALID",
                                  "approval diverges from the annotation chain")
            fingerprints, delivery = self._recompute_five_fingerprints(rt, repo, spec)
            mismatches = delivery_ceremony.fingerprint_mismatches(approved, fingerprints)
            if mismatches:
                field = mismatches[0]
                raise IntakeError(
                    delivery_ceremony.MISMATCH_CODES[field],
                    f"{field} recomputed {str(fingerprints.get(field))[:16]} "
                    f"but the approval froze {str(approved.get(field))[:16]}")
            repair_dir = rt.review_dir / "repair"
            export_dir = repair_dir / "export"
            # disk_usage 需要目录已存在；空目录本身不构成导出。
            export_dir.mkdir(parents=True, exist_ok=True)
            _ensure_disk_space(export_dir)
            before_status = run_git(
                ["status", "--porcelain=v1"], cwd=repo.path).stdout.strip()
            payloads = {
                "patch.diff": (repair_dir / "patch.diff").read_bytes(),
                "delivery_record.json": (repair_dir / "delivery_record.json").read_bytes(),
                "evidence_manifest.json": (repair_dir / "evidence_manifest.json").read_bytes(),
            }
            file_hashes = [{"path": name,
                            "sha256": hashlib.sha256(data).hexdigest()}
                           for name, data in sorted(payloads.items())]
            manifest = delivery_ceremony.build_export_manifest(
                review_id=review_id, session_id=session_id,
                fingerprints=fingerprints,
                branch=delivery["branch"], commit=delivery["commit"],
                file_hashes=file_hashes, created_at=record_ts())
            manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            try:
                delivery_ceremony.assert_export_wording(manifest_text)
            except ValueError as exc:
                raise IntakeError("DELIVERY_WORDING_INVALID", str(exc)) from exc
            for name, data in payloads.items():
                _atomic_bytes(export_dir / name, data)
            _atomic_bytes(export_dir / "content_hash_manifest.json",
                          manifest_text.encode("utf-8"))
            # 受控交付只写审查目录；用户 checkout 必须字节不动。
            _assert_original_unchanged(repo.path, before_status)
            manifest_sha256 = hashlib.sha256(
                manifest_text.encode("utf-8")).hexdigest()
            _append_annotation_event(rt.review_dir, "repair.delivery_exported", {
                "session_id": session_id,
                "package_dir": str(export_dir),
                "files": sorted([*payloads, "content_hash_manifest.json"]),
                "manifest_sha256": manifest_sha256,
                "fingerprints": dict(fingerprints),
            })
            record = dict(self._find_edit_session(rt, session_id))
            record["delivery_export"] = {
                "package_dir": str(export_dir),
                "manifest_sha256": manifest_sha256,
                "at": record_ts(),
            }
            record["updated_at"] = record_ts()
            _write_session_record(
                record, rt.review_dir / "edit-sessions" / f"{session_id}.json")
        return {"status": "EXPORTED", "package_dir": str(export_dir),
                "manifest": manifest}

    def generate_repair(self, review_id: str, raw: dict, *,
                        idempotency_key: str = "") -> dict:
        """Generate a bounded candidate only; application and commit stay separate."""
        if not isinstance(raw, dict) or set(raw) != {"finding_ids", "snippets"}:
            raise IntakeError("REPAIR_CONTEXT_INVALID", "finding_ids and snippets are required")
        if self.provider is None:
            raise IntakeError("MODEL_PROVIDER_UNAVAILABLE", "repair generation requires a configured provider")
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID", "evidence bundle did not verify")
        spec = self._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED", "repair generation was not approved")
        if "selected_snippets" not in spec.data_policy.model_data_categories:
            raise IntakeError("REPAIR_DATA_NOT_AUTHORIZED", "selected_snippets was not approved for model transfer")
        plan = rt.frozen_plan.as_dict() if rt.frozen_plan else _read_json(rt.review_dir / "plan.frozen.json")
        context = RepairContext(
            review_id, str(plan.get("plan_sha256") or ""),
            tuple(raw.get("finding_ids") or ()), tuple(spec.scope.include),
            tuple(raw.get("snippets") or ()), spec.scope.max_modified_files,
            spec.scope.max_changed_lines)
        payload = {"review_id": review_id, "action": "generate_repair",
                   "context_sha256": hashlib.sha256(json.dumps(
                       context.safe_payload(), ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        self._assert_write_budget(rt)
        try:
            candidate = generate_candidate(self.provider, context)
        except RepairError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        directory = rt.review_dir / "repair"
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_text(directory / "candidate.patch", candidate.patch)
        record = {**candidate.public_dict(), "status": "PROPOSED",
                  "retention_days": 7}
        _atomic_json(directory / "candidate.json", record)
        rt.events.append("repair.candidate_proposed", record)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def verify_repair_candidate(self, review_id: str, *,
                                session_id: str | None = None,
                                idempotency_key: str = "") -> dict:
        """Read the proposed candidate back and verify it in isolation.

        This closes the loop generate_repair left open: candidate.patch was
        written but nothing ever read it back.  The autonomous write path
        stops at "verified" — the candidate is applied only inside a
        throwaway worktree, the declared tests run there, and a bound edit
        session advances open -> patch_candidate -> verified.  The third hop
        to awaiting_delivery (branch + commit) stays a human-triggered
        deliver_repair call.
        """
        rt = self._runtime(review_id)
        if self.agent_level is not AgentLevel.L3:
            raise IntakeError("L3_REQUIRED",
                              "autonomous candidate verification requires "
                              "server-bound L3")
        matched, reason = self._standing_authorization_match(rt)
        if matched is None:
            raise IntakeError("STANDING_AUTHORIZATION_REQUIRED", reason)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE",
                              rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID",
                              "evidence bundle did not verify")
        spec = self._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED",
                              "repair verification was not approved")
        repair_dir = rt.review_dir / "repair"
        patch_path = repair_dir / "candidate.patch"
        if not patch_path.is_file():
            raise IntakeError("REPAIR_CANDIDATE_MISSING",
                              "no proposed candidate to verify; generate one first")
        try:
            patch = patch_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IntakeError("REPAIR_CANDIDATE_MISSING", str(exc)) from exc
        patch_digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        payload = {"review_id": review_id, "action": "verify_repair_candidate",
                   "patch_sha256": patch_digest, "session_id": session_id}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        self._assert_write_budget(rt)
        request = rt.request or _read_json(rt.review_dir / "request.json")
        # 跨进程解析：长跑中途重启后 repo_id 这个随机令牌已经没用了，
        # 只能按审查落盘的源快照在允许清单里找回同一个仓库。
        repo = self._resolve_review_repo(request)
        session_record: dict | None = None
        if session_id is not None:
            with self._lock:
                session_record = self._find_edit_session(rt, session_id)
                if str(session_record.get("status")) not in {
                        edit_sessions.OPEN, edit_sessions.PATCH_CANDIDATE}:
                    raise IntakeError("EDIT_SESSION_STATE_INVALID",
                                      f"session is {session_record.get('status')}; "
                                      "only an open session can take a candidate")
                frozen = str(session_record.get("snapshot_sha256") or "")
                observed = str(_repo_snapshot(repo.path).get("snapshot_sha256") or "")
                if not frozen or observed != frozen:
                    self._transition_edit_session(
                        rt, session_record, edit_sessions.STALE,
                        reason=edit_sessions.EXIT_REASONS["stale"])
                    raise IntakeError("SOURCE_SNAPSHOT_CHANGED",
                                      "用户树在会话开启后发生变化；会话已置 "
                                      "stale，候选拒绝验证")
                for entry in session_record.get("candidates") or []:
                    if entry.get("patch_sha256") != patch_digest:
                        continue
                    if entry.get("outcome_kind") == "verified":
                        return dict(entry.get("response") or {})
                    if entry.get("outcome_kind") == "rejected":
                        raise IntakeError(str(entry.get("error_code")),
                                          str(entry.get("error_detail")))
                _ensure_disk_space(rt.review_dir)
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo.path)
        if (expected_snapshot.get("head") != current_snapshot.get("head") or
                expected_snapshot.get("snapshot_sha256") != current_snapshot.get("snapshot_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED",
                              "仓库在审查后发生变化，不能验证旧证据对应的补丁")
        try:
            candidate_paths = validate_candidate_patch(
                patch, allowed_paths=tuple(spec.scope.include),
                max_files=spec.scope.max_modified_files,
                max_changed_lines=spec.scope.max_changed_lines)
        except RepairError as exc:
            if session_record is not None:
                with self._lock:
                    self._append_session_candidate(
                        rt, str(session_id), patch_digest=patch_digest,
                        intent="verify", verdict=f"rejected:{exc.code}",
                        test_result="not_run", outcome_kind="rejected",
                        error_code=exc.code, error_detail=exc.detail)
            raise IntakeError(exc.code, exc.detail) from exc
        if session_record is not None:
            with self._lock:
                if str(session_record.get("status")) == edit_sessions.OPEN:
                    session_record = self._transition_edit_session(
                        rt, session_record, edit_sessions.PATCH_CANDIDATE,
                        reason=f"candidate:{patch_digest[:12]}")
                else:
                    session_record = self._find_edit_session(rt, str(session_id))
                self._append_session_candidate(
                    rt, str(session_id), patch_digest=patch_digest,
                    intent="verify", verdict="pending", test_result="not_run")
        plan = (rt.frozen_plan.as_dict() if rt.frozen_plan is not None
                else _read_json(rt.review_dir / "plan.frozen.json"))
        plan_sha256 = str(plan.get("plan_sha256") or "")
        if not plan_sha256:
            raise IntakeError("REPAIR_PLAN_MISSING", "没有可验证的冻结计划")
        test_files = tuple(request.get("test_files") or ())
        budget = float(request.get("budget_seconds") or 300)
        source_patch = run_git(["diff", "--binary", "HEAD"], cwd=repo.path,
                               check=True, timeout=30).stdout
        verification_holder = {"summary": "not_run", "passed": False}

        def _run_declared(worktree: Path) -> None:
            executor = (SandboxedExecutor(
                            worktree.parent / "sandbox",
                            process_record=repair_dir / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=repair_dir / "runtime_process.json"))
            env = sanitized_environment({"PYTHONWARNINGS": "ignore"})
            result = executor.run(
                [repo.python, "-m", "pytest", *test_files, "-q"],
                cwd=worktree, timeout=budget, env=env)
            passed = result.returncode == 0
            verification_holder["passed"] = passed
            verification_holder["summary"] = (
                "declared tests passed" if passed
                else f"declared tests failed (exit {result.returncode})")
            if passed and session_record is not None:
                # 两跳即止：open→patch_candidate 已在入账前完成，这里只走
                # patch_candidate→verified。awaiting_delivery 及其后的 commit
                # 永远留给人工触发的 deliver_repair。
                with self._lock:
                    refreshed = self._find_edit_session(rt, str(session_id))
                    if str(refreshed.get("status")) == edit_sessions.PATCH_CANDIDATE:
                        self._transition_edit_session(
                            rt, refreshed, edit_sessions.VERIFIED,
                            reason="declared tests passed")

        fingerprint = self._repo_fingerprint(repo)
        try:
            verify_lease = self._leases.acquire(
                repo_fingerprint=fingerprint, review_id=review_id,
                operation="verify", plan_sha256=plan_sha256,
                ttl_seconds=self._lease_ttl_seconds(budget))
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        rt.events.append("repository.lease_acquired", {
            "operation": "verify", "repo_fingerprint": fingerprint,
            "lease_id": verify_lease.lease_id})
        try:
            with isolated_worktree(
                    repo=repo.path, source_patch=source_patch,
                    extra_patches=(patch,),
                    scratch_root=repair_dir / "worktrees",
                    prefix="shuimu-verify-") as worktree:
                for rel in sorted(candidate_paths):
                    if (worktree / rel).is_file():
                        _syntax_gate(worktree, rel)
                _run_declared(worktree)
        finally:
            self._leases.release(verify_lease)
            rt.events.append("repository.lease_released", {
                "operation": "verify", "lease_id": verify_lease.lease_id})
        summary = str(verification_holder.get("summary") or "not_run")
        if not verification_holder.get("passed"):
            if session_record is not None:
                with self._lock:
                    self._append_session_candidate(
                        rt, str(session_id), patch_digest=patch_digest,
                        intent="verify", verdict="rejected:REPAIR_TESTS_FAILED",
                        test_result=summary, outcome_kind="rejected",
                        error_code="REPAIR_TESTS_FAILED", error_detail=summary)
            raise IntakeError("REPAIR_TESTS_FAILED", summary)
        candidate_meta = (_read_json(repair_dir / "candidate.json")
                          if (repair_dir / "candidate.json").is_file() else {})
        candidate_meta["status"] = "VERIFIED"
        candidate_meta["verification"] = {
            "test_scope": list(test_files), "passed": True, "summary": summary,
            "session_id": session_id or "", "verified_at": record_ts(),
        }
        _atomic_json(repair_dir / "candidate.json", candidate_meta)
        response = {
            "schema_version": REPAIR_CANDIDATE_SCHEMA_VERSION,
            "status": "VERIFIED",
            "patch_sha256": patch_digest,
            "session_id": session_id or "",
            "test_scope": list(test_files),
            "test_results": {"passed": True, "summary": summary},
            "verified_at": candidate_meta["verification"]["verified_at"],
        }
        rt.events.append("repair.candidate_verified", {
            "patch_sha256": patch_digest,
            "session_id": session_id or "",
            "test_scope": list(test_files),
            "summary": summary,
        })
        if session_record is not None:
            with self._lock:
                self._append_session_candidate(
                    rt, str(session_id), patch_digest=patch_digest,
                    intent="verify", verdict="verified", test_result=summary,
                    outcome_kind="verified", response=response)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, response)
        return response

    def propose_test(self, review_id: str, raw: dict, *,
                     idempotency_key: str = "",
                     revise_on_ineffective: bool = True) -> dict:
        """为无据/未标新增行提议一条测试：挣到签证才算有效补测。

        语义是 propose → 隔离执行 → 读失败摘录 → revise（最多 2 轮）→
        仍不绿就结构化 NEEDS_HUMAN；跑绿只是闸 1，随后过三段签证
        （docs/holdout-protocol.md）：PASSED_NOT_EFFECTIVE 用生成集反馈
        驱动修订，发送前强制第 0 条泄漏检查，保留集对模型全程不可见。
        模型给出的 code 在挣到签证之前只是提案文本：整个流程只读用户
        checkout，测试文件只存在于 throwaway 工作树，Git 永不为其建
        commit。

        ``revise_on_ineffective`` 关掉时，候选跑绿即停：签证照算、照落档，
        但不会为 PASSED_NOT_EFFECTIVE 再花一轮模型调用。这是「要不要为无效
        候选再付一次」的真实取舍，也正是 M5 里 B 组与 C 组的分界
        （docs/m5-abc-evaluation.md）——两组的计分仍走同一个 verify_candidate。
        """
        # 仓库解析放在方法前部独立成 _resolve_review_repo：同进程创建
        # 的审查直接按 repo_id 命中；跨进程（审查由别的进程跑完）时
        # repo_id 是对方进程的随机令牌，改用审查落盘的源快照在白名单里
        # 找回内容一致的那个仓库。白名单语义不变——没登记的仓库永远不
        # 会被执行任何代码。
        if (not isinstance(raw, dict) or set(raw) != {"finding_ids"}
                or not isinstance(raw.get("finding_ids"), list)
                or len(raw["finding_ids"]) > 20
                or not all(isinstance(x, str) and x.strip()
                           for x in raw["finding_ids"])):
            raise IntakeError("TEST_PROPOSAL_CONTEXT_INVALID",
                              "finding_ids must be a list of at most 20 refs")
        if self.provider is None:
            raise IntakeError("MODEL_PROVIDER_UNAVAILABLE",
                              "test proposal requires a configured provider")
        requested = list(dict.fromkeys(raw["finding_ids"]))
        rt = self._runtime(review_id)
        # 跨进程恢复的运行时没有内存 request：先从盘上补齐，产品模式
        # 闸门和后面的 _gate_model_path 看的必须是同一份落盘事实。
        request = rt.request or _read_json(rt.review_dir / "request.json")
        if not rt.request:
            rt.request = request
        # 补测提案本身就是一次模型调用。标准审查承诺零模型参与：入口
        # 就用稳定错误码拒绝，而不是等模型路径门卫抛 INTERNAL_ERROR。
        if str(request.get("product_mode") or "standard") == "standard":
            raise IntakeError(
                "STANDARD_MODE_MODEL_FORBIDDEN",
                "标准审查承诺零模型参与；补测提案需要智能体审查")
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE, ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE", rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID", "evidence bundle did not verify")
        # 授权闸读的是审查创建时落盘的 review_spec，不是创建进程的内存：
        # 真机接入里审查往往由另一个进程（Cockpit/预跑脚本）跑完，MCP 面
        # 只继承 reviews-root——那时 rt.review_spec 还是 None，但授权事实
        # 在 request.json 里，白纸黑字。恢复失败（盘上没有合法 spec）就
        # 让授权闸照常拒绝，不因为多一条恢复路径而放宽口径。
        spec = self._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED", "test proposal was not approved")
        if "selected_snippets" not in spec.data_policy.model_data_categories:
            raise IntakeError("REPAIR_DATA_NOT_AUTHORIZED",
                              "selected_snippets was not approved for model transfer")
        repo = self._resolve_review_repo(request)
        expected_snapshot = request.get("source_snapshot") or {}
        current_snapshot = _repo_snapshot(repo.path)
        if (expected_snapshot.get("head") != current_snapshot.get("head")
                or expected_snapshot.get("snapshot_sha256") != current_snapshot.get("snapshot_sha256")):
            raise IntakeError("SOURCE_SNAPSHOT_CHANGED",
                              "仓库在审查后发生变化，不能基于旧证据提议测试")
        plan = (rt.frozen_plan.as_dict() if rt.frozen_plan is not None
                else _read_json(rt.review_dir / "plan.frozen.json"))
        plan_sha256 = str(plan.get("plan_sha256") or "")
        if not plan_sha256:
            raise IntakeError("REPAIR_PLAN_MISSING", "没有可验证的冻结计划")
        artifact = _find_evidence_bundle(rt.review_dir)
        report = _read_json(artifact / "report.json")
        lines = ((report.get("render_model") or {}).get("lines") or [])
        evidence_rows = ledger_store.read(artifact / ledger_store.FILENAME)
        test_files = [str(x) for x in (request.get("test_files") or ())]
        allowed_dirs = list(dict.fromkeys(
            PurePosixPath(x).parent.as_posix() for x in test_files)) or ["tests"]
        tracked = tuple(run_git(["ls-files"], cwd=repo.path, check=True,
                                timeout=30).stdout.splitlines())
        existing_tests = tuple(x for x in tracked
                               if PurePosixPath(x).name.startswith("test_")
                               and x.endswith(".py"))
        try:
            built = build_test_input(goal=str(request.get("goal") or ""),
                                     lines=lines, evidence_rows=evidence_rows,
                                     requested_refs=tuple(requested),
                                     existing_test_files=existing_tests,
                                     allowed_test_dirs=tuple(allowed_dirs))
        except TestProposalRejected as exc:
            if exc.code == "no_targets":
                raise IntakeError("TEST_PROPOSAL_NO_TARGETS", exc.detail) from exc
            raise IntakeError("TEST_PROPOSAL_REF_NOT_IN_RUN", exc.detail) from exc
        context = TestProposalContext(
            review_id=review_id, plan_sha256=plan_sha256,
            goal=str(request.get("goal") or ""),
            gaps=tuple({"ref": ref} for ref in built["allowed_refs"]),
            allowed_test_dirs=tuple(allowed_dirs), known_files=tracked,
            existing_test_files=existing_tests)
        payload = {"review_id": review_id, "action": "propose_test",
                   "context_sha256": hashlib.sha256(json.dumps(
                       {"requested": requested, "context": asdict(context)},
                       ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8")).hexdigest()}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        budget = min(120.0, max(10.0, float(request.get("budget_seconds") or 60)))
        directory = rt.review_dir / "test_proposal"
        directory.mkdir(parents=True, exist_ok=True)

        def runner_for(path: str):
            def runner(worktree: Path):
                executor = (SandboxedExecutor(
                                worktree.parent / "sandbox",
                                process_record=directory / "runtime_process.json")
                            if self.execution_mode is ExecutionMode.SANDBOXED
                            else TrustedLocalExecutor(
                                process_record=directory / "runtime_process.json"))
                return executor.run(
                    [repo.python, "-m", "pytest", path, "-q"],
                    cwd=worktree, timeout=budget,
                    env=sanitized_environment({"PYTHONWARNINGS": "ignore"}))
            return runner

        def visa_run_argv(argv, cwd, timeout, env=None):
            executor = (SandboxedExecutor(
                            Path(cwd).parent / "sandbox",
                            process_record=directory / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=directory / "runtime_process.json"))
            merged = {"PYTHONWARNINGS": "ignore"}
            merged.update({str(k): str(v) for k, v in (env or {}).items()})
            return executor.run([str(a) for a in argv], cwd=Path(cwd),
                                timeout=float(timeout),
                                env=sanitized_environment(merged))

        def run_visa(prop) -> dict:
            """基线绿后立即跑三段验证；工具失败按 VISA_ERROR 如实落档。"""
            # 引用清单有意只取文件名：签证按文件级覆盖对账，行号会随候选测试
            # 的修订轮漂移。见 test_visa.enumerate_interventions 的 docstring。
            cited = list(dict.fromkeys(
                str(ref).split(":")[0] for ref in prop.finding_ids))
            try:
                return verify_candidate(
                    repo=repo.path, source_patch=source_patch,
                    candidate_path=prop.path, candidate_code=prop.code,
                    cited_files=cited, py=repo.python,
                    run_argv=visa_run_argv,
                    scratch_root=directory / "worktrees", timeout=budget)
            except VisaError as exc:
                return {"schema_version": "test-visa-v1",
                        "protocol_version": VISA_PROTOCOL_VERSION,
                        "status": "VISA_ERROR", "code": exc.code,
                        "error": exc.detail}

        def holdout_of(visa_record: dict) -> tuple[Intervention, ...]:
            stage = (visa_record or {}).get("holdout_stage") or {}
            return tuple(
                Intervention(rel_path=str(d["rel_path"]),
                             deleted_lines=tuple(int(x) for x in d["deleted_lines"]),
                             mutated_source="", original_source="",
                             rank=int(d.get("rank") or -1), split="holdout")
                for d in stage.get("interventions") or ())

        fingerprint = self._repo_fingerprint(repo)
        try:
            lease = self._leases.acquire(
                repo_fingerprint=fingerprint, review_id=review_id,
                operation="test_proposal", plan_sha256=plan_sha256,
                ttl_seconds=budget * 6 + 1800)
        except LeaseError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        rt.events.append("repository.lease_acquired", {
            "operation": "test_proposal", "repo_fingerprint": fingerprint,
            "lease_id": lease.lease_id})
        status = ""
        attempts: list[dict] = []
        visa: dict = {}
        prompt = built["prompt"]
        try:
            source_patch = run_git(["diff", "--binary", "HEAD"], cwd=repo.path,
                                   check=True, timeout=60).stdout
            for round_index in range(3):
                stage_name = "test-proposal" if round_index == 0 else "test-revision"
                proposal = None
                for schema_attempt in range(2):
                    self._gate_model_path(rt, stage_name)
                    rt.events.append("model.request.started",
                                     {"stage": stage_name,
                                      "attempt": schema_attempt + 1})
                    try:
                        raw_answer = (self.provider.propose_test_request(prompt)
                                      if round_index == 0
                                      else self.provider.revise_test_request(prompt))
                        proposal = parse_proposal(raw_answer, context=context)
                    except TestProposalRejected as exc:
                        rt.events.append("model.request.completed", {
                            "stage": stage_name, "ok": False,
                            "error_type": type(exc).__name__,
                            "error_code": exc.code})
                        self.provider.metrics.schema_rejections += 1
                        # 重试请求携带脱敏错误码与定向指引；修订轮保留
                        # 上一轮的失败上下文，schema 指引叠加其上。
                        previous = dict(prompt.get("previous_attempt") or {})
                        prompt = {**prompt, "previous_attempt": {
                            **previous, **test_retry_hint(exc), "rejected": True}}
                        continue
                    except (ProviderUnavailable, AttributeError) as exc:
                        raise IntakeError("MODEL_PROVIDER_UNAVAILABLE",
                                          str(exc)) from exc
                    rt.events.append("model.request.completed",
                                     {"stage": stage_name, "ok": True})
                    break
                if proposal is None:
                    raise IntakeError(
                        "TEST_PROPOSAL_REJECTED",
                        test_failure_label("invalid_shape"))
                rt.events.append(
                    "test.proposed" if round_index == 0 else "test.revised", {
                        "round": round_index + 1, "stage": stage_name,
                        "path": proposal.path,
                        "code_sha256": proposal.code_sha256,
                        "finding_ids": list(proposal.finding_ids)})
                _atomic_text(directory / ("attempt-%d.py" % (round_index + 1)),
                             proposal.code)
                try:
                    result = run_isolated_new_test(
                        repo=repo.path, source_patch=source_patch,
                        new_file_path=proposal.path,
                        new_file_code=proposal.code,
                        runner=runner_for(proposal.path),
                        scratch_root=directory / "worktrees")
                except RepairError as exc:
                    raise IntakeError(exc.code, exc.detail) from exc
                excerpt = test_failure_excerpt(result.output, path=proposal.path)
                attempts.append({
                    "round": round_index + 1, "stage": stage_name,
                    "path": proposal.path,
                    "code_sha256": proposal.code_sha256,
                    "returncode": result.returncode,
                    "failure": excerpt})
                if result.returncode == 0:
                    visa = run_visa(proposal)
                    attempts[-1]["visa_status"] = str(visa.get("status") or "")
                    hold_stage = visa.get("holdout_stage") or {}
                    visa_status = str(visa.get("status") or "")
                    rt.events.append("test.visa", {
                        "round": round_index + 1, "path": proposal.path,
                        "status": visa_status,
                        "signed": hold_stage.get("signed"),
                        "denominator": hold_stage.get("denominator")})
                    if (not revise_on_ineffective
                            or visa_status != "PASSED_NOT_EFFECTIVE"
                            or round_index >= 2):
                        status = ("PASSED" if round_index == 0
                                  else "PASSED_WITH_REVISION")
                        rt.events.append("test.passed", {
                            "round": round_index + 1, "path": proposal.path,
                            "status": status, "visa_status": visa_status})
                        break
                    # 绿 ≠ 有效：没挣到签证就带着生成集反馈修订。发送前过
                    # 第 0 条泄漏检查——保留集绝不进模型上下文，这是冻结
                    # 协议的运行时执行点。
                    revision_prompt = {
                        **built["prompt"],
                        "previous_attempt": {
                            "untrusted_previous_code": {
                                "notice": UNTRUSTED_NOTICE,
                                "begin": UNTRUSTED_BEGIN,
                                "end": UNTRUSTED_END,
                                "code": proposal.code}},
                        "generation_feedback": generation_feedback(visa)}
                    try:
                        assert_no_holdout_leak(
                            revision_prompt, holdout=holdout_of(visa),
                            allowed_refs=tuple(built["allowed_refs"]))
                    except VisaError as exc:
                        raise IntakeError(exc.code, exc.detail) from exc
                    prompt = revision_prompt
                    rt.events.append("test.not_effective", {
                        "round": round_index + 1, "path": proposal.path,
                        "signed": hold_stage.get("signed"),
                        "denominator": hold_stage.get("denominator")})
                    continue
                rt.events.append("test.run_failed", {
                    "round": round_index + 1, "path": proposal.path,
                    "returncode": result.returncode,
                    "failure_withheld": excerpt["withheld_for_sensitive_match"]})
                if round_index >= 2:
                    break
                previous_failure = (excerpt["excerpt"]
                                    or "（输出含敏感样式，已整段扣留）")
                prompt = {**built["prompt"], "previous_attempt": {
                    "untrusted_previous_code": {
                        "notice": UNTRUSTED_NOTICE, "begin": UNTRUSTED_BEGIN,
                        "end": UNTRUSTED_END, "code": proposal.code},
                    "untrusted_failure_output": {
                        "notice": UNTRUSTED_NOTICE, "begin": UNTRUSTED_BEGIN,
                        "end": UNTRUSTED_END, "excerpt": previous_failure,
                        "truncated": excerpt["truncated"],
                        "withheld_for_sensitive_match":
                            excerpt["withheld_for_sensitive_match"]}}}
            record = {
                "status": status or "NEEDS_HUMAN",
                "prompt_version": TEST_PROPOSAL_PROMPT_VERSION,
                "model": self.provider.info.model_id,
                "review_id": review_id, "plan_sha256": plan_sha256,
                **proposal.public_dict(), "code": proposal.code,
                "attempts": attempts, "revisions": len(attempts) - 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                # 放在展开之后：`public_dict()` 也带一个 schema_version，说的是
                # **模型提案**的格式（test-proposal-v1），而这份档案是提案的
                # 执行记录。写在前面会被静默盖掉，于是落盘的记录自称是提案。
                # 提案那个版本号没有丢——它就是上面的 prompt_version。
                "schema_version": "test-proposal-record-v1",
            }
            if visa:
                record["visa"] = visa
                record["effective"] = (
                    visa.get("status") == "VERIFIED_EFFECTIVE")
            if not status:
                record["question"] = (
                    "为缺口 " + ", ".join(proposal.finding_ids)
                    + " 提议的测试经 " + str(len(attempts))
                    + " 轮隔离执行仍未跑绿：是否人工补一条测试、换一种提法"
                      "重试，或接受该缺口暂无测试？全部尝试与失败摘录已存档于 "
                    + directory.name + "/ 目录。")
                record["escalation"] = {
                    "kind": "NEEDS_HUMAN",
                    "allowed_answers": ["write_manually",
                                        "retry_different_approach",
                                        "accept_gap"]}
                _atomic_json(directory / "needs_human.json", {
                    "question": record["question"],
                    "escalation": record["escalation"],
                    "attempts": attempts})
                rt.events.append("test.needs_human", {
                    "rounds": len(attempts),
                    "finding_ids": list(proposal.finding_ids)})
            _atomic_json(directory / "proposal.json", record)
            with self._lock:
                self._remember_idempotency(idempotency_key, payload, record)
            return record
        finally:
            self._leases.release(lease)
            rt.events.append("repository.lease_released", {
                "operation": "test_proposal", "lease_id": lease.lease_id})

    def _coverage_gap_refs(self, rt: "ReviewRuntime", request: dict,
                           repo) -> tuple[str, ...]:
        """确定性取覆盖缺口：与 propose_test 同一条 build_test_input 组装。

        评测工具层（tools/run_abc_evaluation.py 的 gaps_of）只是这条逻辑
        的镜像，产品侧不 import 工具层。测试文件行被排除——自主写入
        的仪器是为**源码缺口**补测试，不是改测试。
        """
        artifact = _find_evidence_bundle(rt.review_dir)
        report = _read_json(artifact / "report.json")
        lines = ((report.get("render_model") or {}).get("lines") or [])
        evidence_rows = ledger_store.read(artifact / ledger_store.FILENAME)
        test_files = [str(x) for x in (request.get("test_files") or ())]
        allowed_dirs = list(dict.fromkeys(
            PurePosixPath(x).parent.as_posix() for x in test_files)) or ["tests"]
        tracked = tuple(run_git(["ls-files"], cwd=repo.path, check=True,
                                timeout=30).stdout.splitlines())
        existing_tests = tuple(x for x in tracked
                               if PurePosixPath(x).name.startswith("test_")
                               and x.endswith(".py"))
        try:
            built = build_test_input(goal=str(request.get("goal") or ""),
                                     lines=lines, evidence_rows=evidence_rows,
                                     requested_refs=(),
                                     existing_test_files=existing_tests,
                                     allowed_test_dirs=tuple(allowed_dirs))
        except TestProposalRejected:
            return ()
        refs = tuple(ref for ref in built["allowed_refs"]
                     if not _ref_in_test_file(ref))
        return refs[:20]

    def autonomous_write_step(self, review_id: str, *,
                              refs: tuple[str, ...] | list[str] | None = None,
                              idempotency_key: str = "") -> dict:
        """一个无人值守写入步的确定性"发现→仪器"路由器。

        v2 长跑唯一 FAIL 的教训：覆盖缺口被喂给了 generate_repair（代码
        修复），而那台仪器的语言是"补代码"，不是"补测试"。修复不是再
        调一层提示词，而是把派发本身做成产品能力：合格引用按构造就是
        覆盖缺口 → propose_test（补测试、挣签证）；无合格引用如实记
        skipped；路由不了的发现 fail-closed 记 unrouted，**永不**静默
        回落到 generate_repair。门禁与 verify_repair_candidate 同构
        （L3、常驻授权七条件、终态证据、写预算）——驱动仍然不是批准者。
        写入止于 verified：不建分支、不 commit，deliver_repair 仍只归人。
        """
        rt = self._runtime(review_id)
        if self.agent_level is not AgentLevel.L3:
            raise IntakeError(
                "L3_REQUIRED", "autonomous write step requires server-bound L3")
        matched, reason = self._standing_authorization_match(rt)
        if matched is None:
            raise IntakeError("STANDING_AUTHORIZATION_REQUIRED", reason)
        if rt.state.snapshot.status not in {ReviewStatus.COMPLETE,
                                            ReviewStatus.PARTIAL}:
            raise IntakeError("REPAIR_REVIEW_NOT_COMPLETE",
                              rt.state.snapshot.status.value)
        if not rt.state.snapshot.valid_bundle:
            raise IntakeError("REPAIR_EVIDENCE_INVALID",
                              "evidence bundle did not verify")
        spec = self._review_spec(rt)
        if spec is None or not spec.autonomy_policy.allow_repair_branch:
            raise IntakeError("REPAIR_NOT_AUTHORIZED",
                              "autonomous write step was not approved")
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo = self._resolve_review_repo(request)
        gap_refs = self._coverage_gap_refs(rt, request, repo)
        payload = {"review_id": review_id,
                   "action": "autonomous_write_step",
                   "refs": list(refs) if refs is not None else None}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached

        def _record(**kwargs) -> dict:
            base = {"schema_version": "autonomous-write-record-v1",
                    "review_id": review_id, "attempted": False,
                    "instrument": "", "refs": [], "status": "",
                    "verify_status": "", "verified": False,
                    "artifact_sha256": "", "result": None}
            base.update(kwargs)
            return base

        if not gap_refs:
            record = _record(status="SKIPPED_NO_ELIGIBLE_REFS")
            rt.events.append("autonomy.write_skipped", {
                "review_id": review_id, "reason": "no_eligible_refs"})
            with self._lock:
                self._remember_idempotency(idempotency_key, payload, record)
            return record
        if refs is not None:
            requested = list(dict.fromkeys(str(x) for x in refs))
            unknown = [x for x in requested if x not in set(gap_refs)]
            if unknown:
                # fail-closed：不是覆盖缺口的发现不派发。宁可停在原地等
                # 人，也不把错的仪器对准错的发现。
                record = _record(status="UNROUTED", refs=unknown)
                rt.events.append("autonomy.write_unrouted", {
                    "review_id": review_id,
                    "reason": "ref_not_a_coverage_gap", "refs": unknown})
                with self._lock:
                    self._remember_idempotency(idempotency_key, payload, record)
                return record
            dispatch_refs = requested
        else:
            dispatch_refs = list(gap_refs)
        self._assert_write_budget(rt)
        rt.events.append("autonomy.write_routed", {
            "review_id": review_id, "instrument": "propose_test",
            "refs": dispatch_refs,
            "reason": "coverage gap: earn a visa, not a patch"})
        result = self.propose_test(
            review_id, {"finding_ids": dispatch_refs},
            idempotency_key=f"{idempotency_key}:propose"
            if idempotency_key else "")
        visa_status = str((result.get("visa") or {}).get("status") or "")
        verified = visa_status == "VERIFIED_EFFECTIVE"
        record = _record(
            attempted=True, instrument="propose_test", refs=dispatch_refs,
            status="DISPATCHED",
            verify_status="VERIFIED" if verified else (
                visa_status or str(result.get("status") or "")),
            verified=verified,
            artifact_sha256=str(result.get("code_sha256") or ""),
            result=result)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, record)
        return record

    def export_review(self, review_id: str) -> dict:
        try:
            return review_export_manifest(self._runtime(review_id).review_dir)
        except GovernanceError as exc:
            raise IntakeError(exc.code, exc.detail) from exc

    def delete_review(self, review_id: str, *, idempotency_key: str = "") -> dict:
        payload = {"review_id": review_id, "action": "delete_review"}
        with self._lock:
            cached = self._idempotent_result(idempotency_key, payload)
            if cached is not None:
                return cached
        rt = self._runtime(review_id)
        if rt.state.snapshot.status not in TERMINAL:
            raise IntakeError("REVIEW_DELETE_NOT_TERMINAL", rt.state.snapshot.status.value)
        try:
            result = delete_review_data(rt.review_dir)
        except GovernanceError as exc:
            raise IntakeError(exc.code, exc.detail) from exc
        self._runtimes.pop(review_id, None)
        with self._lock:
            self._remember_idempotency(idempotency_key, payload, result)
        return result

    def repair_status(self, review_id: str) -> dict:
        rt = self._runtime(review_id)
        directory = rt.review_dir / "repair"
        manifest = _read_json(directory / "evidence_manifest.json")
        delivery = _read_json(directory / "delivery_record.json")
        candidate = _read_json(directory / "candidate.json")
        if not manifest and not delivery and not candidate:
            return {"status": "NOT_DELIVERED"}
        return {"status": ("DELIVERED" if delivery else "PROPOSED"
                           if candidate else "INCOMPLETE"),
                "candidate": candidate, "evidence_manifest": manifest,
                "delivery_record": delivery}

    def test_proposal_status(self, review_id: str) -> dict:
        rt = self._runtime(review_id)
        record = _read_json(rt.review_dir / "test_proposal" / "proposal.json")
        if not record:
            return {"status": "NOT_PROPOSED"}
        return {"status": str(record.get("status") or ""),
                "proposal": record}

    def _execute(self, rt: ReviewRuntime) -> None:
        failure: Exception | None = None
        try:
            # 运行前的最后断言：intake 之后 request 可能被任何东西改写。
            # 标准审查一旦发现 provider 漂移，在产生任何实验或模型请求
            # 之前就终止，并把漂移本身记成事件。
            if (str(rt.request.get("product_mode") or "standard") == "standard"
                    and str(rt.request.get("model_provider") or "deterministic")
                    != "deterministic"):
                rt.events.append("standard_mode.provider_drift", {
                    "model_provider": rt.request.get("model_provider")})
                raise StandardModeViolation(
                    "STANDARD_MODE_PROVIDER_DRIFT: 标准审查的模型提供方在运行前被改写")
            executor = (SandboxedExecutor(
                            rt.session.run_dir,
                            process_record=rt.review_dir / "runtime_process.json")
                        if self.execution_mode is ExecutionMode.SANDBOXED
                        else TrustedLocalExecutor(
                            process_record=rt.review_dir / "runtime_process.json"))
            rt.events.append("executor.bound", {
                "mode": executor.mode,
                "sandboxed_children": executor.mode == "sandboxed",
                "control_plane": "trusted",
            })
            with execution_scope(executor):
                self._execute_bound(rt)
        except Exception as exc:
            failure = exc
        finally:
            self._prepare_terminal(rt)
            if failure is not None:
                self._fail(rt, failure)
            rt.finished_event.set()

    def _prepare_terminal(self, rt: ReviewRuntime) -> None:
        """Finish cleanup before a terminal state becomes publicly visible."""
        if rt.session is not None:
            rt.session.cleanup()
            rt.session = None
        if rt.lease is not None:
            try:
                self._release_execution_lease(rt)
            except LeaseError:
                pass
        if rt.capability_token:
            self._capability_authority.revoke(rt.capability_token)
        with self._lock:
            if self._live_id == rt.review_id:
                self._live_id = None

    def _execute_bound(self, rt: ReviewRuntime) -> None:
        """一次 Review 的可信控制面；仓库子进程由当前 Executor 隔离。"""
        if rt.cancel_event.is_set():
            if rt.state.snapshot.status is not ReviewStatus.CANCELLING:
                self._transition(rt, ReviewStatus.CANCELLING,
                                 reason="review_cancelled:user")
            rt.events.append("review.cancellation_observed", {"phase": "pre_execution"})
            self._prepare_terminal(rt)
            self._transition(rt, ReviewStatus.ABORTED,
                             reason="review_cancelled:user")
            return
        self._transition(rt, ReviewStatus.BASELINE_RUNNING,
                         evidence_run_id=rt.session.slug,
                         evidence_started=True)
        self._call_tool(
            rt, "collect_test_scope", {"paths": list(rt.session.test_files)},
            reason_code="freeze_test_scope",
            reason="Normalize the approved test scope before execution")
        baseline = self._call_tool(
            rt, "run_baseline", reason_code="establish_baseline",
            reason="Establish the frozen comparison baseline",
            expected_artifacts=("baseline_result",))
        cancelled = self._cancel_requested(rt)
        # Coverage ranking needs the baseline's coverage, which does not exist
        # when the plan is frozen — so the deterministic order is applied here,
        # after the baseline and before the first probe. The frozen universe is
        # untouched: the candidate set is identical, only the visiting order
        # differs, and the loop probes remaining[0], so ranking here is what
        # makes the product match the measured coverage_first baseline.
        summaries = rt.session.candidate_summaries()
        priorities = tuple(rt.frozen_plan.plan.priorities)
        ordered, applied = coverage_first_order(
            priorities, summaries, str(rt.request.get("review_focus") or "evidence-boundary"))
        rt.request["deterministic_scheduler"] = applied
        rt.events.append("scheduler.deterministic_order", {
                "strategy": applied, "frozen_order": list(priorities),
                "applied_order": list(ordered),
        })
        state = AgentState(
            goal=rt.request["goal"], budget_seconds=rt.request["budget_seconds"],
            frozen_universe=priorities,
            remaining=ordered,
            baseline_green=bool(baseline["all_passed"]),
            max_steps=max(1, len(priorities)),
            agent_level=self.agent_level,
            candidates=summaries)
        if not cancelled:
            self._transition(rt, ReviewStatus.EXECUTING)
        stop_reason = StopReason.NO_ELIGIBLE_LEFT
        while state.remaining and not cancelled:
            cancelled = self._cancel_requested(rt)
            if cancelled:
                break
            if state.exhausted():
                stop_reason = (StopReason.MAX_STEPS if state.step >= state.max_steps
                               else StopReason.BUDGET_EXHAUSTED)
                rt.events.append("policy.forced_stop", {"stop_reason": stop_reason.value})
                break
            anchor_id = state.remaining[0]
            rt.events.append("probe.started", {"anchor_id": anchor_id})
            self._transition(rt, ReviewStatus.VERIFYING_RESTORE)
            obs = self._call_tool(
                rt, "counterfactual_probe", {"anchor_id": anchor_id},
                reason_code="collect_counterfactual_evidence",
                reason="Run one approved counterfactual and verify restoration",
                expected_artifacts=("counterfactual_evidence", "restore_proof"))
            cancelled = self._cancel_requested(rt)
            if not cancelled:
                self._transition(rt, ReviewStatus.EXECUTING)
            state = state.advance(obs)
            rt.events.append("observation.recorded", {
                    "observation_id": obs.observation_id,
                    "policy_branch": obs.policy_branch,
                    "anchor_id": obs.anchor_id, "status": obs.status,
                    "identical_to_baseline": obs.identical_to_baseline,
                    "regressed_tests": list(obs.regressed_tests),
                    "evidence_id": obs.evidence_id,
            })
            if cancelled:
                stop_reason = StopReason.HUMAN_REQUESTED
                break
            if state.stagnant():
                # 停滞判据（D1，跑前冻结）：连续 NO_PROGRESS_WINDOW 个观测
                # 指纹相同。健康路径每步消耗锚点、指纹必然变化；这里挡的
                # 是无人值守下观测锚点与剩余集合不匹配导致的原地打转。
                stop_reason = StopReason.NO_PROGRESS
                rt.events.append("policy.forced_stop", {
                    "stop_reason": stop_reason.value,
                    "window": NO_PROGRESS_WINDOW,
                    "anchor_id": obs.anchor_id,
                    "status": obs.status,
                })
                break
            if not state.remaining:
                stop_reason = StopReason.NO_ELIGIBLE_LEFT
                break
            action, fallback = self._next_action(rt, state)
            original_order = state.remaining
            requested_order = tuple(action.order)
            try:
                if action.kind is ActionKind.REPRIORITIZE:
                    state = state.reorder(action.order, obs.observation_id)
                else:
                    state = state.mark_decision(obs.observation_id)
            except ReprioritizationRejected as exc:
                if self.provider:
                    self.provider.metrics.policy_rejections += 1
                    self.provider.metrics.blocked_requests += 1
                    self.provider.metrics.fallbacks += 1
                    reasons = self.provider.metrics.policy_rejection_reasons
                    reasons[exc.code] = reasons.get(exc.code, 0) + 1
                rt.events.append("policy.rejected", {
                        "stage": "scheduler", "code": exc.code,
                        "observation_id": obs.observation_id,
                        "requested_order": list(requested_order),
                })
                fallback = f"policy_rejected:{exc.code}"
                action = _continue_action()
                state = state.mark_decision(obs.observation_id)
            # 标准审查的调度决策不占用 model.* 命名空间：零模型承诺
            # 延伸到事件词表，"账本里没有任何 model 前缀"因此成为可以直接
            # 断言的不变量。智能体审查保留 model.action——那里的决策
            # 确实出自模型或其显式降级。
            action_kind = ("scheduler.action"
                           if str(rt.request.get("product_mode")
                                  or "standard") == "standard"
                           else "model.action")
            rt.events.append(action_kind, {
                    "observation_id": obs.observation_id,
                    "observation_branch": obs.policy_branch,
                    "kind": action.kind.value,
                    "stop_reason": action.stop_reason.value if action.stop_reason else None,
                    "reason": action.reason, "fallback": fallback,
                    "source": ("live_model" if not fallback
                               else ("deterministic"
                                     if fallback == "deterministic"
                                     else "deterministic_fallback")),
                    "requested_order": list(requested_order),
                    "original_order": list(original_order),
                    "actual_order": list(state.remaining),
                    # 决策上下文：目标、预算与最近观测随动作一并入档，
                    # 让"模型看到了什么再做的决定"可回放、可审计。
                    "goal": state.goal,
                    "budget_seconds": state.budget_seconds,
                    "spent_seconds": round(state.spent_seconds, 3),
                    "budget_left_seconds": round(state.budget_left(), 3),
                    "last_observation": obs.__dict__,
                    "remaining_candidates": [c.__dict__ for c in state.candidates
                                             if c.anchor_id in state.remaining],
                    "prompt_version": ("scheduler-policy-v3"
                                       if self.agent_level in
                                       {AgentLevel.L2, AgentLevel.L3}
                                       else "stop-policy-v1"),
            })
            if action.kind is ActionKind.ASK_HUMAN:
                human = self._await_human(
                    rt, action.reason, state.budget_left())
                if human != "continue":
                    stop_reason = StopReason.HUMAN_REQUESTED
                    break
            if action.kind is ActionKind.STOP:
                stop_reason = action.stop_reason or StopReason.GOAL_SATISFIED
                break
            rt.events.append("scheduler.next", {
                    "actual_next_anchor": state.remaining[0],
                    "previous_next_anchor": original_order[0],
                    "selection": ("model_reprioritized"
                                  if action.kind is ActionKind.REPRIORITIZE
                                  else f"frozen_{applied}"),
                    "priority_reason": action.reason,
            })
        cancelled = self._cancel_requested(rt) or cancelled
        if cancelled:
            stop_reason = StopReason.HUMAN_REQUESTED
        self._transition(rt, ReviewStatus.SYNTHESIZING)
        result = self._call_tool(
            rt, "finish_run", {"stop_reason": stop_reason.value},
            reason_code="seal_evidence",
            reason="Seal the approved run and publish its evidence artifacts",
            expected_artifacts=("report", "bundle", "run_status"))
        valid = not verify_bundle(Path(result["report"]).parent)
        narration = self._narrate(rt, Path(result["report"]).parent)
        # 建议挂在 narration 内部（ReviewBundle v2 不加破坏性顶层字段）：
        # 只读、不执行、不进三态结论，失败也不影响审查完成。
        narration["recommendations"] = self._recommend(
            rt, Path(result["report"]).parent, stop_reason.value)
        # 升级率分母（D1）：自主决定 N 次 / 升级 M 次。同样挂 narration
        # 内部——口径与字段见 _autonomy_metrics。
        narration["autonomy"] = self._autonomy_metrics(rt)
        final = (ReviewStatus.ABORTED if cancelled else
                 ReviewStatus.PARTIAL
                 if result["summary"].get("analysis_completion") == "partial"
                 else ReviewStatus.COMPLETE)
        if cancelled:
            rt.events.append("review.cancelled", {"reason": "user"})
        rt.events.append("review.completed", {
                "review_status": final.value,
                "evidence_status": "COMPLETE" if valid else "FAILED",
                "stop_reason": stop_reason.value,
        })
        evidence_dir = Path(result["report"]).parent
        all_events = [e.as_dict() for e in rt.events.all()]
        provider = (self.provider.info.as_dict() if self.provider else
                    {"kind": "deterministic"})
        review_bundle = build_review_bundle_v2(
            review_id=rt.review_id,
            request=rt.request,
            plan=rt.frozen_plan.as_dict(),
            events=all_events,
            scheduler_trace=[
                e for e in all_events
                if e["kind"] in {"observation.recorded", "model.action",
                                 "scheduler.action",
                                 "policy.rejected", "scheduler.next"}
            ],
            evidence_run_id=rt.session.slug,
            evidence_bundle={
                "run_id": rt.session.slug,
                "report": _read_json(evidence_dir / "report.json"),
                "ledger": ledger_store.read(evidence_dir / ledger_store.FILENAME),
                "manifest": _read_json(evidence_dir / "bundle.json"),
                "run_status": _read_json(evidence_dir / "run_status.json"),
            },
            narration=narration,
            provider=provider,
            model_metrics=(self.provider.metrics.delta_since(rt.metrics_base)
                           if self.provider else {}),
            evidence_valid=valid,
        )
        _atomic_json(rt.review_dir / "review_bundle.json", review_bundle)
        traces = (self.provider.private_traces[rt.trace_start:]
                  if self.provider else [])
        if traces:
            _atomic_json(rt.review_dir / "private" / "model_trace.json", {
                "schema_version": "private-model-trace-v1",
                "traces": traces,
            })
        self._prepare_terminal(rt)
        self._transition(rt, final,
                         reason=("review_cancelled:user" if cancelled else ""),
                         valid_bundle=valid)

    def _cancel_requested(self, rt: ReviewRuntime) -> bool:
        if not rt.cancel_event.is_set():
            return False
        if rt.state.snapshot.status not in {ReviewStatus.CANCELLING,
                                            *TERMINAL}:
            self._transition(rt, ReviewStatus.CANCELLING,
                             reason="review_cancelled:user")
        rt.events.append("review.cancellation_observed", {})
        return True

    def _await_human(self, rt: ReviewRuntime, reason: str,
                     budget_left_seconds: float) -> str:
        decision_id = uuid.uuid4().hex
        plan_hash = rt.frozen_plan.plan_sha256
        wait_seconds = max(1.0, min(300.0, float(budget_left_seconds)))
        pending = {
            "schema_version": "human-decision-v1", "decision_id": decision_id,
            "review_id": rt.review_id, "plan_sha256": plan_hash,
            "question": (reason or "Continue this approved review?")[:300],
            "allowed_decisions": ["continue", "stop"],
            "default_decision": "stop", "status": "PENDING",
            "deadline_epoch": time.time() + wait_seconds,
        }
        rt.pending_decision = pending
        rt.decision_answer = ""
        rt.decision_event.clear()
        _atomic_json(rt.review_dir / "decisions" / "pending.json", pending)
        rt.events.append("decision.requested", {
            "decision_id": decision_id, "plan_sha256": plan_hash,
            "deadline_epoch": pending["deadline_epoch"],
            "default_decision": "stop"})
        self._transition(rt, ReviewStatus.AWAITING_HUMAN,
                         reason="human_decision_required")
        while time.time() < pending["deadline_epoch"]:
            if rt.cancel_event.is_set():
                return "stop"
            if rt.decision_event.wait(timeout=.2):
                answer = rt.decision_answer
                if answer == "continue":
                    self._transition(rt, ReviewStatus.EXECUTING,
                                     reason="human_decision:continue")
                    return answer
                return "stop"
        rt.events.append("decision.defaulted", {
            "decision_id": decision_id, "decision": "stop",
            "reason": "deadline_expired"})
        return "stop"

    def _next_action(self, rt: ReviewRuntime, state: AgentState):
        if rt.request["model_provider"] == "live" and self.provider is not None:
            self._gate_model_path(rt, "scheduling")
            rt.events.append("model.request.started", {"stage": "scheduling"})
            try:
                action = self.provider.next_action(state, ToolCatalog())
                rt.events.append("model.request.completed",
                                 {"stage": "scheduling", "ok": True})
                return action, ""
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "scheduling", "ok": False,
                    "error_type": type(exc).__name__})
                self.provider.metrics.fallbacks += 1
                rt.events.append("model.fallback", {
                    "stage": "next_action", "reason": type(exc).__name__,
                    "action": "continue",
                })
                return _continue_action(), type(exc).__name__
        return _continue_action(), "deterministic"

    def _narrate(self, rt: ReviewRuntime, bundle_dir: Path) -> dict:
        rows = ledger_store.read(bundle_dir / ledger_store.FILENAME)
        claims = []
        evidence_ids = {r["record_id"] for r in rows}
        for row in rows:
            if row["record_type"] != ledger_records.CLAIM:
                continue
            p = row["payload"]
            claims.append({
                "record_id": row["record_id"], "claim_type": p["kind"],
                "anchor": p["anchor"], "provenance": p["provenance"],
            })
        claim_ids = [c["record_id"] for c in claims]
        layout = deterministic_layout(claim_ids)
        layout_fallback = False
        if (rt.request.get("model_provider") == "live" and self.provider is not None
                and claim_ids):
            self._gate_model_path(rt, "narration")
            rt.events.append("model.request.started", {"stage": "narration"})
            try:
                layout = self.provider.draft_narration(claim_ids=tuple(claim_ids))
                rt.events.append("model.request.completed",
                                 {"stage": "narration", "ok": True})
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "narration", "ok": False,
                    "error_type": type(exc).__name__})
                self.provider.metrics.fallbacks += 1
                layout_fallback = True
                rt.events.append("model.fallback", {
                    "stage": "narrator", "reason": type(exc).__name__,
                    "action": "deterministic_template",
                })
        try:
            narration = compile_narration(
                claims=claims, evidence_ids=evidence_ids, layout=layout)
            rt.events.append("narrator.compiled", {
                "template_version": narration["template_version"],
                "fact_blocks": len(narration["blocks"]),
                "fallback": layout_fallback,
            })
            return narration
        except NarrationRejected as exc:
            rt.events.append("narrator.fallback", {"reason": str(exc)[:100]})
            return {"template_version": "evidence-narrator-v1",
                    "scope_note": "结论仅适用于本次声明测试范围。", "blocks": []}

    def _autonomy_metrics(self, rt: ReviewRuntime) -> dict:
        """升级率分母（D1）：自主决定 N 次 / 升级 M 次，机器可复核。

        只数事件账本里已有的机器事实，不引入第二套记账：
        - 自主决定 = scheduler.action + model.action（每步调度一条）
        - 升级 = decision.requested + decision.defaulted（超时默认停
          同样算升级——它本来要问人，只是没人应答）
        确定性标准审查如实记 0/0/0.0，不粉饰成"参与"。
        """
        autonomous_kinds = {"scheduler.action", "model.action"}
        escalation_kinds = {"decision.requested", "decision.defaulted"}
        autonomous = sum(1 for event in rt.events.all()
                         if event.kind in autonomous_kinds)
        escalations = sum(1 for event in rt.events.all()
                          if event.kind in escalation_kinds)
        decisions = autonomous + escalations
        return {
            "autonomous_decisions": autonomous,
            "escalations": escalations,
            "escalation_rate": round(escalations / decisions, 6)
            if decisions else 0.0,
            "definition": (
                "autonomous=scheduler.action+model.action; "
                "escalations=decision.requested+decision.defaulted"),
        }

    def _recommend(self, rt: ReviewRuntime, bundle_dir: Path,
                   stop_reason: str) -> dict:
        """调度结束后的证据约束型只读建议；仅 live 运行自动追加一次。

        输入封顶 80 行 / 12KB，代码行包在不可信边界内。校验失败重试一次，
        仍失败则整体降级为 failure_reason——建议从不阻塞审查完成，也从不
        进入三态结论。
        """
        base = {
            "schema_version": RECOMMENDATION_PROMPT_VERSION,
            "generated": False,
            "items": [],
            "model": (self.provider.info.model_id if self.provider else ""),
            "prompt_version": RECOMMENDATION_PROMPT_VERSION,
        }
        if (rt.request.get("model_provider") != "live"
                or self.provider is None):
            return {**base, "skipped_reason": "deterministic_run"}
        self._gate_model_path(rt, "recommendation")
        report = _read_json(bundle_dir / "report.json")
        lines = ((report.get("render_model") or {}).get("lines") or [])
        rows = ledger_store.read(bundle_dir / ledger_store.FILENAME)
        built = build_input(goal=str(rt.request.get("goal") or ""),
                            stop_reason=stop_reason, lines=lines,
                            evidence_rows=rows)
        if not built["allowed"]["evidence_ids"] or not built["allowed"]["line_refs"]:
            return {**base, "skipped_reason": "no_relevant_evidence"}
        last_error = ""
        last_code = ""
        prompt = built["prompt"]
        for attempt in range(2):
            rt.events.append("model.request.started",
                             {"stage": "recommendation",
                              "attempt": attempt + 1})
            try:
                raw = self.provider.recommendation_request(prompt)
                items = parse_recommendations(raw, allowed=built["allowed"])
            except RecommendationRejected as exc:
                rt.events.append("model.request.completed", {
                    "stage": "recommendation", "ok": False,
                    "error_type": type(exc).__name__, "error_code": exc.code})
                self.provider.metrics.schema_rejections += 1
                last_error = failure_label(exc.code)
                last_code = exc.code
                # 重试请求必须携带脱敏错误码与定向指引，否则第二次
                # 与第一次完全相同，模型几乎必然重复同一格式错误。
                prompt = {**built["prompt"],
                          "previous_attempt": retry_hint(exc)}
                continue
            except Exception as exc:
                rt.events.append("model.request.completed", {
                    "stage": "recommendation", "ok": False,
                    "error_type": type(exc).__name__})
                last_error = type(exc).__name__
                last_code = type(exc).__name__
                continue
            rt.events.append("model.request.completed",
                             {"stage": "recommendation", "ok": True})
            rt.events.append("recommendation.generated", {
                "count": len(items),
                "prompt_version": RECOMMENDATION_PROMPT_VERSION,
                "model": self.provider.info.model_id,
            })
            return {**base, "generated": True,
                    "items": [item.as_dict() for item in items],
                    **({} if items else
                       {"skipped_reason": "insufficient_evidence"})}
        rt.events.append("recommendation.failed", {"reason": last_error,
                                                   "code": last_code})
        return {**base, "failure_reason": last_error, "failure_code": last_code}

    def _fail(self, rt: ReviewRuntime, exc: Exception) -> None:
        reason = (f"{exc.stage}:{exc.detail}" if isinstance(exc, SessionError)
                  else f"{type(exc).__name__}:{str(exc)[:200]}")
        try:
            rt.events.append("review.failed", {"reason": reason})
        except Exception:
            pass
        try:
            if rt.state.snapshot.status not in TERMINAL:
                self._transition(rt, ReviewStatus.FAILED, reason=reason,
                                 allow_recovery_terminal=True)
        except Exception:
            pass
        with self._lock:
            if self._live_id == rt.review_id:
                self._live_id = None

    def describe(self, review_id: str) -> dict:
        rt = self._runtime(review_id)
        plan = None
        plan_path = rt.review_dir / "plan.frozen.json"
        draft_path = rt.review_dir / "plan.draft.json"
        if rt.frozen_plan is not None:
            plan = rt.frozen_plan.as_dict()
        elif plan_path.exists():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        elif draft_path.exists():
            plan = json.loads(draft_path.read_text(encoding="utf-8"))
        return {"review_id": review_id, "state": rt.state.snapshot.as_dict(),
                "request": rt.request or _read_json(rt.review_dir / "request.json"),
                "plan": plan, "repair": self.repair_status(review_id),
                "test_proposal": self.test_proposal_status(review_id),
                "last_seq": rt.events.last_seq}

    def events(self, review_id: str) -> EventStore:
        return self._runtime(review_id).events

    def evidence(self, review_id: str, evidence_id: str) -> dict:
        rt = self._runtime(review_id)
        if rt.session is not None:
            try:
                return rt.session.read_evidence(evidence_id)
            except SessionError:
                pass
        artifact = _find_evidence_bundle(rt.review_dir)
        for row in ledger_store.read(artifact / ledger_store.FILENAME):
            if row.get("record_id") == evidence_id:
                return row
        raise IntakeError("EVIDENCE_NOT_FOUND", evidence_id)

    def review_bundle_path(self, review_id: str) -> Path:
        path = self._runtime(review_id).review_dir / "review_bundle.json"
        if not path.exists():
            raise IntakeError("BUNDLE_NOT_READY", review_id)
        return path

    def model_transcript(self, review_id: str) -> dict:
        """Return the complete credential-free model conversation for local UI."""
        rt = self._runtime(review_id)
        path = rt.review_dir / "private" / "model_trace.json"
        if not path.exists():
            return {"schema_version": "local-model-transcript-v1", "calls": []}
        raw = _read_json(path)
        calls = []
        for index, trace in enumerate(raw.get("traces") or [], start=1):
            calls.append({
                "call": index,
                "stage": str(trace.get("stage") or "unknown"),
                "messages": trace.get("messages") or [],
                "raw_response": str(trace.get("raw_response") or ""),
                "http_status": int(trace.get("http_status") or 0),
                "latency_ms": trace.get("latency_ms"),
                "error_type": str(trace.get("error_type") or ""),
                "request_sha256": str(trace.get("request_sha256") or ""),
                "response_sha256": str(trace.get("response_sha256") or ""),
            })
        return {"schema_version": "local-model-transcript-v1", "calls": calls}

    def _source_context(self, review_id: str) -> tuple[ReviewRuntime, RegisteredRepo, str]:
        """解析审查绑定的仓库，并核对工作区快照仍是审查时的那份。"""
        rt = self._runtime(review_id)
        request = rt.request or _read_json(rt.review_dir / "request.json")
        repo_id = str((request.get("source") or {}).get("repo_id") or "")
        if not repo_id:
            raise IntakeError("SOURCE_REPO_UNAVAILABLE",
                              "the review has no bound repository")
        try:
            # 同进程直接命中 repo_id；跨进程（长跑中途重启、或审查由另一个
            # 进程跑完）改按审查落盘的源快照在允许清单里找回同一个仓库。
            # 这条分支以前只会硬失败，所以补上找回不改变任何已工作的路径。
            repo = self._resolve_review_repo(request)
        except IntakeError as exc:
            if exc.code == "SOURCE_SNAPSHOT_CHANGED":
                raise
            raise IntakeError("SOURCE_REPO_UNAVAILABLE", repo_id) from exc
        expected = str((request.get("source_snapshot") or {})
                       .get("snapshot_sha256") or "")
        if not expected:
            raise IntakeError("SOURCE_SNAPSHOT_MISSING", review_id)
        current = _repo_snapshot(repo.path)
        if (not current.get("snapshot_sha256")
                or current["snapshot_sha256"] != expected):
            raise IntakeError(
                "SOURCE_SNAPSHOT_CHANGED",
                "当前源码已经不同于这次审查所使用的版本；原有证据仍可查看，"
                "但不能把它继续标在新源码上")
        return rt, repo, expected

    def source_tree(self, review_id: str) -> dict:
        """证据工作台的文件树：修改文件 > 证据文件 > 评论文件 > 其他。"""
        rt, repo, snapshot_sha = self._source_context(review_id)
        changed = _changed_repo_source_paths(repo.path)
        evidence = _source_evidence_counts(rt.review_dir)
        comments = _source_comment_counts(rt.review_dir)
        entries: list[dict] = []
        directories: dict[str, dict] = {}

        def directory_entry(rel: str) -> dict:
            if rel not in directories:
                directories[rel] = {
                    "path": rel, "name": PurePosixPath(rel).name,
                    "kind": "directory", "language": "",
                    "changed": False, "evidence_count": 0, "comment_count": 0,
                }
            return directories[rel]

        for rel in _list_repo_source_files(repo.path):
            entry = {
                "path": rel, "name": PurePosixPath(rel).name, "kind": "file",
                "language": _source_language(rel),
                "changed": rel in changed,
                "evidence_count": int(evidence.get(rel, 0)),
                "comment_count": int(comments.get(rel, 0)),
            }
            entries.append(entry)
            parent = PurePosixPath(rel).parent
            while parent != PurePosixPath("."):
                folder = directory_entry(parent.as_posix())
                folder["changed"] = bool(folder["changed"] or entry["changed"])
                folder["evidence_count"] += entry["evidence_count"]
                folder["comment_count"] += entry["comment_count"]
                parent = parent.parent
        entries.extend(directories.values())

        def rank(entry: dict) -> int:
            if entry["changed"]:
                return 0
            if entry["evidence_count"]:
                return 1
            if entry["comment_count"]:
                return 2
            return 3

        entries.sort(key=lambda entry: (rank(entry), entry["path"]))
        file_entries = [entry for entry in entries if entry["kind"] == "file"]
        truncated = len(entries) > SOURCE_TREE_MAX_ENTRIES
        return {
            "schema_version": "source-tree-v1",
            "review_id": review_id,
            "source_snapshot_sha256": snapshot_sha,
            "truncated": truncated,
            "counts": {
                "files": len(file_entries),
                "changed_files": sum(1 for e in file_entries if e["changed"]),
                "evidence_files": sum(1 for e in file_entries
                                      if e["evidence_count"]),
                "comment_files": sum(1 for e in file_entries
                                     if e["comment_count"]),
            },
            "entries": entries[:SOURCE_TREE_MAX_ENTRIES],
        }

    def source_file(self, review_id: str, raw_path: object) -> dict:
        """按审查快照只读返回一个源码文件，供 Monaco 工作台渲染。"""
        _rt, repo, snapshot_sha = self._source_context(review_id)
        parts = _clean_source_parts(raw_path)
        rel = PurePosixPath(*parts).as_posix()
        if rel not in set(_list_repo_source_files(repo.path)):
            raise IntakeError("SOURCE_PATH_NOT_FOUND", rel)
        target = repo.path.joinpath(*parts)
        repo_root = repo.path.resolve()
        try:
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise IntakeError("SOURCE_PATH_NOT_FOUND", rel) from exc
        if not resolved.is_relative_to(repo_root):
            raise IntakeError("SOURCE_PATH_FORBIDDEN", rel)
        inside = resolved.relative_to(repo_root)
        if inside.parts and inside.parts[0] == ".git":
            raise IntakeError("SOURCE_PATH_FORBIDDEN", rel)
        if not resolved.is_file():
            raise IntakeError("SOURCE_PATH_NOT_FOUND", rel)
        size = resolved.stat().st_size
        if size > SOURCE_FILE_MAX_BYTES:
            raise IntakeError("SOURCE_FILE_TOO_LARGE",
                              f"{size} bytes exceeds the 512KB read limit")
        data = resolved.read_bytes()
        if b"\x00" in data[:8192]:
            raise IntakeError("SOURCE_FILE_BINARY", rel)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntakeError("SOURCE_FILE_BINARY",
                              "file is not UTF-8 text") from exc
        blob_sha256 = hashlib.sha256(data).hexdigest()
        etag = hashlib.sha256(
            f"source-file-v1:{blob_sha256}:{snapshot_sha}".encode("utf-8")
        ).hexdigest()
        line_count = content.count("\n") + (
            1 if content and not content.endswith("\n") else 0)
        return {
            "schema_version": "source-file-v1",
            "review_id": review_id,
            "path": rel,
            "content": content,
            "language": _source_language(rel),
            "encoding": "utf-8",
            "line_count": line_count,
            "blob_sha256": blob_sha256,
            "source_snapshot_sha256": snapshot_sha,
            "etag": etag,
            "read_only_reason": _SOURCE_READ_ONLY_REASON,
        }

    def draft_from_text(self, text: str, *, product_mode: str = "standard") -> dict:
        """把一句话起草成 ReviewSpec 草案：模型只填表，绝不创建审查。

        一句话起草是一次真实的模型调用，所以它和补测提案一样属于
        智能体审查：标准模式在这里拿稳定错误码拒绝，而不是靠前端
        隐藏入口。旧请求不携带 product_mode 时按标准模式解释（失败关闭）。

        两轮尝试：DraftRejected 时携带脱敏错误码与定向指引重试一次；
        仍失败降级为 failure_code/failure_reason，不抛出、不阻塞人工填表。
        草案在出门前必须拼成 raw spec 并通过 ReviewSpec.parse——那是最终
        闸门：白名单全过但 spec 不收的形态，不允许到达前端。
        """
        sentence = str(text or "").strip()
        if not sentence or len(sentence) > 500:
            raise IntakeError("DRAFT_REQUEST_INVALID",
                              "draft sentence must be 1-500 chars after trimming")
        if product_mode not in PRODUCT_MODES:
            raise IntakeError("PRODUCT_MODE_INVALID", product_mode)
        _gate_model_path_for_mode(product_mode, "draft")
        if self.provider is None:
            return {"schema_version": "draft-from-text-v1",
                    "available": False, "reason": "no_live_provider"}
        built = build_draft_input(sentence=sentence, repos=self.registry.public())
        base = {
            "schema_version": "draft-from-text-v1",
            "available": True,
            "generated": False,
            "prompt_version": DRAFT_PROMPT_VERSION,
            "model": self.provider.info.model_id,
        }
        prompt = built["prompt"]
        last_code = ""
        for _attempt in range(2):
            try:
                raw = self.provider.draft_spec_request(prompt)
                form = parse_draft(raw, allowed=built["allowed"])
            except DraftRejected as exc:
                self.provider.metrics.schema_rejections += 1
                last_code = exc.code
                # 重试请求必须携带脱敏错误码与定向指引，否则第二次与
                # 第一次完全相同，模型几乎必然重复同一格式错误。
                prompt = {**built["prompt"],
                          "previous_attempt": draft_retry_hint(exc)}
                continue
            except Exception as exc:  # noqa: BLE001 - 起草降级，不炸表单
                last_code = type(exc).__name__
                continue
            budget = (form.budget_seconds if form.budget_seconds is not None
                      else DEFAULT_BUDGET_SECONDS)
            repo = self.registry.get(form.repo_id)
            raw_spec = {
                "instruction": form.goal,
                "source": {"kind": "local", "repo_id": form.repo_id},
                "constraints": {"budget_seconds": budget},
                "review_focus": form.review_focus,
                "language": "python",
                "schema_version": SCHEMA_VERSION,
            }
            if form.success_conditions:
                raw_spec["success_conditions"] = list(form.success_conditions)
            if form.non_goals:
                raw_spec["non_goals"] = list(form.non_goals)
            try:
                ReviewSpec.parse(raw_spec)
            except ReviewSpecError:
                last_code = "spec_gate_rejected"
                continue
            preview: dict = {}
            try:
                preview = {"test_files_preview":
                           list(self.registry.discover_python_test_files(repo))}
            except IntakeError:
                preview = {}
            return {**base, "generated": True,
                    "draft": {
                        "repo_id": form.repo_id,
                        "repo_display_name": repo.display_name,
                        "goal": form.goal,
                        "review_focus": form.review_focus,
                        "budget_seconds": budget,
                        "success_conditions": list(form.success_conditions),
                        "non_goals": list(form.non_goals),
                    },
                    "sources": {"goal": "model", "repo_id": "model",
                                "review_focus": "model",
                                "budget_seconds": ("default"
                                                   if form.budget_seconds is None
                                                   else "model")},
                    "spec_preview": raw_spec, **preview,
                    "note": "这是模型对一句话的理解，不是结论"}
        return {**base, "generated": False, "failure_code": last_code,
                "failure_reason": draft_failure_label(last_code)}

    def locate_code(self, raw: dict) -> dict:
        """在一组已授权仓库里做确定性的“一句话→代码定位”。

        这里没有模型：token 来自句子本身，命中来自仓库工作区的符号名/
        文件名/内容匹配，同一棵树两次调用逐字节相同。找不到就明说
        fallback 与原因，让前端退回人工选仓库——绝不猜一个 repo_id。
        返回的草案只是预填：创建审查仍要走 create_v2 的全部闸门。
        """
        if (not isinstance(raw, dict) or set(raw) != {"text", "repo_ids"}
                or not isinstance(raw.get("text"), str)
                or not isinstance(raw.get("repo_ids"), list)):
            raise IntakeError("LOCATE_REQUEST_INVALID",
                              "locate request requires text and repo_ids")
        text = raw["text"].strip()
        repo_ids = raw["repo_ids"]
        if (not text or len(text) > 500 or not repo_ids or len(repo_ids) > 20
                or not all(isinstance(item, str) and item.strip()
                           for item in repo_ids)):
            raise IntakeError("LOCATE_REQUEST_INVALID",
                              "text must be 1-500 chars and repo_ids 1-20 ids")
        ordered_ids = list(dict.fromkeys(repo_ids))
        repos = [self.registry.get(repo_id) for repo_id in ordered_ids]
        tokens = extract_tokens(text)
        base = {"schema_version": LOCATE_SCHEMA_VERSION,
                "searched_repos": ordered_ids, "tokens": tokens}
        if not tokens:
            return {**base, "status": "fallback",
                    "reason": "no_searchable_tokens", "matches": []}
        matches: list[dict] = []
        for repo in repos:
            matches.extend(search_repo(repo.path, tokens, repo_id=repo.repo_id))
        matches.sort(key=lambda m: (-m["score"], m["repo_id"], m["path"],
                                    m["evidence_kind"], m["evidence_detail"]))
        if not matches:
            return {**base, "status": "fallback", "reason": "no_hits",
                    "matches": []}
        top_repo_id = matches[0]["repo_id"]
        repo = self.registry.get(top_repo_id)
        paths = dedupe_paths(matches, top_repo_id)
        language = "python" if any(path.endswith(".py") for path in paths) else "auto"
        raw_spec = {
            "instruction": text,
            "source": {"kind": "local", "repo_id": top_repo_id},
            "review_focus": "evidence-boundary",
            "constraints": {"budget_seconds": 300},
            "language": language,
            "scope": {"include": paths},
            "schema_version": SCHEMA_VERSION,
        }
        # 与 draft_from_text 同一道闸门：白名单全过但 spec 不收的形态，
        # 不允许到达前端。
        ReviewSpec.parse(raw_spec)
        return {**base, "status": "matched", "matches": matches[:12],
                "draft": {
                    "repo_id": top_repo_id,
                    "repo_display_name": repo.display_name,
                    "instruction": text,
                    "review_focus": "evidence-boundary",
                    "budget_seconds": 300,
                    "paths": paths,
                    "language": language,
                },
                "spec_preview": raw_spec,
                "note": "定位来自仓库内确定性检索，不是模型判断；"
                        "找不到时请回退手动选择仓库"}

    def provider_public(self) -> dict:
        return {
            "configured": self.provider is not None,
            "display_name": "OpenAI-compatible" if self.provider else "Deterministic fallback",
            "model_id": self.provider.info.model_id if self.provider else "",
            "live_available": self.provider is not None,
            "agent_level": self.agent_level.value,
            "execution_mode": self.execution_mode.value,
            "prompt_versions": ["planner-v1",
                                ("scheduler-policy-v3"
                                 if self.agent_level in
                                 {AgentLevel.L2, AgentLevel.L3}
                                 else "stop-policy-v1"), "narrator-v1",
                                RECOMMENDATION_PROMPT_VERSION,
                                DRAFT_PROMPT_VERSION,
                                TEST_PROPOSAL_PROMPT_VERSION],
        }


def _continue_action():
    from modou.agent.models import AgentAction
    return AgentAction.parse({"kind": "continue", "reason": "继续冻结顺序"}, ToolCatalog())


def _encode_action_result(result) -> dict:
    if isinstance(result, AgentObservation):
        return {"kind": "agent-observation", "value": {
            "anchor_id": result.anchor_id, "status": result.status,
            "identical_to_baseline": result.identical_to_baseline,
            "regressed_tests": list(result.regressed_tests), "cost_s": result.cost_s,
            "evidence_id": result.evidence_id,
            "observation_id": result.observation_id}}
    if isinstance(result, dict):
        return {"kind": "json-object", "value": _json_safe(result)}
    raise ProtocolError("ACTION_RESULT_NOT_PERSISTABLE", type(result).__name__)


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if hasattr(value, "to_json"):
        return _json_safe(value.to_json())
    raise ProtocolError("ACTION_RESULT_NOT_PERSISTABLE", type(value).__name__)


def _decode_action_result(raw):
    if not isinstance(raw, dict):
        raise ProtocolError("ACTION_RECEIPT_INVALID", "missing persisted result")
    if raw.get("kind") == "json-object" and isinstance(raw.get("value"), dict):
        return json.loads(json.dumps(raw["value"], ensure_ascii=False))
    if raw.get("kind") == "agent-observation" and isinstance(raw.get("value"), dict):
        value = raw["value"]
        return AgentObservation(
            anchor_id=str(value.get("anchor_id") or ""),
            status=str(value.get("status") or ""),
            identical_to_baseline=value.get("identical_to_baseline"),
            regressed_tests=tuple(value.get("regressed_tests") or ()),
            cost_s=float(value.get("cost_s") or 0),
            evidence_id=str(value.get("evidence_id") or ""),
            observation_id=str(value.get("observation_id") or ""))
    raise ProtocolError("ACTION_RECEIPT_INVALID", "unsupported persisted result")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _validate_idempotency_key(value: str) -> str:
    if value is None:
        return ""
    value = str(value).strip()
    if len(value) > 128 or any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in value):
        raise IntakeError("IDEMPOTENCY_KEY_INVALID", "Idempotency-Key 必须是 128 字符以内的可打印字符串")
    return value


def _payload_sha256(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repo_snapshot(repo: Path) -> dict:
    """Capture the source identity needed for a safe resume.

    `git status --porcelain` alone is not enough: editing an already-modified
    file leaves the status line byte-identical, so a snapshot built only from
    it cannot tell "the review target I planned against" from "someone edited
    that file again since". Both the approval gate and repair delivery rely on
    this digest to refuse stale work, so the working-tree *content* has to be
    part of it: the tracked diff against HEAD, plus the bytes of every
    untracked file listed by Git using NUL-delimited exact paths.
    """
    try:
        head = run_git(["rev-parse", "HEAD"], cwd=repo, check=True,
                       timeout=10).stdout.strip()
        status = run_git(["status", "--porcelain=v1", "-uall"], cwd=repo,
                         check=True, timeout=10).stdout
        tracked_diff = run_git(["diff", "--binary", "HEAD"], cwd=repo,
                               check=True, timeout=30).stdout
        untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"],
                            cwd=repo, check=True, timeout=30, text=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {"head": "", "dirty": True, "status_sha256": "",
                "content_sha256": "", "snapshot_sha256": ""}
    status_sha256 = hashlib.sha256(status.encode("utf-8")).hexdigest()
    content = hashlib.sha256()
    content.update(tracked_diff.encode("utf-8", errors="surrogateescape"))
    for raw_path in untracked.split(b"\x00"):
        if not raw_path:
            continue
        # Porcelain's quoted display names are not filesystem paths. NUL
        # records preserve Unicode, quotes, whitespace and embedded newlines.
        relative = os.fsdecode(raw_path)
        try:
            blob = (repo / relative).read_bytes()
        except OSError:
            blob = b"<unreadable>"
        content.update(raw_path)
        content.update(hashlib.sha256(blob).digest())
    content_sha256 = content.hexdigest()
    snapshot_sha256 = hashlib.sha256(json.dumps(
        {"head": head, "status_sha256": status_sha256,
         "content_sha256": content_sha256}, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"head": head, "dirty": bool(status), "status_sha256": status_sha256,
            "content_sha256": content_sha256, "snapshot_sha256": snapshot_sha256}


def _tool_commit() -> str:
    """Return this checkout's commit when available, without failing delivery."""
    try:
        root = Path(__file__).resolve().parents[2]
        return run_git(["rev-parse", "HEAD"], cwd=root, check=True,
                       timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _find_evidence_bundle(review_dir: Path) -> Path:
    candidates = list((review_dir / "artifacts").glob("*/report.json"))
    if not candidates:
        raise IntakeError("BUNDLE_NOT_READY", review_dir.name)
    return candidates[0].parent


def record_ts() -> str:
    """注释链时间戳统一入口，方便测试冻结时间。"""
    return datetime.now(timezone.utc).isoformat()


def _reverification_records(review_dir: Path) -> list[dict]:
    """按 reverification_id 顺序读回复验记录。"""
    directory = review_dir / "reverifications"
    records: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.glob("rvf-*.json")):
            raw = _read_json(path)
            if isinstance(raw, dict) and raw.get("reverification_id"):
                # v1 记录读取时补默认字段；历史文件永不重写。
                records.append(reverification.migrate_v1(raw))
    return records


def _edit_session_records(review_dir: Path) -> list[dict]:
    """按 session_id 顺序读回编辑会话；目录布局与复验记录同构。"""
    directory = review_dir / "edit-sessions"
    records: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.glob("es-*.json")):
            raw = _read_json(path)
            if isinstance(raw, dict) and raw.get("session_id"):
                records.append(raw)
    return records


def _edit_session_chain_events(review_dir: Path, session_id: str) -> list[dict]:
    """一个会话在注释链里的全部事件（读取用，供账本核对）。"""
    return [entry for entry in _read_annotation_events(review_dir)
            if (entry.get("data") or {}).get("session_id") == session_id]


def _latest_delivery_approval_event(review_dir: Path,
                                    session_id: str) -> dict | None:
    """注释链上该会话最近一次批准事件的指纹钉子（读取用）。

    批准文件是原子写的单文件，链是哈希链；导出时两者必须一致，
    单改任何一边都会在这里暴露。
    """
    for entry in reversed(_read_annotation_events(review_dir)):
        if entry.get("kind") != "repair.delivery_approved":
            continue
        data = entry.get("data") or {}
        if str(data.get("session_id") or "") == session_id:
            return data
    return None


def _ensure_disk_space(path: Path, *, required_bytes: int = 64 * 1024 * 1024) -> None:
    """预检剩余空间；磁盘满必须是结构化错误码，绝不塌缩成类名。"""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        raise IntakeError("EDIT_SESSION_DISK_CHECK_FAILED", str(exc)) from exc
    if usage.free < required_bytes:
        raise IntakeError(
            "EDIT_SESSION_DISK_FULL",
            f"only {usage.free} bytes free under {path}, "
            f"need {required_bytes}")


def _write_session_record(record: dict, path: Path) -> None:
    """会话落盘：原子写；ENOSPC 保留真因并给出结构化错误码。"""
    try:
        _atomic_json(path, record)
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            raise IntakeError("EDIT_SESSION_DISK_FULL", str(exc)) from exc
        raise


def _next_claim_revision(review_dir: Path, claim_id: str) -> int:
    """主张的下一个 revision：旧结论只追加，从不覆盖。

    revision 从 1 起（原主张），此后每次复验结算 +1，无论结局是确认、
    修订还是扣留——确认也要留下"被复验过"的痕迹。
    """
    used = 1
    for record in _reverification_records(review_dir):
        if (record.get("claim_id") == claim_id
                and isinstance(record.get("claim_revision"), int)):
            used = max(used, record["claim_revision"])
    return used + 1


def _review_ledger_rows(review_dir: Path, *, strict: bool = False) -> list[dict]:
    """读回账本行；老审查没有账本时返回空而不是报错。"""
    candidates = sorted((review_dir / "artifacts").glob(
        f"*/{ledger_store.FILENAME}"))
    for path in candidates:
        try:
            return ledger_store.read(path, strict=strict)
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
            continue
    return []


def _render_unit(review_dir: Path, unit_id: str) -> dict | None:
    candidates = sorted((review_dir / "artifacts").glob("*/report.json"))
    for path in candidates:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for unit in ((report.get("render_model") or {}).get("units") or []):
            if isinstance(unit, dict) and unit.get("unit_id") == unit_id:
                return unit
    return None


def _anchor_overlaps(experiment_row: dict, path: str,
                     start: int, end: int) -> bool:
    anchor = ((experiment_row.get("payload") or {}).get("anchor") or {})
    if anchor.get("path") != path or not start or not end:
        return False
    a_start = int(anchor.get("line_start") or 0)
    a_end = int(anchor.get("line_end") or 0)
    return a_start <= end and start <= a_end


def _review_experiment_row(review_dir: Path, experiment_id: str) -> dict:
    for row in _review_ledger_rows(review_dir, strict=False):
        if row.get("record_id") == experiment_id:
            return row
    raise IntakeError("REVERIFICATION_EXPERIMENT_NOT_FOUND", experiment_id)


def _terminal_complete_experiments(experiments: dict[str, dict]) -> list[dict]:
    """复验资格闸门：只重放 COMPLETE 终端实验，且干预非空。

    空 intervention 的行在计划阶段就被排除，而不是运行时才炸成
    整次 failed/withheld——"这条不能重放"是计划事实，不是事故。
    """
    out = []
    for row in experiments.values():
        payload = row.get("payload") or {}
        if payload.get("status") != ledger_records.COMPLETE:
            continue
        iv = payload.get("intervention")
        if not isinstance(iv, dict) or not str(iv.get("path") or ""):
            continue
        if not iv.get("file_removed") and not (iv.get("lines") or []):
            continue
        out.append(row)
    return out


def _experiment_statuses(experiment_row: dict,
                         ledger_rows: list[dict]) -> TestVector:
    """原实验的观测向量：从账本 Fact 记录里取回真实状态。

    复验对比的是"测试状态向量"而不是结论文字：结论可以换措辞，
    向量不一致就是不一致。两条硬规则：

    - 状态字面量在构造处强制走 TestStatus 枚举值，账本里出现
      "PASSED" 这类旧写法当场 ValueError——复验扣留，绝不静默
      规范化两套写法。
    - Fact 拿不到就直接抛错。复验对"没证据"的唯一诚实结论是
      扣留；哨兵值会走进"向量不等 → 修订"的分支，把没证据
      说成被推翻，那是假话不是扣留。
    """
    facts = {row["record_id"]: row for row in ledger_rows
             if row.get("record_type") == "Fact"}
    payload = experiment_row.get("payload") or {}
    post_ids = payload.get("post_fact_ids") or []
    for fact_id in post_ids:
        fact = facts.get(fact_id)
        if fact is None:
            continue
        statuses = ((fact.get("payload") or {}).get("data") or {}).get(
            "statuses")
        if isinstance(statuses, list):
            return TestVector.from_pairs(
                [[str(t), str(s)] for t, s in statuses])
    raise IntakeError("REVERIFICATION_ORIGINAL_VECTOR_MISSING",
                      str(experiment_row.get("record_id") or ""))


def _experiment_declared_nodeids(experiment_row: dict,
                                 ledger_rows: list[dict]) -> list[str]:
    """复验声明范围 = 原基线向量的键。基线 Fact 缺失同样视为没证据。"""
    facts = {row["record_id"]: row for row in ledger_rows
             if row.get("record_type") == "Fact"}
    payload = experiment_row.get("payload") or {}
    for fact_id in payload.get("pre_fact_ids") or []:
        fact = facts.get(fact_id)
        if fact is None:
            continue
        statuses = ((fact.get("payload") or {}).get("data") or {}).get(
            "statuses")
        if isinstance(statuses, list):
            return sorted(str(t) for t, _ in statuses)
    raise IntakeError("REVERIFICATION_ORIGINAL_VECTOR_MISSING",
                      str(experiment_row.get("record_id") or ""))


def _experiment_pre_statuses(experiment_row: dict,
                             ledger_rows: list[dict]) -> TestVector:
    """原实验的基线向量：与 _experiment_statuses 同一套账本纪律。

    状态字面量必须已是小写枚举值；Fact 拿不到就抛
    REVERIFICATION_ORIGINAL_VECTOR_MISSING，绝不给哨兵值。
    """
    facts = {row["record_id"]: row for row in ledger_rows
             if row.get("record_type") == "Fact"}
    payload = experiment_row.get("payload") or {}
    for fact_id in payload.get("pre_fact_ids") or []:
        fact = facts.get(fact_id)
        if fact is None:
            continue
        statuses = ((fact.get("payload") or {}).get("data") or {}).get(
            "statuses")
        if isinstance(statuses, list):
            return TestVector.from_pairs(
                [[str(t), str(s)] for t, s in statuses])
    raise IntakeError("REVERIFICATION_ORIGINAL_VECTOR_MISSING",
                      str(experiment_row.get("record_id") or ""))


def _experiment_regressions(experiment_row: dict,
                            ledger_rows: list[dict]) -> list[tuple]:
    """原实验的具名回归：(nodeid, 基线状态, 干预后状态)。

    只有 passed→坏 才算回归（TestVector.regressions 同一判据）；
    基线本来就红的测试不属于这条主张的证据，不参与定级。
    """
    return list(_experiment_statuses(experiment_row, ledger_rows)
                .regressions(_experiment_pre_statuses(experiment_row,
                                                      ledger_rows)))


def _claim_grade(review_dir: Path, claim_id: str) -> str | None:
    """主张的渲染等级（A-D）；拿不到返回 None，让闸门宽松处理。"""
    unit = _render_unit(review_dir, claim_id)
    if unit is None:
        return None
    grade = str(unit.get("admissibility") or "")
    return grade if grade in {"A", "B", "C", "D"} else None


class _ReverificationRunner:
    """在一次性隔离 worktree 里重放一次干预并观察测试向量。

    与主审查同一纪律：worktree 用完即毁，原仓库一个字节都不动；
    测试范围（声明 nodeid）由原基线向量冻结；跑法与主审查同一条
    管线（Executor + pytest_argv + JUnit XML → TestVector）；恢复
    失败立即失败关闭。
    """

    def __init__(self, *, repo: Path, python: str, nodeids: list[str],
                 deadline: float = 120.0):
        if not nodeids:
            raise IntakeError("REVERIFICATION_ORIGINAL_VECTOR_MISSING",
                              "declared nodeid list is empty")
        self.repo = repo
        self.python = str(python)
        self.nodeids = [str(n) for n in nodeids]
        self.deadline = float(deadline)
        self._worktree: Path | None = None
        self._scratch = Path(tempfile.mkdtemp(prefix="smrv-"))
        self.baseline_vector: TestVector | None = None
        self._runs = 0

    @property
    def sandbox_dir(self) -> Path:
        """沙箱 Executor 的可写 scratch（SandboxedExecutor 需要它）。"""
        return self._scratch / "sandbox"

    def ensure_worktree(self) -> Path:
        if self._worktree is not None:
            return self._worktree
        worktree = self._scratch / "worktree"
        completed = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=self.repo, capture_output=True, text=True)
        if completed.returncode != 0:
            raise IntakeError("REVERIFICATION_WORKTREE_FAILED",
                              completed.stderr.strip()[:200])
        # 复验必须重放"审查当时的工作区状态"，而审查对象是未提交修改：
        # 把当前工作区内容复制进 detached worktree。拷贝清单走
        # _list_repo_source_files（git 跟踪 + 未忽略且存在），与
        # 浏览/快照同一口径——不再 rglob 整树，venv 与 artifacts
        # 既拖慢复验又会把脏环境带进结论。
        for rel in _list_repo_source_files(self.repo):
            parts = PurePosixPath(rel).parts
            dest = worktree.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.repo.joinpath(*parts), dest)
        self._worktree = worktree
        return worktree

    def run_baseline_coverage(self) -> tuple[Path, Path]:
        """基线一次运行同时冻结声明范围与 per-test 覆盖率上下文。

        返回 (覆盖率数据文件, rcfile)。采集口径由 write_rcfile 强制
        统一（dynamic_context=test_function），仓库自己的 coverage
        配置被压掉；跑法与 _run_declared 同一条管线。
        """
        worktree = self.ensure_worktree()
        run_dir = self._scratch / "coverage"
        run_dir.mkdir(parents=True, exist_ok=True)
        rcfile = write_rcfile(run_dir)
        data_file = fresh_data_file(run_dir, "revcheck")
        self._runs += 1
        junit = self._scratch / "junit" / f"run-{self._runs:04d}.xml"
        run_declared(_REPLAY_ADAPTER, self.python, worktree, self.nodeids,
                     junit, deadline=self.deadline,
                     coverage_file=str(data_file),
                     coverage_rcfile=str(rcfile),
                     coverage_source=_REPLAY_ADAPTER.package_root)
        return data_file, rcfile

    def collect_probe(self) -> tuple[bool, str]:
        """pytest --collect-only 探针：净收集 = rc∈(0,5) 且无收集错误。

        适配器没有 collect-only 能力（TypeError）时翻译成
        CollectionUnsupported，让策略失败关闭，绝不悄悄换重放。
        """
        try:
            argv = _REPLAY_ADAPTER.pytest_argv(self.python, self.nodeids,
                                               collect_only=True)
        except TypeError as exc:
            raise reverification.CollectionUnsupported(
                "adapter does not support collect-only") from exc
        self._runs += 1
        r = run_argv(argv, self.ensure_worktree(), self.deadline)
        out = (r.stdout or "") + (r.stderr or "")
        crashed = (r.returncode not in (0, 5)
                   or "errors during collection" in out
                   or "ERROR collecting" in out)
        return (not crashed), out[-400:]

    def stage_intervention(self, intervention: dict) -> tuple[Path, bytes]:
        """把干预应用到隔离 worktree，返回 (目标文件, 原始字节)。

        复用主审查的同一删除变换：保行号置空，块清空时补 pass。复验
        与原实验必须是同一种干预，不是"看起来像"，所以这里只做一次
        变换，backup 始终是原始内容。
        """
        if not isinstance(intervention, dict):
            raise IntakeError("REVERIFICATION_INTERVENTION_INVALID",
                              "intervention payload missing")
        worktree = self.ensure_worktree()
        path = str(intervention.get("path") or "")
        if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise IntakeError("REVERIFICATION_INTERVENTION_INVALID",
                              "intervention path must be a safe repo-relative path")
        target = worktree.joinpath(*PurePosixPath(path).parts)
        if intervention.get("file_removed"):
            backup = target.read_bytes()
            target.unlink()
            return target, backup
        lines = intervention.get("lines") or []
        if not lines:
            raise IntakeError("REVERIFICATION_INTERVENTION_INVALID",
                              "line intervention requires lines")
        backup = target.read_bytes()
        source_lines = backup.decode("utf-8").splitlines()
        for lineno in lines:
            if not 1 <= int(lineno) <= len(source_lines):
                raise IntakeError(
                    "REVERIFICATION_INTERVENTION_INVALID",
                    f"line {lineno} out of range")
        try:
            mutation = mutate.delete(
                backup.decode("utf-8"),
                tuple(int(n) for n in lines))
        except (mutate.InvalidTransform, UnicodeDecodeError) as exc:
            raise IntakeError("REVERIFICATION_INTERVENTION_INVALID",
                              f"replay transform failed: {exc}")
        target.write_text(mutation.text, encoding="utf-8")
        return target, backup

    def restore_and_verify(self) -> bool:
        """恢复后重跑声明范围并核对基线：restored_clean 不是口头承诺。"""
        restored = self._run_declared()
        return restored.identical_to(self.baseline_vector)

    def _run_declared(self) -> TestVector:
        worktree = self.ensure_worktree()
        self._runs += 1
        junit = self._scratch / "junit" / f"run-{self._runs:04d}.xml"
        rr = run_declared(_REPLAY_ADAPTER, self.python, worktree,
                          self.nodeids, junit, deadline=self.deadline)
        return rr.vector

    def run_baseline(self) -> TestVector:
        vector = self._run_declared()
        self.baseline_vector = vector
        return vector

    def replay(self, intervention: dict) -> dict:
        target, backup = self.stage_intervention(intervention)
        try:
            vector = self._run_declared()
            # 恢复后必须重跑一次并核对基线：restored_clean 不是口头承诺。
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(backup)
            restored_vector = self._run_declared()
            return {
                "vector": vector,
                "restored_clean":
                    restored_vector.identical_to(self.baseline_vector),
                "run_id": f"replay-{uuid.uuid4().hex[:12]}",
            }
        finally:
            if backup is not None and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(backup)

    def close(self) -> None:
        """显式收尾：销毁隔离 worktree，原仓库自始至终未动。"""
        self._teardown()

    def _teardown(self) -> None:
        subprocess.run(["git", "worktree", "remove", "--force",
                        str(self._worktree)],
                       cwd=self.repo, capture_output=True, text=True)
        self._worktree = None
        shutil.rmtree(self._scratch, ignore_errors=True)


#: 复验与主审查走同一条 pytest 管线。本地仓库审查的适配器是审查
#: 进程即时构造并注册的（inputs.from_local_repo 同口径），跨进程
#: 复验拿不到它；这里只需要 pytest_argv 的白名单与 junit 参数，
#: clone/venv 字段不参与，因此用一个固定本地适配器即可。
_REPLAY_ADAPTER = RepoAdapter(repo="local", clone_dir="local",
                              package_root=".")


def _source_language(path: str) -> str:
    return _SOURCE_LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix, "text")


def _clean_source_parts(raw: object) -> tuple[str, ...]:
    """把前端传来的路径收敛成白名单内的相对 POSIX 分量。

    绝对路径、盘符、反斜杠、空分量、`..`、`.git` 一律在字符串层面
    拒绝：这一步发生在任何文件系统访问之前，所以被拒绝的输入不可能
    顺带泄露服务器上是否存在某个路径。
    """
    if not isinstance(raw, str):
        raise IntakeError("SOURCE_PATH_INVALID", "path must be a string")
    candidate = raw.strip()
    if (not candidate or len(candidate) > 1024 or "\x00" in candidate
            or "\\" in candidate or candidate.startswith("/")
            or (len(candidate) > 1 and candidate[1] == ":")):
        raise IntakeError("SOURCE_PATH_INVALID", raw[:120])
    parts = PurePosixPath(candidate).parts
    if not parts or any(part in {"", ".", "..", ".git"} for part in parts):
        raise IntakeError("SOURCE_PATH_INVALID", raw[:120])
    return parts


def _list_repo_source_files(repo: Path) -> list[str]:
    """可浏览文件 = git 跟踪文件 + 未忽略的新文件，且当前真实存在。

    被删除的文件不出列表（工作区里已经没有了）；子模块目录在父仓库的
    ls-files 里是 gitlink，is_file() 为假，同样自然出局。
    """
    tracked = run_git(["ls-files", "-z"], cwd=repo, check=True,
                      timeout=30).stdout
    untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"],
                        cwd=repo, check=True, timeout=30).stdout
    seen: set[str] = set()
    out: list[str] = []
    for chunk in (tracked, untracked):
        for raw in chunk.split("\x00"):
            rel = raw.strip().strip('"')
            if not rel:
                continue
            try:
                parts = _clean_source_parts(rel)
            except IntakeError:
                continue
            posix = PurePosixPath(*parts).as_posix()
            if posix in seen:
                continue
            if repo.joinpath(*parts).is_file():
                seen.add(posix)
                out.append(posix)
    return out


def _changed_repo_source_paths(repo: Path) -> set[str]:
    """本次补丁触碰的文件：对 HEAD 的差异 + 未忽略的未跟踪文件。

    审查对象就是这两类（inputs.from_local_repo 的同一口径），所以
    `changed` 与"这次审查所检查的新增代码"保持同一边界。
    """
    diff = run_git(["diff", "--name-only", "-z", "HEAD"], cwd=repo,
                   check=True, timeout=30).stdout
    untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"],
                        cwd=repo, check=True, timeout=30).stdout
    out: set[str] = set()
    for chunk in (diff, untracked):
        for raw in chunk.split("\x00"):
            rel = raw.strip().strip('"')
            if not rel:
                continue
            try:
                parts = _clean_source_parts(rel)
            except IntakeError:
                continue
            out.add(PurePosixPath(*parts).as_posix())
    return out


def _source_evidence_counts(review_dir: Path) -> dict[str, int]:
    """每个文件上有多少行拿到了机器证据结论（render_model 的标注行）。"""
    counts: dict[str, int] = {}
    candidates = sorted((review_dir / "artifacts").glob("*/report.json"))
    if not candidates:
        return counts
    try:
        report = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return counts
    lines = ((report.get("render_model") or {}).get("lines") or [])
    for line in lines:
        if not isinstance(line, dict):
            continue
        path = line.get("file")
        label = line.get("label")
        if (isinstance(path, str) and path and isinstance(label, str)
                and label):
            counts[path] = counts.get(path, 0) + 1
    return counts


def _source_comment_counts(review_dir: Path) -> dict[str, int]:
    """每个文件上的人工评论数；评论存储由 P5 落地，这里先接同一目录。"""
    counts: dict[str, int] = {}
    for record in sorted((review_dir / "comments").glob("*.json")):
        try:
            raw = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        anchor = raw.get("anchor") if isinstance(raw, dict) else None
        path = anchor.get("path") if isinstance(anchor, dict) else None
        if isinstance(path, str) and path:
            counts[path] = counts.get(path, 0) + 1
    return counts


def _annotation_entry_digest(entry: dict) -> str:
    body = {key: entry[key] for key in
            ("seq", "event_id", "kind", "occurred_at", "data", "prev_sha256")}
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _annotation_chain_path(review_dir: Path) -> Path:
    return review_dir / "annotations" / "events.jsonl"


def _read_annotation_events(review_dir: Path) -> list[dict]:
    """读回并逐条校验注释事件哈希链；链被改写就整体失败关闭。"""
    path = _annotation_chain_path(review_dir)
    if not path.exists():
        return []
    entries: list[dict] = []
    prev = _ANNOTATION_CHAIN_ZERO
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntakeError("ANNOTATION_CHAIN_INVALID",
                              f"event line {index} is not valid JSON") from exc
        if (not isinstance(entry, dict)
                or entry.get("seq") != len(entries) + 1
                or entry.get("prev_sha256") != prev
                or entry.get("entry_sha256") != _annotation_entry_digest(entry)):
            raise IntakeError("ANNOTATION_CHAIN_INVALID",
                              f"event line {index} breaks the hash chain")
        prev = entry["entry_sha256"]
        entries.append(entry)
    return entries


def _append_annotation_event(review_dir: Path, kind: str, data: dict) -> dict:
    """把一条人工注释事件追加进独立哈希链（调用方须已持有 manager 锁）。"""
    entries = _read_annotation_events(review_dir)
    entry = {
        "seq": len(entries) + 1,
        "event_id": f'ann-{len(entries) + 1:06d}',
        "kind": kind,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "prev_sha256": entries[-1]["entry_sha256"] if entries
        else _ANNOTATION_CHAIN_ZERO,
    }
    entry["entry_sha256"] = _annotation_entry_digest(entry)
    path = _annotation_chain_path(review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return entry


def _comment_records(review_dir: Path) -> list[dict]:
    """按 comment_id 顺序读回全部评论记录（含已撤回/过期）。"""
    directory = review_dir / "comments"
    records: list[dict] = []
    if directory.is_dir():
        for path in sorted(directory.glob("cmt-*.json")):
            raw = _read_json(path)
            if isinstance(raw, dict) and raw.get("comment_id"):
                records.append(raw)
    records.sort(key=lambda item: str(item.get("comment_id") or ""))
    return records


def _next_comment_sequence(records: list[dict]) -> int:
    highest = 0
    for record in records:
        suffix = str(record.get("comment_id") or "").rsplit("-", 1)[-1]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def _review_claim_surface(review_dir: Path) -> tuple[set[str], set[str]]:
    """这次审查实际发出过哪些机器主张与证据记录 ID。

    失败关闭：没有 report.json 就没有任何可绑定的机器主张。绑定面
    取 render_model 的 unit_id 与逐行 evidence_ids，并在账本可读时
    并入全部 record_id——评论只能质疑真实存在过的主张。
    """
    claims: set[str] = set()
    evidence: set[str] = set()
    candidates = sorted((review_dir / "artifacts").glob("*/report.json"))
    if not candidates:
        return claims, evidence
    try:
        report = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return claims, evidence
    model = report.get("render_model") if isinstance(report, dict) else None
    for line in ((model or {}).get("lines") or []):
        if not isinstance(line, dict):
            continue
        unit_id = line.get("unit_id")
        if isinstance(unit_id, str) and unit_id:
            claims.add(unit_id)
        for evidence_id in line.get("evidence_ids") or []:
            if isinstance(evidence_id, str) and evidence_id:
                claims.add(evidence_id)
                evidence.add(evidence_id)
    for unit in ((model or {}).get("units") or []):
        if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str):
            claims.add(unit["unit_id"])
    try:
        for row in ledger_store.read(candidates[0].parent / ledger_store.FILENAME):
            record_id = row.get("record_id")
            if isinstance(record_id, str) and record_id:
                claims.add(record_id)
                evidence.add(record_id)
    except (OSError, ValueError, RuntimeError):
        pass
    try:
        # 策略 4 签证 PASS 追加的受保护主张也是真实发出过的机器主张：
        # 评论可以直接质疑它。它没有可重放的原实验，再质疑会按既有
        # 资格闸 fail-closed 扣留——这是诚实行为，不是漏洞。
        for settled in _reverification_records(review_dir):
            delta = settled.get("evidence_delta")
            if (settled.get("outcome") == "claim.confirmed"
                    and isinstance(delta, dict) and delta.get("new_claim")):
                claims.add(reverification.protected_claim_id(
                    str(settled.get("reverification_id") or "")))
    except (OSError, ValueError, RuntimeError):
        pass
    return claims, evidence


def _validate_comment_text(raw: dict) -> tuple[str, str, str]:
    """评论正文是纯文本：长度、控制字符与 kind 都在入队前挡住。"""
    author = raw.get("author")
    body = raw.get("body")
    kind = raw.get("kind")
    if (not isinstance(author, str) or not isinstance(body, str)
            or not isinstance(kind, str)):
        raise IntakeError("COMMENT_REQUEST_INVALID",
                          "author, body and kind must be strings")
    author = author.strip()
    if not 1 <= len(author) <= COMMENT_AUTHOR_MAX_CHARS:
        raise IntakeError("COMMENT_AUTHOR_INVALID",
                          f"author must be 1-{COMMENT_AUTHOR_MAX_CHARS} chars")
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not 1 <= len(body) <= COMMENT_BODY_MAX_CHARS:
        raise IntakeError("COMMENT_BODY_INVALID",
                          f"body must be 1-{COMMENT_BODY_MAX_CHARS} plain-text chars")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in body):
        raise IntakeError("COMMENT_BODY_INVALID",
                          "body contains control characters")
    if kind not in COMMENT_KINDS:
        raise IntakeError("COMMENT_KIND_INVALID",
                          "kind must be one of " + ", ".join(sorted(COMMENT_KINDS)))
    return author, body, kind


def _validate_comment_reference(raw: dict) -> tuple[str | None, list[str]]:
    """claim_id / evidence_ids 只做类型与规模校验，存在性由主张面把关。"""
    claim_id = raw.get("claim_id")
    if claim_id is not None and (not isinstance(claim_id, str)
                                 or not 1 <= len(claim_id) <= 200):
        raise IntakeError("COMMENT_CLAIM_INVALID",
                          "claim_id must be a 1-200 char string")
    evidence_ids = raw.get("evidence_ids", [])
    if (not isinstance(evidence_ids, list) or len(evidence_ids) > 20
            or any(not isinstance(item, str) or not item
                   for item in evidence_ids)):
        raise IntakeError("COMMENT_EVIDENCE_INVALID",
                          "evidence_ids must be up to 20 non-empty strings")
    deduped = list(dict.fromkeys(evidence_ids))
    return claim_id, deduped
