"""证据可采性分级：一次失败凭什么算数。

产品原本把「向量变了」等同于「这段代码受测试约束」。它不等同。删掉一行
`import json`，模块 import 不了，pytest 在收集阶段就崩了，三条测试**一条都
没跑**——旧口径照样签「承重」，并且用和真实断言失败完全相同的措辞点名那三条
测试。评委只要问一句「这三条里哪一条断言了这一行的行为」，就没有答案。

判据只用两样已经在手的数据：

1. 逐项测试状态转变（``TestVector``），不是退出码；
2. **per-test** 行级基线覆盖率（``coverage`` 的 ``dynamic_context``）——
   才能回答「这条失败的测试执行过那一行吗」。聚合覆盖率回答不了，它只能说
   「范围内某条测试执行过」。

分级只向下不向上：映射不到上下文、上下文有歧义、覆盖率缺失，一律记 B 而不是
A。不确定不能变成更强的主张。
"""
from __future__ import annotations

import re

from .models import (Admissibility, ADMISSIBILITY_ORDER, TestStatus)

#: pytest 会把参数化写进 nodeid（``test_f[1]``），coverage 的上下文名里没有
#: 参数——同一条参数化测试的所有参数共用一个上下文。比对前必须先去掉。
_PARAMS = re.compile(r"\[.*\]$")


def context_tail(nodeid: str) -> str:
    """把 pytest nodeid 折成 coverage 上下文名的尾部。

    ``tests/a/test_same.py::TestGroup::test_in_class``
        → ``test_same.TestGroup.test_in_class``

    只取尾部而不去拼完整点分模块名，是因为「文件路径 → 模块名」取决于仓库有没有
    ``__init__.py``、rootdir 在哪、以及 pytest 的 import 模式。猜错了会静默误判，
    而按尾部唯一匹配失败时我们能知道自己失败了。
    """
    parts = nodeid.split("::")
    if not parts:
        return ""
    module = parts[0].rsplit("/", 1)[-1]
    if module.endswith(".py"):
        module = module[:-3]
    rest = [_PARAMS.sub("", p) for p in parts[1:]]
    return ".".join([module, *rest]) if rest else module


def resolve_context(nodeid: str, known: frozenset[str] | set[str]) -> str | None:
    """在已知上下文名里找这条测试，找不到或不唯一都返回 None。

    不唯一是真实存在的：两个不同目录下的同名测试文件，尾部一样。这种时候
    **不能挑一个**——挑错会把一条没碰过这行的测试记成行为证据。
    """
    tail = context_tail(nodeid)
    if not tail:
        return None
    hits = [name for name in known
            if name == tail or name.endswith("." + tail)]
    return hits[0] if len(hits) == 1 else None


def grade_one(before: TestStatus, after: TestStatus, *,
              executed_the_lines: bool | None) -> Admissibility:
    """给一条状态转变定级。

    ``executed_the_lines`` 为 None 表示「没能确定」——按不确定处理，记 B。
    """
    if after is TestStatus.MISSING:
        return Admissibility.COLLATERAL
    if after is TestStatus.SKIPPED or before is TestStatus.SKIPPED:
        return Admissibility.ENVIRONMENTAL
    if after in (TestStatus.FAILED, TestStatus.ERROR):
        return (Admissibility.BEHAVIOURAL if executed_the_lines
                else Admissibility.INDIRECT)
    return Admissibility.INDIRECT


def grade(regressions, *, deleted_lines, contexts_by_line
          ) -> tuple[tuple[str, str, str, str], ...]:
    """给一个单元的全部具名回归定级。

    ``contexts_by_line``：{行号: {执行过它的测试上下文名}}，来自基线覆盖率。
    ``deleted_lines``：本次干预真正删掉的行号。
    """
    touching: set[str] = set()
    for line in deleted_lines:
        touching |= set(contexts_by_line.get(line, ()))
    known: set[str] = set()
    for names in contexts_by_line.values():
        known |= set(names)

    out = []
    for tid, before, after in regressions:
        if not known:
            # 这个文件一条 per-test 上下文都没有：覆盖率没测到它，或者采集
            # 口径里没开 dynamic_context。无法区分 A 和 B，按 B 记。
            executed = None
        else:
            ctx = resolve_context(tid, known)
            executed = (ctx in touching) if ctx is not None else None
        out.append((tid, before.value, after.value,
                    grade_one(before, after, executed_the_lines=executed).value))
    return tuple(out)


def best(graded: tuple[tuple[str, str, str, str], ...]) -> Admissibility | None:
    """一个单元取它最强的那一条等级。"""
    grades = {g for _, _, _, g in graded}
    for level in ADMISSIBILITY_ORDER:
        if level.value in grades:
            return level
    return None
