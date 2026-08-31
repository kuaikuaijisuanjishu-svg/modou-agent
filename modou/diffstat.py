"""解析 unified diff，取出 AI 补丁的新增物理行。

这里必须保留**每一行的文本**，以便生成可复核的摘要。
因为方案 §3.1 要求空行、注释和只含括号的非执行行不能靠 coverage 缺席判成无据，
而判断"是不是非执行行"需要看内容。行号和文本必须一起带出来。

二进制文件没有物理行：单独标出，不进 H1 的分母。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: 只含闭合括号/逗号/冒号的续行，例如 `)`, `],`, `):`
_BRACKET_ONLY = re.compile(r"^[\s\)\]\}\,\:]*$")


@dataclass
class AddedLine:
    lineno: int                  # 补丁后文件里的物理行号
    text: str

    @property
    def non_executable(self) -> bool:
        """文本层面就能看出不可能是可执行语句的行。

        这只是**充分**判据：判为 True 的一定非执行；判为 False 的仍要以
        coverage 的 executable statements 为准（比如 docstring 续行看着像代码）。
        """
        s = self.text.strip()
        return (not s) or s.startswith("#") or bool(_BRACKET_ONLY.match(s))


@dataclass
class FileDiff:
    path: str                    # 补丁后的路径
    new_file: bool = False
    deleted_file: bool = False
    renamed_from: str | None = None
    binary: bool = False
    added: list[AddedLine] = field(default_factory=list)
    removed_count: int = 0

    @property
    def added_linenos(self) -> list[int]:
        return [a.lineno for a in self.added]

    @property
    def added_count(self) -> int:
        return len(self.added)


def parse(diff_text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    newline = 0

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = FileDiff(path=m.group(2) if m else line.split()[-1][2:])
            files.append(cur)
            newline = 0
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.new_file = True
            continue
        if line.startswith("deleted file mode"):
            cur.deleted_file = True
            continue
        if line.startswith("rename from "):
            cur.renamed_from = line[len("rename from "):].strip()
            continue
        if line.startswith("rename to "):
            cur.path = line[len("rename to "):].strip()
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            cur.binary = True
            continue
        m = _HUNK.match(line)
        if m:
            newline = int(m.group(3))
            continue
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            cur.added.append(AddedLine(newline, line[1:]))
            newline += 1
        elif line.startswith("-"):
            cur.removed_count += 1
        elif line.startswith("\\"):
            continue                       # "\ No newline at end of file"
        else:
            newline += 1                   # 上下文行（含空串表示的空上下文行）

    return files


def added_by_file(diff_text: str) -> dict[str, list[AddedLine]]:
    """{路径: [新增行]}，跳过被删除的文件和二进制文件。"""
    return {f.path: f.added for f in parse(diff_text)
            if f.added and not f.deleted_file and not f.binary}


def binary_files(diff_text: str) -> list[str]:
    return [f.path for f in parse(diff_text) if f.binary]


def new_files(diff_text: str) -> set[str]:
    return {f.path for f in parse(diff_text) if f.new_file}
