"""Deterministic natural-language to code location inside allowed repositories.

口径纪律：这里没有任何模型参与——一句话进入，检索器在仓库工作区内做确定性
匹配（符号名 / 文件名 / 内容命中），返回带证据的定位结果。它是给前端预填
草案用的：定位失败时返回 fallback 与原因，由人退回手动选仓库，绝不猜一个。
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = "locate-code-v1"
MAX_MATCHES = 12
MAX_DRAFT_PATHS = 5
MAX_FILES = 4000
MAX_FILE_BYTES = 262_144

_SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
                 ".next", ".idea", ".vscode"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TS_SYMBOL = re.compile(
    r"\b(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)")
_TS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

# 命中类别的封闭词表与分数。分数只在这一个地方定义，顺序就是优先级：
# 符号名 > 文件名 > 内容。同分时按 (repo_id, path, kind, token) 排序，
# 保证同一棵树两次检索给出逐字节相同的输出。
SYMBOL_EXACT = 100
SYMBOL_PARTIAL = 80
FILENAME_HIT = 60
DIRECTORY_HIT = 40
CONTENT_HIT = 20


def extract_tokens(text: str) -> list[str]:
    """把一句话拆成可检索的 token；顺序保持出现顺序，去重。

    英文标识符保留整词（verify_token），同时拆出 snake/camel 的组成部分
    （verify、token）——文件里叫 token 的地方也该被一句话点到。中文没有
    分词器，长于两字的连续段额外生成二元组：“认证逻辑” 会同时给出
    认证/逻辑，垃圾二元组（证逻）检索不到东西，自然不产生命中。
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if len(token) >= 2 and token not in seen:
            seen.add(token)
            tokens.append(token)

    for match in _WORD.finditer(text):
        word = match.group(0).strip("_")
        if not word:
            continue
        lowered = word.lower()
        add(lowered)
        parts = re.split(r"_+", _CAMEL.sub("_", lowered))
        for part in parts:
            add(part)
    for match in _CJK.finditer(text):
        run = match.group(0)
        if len(run) == 2:
            add(run)
        else:
            add(run)
            for index in range(len(run) - 1):
                add(run[index:index + 2])
    return tokens


def _iter_files(root: Path) -> Iterable[Path]:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIPPED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            count += 1
            if count > MAX_FILES:
                return
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _python_symbols(text: str) -> list[tuple[str, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.name, node.lineno))
    return out


def _script_symbols(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for match in _TS_SYMBOL.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        out.append((match.group(1), line))
    return out


def _symbols(path: Path, text: str) -> list[tuple[str, int]]:
    if path.suffix == ".py":
        return _python_symbols(text)
    if path.suffix in _TS_SUFFIXES:
        return _script_symbols(text)
    return []


def search_repo(root: Path, tokens: list[str], *, repo_id: str) -> list[dict]:
    """在一个仓库工作区内做确定性检索，返回按分数排序的命中。"""
    lowered_tokens = [token.lower() for token in tokens]
    matches: list[dict] = []

    def record(path: str, kind: str, detail: str, score: int) -> None:
        matches.append({"repo_id": repo_id, "path": path, "evidence_kind": kind,
                        "evidence_detail": detail, "score": score})

    for path in _iter_files(root):
        relative = path.relative_to(root).as_posix()
        parts = [part.lower() for part in path.parts]
        stem = path.stem.lower()
        for token in lowered_tokens:
            if token in stem:
                record(relative, "filename",
                       f"filename contains '{token}'", FILENAME_HIT)
                break
        for token in lowered_tokens:
            if any(token in part for part in parts[:-1]):
                record(relative, "filename",
                       f"directory contains '{token}'", DIRECTORY_HIT)
                break
        text = _read_text(path)
        if not text:
            continue
        for name, lineno in _symbols(path, text):
            lowered_name = name.lower()
            for token in lowered_tokens:
                if lowered_name == token:
                    record(relative, "symbol",
                           f"symbol '{name}' defined at line {lineno}",
                           SYMBOL_EXACT)
                    break
                if token in lowered_name:
                    record(relative, "symbol",
                           f"symbol '{name}' defined at line {lineno}",
                           SYMBOL_PARTIAL)
                    break
        lines = text.splitlines()
        for token in lowered_tokens:
            for index, line in enumerate(lines, 1):
                if token in line.lower():
                    record(relative, "content",
                           f"content contains '{token}' at line {index}",
                           CONTENT_HIT)
                    break
    matches.sort(key=lambda m: (-m["score"], m["repo_id"], m["path"],
                                m["evidence_kind"], m["evidence_detail"]))
    return matches


def dedupe_paths(matches: list[dict], repo_id: str, limit: int = MAX_DRAFT_PATHS) -> list[str]:
    paths: list[str] = []
    for match in matches:
        if match["repo_id"] == repo_id and match["path"] not in paths:
            paths.append(match["path"])
        if len(paths) >= limit:
            break
    return paths
