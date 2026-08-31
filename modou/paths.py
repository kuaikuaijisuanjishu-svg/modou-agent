"""Filesystem locations for isolated workspaces and local run outputs."""
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CONFIGS = PROJECT / "configs"

_DEFAULT_SCRATCH = Path.home() / ".modou" / "scratch"
SCRATCH = Path(os.environ.get("MODOU_SCRATCH") or _DEFAULT_SCRATCH)

CACHE = SCRATCH / "cache"
WORK = SCRATCH / "work"
WORKTREES = WORK / "modou_wt"
RUNS = SCRATCH / "modou_runs"
PATCHES = CACHE / "patches"
PATCHES2 = CACHE / "patches2"
META = CACHE / "metadata.jsonl"
CONTAMINATED = frozenset()


def run_dir(instance_id: str) -> Path:
    d = RUNS / instance_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure():
    for p in (CACHE, WORK, WORKTREES, RUNS, PATCHES):
        p.mkdir(parents=True, exist_ok=True)
