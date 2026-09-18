"""临时工作树、基线快照、恢复校验。

水木验码不修改用户仓库。所有试删只发生在 scratch 里的 git worktree，
每次探测后恢复到同一基线，并用 tree 哈希验证恢复是否干净——
"我以为回滚了"和"确实回滚了"是两回事。

基线快照的做法：打完 AI patch 与 test_patch 后在 detached HEAD 上提交一次。
之后恢复 = `git reset --hard` + `git clean -fdx`，校验 = tree 哈希 + 空 status。
JUnit/coverage/证书都写在工作树之外，所以 clean -fdx 不会误伤证据。
"""
from __future__ import annotations

import atexit
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .adapters import RepoAdapter
from .safe_git import run_git


class WorkspaceError(RuntimeError):
    pass


class DirtyRestore(WorkspaceError):
    """恢复之后工作树和基线对不上。这次实验的结论一律作废。"""


#: 本进程里所有还活着的 worktree。`cleanup()` 从这里摘掉自己。
#:
#: 为什么需要它：`prepare()` 建的工作树落在**全局**的 `paths.WORKTREES`，
#: 而不是调用方自己的临时目录。只要有一条路径没跑到 `cleanup()`——
#: 分步驱动 `AnalysisSession` 的调用方（工具脚本、测试）拿到 workspace
#: 之后直接结束就是这种情况——那个目录就永远留在用户 home 下，
#: 而且在它的母仓库被删掉之后连 `git worktree remove` 都救不回来。
#: 进程退出时兜底清一遍，是"跑完或中断都不留残留"唯一不依赖调用方自觉的做法。
_LIVE: "dict[Path, Workspace]" = {}
_LIVE_LOCK = threading.Lock()


def _register(ws: "Workspace") -> None:
    with _LIVE_LOCK:
        _LIVE[ws.path] = ws


def _unregister(path: Path) -> None:
    with _LIVE_LOCK:
        _LIVE.pop(path, None)


def live_worktrees() -> tuple[Path, ...]:
    """本进程尚未清理的工作树。给兜底与自检用。"""
    with _LIVE_LOCK:
        return tuple(_LIVE)


@atexit.register
def _cleanup_live_worktrees() -> None:
    """进程退出兜底。异常一律吞掉——退出路径上不能再抛。"""
    with _LIVE_LOCK:
        remaining = list(_LIVE.values())
    for ws in remaining:
        try:
            ws.cleanup()
        except Exception:
            pass


def _git(args: list[str], cwd: Path, timeout: float = 300) -> subprocess.CompletedProcess:
    return run_git(args, cwd=cwd, timeout=timeout)


def prune_registrations(repo_root: Path) -> bool:
    """删掉母仓库 `.git/worktrees` 下指向已消失目录的登记项。

    `--expire=now` 不能省：`git worktree prune` 默认按 `gc.worktreePruneExpire`
    （3 个月）算过期，刚刚删掉的目录是清不掉的，于是"清理过了"和"真的清干净了"
    又会分家。仓库本身已经不在时返回 False，而不是抛——清理路径不制造新故障。
    """
    if not Path(repo_root).exists():
        return False
    return _git(["worktree", "prune", "--expire=now"],
                Path(repo_root), timeout=60).returncode == 0


@dataclass
class Workspace:
    instance_id: str
    adapter: RepoAdapter
    repo_root: Path              # 共享的 clone
    path: Path                   # 本实例的 worktree
    baseline_tree: str           # 基线 tree 哈希
    python: str                  # venv 解释器绝对路径
    base_commit: str = ""        # 打补丁**之前**的 commit
    cleaned: bool = False        # cleanup() 是否已经跑到

    # -------------------------------------------------------------- 恢复

    def restore(self) -> None:
        """恢复到基线，并校验确实回到了基线。"""
        reset = _git(["reset", "--hard", "--quiet", "HEAD"], self.path)
        clean = _git(["clean", "-fdxq"], self.path)
        tr = _git(["rev-parse", "HEAD^{tree}"], self.path)
        sr = _git(["status", "--porcelain"], self.path)
        if reset.returncode or clean.returncode or tr.returncode or sr.returncode:
            detail = "；".join(x.stderr.strip()[:120] for x in
                                (reset, clean, tr, sr) if x.returncode)
            raise DirtyRestore(f"{self.instance_id} 恢复命令失败：{detail}")
        tree = tr.stdout.strip()
        status = sr.stdout.strip()
        if tree != self.baseline_tree or status:
            raise DirtyRestore(
                f"{self.instance_id} 恢复后与基线不一致："
                f"tree={tree[:8]} 期望={self.baseline_tree[:8]} status={status[:200]!r}")

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text(encoding="utf-8", errors="surrogateescape")

    def write(self, rel: str, text: str) -> None:
        (self.path / rel).write_text(text, encoding="utf-8", errors="surrogateescape")

    def exists_in_base(self, rel: str) -> bool:
        """打补丁**之前**的 commit 里存不存在这个路径（游离判据①）。

        必须用 base_commit，不能用 HEAD —— prepare() 把打过补丁的状态提交成了基线，
        所以 HEAD 里当然有补丁新建的文件。用 HEAD 会让每个新文件都被判成"补丁前已存在"，
        游离引擎永远不会命中。
        """
        ref = self.base_commit or "HEAD"
        r = _git(["cat-file", "-e", f"{ref}:{rel}"], self.path)
        return r.returncode == 0

    def cleanup(self) -> None:
        """删掉工作树目录，并摘掉母仓库里的登记项。可重复调用。

        早先这里只发一条 `git worktree remove --force` 就算完，返回码不看。
        那条命令有两种常见的失败方式，而且都**不会**留下任何痕迹：

        - 母仓库已经不在了（用户仓库是临时目录、或被 `_staged()` 重建过），
          `remove` 直接失败，`rmtree` 把目录删掉，登记项却留在别处；
        - 母仓库还在，但登记项已经断了，`remove` 拒绝删，
          `ignore_errors=True` 的 `rmtree` 再把目录删掉——看着"清理成功"。

        所以现在按结果判定：目录必须消失，母仓库里必须 `prune` 一次。
        `prune` 是清理这次留下的登记项的唯一可靠手段，因为 `remove` 失败时
        它就是剩下的那条路。
        """
        detail = ""
        if Path(self.repo_root).exists():
            # cwd 不存在时 subprocess 连 git 都起不来，抛的是 FileNotFoundError
            # 而不是返回非零——所以先判断母仓库还在不在，再发命令。
            removed = _git(["worktree", "remove", "--force", str(self.path)],
                           self.repo_root)
            if removed.returncode:
                # `remove` 没成功时目录要自己删——删不掉才是真失败。
                shutil.rmtree(self.path, ignore_errors=True)
                detail = removed.stderr.strip()[:200]
        else:
            # 母仓库已经不在（临时目录被清、或被重建过）。登记项随它一起没了，
            # 这里只剩目录要收。这正是绝大多数残留的来历。
            shutil.rmtree(self.path, ignore_errors=True)
            detail = f"母仓库已不存在：{self.repo_root}"
        prune_registrations(self.repo_root)
        _unregister(self.path)
        self.cleaned = True
        if self.path.exists():
            raise WorkspaceError(
                f"{self.instance_id} 工作树未能删除：{self.path}"
                + (f"（{detail}）" if detail else ""))

    # 让忘记 cleanup 变成一件更难做到的事。
    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.cleanup()
        return False


