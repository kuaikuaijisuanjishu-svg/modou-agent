"""补丁语法闸：应用之后、提交之前，改动文件必须能被机器读懂。

这是今天唯一真缺的一类校验：补丁边界（范围/路径/大小）与验证器
（声明的测试）都不能证明改动文件语法成立。闸是失败关闭的——
不认识的语言直接拒绝，绝不"跳过语法检查后继续"。

本模块只读文件、只做解析，不写任何东西；错误码是结构化契约
（REPAIR_SYNTAX_*），调用方原样透传，不允许塌缩成异常类名。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


class SyntaxGuardError(RuntimeError):
    """语法闸拒绝；code 是 REPAIR_SYNTAX_* 契约错误码。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


NODE_TIMEOUT_SECONDS = 30

# node 从临时脚本读探针本体，候选查找根与目标文件走 argv，
# 避免命令行长度与引号转义问题。
# 适配器选型说明：本仓库工具链是 typescript@7（原生编译器，不暴露
# transpileModule JS API）+ rolldown/oxc（vite 8 的解析内核）。oxc 的
# transformSync 是纯语法转换——只报 PARSE_ERROR、不做类型检查——恰好是
# 语法闸要的判据，多报类型错会把合法补丁误杀。
_TS_PROBE_SCRIPT = r"""
const fs = require('fs');
const { createRequire } = require('module');
const path = require('path');
const candidates = JSON.parse(process.argv[2] || '[]');
const file = process.argv[3];
let oxc = null;
let lastErr = '';
for (const candidate of candidates) {
  try {
    const req = createRequire(path.join(candidate, 'package.json'));
    const mod = req('rolldown/experimental');
    if (mod && mod.transformSync) { oxc = mod; break; }
  } catch (err) { lastErr = String((err && err.message) || err); }
}
if (!oxc) {
  process.stdout.write(JSON.stringify({
    ok: false, code: 'REPAIR_SYNTAX_ADAPTER_UNAVAILABLE',
    detail: ('rolldown/oxc transform not found for .ts syntax check; '
             + lastErr).slice(0, 300)}));
  process.exit(0);
}
const text = fs.readFileSync(file, 'utf8');
const loader = file.endsWith('.tsx') ? 'tsx' : 'ts';
let out = null;
try {
  out = oxc.transformSync(file, text, { loader: loader, lang: loader });
} catch (err) {
  process.stdout.write(JSON.stringify(
    {ok: false, code: 'REPAIR_SYNTAX_INVALID',
     detail: String((err && err.message) || err).slice(0, 300)}));
  process.exit(0);
}
const errors = (out && out.errors) || [];
if (errors.length) {
  const detail = errors
    .map(e => String(e.message || '').replace(/\x1b\[[0-9;]*m/g, ''))
    .join('; ').slice(0, 300);
  process.stdout.write(JSON.stringify(
    {ok: false, code: 'REPAIR_SYNTAX_INVALID', detail}));
} else {
  process.stdout.write(JSON.stringify({ok: true}));
}
"""


def _ts_candidate_roots(worktree: Path) -> list[str]:
    """rolldown/oxc 的候选查找根：目标 worktree 优先，工具自带兜底。"""
    roots: list[str] = []
    tool_root = Path(__file__).resolve().parents[2]
    # 本仓库的解析内核（rolldown，随 vite 8 安装）装在 web/ 前端
    # 工作区下，工具根自身没有 node_modules；三个位置都试，谁有就
    # 用谁。登记的是"查找根"（node_modules 的宿主目录），node 从
    # 宿主向上解析即可命中。
    for base in (worktree, tool_root, tool_root / "web"):
        if (base / "node_modules" / "rolldown").is_dir() and base not in roots:
            roots.append(str(base))
    return roots


def check_text(path: str, text: str) -> None:
    """按后缀分派语法检查；不认识的扩展名失败关闭。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        try:
            compile(text, path, "exec")
        except (SyntaxError, ValueError) as exc:
            message = str(exc.msg) if hasattr(exc, "msg") else str(exc)
            raise SyntaxGuardError(
                "REPAIR_SYNTAX_INVALID",
                f"{path}: {message[:200]}") from exc
        return
    if suffix in {".ts", ".tsx"}:
        # check_text 拿到的是纯文本，没有 worktree 上下文；.ts 必须
        # 走 check_file 的磁盘适配器路径。
        raise SyntaxGuardError(
            "REPAIR_SYNTAX_ADAPTER_REQUIRED",
            ".ts files must be checked on disk via check_file")
    raise SyntaxGuardError(
        "REPAIR_SYNTAX_LANGUAGE_UNSUPPORTED",
        f"no syntax check adapter for {suffix or '(no suffix)'} files; "
        "refusing to skip the syntax gate")


def check_file(target: Path, *, worktree: Path | None = None) -> None:
    """检查 worktree 里一个真实文件的语法；按后缀分派。"""
    target = Path(target)
    suffix = target.suffix.lower()
    if suffix in {".ts", ".tsx"}:
        _check_typescript(target, worktree=worktree or target.parent)
        return
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SyntaxGuardError(
            "REPAIR_SYNTAX_INVALID",
            f"{target.name}: file is not readable UTF-8 text ({exc})") from exc
    check_text(str(target), text)


def _check_typescript(target: Path, *, worktree: Path) -> None:
    candidates = _ts_candidate_roots(worktree)
    if not candidates:
            raise SyntaxGuardError(
                "REPAIR_SYNTAX_ADAPTER_UNAVAILABLE",
                "rolldown/oxc transform not found for .ts syntax check")
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", prefix="shuimu-ts-guard-",
        encoding="utf-8", delete=False)
    script_path = Path(handle.name)
    with handle:
        handle.write(_TS_PROBE_SCRIPT)
    try:
        try:
            result = subprocess.run(
                ["node", str(script_path),
                 json.dumps(candidates), str(target)],
                capture_output=True, text=True, check=False,
                timeout=NODE_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyntaxGuardError(
                "REPAIR_SYNTAX_ADAPTER_FAILED",
                f"ts/tsx syntax check could not run: {exc}") from exc
        if result.returncode != 0:
            raise SyntaxGuardError(
                "REPAIR_SYNTAX_ADAPTER_FAILED",
                (result.stderr.strip() or "node exited non-zero")[:300])
        try:
            verdict = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SyntaxGuardError(
                "REPAIR_SYNTAX_ADAPTER_FAILED",
                f"syntax check returned unparseable output: {exc}") from exc
        if not verdict.get("ok"):
            raise SyntaxGuardError(str(verdict.get("code")),
                                   str(verdict.get("detail"))[:300])
    finally:
        script_path.unlink(missing_ok=True)
