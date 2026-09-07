"""Agent Control Plane：模型在这里决定**做哪个实验**，不在这里决定**结论是什么**。

这条边界是 ADR-002，也是整个包存在的理由。本包里的代码可以调用工具、
排优先级、决定停止、引用证据解释；**不能**写 verdict、不能改测试状态、
不能构造没有 provenance 的主张。事实裁决全部在 `modou/`（Evidence Plane）
与 `modou/ledger/`（证据账本）里，那两处不 import 本包。

依赖方向是单向的：`agent` → `ledger` → `models`。反过来 import 就说明
边界破了。
"""
