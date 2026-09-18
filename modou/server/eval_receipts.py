"""只读评测回执浏览器：runs/**/receipt-*.json 的安全索引与读取。

回执是冻结评测的证据，不是可编辑的数据：这里只提供索引和明细两种
读取，id 必须命中白名单正则（文件名 stem），杜绝把 id 当路径拼接
造成的目录穿越。任何写操作都不存在——回执一经产出就是只读证据。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# id = 回执文件名去扩展名。只认小写字母/数字/连字符，从结构上排除
# "../"、".."、绝对路径与大小写混淆变体。
_RECEIPT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _receipt_files(root: Path) -> list[Path]:
    runs = root / "runs"
    if not runs.is_dir():
        return []
    return sorted(
        (p for p in runs.glob("**/receipt-*.json") if p.is_file()),
        key=lambda p: p.name)


def receipt_currency(raw: dict, root: Path) -> dict:
    """Read-time applicability; never rewrite a historical verdict or freeze."""
    freeze = raw.get("freeze") if isinstance(raw.get("freeze"), dict) else {}
    result = {"status": "unverified", "reason": "冻结来源未验证；仅展示历史记录", "changed_files": []}
    name = Path(str(freeze.get("path") or "")).name
    if not re.fullmatch(r"l3_unattended_freeze_v[0-9]+\.json", name):
        return result
    path = root / "configs" / "evals" / name
    try:
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != freeze.get("sha256"):
            return {**result, "reason": "历史冻结文件缺失或哈希不符"}
        config = json.loads(data)
        files = config.get("frozen_code") or []
        if not files:
            return result
        changed = []
        for entry in files:
            relative = Path(entry["path"])
            target = root / relative
            if relative.is_absolute() or not target.resolve().is_relative_to(root.resolve()):
                return result
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
                changed.append(str(relative))
        return {"status": "stale" if changed else "current",
                "reason": "源码已变化；历史回执不代表当前版本通过" if changed else "当前源码与该回执冻结一致",
                "changed_files": changed}
    except (OSError, ValueError, KeyError, TypeError):
        return result


def _summary(path: Path, root: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    freeze = raw.get("freeze") if isinstance(raw.get("freeze"), dict) else {}
    return {
        "id": path.stem,
        "source_binding": receipt_currency(raw, root),
        "schema_version": str(raw.get("schema_version") or ""),
        "verdict": str(raw.get("verdict") or ""),
        "started_at": str(raw.get("started_at") or ""),
        "finished_at": str(raw.get("finished_at") or ""),
        "freeze_path": str(freeze.get("path") or ""),
        "freeze_sha256": str(freeze.get("sha256") or ""),
        "criteria_total": len(raw.get("criteria") or []),
        "criteria_passed": sum(
            1 for c in raw.get("criteria") or []
            if isinstance(c, dict) and c.get("passed") is True),
        "source": (str(path.relative_to(PROJECT_ROOT))
                   if path.is_relative_to(PROJECT_ROOT) else str(path)),
    }


def list_receipts(root: Path | None = None) -> list[dict]:
    """全部回执的索引（新→旧按文件名倒序）。损坏文件跳过，不炸端点。"""
    entries = [s for s in (_summary(p, root or PROJECT_ROOT)
                           for p in _receipt_files(root or PROJECT_ROOT))
               if s is not None]
    entries.sort(key=lambda e: (e.get("finished_at") or "", e["id"]),
                 reverse=True)
    return entries


def get_receipt(receipt_id: str, root: Path | None = None) -> dict | None:
    """按 id 取一份回执明细；id 不合法或不存在返回 None。"""
    if not isinstance(receipt_id, str) or not _RECEIPT_ID.match(receipt_id):
        return None
    for path in _receipt_files(root or PROJECT_ROOT):
        if path.stem != receipt_id:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return {**raw, "source_binding": receipt_currency(raw, root or PROJECT_ROOT)} if isinstance(raw, dict) else None
    return None
