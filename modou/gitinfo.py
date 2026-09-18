"""工具自身的版本信息。

为什么单独一个模块：`RunManifest.tool_commit` 原来在 `run_one.py` 里写死成 `""`，
**光执行 `git init` 不会改变任何证书**——必须真的去读。

有未提交修改时记 `<commit>+dirty`。这一点不能省：
一个 dirty 的工作树跑出来的结果，和那个 commit 并不对应，
把它记成干净的 commit 等于伪造可复现性。
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from . import paths


def _git(args: list[str], cwd: Path) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=30)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


@lru_cache(maxsize=4)
def _cached_tool_commit(root: Path) -> str:
    rc, head = _git(["rev-parse", "HEAD"], root)
    if rc != 0 or not head:
        return ""
    rc, status = _git(["status", "--porcelain"], root)
    if rc != 0:
        return head
    return f"{head}+dirty" if status else head


def current_tool_commit(root: Path | None = None, *, refresh: bool = False) -> str:
    """返回 `<commit>`、`<commit>+dirty`，或非 git 环境下的空串。

    空串是诚实的"不知道"——调用方据此改用 `tools/freeze.py` 的源码 tar SHA-256 兜底。

    **校验路径一律传 `refresh=True`。** 缓存是为了让一次跑数里的几十次调用不去
    反复 fork git，但工作树在同一个进程里是会变脏的（比如刚写完一份产物），
    一个返回过期状态的"当前 commit"会让 dirty 校验形同虚设。
    """
    root = root or paths.PROJECT
    if refresh:
        _cached_tool_commit.cache_clear()
    return _cached_tool_commit(root)


def is_dirty(commit: str) -> bool:
    return commit.endswith("+dirty")
