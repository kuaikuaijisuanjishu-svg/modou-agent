"""预授权自动采用策略（T04）：持久账本 + 准入判定，不执行采用。

实现负责人：子智能体A。本模块只回答"这次自动采用是否被允许"并记账；
封存/规则保护/版本检查/恢复等执行语义仍走 tasks.py 的人工采用路径
（接线点见各方法 docstring 与 docs/interfaces/jobs.md）。

设计不变量（默认全部 fail-closed）：

1. 默认关闭：``enabled`` 默认 False；没有任何配置推导能打开它，只有
   构造方显式传入。授权记录（grant）必须事先人工登记，模块自身从不
   生成或放宽 grant。
2. 每任务额度：按 ``task_id`` 持久计数——每任务最多两轮候选生成、
   自动采用一次；round_id 只作记录，轮次增加不重置额度。
3. 滚动窗口：同一仓库指纹滚动一小时最多三次自动采用"尝试"（预占即
   计数，无论后续成败/恢复），跨审查、跨任务、跨进程重启持久。
4. 授权时效：默认两小时失效。更严格的登记时限继续生效；更宽松的旧
   配置不能绕过新上限——有效期限一律取 ``min(登记值, 登记时刻+上限)``。
5. 额度预占：写入前先预占（``assert_allowed`` 追加 attempt 后才返回）；
   失败与恢复不重复扣账（计数以账本条目为准，``settle`` 幂等且不返还
   额度）；恢复路径也必须先过同样的闸门，不能成为绕限额的入口。
6. 行为依据：业务源码修复必须关联事先冻结的行为依据或可复现失败
   记录（id + sha256），缺失或形状不完整即拒绝。
7. 复用人工路径：本模块不提供任何采用执行；也正因如此——
8. 硬边界：本模块不存在自动提交/推送/合并/依赖变更/权限变更的接口。
9. 触发标记：每次自动写入必须携带 ``trigger_source``（复用 jobs 契约
   字段，adoption_apply 必填）；同源重复触发直接拒绝（循环抑制）。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

from modou.server.control import IntakeError, _atomic_json

__all__ = ["AutoAdoptionPolicy", "LEDGER_SCHEMA_VERSION"]

LEDGER_SCHEMA_VERSION = "auto-adoption-ledger-v1"

# 新上限（只能更严，不能更松；构造参数同样被钳制在这之下）。
DEFAULT_WINDOW_SECONDS = 3600.0          # 滚动窗口：一小时
DEFAULT_WINDOW_ATTEMPT_LIMIT = 3         # 窗口内最多三次"尝试"
DEFAULT_AUTHORIZATION_TTL_SECONDS = 7200.0  # 授权默认两小时失效
DEFAULT_MAX_ADOPTIONS_PER_TASK = 1       # 每任务自动采用一次
DEFAULT_MAX_CANDIDATE_ROUNDS_PER_TASK = 2   # 每任务最多两轮候选生成

_GRANT_FIELDS = {"grant_id", "repo_fingerprint", "allowed_paths",
                 "valid_until", "budget_seconds_max", "authorized_by",
                 "created_at"}
_ATTEMPT_FIELDS = {"attempt_id", "grant_id", "task_id", "round_id",
                   "criterion_id", "repo_fingerprint", "paths",
                   "budget_seconds", "trigger_source", "basis",
                   "reserved_at", "status", "settled_at", "detail"}
_ROUND_FIELDS = {"task_id", "round_id", "noted_at"}
_BASIS_KINDS = {"behavioral_basis", "failure_record"}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_RE = re.compile(r"[A-Za-z0-9._:/-]{1,200}")
_SETTLE_STATUSES = {"applied", "failed", "recovered"}


def _fail(code: str, detail: str):
    raise IntakeError(code, detail)


def _bounded_text(value, name: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail("AUTO_ADOPTION_REQUEST_INVALID",
              f"{name} must be a nonempty bounded string")
    return value.strip()


def _safe_relative_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > 400:
        _fail("AUTO_ADOPTION_REQUEST_INVALID", "paths entries must be bounded strings")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        _fail("AUTO_ADOPTION_REQUEST_INVALID", f"paths entry must be repo-relative: {value}")
    return path.as_posix()


class AutoAdoptionPolicy:
    """预授权自动采用的准入策略与持久账本。

    构造参数 ``path`` 注入账本 JSON 位置（原子写）。其余限额外显式传入
    时只能收紧（取与内置上限的 min），不能放宽。账本不可读/损坏时所有
    判定失败关闭。

    接线点（集成人，``tasks.py`` 冻结边界外的唯一两处调用）：

    - ``_auto_confirm`` 开头（现有闸门之后、任何写入之前）::

          policy = getattr(self.manager, "auto_adoption_policy", None)
          if policy is not None:
              attempt = policy.assert_allowed(
                  task_id=task["task_id"],
                  round_id=str(c.get("round_id") or ""),
                  criterion_id=c["criterion_id"],
                  repo_fingerprint=self.manager._repo_fingerprint(repo),
                  paths=list((c.get("candidate") or {}).get("paths") or []),
                  budget_seconds=(c.get("adoption_plan") or {}).get("budget_seconds"),
                  trigger_source=...,          # 见 jobs.md 循环抑制
                  basis=c.get("behavioral_basis"),
                  rule_protection=(c.get("candidate") or {}).get("rule_protection"))
          # 将 attempt["attempt_id"] 存到 criterion（如 c["auto_attempt_id"]）。

    - ``_confirm`` 成功/失败返回路径之后（仅当该次确认为自动采用）::

          policy.settle(attempt_id, status="applied" | "failed")

      ``_recover`` 完成恢复后以 ``status="recovered"`` 结算；恢复前若需
      要再次写入，仍必须先 ``assert_allowed``（额度不返还）。
    """

    def __init__(self, path, *, enabled: bool = False,
                 window_seconds: float = DEFAULT_WINDOW_SECONDS,
                 window_attempt_limit: int = DEFAULT_WINDOW_ATTEMPT_LIMIT,
                 authorization_ttl_cap_seconds: float = DEFAULT_AUTHORIZATION_TTL_SECONDS,
                 max_adoptions_per_task: int = DEFAULT_MAX_ADOPTIONS_PER_TASK,
                 max_candidate_rounds_per_task: int = DEFAULT_MAX_CANDIDATE_ROUNDS_PER_TASK):
        self.path = Path(path)
        self.enabled = bool(enabled)
        # 外显限额外只能收紧：与内置上限取 min，防止宽松配置绕过新约束。
        self.window_seconds = min(float(window_seconds), DEFAULT_WINDOW_SECONDS)
        self.window_attempt_limit = min(int(window_attempt_limit),
                                        DEFAULT_WINDOW_ATTEMPT_LIMIT)
        self.authorization_ttl_cap_seconds = min(
            float(authorization_ttl_cap_seconds), DEFAULT_AUTHORIZATION_TTL_SECONDS)
        self.max_adoptions_per_task = min(int(max_adoptions_per_task),
                                          DEFAULT_MAX_ADOPTIONS_PER_TASK)
        self.max_candidate_rounds_per_task = min(int(max_candidate_rounds_per_task),
                                                 DEFAULT_MAX_CANDIDATE_ROUNDS_PER_TASK)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ ledger

    def _load(self) -> dict:
        """读取账本；缺失视为空账本，损坏失败关闭。"""
        if not self.path.exists():
            return {"schema_version": LEDGER_SCHEMA_VERSION,
                    "grants": [], "candidate_rounds": [], "attempts": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("AUTO_ADOPTION_LEDGER_UNREADABLE", str(exc))
        if (not isinstance(raw, dict)
                or raw.get("schema_version") != LEDGER_SCHEMA_VERSION
                or not isinstance(raw.get("grants"), list)
                or not isinstance(raw.get("candidate_rounds"), list)
                or not isinstance(raw.get("attempts"), list)
                or any(not isinstance(g, dict) or set(g) != _GRANT_FIELDS for g in raw["grants"])
                or any(not isinstance(a, dict) or set(a) != _ATTEMPT_FIELDS for a in raw["attempts"])
                or any(not isinstance(r, dict) or set(r) != _ROUND_FIELDS for r in raw["candidate_rounds"])):
            _fail("AUTO_ADOPTION_LEDGER_UNREADABLE",
                  "ledger schema mismatch; refusing to decide on a foreign file")
        return raw

    def _save(self, ledger: dict) -> None:
        _atomic_json(self.path, ledger)

    def snapshot(self) -> dict:
        """账本只读视图（深拷贝），供审计展示。"""
        with self._lock:
            return json.loads(json.dumps(self._load()))

    # ------------------------------------------------------------------ grants

    def register_grant(self, *, repo_fingerprint: str, allowed_paths,
                       budget_seconds_max: float, authorized_by: str,
                       valid_until: float | None = None,
                       ttl_seconds: float | None = None, now=None) -> dict:
        """人工登记一条预授权记录（ops 工具/测试入口）。

        生产路径是人工直接编写账本文件中的 ``grants``；本方法只是同一
        schema 的受控写入门。时限取交集：有效期限 =
        ``min(显式 valid_until, now+ttl, now+上限)``，宽松值被上限截断。
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            fingerprint = _bounded_text(repo_fingerprint, "repo_fingerprint")
            if not _FINGERPRINT_RE.fullmatch(fingerprint):
                _fail("AUTO_ADOPTION_REQUEST_INVALID", "repo_fingerprint has unexpected characters")
            budget = budget_seconds_max
            if (isinstance(budget, bool) or not isinstance(budget, (int, float))
                    or not 1 <= float(budget) <= 86400):
                _fail("AUTO_ADOPTION_REQUEST_INVALID",
                      "budget_seconds_max must be a number between 1 and 86400")
            if not isinstance(allowed_paths, list) or not allowed_paths:
                _fail("AUTO_ADOPTION_REQUEST_INVALID",
                      "allowed_paths must be a non-empty list of repo-relative scopes")
            scopes = []
            for entry in allowed_paths:
                # 以 "/" 结尾的 scope 表示子树前缀；保留标记以区别单文件。
                subtree = isinstance(entry, str) and entry.endswith("/")
                scope = _safe_relative_path(entry)
                scopes.append(scope + "/" if subtree else scope)
            ceiling = moment + self.authorization_ttl_cap_seconds
            expiry = ceiling
            if valid_until is not None:
                if isinstance(valid_until, bool) or not isinstance(valid_until, (int, float)):
                    _fail("AUTO_ADOPTION_REQUEST_INVALID", "valid_until must be an epoch number")
                expiry = min(expiry, float(valid_until))
            if ttl_seconds is not None:
                if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)):
                    _fail("AUTO_ADOPTION_REQUEST_INVALID", "ttl_seconds must be a number")
                expiry = min(expiry, moment + float(ttl_seconds))
            grant = {"grant_id": "grant-" + secrets.token_hex(6),
                     "repo_fingerprint": fingerprint,
                     "allowed_paths": scopes, "valid_until": expiry,
                     "budget_seconds_max": float(budget),
                     "authorized_by": _bounded_text(authorized_by, "authorized_by", 120),
                     "created_at": moment}
            ledger = self._load()
            ledger["grants"].append(grant)
            self._save(ledger)
            return dict(grant)

    def _grant_for(self, ledger: dict, repo_fingerprint: str, moment: float):
        """返回 (grant, 有效期限)；无任何匹配 -> (None, None)；仅过期 -> (None, 期限)。"""
        matches = [g for g in ledger["grants"]
                   if g.get("repo_fingerprint") == repo_fingerprint]
        if not matches:
            return None, None

        def effective(g: dict) -> float:
            # 旧账本可能被手工放宽 valid_until；登记时刻+上限仍然是天花板。
            return min(float(g["valid_until"]),
                       float(g["created_at"]) + self.authorization_ttl_cap_seconds)

        live = [g for g in matches if effective(g) > moment]
        if not live:
            return None, min(effective(g) for g in matches)
        # 多条并存授权取更短剩余时限（更严格者生效）。
        best = min(live, key=effective)
        return best, effective(best)

    # ------------------------------------------------------- candidate rounds

    def note_candidate_generation(self, *, task_id: str, round_id: str,
                                  now=None) -> dict:
        """记录一轮候选生成；每任务去重计数，超过上限拒绝。

        计数键是 ``task_id``（非 round_id）：同一 round 重复登记幂等，
        新增 round 继续占用同一份任务额度，轮次增加不重置。
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            task = _bounded_text(task_id, "task_id", 100)
            round_id = _bounded_text(round_id, "round_id", 100)
            ledger = self._load()
            rounds = ledger["candidate_rounds"]
            known = {r["task_id"]: [] for r in rounds}
            for record in rounds:
                known.setdefault(record["task_id"], []).append(record["round_id"])
            used = set(known.get(task, []))
            if round_id not in used:
                if len(used) >= self.max_candidate_rounds_per_task:
                    _fail("AUTO_ADOPTION_CANDIDATE_ROUNDS_EXCEEDED",
                          f"task already used {len(used)}/"
                          f"{self.max_candidate_rounds_per_task} candidate generation rounds")
                rounds.append({"task_id": task, "round_id": round_id, "noted_at": moment})
                self._save(ledger)
                used.add(round_id)
            return {"task_id": task, "rounds_used": len(used),
                    "limit": self.max_candidate_rounds_per_task}

    # ---------------------------------------------------------------- decisions

    def _check_basis(self, basis) -> dict:
        if not isinstance(basis, dict):
            _fail("AUTO_ADOPTION_BASIS_MISSING",
                  "business-source fixes require a frozen behavioral basis or failure record")
        kind = basis.get("kind")
        record_id = basis.get("record_id")
        sha = basis.get("sha256")
        if kind not in _BASIS_KINDS:
            _fail("AUTO_ADOPTION_BASIS_MISSING",
                  "basis.kind must be behavioral_basis or failure_record")
        if not isinstance(record_id, str) or not record_id.strip() or len(record_id) > 200:
            _fail("AUTO_ADOPTION_BASIS_MISSING", "basis.record_id is required")
        if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
            _fail("AUTO_ADOPTION_BASIS_MISSING",
                  "basis.sha256 must be the frozen record's sha256 hex digest")
        return {"kind": kind, "record_id": record_id.strip(), "sha256": sha}

    def _check_rule_protection(self, rule_protection) -> None:
        if not isinstance(rule_protection, dict):
            _fail("AUTO_ADOPTION_RULE_PROTECTION_MISSING",
                  "auto adoption requires a computed rule-protection comparison")
        verdict = rule_protection.get("verdict")
        if verdict != "clean":
            _fail("AUTO_ADOPTION_RULE_PROTECTION_BLOCKED",
                  f"candidate weakens test rules (verdict={verdict!r})")
        deltas = {"assertions_delta": ("<", 0), "skip_delta": (">", 0),
                  "collection_delta": ("<", 0), "scope_delta": ("<", 0)}
        for key, (op, edge) in deltas.items():
            value = rule_protection.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _fail("AUTO_ADOPTION_RULE_PROTECTION_MISSING",
                      f"{key} must be numeric")
            if (op == "<" and value < edge) or (op == ">" and value > edge):
                _fail("AUTO_ADOPTION_RULE_PROTECTION_BLOCKED",
                      f"candidate weakens test rules ({key}={value})")
        if rule_protection.get("config_delta"):
            _fail("AUTO_ADOPTION_RULE_PROTECTION_BLOCKED",
                  "candidate changes run configuration (config_delta)")

    def assert_allowed(self, *, task_id: str, repo_fingerprint: str,
                       trigger_source: str, basis, paths, budget_seconds,
                       round_id: str = "", criterion_id: str = "",
                       rule_protection=None, now=None) -> dict:
        """判定本次自动采用是否允许；允许则**预占**额度并返回 attempt 记录。

        返回即表示账本已落一条 ``status=reserved`` 的尝试：无论后续
        ``_confirm`` 成功、失败还是进入恢复，这次尝试都已计数；只能用
        ``settle`` 收尾，不能撤销。
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            if not self.enabled:
                _fail("AUTO_ADOPTION_DISABLED",
                      "auto adoption policy is disabled by default; enable requires explicit human configuration")
            task = _bounded_text(task_id, "task_id", 100)
            fingerprint = _bounded_text(repo_fingerprint, "repo_fingerprint")
            if not _FINGERPRINT_RE.fullmatch(fingerprint):
                _fail("AUTO_ADOPTION_REQUEST_INVALID", "repo_fingerprint has unexpected characters")
            if (not isinstance(trigger_source, str) or not trigger_source.strip()
                    or len(trigger_source) > 200):
                _fail("AUTO_ADOPTION_TRIGGER_REQUIRED",
                      "auto adoption writes must carry trigger_source (loop suppression)")
            trigger = trigger_source.strip()
            checked_basis = self._check_basis(basis)
            self._check_rule_protection(rule_protection)
            clean_paths = [_safe_relative_path(p) for p in paths] if isinstance(paths, list) else None
            if not clean_paths:
                _fail("AUTO_ADOPTION_REQUEST_INVALID",
                      "paths must be a non-empty list of candidate file paths")
            budget = budget_seconds
            if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not budget > 0:
                _fail("AUTO_ADOPTION_REQUEST_INVALID",
                      "budget_seconds must be a positive number")
            ledger = self._load()
            attempts = ledger["attempts"]
            # 循环抑制：同一触发源只能驱动一次自动采用，全局持久。
            if any(a["trigger_source"] == trigger for a in attempts):
                _fail("AUTO_ADOPTION_TRIGGER_DUPLICATE",
                      "trigger_source already drove an auto adoption attempt")
            found = self._grant_for(ledger, fingerprint, moment)
            grant, effective = found
            if grant is None:
                if effective is None:
                    _fail("AUTO_ADOPTION_NOT_AUTHORIZED",
                          "repository is not covered by any registered grant")
                _fail("AUTO_ADOPTION_AUTHORIZATION_EXPIRED",
                      f"registered authorization expired at {effective:.0f} "
                      f"(ttl capped at {self.authorization_ttl_cap_seconds:.0f}s)")
            if not self._paths_covered(grant["allowed_paths"], clean_paths):
                _fail("AUTO_ADOPTION_SCOPE_NOT_AUTHORIZED",
                      "candidate paths fall outside the registered file scope")
            if float(budget) > float(grant["budget_seconds_max"]):
                _fail("AUTO_ADOPTION_BUDGET_EXCEEDED",
                      "adoption budget exceeds the registered authorization ceiling")
            # 每任务额度按 task_id 持久计数；预占即计数，成败不返还。
            if sum(1 for a in attempts if a["task_id"] == task) >= self.max_adoptions_per_task:
                _fail("AUTO_ADOPTION_TASK_LIMIT_REACHED",
                      f"task already consumed its auto adoption quota "
                      f"(round changes do not reset it)")
            # 滚动窗口：半开区间 [0, window_seconds)，未来时间戳不计入。
            recent = [a for a in attempts if a["repo_fingerprint"] == fingerprint
                      and 0 <= moment - float(a["reserved_at"]) < self.window_seconds]
            if len(recent) >= self.window_attempt_limit:
                _fail("AUTO_ADOPTION_WINDOW_EXCEEDED",
                      f"repository hit its rolling auto adoption attempt cap "
                      f"({len(recent)}/{self.window_attempt_limit} in {self.window_seconds:.0f}s)")
            attempt = {"attempt_id": "adopt-" + secrets.token_hex(6),
                       "grant_id": grant["grant_id"], "task_id": task,
                       "round_id": _bounded_text(round_id, "round_id", 100) if round_id else "",
                       "criterion_id": _bounded_text(criterion_id, "criterion_id", 100) if criterion_id else "",
                       "repo_fingerprint": fingerprint, "paths": clean_paths,
                       "budget_seconds": float(budget), "trigger_source": trigger,
                       "basis": checked_basis, "reserved_at": moment,
                       "status": "reserved", "settled_at": None, "detail": ""}
            attempts.append(attempt)
            self._save(ledger)
            return dict(attempt)

    def settle(self, attempt_id: str, *, status: str, detail: str = "",
               now=None) -> dict:
        """结算一次尝试（幂等，不返还额度）。

        ``status``: ``applied``（确认成功）/ ``failed``（确认失败）/
        ``recovered``（部分写入已恢复）。重复结算同一 attempt 是无操作；
        无论哪种结局，该次尝试都已计入窗口与任务额度。
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            attempt_id = _bounded_text(attempt_id, "attempt_id", 100)
            if status not in _SETTLE_STATUSES:
                _fail("AUTO_ADOPTION_REQUEST_INVALID",
                      "status must be applied|failed|recovered")
            ledger = self._load()
            for attempt in ledger["attempts"]:
                if attempt["attempt_id"] == attempt_id:
                    if attempt["status"] in _SETTLE_STATUSES:
                        return dict(attempt)  # 幂等：不重复扣账（计数本就按条目）
                    attempt["status"] = status
                    attempt["settled_at"] = moment
                    attempt["detail"] = str(detail or "")[:500]
                    self._save(ledger)
                    return dict(attempt)
            _fail("AUTO_ADOPTION_ATTEMPT_NOT_FOUND", "unknown attempt_id")

    @staticmethod
    def _paths_covered(scopes, paths) -> bool:
        return all(any(p == scope or (scope.endswith("/") and p.startswith(scope))
                      for scope in scopes) for p in paths)

    # 审计辅助：账本摘要（只读）。有意不提供任何执行类方法——见模块
    # docstring 第 8 条硬边界：提交/推送/合并/依赖/权限变更接口不存在。
    def usage(self, *, repo_fingerprint: str = "", now=None) -> dict:
        moment = time.time() if now is None else float(now)
        ledger = self._load()
        attempts = [a for a in ledger["attempts"]
                    if not repo_fingerprint or a["repo_fingerprint"] == repo_fingerprint]
        recent = [a for a in attempts
                  if 0 <= moment - float(a["reserved_at"]) < self.window_seconds]
        return {"enabled": self.enabled,
                "attempts_total": len(attempts),
                "attempts_in_window": len(recent),
                "window_attempt_limit": self.window_attempt_limit,
                "window_seconds": self.window_seconds}
