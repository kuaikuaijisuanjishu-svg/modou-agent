"""候选节点：能整块删掉、且只会删到本补丁新增行的 AST 语句。

为什么必须用 AST 而不是"连续行段"：连续行段无法稳定表达语义单元，
31.4% 的候选删完语法就不合法，从未真正进入测试——既没被证明可删，也没被证明必要。
根因是删除单位用了 diff 的产物，而 diff 的产物不是语义单位。

层次结构留给 hdd.py 用：先试粗节点，红了再下钻子节点。
"""
from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field


@dataclass
class Node:
    node_type: str
    start: int                       # 物理行（含装饰器）
    end: int
    depth: int
    children: list["Node"] = field(default_factory=list)
    #: 真实结构路径，如 `FunctionDef:add/If[0]/Assign[1]`。
    #: 账本的 UnitAnchor 靠它做稳定身份——只用 node_type 的话，
    #: 同一个文件里所有 Assign 都长一样，行号一变身份就跟着变。
    structural_path: str = ""

    @property
    def span(self) -> range:
        return range(self.start, self.end + 1)

    @property
    def lines(self) -> tuple[int, ...]:
        return tuple(self.span)

    @property
    def size(self) -> int:
        return self.end - self.start + 1


def _span(node: ast.stmt) -> tuple[int, int]:
    """语句的物理行跨度。装饰器行计入函数/类的跨度。"""
    start = node.lineno
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, getattr(d, "lineno", start))
    end = getattr(node, "end_lineno", None) or node.lineno
    return start, end


def _body_fields(node: ast.AST):
    for f in ("body", "orelse", "finalbody", "handlers"):
        for child in getattr(node, f, []) or []:
            yield child


def build(source: str) -> list[Node]:
    """解析成语句节点树。解析失败返回空表（调用方据此判 no_valid_transform）。"""
    try:
        with warnings.catch_warnings():
            # 被审仓库里的旧字符串转义会在新 Python 上产生海量 SyntaxWarning。
            # 这些警告不改变 AST，却会污染正式日志并消耗 wall-clock 预算。
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []

    def walk(stmts, depth: int, prefix: str) -> list[Node]:
        out = []
        for i, s in enumerate(stmts):
            if not isinstance(s, ast.stmt):
                continue
            a, b = _span(s)
            label = type(s).__name__
            name = getattr(s, "name", None)
            seg = f"{label}:{name}" if name else f"{label}[{i}]"
            sp = f"{prefix}/{seg}" if prefix else seg
            kids = walk(list(_body_fields(s)), depth + 1, sp)
            out.append(Node(label, a, b, depth, kids, structural_path=sp))
        return out

    return walk(tree.body, 0, "")


def _starts_on(source_lines: list[str], lineno: int) -> bool:
    """这一行是不是只承载一条语句（拒绝同行多语句）。"""
    if not (0 < lineno <= len(source_lines)):
        return True
    text = source_lines[lineno - 1]
    stripped = text.strip()
    # 粗判：分号分隔的同行多语句直接拒绝（字符串里含分号的少见，宁可少删）
    return ";" not in stripped or stripped.startswith("#")


def candidates(source: str, added: set[int]) -> list[Node]:
    """返回可以整块删除的顶层候选，children 保留供下钻。

    条件：节点完整物理行跨度都属于本补丁新增行；且跨度边界行不与别的语句共享。
    """
    lines = source.splitlines()
    roots = build(source)
    out: list[Node] = []

    def visit(n: Node):
        if set(n.span) <= added and _starts_on(lines, n.start) and \
                _starts_on(lines, n.end):
            out.append(n)
            return                       # 命中就不再往下找，子节点留给下钻
        for k in n.children:
            visit(k)

    for r in roots:
        visit(r)
    return out


def flatten_pending(nodes: list[Node]) -> list[Node]:
    """把一批节点的所有后代摊平，粗的在前。"""
    out: list[Node] = []

    def rec(ns: list[Node]):
        for n in ns:
            out.append(n)
            rec(n.children)

    rec(nodes)
    out.sort(key=lambda n: (-n.size, n.start))
    return out
