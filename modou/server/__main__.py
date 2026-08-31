"""CLI entry point: python -m modou.server --allow-repo ..."""
from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

import uvicorn

from modou.agent.provider import OpenAICompatibleProvider, ProviderUnavailable

from .app import create_app
from .control import RepoRegistry, ReviewManager


def main() -> int:
    parser = argparse.ArgumentParser(description="墨斗本地 Review Cockpit")
    parser.add_argument("--allow-repo", action="append", required=True, type=Path,
                        help="明确允许执行测试的 Git 仓库（可重复）")
    parser.add_argument("--repo-python", action="append", default=[],
                        metavar="REPO=PYTHON",
                        help="为登记仓库绑定其测试解释器；浏览器不能覆盖")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reviews-root", type=Path,
                        default=Path.home() / ".modou" / "reviews")
    parser.add_argument("--preset-config", type=Path,
                        help="评委模式案例 JSON；仅接受已授权仓库名和仓库内相对测试路径")
    parser.add_argument("--agent-level", choices=["l1", "l2"], default="l1",
                        help="服务器绑定的 Agent 能力级别；浏览器不能覆盖")
    parser.add_argument("--execution-mode", choices=["trusted_local", "sandboxed"],
                        default="trusted_local",
                        help="服务器绑定的测试子进程执行模式；浏览器不能覆盖")
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)  # 256 bits before URL-safe encoding
    try:
        provider = OpenAICompatibleProvider.from_env()
    except ProviderUnavailable:
        provider = None
    python_by_repo = {}
    for binding in args.repo_python:
        if "=" not in binding:
            parser.error("--repo-python 必须是 REPO=PYTHON")
        repo_text, python_text = binding.split("=", 1)
        python_by_repo[Path(repo_text)] = Path(python_text)
    presets = []
    if args.preset_config:
        try:
            presets = json.loads(args.preset_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"--preset-config 无法读取：{exc}")
        if not isinstance(presets, list):
            parser.error("--preset-config 顶层必须是数组")
    registry = RepoRegistry(args.allow_repo, python_by_repo=python_by_repo,
                            presets=presets)
    manager = ReviewManager(registry, root=args.reviews_root, provider=provider,
                            agent_level=args.agent_level,
                            execution_mode=args.execution_mode)
    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    app = create_app(manager=manager, token=token, host=args.host,
                     port=args.port, web_dist=web_dist)
    # flush 是必需的：stdout 被重定向（`> log`、nohup、后台跑）时不是 tty，
    # Python 会缓冲；而 uvicorn.run() 之后就再也不返回了，于是这三行——
    # 包括**唯一一次打印的 token**——永远进不了日志文件。
    print(f"执行模式：{args.execution_mode}（服务器绑定）", flush=True)
    print(f"Agent level：{args.agent_level.upper()}（服务器绑定）", flush=True)
    print(f"打开：http://{args.host}:{args.port}/#token={token}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
