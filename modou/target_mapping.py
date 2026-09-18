"""T02：源码改写后的目标映射（联合定位）。

替换旧机制"文件 + 完全相同文本"的匹配（`modou/server/tasks.py::_compare` 的
调用点），改为三种定位依据联合：

  patch          补丁位置映射：hunk 行号迁移。输入可以是 git diff 补丁，也可以
                 是旧/新两份文件树（此时用 SequenceMatcher 合成等价的行段结构，
                 并用内容哈希配对整体重命名）。对任意文本文件有效。
  function_scope 函数范围：Python AST 找到目标行所在的函数/类，重写后同名函数
                 即候选。本轮仅 Python；Go/TS/Java 留接口与能力声明（见
                 `language_capabilities`），后续任务实现。
  context        上下文共位：目标行前后若干行的内容相似度。对任意文本文件有效。

纪律（总计划 T02 与 docs/interfaces/receipts.md）：

* 只在**唯一可靠**匹配时自动建立对应；`bases` 记录用了哪些依据，`confidence`
  记录置信度。
* 重复代码、目标删除、重命名不明、跨函数移动 → `needs_confirmation`（附候选
  列表与各自依据/位置）或 `deleted`，绝不猜。
* 映射记录可序列化为 JSON，供 `confirm_target_mapping` 消费（`apply_confirmation`）。
  人工确认只确认目标身份——确认后仍必须真实复验，本模块不生成任何"成功"结论。
* 语言执行回执无法定位源码时保留原始测试身份、不伪造行号
  （`preserve_test_identity`）。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

__all__ = [
    "MAPPING_SCHEMA_VERSION", "TargetMappingError",
    "CandidateMatch", "TargetMapping", "MappingReport",
    "map_targets", "apply_confirmation", "preserve_test_identity",
    "language_capabilities", "read_tree",
]

MAPPING_SCHEMA_VERSION = "target-mapping-v1"

# 定位依据（封闭词表，顺序即展示优先级）
BASIS_PATCH = "patch"
BASIS_FUNCTION_SCOPE = "function_scope"
BASIS_CONTEXT = "context"
BASIS_ORDER = (BASIS_PATCH, BASIS_FUNCTION_SCOPE, BASIS_CONTEXT)

# 状态（封闭词表）
STATUS_MAPPED = "mapped"
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
STATUS_DELETED = "deleted"
STATUS_CONFIRMED = "confirmed"

# 原因（封闭词表）
REASON_LINE_DELETED = "line_deleted"
REASON_FILE_DELETED = "file_deleted"
REASON_DUPLICATE_CODE = "duplicate_code"
REASON_CROSS_FUNCTION_MOVE = "cross_function_move"
REASON_RENAMED_UNCLEAR = "renamed_unclear"
REASON_NO_RELIABLE_MATCH = "no_reliable_match"

#: 自动建立对应需要的最低置信度
AUTO_THRESHOLD = 0.6
#: 文件已消失（重命名不明）时，跨文件自动建立对应需要更高置信度
STRONG_THRESHOLD = 0.75
#: 组间置信度差小于该值视为并列歧义
TIE_MARGIN = 0.05
#: 上下文共位窗口半径（行）
CONTEXT_WINDOW = 5
#: 每个目标最多列出的候选数
MAX_CANDIDATES = 4
#: 上下文搜索的单文件行数上限（超出则放弃 context 依据，不抽样猜测）
MAX_CONTEXT_LINES = 20_000
#: read_tree 的保护上限
MAX_TREE_FILES = 4000
MAX_TREE_BYTES = 1_048_576
_SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
                 ".next", ".idea", ".vscode"}

#: 函数范围依据的语言能力声明：本轮仅 Python，Go/TS/Java 留接口待后续任务。
LANGUAGE_CAPABILITIES = {
    "python": {"patch_position": True, "function_scope": True,
               "context_colocation": True},
    "go": {"patch_position": True, "function_scope": False,
           "context_colocation": True},
    "typescript": {"patch_position": True, "function_scope": False,
                   "context_colocation": True},
    "javascript": {"patch_position": True, "function_scope": False,
                   "context_colocation": True},
    "java": {"patch_position": True, "function_scope": False,
             "context_colocation": True},
}
_LANGUAGE_ALIASES = {
    "py": "python", "python": "python",
    "go": "go",
    "ts": "typescript", "tsx": "typescript", "typescript": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript",
    "cjs": "javascript", "javascript": "javascript",
    "java": "java",
}
#: 函数范围依据当前实际支持的后缀（与 LANGUAGE_CAPABILITIES 声明一致）
AST_SCOPE_SUFFIXES = {".py"}


class TargetMappingError(ValueError):
    """映射请求或确认请求形状非法。"""


def language_capabilities(language: str) -> dict:
    """按语言名（或文件后缀）返回封闭的能力声明副本。

    未登记的语言只声明通用的 patch/context 能力，不假装支持函数范围。
    """
    key = str(language or "").lower().lstrip(".")
    name = _LANGUAGE_ALIASES.get(key, "")
    if name:
        return dict(LANGUAGE_CAPABILITIES[name])
    return {"patch_position": True, "function_scope": False,
            "context_colocation": True, "declared": False}


# ---------------------------------------------------------------- 行段结构

@dataclass
class Segment:
    """旧行区间到新行区间的恒等迁移段（1-based，含端点）。"""
    old_start: int
    old_end: int
    new_start: int
    new_end: int


@dataclass
class HunkMeta:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list = field(default_factory=list)   # [(tag, old_ln, new_ln, text)]


@dataclass
class FileChange:
    """一个 (旧路径 → 新路径) 的行级变更结构。"""
    old_path: str
    new_path: str
    deleted: bool = False
    new_file: bool = False
    renamed: bool = False
    hunks: list = field(default_factory=list)               # [HunkMeta]
    equal_segments: list = field(default_factory=list)      # [Segment]
    removed_old_ranges: list = field(default_factory=list)  # [(a, b)] 1-based
    source: str = "git_diff"                                # 或 "tree_diff"

    def finalize(self) -> None:
        """把 hunk 序列整理成等值段 + 删除段，并补出 hunk 之间的隐含恒等段。"""
        delta = 0
        prev = 0
        self.equal_segments = []
        self.removed_old_ranges = []
        for h in self.hunks:
            lo, hi = prev + 1, h.old_start - 1
            if lo <= hi:
                self.equal_segments.append(
                    Segment(lo, hi, lo + delta, hi + delta))
            cur_eq = None
            cur_rm = None
            for tag, old_ln, new_ln, _text in h.lines:
                if tag == " ":
                    if cur_rm is not None:
                        self.removed_old_ranges.append(cur_rm)
                        cur_rm = None
                    if cur_eq is not None and cur_eq.old_end == old_ln - 1 \
                            and cur_eq.new_end == new_ln - 1:
                        cur_eq.old_end = old_ln
                        cur_eq.new_end = new_ln
                    else:
                        if cur_eq is not None:
                            self.equal_segments.append(cur_eq)
                        cur_eq = Segment(old_ln, old_ln, new_ln, new_ln)
                elif tag == "-":
                    if cur_eq is not None:
                        self.equal_segments.append(cur_eq)
                        cur_eq = None
                    if cur_rm is not None and cur_rm[1] == old_ln - 1:
                        cur_rm = (cur_rm[0], old_ln)
                    else:
                        if cur_rm is not None:
                            self.removed_old_ranges.append(cur_rm)
                        cur_rm = (old_ln, old_ln)
                # "+" 行没有旧行对应，不进任何段
            if cur_eq is not None:
                self.equal_segments.append(cur_eq)
            if cur_rm is not None:
                self.removed_old_ranges.append(cur_rm)
            delta += h.new_count - h.old_count
            prev = h.old_start + h.old_count - 1
        # hunk 之后的尾部恒等段（旧行上限未知，用一个大数表示开区间到文件尾）
        self.equal_segments.append(
            Segment(prev + 1, 10 ** 9, prev + 1 + delta, 10 ** 9))

    def map_line(self, old_ln: int) -> tuple[str, int | None]:
        for seg in self.equal_segments:
            if seg.old_start <= old_ln <= seg.old_end:
                return "mapped", seg.new_start + (old_ln - seg.old_start)
        for a, b in self.removed_old_ranges:
            if a <= old_ln <= b:
                return "removed", None
        return "unknown", None

    def apply(self, old_lines: list[str]) -> list[str]:
        """按 hunk 重建新文件行（补丁模式下 new_tree 缺失时使用）。"""
        out: list[str] = []
        prev = 0
        for h in self.hunks:
            out.extend(old_lines[prev:h.old_start - 1])
            for tag, _old_ln, _new_ln, text in h.lines:
                if tag in (" ", "+"):
                    out.append(text)
            prev = h.old_start + h.old_count - 1
        out.extend(old_lines[prev:])
        return out


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_git_diff(diff_text: str) -> list[FileChange]:
    """解析 unified git diff（含 rename/deleted/new file 记录）。

    局限：`diff --git` 行的路径解析假定常见 `a/x b/x` 形状；带空格路径以
    `---`/`+++`/`rename from` 行为准。
    """
    files: list[FileChange] = []
    cur: FileChange | None = None
    old_ln = new_ln = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            cur = FileChange(old_path="", new_path="")
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if line.startswith("rename from "):
            cur.old_path = line[len("rename from "):].strip()
            cur.renamed = True
            continue
        if line.startswith("rename to "):
            cur.new_path = line[len("rename to "):].strip()
            cur.renamed = True
            continue
        if line.startswith("deleted file mode"):
            cur.deleted = True
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            # 二进制文件没有行级映射依据；保持空 hunks，调用方按无法定位处理
            continue
        if not in_hunk:
            if line.startswith("--- "):
                cur.old_path = _strip_ab(line[4:].strip())
                continue
            if line.startswith("+++ "):
                cur.new_path = _strip_ab(line[4:].strip())
                continue
        m = _HUNK_RE.match(line)
        if m:
            h = HunkMeta(int(m.group(1)), int(m.group(2) or 1),
                         int(m.group(3)), int(m.group(4) or 1))
            cur.hunks.append(h)
            old_ln, new_ln = h.old_start, h.new_start
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue                      # "\ No newline at end of file"
        tag, text = (line[0], line[1:]) if line else (" ", "")
        if tag == " ":
            h.lines.append((" ", old_ln, new_ln, text))
            old_ln += 1
            new_ln += 1
        elif tag == "-":
            h.lines.append(("-", old_ln, None, text))
            old_ln += 1
        elif tag == "+":
            h.lines.append(("+", None, new_ln, text))
            new_ln += 1
    for fc in files:
        if fc.renamed and not fc.old_path:
            fc.old_path = fc.new_path
        fc.finalize()
    return [fc for fc in files if fc.old_path or fc.new_path or fc.hunks]


def _strip_ab(path: str) -> str:
    if path == "/dev/null":
        return ""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def _tree_change(old_path: str, new_path: str, old_text: str,
                 new_text: str) -> FileChange:
    """两树对比：用 SequenceMatcher 合成与 git hunk 等价的行段结构。"""
    a, b = old_text.splitlines(), new_text.splitlines()
    fc = FileChange(old_path, new_path, source="tree_diff")
    sm = SequenceMatcher(None, a, b, autojunk=False)
    equal: list[Segment] = []
    removed: list[tuple[int, int]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            equal.append(Segment(i1 + 1, i2, j1 + 1, j2))
        elif tag in ("replace", "delete"):
            removed.append((i1 + 1, i2))
    # 排序合并成 finalize 同构的表示：equal_segments 直接可用，removed 去重排序
    equal.sort(key=lambda s: s.old_start)
    removed.sort()
    fc.equal_segments = equal
    fc.removed_old_ranges = removed
    return fc


# ---------------------------------------------------------------- AST 范围

@dataclass
class ScopeDef:
    qualname: str    # 如 "SessionValidator.check"
    start: int       # 1-based，含装饰器行
    end: int


def python_scopes(text: str) -> list[ScopeDef]:
    """列出全部函数/类定义及其行跨度；解析失败返回空表（不猜）。"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out: list[ScopeDef] = []

    def visit(stmts, prefix: str) -> None:
        for node in stmts:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            start = node.lineno
            for dec in getattr(node, "decorator_list", []) or []:
                start = min(start, getattr(dec, "lineno", start))
            end = getattr(node, "end_lineno", None) or node.lineno
            name = f"{prefix}.{node.name}" if prefix else node.name
            out.append(ScopeDef(name, start, end))
            visit(node.body, name)

    visit(tree.body, "")
    return out


