"""引擎 2：无据。

判据只有一条（方案 §6.2）：

> 该行属于 coverage 的 executable statements，且不在 executed lines 中。

两条禁令：

1. **不得**通过"删除后测试仍绿"生成无据。零覆盖时测试全绿本来就不构成删除证据——
   测试压根没执行到它，全绿说明不了任何事。无据必须直接由覆盖率得出。
2. **不得**把非执行行（空行、注释、纯括号续行）判成无据。
   它们不在 executable statements 里，"覆盖率中没有它"是废话，不是证据。
   这类行除非继承了一个完整证据单元，否则是 未标注/non_executable。

不跑任何删除，因此极便宜。
"""
from __future__ import annotations

from ..coverage import CoverageResult
from ..diffstat import AddedLine
from ..models import Label, LineResult, Unlabeled


def label_lines(path: str, added: list[AddedLine], cov: CoverageResult,
                supported: bool) -> list[LineResult]:
    """给一个文件的新增行做无据/非执行的初判。

    supported=False 表示这是测试/配置/文档一类不做删除探测的文件：
    它仍然可以拿无据标签（覆盖率对它一样有话说），但永远不会拿惰性。
    """
    # 这个文件到底有没有被覆盖率测量过？没测量过 ≠ 里面的行不可执行。
    measured = path in cov.executable

    out: list[LineResult] = []
    for a in added:
        executable = cov.is_executable(path, a.lineno)

        # 文本上就看得出非执行（空行、注释、纯括号）—— 任何情况下都不判无据
        if a.non_executable:
            out.append(LineResult(path=path, lineno=a.lineno,
                                  label=Label.UNLABELED,
                                  reason=Unlabeled.NON_EXECUTABLE,
                                  executable=False))
            continue

        # 整个文件压根没被测量到（典型情况：新建的脚本没人 import）。
        # 这时说"不可执行"是错的——我们只是没有数据。必须与 non_executable 区分开，
        # 否则一个未被导入的新脚本会被整片误记成“非执行行”。
        if not measured:
            out.append(LineResult(path=path, lineno=a.lineno,
                                  label=Label.UNLABELED,
                                  reason=Unlabeled.NOT_MEASURED,
                                  executable=False))
            continue

        # 文件测量过，但 coverage 判定这一行不是语句（如 docstring 续行）
        if not executable:
            out.append(LineResult(path=path, lineno=a.lineno,
                                  label=Label.UNLABELED,
                                  reason=Unlabeled.NON_EXECUTABLE,
                                  executable=False))
            continue

        if cov.unevidenced(path, a.lineno):
            out.append(LineResult(path=path, lineno=a.lineno,
                                  label=Label.UNEVIDENCED, executable=True))
        else:
            # 被执行过。它的归宿要等引擎 3 的删除探测来定。
            reason = (Unlabeled.UNSUPPORTED_FILE if not supported
                      else Unlabeled.NOT_ISOLATED)
            out.append(LineResult(path=path, lineno=a.lineno,
                                  label=Label.UNLABELED, reason=reason,
                                  executable=True))
    return out


def pending_probe(results: list[LineResult]) -> list[int]:
    """还需要引擎 3 去探测的行：被执行过、且暂时挂在 NOT_ISOLATED 上的。"""
    return [r.lineno for r in results
            if r.label is Label.UNLABELED and r.reason is Unlabeled.NOT_ISOLATED]
