"""T05：人工候选入口、隔离执行对比与验收规则保护。

对齐冻结契约 ``docs/interfaces/candidates.md``（分离表示 + 规则 1–7）与
总计划 T05。本模块只做四件事，**不输出任何"修复成功"结论**：

1. ``ManualCandidateStore`` —— 人工上传/编辑候选的目录持久化（JSON 原子
   写）。封存内容落 ``candidates/<id>/content``，元数据落 ``meta.json``；
   同 id 重复写入不同内容 → 明确报错（契约规则 7：封存后不可变）。
2. 执行结果消费（纯函数）—— 输入基线/候选两份 junit xml 文本（或已解析
   的测试列表），输出断言计数、skip 集合、收集到的测试身份集合、测试范
   围差异与运行配置差异。测试身份与 ``modou/server/tasks.py::_prepare``
   的解析口径一致（``classname::name``，任意直接子节点
   skipped/failure/error 即未通过）。
3. ``rule_protection`` —— 采用前后对比判定：删除断言、原通过测试消失、
   新增 skip、收集数量下降、测试范围缩小 → ``blocked``；运行配置变化 →
   至少 ``suspect``。``blocked`` 给出人可读 reason（契约规则 4）。
4. 签证资格、回归资格与规则调整要求：``visa_qualification`` /
   ``manual_adoption_permission`` / ``auto_adoption_permission``（规则 3，
   T04 复用）、``regression_qualification``（规则 6）、
   ``rule_change_requires_new_requirement_version``（规则 5；接线由集成人做）。

人工候选与模型候选进入**相同**的隔离执行与版本复验流程（规则 1）——本模
块不自己执行测试，只封存内容并消费隔离执行产物。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from modou.source_weakening import analyze_source_diff

__all__ = [
    "CANDIDATE_INTAKE_SCHEMA_VERSION", "CandidateIntakeError",
    "generate_candidate_id", "content_sha256",
    "ManualCandidateStore",
    "TestOutcome", "TestRunRecord", "parse_junit", "parse_test_list",
    "consume_run_results",
    "rule_protection",
    "visa_qualification", "manual_adoption_permission",
    "auto_adoption_permission",
    "regression_qualification",
    "rule_change_requires_new_requirement_version",
]

CANDIDATE_INTAKE_SCHEMA_VERSION = "candidate-intake-v1"

# 候选 id（冻结契约）：cand-<hex12>
_CANDIDATE_ID_RE = re.compile(r"^cand-[0-9a-f]{12}$")
# 候选路径：相对路径，非隐藏文件，禁止 .. 越界（沿用 test-only 白名单精神）
_CANDIDATE_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*/?$")

# 人工候选来源（封闭词表，契约 origin_detail.source）
SOURCE_UPLOAD = "upload"
SOURCE_EDITOR = "editor"
_MANUAL_SOURCES = (SOURCE_UPLOAD, SOURCE_EDITOR)

# 测试执行状态（封闭词表）
STATUS_PASS = "pass"
STATUS_FAILURE = "failure"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
# junit 中视为"未通过"的直接子节点（与 tasks.py::_prepare 的口径一致）
_NOT_PASS_TAGS = {"skipped", "failure", "error"}

# 规则保护判定（封闭词表，与契约 rule_protection.verdict 一致）
VERDICT_CLEAN = "clean"
VERDICT_SUSPECT = "suspect"
VERDICT_BLOCKED = "blocked"

# 签证资格（封闭词表，与契约 visa.status 一致）
VISA_ELIGIBLE = "eligible"
VISA_WITHHELD_SMALL_CASE = "withheld_small_case"
VISA_INELIGIBLE = "ineligible"

# modou/agent/test_visa.py 的签证状态 → 契约 visa.status
_VISA_STATUS_MAP = {
    "VERIFIED_EFFECTIVE": VISA_ELIGIBLE,
    "WITHHELD_SMALL_CASE": VISA_WITHHELD_SMALL_CASE,
    "PASSED_NOT_EFFECTIVE": VISA_INELIGIBLE,
    "WITHHELD_NO_HOLDOUT": VISA_INELIGIBLE,
}

#: 单条候选 operator_note 上限；封存内容上限防止把任意大文件当候选
_MAX_NOTE_CHARS = 2000
_MAX_CONTENT_BYTES = 1_048_576
_MAX_TEST_TARGETS = 64


class CandidateIntakeError(ValueError):
    """候选封存、解析或保护判定的请求形状非法。

    ``code`` 是封闭错误码（``CANDIDATE_*`` / ``INTAKE_*`` / ``RUN_*``），
    ``detail`` 为人可读说明，风格与 ``modou/server/tasks.py::IntakeError``
    一致，便于集成人按同一模式接线。
    """

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- 封存存储

def generate_candidate_id() -> str:
    """生成契约格式的候选 id：``cand-<hex12>``。"""
    return "cand-" + secrets.token_hex(6)


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """JSON 原子写：临时文件 + os.replace，读者永远不会看到半截文件。"""
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                              sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class ManualCandidateStore:
    """人工候选的目录持久化（封存后不可变）。

    目录布局（``content_ref`` 与冻结契约 sealed_content 一致）::

        <root>/candidates/<candidate_id>/meta.json   # 候选记录（不含正文的封存字段）
        <root>/candidates/<candidate_id>/content     # 封存正文

    * ``save``：校验形状 → 计算 sha256 → 内容/元数据原子落盘；同 id 重复
      保存**相同**内容为幂等，返回既有记录；不同内容 →
      ``CANDIDATE_SEALED_IMMUTABLE``。
    * ``load``：读回记录；``with_content=True`` 时附正文。
    * ``verify_integrity``：重算正文哈希对照封存值，改写 → 报错
      （"候选内容被改写"验收场景）。
    * ``list_candidates``：按 created_at 稳定排序返回 id 列表。
    """

    def __init__(self, root):
        self.root = Path(root)

    # -- 内部路径 ----------------------------------------------------------

    def _dir(self, candidate_id: str) -> Path:
        if not _CANDIDATE_ID_RE.match(candidate_id or ""):
            raise CandidateIntakeError(
                "CANDIDATE_ID_INVALID",
                "candidate_id must match cand-<hex12>")
        # id 已被字符白名单约束，这里再断言一次不含路径分隔符
        assert "/" not in candidate_id and "." not in candidate_id
        return self.root / "candidates" / candidate_id

    # -- 校验 --------------------------------------------------------------

    @staticmethod
    def _validate_raw(raw: dict) -> None:
        if not isinstance(raw, dict):
            raise CandidateIntakeError("CANDIDATE_INVALID", "record must be a dict")
        required = {"candidate_id", "origin_detail", "sealed_content", "target"}
        missing = required - set(raw)
        if missing:
            raise CandidateIntakeError("CANDIDATE_INVALID",
                                       "missing fields: " + ",".join(sorted(missing)))
        if raw.get("origin") not in (None, "manual"):
            raise CandidateIntakeError(
                "CANDIDATE_ORIGIN_INVALID",
                "ManualCandidateStore only seals origin=manual candidates")
        detail = raw["origin_detail"]
        if (not isinstance(detail, dict)
                or detail.get("source") not in _MANUAL_SOURCES
                or not isinstance(detail.get("operator_note"), str)
                or not detail["operator_note"].strip()
                or len(detail["operator_note"]) > _MAX_NOTE_CHARS):
            raise CandidateIntakeError(
                "CANDIDATE_ORIGIN_DETAIL_INVALID",
                "origin_detail needs source in {upload,editor} and a bounded"
                " nonempty operator_note")
        sealed = raw["sealed_content"]
        if not isinstance(sealed, dict) or not isinstance(sealed.get("path"), str) \
                or not isinstance(sealed.get("content"), str):
            raise CandidateIntakeError(
                "CANDIDATE_SEALED_CONTENT_INVALID",
                "sealed_content needs path and content strings")
        path = sealed["path"]
        if not path or path.startswith("/") or ".." in path.split("/") \
                or path.startswith(".") or not _CANDIDATE_PATH_RE.match(path):
            raise CandidateIntakeError(
                "CANDIDATE_PATH_INVALID",
                "sealed path must be a relative repo path without traversal")
        if len(sealed["content"].encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise CandidateIntakeError(
                "CANDIDATE_CONTENT_TOO_LARGE",
                f"sealed content exceeds {_MAX_CONTENT_BYTES} bytes")
        target = raw["target"]
        if not isinstance(target, dict) or not isinstance(target.get("criterion_id"), str) \
                or not target["criterion_id"].strip():
            raise CandidateIntakeError(
                "CANDIDATE_TARGET_INVALID",
                "target needs a nonempty criterion_id")
        targets = target.get("test_targets")
        if (not isinstance(targets, list) or not targets
                or not all(isinstance(t, str) and t.strip() for t in targets)
                or len(targets) > _MAX_TEST_TARGETS):
            raise CandidateIntakeError(
                "CANDIDATE_TARGET_INVALID",
                "target.test_targets must be a nonempty bounded list of strings")

    # -- 公开 API ----------------------------------------------------------

    def save(self, raw: dict) -> dict:
        """封存一条人工候选；返回持久化记录（契约"分离表示"的候选维度）。"""
        self._validate_raw(raw)
        candidate_id = raw["candidate_id"]
        directory = self._dir(candidate_id)
        content = raw["sealed_content"]["content"]
        sha = content_sha256(content)
        expected = raw["sealed_content"].get("content_sha256")
        if expected is not None and expected != sha:
            raise CandidateIntakeError(
                "CANDIDATE_CONTENT_HASH_MISMATCH",
                "declared content_sha256 does not match the sealed content")
        record = {
            "schema_version": CANDIDATE_INTAKE_SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "origin": "manual",
            "origin_detail": {
                "source": raw["origin_detail"]["source"],
                "operator_note": raw["origin_detail"]["operator_note"].strip(),
            },
            "sealed_content": {
                "path": raw["sealed_content"]["path"],
                "content_sha256": sha,
                "content_ref": f"candidates/{candidate_id}/content",
            },
            "target": {
                "criterion_id": raw["target"]["criterion_id"],
                "test_targets": list(dict.fromkeys(raw["target"]["test_targets"])),
            },
            "created_at": raw.get("created_at") or time.time(),
        }
        content_path = directory / "content"
        meta_path = directory / "meta.json"
        if meta_path.is_file() or content_path.is_file():
            existing = self._read_meta(candidate_id)
            if existing["sealed_content"]["content_sha256"] != sha:
                raise CandidateIntakeError(
                    "CANDIDATE_SEALED_IMMUTABLE",
                    f"candidate {candidate_id} was sealed with different"
                    " content; upload a new candidate instead")
            # 同 id 同内容：幂等返回既有封存记录，不改写 created_at
            self.verify_integrity(candidate_id)
            return existing
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(content_path, content)
        _atomic_write_json(meta_path, record)
        return self._read_meta(candidate_id)

    def _read_meta(self, candidate_id: str) -> dict:
        path = self._dir(candidate_id) / "meta.json"
        if not path.is_file():
            raise CandidateIntakeError("CANDIDATE_NOT_FOUND",
                                       f"no sealed candidate {candidate_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def load(self, candidate_id: str, *, with_content: bool = False) -> dict:
        record = self._read_meta(candidate_id)
        if with_content:
            record = dict(record)
            record["sealed_content"] = {**record["sealed_content"],
                                        "content": self._read_content(candidate_id)}
        return record

    def _read_content(self, candidate_id: str) -> str:
        path = self._dir(candidate_id) / "content"
        if not path.is_file():
            raise CandidateIntakeError("CANDIDATE_NOT_FOUND",
                                       f"sealed content missing for {candidate_id}")
        return path.read_text(encoding="utf-8")

    def verify_integrity(self, candidate_id: str) -> dict:
        """重算封存正文哈希；落盘内容被改写 → ``CANDIDATE_CONTENT_REWRITTEN``。"""
        record = self._read_meta(candidate_id)
        actual = content_sha256(self._read_content(candidate_id))
        if actual != record["sealed_content"]["content_sha256"]:
            raise CandidateIntakeError(
                "CANDIDATE_CONTENT_REWRITTEN",
                f"sealed content of {candidate_id} no longer matches its"
                f" recorded sha256 ({record['sealed_content']['content_sha256']})"
                f" -> {actual}")
        return {"candidate_id": candidate_id, "content_sha256": actual,
                "integrity": "ok"}

    def list_candidates(self) -> list:
        root = self.root / "candidates"
        if not root.is_dir():
            return []
        records = []
        for meta in sorted(root.glob("cand-*")):
            if (meta / "meta.json").is_file():
                records.append(self._read_meta(meta.name))
        return [r["candidate_id"] for r in sorted(records,
                                                  key=lambda r: (r["created_at"],
                                                                 r["candidate_id"]))]


# ------------------------------------------------- 隔离执行结果消费（纯函数）

@dataclass
class TestOutcome:
    """单个测试的身份与结局。

    ``test_id`` 与 ``tasks.py::_prepare`` 相同：``classname::name``；无
    classname 的具名测试退化为裸 ``name``，不伪造模块前缀。
    """
    test_id: str
    status: str  # STATUS_PASS | STATUS_FAILURE | STATUS_ERROR | STATUS_SKIPPED
    classname: str = ""
    name: str = ""


@dataclass
class TestRunRecord:
    """一份隔离执行结果的解析视图（junit xml 或传入的测试列表）。"""
    tests: list = field(default_factory=list)      # list[TestOutcome]
    collection_errors: list = field(default_factory=list)  # list[test_id]
    source: str = ""                               # "junit" | "test_list"

    @property
    def statuses(self) -> dict:
        return {t.test_id: t.status for t in self.tests}

    @property
    def collected_ids(self) -> set:
        return {t.test_id for t in self.tests}

    def passing(self) -> set:
        return {t.test_id for t in self.tests if t.status == STATUS_PASS}

    def skipped(self) -> set:
        return {t.test_id for t in self.tests if t.status == STATUS_SKIPPED}

    def counts(self) -> dict:
        c = {STATUS_PASS: 0, STATUS_FAILURE: 0, STATUS_ERROR: 0, STATUS_SKIPPED: 0}
        for t in self.tests:
            c[t.status] += 1
        c["total"] = len(self.tests)
        return c


def _outcome_from_junit_case(case) -> TestOutcome:
    classname = case.attrib.get("classname", "")
    name = case.attrib.get("name", "")
    test_id = f"{classname}::{name}" if classname else name
    status = STATUS_PASS
    for child in case:
        if child.tag in _NOT_PASS_TAGS:
            status = child.tag
            break
    return TestOutcome(test_id=test_id, status=status,
                       classname=classname, name=name)


def _is_collection_error(outcome: TestOutcome, case) -> bool:
    """识别收集失败。pytest 9 实测格式：``classname=""``、``name=<模块路径>``、
    ``<error message="collection failure">``；旧版本用 ``collection_failure``
    用例名。两种都认，不猜其他。"""
    if outcome.name == "collection_failure":
        return True
    for child in case:
        if child.tag == STATUS_ERROR and "collection" in (
                child.attrib.get("message") or child.text or "").lower():
            return True
    return False


def parse_junit(xml_text: str) -> TestRunRecord:
    """解析 junit xml 文本（与 ``tasks.py::_prepare`` 相同口径）。

    收集失败识别见 ``_is_collection_error``（pytest 9 实测：``classname=""
    name="tests.test_broken"`` + ``<error message="collection failure">``）；
    此外整轮 0 收集（没有任何 testcase）也记为 collection error，不允许
    "没跑到任何测试"冒充通过。
    """
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise CandidateIntakeError("RUN_RESULT_INVALID",
                                   "junit xml must be a nonempty string")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise CandidateIntakeError("RUN_RESULT_INVALID",
                                   f"junit xml is not parseable: {exc}") from exc
    tests = []
    collection_errors = []
    for case in root.iter("testcase"):
        outcome = _outcome_from_junit_case(case)
        tests.append(outcome)
        if _is_collection_error(outcome, case):
            collection_errors.append(outcome.test_id)
    if not tests:
        collection_errors.append("<none collected>")
    return TestRunRecord(tests=tests, collection_errors=collection_errors,
                         source="junit")


def parse_test_list(tests) -> TestRunRecord:
    """消费已解析的测试列表：``[{test_id, status}, ...]``（或 ``"id"`` 视为 pass）。

    隔离执行由调用方完成（T03 作业）；本入口保证"同一套对比逻辑既吃
    junit xml 也吃已解析产物"，接线时二选一即可。
    """
    if not isinstance(tests, (list, tuple)):
        raise CandidateIntakeError("RUN_RESULT_INVALID",
                                   "test list must be a sequence of outcomes")
    outcomes = []
    collection_errors = []
    for item in tests:
        if isinstance(item, str):
            item = {"test_id": item, "status": STATUS_PASS}
        if not isinstance(item, dict) or not isinstance(item.get("test_id"), str) \
                or not item["test_id"].strip():
            raise CandidateIntakeError("RUN_RESULT_INVALID",
                                       "each outcome needs a test_id string")
        status = item.get("status", STATUS_PASS)
        if status not in {STATUS_PASS, STATUS_FAILURE, STATUS_ERROR, STATUS_SKIPPED}:
            raise CandidateIntakeError("RUN_RESULT_INVALID",
                                       f"unknown status {status!r} for"
                                       f" {item['test_id']}")
        test_id = item["test_id"]
        classname, _, name = test_id.rpartition("::")
        outcomes.append(TestOutcome(test_id=test_id, status=status,
                                    classname=classname, name=name))
        if item.get("collection_error") or status == STATUS_ERROR and "collection" in name.lower():
            collection_errors.append(test_id)
    if not outcomes:
        collection_errors.append("<none collected>")
    return TestRunRecord(tests=outcomes, collection_errors=collection_errors,
                         source="test_list")


def _as_run_record(run) -> TestRunRecord:
    if isinstance(run, TestRunRecord):
        return run
    if isinstance(run, str):
        return parse_junit(run)
    return parse_test_list(run)


def consume_run_results(baseline, candidate) -> dict:
    """对比基线执行与候选执行（纯函数，不执行任何测试）。

    输出五组事实（契约 rule_protection 的原料）：断言计数（按
    failure/error/skipped/pass 统计）、skip 集合变化、收集到的测试身份集
    合差异、（配合 scope_config 的）测试范围差异占位、运行配置差异占位。
    只陈述事实，不下通过/成功结论。
    """
    base = _as_run_record(baseline)
    cand = _as_run_record(candidate)
    base_counts, cand_counts = base.counts(), cand.counts()
    base_pass, cand_pass = base.passing(), cand.passing()
    base_skip, cand_skip = base.skipped(), cand.skipped()
    base_ids, cand_ids = base.collected_ids, cand.collected_ids
    return {
        "assertions": {"baseline": base_counts, "candidate": cand_counts,
                       "delta": {k: cand_counts[k] - base_counts[k]
                                 for k in base_counts}},
        "passing_sets": {"baseline": sorted(base_pass), "candidate": sorted(cand_pass)},
        "disappeared_passing": sorted(base_pass - cand_pass),
        "skip": {"baseline": sorted(base_skip), "candidate": sorted(cand_skip),
                 "new_skips": sorted(cand_skip - base_skip),
                 "removed_skips": sorted(base_skip - cand_skip)},
        "collected": {"baseline": sorted(base_ids), "candidate": sorted(cand_ids),
                      "appeared": sorted(cand_ids - base_ids),
                      "disappeared": sorted(base_ids - cand_ids),
                      "delta": len(cand_ids) - len(base_ids)},
        "collection_errors": {"baseline": base.collection_errors,
                              "candidate": cand.collection_errors},
        "note": "facts only; no success verdict is produced here",
    }


# ----------------------------------------------------------- 规则保护判定

def _scope_entries(scope) -> set:
    """把一份声明范围（``{test_files: [...], markers: [...]}`` 或列表）折成
    可比对的条目集合：``file:<path>`` / ``marker:<name>`` / 裸字符串。"""
    if scope is None:
        return set()
    if isinstance(scope, (list, tuple, set)):
        return {str(x) for x in scope}
    if isinstance(scope, dict):
        entries = {f"file:{p}" for p in scope.get("test_files") or []}
        entries |= {f"marker:{m}" for m in scope.get("markers") or []}
        return entries
    raise CandidateIntakeError("SCOPE_CONFIG_INVALID",
                               "scope entries must be a list or a"
                               " {test_files, markers} dict")


def rule_protection(baseline, candidate, scope_config=None, *, source_diff=None):
    """采用前后的规则保护判定（契约规则 4；只做保护，不判定修复成功）。

    * 原通过的测试消失，或通过数增加但断言（用例）总数下降 → blocked
    * 新增 skip、原通过测试变 skip → blocked
    * 收集数量下降、候选侧收集错误 → blocked
    * 声明测试范围缺少文件或标记（范围缩小）→ blocked
    * 运行配置差异（-k、--deselect、conftest 标志位等，以 config 差异列
      表传入）→ 至少 suspect
    * ``source_diff``（N01，可选）：``{"files": {path: {"baseline": 文本,
      "candidate": 文本}}}。对测试模块与配置类文件做结构性源码弱化分析
      （删断言/恒真/skip/范围/配置，见 ``modou.source_weakening``）：
      blocked 发现 → blocked；suspect 发现 → 至少 suspect，交人工复核
    * verdict 优先级 blocked > suspect > clean
    """
    facts = consume_run_results(baseline, candidate)
    assertions = facts["assertions"]
    scope_config = scope_config or {}
    if not isinstance(scope_config, dict):
        raise CandidateIntakeError("SCOPE_CONFIG_INVALID",
                                   "scope_config must be a dict")
    base_scope = _scope_entries(scope_config.get("baseline_scope"))
    cand_scope = _scope_entries(scope_config.get("candidate_scope"))
    scope_lost = sorted(base_scope - cand_scope)
    config_delta = list(scope_config.get("config_delta") or [])
    if not all(isinstance(d, dict) and isinstance(d.get("key"), str)
               for d in config_delta):
        raise CandidateIntakeError("SCOPE_CONFIG_INVALID",
                                   "config_delta entries need a key string")

    source_report = None
    source_suspects = []
    if source_diff is not None:
        if not isinstance(source_diff, dict) or not isinstance(source_diff.get("files"), dict):
            raise CandidateIntakeError("SCOPE_CONFIG_INVALID",
                                       "source_diff must be a dict with a files map")
        source_report = analyze_source_diff(source_diff["files"])

    blocking = []       # (code, reason) —— 契约级禁止，禁止以修复成功记账
    if facts["disappeared_passing"]:
        blocking.append(("ASSERTION_REMOVED",
                         "原通过过的测试在候选执行中消失："
                         + ", ".join(facts["disappeared_passing"][:5])))
    assertions_delta = assertions["delta"]["total"]
    pass_delta = assertions["delta"][STATUS_PASS]
    if pass_delta > 0 and assertions_delta < 0:
        blocking.append(("ASSERTION_REMOVED",
                         f"通过数增加 {pass_delta} 但断言（用例）总数下降"
                         f" {assertions_delta}，疑似删除断言换取通过"))
    new_skips = facts["skip"]["new_skips"]
    if new_skips:
        blocking.append(("SKIP_ADDED", "新增 skip：" + ", ".join(new_skips[:5])))
    turned_skip = sorted(set(new_skips) & set(facts["passing_sets"]["baseline"]))
    if turned_skip:
        blocking.append(("SKIP_ADDED",
                         "原通过测试变为 skip：" + ", ".join(turned_skip[:5])))
    collection_delta = facts["collected"]["delta"]
    if collection_delta < 0:
        blocking.append(("COLLECTION_SHRUNK",
                         f"收集到的测试数量下降 {collection_delta}："
                         + ", ".join(facts["collected"]["disappeared"][:5])))
    if facts["collection_errors"]["candidate"]:
        blocking.append(("COLLECTION_ERROR",
                         "候选执行存在收集错误，不能算通过："
                         + ", ".join(facts["collection_errors"]["candidate"][:5])))
    if scope_lost:
        blocking.append(("SCOPE_SHRUNK",
                         "声明的测试范围缩小：" + ", ".join(scope_lost[:5])))
    if source_report is not None:
        for finding in source_report["findings"]:
            if finding["severity"] == VERDICT_BLOCKED:
                blocking.append((finding["code"],
                                 f"{finding['path']} {finding['test']}: "
                                 + finding["detail"]))
            else:
                source_suspects.append(finding)
    verdict_reasons = []
    if blocking:
        verdict = VERDICT_BLOCKED
        verdict_reasons = [f"{code}: {reason}" for code, reason in blocking]
    elif config_delta or source_suspects:
        verdict = VERDICT_SUSPECT
        verdict_reasons = ["运行配置差异：" + ", ".join(
            str(d.get("key")) for d in config_delta)] if config_delta else []
        verdict_reasons += [f"{f['code']}: {f['path']} {f['test']}: {f['detail']}"
                            for f in source_suspects[:5]]
    else:
        verdict = VERDICT_CLEAN
    if verdict == VERDICT_CLEAN:
        clean_reason = "未检测到断言删除、新增 skip、收集下降或范围缩小"
        if source_report is not None:
            clean_reason += "；源码维度未命中已知弱化模式"
    else:
        clean_reason = ""
    return {
        "assertions_delta": assertions_delta,
        "skip_delta": len(new_skips),
        "collection_delta": collection_delta,
        "scope_delta": -len(scope_lost),
        "config_delta": config_delta,
        "source_findings": (source_report or {}).get("findings", []),
        "source_blocked": len([f for f in (source_report or {}).get("findings", [])
                               if f["severity"] == VERDICT_BLOCKED]),
        "source_suspect": len(source_suspects),
        "verdict": verdict,
        "reason": "；".join(verdict_reasons) if verdict_reasons else clean_reason,
        "blocking_codes": [code for code, _ in blocking],
        "facts": facts,
        "note": "rule protection only; a clean verdict is not a repair-success"
                " claim (verification/visa/outcome are separate dimensions)",
    }


# --------------------------------------------------------------- 签证资格

def visa_qualification(visa_record) -> dict:
    """把签证记录折成契约的 visa 维度（规则 2：与执行结果分开表示）。

    接受 ``modou/agent/test_visa.py`` 的记录 dict（看 ``status`` 字段）、
    直接的状态字符串、或空值（无签证记录 → ineligible）。不伪造资格。
    """
    if isinstance(visa_record, str):
        raw_status = visa_record
    elif isinstance(visa_record, dict):
        raw_status = str(visa_record.get("status") or "")
    else:
        raw_status = ""
    status = _VISA_STATUS_MAP.get(raw_status)
    if status is None:
        status = VISA_INELIGIBLE
    reason = {
        VISA_ELIGIBLE: "签证记录为 VERIFIED_EFFECTIVE（干预验证签署）",
        VISA_WITHHELD_SMALL_CASE: "小案例：可干预点不足，签证扣留（资格不足）",
        VISA_INELIGIBLE: f"无有效签证资格（status={raw_status or 'missing'}）",
    }[status]
    out = {"status": status, "reason": reason}
    if isinstance(visa_record, dict):
        out["threshold"] = visa_record.get("reason") or visa_record.get("threshold") or ""
    return out


def manual_adoption_permission(qualification) -> dict:
    """人工采用许可（规则 3）。

    eligible → 允许；withheld_small_case → 允许人工确认采用但携带
    ``qualification_insufficient=true``；ineligible → 拒绝（风险接受是另一
    条既有路径，不在本许可内）。
    """
    status = qualification.get("status") if isinstance(qualification, dict) else qualification
    if status == VISA_WITHHELD_SMALL_CASE:
        return {"allowed": True, "qualification_insufficient": True,
                "mode": "manual_confirmation",
                "reason": "小案例签证扣留：人工可确认采用，但记录资格不足；"
                          "采用后仍须走相同隔离执行与版本复验"}
    if status == VISA_ELIGIBLE:
        return {"allowed": True, "qualification_insufficient": False,
                "mode": "manual_confirmation", "reason": "签证资格有效"}
    return {"allowed": False, "qualification_insufficient": True,
            "mode": "manual_confirmation",
            "reason": "候选缺少有效签证资格；如需保留结论请走显式风险接受，"
                      "不得以人工采用替代"}


def auto_adoption_permission(qualification) -> dict:
    """自动采用许可（规则 3 / T04 复用）：仅 eligible 放行，恒拒绝其余。"""
    status = qualification.get("status") if isinstance(qualification, dict) else qualification
    if status == VISA_ELIGIBLE:
        return {"allowed": True, "qualification_insufficient": False,
                "mode": "auto", "reason": "签证资格有效"}
    return {"allowed": False, "qualification_insufficient": True,
            "mode": "auto",
            "reason": f"自动采用要求有效签证资格，当前为 {status}；"
                      "小案例扣留只允许人工确认采用"}


# --------------------------------------------------------------- 回归资格

def regression_qualification(fault_run, fix_run, test_id: str) -> dict:
    """回归测试资格（规则 6）：同一测试须故障版本 failed、修复版本 passed。

    只消费传入的执行记录，不执行也不伪造运行结果；不满足（含任一侧缺
    失、任一侧通过/未收集）→ ``qualified=false``，扣留回归保护结论。
    """
    if not isinstance(test_id, str) or not test_id.strip():
        raise CandidateIntakeError("REGRESSION_TARGET_INVALID",
                                   "test_id must be a nonempty string")
    fault = _as_run_record(fault_run).statuses.get(test_id)
    fix = _as_run_record(fix_run).statuses.get(test_id)
    qualified = fault == STATUS_FAILURE and fix == STATUS_PASS
    return {
        "test_id": test_id,
        "qualified": qualified,
        "evidence": {"fault_status": fault, "fix_status": fix},
        "reason": ("故障版本 failed 且修复版本 passed，回归资格成立"
                   if qualified else
                   "回归保护结论扣留：需要同一测试在故障版本 failed、修复"
                   f"版本 passed；实际 fault={fault} fix={fix}"),
    }


# ----------------------------------------------------- 规则调整→要求版本

_WEAKENING_KEYS = {"-k", "--deselect", "--ignore", "--skip", "--skip-tests",
                   "addopts", "conftest", "minversion", "markers_removed"}
_WEAKENING_HINTS = ("removed", "deleted", "narrowed", "skip", "deselect",
                    "ignore", "disabled")


def _change_is_weakening(change: dict) -> bool:
    if "weakening" in change:
        return bool(change["weakening"])
    key = str(change.get("key", ""))
    if key in _WEAKENING_KEYS:
        after, before = change.get("after"), change.get("before")
        # 已知过滤键上出现新增过滤值（原来没有）即视为弱化
        return (before in (None, "", []) and after not in (None, "", [])) \
            or (isinstance(before, list) and isinstance(after, list)
                and len(after) < len(before))
    text = json.dumps(change, ensure_ascii=False, default=str).lower()
    return any(h in text for h in _WEAKENING_HINTS)


def rule_change_requires_new_requirement_version(changes) -> dict:
    """合法测试规则调整必须创建独立要求版本并人工确认（规则 5）。

    输入调整列表（如 ``{"key": "-m", "before": "slow", "after": "slow,smoke"}``、
    conftest 标志位修改、测试规则文件增改）。弱化性调整不属于"合法调
    整"：它们应被 ``rule_protection`` 判 blocked，而不是靠开新要求版本洗
    白。本函数只给判定与说明对象，接线（task-rounds 的
    ``requirement_changed``）由集成人完成。
    """
    if not isinstance(changes, (list, tuple)):
        raise CandidateIntakeError("RULE_CHANGES_INVALID",
                                   "changes must be a list of rule-change dicts")
    legal, weakening = [], []
    for change in changes:
        if not isinstance(change, dict) or not str(change.get("key", "")).strip():
            raise CandidateIntakeError("RULE_CHANGES_INVALID",
                                       "each change needs a key string")
        (weakening if _change_is_weakening(change) else legal).append(change)
    required = bool(legal)
    return {
        "required": required,
        "legal_changes": legal,
        "weakening_changes": weakening,
        "action": {
            "create_requirement_version": required,
            "manual_confirmation_required": True,
            "requirement_changed": required,
        },
        "reason": ("合法（非弱化）测试规则调整需要创建独立要求版本并经人工确认；"
                   "原要求不能因此记为被修好" if required else
                   "没有需要开新要求版本的合法规则调整"),
        "note": "弱化性调整（删除断言/skip/缩范围/过滤）交给 rule_protection"
                " 判 blocked，不能以新要求版本替代；本判定不接线、不落盘。",
    }
