"""引擎 1：游离。

五条判据必须**全部**成立（方案 §6.1）：
  ① base commit 里不存在该路径；
  ② 文件在与不在两种状态下，pytest 收集到的 nodeid 集合完全一致，且两次收集都无错误；
  ③ 全仓库 AST import 图没有任何静态引用；
  ④ 临时移除后，已声明测试状态向量与基线逐项相同。
  ⑤ Trial 经 git reset + clean 后 tree hash 与工作树状态均回到基线。

为什么不用文件名启发式：上一版按 `test_*` 这类命名把文件划进禁区，
既太松（`reproduce.py` 不叫 test 但同样是残留），又太紧（把大量浪费划成不可碰）。
更要命的是，"名字像测试所以我删了它"是站不住的；
"pytest 根本收集不到它、全仓库没人 import 它、删了每个测试项状态都不变"才站得住。

**补丁前已存在的文件永不命中游离** —— 这是硬负向控制，必须有测试守着。
"""
from __future__ import annotations

import ast
import time
import warnings
from pathlib import Path

from .. import trial
from ..ledger import anchors, observe
from ..models import (EvidenceUnit, Label, TestVector, Transform, make_unit_id)
from ..testrange import collect_nodeids, TestRangeError

#: 证书必须声明这几件事排除不了
DRIFT_UNCOVERED = (
    "无法排除通过反射、运行时字符串拼接导入的使用",
    "无法排除仓库外的脚本、打包入口点（entry_points）引用",
    "结论只在已声明的测试范围内成立",
)


# ------------------------------------------------------------ 静态引用图

def _module_names(rel_path: str, package_root: str) -> set[str]:
    """一个文件可能被哪些模块名指代。"""
    if not rel_path.endswith(".py"):
        return set()
    p = rel_path[:-3]
    names = set()
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    dotted = p.replace("/", ".")
    names.add(dotted)
    names.add(dotted.rsplit(".", 1)[-1])          # 裸模块名（同包内相对引用）
    return {n for n in names if n}


