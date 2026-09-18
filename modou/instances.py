"""实例数据的加载：补丁、元数据、声明测试文件。

`eval.sh` 来自基准而不是 scaffold，同一个 instance_id 在不同 scaffold 下是同一份，
所以缓存在哪个 scaffold 目录下都可以复用。

但更好的做法是尽量**不依赖 eval.sh**：Sphinx 的声明测试项本身就是完整 nodeid，
文件路径直接就在里面。只有 SymPy 这种给裸函数名的仓库才需要回退到 eval.sh。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import paths
from .adapters import declared_tests


# The internal Linux acceptance image is intentionally network-isolated and
# cannot populate the historical benchmark cache. Keep the one frozen Day 4
# primary input as a small source-controlled compatibility fixture; normal
# installations continue to prefer the operator-managed cache.
_FROZEN_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "benchmark_primary"


def _meta_path() -> Path:
    return paths.META if paths.META.exists() else _FROZEN_FIXTURE_ROOT / "swebench_verified.jsonl"


def _patch_path(instance_id: str, scaffold: str) -> Path:
    cached = scaffold_dir(scaffold) / instance_id / "patch.diff"
    if cached.exists():
        return cached
    return _FROZEN_FIXTURE_ROOT / "patches" / instance_id / "patch.diff"


@lru_cache(maxsize=1)
def load_meta() -> dict[str, dict]:
    source = _meta_path()
    if not source.exists():
        raise FileNotFoundError(f"元数据未缓存：{paths.META}（先跑 tools/fetch.py）")
    return {json.loads(l)["instance_id"]: json.loads(l) for l in source.open()}


def scaffold_dir(scaffold: str) -> Path:
    """Tools 那份在 PATCHES，其它在 PATCHES2/<submission>。"""
    if scaffold in ("20241022_tools_claude-3-5-sonnet-updated", "tools"):
        return paths.PATCHES
    return paths.PATCHES2 / scaffold


def instance_dir(instance_id: str, scaffold: str) -> Path:
    return scaffold_dir(scaffold) / instance_id


def has_patch(instance_id: str, scaffold: str) -> bool:
    return (instance_dir(instance_id, scaffold) / "patch.diff").exists()


def load_patch(instance_id: str, scaffold: str) -> str:
    p = _patch_path(instance_id, scaffold)
    if not p.exists():
        raise FileNotFoundError(f"补丁未缓存：{instance_id}（先跑 tools/fetch.py）")
    return p.read_text(encoding="utf-8", errors="surrogateescape")


def is_resolved(instance_id: str, scaffold: str) -> bool | None:
    p = instance_dir(instance_id, scaffold) / "report.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
        return bool(d[list(d)[0]].get("resolved"))
    except Exception:                                   # noqa: BLE001
        return None


def list_instances(scaffold: str, repo_prefix: str | None = None,
                   resolved_only: bool = True) -> list[str]:
    d = scaffold_dir(scaffold)
    if not d.exists():
        return []
    out = []
    for sub in sorted(d.iterdir()):
        if not sub.is_dir() or not (sub / "patch.diff").exists():
            continue
        if repo_prefix and not sub.name.startswith(repo_prefix):
            continue
        if resolved_only and is_resolved(sub.name, scaffold) is not True:
            continue
        out.append(sub.name)
    return out


def _eval_sh(instance_id: str) -> str:
    """任一 scaffold 目录下的 eval.sh 都行——它来自基准，不来自 scaffold。"""
    for d in (paths.PATCHES, *(sorted(paths.PATCHES2.iterdir())
                               if paths.PATCHES2.exists() else ())):
        p = (d / instance_id / "eval.sh") if d.name != instance_id else None
        if p and p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    fixture = _FROZEN_FIXTURE_ROOT / "patches" / instance_id / "eval.sh"
    if fixture.exists():
        return fixture.read_text(encoding="utf-8", errors="replace")
    return ""


def declared_test_files(instance_id: str, adapter, meta: dict) -> list[str]:
    """声明测试项落在哪些文件里。

    优先从 nodeid 直接取（Sphinx 那种）；取不到再回退 eval.sh（SymPy 那种裸函数名）。
    """
    explicit = meta.get("test_files")
    if isinstance(explicit, list) and all(isinstance(item, str) for item in explicit):
        return list(dict.fromkeys(explicit))
    files, seen = [], set()
    for t in declared_tests(meta):
        if "::" in t:
            f = t.split("::", 1)[0]
            if f.endswith(".py") and f not in seen:
                seen.add(f)
                files.append(f)
    if files:
        return files
    return adapter.test_files(_eval_sh(instance_id))
