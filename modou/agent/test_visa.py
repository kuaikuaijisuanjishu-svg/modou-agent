"""三段验证器：一条被提议的测试如何挣得「有效补测」签证。

对应 docs/holdout-protocol.md（2026-09-12 冻结）的四条签证与三段映射：

- 第 0 条 payload 隔离：生成/修订请求里不得存在保留集干预的任何指纹；
- 闸 1 基线绿：候选在未干预仓库上全绿（本模块复验并采集 per-test 上下文）；
- 闸 2 干预闸：生成集干预 × 断言级失败 × k=2 两次一致；
- 闸 3 保留集闸：一次性计分，保留集签中 ≥1 才叫「有效补测」。

干预 = 保行号删除变换（modou/mutate.py）。枚举单位 = 引用文件的 AST 语句
单元（含 import——删掉 import 后收集崩坏正是 C 级连带的来源，按协议记
EXCLUDED_UNCOLLECTABLE，不进分母但要披露）。枚举不做 hdd 的层次化下钻：
签证要的是一组确定性的干预，不是归因树。

判级不得在此放宽：ERROR / MISSING / 超时 / 崩溃都是观测型失败，签不了 A
（这条比现行证据分级更严——那里 ERROR 且执行过行也记 A；证据证书口径
不变，签证口径按冻结协议收紧为仅 FAILED）。k=2 两次不一致记 FLAKY，
同样不签。
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from .. import mutate
from ..admissibility import resolve_context
from ..astnodes import candidates as ast_candidates
from ..coverage import CoverageUnavailable, read_contexts, write_rcfile
from ..models import TestStatus
from ..testrange import JUnitUnusable, parse_junit
from .repair import isolated_worktree

__all__ = ["Intervention", "Enumeration", "VisaError", "VISA_PROTOCOL_VERSION",
           "K_RERUNS", "MIN_ADMITTED_PER_CASE", "MAX_INTERVENTIONS",
           "enumerate_interventions", "assert_no_holdout_leak",
           "evaluate_intervention", "verify_candidate", "generation_feedback"]

#: 协议身份：与本模块配套的冻结版本。判据语义变化必须换版本号。
VISA_PROTOCOL_VERSION = "holdout-protocol-v1-frozen-2026-09-12"
K_RERUNS = 2                      # 同一干预下候选共独立运行 2 次，写死
MIN_ADMITTED_PER_CASE = 8         # 案例准入：admitted 干预 ≥ 8 才验证
MAX_INTERVENTIONS = 40            # 产品上限：引用文件超大时先截断再划分
GENERATION_MODULUS = 10           # 划分规则：rank % 10 < 3 进生成集
GENERATION_SLOTS = 3

RunArgv = Callable


class VisaError(RuntimeError):
    """签证流程失败关闭。code 可安全回传展示，detail 可能含路径。"""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Intervention:
    """一次 admitted 干预：删除哪些物理行、删完的文件长什么样。"""

    rel_path: str
    deleted_lines: tuple[int, ...]
    mutated_source: str
    original_source: str
    rank: int = -1                 # 案例内排序位次，-1 表示尚未划分
    split: str = ""                # "generation" | "holdout" | ""

    def sort_key(self) -> tuple[str, int]:
        return (self.rel_path, min(self.deleted_lines))

    def public(self) -> dict:
        return {"rel_path": self.rel_path,
                "deleted_lines": list(self.deleted_lines),
                "rank": self.rank, "split": self.split}


@dataclass(frozen=True)
class Enumeration:
    interventions: tuple[Intervention, ...]   # 已按协议排序并划分
    excluded: tuple[dict, ...]                # 未准入项逐条留痕
    cap_applied: bool = False

    @property
    def generation(self) -> tuple[Intervention, ...]:
        return tuple(iv for iv in self.interventions
                     if iv.split == "generation")

    @property
    def holdout(self) -> tuple[Intervention, ...]:
        return tuple(iv for iv in self.interventions if iv.split == "holdout")


def _executable_unit(src_lines: list[str], node) -> bool:
    """docstring / 注释 / 空行占满的单元不可签——任何测试都观察不到其删除。"""
    seg = src_lines[node.start - 1:node.end]
    body = "\n".join(seg).strip()
    if not body:
        return False
    if all((not ln.strip()) or ln.lstrip().startswith("#") for ln in seg):
        return False
    quote = body[:3]
    if quote in ('"""', "'''") and body.count(quote) >= 2:
        return False
    return True


