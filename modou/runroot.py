"""Output root and lifecycle helpers for a local review run.

Layout:

    <run_root>/
      run_metadata.json           Software and input metadata
      source.tar
      manifest.json               Immutable run manifest
      run_status.json             生命周期
      results/<scaffold>__<instance>/
        report.json
        ledger.jsonl
      evaluation_results.json

Runs are written outside the reviewed repository so generated evidence cannot
change the repository state.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

#: 正式跑数的默认落点。刻意在仓库之外。
FORMAL_ROOT = Path(os.environ.get("MODOU_FORMAL_RUNS")
                   or Path.home() / ".modou" / "formal-runs")

INCOMPLETE = "INCOMPLETE"        # 开跑即写。进程死掉就停在这里——这正是我们要的
COMPLETE = "COMPLETE"
FAILED = "FAILED"


class RunRootBusy(RuntimeError):
    """这个 run root 已经有产物了。正式模式拒绝覆盖，请换一个新的。"""


@dataclass
class RunRoot:
    path: Path
    official: bool = True
    unofficial_reason: str = ""

    # ------------------------------------------------------------ 布局

    @property
    def results(self) -> Path:
        return self.path / "results"

    @property
    def status_file(self) -> Path:
        return self.path / "run_status.json"

    @property
    def manifest_copy(self) -> Path:
        return self.path / "evaluation_manifest.json"

    @property
    def results_json(self) -> Path:
        return self.path / "evaluation_results.json"

    def unit_dir(self, slug: str) -> Path:
        d = self.results / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def run_id(self) -> str:
        return self.path.name

    # ------------------------------------------------------------ 生命周期

    def has_products(self) -> bool:
        return (self.results.exists() and any(self.results.iterdir())) \
            or self.results_json.exists()

    def begin(self, *, cli: dict | None = None) -> "RunRoot":
        """开跑。正式模式下已有产物即拒绝——不覆盖，不追加。"""
        if self.official and self.has_products():
            raise RunRootBusy(
                f"{self.path} 已有产物（results/ 或 evaluation_results.json）。"
                f"正式跑数拒绝覆盖，请换一个新的 run root。"
                f"\n运行目录已存在，默认拒绝覆盖。")
        self.results.mkdir(parents=True, exist_ok=True)
        self._write_status(INCOMPLETE, reason="", cli=cli or {})
        return self

    def finish(self, ok: bool, reason: str = "") -> None:
        self._write_status(COMPLETE if ok else FAILED, reason=reason)

    def status(self) -> dict:
        if not self.status_file.exists():
            return {"status": "(无记录)"}
        return json.loads(self.status_file.read_text())

    def _write_status(self, status: str, *, reason: str,
                      cli: dict | None = None) -> None:
        prev = self.status() if self.status_file.exists() else {}
        payload = {
            "run_id": self.run_id,
            "status": status,
            "official": self.official,
            "unofficial_reason": self.unofficial_reason,
            "started_at": prev.get("started_at") or _now(),
            "finished_at": _now() if status != INCOMPLETE else None,
            "reason": reason,
            "cli": cli if cli is not None else prev.get("cli", {}),
        }
        self.path.mkdir(parents=True, exist_ok=True)
        self.status_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def formal(run_id: str) -> RunRoot:
    """按 run_id 取一个仓库外的正式 run root。"""
    return RunRoot(path=FORMAL_ROOT / run_id, official=True)


def write_atomic(dest: Path, text: str) -> Path:
    """先写临时文件再 rename。

    半份 JSON 比没有 JSON 更坏——它看起来像一份正式产物。
    发布必须是原子的：要么完整出现，要么根本不出现。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    return dest


BUNDLE = "bundle.json"


def publish_bundle(run_dir: Path, pairs: list[tuple[Path, str]],
                   *, run_id: str) -> list[Path]:
    """发布一组文件，**最后**写一个提交标记。

    `publish_all` 只能保证"发布前失败安全"：写 `.partial` 阶段崩了两份都不出现。
    但它最后仍是两次顺序 rename——进程死在两次之间，目录里就只剩 `report.json`，
    看上去像一份正常产物。多文件的崩溃原子性单靠 rename 拿不到。

    所以最后再写一份 `bundle.json`，记下每份文件的 SHA-256。
    **读取端没有看到它，就必须拒绝把这两份文件当成一次完整产出。**
    """
    written = publish_all(pairs)
    manifest = {
        "bundle_version": 1,
        "run_id": run_id,
        "files": {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                           "bytes": p.stat().st_size}
                  for p in written},
    }
    write_atomic(run_dir / BUNDLE,
                 json.dumps(manifest, ensure_ascii=False, indent=2))
    return written


def verify_bundle(run_dir: Path) -> list[str]:
    """校验一次产出是否完整。返回问题清单，空 = 完整。"""
    bp = run_dir / BUNDLE
    if not bp.exists():
        return [f"没有 {BUNDLE} 提交标记：这两份文件不构成一次完整产出"]
    try:
        m = json.loads(bp.read_text())
    except json.JSONDecodeError as e:
        return [f"{BUNDLE} 不是合法 JSON：{e}"]
    problems = []
    for name, want in (m.get("files") or {}).items():
        f = run_dir / name
        if not f.exists():
            problems.append(f"{name} 缺失")
            continue
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        if got != want.get("sha256"):
            problems.append(f"{name} 内容与提交标记不符")
    return problems


def publish_all(pairs: list[tuple[Path, str]]) -> list[Path]:
    """一组文件一起发布：全部先落 `.partial`，再逐个 rename。

    为什么不逐个 write_atomic：`report.json` 和 `ledger.jsonl` 是**一次运行的两面**。
    只出现其中一个，读的人会以为另一个是被谁删了，而不是根本没写成。
    先把两份都准备好，能减到只剩两次 rename 的窗口。
    """
    tmps: list[tuple[Path, Path]] = []
    try:
        for dest, text in pairs:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".partial")
            tmp.write_text(text, encoding="utf-8")
            tmps.append((tmp, dest))
    except BaseException:
        for tmp, _ in tmps:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dest in tmps:
        os.replace(tmp, dest)
    return [d for _, d in tmps]
