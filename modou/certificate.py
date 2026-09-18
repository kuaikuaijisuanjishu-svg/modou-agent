"""证书与运行清单的 JSON 序列化。

六个字段（主张 / 依据 / 方法 / 口径 / 未覆盖 / 状态）与明算的复算证书同构。
措辞由 EvidenceUnit.statement() 统一产出，不在这里另起一套说法。

「未覆盖」是整张证书的重心：它让每张证书自己说清这个结论到底覆盖了什么，
读的人不必去翻文档。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import EvidenceUnit, Label, LineResult, RunManifest

#: 所有证书共有的未覆盖项
COMMON_UNCOVERED = (
    "结论只在已声明的测试范围（FAIL_TO_PASS ∪ PASS_TO_PASS）内成立",
    "不等于语义等价：未被这批测试覆盖的路径、外部副作用、跨进程行为均未验证",
)


def _method(u: EvidenceUnit, cmd_hint: str) -> str:
    return f"{u.transform.describe()}；随后执行已声明测试范围（{cmd_hint}）并逐项比对状态向量"


def _assumption(u: EvidenceUnit) -> str:
    return (f"已声明测试范围共 {len(u.baseline)} 个测试项；"
            f"「未观察到变化」= 全部 {len(u.baseline)} 项状态逐项相同")


#: 每一级在证书里怎么说人话。措辞不能强于这一级真正支撑的东西。
_GRADE_TEXT = {
    "A": "行为证据：这条测试在基线中执行过被移除的行，并在移除后失败",
    "B": "间接证据：这条测试在基线中没有执行过被移除的行",
    "C": "连带损坏：这条测试没有被执行（导入失败或收集期错误）",
    "D": "环境变动：跳过条件改变，与被测行为无关",
}


def _evidence(u: EvidenceUnit) -> list[str]:
    out = []
    if u.verdict is Label.LOAD_BEARING:
        graded = {tid: g for tid, _, _, g in u.graded_regressions}
        for tid, was, now in u.regressions()[:5]:
            grade = graded.get(tid, "")
            suffix = f"（{grade} 级）" if grade else ""
            out.append(f"{tid}：{was.value} → {now.value}{suffix}")
        # 一条 B 级证书必须自己说出它是 B 级，不能让读者从等级字母里猜。
        if u.admissibility is not None:
            out.append(_GRADE_TEXT.get(u.admissibility.value, ""))
        out.append("回滚后状态向量已重新与基线逐项一致"
                   if u.restore_is_clean() else "回滚校验未通过")
    elif u.verdict is Label.INERT:
        out.append(f"该单元被测试执行到的行：{list(u.covered_lines)[:8] or '（见覆盖率数据）'}")
        out.append(f"变换后 {len(u.baseline)} 个测试项状态逐项相同")
    elif u.verdict is Label.DRIFT:
        out += ["base commit 中不存在该路径",
                "pytest 收集清单在该文件存在与移除两种状态下完全一致",
                "全仓库 AST 引用图中无静态引用",
                "临时移除后状态向量逐项相同"]
    return out


def to_dict(u: EvidenceUnit, cmd_hint: str = "pytest") -> dict:
    uncovered = list(COMMON_UNCOVERED)
    if u.note:
        uncovered = u.note.split("；") + uncovered
    return {
        "unit_id": u.unit_id,
        "状态": u.verdict.value if u.verdict else "未得出结论",
        "可采性": u.admissibility.value if u.admissibility else None,
        "可采性说明": (_GRADE_TEXT.get(u.admissibility.value, "")
                       if u.admissibility else ""),
        "逐条定级": [{"test_id": tid, "before": before, "after": after,
                      "等级": grade}
                     for tid, before, after, grade in u.graded_regressions],
        "主张": u.statement(),
        "依据": _evidence(u),
        "方法": _method(u, cmd_hint),
        "口径": _assumption(u),
        "未覆盖": uncovered,
        "位置": {"file": u.path, "start": u.line_start, "end": u.line_end,
                 "node_type": u.node_type},
        "耗时秒": round(u.seconds, 2),
        "回滚干净": u.restore_is_clean() if u.verdict is Label.LOAD_BEARING else None,
    }


def build_payload(manifest: RunManifest, units: list[EvidenceUnit],
                  lines: list[LineResult], summary: dict,
                  cmd_hint: str = "pytest",
                  render_model: dict | None = None,
                  minimizations: list[dict] | None = None) -> dict:
    """构造报告内容，但**不落盘**。

    与 write_report 分开，是为了让调用方能在发布之前先跑校验（如账本 parity）：
    半份 report.json 比没有 report.json 更坏——它看起来像一份正式产物。
    """
    payload = {
        "manifest": manifest.to_json(),
        "summary": summary,
        "certificates": [to_dict(u, cmd_hint) for u in units],
        "lines": [
            {"file": r.path, "line": r.lineno, "label": r.label.value,
             "reason": r.reason.value if r.reason else None,
             "unit_id": r.unit_id}
            for r in lines
        ],
        "口径声明": list(COMMON_UNCOVERED),
    }
    if minimizations:
        # Additive and optional: absent unless ddmin ran, so every existing
        # report and every v2 required field stays byte-for-byte as it was.
        payload["minimizations"] = minimizations
    if render_model is not None:
        # 规范化渲染模型：离线回放靠它重建输出，不必反向解析中文证书。
        payload["render_model"] = render_model
    return payload


def write_report(run_dir: Path, manifest: RunManifest,
                 units: list[EvidenceUnit], lines: list[LineResult],
                 summary: dict, cmd_hint: str = "pytest",
                 render_model: dict | None = None) -> Path:
    """证书写在工作树之外，恢复基线时不会被 clean 掉。发布是原子的。"""
    from .runroot import write_atomic
    payload = build_payload(manifest, units, lines, summary, cmd_hint,
                            render_model)
    return write_atomic(run_dir / "report.json",
                        json.dumps(payload, ensure_ascii=False, indent=2))
