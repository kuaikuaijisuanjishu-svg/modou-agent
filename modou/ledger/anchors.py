"""Evidence anchors attach observations to stable source objects.
强行让每一行恰好拿一个标签，会让注释行只能"继承"结论（继承是编造，不是观测），
让同一张证书按行数重复计权（一个大文件就能绑架统计），
并且把多种证据压成一个最终标签。

但**逐行覆盖率确实是逐行事实**，它得有地方挂。所以拆成两类：

- `Anchor`：证据与主张挂这里（文件 / hunk / AST 单元 / 符号）；
- `SourceLocation`：逐行事实挂这里。

投影层再通过 Anchor 的 span 与 SourceLocation 做 join。
这样既不回到"行是唯一主对象"，又允许逐行 coverage 作为真实事实存在。

**身份必须稳定。** `HunkAnchor` 不能只有序号——补丁一变，同一序号指向别的东西；
`UnitAnchor` 不能只靠行号——行号在快照之间不可比。所以两者都带内容摘要，
所有 Anchor 与 SourceLocation 都带 `snapshot_id`。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from typing import Optional


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:16]


@dataclass(frozen=True)
class Anchor:
    """证据与主张的挂载点。`aid` 是内容寻址的稳定身份。"""
    kind: str                      # file | hunk | unit | symbol
    snapshot_id: str               # S0 / S1 / S2 / SH:<...>——同路径在不同快照不是同一对象
    path: str
    #: AST 单元用（结构路径，如 "FunctionDef:foo/If"）；符号用 qualname
    structural_path: str = ""
    #: 内容摘要。hunk 用 hunk 正文，unit 用被覆盖的源码
    content_digest: str = ""
    line_start: int = 0
    line_end: int = 0
    hunk_idx: Optional[int] = None
    patch_sha256: str = ""

    @property
    def aid(self) -> str:
        """身份 = 种类 + 快照 + 路径 + 结构位置 + 内容摘要。

        **行号刻意不进身份。** 行号是位置元数据：在文件顶部插一行注释，
        同一个 AST 单元的结构位置和源码都没变，它就该还是同一个对象。
        把行号算进去会让"身份稳定"这个词失去意义——而身份稳定正是
        Anchors keep identity stable when line positions change.
        """
        parts = [self.kind, self.snapshot_id, self.path, self.structural_path,
                 self.content_digest, str(self.hunk_idx), self.patch_sha256]
        h = hashlib.sha256()
        for p in parts:                       # 长度前缀，避免边界歧义
            h.update(f"{len(p)}\x00{p}".encode())
        return h.hexdigest()[:16]

    @property
    def span(self) -> range:
        return range(self.line_start, self.line_end + 1)

    def to_json(self) -> dict:
        d = asdict(self)
        d["aid"] = self.aid
        return d


@dataclass(frozen=True)
class SourceLocation:
    """逐行事实的挂载点。**不承载主张**，只承载观测。"""
    snapshot_id: str
    path: str
    lineno: int

    @property
    def lid(self) -> str:
        return f"{self.snapshot_id}:{self.path}:{self.lineno}"

    def to_json(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "path": self.path,
                "lineno": self.lineno, "lid": self.lid}


# ---------------------------------------------------------------- 构造器

def file_anchor(snapshot_id: str, path: str, *, content: str = "",
                line_end: int = 0) -> Anchor:
    return Anchor(kind="file", snapshot_id=snapshot_id, path=path,
                  content_digest=digest(content) if content else "",
                  line_start=1, line_end=line_end or 1)


def unit_anchor(snapshot_id: str, path: str, *, structural_path: str,
                source: str, line_start: int, line_end: int) -> Anchor:
    return Anchor(kind="unit", snapshot_id=snapshot_id, path=path,
                  structural_path=structural_path, content_digest=digest(source),
                  line_start=line_start, line_end=line_end)


def hunk_anchor(snapshot_id: str, path: str, *, hunk_idx: int, body: str,
                patch_sha256: str, line_start: int = 0, line_end: int = 0) -> Anchor:
    return Anchor(kind="hunk", snapshot_id=snapshot_id, path=path,
                  content_digest=digest(body), hunk_idx=hunk_idx,
                  patch_sha256=patch_sha256,
                  line_start=line_start, line_end=line_end)


def snapshot_anchor(snapshot_id: str) -> Anchor:
    """run 级事实的挂载点（如整次运行的测试收集清单）。

    它不属于任何文件，但仍然必须有身份——否则 provenance 无处可指。
    """
    return Anchor(kind="snapshot", snapshot_id=snapshot_id, path="")


def symbol_anchor(snapshot_id: str, path: str, *, qualname: str) -> Anchor:
    return Anchor(kind="symbol", snapshot_id=snapshot_id, path=path,
                  structural_path=qualname)


def from_json(d: dict) -> Anchor:
    """从落盘的 JSON 还原 Anchor。字段名与 `to_json()` 对称。"""
    return Anchor(
        kind=d["kind"], snapshot_id=d["snapshot_id"], path=d["path"],
        structural_path=d.get("structural_path", ""),
        content_digest=d.get("content_digest", ""),
        line_start=d.get("line_start", 0), line_end=d.get("line_end", 0),
        hunk_idx=d.get("hunk_idx"), patch_sha256=d.get("patch_sha256", ""))