def enumerate_interventions(
        sources: Mapping[str, str],
        target_lines: Mapping[str, set[int]] | None = None,
) -> Enumeration:
    """对引用文件枚举 admitted 干预，并按冻结规则做 30/70 划分。

    target_lines 为空表示取该文件全部行（产品口径：签证问的是「这条测试
    约束了这个文件的行为吗」，不限于 diff 的新增行）。排序键
    (rel_path, min(deleted_lines))，rank 从 0 起，rank % 10 < 3 进生成集。
    确定性划分：无随机数、无种子，任何人重跑得到同一结果。
    """
    admitted: list[Intervention] = []
    excluded: list[dict] = []
    for path in sorted(sources):
        source = sources[path]
        src_lines = source.splitlines()
        lines = ({int(x) for x in (target_lines or {}).get(path, ())}
                 or set(range(1, len(src_lines) + 1)))
        seen: set[tuple[int, ...]] = set()
        for node in ast_candidates(source, lines):
            key = node.lines
            if key in seen:
                continue
            seen.add(key)
            if not _executable_unit(src_lines, node):
                excluded.append({"rel_path": path, "deleted_lines": list(key),
                                 "code": "non_executable_unit"})
                continue
            try:
                mutation = mutate.delete(source, key)
            except mutate.InvalidTransform:
                excluded.append({"rel_path": path, "deleted_lines": list(key),
                                 "code": "no_valid_transform"})
                continue
            admitted.append(Intervention(rel_path=path,
                                         deleted_lines=tuple(mutation.deleted),
                                         mutated_source=mutation.text,
                                         original_source=source))
    admitted.sort(key=lambda iv: iv.sort_key())
    cap_applied = len(admitted) > MAX_INTERVENTIONS
    if cap_applied:
        for iv in admitted[MAX_INTERVENTIONS:]:
            excluded.append({"rel_path": iv.rel_path,
                             "deleted_lines": list(iv.deleted_lines),
                             "code": "enumeration_cap"})
        admitted = admitted[:MAX_INTERVENTIONS]
    ranked = tuple(
        replace(iv, rank=i,
                split=("generation"
                       if i % GENERATION_MODULUS < GENERATION_SLOTS
                       else "holdout"))
        for i, iv in enumerate(admitted))
    return Enumeration(interventions=ranked, excluded=tuple(excluded),
                       cap_applied=cap_applied)


def _iter_nodes(obj):
    """深度优先走一遍 JSON 形结构，产出每个容器与字符串值。"""
    stack = [obj]
    strings: list[str] = []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield ("dict", cur)
            stack.extend(cur.values())
            strings.extend(str(k) for k in cur)
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)
        elif isinstance(cur, str):
            strings.append(cur)
    yield ("strings", strings)


def assert_no_holdout_leak(payload, *, holdout, allowed_refs=(),
                           sources: Mapping[str, str] | None = None) -> None:
    """第 0 条：payload 里不得存在保留集干预的任何指纹。

    允许出现：案例源码与缺口引用（allowed_refs——那是任务本身）。
    拒绝出现：保留集干预的显式记录（rel_path + deleted_lines、split 标记）、
    不属于 allowed_refs 的保留集行引用、以及被删行原文的 diff 形片段。
    命中即抛 VisaError——宁可拒绝整个请求，也不让保留集进模型上下文。
    """
    holdout = tuple(holdout)
    if not holdout:
        return
    allowed = {str(r) for r in allowed_refs}
    lines_by_path: dict[str, set[int]] = {}
    for iv in holdout:
        lines_by_path.setdefault(iv.rel_path, set()).update(iv.deleted_lines)
    all_strings: list[str] = []
    for kind, node in _iter_nodes(payload):
        if kind == "strings":
            all_strings.extend(node)
            continue
        if node.get("split") == "holdout":
            raise VisaError("HOLDOUT_LEAK",
                            "payload carries holdout split metadata")
        rp = node.get("rel_path")
        dl = node.get("deleted_lines")
        if (isinstance(rp, str) and isinstance(dl, (list, tuple))
                and rp in lines_by_path
                and any(isinstance(x, int) and x in lines_by_path[rp]
                        for x in dl)):
            raise VisaError("HOLDOUT_LEAK",
                            f"payload carries holdout intervention: {rp}")
    for text in all_strings:
        for path, linenos in lines_by_path.items():
            for ln in linenos:
                ref = f"{path}:{ln}"
                if text == ref and ref not in allowed:
                    raise VisaError("HOLDOUT_LEAK",
                                    f"holdout line reference {ref} in payload")
    if sources:
        for iv in holdout:
            src_lines = sources.get(iv.rel_path, "").splitlines()
            for ln in iv.deleted_lines:
                body = (src_lines[ln - 1].strip()
                        if 0 < ln <= len(src_lines) else "")
                if len(body) < 8:
                    continue
                for text in all_strings:
                    if f"- {body}" in text:
                        raise VisaError(
                            "HOLDOUT_LEAK",
                            f"holdout diff fragment {iv.rel_path}:{ln}")


