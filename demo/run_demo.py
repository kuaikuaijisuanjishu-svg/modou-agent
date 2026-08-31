"""Run the self-contained public demonstration."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modou import paths                                            # noqa: E402
from modou.application import (AnalysisRequest, ExecutionMode,     # noqa: E402
                               analyze_patch, load_run_bundle,
                               stream_events)

DEMO = Path(__file__).resolve().parent / "retry_demo"


def _python() -> str:
    """Return the interpreter used for the fixture tests."""
    cand = paths.WORK / ".venv39" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


def main() -> int:
    if not DEMO.exists():
        from demo.build_demo import build
        build()

    req = AnalysisRequest(
        repo_path=DEMO,                            # ← 只要一个仓库路径
        test_files=("tests/test_backoff.py",),
        python=_python(),
        budget_seconds=120.0,
        mode=ExecutionMode.TRUSTED_LOCAL,
        goal="确认新增的退避逻辑哪些有测试证据",
        quiet=False,
    )
    print(f"执行模式：{req.mode.label}\n")
    handle = analyze_patch(req)

    print("\n── 事件流 ──")
    for ev in stream_events(handle):
        data = dict(ev.data)
        if "report" in data:
            data["report"] = "<本地运行目录>"
        print(f"  {ev.kind:<20} {data}")

    if not handle.ok:
        print(f"\n失败关闭：[{handle.failure_stage}] {handle.failure_detail}")
        return 1

    print(f"\n── 逐行分布 ──\n  {handle.summary['by_label']}")
    bundle = load_run_bundle(str(handle.run_dir))
    print(f"  bundle 校验通过，账本 {len(bundle.ledger_rows)} 条记录")
    return 0


if __name__ == "__main__":
    sys.exit(main())
