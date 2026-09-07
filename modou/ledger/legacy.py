"""镜像适配器：把旧引擎的产出翻译进账本。

SPEC v2.2 §3.2 的 M1：**旧引擎输出照常，同时镜像写入账本。**
这一步**不改变任何对外结论**，它只是让两套表示并存，
从而让差异断言变成每次运行都跑的持续闸门，而不是一次性验收。

--------------------------------------------------------------------------
**必须说清楚这里有什么是脚手架**

`LegacyLine` 这类 Fact 是**迁移脚手架**：它把旧的逐行标签原样搬进账本，
好让 `projection_parity` 能证明"序列化没丢字段、适配器可逆"。

**它不是证据。** 一条 `LegacyLine` 只说明"旧引擎当时是这么标的"，
不说明这个标注本身对不对。真正的 Fact→Claim 推导要到 M3/M4 才落地，
M5 会把这层脚手架连同旧引擎的直写路径一起删掉。

任何人拿 M1 的 parity 通过率去论证"新 Claim 引擎已经可靠"，都是误读。
--------------------------------------------------------------------------

M4 起 Claim 由 `claim_builder.py` 在实验发生后从 Fact / Experiment 独立推导；
本模块不得读取旧 verdict 生成 Claim。
"""
from __future__ import annotations

from ..models import EvidenceUnit, LineResult
from . import anchors, records
from .records import Record

#: 本次运行操作的快照：base + AI patch + test_patch。见 SPEC §1。
SNAPSHOT_S2 = "S2"

#: 迁移脚手架的 Fact 种类。M5 删除。
F_LEGACY_LINE = "LegacyLine"
F_LEGACY_VERDICT = "LegacyVerdict"


def _unit_anchor(u: EvidenceUnit) -> anchors.Anchor:
    """优先用实验发生时记下的那个 Anchor。

    重建是有代价的：重建逻辑与实验层各写一份，任何一处改了结构路径或摘要口径，
    主张的 Anchor 就和它 provenance 里的证据对不上——而那正是 validate.py
    第一次跑就抓到的问题。
    """
    if u.anchor_json:
        return anchors.from_json(u.anchor_json)
    if u.node_type == "file":
        return anchors.file_anchor(SNAPSHOT_S2, u.path, line_end=u.line_end)
    return anchors.unit_anchor(
        SNAPSHOT_S2, u.path, structural_path=u.node_type,
        source=f"{u.path}:{u.line_start}-{u.line_end}:{u.node_type}",
        line_start=u.line_start, line_end=u.line_end)


def mirror(*, run_id: str, units: list[EvidenceUnit],
           lines: list[LineResult]) -> list[Record]:
    """产出账本记录。**只在内存里构造，不落盘**——发布必须等 parity 通过。

    M2 起，`Experiment` 与测试向量 `Fact` 由 `modou/trial.py` 的 TrialRunner
    在**实验真正发生的时刻**写入，这里不再重复生成。事后从 `EvidenceUnit`
    反推能拿到的只有结论，拿不到"当时跑了什么、花了多久、回滚干不干净"。
    """
    out: list[Record] = []

    for u in units:
        a = _unit_anchor(u)

        # 迁移脚手架：旧判定原样留档，供 parity 比对。M5 删除。
        out.append(records.fact(
            F_LEGACY_VERDICT, anchor=a,
            payload={"verdict": u.verdict.value if u.verdict else None,
                     "unit_id": u.unit_id, "note": u.note,
                     "covered_lines": list(u.covered_lines)},
            observer="legacy_engine", run_id=run_id))

    # 迁移脚手架：逐行标签。挂 SourceLocation，不挂 Anchor——它是逐行观测。
    for r in lines:
        out.append(records.fact(
            F_LEGACY_LINE,
            location=anchors.SourceLocation(SNAPSHOT_S2, r.path, r.lineno),
            payload={"label": r.label.value,
                     "reason": r.reason.value if r.reason else None,
                     "unit_id": r.unit_id, "executable": r.executable},
            observer="legacy_engine", run_id=run_id))

    return out
