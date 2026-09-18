"""规则保护的源码维度（N01）：对测试相关 Python 源差异做结构性弱化分析。

背景（06号核查 §2C / 09号探针）：``rule_protection`` 原先只消费具名测试
的执行状态。保留测试名、把内部 ``assert`` 改成 ``pass``，两次执行都成
功、用例数与通过状态不变，送入保护函数返回 ``clean``。本模块补上源码
差异维度，供采用管线统一入口与直接调用方一起使用。

判定边界（照任务单 N01 的实现边界写死）：

* 本分析是**结构性防护**，不能证明语义等价。``clean`` 不表示"没有弱化"，
  只表示"已知的结构性弱化模式都没有命中"。
* 能稳健判定的（删断言、恒真断言、保留测试新增 skip、删测试、参数化收
  缩）给 ``blocked``；方向依赖上下文、无法稳健判定的（数值阈值/期望值
  变化、配置类文件改动、解析失败、新测试带 skip）给 ``suspect``，明确
  交人工复核，不假装能自动裁决。
* 不把"断言计数增加"当修复有效条件，也不把"断言没有减少"当未弱化的充
  分条件——本模块只报告命中的事实模式，不做任何"修复成功"结论。
"""
from __future__ import annotations

import ast

__all__ = ["analyze_source_diff"]

# blocked：结构性可稳健判定，直接禁止
# suspect：需要人工复核，不能自动放行也不能自动拒绝
_SEV_BLOCKED = "blocked"
_SEV_SUSPECT = "suspect"

# 视为测试模块的文件名模式；其余 .py（业务代码）不在测试规则保护范围
_TEST_MODULE_SUFFIXES = ("test_*.py", "*_test.py")

# 测试规则/配置类文件：内容变化一律 suspect（conftest 能改收集与断言行为）
_CONFIG_BASENAMES = {"conftest.py", "pytest.ini", "setup.cfg", "tox.ini",
                     "pyproject.toml"}

# pytest/unittest 的跳过记号（装饰器与调用）
_SKIP_MARKERS = {"skip", "skipif", "xfail"}


def _is_test_module(path: str) -> bool:
    from fnmatch import fnmatch
    from pathlib import PurePosixPath
    name = PurePosixPath(path).name
    return any(fnmatch(name, pat) for pat in _TEST_MODULE_SUFFIXES)


def _is_config_file(path: str) -> bool:
    from pathlib import PurePosixPath
    return PurePosixPath(path).name in _CONFIG_BASENAMES


def _parse(source):
    """解析 Python 源码；空内容按 None 处理，坏语法抛给调用方归类。"""
    if source is None or not str(source).strip():
        return None
    return ast.parse(source)


class _TestItem:
    """一个测试条目（模块级断言集合或单个测试函数）的结构摘要。"""

    __slots__ = ("qualname", "asserts", "has_skip", "parametrize_len")

    def __init__(self, qualname, asserts, has_skip, parametrize_len):
        self.qualname = qualname
        self.asserts = asserts          # list[ast.Assert]
        self.has_skip = has_skip
        self.parametrize_len = parametrize_len


def _decorator_skip(dec) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        # pytest.mark.skip(...) / unittest.skipIf(...) 等属性链
        return target.attr in _SKIP_MARKERS
    if isinstance(target, ast.Name):
        return target.id in _SKIP_MARKERS
    return False