def _scope_provider(suffix: str):
    """函数范围依据的提供者注册点：本轮仅 Python，Go/TS/Java 留接口。"""
    if suffix in AST_SCOPE_SUFFIXES:
        return python_scopes
    return None


def _enclosing(scopes: list[ScopeDef], line: int) -> ScopeDef | None:
    best = None
    for s in scopes:
        if s.start <= line <= s.end and (best is None or s.start > best.start):
            best = s
    return best


# ---------------------------------------------------------------- 相似度

def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _context_scores(old_lines: list[str], idx: int, new_lines: list[str],
                    window: int, centers: Iterable[int] | None = None):
    """对每个候选中心行号计算窗口加权相似度。返回 [(center, score)]。

    目标行自身（o=0）权重加倍：否则"目标行被改写"与"整体上移一行"在
    等权窗口下得分相同，峰值会落到相邻的错误行上。
    """
    offs = [o for o in range(-window, window + 1)
            if 0 <= idx + o < len(old_lines)]
    if not offs:
        return []
    rng = centers if centers is not None else range(len(new_lines))
    out = []
    for c in rng:
        if not 0 <= c < len(new_lines):
            continue
        total = 0.0
        weight_sum = 0.0
        for o in offs:
            j = c + o
            ratio = (_ratio(old_lines[idx + o], new_lines[j])
                     if 0 <= j < len(new_lines) else 0.0)
            weight = 2.0 if o == 0 else 1.0
            total += weight * ratio
            weight_sum += weight
        out.append((c, total / weight_sum))
    return out


