"""Five-fingerprint delivery approval ceremony (M2).

批准侧算一遍五指纹、交付侧重算一遍，交付侧绝不采信批准记录里的
字符串——这条原则原样搬自 tools/verify_release_evidence.py：被描述的
工件要重新读，摘要要从内容重算，替进一个"形状合法"的值也会失败
关闭。

五个指纹全部来自既有数据源，不新造：
1. snapshot_sha256 —— control._repo_snapshot() 的仓库快照摘要；
2. patch_sha256 —— 与 RepairDelivery.patch_sha256 同算法；
3. test_sha256 —— RepairVerification 的 test_scope 与结果的规范化
   JSON 摘要；
4. evidence_manifest_sha256 —— 与 RepairDelivery.evidence_manifest_sha256
   同算法（证据清单规范化 JSON 摘要）；
5. target_ref —— 批准时 git rev-parse 解析出的交付分支 ref。

四类拒绝条件天然落进交付侧重算：
- 陈旧批准：快照重算对不上（DELIVERY_APPROVAL_STALE）；
- 分支移动：目标 ref 重算对不上（DELIVERY_BRANCH_MOVED）；
- 证据指纹不一致：证据清单重算对不上（DELIVERY_EVIDENCE_MISMATCH）；
- 权限扩张：补丁重算后越界/触碰禁区路径（DELIVERY_SCOPE_EXPANDED）。

本模块只做纯逻辑：哈希、比对、记录拼装。Git 与磁盘归宿主
control.py，与 edit_sessions 的分层纪律一致。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .evidence import sha256_json

APPROVAL_SCHEMA = "delivery-approval-v1"
EXPORT_SCHEMA = "delivery-export-v1"

#: 交付物措辞红线。independent_security_release_signoff 仍 pending，
#: 交付物上出现"签名"措辞会被读成已安全签署——一律写内容哈希清单。
DISCLAIMER = "内容哈希清单，非安全签署"
FORBIDDEN_TERMS = ("签名", "signature", "signed", "signoff")

FINGERPRINT_FIELDS = ("snapshot_sha256", "patch_sha256", "test_sha256",
                      "evidence_manifest_sha256", "target_ref")

#: 每个指纹重算对不上时的契约错误码。四类拒绝各占一码；test_sha256
#: 是证据摘要之外的独立钉子（test_scope/结果被单独换掉也藏不住）。
MISMATCH_CODES = {
    "snapshot_sha256": "DELIVERY_APPROVAL_STALE",
    "patch_sha256": "DELIVERY_PATCH_MISMATCH",
    "test_sha256": "DELIVERY_TEST_MISMATCH",
    "evidence_manifest_sha256": "DELIVERY_EVIDENCE_MISMATCH",
    "target_ref": "DELIVERY_BRANCH_MOVED",
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX_OID = re.compile(r"^[0-9a-f]{40,64}$")


def is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def is_hex_oid(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX_OID.fullmatch(value))


def patch_fingerprint(patch_text: str) -> str:
    """与 RepairDelivery.patch_sha256 同算法，别处不得另起炉灶。"""
    return hashlib.sha256(str(patch_text).encode("utf-8")).hexdigest()


def test_fingerprint(test_scope, test_results) -> str:
    """RepairVerification 的 test_scope + 结果 → 规范化 JSON 摘要。"""
    return sha256_json({"test_scope": list(test_scope or []),
                        "test_results": test_results or {}})


def evidence_fingerprint(manifest: dict) -> str:
    """与 RepairDelivery.evidence_manifest_sha256 同算法。"""
    return sha256_json(manifest)


@dataclass(frozen=True)
class FiveFingerprints:
    snapshot_sha256: str
    patch_sha256: str
    test_sha256: str
    evidence_manifest_sha256: str
    target_ref: str

    def as_dict(self) -> dict:
        return {field: getattr(self, field) for field in FINGERPRINT_FIELDS}

    def problems(self) -> list[str]:
        out: list[str] = []
        for field in FINGERPRINT_FIELDS[:4]:
            if not is_sha256(getattr(self, field)):
                out.append(f"{field} must be a lowercase sha256")
        if not is_hex_oid(self.target_ref):
            out.append("target_ref must be a hexadecimal Git object id")
        return out


def fingerprint_mismatches(approved: dict, recomputed: dict) -> list[str]:
    """按固定顺序返回重算对不上的指纹字段；空列表 = 五个全对。"""
    if not isinstance(approved, dict) or not isinstance(recomputed, dict):
        return list(FINGERPRINT_FIELDS)
    return [field for field in FINGERPRINT_FIELDS
            if approved.get(field) != recomputed.get(field)]


def approval_problems(approval: dict) -> list[str]:
    """批准记录的形状闸。只验形状；值一律留给交付侧重算比对。"""
    if not isinstance(approval, dict):
        return ["approval must be an object"]
    problems: list[str] = []
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        problems.append("unsupported approval schema")
    for field in ("review_id", "session_id", "branch", "commit", "created_at"):
        if not str(approval.get(field) or "").strip():
            problems.append(f"missing field: {field}")
    fingerprints = approval.get("fingerprints")
    if not isinstance(fingerprints, dict):
        return [*problems, "fingerprints must be an object"]
    if set(fingerprints) != set(FINGERPRINT_FIELDS):
        return [*problems, "fingerprints must carry exactly the five fields"]
    try:
        five = FiveFingerprints(
            **{field: fingerprints[field] for field in FINGERPRINT_FIELDS})
    except TypeError as exc:
        return [*problems, f"fingerprints malformed: {exc}"]
    problems.extend(five.problems())
    return problems


def build_approval_record(*, review_id: str, session_id: str,
                          fingerprints: dict, branch: str, commit: str,
                          note: str = "", created_at: str = "") -> dict:
    try:
        five = FiveFingerprints(
            **{field: fingerprints[field] for field in FINGERPRINT_FIELDS})
    except KeyError as exc:
        raise ValueError(f"fingerprints are missing {exc}") from exc
    record = {
        "schema_version": APPROVAL_SCHEMA,
        "review_id": review_id,
        "session_id": session_id,
        "fingerprints": five.as_dict(),
        "branch": branch,
        "commit": commit,
        "note": note,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    problems = approval_problems(record)
    if problems:
        raise ValueError("; ".join(problems))
    return record


def build_export_manifest(*, review_id: str, session_id: str,
                          fingerprints: dict, branch: str, commit: str,
                          file_hashes: list[dict],
                          created_at: str = "") -> dict:
    """内容哈希清单：只描述字节，不承诺安全签署。"""
    entries = sorted(({"path": str(entry["path"]),
                       "sha256": str(entry["sha256"])}
                      for entry in file_hashes), key=lambda entry: entry["path"])
    return {
        "schema_version": EXPORT_SCHEMA,
        "review_id": review_id,
        "session_id": session_id,
        "branch": branch,
        "commit": commit,
        "disclaimer": DISCLAIMER,
        "files": entries,
        "fingerprints": {field: str(fingerprints[field])
                         for field in FINGERPRINT_FIELDS},
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }


def assert_export_wording(text: str) -> None:
    """交付物措辞红线：清单声明在场，任何"签名"措辞不在场。"""
    if not isinstance(text, str) or DISCLAIMER not in text:
        raise ValueError(f"export must state: {DISCLAIMER}")
    lowered = text.lower()
    for term in FORBIDDEN_TERMS:
        if term in text or (term.isascii() and term in lowered):
            raise ValueError(f"export wording must not mention {term!r}")
