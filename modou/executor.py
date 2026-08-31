"""统一的仓库代码子进程执行边界。

控制面仍负责工作树干预与恢复；所有会 import/执行仓库代码的命令只通过
``run_argv`` 进入这里。执行模式用 contextvar 绑定到一次 Review 线程，避免
把模式参数扩散到 Evidence Plane 的每一层。
"""
from __future__ import annotations

import contextvars
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol


class ExecutorUnavailable(RuntimeError):
    """请求的执行边界无法初始化；必须失败关闭。"""


class CommandExecutor(Protocol):
    mode: str

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess: ...


BASE_ENV_ALLOW = frozenset({
    # 启动解释器与基础本地化所需；不继承应用、云厂商、Git 或模型配置。
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
    "TERM", "COLORTERM", "NO_COLOR", "USER",
})

OVERRIDE_ENV_ALLOW = frozenset({
    # 只有测试/coverage 产物重定向和沙箱运行目录可以由调用方覆盖。
    "HOME", "TMPDIR", "MODOU_SCRATCH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONWARNINGS", "PYTEST_ADDOPTS", "COVERAGE_FILE", "COVERAGE_RCFILE",
    # src 布局的仓库（src/pkg/…）不把自己放在 sys.path 上，于是测试导入的是
    # **环境里装着的那份**，而不是工作树里正被审查的那份——结论会安静地指向
    # 错误的代码。SymPy 与 Sphinx 是扁平布局，所以这个缺陷至今没有暴露。
    # 它进的是「可覆盖」而不是「可继承」：调用方必须显式声明一个源码根，
    # 父进程环境里的 PYTHONPATH 依然进不来。
    "PYTHONPATH",
})


def sanitized_environment(overrides: dict | None = None) -> dict[str, str]:
    """构造被测子进程环境；从零白名单，不从父进程黑名单删减。"""
    e = {key: value for key, value in os.environ.items()
         if key in BASE_ENV_ALLOW}
    e["PYTHONWARNINGS"] = "ignore::UserWarning,ignore::SyntaxWarning,ignore::DeprecationWarning"
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    if overrides:
        normalized = {str(k): str(v) for k, v in overrides.items()}
        forbidden = sorted(set(normalized) - OVERRIDE_ENV_ALLOW)
        if forbidden:
            raise ValueError(
                "子进程环境覆盖项不在白名单：" + ", ".join(forbidden))
        e.update(normalized)
    return e


class TrustedLocalExecutor:
    mode = "trusted_local"

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=timeout)


class SandboxedExecutor:
    """macOS Seatbelt 子进程执行器；仓库只读，产物只写 scratch。"""

    mode = "sandboxed"

    def __init__(self, scratch: Path):
        from tools.sandbox import launch

        if not launch.available():
            raise ExecutorUnavailable("sandbox-exec unavailable")
        self.scratch = Path(scratch).expanduser().resolve()
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = self.scratch / "tmp"
        self.home = self.scratch / "home"
        self.tmp.mkdir(exist_ok=True)
        self.home.mkdir(exist_ok=True)
        self.profile = self.scratch / "seatbelt.sb"
        # 区分“被测命令失败”和“沙箱本身起不来”：正式运行前先做初始化探针。
        probe = launch.execute(
            [sys.executable, "-c", "pass"], scratch=self.scratch,
            profile_path=self.profile, cwd=self.scratch,
            credential_home=Path.home(), process_home=self.home,
            timeout=10, env={})
        if probe.returncode != 0:
            raise ExecutorUnavailable(
                f"sandbox initialization failed rc={probe.returncode}: "
                f"{(probe.stderr or '')[-200:]}")

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess:
        from tools.sandbox import launch

        child_env = dict(env)
        child_env.update({
            "HOME": str(self.home),
            "TMPDIR": str(self.tmp),
            "MODOU_SCRATCH": str(self.scratch),
            # pytest cache、basetemp 不能落入只读仓库。
            "PYTEST_ADDOPTS": (
                f"--basetemp={self.tmp / 'pytest'} "
                f"-o cache_dir={self.scratch / 'pytest-cache'}"),
        })
        return launch.execute(
            argv, scratch=self.scratch, profile_path=self.profile, cwd=cwd,
            credential_home=Path.home(), process_home=self.home,
            timeout=timeout, env=child_env)


_CURRENT: contextvars.ContextVar[CommandExecutor] = contextvars.ContextVar(
    "modou_command_executor", default=TrustedLocalExecutor())


def current_executor() -> CommandExecutor:
    return _CURRENT.get()


@contextmanager
def execution_scope(executor: CommandExecutor) -> Iterator[None]:
    token = _CURRENT.set(executor)
    try:
        yield
    finally:
        _CURRENT.reset(token)