def _peaks(scores, floor: float) -> list[tuple[int, float]]:
    """取达到 floor 的峰值中心：相邻中心合并为一个峰，最多 MAX_CANDIDATES 个。"""
    ranked = sorted(((c, s) for c, s in scores if s >= floor),
                    key=lambda p: (-p[1], p[0]))
    out: list[tuple[int, float]] = []
    for c, s in ranked:
        if any(abs(c - pc) <= 1 for pc, _ in out):
            continue
        out.append((c, s))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


# ---------------------------------------------------------------- 记录形状

@dataclass
class CandidateMatch:
    file: str
    line: int
    basis: str            # "patch" | "function_scope" | "context" | 组合（"+"连接）
    confidence: float
    text: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "basis": self.basis,
                "confidence": round(self.confidence, 3), "text": self.text,
                "detail": self.detail}


@dataclass
class TargetMapping:
    mapping_id: str
    origin: dict                    # 原目标行（origin_rows 形状，原样保留）
    status: str
    mapped_file: str | None = None
    mapped_line: int | None = None
    mapped_text: str | None = None
    bases: list[str] = field(default_factory=list)
    confidence: float = 0.0
    candidates: list[CandidateMatch] = field(default_factory=list)
    reason: str = ""
    #: 任何映射（含人工确认后）都不免除真实复验；本模块永不生成成功结论
    requires_reverification: bool = True

    def as_dict(self) -> dict:
        return {
            "schema_version": MAPPING_SCHEMA_VERSION,
            "mapping_id": self.mapping_id,
            "origin": dict(self.origin),
            "status": self.status,
            "mapped": (None if self.mapped_file is None else {
                "file": self.mapped_file, "line": self.mapped_line,
                "text": self.mapped_text}),
            "bases": list(self.bases),
            "confidence": round(self.confidence, 3),
            "candidates": [c.as_dict() for c in self.candidates],
            "reason": self.reason,
            "requires_reverification": self.requires_reverification,
        }