def evaluate_intervention(iv: Intervention, attempts, *,
                          baseline_statuses: Mapping[str, TestStatus],
                          contexts: Mapping[str, Mapping[int, frozenset]],
) -> dict:
    """纯判定：一次干预的 k 次运行 → 等级与是否签中（无副作用，可单测）。

    attempts：k 份观测，每份是 {"statuses": {nodeid: TestStatus}} 或
    {"error": "collect_failed" | "timeout" | "crash"}。签中（A 级）= 存在
    一条基线绿 → 两次均断言级 FAILED → 且基线 per-test 覆盖里执行过被删行
    的测试。ERROR / MISSING / 超时 / 崩溃一律观测型；两次不一致记 FLAKY。
    """
    errors = sorted({a["error"] for a in attempts if "error" in a})
    detail = {
        "rel_path": iv.rel_path, "deleted_lines": list(iv.deleted_lines),
        "rank": iv.rank, "split": iv.split,
        "attempts": [
            ({"statuses": {t: s.value for t, s in a["statuses"].items()}}
             if "statuses" in a else {"error": a["error"]})
            for a in attempts],
    }
    if errors:
        detail["grade"] = ("UNCOLLECTABLE" if "collect_failed" in errors
                           else "OBSERVATION_ONLY")
        detail["error"] = errors[0]
        detail["signed"] = False
        return detail
    failed_sets = [{t for t, s in a["statuses"].items()
                    if s is TestStatus.FAILED} for a in attempts]
    stable = set.intersection(*failed_sets) if failed_sets else set()
    unstable = set.union(*failed_sets) - stable if failed_sets else set()
    regressions = {t for t in stable
                   if baseline_statuses.get(t) is TestStatus.PASSED}
    flaky = {t for t in unstable
             if baseline_statuses.get(t) is TestStatus.PASSED}
    transitioned = {t for a in attempts for t, s in a["statuses"].items()
                    if baseline_statuses.get(t) is TestStatus.PASSED
                    and s is not TestStatus.PASSED}
    detail["flaky"] = bool(flaky)
    if not regressions:
        detail["grade"] = ("OBSERVATION_ONLY" if transitioned
                           else "NO_REGRESSION")
        detail["signed"] = False
        return detail
    by_line = contexts.get(iv.rel_path, {})
    touching: set[str] = set()
    for line in iv.deleted_lines:
        touching |= set(by_line.get(line, ()))
    known: set[str] = set()
    for names in by_line.values():
        known |= set(names)
    best_grade = "B"
    signed_tests: list[str] = []
    for tid in sorted(regressions):
        ctx = resolve_context(tid, known)
        if ctx is not None and ctx in touching:
            best_grade = "A"
            signed_tests.append(tid)
    detail["grade"] = best_grade
    detail["signed"] = best_grade == "A"
    detail["signed_tests"] = signed_tests
    return detail


class _CollectFailed(RuntimeError):
    pass


def _collect(run_argv: RunArgv, worktree: Path, py: str,
             candidate_path: str, timeout: float) -> list[str]:
    r = run_argv([py, "-m", "pytest", candidate_path,
                  "--collect-only", "-q"], worktree, timeout)
    ids = [ln.strip() for ln in (r.stdout or "").splitlines()
           if "::" in ln and not ln.startswith(("=", "_", " ", "E "))]
    if r.returncode not in (0, 5) or not ids:
        raise _CollectFailed()
    if any(w in (r.stdout or "") for w in ("errors during collection",
                                           "ERROR collecting")):
        raise _CollectFailed()
    return ids


