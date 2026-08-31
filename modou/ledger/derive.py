"""Derive line conclusions from structured ledger facts.

The inputs are facts and experiments; the output is a line conclusion with evidence ids.

于是 parity 第一次有意义：它不再验证 round-trip，而是验证
**新推导与旧引擎在同一批行上是否给出同一结论**。对不上就是学到东西。

--------------------------------------------------------------------------
**这里推导的是「结论」，不是「策略」。**

哪个 AST 节点算"最小已隔离单元"、什么时候停止下钻、预算怎么分——
那些是引擎的策略，仍然留在 `hdd.py`，也应该留在那里。
本模块只回答：**给定已经记录下来的事实与实验，每一行的结论是什么、
凭哪几条记录。** 把策略也搬进来不会让结论更可靠，只会让它更难审。
--------------------------------------------------------------------------

优先级沿用 `label.merge`：游离 > 承重 > 无据 > 惰性 > 未标注。
每一条都写明理由，见该模块。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Label, LineResult, Unlabeled
from .records import (CLAIM, C_REQUIRED_BY_TEST, EXPERIMENT, FACT,
                      F_COLLECT_SET, F_FILE_KIND, F_GIT_PRESENCE,
                      F_LINE_CONTEXT, F_PROBE_OUTCOME, F_REF_EDGE,
                      F_TEST_VECTOR, COMPLETE)


@dataclass
class DerivedLine:
    """一行的推导结论，外加它凭什么。"""
    path: str
    lineno: int
    label: Label
    reason: Unlabeled | None = None
    unit_id: str | None = None
    executable: bool = True
    evidence_ids: tuple[str, ...] = ()

    def as_line_result(self) -> LineResult:
        return LineResult(path=self.path, lineno=self.lineno, label=self.label,
                          reason=self.reason, unit_id=self.unit_id,
                          executable=self.executable)


@dataclass
class _Index:
    """按用途把账本切开，避免每条推导都全表扫描。"""
    line_ctx: dict = field(default_factory=dict)      # path -> payload
    file_kind: dict = field(default_factory=dict)
    git: dict = field(default_factory=dict)
    ref: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)
    collect: dict | None = None
    collects: dict = field(default_factory=dict)     # record_id -> payload
    vectors: dict = field(default_factory=dict)       # record_id -> payload
    experiments: list = field(default_factory=list)
    exp_status: dict = field(default_factory=dict)    # record_id -> status
    claims: list = field(default_factory=list)


def _index(rows: list[dict]) -> _Index:
    ix = _Index()
    for r in rows:
        p = r["payload"]
        rid = r["record_id"]
        if r["record_type"] == FACT:
            kind = p.get("kind")
            path = (p.get("anchor") or {}).get("path", "")
            data = p.get("data") or {}
            if kind == F_LINE_CONTEXT:
                ix.line_ctx[path] = (data, rid)
            elif kind == F_FILE_KIND:
                ix.file_kind[path] = (data, rid)
            elif kind == F_GIT_PRESENCE:
                ix.git[path] = (data, rid)
            elif kind == F_REF_EDGE:
                ix.ref[path] = (data, rid)
            elif kind == F_PROBE_OUTCOME:
                ix.probe[path] = (data, rid)
            elif kind == F_COLLECT_SET:
                ix.collects[rid] = data
                if data.get("which", "baseline") == "baseline":
                    ix.collect = (data, rid)
            elif kind == F_TEST_VECTOR:
                ix.vectors[rid] = data
        elif r["record_type"] == EXPERIMENT:
            ix.experiments.append((p, rid))
            ix.exp_status[rid] = p.get("status")
        elif r["record_type"] == CLAIM:
            ix.claims.append((p, rid))
    return ix


def _vector_comparison(ix: _Index, exp: dict) -> tuple[bool, str, str]:
    """从账本里的完整状态列表逐项复算 baseline/mutated 是否一致。

    不采信写入时附带的 ``identical_to_baseline`` 布尔值：那只是便于阅读的
    冗余字段，不是推导输入。缺任一向量、测试 ID 不同或状态不同都返回失败。
    """
    baseline_id = next((fid for fid in exp.get("pre_fact_ids", [])
                        if (ix.vectors.get(fid) or {}).get("which") == "baseline"), "")
    mutated_id = next((fid for fid in exp.get("post_fact_ids", [])
                       if (ix.vectors.get(fid) or {}).get("which") == "mutated"), "")
    if not baseline_id or not mutated_id:
        return False, baseline_id, mutated_id
    baseline = ix.vectors[baseline_id].get("statuses")
    mutated = ix.vectors[mutated_id].get("statuses")
    if not isinstance(baseline, list) or not isinstance(mutated, list):
        return False, baseline_id, mutated_id
    return sorted(baseline) == sorted(mutated), baseline_id, mutated_id


def _identical(ix: _Index, exp: dict) -> bool | None:
    """向后兼容的普通单元实验判断；游离不得调用此捷径。"""
    for fid in exp.get("post_fact_ids", []):
        v = ix.vectors.get(fid)
        if v and v.get("which") == "mutated":
            return v.get("identical_to_baseline")
    return None


def _collect_identical(ix: _Index, exp: dict) -> tuple[bool, str]:
    """文件移除前后的 pytest 收集集是否真实逐项相同。"""
    if ix.collect is None:
        return False, ""
    baseline, baseline_id = ix.collect
    mutated_id = ""
    mutated = None
    for fid in exp.get("post_fact_ids", []):
        data = ix.collects.get(fid)
        if data and data.get("which") == "mutated":
            mutated, mutated_id = data, fid
            break
    if mutated is None:
        return False, ""
    same = sorted(baseline.get("nodeids", [])) == sorted(mutated.get("nodeids", []))
    return same, mutated_id


def derive(rows: list[dict], *, three_state: bool = True) -> list[DerivedLine]:
    """账本 → 逐行结论。**纯函数**：同一份账本必须给出同一组结论。"""
    ix = _index(rows)
    out: dict[tuple[str, int], DerivedLine] = {}

    # ---------------------------------------------------------- 底：覆盖率
    # 无据、未测量、非执行行全部由这一份事实决定。
    for path, (ctx, cid) in ix.line_ctx.items():
        measured = ctx["measured"]
        executable = set(ctx["executable"])
        executed = set(ctx["executed"])
        textual = set(ctx["textually_non_executable"])
        allowed = ix.file_kind.get(path, ({}, ""))[0].get("probe_allowed", True)
        kid = ix.file_kind.get(path, ({}, ""))[1]
        for ln in ctx["added_lines"]:
            ev = (cid,)
            if ln in textual:
                # 文本上就看得出非执行。空行、注释、纯括号续行**不能**
                # 因为"覆盖率里没有它"就被推成无据。
                d = DerivedLine(path, ln, Label.UNLABELED,
                                Unlabeled.NON_EXECUTABLE, executable=False,
                                evidence_ids=ev)
            elif not measured:
                # 整个文件没被测量到 ≠ 里面的行不可执行。只是没有数据。
                d = DerivedLine(path, ln, Label.UNLABELED,
                                Unlabeled.NOT_MEASURED, executable=False,
                                evidence_ids=ev)
            elif ln not in executable:
                d = DerivedLine(path, ln, Label.UNLABELED,
                                Unlabeled.NON_EXECUTABLE, executable=False,
                                evidence_ids=ev)
            elif ln not in executed:
                d = DerivedLine(path, ln, Label.UNEVIDENCED, None,
                                executable=True, evidence_ids=ev)
            else:
                d = DerivedLine(path, ln, Label.UNLABELED,
                                Unlabeled.UNSUPPORTED_FILE if not allowed
                                else Unlabeled.NOT_ISOLATED,
                                executable=True,
                                evidence_ids=ev + ((kid,) if kid else ()))
            out[(path, ln)] = d

    # ---------------------------------------------------------- 探测未归因原因
    for path, (pdata, pid) in ix.probe.items():
        for s, reason in (pdata.get("unresolved") or {}).items():
            key = (path, int(s))
            d = out.get(key)
            if d is not None and d.label is Label.UNLABELED:
                d.reason = Unlabeled(reason)
                d.evidence_ids = d.evidence_ids + (pid,)

    # ---------------------------------------------------------- 惰性
    # 判据：删掉这个单元后状态向量逐项相同。**不下钻**，所以一个单元一条。
    # 它落在单元跨度内的所有行上，但**无据优先**——一行从没被执行过，
    # "删了没变化"对它什么都没说，此时给惰性是在暗示可删，不诚实。
    for exp, xid in ix.experiments:
        a = exp.get("anchor") or {}
        if a.get("kind") != "unit" or exp.get("status") != COMPLETE:
            continue
        if _identical(ix, exp) is not True:
            continue
        for ln in range(a.get("line_start", 0), a.get("line_end", 0) + 1):
            d = out.get((a["path"], ln))
            if d is None:
                continue
            if d.label is Label.UNEVIDENCED and d.executable:
                continue                       # 无据 > 惰性
            if d.label is Label.UNLABELED:
                d.label, d.reason = Label.INERT, None
                d.evidence_ids = d.evidence_ids + (xid,)

    # ---------------------------------------------------------- 承重
    # 只认 Claim。粗节点删了也会红，但那不是最小已隔离单元——
    # 是否已下钻到底是引擎的策略判断，它体现在"谁产出了 Claim"上。
    for cl, cid in ix.claims:
        if cl.get("kind") != C_REQUIRED_BY_TEST:
            continue
        # 没跑完的实验什么都没观测到，撑不起承重。
        # `validate` 已经会让这种账本整份失败关闭；这里再挡一次，
        # 因为 derive 是纯函数，Agent 层的 read_evidence 会拿它去读
        # **别人给的**账本，那时没有发布闸门在前面守着。
        if any(ix.exp_status.get(pid, COMPLETE) != COMPLETE
               for pid in cl.get("provenance", [])):
            continue
        a = cl.get("anchor") or {}
        for ln in range(a.get("line_start", 0), a.get("line_end", 0) + 1):
            d = out.get((a["path"], ln))
            if d is None:
                continue
            d.label, d.reason = Label.LOAD_BEARING, None
            d.evidence_ids = d.evidence_ids + (cid,) + tuple(cl.get("provenance", []))

    # ---------------------------------------------------------- 游离
    # 五条判据全部成立才算。任何一条缺失都不给——
    # 补丁前已存在的文件永不命中，这是硬负向控制。
    # dict.fromkeys 而不是 `+`：一个 path 同时有覆盖率事实和 Git 事实时，
    # 走两遍会把同一批证据 ID 追加两次，让"这行有几条证据"虚高一倍。
    for path in dict.fromkeys(list(ix.line_ctx) + list(ix.git)):
        g, gid = ix.git.get(path, (None, ""))
        r, rid = ix.ref.get(path, (None, ""))
        if not g or g.get("exists_in_base") is not False:
            continue
        if not r or r.get("found") is not False:
            continue
        hit = next(((x, xi) for x, xi in ix.experiments
                    if (x.get("anchor") or {}).get("path") == path
                    and (x.get("anchor") or {}).get("kind") == "file"
                    and x.get("status") == COMPLETE
                    and x.get("restored_clean") is True
                    and _vector_comparison(ix, x)[0]
                    and _collect_identical(ix, x)[0]), None)
        if hit is None:
            continue
        _, mutated_collect_id = _collect_identical(ix, hit[0])
        _, baseline_vector_id, mutated_vector_id = _vector_comparison(ix, hit[0])
        ev = (gid, rid, hit[1], ix.collect[1], mutated_collect_id,
              baseline_vector_id, mutated_vector_id)
        for (p, ln), d in out.items():
            if p == path:
                d.label, d.reason = Label.DRIFT, None
                d.evidence_ids = d.evidence_ids + ev

    # ---------------------------------------------------------- 三态降级
    if three_state:
        for d in out.values():
            if d.label is Label.INERT:
                # 不删代码，只是不作此主张。原因必须显式，不能悄悄消失。
                d.label, d.reason = Label.UNLABELED, Unlabeled.INERT_WITHHELD

    return sorted(out.values(), key=lambda d: (d.path, d.lineno))
