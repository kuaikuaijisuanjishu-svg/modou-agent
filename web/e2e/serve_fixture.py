#!/usr/bin/env python3
"""Serve the public app against a throwaway repository for browser checks."""
from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
TOKEN = "public-flow-test-token"
PORT = 8788

BASE = {
    "pkg/__init__.py": "",
    "pkg/calc.py": "def total(items):\n    return sum(items)\n",
    "tests/test_calc.py": (
        "from pkg.calc import total\n\n\n"
        "def test_total():\n    assert total([1, 2]) == 3\n"),
    ".gitignore": "__pycache__/\n*.py[cod]\n.pytest_cache/\n",
}
PATCHED_CALC = (
    "def total(items):\n    return sum(items)\n\n\n"
    "def scaled(items, factor):\n    return total(items) * factor\n\n\n"
    "def unreached(items):\n    return max(items)\n")
PATCHED_TEST = (
    "from pkg.calc import total, scaled\n\n\n"
    "def test_total():\n    assert total([1, 2]) == 3\n\n\n"
    "def test_scaled():\n    assert scaled([1, 2], 3) == 9\n")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_repo(root: Path) -> Path:
    repo = root / "public-fixture"
    for name, text in BASE.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=public-flow", "-c",
         "user.email=public-flow@example.invalid", "commit", "-qm", "base")
    (repo / "pkg" / "calc.py").write_text(PATCHED_CALC, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(PATCHED_TEST, encoding="utf-8")
    return repo


def main() -> int:
    import uvicorn
    from modou.server import RepoRegistry, ReviewManager, create_app

    workdir = Path(tempfile.mkdtemp(prefix="shuimu-public-flow-"))
    atexit.register(shutil.rmtree, workdir, True)
    repo = build_repo(workdir)
    web_dist = ROOT / "web" / "dist"
    if not (web_dist / "index.html").exists():
        print("web/dist 不存在，请先运行 npm run build", file=sys.stderr)
        return 2

    registry = RepoRegistry([repo], python_by_repo={repo: Path(sys.executable)},
                            presets=[{
                                "preset_id": "public-flow",
                                "display_name": "公开真实案例",
                                "description": "真实服务、可恢复实验、公开 fixture",
                                "repo_name": repo.name,
                                "test_files": ["tests/test_calc.py"],
                                "goal": "确认新增代码是否由声明测试约束",
                                "budget_seconds": 120,
                                "model_provider": "deterministic",
                            }])
    manager = ReviewManager(registry, root=workdir / "reviews")
    app = create_app(manager=manager, token=TOKEN, host="127.0.0.1", port=PORT,
                     web_dist=web_dist)
    print(f"public flow ready: http://127.0.0.1:{PORT}/#token={TOKEN}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, access_log=False,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