def _call_is_skip(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in _SKIP_MARKERS \
        and isinstance(func.value, (ast.Name, ast.Attribute))


def _parametrize_len(dec) -> int | None:
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    if dec.func.attr != "parametrize":
        return None
    for arg in dec.args[1:]:
        if isinstance(arg, (ast.List, ast.Tuple)):
            return len(arg.elts)
    return None


def _collect_items(tree: ast.AST) -> dict:
    """收集 (qualname -> _TestItem)；模块级散断言记为 "<module>"。"""
    items: dict = {}
    module_asserts = [node for node in tree.body if isinstance(node, ast.Assert)]
    if module_asserts:
        items["<module>"] = _TestItem("<module>", module_asserts, False, None)

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _visit_function(self, node):
            name = node.name
            if self.stack:
                owner = self.stack[-1]
                is_test = name.startswith("test") and owner.startswith("Test")
            else:
                is_test = name.startswith("test")
            self.stack.append(name)
            if is_test:
                asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
                has_skip = any(_decorator_skip(d) for d in node.decorator_list) \
                    or any(_call_is_skip(n) for n in ast.walk(node))
                p_len = None
                for d in node.decorator_list:
                    got = _parametrize_len(d)
                    if got is not None:
                        p_len = got if p_len is None else max(p_len, got)
                qualname = "::".join(self.stack[:-1] + [name])
                items[qualname] = _TestItem(qualname, asserts, has_skip, p_len)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    _Visitor().visit(tree)
    return items


def _is_tautology(node: ast.expr) -> bool:
    """结构性恒真：``assert True`` / ``assert x == x`` / ``assert x is x``。

    只收结构上恒真的小子集，宁可漏判也不误伤普通断言。
    """
    if isinstance(node, ast.Constant) and bool(node.value) is True:
        return True
    if isinstance(node, ast.Compare) and len(node.ops) == 1 \
            and isinstance(node.ops[0], (ast.Eq, ast.Is)) \
            and ast.dump(node.left) == ast.dump(node.comparators[0]):
        return True
    return False


def _compare_atoms(node: ast.expr) -> list:
    """提取断言表达式里的数值比较原子：[(op名, 数值, 方向), ...]。"""
    atoms = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        operands = [sub.left] + list(sub.comparators)
        for op, right in zip(sub.ops, operands[1:]):
            op_name = type(op).__name__
            if op_name not in {"Lt", "LtE", "Gt", "GtE", "Eq", "NotEq"}:
                continue
            if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)) \
                    and not isinstance(right.value, bool):
                atoms.append((op_name, right.value, "right"))
            elif isinstance(sub.left, ast.Constant) and isinstance(sub.left.value, (int, float)) \
                    and not isinstance(sub.left.value, bool):
                atoms.append((op_name, sub.left.value, "left"))
    return atoms


def _pair_threshold_findings(base_assert, cand_assert, path, qualname, out):
    """同位置断言的数值变化：能确认方向变宽 → suspect，交人工复核。"""
    base_atoms = _compare_atoms(base_assert.test)
    cand_atoms = _compare_atoms(cand_assert.test)
    for (b_op, b_val, _b_side), (c_op, c_val, _c_side) in zip(base_atoms, cand_atoms):
        if b_op != c_op or isinstance(b_val, complex) or b_val == c_val:
            continue
        widened = (c_op in {"Lt", "LtE"} and c_val > b_val) \
            or (c_op in {"Gt", "GtE"} and c_val < b_val)
        if widened:
            out.append({"code": "SRC_THRESHOLD_RELAXED", "path": path,
                        "test": qualname, "severity": _SEV_SUSPECT,
                        "detail": f"比较阈值 {b_op} {b_val} → {c_op} {c_val}，"
                                  f"接受集合变宽，需人工复核"})
        elif c_op in {"Eq", "NotEq"}:
            out.append({"code": "SRC_EXPECTATION_CHANGED", "path": path,
                        "test": qualname, "severity": _SEV_SUSPECT,
                        "detail": f"期望值 {b_val} → {c_val}，要求变化需人工确认"})


