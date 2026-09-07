"""四态 fixture 的接入。

真实实例受环境、网络、仓库版本影响，不适合当确定性验收，也不适合录屏——
一次网络抖动就能让演示当场翻车。fixture 是自足的：`git init` 出来的小仓库，
几秒跑完，四态必然齐现。

它不进入任何指标，只做两件事：回归验收、稳定录屏。
"""
from __future__ import annotations

import json
from pathlib import Path

from .adapters import ADAPTERS, RepoAdapter

FIXTURE_REPO = "fixture/four_state"

ADAPTERS[FIXTURE_REPO] = RepoAdapter(
    repo=FIXTURE_REPO, clone_dir="four_state_repo", package_root=".",
    venv=".venv39")

DECLARED = ["tests/test_calc.py::test_add", "tests/test_calc.py::test_describe"]


def meta_for(root: Path) -> dict:
    """构造一份与 SWE-bench 同形的元数据。"""
    import subprocess
    base = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=str(root),
                          capture_output=True, text=True).stdout.strip()
    return {
        "instance_id": "fixture__four_state",
        "repo": FIXTURE_REPO,
        "base_commit": base,
        "test_patch": "",
        "FAIL_TO_PASS": json.dumps(DECLARED[:1]),
        "PASS_TO_PASS": json.dumps(DECLARED[1:]),
        "version": "0",
    }


def patch_for(root: Path) -> str:
    return (root / "ai_patch.diff").read_text()


class FixtureAdapter(RepoAdapter):
    pass


def coverage_source_override() -> str:
    """fixture 的"包"就是仓库根，测试在 tests/。"""
    return "."