def _iter_imports(tree: ast.AST, importer_pkg: str):
    """产出这个文件静态引用到的模块名。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:                         # 相对导入
                parts = importer_pkg.split(".") if importer_pkg else []
                up = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join([*up, base]) if base else ".".join(up)
            if base:
                yield base
            for a in node.names:                   # from pkg import mod
                yield f"{base}.{a.name}" if base else a.name
        elif isinstance(node, ast.Call):
            fn = node.func
            is_imp = (isinstance(fn, ast.Attribute) and fn.attr == "import_module") \
                or (isinstance(fn, ast.Name) and fn.id in ("import_module", "__import__"))
            if is_imp and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    yield a0.value


def statically_referenced(worktree: Path, rel_path: str,
                          package_root: str) -> str | None:
    """有任何文件静态引用它就返回那个引用者，否则 None。"""
    targets = _module_names(rel_path, package_root)
    if not targets:
        return None
    target_file = worktree / rel_path
    for py in worktree.rglob("*.py"):
        if py == target_file or ".git" in py.parts:
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        rel = str(py.relative_to(worktree))
        pkg = rel[:-3].replace("/", ".").rsplit(".", 1)[0]
        for name in _iter_imports(tree, pkg):
            if name in targets:
                return rel
            for t in targets:
                if name.startswith(t + ".") or name.endswith("." + t):
                    return rel
    return None


# ------------------------------------------------------------ 主判定

def evaluate(ws, adapter, candidate: str, *, baseline: TestVector,
             nodeids: list[str], test_files: list[str], run_dir: Path,
             baseline_collected: list[str], deadline: float, runner
             ) -> tuple[bool, EvidenceUnit | None, str]:
    """判一个候选文件是不是游离。返回 (是否游离, 证据单元, 未通过的原因)。"""
    t0 = time.time()

    # ① base commit 里不存在
    in_base = ws.exists_in_base(candidate)
    runner.collector.append(observe.git_presence(
        candidate, exists_in_base=in_base,
        base_commit=getattr(ws, "base_commit", ""), run_id=runner.run_id))
    if in_base:
        return False, None, "补丁前已存在该文件（硬负向控制）"

    src = ws.path / candidate
    if not src.exists():
        return False, None, "工作树里找不到该文件"

    # ③ 引用扫描（先做，最便宜，不用跑测试）
    #    .py 走 AST import 图；非 .py 不是模块，只能被路径字符串用到，扫文本
    if candidate.endswith(".py"):
        ref = statically_referenced(ws.path, candidate, adapter.package_root)
        how = "ast_import_graph"
    else:
        ref = referenced_by_path(ws.path, candidate)
        how = "path_string_scan"
    runner.collector.append(observe.ref_edge(
        candidate, referenced_by=ref, method=how, run_id=runner.run_id))
    if ref:
        return False, None, f"被 {ref} 引用"

    # ② + ④ 需要把文件临时移开。移开与放回统一由 TrialRunner 负责——
    #    "无论判成什么都放回去"这种不能出错的动作，只应该有一份实现。
    text = src.read_text(encoding="utf-8", errors="surrogateescape")
    anchor = anchors.file_anchor("S2", candidate, content=text,
                                 line_end=max(1, text.count("\n") + 1))

    def during():
        """② 移除后收集清单必须完全一致。不一致就没必要再跑测试了。"""
        try:
            after = collect_nodeids(adapter, ws.python, ws.path, test_files)
        except TestRangeError as e:
            return f"移除后收集失败：{str(e)[:100]}"
        cf = observe.collect_set(after, run_id=runner.run_id, which="mutated",
                                 anchor=anchor)
        runner.collector.append(cf)
        if set(after) != set(baseline_collected):
            d1 = set(baseline_collected) - set(after)
            d2 = set(after) - set(baseline_collected)
            return trial.DuringObservation(
                f"收集清单发生变化（少{len(d1)} 多{len(d2)}）",
                (cf.record_id,))
        return trial.DuringObservation(fact_ids=(cf.record_id,))

    out = runner.remove_file(
        candidate, anchor=anchor, deadline=deadline,
        junit_name=f"drift__{candidate.replace('/', '__')}.xml",
        stash_dir=run_dir, during=during)

    if out.status is trial.ABORTED:
        return False, None, out.abort_reason
    if out.status is trial.TIMEOUT:
        return False, None, f"移除后测试失败：{out.abort_reason}"
    if not out.ran or out.vector is None:
        return False, None, out.abort_reason or "移除实验未能完成"

    # ④ 声明测试状态向量必须逐项相同
    if not out.vector.identical_to(baseline):
        regs = out.vector.regressions(baseline)
        return False, None, f"移除后状态向量变化（{len(regs)} 项回归）"

    unit = EvidenceUnit(
        unit_id=make_unit_id(candidate, (), "drift"),
        path=candidate, line_start=1,
        line_end=max(1, text.count("\n") + 1),
        node_type="file",
        transform=Transform(deleted_lines=(), file_removed=True),
        baseline=baseline, mutated=out.vector,
        seconds=time.time() - t0, verdict=Label.DRIFT,
        note="；".join(DRIFT_UNCOVERED),
        experiment_id=out.experiment_id, fact_ids=out.fact_ids,
        anchor_json=anchor.to_json())
    return True, unit, ""


#: 非 Python 文件只能通过"路径字符串"被用到，扫文本即可
_TEXT_SUFFIXES = (".py", ".cfg", ".ini", ".toml", ".txt", ".rst", ".md",
                  ".yaml", ".yml", ".json", ".in")


def referenced_by_path(worktree: Path, rel_path: str) -> str | None:
    """非 .py 文件的引用扫描：它只可能被路径字符串引用（open()、data_files 等）。

    比 AST 引用图弱，但对这类文件是对的——它不是模块，import 不到。
    """
    import posixpath
    base = posixpath.basename(rel_path)
    target = worktree / rel_path
    for f in worktree.rglob("*"):
        if not f.is_file() or f == target or ".git" in f.parts:
            continue
        if f.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if rel_path in text or base in text:
            return str(f.relative_to(worktree))
    return None


def candidates(diff_added: dict, new_files: set[str]) -> list[str]:
    """游离考察本补丁**新建**的文件。

    不限于 .py：agent 丢下的 `crv_types.py.bak` 这类备份文件同样"没有进入
    仓库的测试与引用图"，四条判据对它一样成立，而且它连模块都不是、更不可能被 import。
    早先只看 .py 让一个 1854 行的 .bak 文件永远拿不到任何标签。
    """
    return sorted(p for p in diff_added if p in new_files)