@dataclass
class MappingReport:
    targets: list[TargetMapping] = field(default_factory=list)

    def as_dict(self) -> dict:
        counts = {STATUS_MAPPED: 0, STATUS_NEEDS_CONFIRMATION: 0,
                  STATUS_DELETED: 0}
        for t in self.targets:
            counts[t.status] = counts.get(t.status, 0) + 1
        return {"schema_version": MAPPING_SCHEMA_VERSION,
                "mappings": [t.as_dict() for t in self.targets],
                "summary": counts}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False,
                          sort_keys=True, indent=2)


# ---------------------------------------------------------------- 输入整理

def read_tree(root: Path, *, max_files: int = MAX_TREE_FILES,
              max_bytes: int = MAX_TREE_BYTES) -> dict[str, str]:
    """从目录读出 {相对 posix 路径: 文本}，供 map_targets 的 old_tree/new_tree。"""
    out: dict[str, str] = {}
    root = Path(root)
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            count += 1
            if count > max_files:
                return out
            try:
                if path.stat().st_size > max_bytes:
                    continue
                out[path.relative_to(root).as_posix()] = path.read_text(
                    encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return out


def _as_tree(value, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, (str, Path)):
        tree = read_tree(Path(value))
        if not tree:
            raise TargetMappingError(
                f"{name} path has no readable text files: {value}")
        return tree
    raise TargetMappingError(f"{name} must be a dict or a directory path")


def _build_pairings(old_tree: dict[str, str], new_tree: dict[str, str],
                    patch: str | None) -> dict[str, FileChange]:
    pairings: dict[str, FileChange] = {}
    if patch:
        for fc in parse_git_diff(patch):
            if fc.old_path:
                pairings[fc.old_path] = fc
        return pairings
    # 两树模式：共同路径合成行段；整体重命名用内容哈希唯一配对
    for path in sorted(set(old_tree) & set(new_tree)):
        if old_tree[path] == new_tree[path]:
            continue
        pairings[path] = _tree_change(path, path, old_tree[path], new_tree[path])
    old_only = sorted(set(old_tree) - set(new_tree))
    new_only = sorted(set(new_tree) - set(old_tree))
    by_hash: dict[str, list[str]] = {}
    for path in new_only:
        by_hash.setdefault(
            hashlib.sha256(new_tree[path].encode()).hexdigest(), []).append(path)
    for old_path in old_only:
        hits = by_hash.get(
            hashlib.sha256(old_tree[old_path].encode()).hexdigest(), [])
        if len(hits) == 1:
            fc = _tree_change(old_path, hits[0], old_tree[old_path],
                              new_tree[hits[0]])
            fc.renamed = True
            pairings[old_path] = fc
    return pairings


def _derive_new_tree(old_tree: dict[str, str], patch: str) -> dict[str, str]:
    out = dict(old_tree)
    for fc in parse_git_diff(patch):
        if not fc.old_path or fc.old_path not in out:
            continue
        if fc.deleted:
            out.pop(fc.old_path, None)
            continue
        out[fc.new_path or fc.old_path] = "\n".join(
            fc.apply(out[fc.old_path].splitlines()))
        if fc.renamed and fc.new_path != fc.old_path:
            out.pop(fc.old_path, None)
    return out


# ---------------------------------------------------------------- 主流程

def map_targets(targets: Iterable[dict], *, old_tree=None, new_tree=None,
                patch: str | None = None,
                window: int = CONTEXT_WINDOW) -> MappingReport:
    """把原目标集合映射到改写后的源码位置。

    targets：origin_rows 形状的行（需要 file/line，text 可选但有则更稳）。
    变更描述二选一：patch（git diff 文本）或 new_tree（新文件树）；两者都给
    时以 patch 的行号迁移为准、new_tree 用于内容校验与上下文搜索。
    """
    rows = list(targets)
    if not rows:
        raise TargetMappingError("at least one target row is required")
    for row in rows:
        if (not isinstance(row, dict)
                or not isinstance(row.get("file"), str) or not row.get("file")
                or not isinstance(row.get("line"), int) or row["line"] < 1
                or not isinstance(row.get("text", ""), str)):
            raise TargetMappingError(
                "each target must be an origin-row dict with file, line>=1, text")
    old = _as_tree(old_tree, "old_tree")
    new = _as_tree(new_tree, "new_tree")
    if patch is not None and not isinstance(patch, str):
        raise TargetMappingError("patch must be a unified diff string")
    if not patch and not new:
        raise TargetMappingError(
            "a change description is required: patch or new_tree")
    if patch and not new:
        new = _derive_new_tree(old, patch)
    pairings = _build_pairings(old, new, patch)

    report = MappingReport()
    for row in rows:
        report.targets.append(
            _map_one(row, old, new, pairings, window))
    return report


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _mapping_id(origin: dict) -> str:
    canonical = json.dumps(
        {"file": origin.get("file"), "line": origin.get("line"),
         "text": origin.get("text", "")}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))
    return "map-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _map_one(target: dict, old_tree: dict[str, str], new_tree: dict[str, str],
             pairings: dict[str, FileChange], window: int) -> TargetMapping:
    origin = dict(target)
    old_path = target["file"]
    old_ln = target["line"]
    old_lines = _lines(old_tree[old_path]) if old_path in old_tree else None
    anchor = (old_lines[old_ln - 1] if old_lines and old_ln <= len(old_lines)
              else target.get("text", ""))
    fc = pairings.get(old_path)
    new_path = None
    if fc is not None and fc.new_path:
        new_path = fc.new_path
    elif old_path in new_tree:
        new_path = old_path
    new_lines = _lines(new_tree[new_path]) if new_path else None

    candidates: list[CandidateMatch] = []
    old_provider = _scope_provider(Path(old_path).suffix)
    new_provider = (_scope_provider(Path(new_path).suffix)
                    if new_path else None)
    old_scopes = (old_provider(old_tree[old_path])
                  if old_provider and old_path in old_tree else [])
    new_scopes = (new_provider(new_tree[new_path])
                  if new_provider and new_path and new_path in new_tree else [])
    old_enc = _enclosing(old_scopes, old_ln)
    #: 两边都能解析出函数范围时才做"跨函数边界"判定；否则不假装知道边界
    scope_aware = bool(old_scopes) and bool(new_scopes)

    # ---- 依据 (a) patch：行号迁移 + 行文本校验 ----
    patch_state, patch_line = "none", None
    if fc is not None and fc.deleted:
        patch_state = "file_deleted"
    elif fc is not None and new_lines is not None:
        kind, mapped = fc.map_line(old_ln)
        if kind == "mapped" and mapped is not None and mapped <= len(new_lines):
            if new_lines[mapped - 1] == anchor:
                patch_state, patch_line = "mapped", mapped
            else:
                patch_state = "broken"
        elif kind == "removed":
            patch_state = "removed"
        else:
            patch_state = "unknown"
    elif fc is None and old_path in old_tree and new_lines is not None:
        # 补丁未涉及该文件：恒等迁移，仍需校验
        if old_ln <= len(new_lines) and new_lines[old_ln - 1] == anchor:
            patch_state, patch_line = "mapped", old_ln

    # ---- 依据 (b) function_scope：同名函数唯一/重复 ----
    if old_enc is not None and new_scopes:
        same = [s for s in new_scopes if s.qualname == old_enc.qualname]
        for s in same:
            span_centers = range(s.start - 1, s.end)
            if anchor:
                hits = [i for i in span_centers if new_lines[i] == anchor]
                if len(hits) == 1:
                    candidates.append(CandidateMatch(
                        new_path, hits[0] + 1, BASIS_FUNCTION_SCOPE, 0.95,
                        new_lines[hits[0]],
                        f"同名函数 {s.qualname} 内唯一文本命中"))
                    continue
            if len(new_lines) <= MAX_CONTEXT_LINES:
                for c, score in _peaks(_context_scores(
                        old_lines, old_ln - 1, new_lines, window,
                        centers=span_centers), AUTO_THRESHOLD):
                    candidates.append(CandidateMatch(
                        new_path, c + 1, BASIS_FUNCTION_SCOPE, score,
                        new_lines[c],
                        f"同名函数 {s.qualname} 范围内上下文最佳"))

    # ---- 依据 (c) context：上下文共位（任意文本文件） ----
    if old_lines is not None:
        search_files = [new_path] if new_path else sorted(new_tree)
        for file in search_files:
            flines = _lines(new_tree[file])
            if len(flines) > MAX_CONTEXT_LINES:
                continue
            for c, score in _peaks(_context_scores(
                    old_lines, old_ln - 1, flines, window), AUTO_THRESHOLD):
                candidates.append(CandidateMatch(
                    file, c + 1, BASIS_CONTEXT, score, flines[c],
                    f"上下文相似度 {score:.3f}"))

    # ---- 合并同一 (file, line) 的依据 ----
    groups: dict[tuple[str, int], CandidateMatch] = {}
    for cand in candidates:
        key = (cand.file, cand.line)
        if key not in groups:
            groups[key] = CandidateMatch(
                cand.file, cand.line, cand.basis, cand.confidence,
                cand.text, cand.detail)
        else:
            g = groups[key]
            bases = set(g.basis.split("+")) | set(cand.basis.split("+"))
            g.basis = "+".join(b for b in BASIS_ORDER if b in bases)
            g.confidence = max(g.confidence, cand.confidence)
            g.detail = f"{g.detail}; {cand.detail}"
    merged = sorted(groups.values(),
                    key=lambda c: (-c.confidence, c.file, c.line))

    def _same_function(file: str, line: int) -> bool:
        """候选位置与原目标是否仍在同一函数边界内（仅两边都能解析范围时判定）。"""
        if not scope_aware:
            return True
        cand_enc = (_enclosing(new_scopes, line) if file == new_path
                    else None)
        return ((old_enc is None and cand_enc is None)
                or (old_enc is not None and cand_enc is not None
                    and old_enc.qualname == cand_enc.qualname))

    if patch_state == "mapped":
        merged.insert(0, CandidateMatch(
            new_path, patch_line, BASIS_PATCH, 1.0,
            new_lines[patch_line - 1] if patch_line <= len(new_lines) else "",
            "hunk 行号迁移且行文本校验一致"))
    merged = merged[:MAX_CANDIDATES]

    record = TargetMapping(_mapping_id(origin), origin, "", candidates=merged)

    def _finish(status, reason="", mapped=None, bases=None, conf=0.0):
        record.status = status
        record.reason = reason
        if mapped is not None:
            record.mapped_file, record.mapped_line = mapped[0], mapped[1]
            record.mapped_text = (new_tree.get(mapped[0], "").splitlines()
                                  [mapped[1] - 1]
                                  if mapped[1] <= len(_lines(
                                      new_tree.get(mapped[0], ""))) else "")
        record.bases = list(bases or [])
        record.confidence = conf
        return record

    # ---- 决策：绝不猜 ----
    if patch_state == "file_deleted":
        if merged:
            return _finish(STATUS_NEEDS_CONFIRMATION,
                           REASON_CROSS_FUNCTION_MOVE)
        return _finish(STATUS_DELETED, REASON_FILE_DELETED)

    if patch_state == "mapped":
        # 补丁能逐行追踪，但若目标跨出了原函数边界（移动/被包裹进新函数），
        # 行号追踪不再等于身份对应——交给人工确认
        if _same_function(new_path, patch_line):
            return _finish(STATUS_MAPPED, "", (new_path, patch_line),
                           [BASIS_PATCH], 1.0)
        return _finish(STATUS_NEEDS_CONFIRMATION, REASON_CROSS_FUNCTION_MOVE)

    strong = [g for g in merged if g.confidence >= AUTO_THRESHOLD]
    if strong:
        best = strong[0].confidence
        top = [g for g in strong if best - g.confidence <= TIE_MARGIN]
    else:
        top = []
    if len(top) > 1:
        # 并列歧义：重复代码等多等价候选，交给人工确认
        return _finish(STATUS_NEEDS_CONFIRMATION, REASON_DUPLICATE_CODE)
    if len(top) == 1:
        g = top[0]
        if patch_state == "removed":
            if _same_function(g.file, g.line):
                return _finish(STATUS_MAPPED, "", (g.file, g.line),
                               g.basis.split("+"), g.confidence)
            reason = (REASON_RENAMED_UNCLEAR if fc is not None and fc.renamed
                      else REASON_CROSS_FUNCTION_MOVE)
            return _finish(STATUS_NEEDS_CONFIRMATION, reason)
        # 补丁缺位或校验失败但无删除记录：唯一强匹配可建立对应
        if new_path == old_path or (fc is not None and fc.renamed):
            return _finish(STATUS_MAPPED, "", (g.file, g.line),
                           g.basis.split("+"), g.confidence)
        # 旧文件在新树中消失且无重命名记录：跨文件匹配要求更高置信度
        if g.confidence >= STRONG_THRESHOLD:
            return _finish(STATUS_MAPPED, "", (g.file, g.line),
                           g.basis.split("+"), g.confidence)
        return _finish(STATUS_NEEDS_CONFIRMATION, REASON_RENAMED_UNCLEAR)

    # ---- 没有任何可靠候选 ----
    if patch_state == "removed":
        if fc is not None and fc.renamed:
            return _finish(STATUS_NEEDS_CONFIRMATION, REASON_RENAMED_UNCLEAR)
        return _finish(STATUS_DELETED, REASON_LINE_DELETED)
    if old_path not in old_tree:
        return _finish(STATUS_NEEDS_CONFIRMATION, REASON_NO_RELIABLE_MATCH)
    if old_path not in new_tree and not (fc is not None and fc.renamed):
        # 两树模式下文件消失且内容无处可寻：可能是删除也可能是重命名，不猜
        return _finish(STATUS_NEEDS_CONFIRMATION, REASON_RENAMED_UNCLEAR)
    return _finish(STATUS_NEEDS_CONFIRMATION, REASON_NO_RELIABLE_MATCH)


