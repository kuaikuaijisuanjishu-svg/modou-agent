"""统一的仓库代码子进程执行边界。

控制面仍负责工作树干预与恢复；所有会 import/执行仓库代码的命令只通过
``run_argv`` 进入这里。执行模式用 contextvar 绑定到一次 Review 线程，避免
把模式参数扩散到 Evidence Plane 的每一层。
"""
from __future__ import annotations

import contextvars
import ctypes
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

try:  # resource is POSIX-only; the executor still imports on Windows.
    import resource
except ImportError:  # pragma: no cover - exercised only on Windows.
    resource = None  # type: ignore[assignment]


class ExecutorUnavailable(RuntimeError):
    """请求的执行边界无法初始化；必须失败关闭。"""


class ResourceLimitExceeded(RuntimeError):
    """被测进程超过一次执行允许的资源预算，执行器已终止它。"""

    def __init__(self, kind: str, limit: int, observed: int | None = None):
        self.kind = kind
        self.limit = int(limit)
        self.observed = None if observed is None else int(observed)
        suffix = f"（观测 {self.observed}）" if observed is not None else ""
        super().__init__(f"resource limit exceeded: {kind} > {limit}{suffix}")


@dataclass(frozen=True)
class ResourceLimits:
    """每次仓库子进程执行的资源上限。

    ``memory`` 和 ``processes`` 在 macOS 上不能可靠地通过 rlimit 施加，
    因而由父进程轮询进程树；``disk`` 和 ``files`` 采用相对执行前快照的
    新增量，避免把已有的大仓库误判为超限。所有值均为一次命令的上限。
    """

    cpu_seconds: int = 600
    max_memory_bytes: int = 512 << 20
    max_processes: int = 64
    max_disk_bytes: int = 512 << 20
    max_files: int = 20_000
    max_output_bytes: int = 8 << 20
    max_file_bytes: int = 256 << 20

    def __post_init__(self) -> None:
        for name in ("cpu_seconds", "max_memory_bytes", "max_processes",
                     "max_disk_bytes", "max_files", "max_output_bytes",
                     "max_file_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_RESOURCE_LIMITS = ResourceLimits()


def _process_identity(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
            text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return " ".join(result.stdout.split()) if result.returncode == 0 else ""


def _process_is_zombie(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
            text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith("Z")


def _write_process_record(path: Path | None, process: subprocess.Popen,
                          argv: list[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "schema_version": "executor-process-v1", "pid": process.pid,
        "process_group_id": process.pid,
        "process_identity": _process_identity(process.pid),
        "argv_sha256": hashlib.sha256(
            json.dumps(argv, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "created_at_epoch": time.time(),
    }
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _clear_process_record(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def recover_process_record(path: Path) -> str:
    """Terminate a verifiably matching orphan process group on startup.

    Returns ``none``, ``stale_removed``, ``terminated`` or ``uncertain``.
    An identity mismatch means the PID was reused and is never killed.
    """
    path = Path(path)
    if not path.exists():
        return "none"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != "executor-process-v1":
            return "uncertain"
        pid = int(raw["pid"])
        pgid = int(raw["process_group_id"])
        recorded_identity = str(raw["process_identity"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return "uncertain"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        _clear_process_record(path)
        return "stale_removed"
    except (PermissionError, OSError):
        return "uncertain"
    current_identity = _process_identity(pid)
    if not recorded_identity or not current_identity:
        return "uncertain"
    if current_identity != recorded_identity:
        _clear_process_record(path)
        return "stale_removed"
    try:
        if os.name == "nt":
            os.kill(pid, signal.SIGKILL)
        else:
            os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError):
        return "uncertain"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _process_is_zombie(pid):
            _clear_process_record(path)
            return "terminated"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            _clear_process_record(path)
            return "terminated"
        except OSError:
            _clear_process_record(path)
            return "terminated"
        time.sleep(.02)
    return "uncertain"


class CommandExecutor(Protocol):
    mode: str
    limits: ResourceLimits

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess: ...


def _kill_process_group(process: subprocess.Popen) -> None:
    """Stop a timed-out command and descendants, not only the direct child."""
    if os.name == "nt":
        process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


_LINUX_SUBREAPER_ENABLED = False


def _enable_linux_subreaper() -> None:
    """Adopt and reap descendants after a process-group kill in containers.

    A minimal container often runs the test process as PID 1.  Killing the
    timed-out parent otherwise leaves its children as unreaped zombies, for
    which ``os.kill(pid, 0)`` still succeeds.  Linux's subreaper facility lets
    this executor adopt those descendants and reap them deterministically.
    """
    global _LINUX_SUBREAPER_ENABLED
    if _LINUX_SUBREAPER_ENABLED or not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # PR_SET_CHILD_SUBREAPER = 36.
        if libc.prctl(36, 1, 0, 0, 0) == 0:
            _LINUX_SUBREAPER_ENABLED = True
    except (AttributeError, OSError):
        pass


def _reap_linux_orphans(timeout: float = 0.25) -> None:
    if not _LINUX_SUBREAPER_ENABLED:
        return
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid:
            continue
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def _resource_preexec(limits: ResourceLimits):
    """Build a POSIX pre-exec hook for limits the kernel actually supports."""
    if resource is None:
        return None

    def apply() -> None:
        # CPU and file-size limits are supported by both macOS and Linux.
        resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (limits.max_file_bytes, limits.max_file_bytes))
        # Linux containers can enforce address space. macOS reports success for
        # some variants but does not enforce it, so leave it to the RSS watcher.
        if sys.platform != "darwin" and hasattr(resource, "RLIMIT_AS"):
            try:
                resource.setrlimit(resource.RLIMIT_AS,
                                   (limits.max_memory_bytes, limits.max_memory_bytes))
            except (OSError, ValueError):
                pass

    return apply


_SKIP_TREE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__"})


def _iter_tree_files(roots: tuple[Path, ...]):
    """Yield regular files without following symlinks or vendor trees."""
    stack = [Path(root).resolve() for root in roots if Path(root).exists()]
    while stack:
        directory = stack.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIP_TREE_DIRS:
                            stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path), int(entry.stat(follow_symlinks=False).st_size)
                except OSError:
                    continue


def _tree_snapshot(roots: tuple[Path, ...]) -> dict[Path, int]:
    return {path: size for path, size in _iter_tree_files(roots)}


def _tree_delta(before: dict[Path, int], roots: tuple[Path, ...]) -> tuple[int, int]:
    if not roots:
        return 0, 0
    current = _tree_snapshot(roots)
    added_bytes = sum(max(0, size - before.get(path, 0)) for path, size in current.items())
    added_files = sum(1 for path in current if path not in before)
    return added_bytes, added_files


def _tree_usage(pid: int) -> tuple[int, int]:
    """Return RSS bytes and process count for a child process tree."""
    if os.name == "nt":
        return 0, 1
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,rss="],
            capture_output=True, text=True, check=False, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return 0, 1
    parents: dict[int, list[int]] = {}
    rss: dict[int, int] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            child, parent, resident_kb = map(int, fields)
        except ValueError:
            continue
        parents.setdefault(parent, []).append(child)
        rss[child] = max(0, resident_kb) * 1024
    seen = {int(pid)}
    queue = [int(pid)]
    while queue:
        parent = queue.pop()
        for child in parents.get(parent, ()):
            if child not in seen:
                seen.add(child)
                queue.append(child)
    return sum(rss.get(item, 0) for item in seen), len(seen)


def _run_with_process_group(argv: list[str], *, cwd: Path,
                            timeout: float,
                            env: dict[str, str],
                            limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
                            monitor_roots: tuple[Path, ...] = (),
                            process_record: Path | None = None) -> subprocess.CompletedProcess:
    """Run one command with killable process groups and resource monitoring."""
    _enable_linux_subreaper()
    before_tree = _tree_snapshot(monitor_roots)
    process = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=False, start_new_session=os.name != "nt",
        preexec_fn=_resource_preexec(limits) if os.name != "nt" else None)
    try:
        _write_process_record(process_record, process, argv)
    except Exception:
        _kill_process_group(process)
        process.wait(timeout=2)
        raise
    selector = selectors.DefaultSelector()
    if process.stdout is not None:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    if process.stderr is not None:
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    captured = 0
    started = time.monotonic()

    def terminate() -> None:
        _kill_process_group(process)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        _reap_linux_orphans()

    def finish_output() -> tuple[str, str]:
        # Pipes are drained after a kill so the child is never left as a zombie.
        for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
            if stream is None:
                continue
            try:
                data = stream.read() or b""
            except OSError:
                data = b""
            if data:
                chunks[name].append(data[:max(0, limits.max_output_bytes - sum(
                    len(part) for values in chunks.values() for part in values))])
        return tuple((b"".join(chunks[name])).decode("utf-8", errors="replace")
                     for name in ("stdout", "stderr"))  # type: ignore[return-value]

    try:
        while selector.get_map() or process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                terminate()
                stdout, stderr = finish_output()
                raise subprocess.TimeoutExpired(process.args, timeout,
                                                output=stdout, stderr=stderr)
            usage = _tree_usage(process.pid)
            if usage[0] > limits.max_memory_bytes:
                terminate(); finish_output()
                raise ResourceLimitExceeded("memory_bytes", limits.max_memory_bytes, usage[0])
            if usage[1] > limits.max_processes:
                terminate(); finish_output()
                raise ResourceLimitExceeded("processes", limits.max_processes, usage[1])
            delta_bytes, delta_files = _tree_delta(before_tree, monitor_roots)
            if delta_bytes > limits.max_disk_bytes:
                terminate(); finish_output()
                raise ResourceLimitExceeded("disk_bytes", limits.max_disk_bytes, delta_bytes)
            if delta_files > limits.max_files:
                terminate(); finish_output()
                raise ResourceLimitExceeded("files", limits.max_files, delta_files)
            events = selector.select(timeout=min(.1, max(.01, timeout - elapsed)))
            for key, _ in events:
                try:
                    data = os.read(key.fileobj.fileno(), 64 * 1024)
                except OSError:
                    data = b""
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(data)
                if captured > limits.max_output_bytes:
                    terminate(); finish_output()
                    raise ResourceLimitExceeded("output_bytes", limits.max_output_bytes, captured)
                chunks[key.data].append(data)
        stdout, stderr = finish_output()
        return subprocess.CompletedProcess(process.args, process.returncode,
                                           stdout, stderr)
    finally:
        selector.close()
        if process.poll() is not None:
            _clear_process_record(process_record)


BASE_ENV_ALLOW = frozenset({
    # 启动解释器与基础本地化所需；不继承应用、云厂商、Git 或模型配置。
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ",
    "TERM", "COLORTERM", "NO_COLOR", "USER",
})

OVERRIDE_ENV_ALLOW = frozenset({
    # 只有测试/coverage 产物重定向和沙箱运行目录可以由调用方覆盖。
    "HOME", "TMPDIR", "SHUIMU_YANMA_SCRATCH", "PYTHONDONTWRITEBYTECODE",
    "PYTHONWARNINGS", "PYTEST_ADDOPTS", "COVERAGE_FILE", "COVERAGE_RCFILE",
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

    def __init__(self, limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
                 process_record: Path | None = None):
        self.limits = limits
        self.process_record = process_record

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess:
        return _run_with_process_group(argv, cwd=cwd, timeout=timeout, env=env,
                                       limits=self.limits, monitor_roots=(Path(cwd),),
                                       process_record=self.process_record)


class SandboxedExecutor:
    """macOS Seatbelt 子进程执行器；仓库只读，产物只写 scratch。"""

    mode = "sandboxed"

    def __init__(self, scratch: Path,
                 limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
                 process_record: Path | None = None):
        from tools.sandbox import launch

        if not launch.available():
            raise ExecutorUnavailable("sandbox-exec unavailable")
        self.limits = limits
        self.process_record = process_record
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
            timeout=10, env={}, cpu_seconds=limits.cpu_seconds,
            max_file_bytes=limits.max_file_bytes)
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
            "SHUIMU_YANMA_SCRATCH": str(self.scratch),
            # pytest cache、basetemp 不能落入只读仓库。
            "PYTEST_ADDOPTS": (
                f"--basetemp={self.tmp / 'pytest'} "
                f"-o cache_dir={self.scratch / 'pytest-cache'}"),
        })
        return launch.execute(
            argv, scratch=self.scratch, profile_path=self.profile, cwd=cwd,
            credential_home=Path.home(), process_home=self.home,
            timeout=timeout, env=child_env,
            cpu_seconds=self.limits.cpu_seconds,
            max_file_bytes=self.limits.max_file_bytes,
            process_record=self.process_record)


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
