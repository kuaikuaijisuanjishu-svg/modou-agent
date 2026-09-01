"""临时工作树、基线快照、恢复校验。

水木验码不修改用户仓库。所有试删只发生在 scratch 里的 git worktree，
每次探测后恢复到同一基线，并用 tree 哈希验证恢复是否干净——
"我以为回滚了"和"确实回滚了"是两回事。

基线快照的做法：打完 AI patch 与 test_patch 后在 detached HEAD 上提交一次。
之后恢复 = `git reset --hard` + `git clean -fdx`，校验 = tree 哈希 + 空 status。
JUnit/coverage/证书都写在工作树之外，所以 clean -fdx 不会误伤证据。
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .adapters import RepoAdapter


class WorkspaceError(RuntimeError):
    pass


class DirtyRestore(WorkspaceError):
    """恢复之后工作树和基线对不上。这次实验的结论一律作废。"""


def _git(args: list[str], cwd: Path, timeout: float = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


@dataclass
class Workspace:
    instance_id: str
    adapter: RepoAdapter
    repo_root: Path              # 共享的 clone
    path: Path                   # 本实例的 worktree
    baseline_tree: str           # 基线 tree 哈希
    python: str                  # venv 解释器绝对路径
    base_commit: str = ""        # 打补丁**之前**的 commit

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
        _git(["worktree", "remove", "--force", str(self.path)], self.repo_root)
        shutil.rmtree(self.path, ignore_errors=True)


def prepare(instance_id: str, meta: dict, adapter: RepoAdapter,
            ai_patch: str, slug: str = "",
            repo_root: Path | None = None,
            python: str | None = None) -> Workspace:
    """建 worktree，打 AI 补丁与 test_patch，提交成基线。失败一律抛，不返回 None。

    slug 用来区分同一 instance_id 的不同工作副本——并行运行时它们会同时开工，
    共用一个 worktree 路径就会互相踩踏。

    `repo_root` / `python` 是给**用户自己的仓库**用的覆盖项（`inputs.from_local_repo`）。
    默认仍走 `paths.WORK` 下的 clone 与 venv。

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
            _git(["worktree", "remove", "--force", str(wt)], repo_root)
            raise WorkspaceError(f"{name} 应用失败：{ap.stderr[-300:]}")

    # 把打过补丁的状态固化成基线提交
    _git(["add", "-A"], wt)
    _git(["-c", "user.email=modou@local", "-c", "user.name=modou",
          "commit", "-q", "--allow-empty", "-m", "modou baseline"], wt)
    tree = _git(["rev-parse", "HEAD^{tree}"], wt).stdout.strip()
    if not tree:
        _git(["worktree", "remove", "--force", str(wt)], repo_root)
        raise WorkspaceError("无法取得基线 tree 哈希")

    return Workspace(instance_id=instance_id, adapter=adapter, repo_root=repo_root,
                     path=wt, baseline_tree=tree, python=str(py),
                     base_commit=meta["base_commit"])
