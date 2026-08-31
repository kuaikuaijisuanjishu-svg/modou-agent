"""保行号的删除变换。

**不使用 AST unparse 重写整个文件。** unparse 会重排格式、注释全丢、行号全变，
而覆盖率数据、diff 新增行集合、证书里的行号全都建立在原始行号上——
一旦行号漂移，前面所有的证据都对不上了。

所以删除的做法是：把要删的行**置空**（保留换行），行号原地不动。
如果父代码块因此变空，就在第一行补一个同缩进的 `pass`，并在证书里记下来。
"""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass


@dataclass
class Mutation:
    text: str
    deleted: tuple[int, ...]
    #: First line a `pass` was inserted at, or None. Kept as a plain int
    #: because it is written into evidence records and rendered into
    #: certificates; `pass_inserted_lines` carries the full set.
    pass_inserted_at: int | None = None
    pass_inserted_lines: tuple[int, ...] = ()


class InvalidTransform(RuntimeError):
    """无法形成语法合法的删除。调用方记 no_valid_transform，不算失败。"""


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _compiles(text: str) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            ast.parse(text)
            compile(text, "<modou>", "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def delete(source: str, lines: tuple[int, ...]) -> Mutation:
    """删掉这些物理行。返回变换后的文本；不合法就抛 InvalidTransform。"""
    if not lines:
        raise InvalidTransform("空的删除集合")
    src = source.splitlines(keepends=True)
    target = set(lines)
    if any(n < 1 or n > len(src) for n in target):
        raise InvalidTransform("行号越界")

    def blanked(extra_pass_at: frozenset[int]) -> str:
        out = []
        for i, text in enumerate(src, 1):
            if i not in target:
                out.append(text)
            elif i in extra_pass_at:
                out.append(_indent_of(src[i - 1]) + "pass\n")
            else:
                out.append("\n" if text.endswith("\n") else "")
        return "".join(out)

    plain = blanked(frozenset())
    if _compiles(plain):
        return Mutation(text=plain, deleted=tuple(sorted(target)))

    # 父代码块可能被清空了，补一个同缩进的 pass 再试
    first = min(target)
    with_pass = blanked(frozenset({first}))
    if _compiles(with_pass):
        return Mutation(text=with_pass, deleted=tuple(sorted(target)),
                        pass_inserted_at=first, pass_inserted_lines=(first,))

    # 一次删除可能清空**多个**代码块——ddmin 正是这种情形：它同时移除分散在
    # 不同函数体里的语句。补一个 pass 只能救活第一个块，其余仍然是空的，
    # 于是整次删除被当成"无法形成合法变换"而放弃。按连续删除区间各补一个：
    # 多行语句仍是一个区间、行为与上面一致，跨块删除则每块都补得上。
    runs: list[int] = []
    for line in sorted(target):
        if not runs or line - 1 not in target:
            runs.append(line)
    if len(runs) > 1:
        per_run = blanked(frozenset(runs))
        if _compiles(per_run):
            return Mutation(text=per_run, deleted=tuple(sorted(target)),
                            pass_inserted_at=runs[0],
                            pass_inserted_lines=tuple(runs))

    raise InvalidTransform("删除后语法不合法，补 pass 也救不回来")


def apply_to(ws, rel_path: str, lines: tuple[int, ...]) -> tuple[Mutation, str]:
    """在工作树里做一次删除。返回 (变换, 原始文本) 供回滚。"""
    original = ws.read(rel_path)
    m = delete(original, lines)
    ws.write(rel_path, m.text)
    return m, original


def revert(ws, rel_path: str, original: str) -> None:
    ws.write(rel_path, original)
