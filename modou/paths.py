"""落盘位置。工作副本一律在 scratch，产物写在工作树之外。

关键约束（方案 §7）：JUnit、coverage 和证书必须写在**工作树外**的运行目录，
否则每次探测后的"恢复基线快照"会把证据一起抹掉。
"""
from pathlib import Path
from .env import get as env_get

PROJECT = Path(__file__).resolve().parents[1]
CONFIGS = PROJECT / "configs"

#: 默认落在 home 下的持久目录。
#: 早先这里写死的是某次会话的 `/private/tmp/.../<session-uuid>/scratchpad`，
#: 里面装着两个仓库的 clone、三个 venv 和全部缓存补丁——而 `/private/tmp`
#: 随时可能被系统清理。演示当天环境蒸发和"跑不出数"是同一级别的事故，
#: 而且它不会有任何报错，只会变成"仓库未 clone"。
#: `tools/setup.py` 可以从零重建这个目录。
_DEFAULT_SCRATCH = Path.home() / ".modou" / "scratch"
SCRATCH = Path(env_get("SCRATCH") or _DEFAULT_SCRATCH)

CACHE = SCRATCH / "cache"                 # 下载的补丁与数据集（与 aokamu 共用）
WORK = SCRATCH / "work"                   # git clone 与 venv
WORKTREES = WORK / "modou_wt"             # 每实例的临时工作树
RUNS = SCRATCH / "modou_runs"             # **工作树之外**：JUnit / coverage / 证书

PATCHES = CACHE / "patches"               # Tools scaffold（aokamu 已下载）
PATCHES2 = CACHE / "patches2"             # 其它 scaffold
META = CACHE / "swebench_verified.jsonl"

BUCKET = "https://swe-bench-submissions.s3.amazonaws.com"

#: 补丁内容在设计实验前被人读过，不得进入盲评指标
CONTAMINATED = frozenset({"sympy__sympy-24066"})


def run_dir(instance_id: str) -> Path:
    d = RUNS / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure():
    for p in (CACHE, WORK, WORKTREES, RUNS, PATCHES):
        p.mkdir(parents=True, exist_ok=True)