# ---------------------------------------------------------------- 确认与回执

def apply_confirmation(mapping: dict, *, confirmed: bool,
                       file: str | None = None, line: int | None = None,
                       operator_note: str = "") -> dict:
    """消费 confirm_target_mapping 动作：人工确认**只确认目标身份**。

    只允许确认候选列表（或既有 mapped 位置）里出现过的位置——确认动作不能
    引入映射阶段从未出现过的坐标。确认后 `requires_reverification` 保持
    True：确认不产生、也不能产生任何"成功"结论。
    """
    if not isinstance(mapping, dict) or "mapping_id" not in mapping:
        raise TargetMappingError("mapping must be a serialized mapping record")
    if mapping.get("schema_version") != MAPPING_SCHEMA_VERSION:
        raise TargetMappingError("unsupported mapping schema version")
    out = dict(mapping)
    note = str(operator_note or "")
    if not confirmed:
        out["confirmation"] = {"confirmed": False, "operator_note": note}
        return out
    if not isinstance(file, str) or not file or not isinstance(line, int):
        raise TargetMappingError(
            "confirmed=true requires an explicit file and line from candidates")
    allowed = {(c["file"], c["line"]) for c in mapping.get("candidates", [])}
    mapped = mapping.get("mapped")
    if mapped:
        allowed.add((mapped["file"], mapped["line"]))
    if (file, line) not in allowed:
        raise TargetMappingError(
            "confirmation choice is not among the recorded candidates")
    chosen = next((c for c in mapping.get("candidates", [])
                   if c["file"] == file and c["line"] == line), None)
    if chosen is None:
        chosen = {"basis": "+".join(mapping.get("bases", [])),
                  "confidence": mapping.get("confidence", 0.0),
                  "text": mapped.get("text", "")}
    out["status"] = STATUS_CONFIRMED
    out["mapped"] = {"file": file, "line": line, "text": chosen.get("text", "")}
    out["bases"] = chosen["basis"].split("+")
    out["confidence"] = chosen.get("confidence", 0.0)
    out["confirmation"] = {"confirmed": True, "operator_note": note,
                           "choice": {"file": file, "line": line}}
    out["requires_reverification"] = True
    out["note"] = ("人工确认仅确认目标身份；确认后仍必须真实复验，"
                   "本记录不构成任何成功结论")
    return out


