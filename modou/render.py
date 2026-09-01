"""逐行染色 diff —— 以及它背后的**规范化渲染模型**。

演示口径（方案 §15）——颜色必须与证据单元一致，多行共用一张证书时用连线标出：

  红（承重）  删掉这个单元后，具体测试会失败
  灰（惰性）  被执行过，但移除所在单元后声明测试没有任何变化
  黄（无据）  测试从未执行过这些行
  白（游离）  文件甚至没有进入仓库的测试与引用图

不得出现"安全删除""语义等价""证明某行无用"。

--------------------------------------------------------------------------
**为什么要有 `build_model` / `render_report` 这一层**

早先渲染直接吃 `EvidenceUnit` 与 `{路径: 整文件行}`，而写进 `report.json` 的
只有中文展示文本（"某某测试：passed → failed"），没有状态向量。
结果是**离线回放无法重建原对象**——除非反向解析中文证书，而那是不能做的：
一旦证书措辞改一个字，回放就错，且错得很安静。

所以拆成两步：`build_model()` 产出一份规范化、机器可读的模型写进报告，
`render_report()` **只吃这个模型**。实时输出与离线回放调用同一个函数，
"逐行一致"才成为结构保证，而不是两套代码碰巧长得一样。
--------------------------------------------------------------------------
"""
from __future__ import annotations

from collections import defaultdict

from .models import EvidenceUnit, Label, LineResult

#: 渲染模型版本。老报告（run1/run2）没有这个字段，回放时必须降级并明说。
SCHEMA_VERSION = 3

RED, GREY, YELLOW, WHITE, DIM, BOLD, OFF = (
    "\033[31m", "\033[90m", "\033[33m", "\033[97m", "\033[2m", "\033[1m", "\033[0m")

COLOR = {
    Label.LOAD_BEARING: RED,
    Label.INERT: GREY,
    Label.UNEVIDENCED: YELLOW,
    Label.DRIFT: WHITE,
    Label.UNLABELED: DIM,
}

MARK = {
    Label.LOAD_BEARING: "承重",
    Label.INERT: "惰性",
    Label.UNEVIDENCED: "无据",
    Label.DRIFT: "游离",
    Label.UNLABELED: "未标",
}

_COLOR_BY_VALUE = {l.value: c for l, c in COLOR.items()}
_MARK_BY_VALUE = {l.value: m for l, m in MARK.items()}


# ---------------------------------------------------------------- 模型

