"""Generate a self-contained demo repository with an uncommitted patch.

The fixture uses a real local repository path and a declared pytest file.

```text
一个 Git 仓库 + 未提交的改动 + 一句 pytest 文件路径
```

补丁里刻意安排三态（提交版不展示惰性）：

- `backoff.py` 新增 `jittered_delay`，被 `test_jitter_within_bounds` 直接覆盖，
  删掉就红                                                    → **承重**
- `backoff.py` 新增 `retry_after` 的 429 分支，测试从没走到      → **无据**
- 仓库根新增 `debug_probe.py`，没人 import、pytest 收集不到      → **游离**
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "retry_demo"

BASE = {
    # 真实 Python 项目都会忽略这些，而这个 demo 本来就是给人跑 pytest 的：
    # 少了 .gitignore，谁跑一次测试，__pycache__ 和 .pytest_cache 就成了
    # 未跟踪文件，按"未跟踪文件必须进补丁"的规则被扫进补丁，
    # 补丁里出现 .pyc 二进制后整次审查在打补丁阶段就失败。
    ".gitignore": "__pycache__/\n*.py[cod]\n.pytest_cache/\n",
    "retrylib/__init__.py": '"""一个很小的重试工具库。"""\n',
    "retrylib/backoff.py": '''"""退避策略。"""

BASE_DELAY = 1.0
MAX_DELAY = 60.0


def exponential_delay(attempt):
    """第 attempt 次重试等多久（秒）。"""
    if attempt < 0:
        raise ValueError("attempt 不能为负")
    return min(BASE_DELAY * (2 ** attempt), MAX_DELAY)


def should_retry(status_code):
    """哪些 HTTP 状态码值得重试。"""
    return status_code in (500, 502, 503, 504)
''',
    "tests/test_backoff.py": '''from retrylib.backoff import exponential_delay, should_retry


def test_exponential_delay_grows():
    assert exponential_delay(0) == 1.0
    assert exponential_delay(3) == 8.0


def test_exponential_delay_capped():
    assert exponential_delay(20) == 60.0


def test_should_retry_only_5xx():
    assert should_retry(503) is True
    assert should_retry(404) is False
''',
}

# ---- "AI 补丁"：改写 backoff.py、加测试、丢一个调试脚本
PATCHED_BACKOFF = '''"""退避策略。"""

import random

BASE_DELAY = 1.0
MAX_DELAY = 60.0
JITTER_RATIO = 0.25


def exponential_delay(attempt):
    """第 attempt 次重试等多久（秒）。"""
    if attempt < 0:
        raise ValueError("attempt 不能为负")
    return min(BASE_DELAY * (2 ** attempt), MAX_DELAY)


def jittered_delay(attempt, rng=None):
    """加抖动，避免大量客户端同时重试造成惊群。"""
    rng = rng or random
    base = exponential_delay(attempt)
    spread = base * JITTER_RATIO
    return base - spread + rng.random() * (2 * spread)


def should_retry(status_code):
    """哪些 HTTP 状态码值得重试。"""
    return status_code in (500, 502, 503, 504)


def retry_after(status_code, headers=None):
    """服务端让我们等多久。429 分支是新加的，测试没有覆盖到。"""
    headers = headers or {}
    if status_code == 429:
        raw = headers.get("Retry-After")
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                return BASE_DELAY
        return BASE_DELAY
    return 0.0
'''

PATCHED_TEST = '''from retrylib.backoff import (exponential_delay, jittered_delay,
                              should_retry)


class _FixedRng:
    def random(self):
        return 0.5


def test_exponential_delay_grows():
    assert exponential_delay(0) == 1.0
    assert exponential_delay(3) == 8.0


def test_exponential_delay_capped():
    assert exponential_delay(20) == 60.0


def test_should_retry_only_5xx():
    assert should_retry(503) is True
    assert should_retry(404) is False


def test_jitter_within_bounds():
    d = jittered_delay(3, rng=_FixedRng())
    assert 6.0 <= d <= 10.0
'''

DEBUG_PROBE = '''"""临时调试脚本：手工看看退避曲线。没有人 import 它。"""
from retrylib.backoff import exponential_delay


def main():
    for i in range(8):
        print(i, exponential_delay(i))


if __name__ == "__main__":
    main()
'''


def _git(args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args[:2])} 失败：{r.stderr[-300:]}")
    return r.stdout


def build(root: Path = DEMO) -> Path:
    """建仓库 → 提交基线 → 把 AI 改动留在工作树里（**不提交**）。"""
    import shutil
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    for rel, text in BASE.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")

    _git(["init", "-q", "-b", "main"], root)
    _git(["add", "-A"], root)
    _git(["-c", "user.email=demo@local", "-c", "user.name=demo",
          "commit", "-q", "-m", "退避策略的初始实现"], root)

    # AI 的改动：留在工作树，不提交——这就是用户 review 时的真实状态
    (root / "retrylib" / "backoff.py").write_text(PATCHED_BACKOFF, encoding="utf-8")
    (root / "tests" / "test_backoff.py").write_text(PATCHED_TEST, encoding="utf-8")
    (root / "debug_probe.py").write_text(DEBUG_PROBE, encoding="utf-8")
    return root


if __name__ == "__main__":
    out = build()
    print(f"demo 仓库已建：{out}")
    print(f"  基线 commit：{_git(['rev-parse', '--short', 'HEAD'], out).strip()}")
    print(f"  未提交改动：\n{_git(['status', '--short'], out)}")
