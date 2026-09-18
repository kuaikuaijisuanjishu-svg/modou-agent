import fcntl
import json
import os
import sys
import time
import traceback
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))

from modou import paths                                             # noqa: E402


def _hold_suite_lock():
    """拒绝第二个并发的测试套件，并说出是谁占着。

    为什么需要机器来管：并发跑同一套测试**不报错**，只让失败集合每次换一批。
    集成用例在跑之前会重建共享的 `work/four_state_repo`（`test_integration._staged`），
    那会把另一个进程正跑着的 worktree 打成孤儿，于是它那边每条 git 命令都变成

        fatal: not a git repository: <repo>/.git/worktrees/<name>

    上一次撞见这件事，是靠人恰好去 `ps` 看了一眼。写进文档的纪律拦不住它，
    因为出事的时候没人在读文档。

    锁按**真正被共用的东西**加：`paths.SCRATCH`。两个工作树只要 scratch 不同
    就不冲突，这时拦下来才是误伤。`flock` 是进程活着才持有的，进程被杀也会
    自动释放，所以不会留下需要人去清的僵尸锁。
    """
    if os.environ.get("MODOU_ALLOW_CONCURRENT_SUITE") == "1":
        # 明确知道 scratch 不共用时的出口。留着它是因为演示当天不该被一把
        # 解释不清的锁挡住——但默认必须是拦。
        return None
    paths.SCRATCH.mkdir(parents=True, exist_ok=True)
    lock_path = paths.SCRATCH / "test-suite.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 4096).decode("utf-8", "replace").strip()
        os.close(fd)
        print(f"拒绝启动：已经有一个测试套件在跑，共用 {paths.SCRATCH}。\n"
              f"  占用者：{holder or '（锁文件是空的，占用者刚启动）'}\n"
              f"  锁文件：{lock_path}\n"
              "并发跑同一套测试会互相把 worktree 打成孤儿，失败集合每次都不一样。\n"
              "等它跑完再来；确认两边 scratch 不共用时可用 "
              "MODOU_ALLOW_CONCURRENT_SUITE=1 跳过。", file=sys.stderr)
        raise SystemExit(2)
    os.ftruncate(fd, 0)
    os.write(fd, (f"pid={os.getpid()} 起于={time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"cwd={os.getcwd()}\n").encode("utf-8"))
    os.fsync(fd)
    return fd                     # 故意不关：fd 一关锁就没了，要握到进程结束


_LOCK_FD = _hold_suite_lock()

MODULES = []
for path in sorted((root / "tests").glob("test_*.py")):
    if "--unit-only" in sys.argv and path.stem == "test_integration":
        continue
    MODULES.append(__import__(path.stem))

# 模块级函数 + 测试类里的方法。只收模块级会**静默漏掉**类里的用例：
# pytest 收 835 条、这里只收 816 条，差的 19 条全在 test_recommendations.py 的
# 两个类里，而 CI 跑的是本文件——那 19 条等于从未被强制执行过，绿灯却照常亮。
# 漏收比失败更危险：失败会喊，漏收不会。
def _collect(module):
    found = []
    for attr in sorted(dir(module)):
        value = getattr(module, attr)
        if attr.startswith("test_") and callable(value):
            found.append((attr, value))
        elif attr.startswith("Test") and isinstance(value, type):
            for method in sorted(dir(value)):
                if method.startswith("test_"):
                    call = _bound(value, method)
                    call.pytestmark = getattr(getattr(value, method),
                                              "pytestmark", [])
                    found.append((f"{attr}.{method}", call))
    return found


def _params(fn):
    """把 @pytest.mark.parametrize 展开成一串取值组。

    读的是 pytest 自己挂在函数上的 `pytestmark`，不重新发明一套标注。多个
    parametrize 叠加时取笛卡尔积，与 pytest 的语义一致。看不懂的标注返回 None，
    调用方按零参调一次——那会以 TypeError 记 FAIL，而不是被悄悄跳过。
    """
    marks = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
    if not marks:
        return [((), {})]
    combos = [{}]
    for mark in reversed(marks):
        names_arg, values = mark.args[0], list(mark.args[1])
        keys = ([n.strip() for n in names_arg.split(",")]
                if isinstance(names_arg, str) else list(names_arg))
        grown = []
        for combo in combos:
            for value in values:
                row = value if len(keys) > 1 else (value,)
                grown.append({**combo, **dict(zip(keys, row))})
        combos = grown
    return [((), combo) for combo in combos]


def _bound(cls, method):
    """类里的用例按 pytest 的语义调用：每条用例一个新实例。"""
    def call(**kwargs):
        return getattr(cls(), method)(**kwargs)
    return call


def _expand(label, fn):
    """一条声明 → 若干条可调用用例（parametrize 展开后每组一条）。"""
    target = fn.__func__ if hasattr(fn, "__func__") else fn
    out = []
    for _, kwargs in _params(target):
        suffix = ("[" + "-".join(str(v) for v in kwargs.values()) + "]") if kwargs else ""
        out.append((label + suffix, (lambda f=fn, k=kwargs: f(**k))))
    return out


names = [(m, label, call) for m in MODULES for n, fn in _collect(m)
         for label, call in _expand(n, fn)]
fails = []
cases = []
for mod, name, fn in names:
    try:
        fn()
        print(f"  PASS  {name}")
        cases.append({"name": f"{mod.__name__}.{name}", "passed": True})
    except Exception:
        fails.append(name)
        cases.append({"name": f"{mod.__name__}.{name}", "passed": False})
        print(f"  FAIL  {name}")
        traceback.print_exc()

passed = len(names) - len(fails)
print(f"\n{passed}/{len(names)} 通过"
      + (f"，失败：{fails}" if fails else "，全部通过"))
result = {
    "schema_version": "modou-test-result-v1",
    "suite": "python",
    "collected": len(names),
    "total": len(names),
    "passed": passed,
    "failed": len(fails),
    "skipped": 0,
    "xfailed": 0,
    "xpassed": 0,
    "cases": cases,
}
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
if "--json-output" in sys.argv:
    index = sys.argv.index("--json-output")
    if index + 1 >= len(sys.argv):
        raise SystemExit("--json-output requires a path")
    Path(sys.argv[index + 1]).write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
sys.exit(1 if fails else 0)