def build_model(lines: list[LineResult], units: list[EvidenceUnit],
                source: dict[str, list[str]], summary: dict,
                evidence_by_line: dict | None = None) -> dict:
    """把渲染需要的一切压成一份自足的、机器可读的模型。

    `source` 是 {路径: 整文件行}；模型里只留**新增行**的文本，
    整文件正文不进报告（真实补丁动辄几千行文件，没必要）。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "lines": [
            {
                "file": r.path,
                "line": r.lineno,
                "text": _line_text(source, r.path, r.lineno),
                "label": r.label.value,
                "reason": r.reason.value if r.reason else None,
                "unit_id": r.unit_id,
                # 这一行的结论**凭哪几条账本记录**。来自 ledger.derive 的真实推导，
                # 不在这里重算——UI 要能点开看证据，靠的就是它。
                "evidence_ids": list((evidence_by_line or {}).get(
                    (r.path, r.lineno), ())),
            }
            for r in sorted(lines, key=lambda x: (x.path, x.lineno))
        ],
        "units": [
            {
                "unit_id": u.unit_id,
                "verdict": u.verdict.value if u.verdict else None,
                "location": {"file": u.path, "start": u.line_start,
                             "end": u.line_end, "node_type": u.node_type},
                "regressions": [
                    {"test_id": t, "before": was.value, "after": now.value}
                    for t, was, now in u.regressions()
                ],
                "declared_size": len(u.baseline),
                "covered_lines": list(u.covered_lines),
                "restore_clean": (u.restore_is_clean()
                                  if u.verdict is Label.LOAD_BEARING else None),
                "seconds": round(u.seconds, 2),
            }
            for u in units
        ],
        "summary": summary,
    }


def _line_text(source: dict[str, list[str]], path: str, lineno: int) -> str:
    text = source.get(path) or []
    return text[lineno - 1].rstrip("\n") if 0 < lineno <= len(text) else ""


# ---------------------------------------------------------------- 渲染

def render_report(model: dict, color: bool = True) -> str:
    """**唯一**的渲染入口。实时输出与离线回放都走这里。"""
    return (_render_lines(model, color) + "\n"
            + _render_summary(model, color))


def _c(s: str, code: str, color: bool) -> str:
    return f"{code}{s}{OFF}" if color else s


def _render_lines(model: dict, color: bool = True) -> str:
    rows = model.get("lines", [])
    by_unit: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r.get("unit_id"):
            by_unit[r["unit_id"]].append(r["line"])
    shared = {u for u, ls in by_unit.items() if len(ls) > 1}

    out: list[str] = []
    for path in sorted({r["file"] for r in rows}):
        out.append(_c(f"\n── {path}", BOLD, color))
        prev = None
        for r in sorted((x for x in rows if x["file"] == path),
                        key=lambda x: x["line"]):
            if prev is not None and r["line"] != prev + 1:
                out.append(_c("   ⋮", DIM, color))
            prev = r["line"]
            link = "│" if r.get("unit_id") in shared else " "
            tag = _MARK_BY_VALUE.get(r["label"], r["label"])
            if r["label"] == Label.UNLABELED.value and r.get("reason"):
                tag = f"未标·{r['reason']}"
            col = _COLOR_BY_VALUE.get(r["label"], DIM)
            out.append(f"  {_c(f'{tag:<18}', col, color)}{link} "
                       f"{_c(str(r['line']).rjust(5), DIM, color)} {r.get('text', '')}")
    return "\n".join(out)


def _render_summary(model: dict, color: bool = True) -> str:
    summary = model.get("summary", {})
    total = summary.get("total_added_lines", 0)
    by = summary.get("by_label", {})
    out = [_c("\n" + "─" * 64, DIM, color), _c("逐行标注小结", BOLD, color)]
    for lab in (Label.LOAD_BEARING, Label.INERT, Label.UNEVIDENCED, Label.DRIFT):
        n = by.get(lab.value, 0)
        pct = (n / total * 100) if total else 0
        out.append(f"  {_c(MARK[lab], COLOR[lab], color)}   {n:4d} 行  {pct:5.1f}%")
    un = by.get(Label.UNLABELED.value, 0)
    out.append(f"  {_c('未标注', DIM, color)} {un:4d} 行  "
               f"{(un / total * 100) if total else 0:5.1f}%")
    for reason, n in sorted(summary.get("by_reason", {}).items(),
                            key=lambda kv: -kv[1]):
        out.append(_c(f"        └ {reason}: {n}", DIM, color))
    h1 = "{:.1f}%".format(summary.get("h1", 0.0) * 100)
    out.append(f"\n  新增物理行 {total}，标注覆盖率 {_c(h1, BOLD, color)}")

    lb = [u for u in model.get("units", [])
          if u.get("verdict") == Label.LOAD_BEARING.value]
    if lb:
        out.append(_c("\n承重单元点名的回归测试：", BOLD, color))
        for u in lb[:4]:
            loc = u["location"]
            for reg in u.get("regressions", [])[:2]:
                out.append(f"  {loc['file']}:{loc['start']}-{loc['end']} → "
                           f"{reg['test_id']}  {reg['before']}→{reg['after']}")
    out.append(_c("\n水木验码不替你删代码。它告诉你：哪些部分测试明确要求保留，"
                  "哪些部分测试没有意见，哪些部分测试根本没看见。", DIM, color))
    return "\n".join(out)
