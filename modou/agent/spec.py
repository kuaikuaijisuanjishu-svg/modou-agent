"""Closed, auditable task contract for natural-language evidence reviews."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "review-spec-v2"
DEFAULT_GOAL = "审查补丁新增代码的证据边界"
DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"
_LANGUAGES = frozenset({"auto", "python", "typescript"})
_INPUT_LANGUAGES = frozenset({"auto", "zh", "en", "mixed"})
_FOCUSES = frozenset({"evidence-boundary", "named-regression", "coverage-gap",
                      "call-path", "budget-first"})
_RISKS = frozenset({"L0", "L1", "L2", "L3"})
_DATA_CATEGORIES = frozenset({"metadata", "diff_summary", "selected_snippets",
                              "test_facts", "diagnostics", "memory_rules"})
_BUDGET_RE = re.compile(
    r"(?<!\d)(\d{1,4})\s*(?:分钟|分鐘|mins?|minutes?|秒|seconds?|s)(?![a-z])", re.I)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ReviewSpecError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _closed(raw: Any, allowed: set[str], code: str, field_name: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ReviewSpecError(code, f"{field_name} has unsupported fields")
    return raw


def _strings(value: Any, field_name: str, *, limit: int = 80,
             allowed: frozenset[str] | None = None) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReviewSpecError("SPEC_SCHEMA_INVALID", f"{field_name} must be a string array")
    if len(value) > limit:
        raise ReviewSpecError("SPEC_LIMIT_EXCEEDED", field_name)
    cleaned = tuple(item.strip() for item in value if item.strip())
    if len(cleaned) != len(set(cleaned)):
        raise ReviewSpecError("SPEC_DUPLICATE", field_name)
    if allowed is not None and set(cleaned) - allowed:
        raise ReviewSpecError("SPEC_VALUE_INVALID", field_name)
    return cleaned


def _integer(raw: dict, key: str, default: int, minimum: int, maximum: int,
             code: str = "SPEC_CONSTRAINTS_INVALID") -> int:
    try:
        value = int(raw.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ReviewSpecError(code, f"{key} must be an integer") from exc
    if isinstance(raw.get(key), bool) or not minimum <= value <= maximum:
        raise ReviewSpecError(code, f"{key} must be between {minimum} and {maximum}")
    return value


def _boolean(raw: dict, key: str, default: bool, code: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ReviewSpecError(code, f"{key} must be boolean")
    return value


def _hash(value: Any, name: str) -> str:
    text = str(value or "")
    if text and not _HEX64.fullmatch(text):
        raise ReviewSpecError("SPEC_HASH_INVALID", f"{name} must be sha256")
    return text


def _memory_hash(value: Any) -> str:
    try:
        return _hash(value, "memory_snapshot_sha256")
    except ReviewSpecError as exc:
        raise ReviewSpecError("SPEC_MEMORY_HASH_INVALID", exc.detail) from exc


@dataclass(frozen=True)
class ReviewSource:
    kind: str
    repo_id: str
    base_ref: str = ""
    target_ref: str = ""
    target_commit: str = ""
    workspace_snapshot_sha256: str = ""

    @classmethod
    def parse(cls, raw: Any) -> "ReviewSource":
        raw = _closed(raw, {"kind", "repo_id", "base_ref", "target_ref",
                            "target_commit", "workspace_snapshot_sha256"},
                      "SPEC_SOURCE_INVALID", "source")
        kind, repo_id = str(raw.get("kind") or ""), str(raw.get("repo_id") or "")
        if kind != "local" or not repo_id or len(repo_id) > 128:
            raise ReviewSpecError("SPEC_SOURCE_INVALID",
                                  "source must name one registered local repository")
        target_commit = str(raw.get("target_commit") or "")
        if target_commit and not re.fullmatch(r"[0-9a-f]{40}", target_commit):
            raise ReviewSpecError("SPEC_SOURCE_INVALID", "target_commit must be a full commit")
        return cls(kind, repo_id, str(raw.get("base_ref") or "")[:200],
                   str(raw.get("target_ref") or "")[:200], target_commit,
                   _hash(raw.get("workspace_snapshot_sha256"),
                         "workspace_snapshot_sha256"))


@dataclass(frozen=True)
class ReviewScope:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    max_modified_files: int = 5
    max_changed_lines: int = 400

    @classmethod
    def parse(cls, raw: Any) -> "ReviewScope":
        raw = _closed(raw, {"include", "exclude", "test_files", "max_modified_files",
                            "max_changed_lines"}, "SPEC_SCOPE_INVALID", "scope")
        return cls(_strings(raw.get("include"), "scope.include"),
                   _strings(raw.get("exclude"), "scope.exclude"),
                   _strings(raw.get("test_files"), "scope.test_files"),
                   _integer(raw, "max_modified_files", 5, 1, 50, "SPEC_SCOPE_INVALID"),
                   _integer(raw, "max_changed_lines", 400, 1, 5000, "SPEC_SCOPE_INVALID"))


@dataclass(frozen=True)
class ResourceConstraints:
    budget_seconds: int = 300
    max_input_tokens: int = 12000
    max_output_tokens: int = 2000
    max_total_tokens: int = 20000
    max_cost_cny_fen: int = 500
    max_cpu_seconds: int = 300
    max_memory_bytes: int = 1_073_741_824
    max_disk_bytes: int = 536_870_912
    max_processes: int = 32
    max_concurrency: int = 1
    max_download_bytes: int = 0
    allow_network: bool = False
    allow_dependency_install: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "ResourceConstraints":
        raw = _closed(raw, {"budget_seconds", "max_input_tokens", "max_output_tokens",
                            "max_total_tokens", "max_cost_cny_fen", "max_cpu_seconds",
                            "max_memory_bytes", "max_disk_bytes", "max_processes",
                            "max_concurrency", "max_download_bytes", "allow_network",
                            "allow_dependency_install"},
                      "SPEC_CONSTRAINTS_INVALID", "constraints")
        value = cls(
            _integer(raw, "budget_seconds", 300, 1, 3600),
            _integer(raw, "max_input_tokens", 12000, 1, 2_000_000),
            _integer(raw, "max_output_tokens", 2000, 1, 100_000),
            _integer(raw, "max_total_tokens", 20000, 1, 2_000_000),
            _integer(raw, "max_cost_cny_fen", 500, 0, 5000),
            _integer(raw, "max_cpu_seconds", 300, 1, 7200),
            _integer(raw, "max_memory_bytes", 1_073_741_824, 16_777_216, 68_719_476_736),
            _integer(raw, "max_disk_bytes", 536_870_912, 1_048_576, 1_099_511_627_776),
            _integer(raw, "max_processes", 32, 1, 1024),
            _integer(raw, "max_concurrency", 1, 1, 8),
            _integer(raw, "max_download_bytes", 0, 0, 10_737_418_240),
            _boolean(raw, "allow_network", False, "SPEC_CONSTRAINTS_INVALID"),
            _boolean(raw, "allow_dependency_install", False, "SPEC_CONSTRAINTS_INVALID"),
        )
        if value.max_total_tokens < value.max_output_tokens:
            raise ReviewSpecError("SPEC_CONSTRAINTS_INVALID",
                                  "max_total_tokens must cover max_output_tokens")
        if not value.allow_network and value.max_download_bytes:
            raise ReviewSpecError("SPEC_CONSTRAINTS_INVALID",
                                  "download budget requires network authorization")
        if value.allow_dependency_install and not value.allow_network:
            raise ReviewSpecError("SPEC_CONSTRAINTS_INVALID",
                                  "dependency installation requires network authorization")
        return value


@dataclass(frozen=True)
class AutonomyPolicy:
    model_provider: str = "deterministic"
    allowed_tools: tuple[str, ...] = ()
    max_risk: str = "L2"
    allow_repair_branch: bool = False
    allow_local_commit: bool = False
    allow_generated_tests: bool = False
    allow_dependency_install: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "AutonomyPolicy":
        raw = _closed(raw, {"model_provider", "allowed_tools", "max_risk",
                            "allow_repair_branch", "allow_local_commit",
                            "allow_generated_tests", "allow_dependency_install"},
                      "SPEC_AUTONOMY_INVALID", "autonomy_policy")
        provider = str(raw.get("model_provider") or "deterministic")
        inferred_write = bool(raw.get("allow_repair_branch") or
                              raw.get("allow_local_commit") or
                              raw.get("allow_dependency_install"))
        risk = str(raw.get("max_risk") or ("L3" if inferred_write else "L2")).upper()
        if provider not in {"deterministic", "live"} or risk not in _RISKS:
            raise ReviewSpecError("SPEC_AUTONOMY_INVALID", "provider or risk is invalid")
        repair = _boolean(raw, "allow_repair_branch", False, "SPEC_AUTONOMY_INVALID")
        commit = _boolean(raw, "allow_local_commit", repair, "SPEC_AUTONOMY_INVALID")
        install = _boolean(raw, "allow_dependency_install", False, "SPEC_AUTONOMY_INVALID")
        if commit and not repair:
            raise ReviewSpecError("SPEC_AUTONOMY_INVALID",
                                  "local commit requires repair branch authorization")
        if (repair or commit or install) and risk != "L3":
            raise ReviewSpecError("SPEC_AUTONOMY_INVALID", "write capability requires L3")
        return cls(provider, _strings(raw.get("allowed_tools"),
                                      "autonomy_policy.allowed_tools", limit=64), risk,
                   repair, commit,
                   _boolean(raw, "allow_generated_tests", False, "SPEC_AUTONOMY_INVALID"),
                   install)


@dataclass(frozen=True)
class TestPolicy:
    auto_discover: bool = True
    user_specified: tuple[str, ...] = ()
    allow_generated: bool = False
    flaky_policy: str = "report_without_washing"

    @classmethod
    def parse(cls, raw: Any) -> "TestPolicy":
        raw = _closed(raw, {"auto_discover", "user_specified", "allow_generated",
                            "flaky_policy"}, "SPEC_TEST_POLICY_INVALID", "test_policy")
        flaky = str(raw.get("flaky_policy") or "report_without_washing")
        if flaky not in {"report_without_washing", "stop", "sample_and_downgrade"}:
            raise ReviewSpecError("SPEC_TEST_POLICY_INVALID", "flaky_policy is invalid")
        return cls(_boolean(raw, "auto_discover", True, "SPEC_TEST_POLICY_INVALID"),
                   _strings(raw.get("user_specified"), "test_policy.user_specified"),
                   _boolean(raw, "allow_generated", False, "SPEC_TEST_POLICY_INVALID"), flaky)


@dataclass(frozen=True)
class DataPolicy:
    model_data_categories: tuple[str, ...] = ("metadata", "diff_summary", "test_facts")
    redact_secrets: bool = True
    allow_private_source_fulltext: bool = False
    retention_days: int = 30
    failed_candidate_retention_days: int = 7
    telemetry_enabled: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "DataPolicy":
        raw = _closed(raw, {"model_data_categories", "redact_secrets",
                            "allow_private_source_fulltext", "retention_days",
                            "failed_candidate_retention_days", "telemetry_enabled"},
                      "SPEC_DATA_POLICY_INVALID", "data_policy")
        categories = (_strings(raw.get("model_data_categories"),
                               "data_policy.model_data_categories",
                               allowed=_DATA_CATEGORIES) if "model_data_categories" in raw
                      else ("metadata", "diff_summary", "test_facts"))
        if _boolean(raw, "allow_private_source_fulltext", False, "SPEC_DATA_POLICY_INVALID"):
            raise ReviewSpecError("SPEC_DATA_POLICY_INVALID",
                                  "private source fulltext is prohibited in v2")
        return cls(categories,
                   _boolean(raw, "redact_secrets", True, "SPEC_DATA_POLICY_INVALID"), False,
                   _integer(raw, "retention_days", 30, 0, 365, "SPEC_DATA_POLICY_INVALID"),
                   _integer(raw, "failed_candidate_retention_days", 7, 0, 90,
                            "SPEC_DATA_POLICY_INVALID"),
                   _boolean(raw, "telemetry_enabled", False, "SPEC_DATA_POLICY_INVALID"))


@dataclass(frozen=True)
class OutputPolicy:
    review_bundle: bool = True
    human_report: bool = True
    candidate_patch: bool = False
    local_branch: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "OutputPolicy":
        raw = _closed(raw, {"review_bundle", "human_report", "candidate_patch",
                            "local_branch"}, "SPEC_OUTPUT_POLICY_INVALID", "output_policy")
        return cls(*(_boolean(raw, key, default, "SPEC_OUTPUT_POLICY_INVALID")
                     for key, default in (("review_bundle", True), ("human_report", True),
                                          ("candidate_patch", False), ("local_branch", False))))


@dataclass(frozen=True)
class ManualOverrides:
    fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: Any) -> "ManualOverrides":
        raw = _closed(raw, {"fields", "conflicts"}, "SPEC_OVERRIDES_INVALID",
                      "manual_overrides")
        return cls(_strings(raw.get("fields"), "manual_overrides.fields"),
                   _strings(raw.get("conflicts"), "manual_overrides.conflicts"))


@dataclass(frozen=True)
class ModelPolicy:
    provider: str = "deterministic"
    model_category: str = "low_cost"
    max_context_tokens: int = 16000
    fallback: str = "deterministic"

    @classmethod
    def parse(cls, raw: Any) -> "ModelPolicy":
        raw = _closed(raw, {"provider", "model_category", "max_context_tokens", "fallback"},
                      "SPEC_MODEL_POLICY_INVALID", "model_policy")
        provider, category = str(raw.get("provider") or "deterministic"), str(
            raw.get("model_category") or "low_cost")
        fallback = str(raw.get("fallback") or "deterministic")
        if provider not in {"deterministic", "configured"} or category not in {
                "low_cost", "standard", "high_capability"} or fallback not in {
                "deterministic", "stop"}:
            raise ReviewSpecError("SPEC_MODEL_POLICY_INVALID", "model policy value is invalid")
        return cls(provider, category,
                   _integer(raw, "max_context_tokens", 16000, 1024, 2_000_000,
                            "SPEC_MODEL_POLICY_INVALID"), fallback)


@dataclass(frozen=True)
class ReviewSpec:
    instruction: str
    source: ReviewSource
    goal: str
    input_language: str = "auto"
    success_conditions: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    scope: ReviewScope = field(default_factory=ReviewScope)
    constraints: ResourceConstraints = field(default_factory=ResourceConstraints)
    autonomy_policy: AutonomyPolicy = field(default_factory=AutonomyPolicy)
    test_policy: TestPolicy = field(default_factory=TestPolicy)
    data_policy: DataPolicy = field(default_factory=DataPolicy)
    output_policy: OutputPolicy = field(default_factory=OutputPolicy)
    manual_overrides: ManualOverrides = field(default_factory=ManualOverrides)
    memory_snapshot_sha256: str = ""
    model_policy: ModelPolicy = field(default_factory=ModelPolicy)
    language: str = "auto"
    review_focus: str = "evidence-boundary"
    created_at: str = DEFAULT_CREATED_AT
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def parse(cls, raw: Any) -> "ReviewSpec":
        if not isinstance(raw, dict):
            raise ReviewSpecError("SPEC_NOT_OBJECT", "review spec must be an object")
        allowed = {"instruction", "input_language", "source", "goal", "success_conditions",
                   "non_goals", "scope", "constraints", "autonomy_policy", "test_policy",
                   "data_policy", "output_policy", "manual_overrides",
                   "memory_snapshot_sha256", "model_policy", "language", "review_focus",
                   "created_at", "schema_version"}
        if set(raw) - allowed:
            raise ReviewSpecError("SPEC_EXTRA_FIELDS", str(sorted(set(raw) - allowed)))
        if raw.get("schema_version") not in {None, SCHEMA_VERSION}:
            raise ReviewSpecError("SPEC_SCHEMA_VERSION_INVALID", str(raw.get("schema_version")))
        instruction = str(raw.get("instruction") or "").strip()
        goal = str(raw.get("goal") or instruction or DEFAULT_GOAL).strip()[:500]
        if not instruction and not raw.get("goal"):
            raise ReviewSpecError("SPEC_GOAL_REQUIRED", "instruction or goal is required")
        language, input_language = str(raw.get("language") or "auto"), str(
            raw.get("input_language") or "auto")
        if language not in _LANGUAGES or input_language not in _INPUT_LANGUAGES:
            raise ReviewSpecError("SPEC_LANGUAGE_INVALID", f"{language}/{input_language}")
        focus = str(raw.get("review_focus") or "evidence-boundary")
        if focus not in _FOCUSES:
            raise ReviewSpecError("SPEC_REVIEW_FOCUS_INVALID", focus)
        created_at = str(raw.get("created_at") or DEFAULT_CREATED_AT)
        try:
            parsed_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ReviewSpecError("SPEC_CREATED_AT_INVALID", created_at) from exc
        if parsed_at.tzinfo is None:
            raise ReviewSpecError("SPEC_CREATED_AT_INVALID", "created_at needs a timezone")
        constraints_raw = raw.get("constraints")
        constraints = ResourceConstraints.parse(constraints_raw)
        if constraints_raw is None:
            inferred = _instruction_budget(instruction)
            if inferred is not None:
                constraints = ResourceConstraints(budget_seconds=inferred)
        autonomy, tests = AutonomyPolicy.parse(raw.get("autonomy_policy")), TestPolicy.parse(
            raw.get("test_policy"))
        output = OutputPolicy.parse(raw.get("output_policy"))
        if autonomy.allow_generated_tests != tests.allow_generated:
            if "autonomy_policy" in raw and "test_policy" in raw:
                raise ReviewSpecError("SPEC_POLICY_CONFLICT", "generated-test permissions disagree")
            tests = TestPolicy(tests.auto_discover, tests.user_specified,
                               autonomy.allow_generated_tests, tests.flaky_policy)
        if autonomy.allow_repair_branch != output.local_branch:
            if "autonomy_policy" in raw and "output_policy" in raw:
                raise ReviewSpecError("SPEC_POLICY_CONFLICT", "local-branch permissions disagree")
            output = OutputPolicy(output.review_bundle, output.human_report,
                                  autonomy.allow_repair_branch, autonomy.allow_repair_branch)
        if autonomy.allow_dependency_install != constraints.allow_dependency_install and (
                "autonomy_policy" in raw and "constraints" in raw):
            raise ReviewSpecError("SPEC_POLICY_CONFLICT", "install permissions disagree")
        model = ModelPolicy.parse(raw.get("model_policy"))
        expected_provider = "configured" if autonomy.model_provider == "live" else "deterministic"
        if "model_policy" in raw and model.provider != expected_provider:
            raise ReviewSpecError("SPEC_POLICY_CONFLICT", "model provider policies disagree")
        if "model_policy" not in raw:
            model = ModelPolicy(provider=expected_provider)
        return cls(instruction[:2000], ReviewSource.parse(raw.get("source")), goal,
                   input_language, _strings(raw.get("success_conditions"), "success_conditions"),
                   _strings(raw.get("non_goals"), "non_goals"),
                   ReviewScope.parse(raw.get("scope")), constraints, autonomy, tests,
                   DataPolicy.parse(raw.get("data_policy")), output,
                   ManualOverrides.parse(raw.get("manual_overrides")),
                   _memory_hash(raw.get("memory_snapshot_sha256")), model,
                   language, focus, created_at)

    def as_dict(self) -> dict:
        raw = asdict(self)
        for name in ("success_conditions", "non_goals"):
            raw[name] = list(raw[name])
        for name in ("scope", "autonomy_policy", "test_policy", "data_policy",
                     "manual_overrides"):
            raw[name] = {key: list(value) if isinstance(value, tuple) else value
                         for key, value in raw[name].items()}
        return raw

    @property
    def sha256(self) -> str:
        canonical = json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _instruction_budget(instruction: str) -> int | None:
    match = _BUDGET_RE.search(instruction)
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(0).lower()
    seconds = value * 60 if any(token in unit for token in ("分", "min")) else value
    return seconds if 1 <= seconds <= 3600 else None