def _analyze_test_module(path, base_text, cand_text, out, base_union, cand_union):
    try:
        base_tree = _parse(base_text)
        cand_tree = _parse(cand_text)
    except SyntaxError:
        out.append({"code": "SRC_PARSE_UNAVAILABLE", "path": path,
                    "test": "<file>", "severity": _SEV_SUSPECT,
                    "detail": "源码无法稳健解析，人工复核"})
        return
    base_items = _collect_items(base_tree) if base_tree is not None else {}
    cand_items = _collect_items(cand_tree) if cand_tree is not None else {}
    base_union.update(base_items)
    cand_union.update(cand_items)
    for qualname, cand_item in cand_items.items():
        base_item = base_items.get(qualname)
        # 恒真断言：候选侧任何结构恒真的 assert，只要不是基线同位置原有
        # 的恒真（那是历史遗留，不是本次引入），一律 blocked。新测试里的
        # assert True 正是"补测放宽标准取巧"的现实入口。
        for idx, cand_assert in enumerate(cand_item.asserts):
            if not _is_tautology(cand_assert.test):
                continue
            preexisting = base_item is not None and idx < len(base_item.asserts) \
                and _is_tautology(base_item.asserts[idx].test)
            if not preexisting:
                out.append({"code": "SRC_ASSERT_TAUTOLOGY", "path": path,
                            "test": qualname, "severity": _SEV_BLOCKED,
                            "detail": "断言为结构恒真（如 assert True / x == x），"
                                      "不构成有效检查"})
        if base_item is None:
            if cand_item.has_skip:
                out.append({"code": "SRC_SKIP_ADDED_NEW_TEST", "path": path,
                            "test": qualname, "severity": _SEV_SUSPECT,
                            "detail": "新增测试自带 skip/xfail，人工复核"})
            continue
        # 保留测试：断言数量下降 → blocked（含断言降级为注释/print）
        if len(cand_item.asserts) < len(base_item.asserts):
            out.append({"code": "SRC_ASSERT_REMOVED", "path": path,
                        "test": qualname, "severity": _SEV_BLOCKED,
                        "detail": f"断言数量 {len(base_item.asserts)} → "
                                  f"{len(cand_item.asserts)}"})
        for base_assert, cand_assert in zip(base_item.asserts, cand_item.asserts):
            _pair_threshold_findings(base_assert, cand_assert, path, qualname, out)
        if cand_item.has_skip and not base_item.has_skip:
            out.append({"code": "SRC_SKIP_ADDED", "path": path,
                        "test": qualname, "severity": _SEV_BLOCKED,
                        "detail": "保留测试新增 skip/xfail"})
        if cand_item.parametrize_len is not None and base_item.parametrize_len is not None \
                and cand_item.parametrize_len < base_item.parametrize_len:
            out.append({"code": "SRC_SCOPE_REDUCED", "path": path,
                        "test": qualname, "severity": _SEV_BLOCKED,
                        "detail": f"参数化用例 {base_item.parametrize_len} → "
                                  f"{cand_item.parametrize_len}"})


def analyze_source_diff(files: dict) -> dict:
    """对采用前后的测试相关源码差异做结构性弱化分析（纯函数，不执行代码）。

    ``files``：``{path: {"baseline": 文本或 None, "candidate": 文本或
    None}}``。``None`` 表示该侧不存在（新增/删除文件）。只分析测试模块
    与配置类文件；业务代码改动不属于测试规则保护范围。

    返回 ``{"findings", "blocked_codes", "suspect_codes", "verdict_hint",
    "analyzed_files", "note"}``；``verdict_hint`` 取
    ``blocked > suspect > clean``，供 ``rule_protection`` 合并进总判定。
    """
    if not isinstance(files, dict):
        raise ValueError("source_diff.files must be a dict of path -> {baseline, candidate}")
    findings: list = []
    base_union: set = set()
    cand_union: set = set()
    analyzed = 0
    for path in sorted(files):
        pair = files.get(path) or {}
        if not isinstance(pair, dict):
            raise ValueError(f"source_diff.files[{path!r}] must be a dict")
        base_text, cand_text = pair.get("baseline"), pair.get("candidate")
        if _is_config_file(path):
            if base_text != cand_text:
                findings.append({"code": "SRC_CONFIG_TOUCHED", "path": path,
                                 "test": "<config>", "severity": _SEV_SUSPECT,
                                 "detail": "测试配置/conftest 变化，能改变收集与"
                                           "断言行为，人工复核"})
                analyzed += 1
            continue
        if not _is_test_module(path):
            continue
        if base_text is None and cand_text is None:
            continue
        _analyze_test_module(path, base_text, cand_text, findings,
                             base_union, cand_union)
        analyzed += 1
    # 删除的测试：以两侧并集为准，测试在文件间搬动不算删除
    for qualname in sorted(base_union - cand_union):
        if qualname == "<module>":
            continue
        findings.append({"code": "SRC_SCOPE_REDUCED", "path": "<moved?>",
                         "test": qualname, "severity": _SEV_BLOCKED,
                         "detail": "基线存在的测试在候选侧消失"})
    blocked = sorted({f["code"] for f in findings if f["severity"] == _SEV_BLOCKED})
    suspect = sorted({f["code"] for f in findings if f["severity"] == _SEV_SUSPECT})
    hint = "blocked" if blocked else ("suspect" if suspect else "clean")
    return {
        "findings": findings,
        "blocked_codes": blocked,
        "suspect_codes": suspect,
        "verdict_hint": hint,
        "analyzed_files": analyzed,
        "note": "结构性源码防护：不能证明语义等价；clean 只表示未命中已知"
                "弱化模式，不是修复成功声明；suspect 必须人工复核",
    }
