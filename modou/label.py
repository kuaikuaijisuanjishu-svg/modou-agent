"""把三个引擎的产出合并成每行恰好一个最终结果。

优先级（强证据压弱证据）：

    游离 > 承重 > 无据 > 惰性 > 未标注

几个不那么显然的判断，写下理由免得以后自己改错：

- **承重 > 无据**：承重来自一次真实的隔离实验（删了它，指定测试回归），
  而无据只是"没有测试执行到"。前者是做出来的证据，后者是缺席。
- **无据 > 惰性**：一行从没被执行过，那么"删掉它所在的单元后测试没变化"
  对这一行什么都没说。此时给惰性是在暗示可删，不诚实。
- **非执行行继承所在单元的结论**（方案 §3.1）：注释和空行本身无所谓执行不执行，
  但如果它所在的整个单元被删掉且有结论，这个结论覆盖它。
  没有继承到任何单元时，它是 未标注/non_executable。
"""
from __future__ import annotations

from collections import Counter

from .models import EvidenceUnit, Label, LineResult, Unlabeled

_RANK = {Label.DRIFT: 0, Label.LOAD_BEARING: 1, Label.UNEVIDENCED: 2,
         Label.INERT: 3, Label.UNLABELED: 4}

#: 提交版为三态。四态版在 H3 护栏上未通过——两次独立标注都显示"惰性"会把
#: 仅执行到定义行、方法体其实没被测试的新增功能，误导性地呈现成可移除。
#: 惰性的实现保留，但默认不对外呈现，退回 未标注/inert_withheld。
THREE_STATE_DEFAULT = True


def withhold_inert(results: list[LineResult]) -> list[LineResult]:
    """把惰性退回未标注，理由 inert_withheld。不删代码，只是不作此主张。"""
    out = []
    for r in results:
        if r.label is Label.INERT:
            out.append(LineResult(path=r.path, lineno=r.lineno,
                                  label=Label.UNLABELED,
                                  reason=Unlabeled.INERT_WITHHELD,
                                  unit_id=r.unit_id, executable=r.executable))
        else:
            out.append(r)
    return out


def merge(base: list[LineResult], units: list[EvidenceUnit],
          drift_files: set[str]) -> list[LineResult]:
    """base 是引擎 2 给出的初判；units 是引擎 1/3 产出的证据单元。"""
    by_key = {(r.path, r.lineno): r for r in base}

    for u in units:
        if u.verdict is None:
            continue
        for ln in u.span:
            key = (u.path, ln)
            cur = by_key.get(key)
            if cur is None:
                continue                     # 不是本补丁的新增行，不管

            candidate = u.verdict
            # 惰性只能落在被执行过的行，或继承单元结论的非执行行上
            if candidate is Label.INERT and cur.executable and \
                    cur.label is Label.UNEVIDENCED:
                continue                     # 无据 > 惰性

            if _RANK[candidate] < _RANK[cur.label]:
                by_key[key] = LineResult(
                    path=u.path, lineno=ln, label=candidate,
                    reason=None, unit_id=u.unit_id, executable=cur.executable)

    for path in drift_files:
        for (p, ln), r in list(by_key.items()):
            if p == path:
                by_key[(p, ln)] = LineResult(
                    path=p, lineno=ln, label=Label.DRIFT,
                    reason=None, unit_id=r.unit_id, executable=r.executable)

    return sorted(by_key.values(), key=lambda r: (r.path, r.lineno))


def summarize(results: list[LineResult]) -> dict:
    """已标注比例与状态分布。分母是全部新增物理行。"""
    total = len(results)
    labels = Counter(r.label for r in results)
    reasons = Counter(r.reason for r in results if r.reason is not None)
    labeled = sum(labels[l] for l in
                  (Label.DRIFT, Label.LOAD_BEARING, Label.INERT, Label.UNEVIDENCED))
    return {
        "total_added_lines": total,
        "labeled": labeled,
        "h1": (labeled / total) if total else 0.0,
        "by_label": {l.value: labels.get(l, 0) for l in Label},
        "by_reason": {r.value: n for r, n in reasons.items()},
    }


def unlabeled_reason_counts(results: list[LineResult]) -> Counter:
    return Counter(r.reason.value for r in results if r.reason is not None)
