"""补丁证据账本（SPEC v2.2）。

分层：**事实 → 实验 → 主张 → 投影**。标签不再是核心，是投影。

M1 的状态：账本与旧引擎**并存**，旧引擎照常出结论，账本镜像写入，
`projection_parity` 每次运行都跑，作为持续闸门。
`legacy.py` 里的脚手架会在 M5 随旧的直写路径一起删除。
"""
from . import (anchors, claim_builder, derive, legacy, observe, parity, records,  # noqa: F401
               store, validate)

__all__ = ["anchors", "claim_builder", "derive", "legacy", "observe", "parity",
           "records", "store", "validate"]
