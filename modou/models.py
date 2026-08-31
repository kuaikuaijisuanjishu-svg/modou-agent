"""墨斗的数据模型。

三个核心对象：
- TestVector：已声明测试范围的逐项状态向量。判定"有没有变化"比的是它，不是退出码。
- EvidenceUnit：删除实验的**最小结论对象**。不是行——多行可以共用一张证书。
- LineResult：每条新增物理行恰好一个最终结果。

措辞纪律写在类型里：承重的 statement 只能说"该行属于一个最小已隔离单元；
移除该单元后，指定测试发生回归"，不能说"已证明这一行单独不可删除"。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------- 测试状态

class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSING = "missing"          # 声明了但本次没收集到／没跑到


#: 由 passed 变成这些里的任何一个，都算回归
REGRESSION_TARGETS = frozenset({
    TestStatus.FAILED, TestStatus.ERROR, TestStatus.SKIPPED, TestStatus.MISSING,
})


@dataclass(frozen=True)
class TestVector:
    """已声明测试范围的逐项状态。比较必须逐项，不允许用退出码代替。"""
    statuses: tuple[tuple[str, TestStatus], ...]

    @classmethod
    def of(cls, mapping: dict[str, TestStatus]) -> "TestVector":
        return cls(tuple(sorted((k, TestStatus(v)) for k, v in mapping.items())))

    def as_dict(self) -> dict[str, TestStatus]:
        return dict(self.statuses)

    def identical_to(self, other: "TestVector") -> bool:
        return self.statuses == other.statuses

    def regressions(self, baseline: "TestVector") -> list[tuple[str, TestStatus, TestStatus]]:
        """相对基线发生回归的测试项：(test_id, 基线状态, 现在状态)。"""
        base = baseline.as_dict()
        now = self.as_dict()
        out = []
        for tid, was in base.items():
            is_now = now.get(tid, TestStatus.MISSING)
            if was is TestStatus.PASSED and is_now in REGRESSION_TARGETS:
                out.append((tid, was, is_now))
        return out

    @property
    def all_passed(self) -> bool:
        return all(s is TestStatus.PASSED for _, s in self.statuses)

    def __len__(self) -> int:
        return len(self.statuses)


# ---------------------------------------------------------------- 四态与未标注

class Label(str, Enum):
    DRIFT = "游离"
    LOAD_BEARING = "承重"
    INERT = "惰性"
    UNEVIDENCED = "无据"
    UNLABELED = "未标注"


#: 四态 —— H1 的分子只数这四个
LABELED = frozenset({Label.DRIFT, Label.LOAD_BEARING, Label.INERT, Label.UNEVIDENCED})


class Unlabeled(str, Enum):
    """未标注的机器可读原因。每一个都必须能在报告里单独计数。"""
    NON_EXECUTABLE = "non_executable"              # 空行/注释/纯括号，且未继承证据单元
    NOT_MEASURED = "not_measured"                  # 该文件根本没被覆盖率测量到（多半没人 import）
    BUDGET_EXHAUSTED = "budget_exhausted"          # 300 秒预算用尽
    NO_VALID_TRANSFORM = "no_valid_transform"      # 无法形成语法合法的删除
    NOT_ISOLATED = "not_isolated"                  # 下钻到底仍未能单独归因
    FLAKY_OR_DIRTY_RESTORE = "flaky_or_dirty_restore"   # 回滚后向量对不回基线
    UNSUPPORTED_FILE = "unsupported_file"          # 测试/配置/文档/二进制等不做探测的文件
    PROBE_TIMEOUT = "probe_timeout"                # 单次探测超时
    INERT_WITHHELD = "inert_withheld"              # 探测判为惰性，但四态版未通过 H3 护栏，不对外呈现


@dataclass
class LineResult:
    """一条 AI 补丁新增物理行的最终结果。"""
    path: str
    lineno: int
    label: Label = Label.UNLABELED
    reason: Optional[Unlabeled] = None
    unit_id: Optional[str] = None
    executable: bool = True                        # coverage 认为这行可执行吗

    def __post_init__(self):
        if self.label is Label.UNLABELED and self.reason is None:
            raise ValueError(f"{self.path}:{self.lineno} 未标注必须带原因")
        if self.label is not Label.UNLABELED and self.reason is not None:
            raise ValueError(f"{self.path}:{self.lineno} 已标注不应带未标注原因")


# ---------------------------------------------------------------- 证据单元

@dataclass
class Transform:
    """实际做了什么变换。保行号：删除的行变成空行，不是真的移走。"""
    deleted_lines: tuple[int, ...]
    pass_inserted_at: Optional[int] = None         # 因父块被清空而补 pass 的行号
    file_removed: bool = False                     # 游离引擎的整文件临时移除

    def describe(self) -> str:
        if self.file_removed:
            return "临时移除整个文件"
        s = f"删除 {len(self.deleted_lines)} 行（置空，保持行号）"
        if self.pass_inserted_at is not None:
            s += f"；父代码块被清空，在第 {self.pass_inserted_at} 行补 pass"
        return s


@dataclass
class EvidenceUnit:
    """删除实验的最小结论对象。多条行可以共用一个 unit_id。"""
    unit_id: str
    path: str
    line_start: int
    line_end: int
    node_type: str                                  # ast 节点类型，或 "file" / "line"
    transform: Transform
    baseline: TestVector
    mutated: Optional[TestVector] = None
    restored: Optional[TestVector] = None
    covered_lines: tuple[int, ...] = ()
    seconds: float = 0.0
    verdict: Optional[Label] = None
    note: str = ""
    #: Ledger references for the related observation and fact records.
    #: 主张的 provenance 指向的是真实跑过的那次实验，而不是事后反推的结论。
    experiment_id: str = ""
    fact_ids: tuple[str, ...] = ()
    #: 这个单元在账本里的 Anchor（原始 JSON，避免 models 依赖 ledger）。
    #: **不能让下游去重建**——重建一次就多一处会漂移的实现，
    #: 而 Anchor 一漂移，主张与它的证据就对不上了。
    anchor_json: dict = field(default_factory=dict)

    @property
    def span(self) -> range:
        return range(self.line_start, self.line_end + 1)

    def regressions(self) -> list[tuple[str, TestStatus, TestStatus]]:
        return self.mutated.regressions(self.baseline) if self.mutated else []

    def restore_is_clean(self) -> bool:
        """回滚之后向量必须重新与基线逐项相同，否则这次实验作废。"""
        return self.restored is not None and self.restored.identical_to(self.baseline)

    def statement(self) -> str:
        """证书「主张」栏。措辞受纪律约束。"""
        where = f"{self.path}:{self.line_start}" + (
            f"-{self.line_end}" if self.line_end != self.line_start else "")
        if self.verdict is Label.LOAD_BEARING:
            rs = self.regressions()
            names = "、".join(t for t, _, _ in rs[:3]) or "（未记录）"
            more = f" 等 {len(rs)} 项" if len(rs) > 3 else ""
            return (f"{where} 属于一个最小已隔离单元；移除该单元后，"
                    f"指定测试发生回归：{names}{more}")
        if self.verdict is Label.INERT:
            return (f"{where} 所在单元被测试执行过；移除该单元后，"
                    f"已声明测试范围内 {len(self.baseline)} 个测试项状态逐项相同")
        if self.verdict is Label.DRIFT:
            return (f"{self.path} 由本补丁新建，不被已声明测试范围收集、"
                    f"无静态引用，临时移除后测试状态向量逐项相同")
        return f"{where} 未得出结论"


def make_unit_id(path: str, lines: tuple[int, ...], kind: str) -> str:
    h = hashlib.sha256(f"{path}|{kind}|{','.join(map(str, lines))}".encode()).hexdigest()
    return h[:12]


# ---------------------------------------------------------------- 运行清单

@dataclass
class RunManifest:
    """一次实例运行的完整来源记录，写在工作树之外。"""
    instance_id: str
    repo: str
    base_commit: str
    scaffold: str
    patch_sha256: str
    test_patch_sha256: str
    tool_commit: str
    adapter_version: str
    declared_tests: tuple[str, ...] = ()
    contaminated: bool = False
    excluded_reason: Optional[str] = None
    #: 这次运行由哪份冻结定义。空串 = 未绑定任何冻结（临时单跑）。
    freeze_sha256: str = ""
    #: 正式产物必须是 True。--unofficial 时写 False 并给出原因——
    #: 只改终端标题和文件名是不够的，读证书的人看不到那些。
    official: bool = False
    unofficial_reason: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        d["declared_tests"] = list(self.declared_tests)
        return d


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