def _run_once(run_argv: RunArgv, worktree: Path, py: str,
              candidate_path: str, junit_path: Path,
              timeout: float) -> dict:
    """在当前工作树状态下跑一遍候选，产出逐项状态或观测型错误。"""
    try:
        ids = _collect(run_argv, worktree, py, candidate_path, timeout)
    except _CollectFailed:
        return {"error": "collect_failed"}
    if junit_path.exists():
        junit_path.unlink()
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_argv([py, "-m", "pytest", candidate_path, "-q",
                  f"--junitxml={junit_path}"], worktree, timeout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception:
        return {"error": "crash"}
    try:
        vector = parse_junit(junit_path, ids)
    except JUnitUnusable:
        return {"error": "crash"}
    return {"statuses": vector.as_dict()}


def _run_baseline(run_argv: RunArgv, worktree: Path, py: str,
                  candidate_path: str, run_dir: Path,
                  timeout: float
                  ) -> tuple[dict[str, TestStatus],
                             dict[str, dict[int, frozenset]]]:
    """基线一次运行同时拿逐项状态（JUnit）与 per-test 上下文（coverage）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    rcfile = write_rcfile(run_dir)
    data_file = run_dir / f".coverage.visa.{int(time.time() * 1000)}"
    if data_file.exists():
        data_file.unlink()
    junit = run_dir / "baseline-junit.xml"
    if junit.exists():
        junit.unlink()
    try:
        ids = _collect(run_argv, worktree, py, candidate_path, timeout)
    except _CollectFailed as exc:
        raise VisaError("VISA_BASELINE_COLLECT_FAILED",
                        "candidate cannot be collected in isolation") from exc
    env = {"COVERAGE_FILE": str(data_file), "COVERAGE_RCFILE": str(rcfile)}
    try:
        run_argv([py, "-m", "coverage", "run",
                  f"--rcfile={rcfile}", "-m", "pytest", candidate_path,
                  "-q", f"--junitxml={junit}"], worktree, timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise VisaError("VISA_BASELINE_TIMEOUT",
                        "baseline coverage run exceeded the budget") from exc
    try:
        statuses = parse_junit(junit, ids).as_dict()
    except JUnitUnusable as exc:
        raise VisaError("VISA_BASELINE_JUNIT_UNUSABLE",
                        "baseline JUnit could not be mapped back to nodeids") from exc
    try:
        contexts = read_contexts(py, worktree, data_file, run_argv=run_argv,
                                 rcfile=rcfile)
    except CoverageUnavailable as exc:
        raise VisaError("VISA_BASELINE_COVERAGE_UNAVAILABLE",
                        str(exc)) from exc
    return statuses, contexts


def _stage_intervention(iv: Intervention, *, repo: Path,
                        source_patch: str, candidate_path: str,
                        candidate_code: str, py: str, run_argv: RunArgv,
                        scratch_root: Path, timeout: float,
                        baseline_statuses, contexts) -> dict:
    with isolated_worktree(repo=repo, source_patch=source_patch,
                           scratch_root=scratch_root,
                           prefix="shuimu-visa-") as worktree:
        target = worktree / iv.rel_path
        target.write_text(iv.mutated_source, encoding="utf-8")
        cand = worktree / candidate_path
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(candidate_code, encoding="utf-8")
        attempts = [_run_once(run_argv, worktree, py, candidate_path,
                              scratch_root / f"iv{iv.rank}-run{j + 1}.xml",
                              timeout)
                    for j in range(K_RERUNS)]
    return evaluate_intervention(iv, attempts,
                                 baseline_statuses=baseline_statuses,
                                 contexts=contexts)


def verify_candidate(*, repo: Path, source_patch: str,
                     candidate_path: str, candidate_code: str,
                     cited_files, py: str, run_argv: RunArgv,
                     scratch_root: Path, timeout: float = 60.0) -> dict:
    """跑完整三段验证，产出可落盘的签证记录（JSON 形 dict）。

    调用方必须已保证候选在隔离工作树基线绿（闸 1 的第一半）；本函数
    复验一次并采集 per-test 上下文，然后对生成集与保留集各跑一遍
    k=2 计分。所有工作树一次性、detach、退出即拆；用户 checkout
    在每次进出时都被断言字节不动。
    """
    scratch_root = Path(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    with isolated_worktree(repo=repo, source_patch=source_patch,
                           scratch_root=scratch_root,
                           prefix="shuimu-visa-") as worktree:
        cand = worktree / candidate_path
        if cand.exists():
            raise VisaError("VISA_CANDIDATE_PATH_EXISTS", candidate_path)
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(candidate_code, encoding="utf-8")
        sources: dict[str, str] = {}
        for rel in cited_files:
            path = worktree / str(rel)
            if not path.is_file():
                raise VisaError("VISA_TARGET_MISSING", str(rel))
            sources[str(rel)] = path.read_text(encoding="utf-8")
        statuses, contexts = _run_baseline(
            run_argv, worktree, py, candidate_path,
            scratch_root / "baseline", timeout)
    not_green = {t: s.value for t, s in statuses.items()
                 if s is not TestStatus.PASSED}
    if not statuses or not_green:
        raise VisaError("VISA_BASELINE_NOT_GREEN",
                        json.dumps(not_green or {"candidate": "no tests"}))
    if not contexts.get(candidate_path):
        raise VisaError("VISA_BASELINE_NO_CONTEXTS",
                        "coverage tracer produced no per-test context")
    enumeration = enumerate_interventions(sources)
    record = {
        "schema_version": "test-visa-v1",
        "protocol_version": VISA_PROTOCOL_VERSION,
        "k": K_RERUNS,
        "case": {"cited_files": [str(x) for x in cited_files],
                 "admitted": len(enumeration.interventions),
                 "excluded": [dict(x) for x in enumeration.excluded],
                 "cap_applied": enumeration.cap_applied,
                 "baseline_tests": sorted(statuses)},
        "split_rule": f"rank % {GENERATION_MODULUS} < {GENERATION_SLOTS}"
                      " -> generation",
    }
    if len(enumeration.interventions) < MIN_ADMITTED_PER_CASE:
        return {**record, "status": "WITHHELD_SMALL_CASE",
                "reason": (f"admitted interventions "
                           f"{len(enumeration.interventions)}"
                           f" < {MIN_ADMITTED_PER_CASE}"),
                "generation_stage": {"interventions": [], "signed": 0},
                "holdout_stage": {"interventions": [], "denominator": 0,
                                  "signed": 0, "flaky": 0},
                "effective_rate": None}
    gen_details = [_stage_intervention(
        iv, repo=repo, source_patch=source_patch,
        candidate_path=candidate_path, candidate_code=candidate_code,
        py=py, run_argv=run_argv, scratch_root=scratch_root,
        timeout=timeout, baseline_statuses=statuses, contexts=contexts)
        for iv in enumeration.generation]
    hold_details = [_stage_intervention(
        iv, repo=repo, source_patch=source_patch,
        candidate_path=candidate_path, candidate_code=candidate_code,
        py=py, run_argv=run_argv, scratch_root=scratch_root,
        timeout=timeout, baseline_statuses=statuses, contexts=contexts)
        for iv in enumeration.holdout]
    uncollectable = [d for d in hold_details if d["grade"] == "UNCOLLECTABLE"]
    denominator = len(hold_details) - len(uncollectable)
    signed = [d for d in hold_details if d["signed"]]
    flaky = sum(1 for d in hold_details if d.get("flaky"))
    status = ("WITHHELD_NO_HOLDOUT" if denominator == 0
              else "VERIFIED_EFFECTIVE" if signed
              else "PASSED_NOT_EFFECTIVE")
    return {**record, "status": status,
            "generation_stage": {
                "interventions": gen_details,
                "signed": sum(1 for d in gen_details if d["signed"]),
                "flaky": sum(1 for d in gen_details if d.get("flaky"))},
            "holdout_stage": {
                "interventions": hold_details,
                "uncollectable": [d["rel_path"] + ":"
                                  + str(d["deleted_lines"][0])
                                  for d in uncollectable],
                "denominator": denominator,
                "signed": len(signed),
                "flaky": flaky},
            "effective_rate": (round(len(signed) / denominator, 4)
                               if denominator else None)}


def generation_feedback(visa_record: dict) -> dict:
    """把生成集验证结果折成修订反馈（只含生成集；保留集绝不进模型上下文）。"""
    stage = (visa_record or {}).get("generation_stage") or {}
    rows = stage.get("interventions") or ()
    return {
        "prompt_slot": "generation_feedback",
        "notice": ("以下为候选测试在生成集干预上的验证结果；保留集对生成与"
                   "修订不可见，也不存在于本反馈中。"),
        "generation_interventions": [
            {"rel_path": d["rel_path"], "deleted_lines": d["deleted_lines"],
             "grade": d["grade"], "signed": bool(d["signed"])}
            for d in rows],
        "signed_on_generation": int(stage.get("signed") or 0),
        "total_generation": len(rows),
    }