def prepare(instance_id: str, meta: dict, adapter: RepoAdapter,
            ai_patch: str, slug: str = "",
            repo_root: Path | None = None,
            python: str | None = None) -> Workspace:
    """建 worktree，打 AI 补丁与 test_patch，提交成基线。失败一律抛，不返回 None。

    slug 用来区分同一 instance_id 的不同 scaffold —— 并行评测时它们会同时开工，
    共用一个 worktree 路径就会互相踩踏。

    `repo_root` / `python` 是给**用户自己的仓库**用的覆盖项（`inputs.from_local_repo`）。
    默认仍走 `paths.WORK` 下的 clone 与 venv，冻结样本评测路径完全不变。

    在用户仓库上开 worktree **不会动他们的工作树**：`worktree add --detach`
    只读地引用对象库，当前分支、暂存区、未提交改动都不受影响。
    但它会在用户仓库的 `.git/worktrees/` 下留登记项，所以 `cleanup()` 必须跑到。
    """
    repo_root = Path(repo_root) if repo_root else paths.WORK / adapter.clone_dir
    if not repo_root.exists():
        raise WorkspaceError(f"仓库未 clone：{repo_root}")
    py = Path(python) if python else paths.WORK / adapter.venv / "bin" / "python"
    if not py.exists():
        raise WorkspaceError(f"Python 解释器不存在：{py}")

    base_name = f"{slug}__{instance_id}" if slug else instance_id
    # 路径必须按**运行**唯一，而不能只按 instance 唯一。多个 Review（甚至多个
    # 水木验码进程）可以同时审查同名本地仓库；共享路径会让一方删除另一方的
    # worktree，并在用户仓库 .git/worktrees 下争用同一个锁。
    wt = paths.WORKTREES / f"{base_name}__{uuid.uuid4().hex[:12]}"
    wt.parent.mkdir(parents=True, exist_ok=True)

    r = _git(["worktree", "add", "--detach", "--quiet", str(wt),
              meta["base_commit"]], repo_root)
    if r.returncode:
        _discard(wt, repo_root)
        raise WorkspaceError(f"worktree 建立失败：{r.stderr[-300:]}")

    for name, text in (("ai_patch", ai_patch),
                       ("test_patch", meta.get("test_patch") or "")):
        if not text.strip():
            continue
        pf = wt / ".modou.patch"
        pf.write_text(text, encoding="utf-8", errors="surrogateescape")
        ap = _git(["apply", "-p1", "--whitespace=nowarn", str(pf)], wt)
        if ap.returncode:
            ap = _git(["apply", "-p1", "--3way", "--whitespace=nowarn", str(pf)], wt)
        pf.unlink()
        if ap.returncode:
            _discard(wt, repo_root)
            raise WorkspaceError(f"{name} 应用失败：{ap.stderr[-300:]}")

    # 把打过补丁的状态固化成基线提交
    _git(["add", "-A"], wt)
    _git(["-c", "user.email=modou@local", "-c", "user.name=modou",
          "commit", "-q", "--allow-empty", "-m", "modou baseline"], wt)
    tree = _git(["rev-parse", "HEAD^{tree}"], wt).stdout.strip()
    if not tree:
        _discard(wt, repo_root)
        raise WorkspaceError("无法取得基线 tree 哈希")

    ws = Workspace(instance_id=instance_id, adapter=adapter, repo_root=repo_root,
                   path=wt, baseline_tree=tree, python=str(py),
                   base_commit=meta["base_commit"])
    _register(ws)
    return ws


def _discard(wt: Path, repo_root: Path) -> None:
    """`prepare()` 半途失败时的清理。半个工作树也是工作树，一样要连登记项一起收。"""
    _git(["worktree", "remove", "--force", str(wt)], repo_root)
    shutil.rmtree(wt, ignore_errors=True)
    prune_registrations(repo_root)