_RESOLVED_STATUSES = {STATUS_MAPPED, STATUS_CONFIRMED}


def preserve_test_identity(test_id: str, mapping_status: str, *,
                           mapped_location: dict | None = None,
                           original_target: dict | None = None) -> dict:
    """语言执行回执的测试身份保留帮助函数（receipts 契约第 5 条）。

    无法定位源码时保留原始测试身份、不伪造行号：未解析状态携带
    mapped_location 直接拒绝（调用方无法凭空给出坐标），回执片段的
    source_location 保持 null，original_target 原样保留。
    """
    if not isinstance(test_id, str) or not test_id.strip():
        raise TargetMappingError("test_id must be a nonempty string")
    if not isinstance(mapping_status, str) or not mapping_status:
        raise TargetMappingError("mapping_status must be a nonempty string")
    resolved = mapping_status in _RESOLVED_STATUSES
    if resolved and not mapped_location:
        raise TargetMappingError("resolved mapping must carry its location")
    if not resolved and mapped_location is not None:
        raise TargetMappingError(
            "unresolved mapping must not carry a source location; "
            "fabricating a line number is forbidden")
    fragment = {
        "test_id": test_id,
        "mapping_status": mapping_status,
        "source_line_resolved": resolved,
        "source_location": ({"file": mapped_location["file"],
                             "line": mapped_location["line"]}
                            if resolved else None),
        "original_target": (dict(original_target) if original_target else None),
    }
    if not resolved:
        fragment["note"] = "无法定位源码行：保留原始测试身份，不伪造行号"
    return fragment
