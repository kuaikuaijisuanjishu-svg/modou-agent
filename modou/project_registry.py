"""T07：服务端登记的项目运行配置、环境诊断与四语言统一执行入口。

职责边界（docs/interfaces/adapters-packs.md）：

- 登记：``ProjectRegistry.register()`` 只接受服务端登记的固定配置——
  ``{project_id, name, root_path, language, adapter_id, run_config,
  registered_by, created_at}``。网页/客户端只按 ``project_id``（及登记范围内
  的 test_ids 子集）选择，不提交自由命令文本；固定 argv 一律由既有语言
  适配器构造（无 shell、默认离线、不隐式安装依赖），登记侧不复制命令。
- 诊断：``diagnose(project_id)`` 用真实探测（``shutil.which`` + 版本命令 +
  可导入性探针）返回 ``{runtime_present, runtime_version, missing,
  capabilities_ready}``。能力声明的真值来自这里，不由语言选项决定。
- 执行：``execute_tests(project_id, test_ids=None)`` 先诊断；运行时缺失返回
  ``env_unavailable`` 回执（含缺失项），否则真实 subprocess 执行并
  ``parse`` 出具名测试结果。
- 依赖准备：``prepare_dependencies(project_id)`` 独立执行登记的
  ``prepare_command`` 并封存记录；``execute_tests`` 绝不调用它。
- ``adapter_for(project_id)`` 返回适配器实例，供集成人在候选隔离验证
  （tasks.py::_prepare 目前硬编码 pytest）按语言分发。
- ``declare_capabilities()`` 汇总各登记项目的能力并按诊断真值过滤。

本模块不修改任何既有适配器；Python 适配器的 ``--json-report-file`` 需要
pytest-json-report 插件以 ``PYTEST_ADDOPTS=--json-report`` 启用，该变量在
executor 的环境覆盖白名单内（OVERRIDE_ENV_ALLOW），由包装执行器注入，
不改变固定 argv。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .executor import (CommandExecutor, ResourceLimits, TrustedLocalExecutor,
                       sanitized_environment)
from .go_adapter import GoProject
from .java_adapter import JavaProject
from .language_adapter import AdapterCapabilities
from .language_adapters import supported_languages
from .python_adapter import PythonPytestAdapter
from .typescript_adapter import TypeScriptProject

REGISTRY_SCHEMA_VERSION = "registered-projects-v1"
RUN_RECEIPT_SCHEMA_VERSION = "modou-project-run-v1"
PREPARE_RECORD_SCHEMA_VERSION = "modou-prepare-dependencies-v1"

#: 适配器统一能力名（docs/interfaces/adapters-packs.md）。
CAPABILITY_NAMES = ("named_tests", "candidate_tests", "adoption_recheck",
                    "environment_probe")

_LANGUAGE_ALIASES = {"js": "typescript", "javascript": "typescript", "ts": "typescript",
                     "golang": "go", "jvm": "java", "py": "python"}

_ADAPTER_IDS = {
    "python": ("python-pytest-v1",),
    "typescript": ("typescript-vitest-v1", "typescript-jest-v1"),
    "go": ("go-test-v1",),
    "java": ("java-maven-surefire-v1",),
}

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "registered-projects.json"


class ProjectRegistryError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_language(language: str) -> str:
    normalized = (language or "").strip().lower()
    normalized = _LANGUAGE_ALIASES.get(normalized, normalized)
    if normalized not in supported_languages():
        raise ProjectRegistryError("LANGUAGE_UNSUPPORTED", str(language))
    return normalized


@dataclass(frozen=True)
class RegisteredProject:
    project_id: str
    name: str
    root_path: str
    language: str
    adapter_id: str
    run_config: dict[str, Any] = field(default_factory=dict)
    registered_by: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "root_path": self.root_path,
            "language": self.language,
            "adapter_id": self.adapter_id,
            "run_config": json.loads(json.dumps(self.run_config)),
            "registered_by": self.registered_by,
            "created_at": self.created_at,
        }


class _EnvAugmentedExecutor:
    """在适配器构造的环境上追加白名单变量；不触碰固定 argv。

    供 Python 适配器启用 pytest-json-report（其固定命令已携带
    ``--json-report-file``；启用开关走 ``PYTEST_ADDOPTS`` 白名单覆盖）。
    """

    mode = "trusted_local+env_overrides"

    def __init__(self, inner: CommandExecutor, extra_env: dict[str, str]):
        self.inner = inner
        self.extra_env = dict(extra_env)
        self.limits = getattr(inner, "limits", None)

    def run(self, argv: list[str], *, cwd: Path, timeout: float,
            env: dict[str, str]) -> subprocess.CompletedProcess:
        merged = dict(env)
        merged.update(self.extra_env)
        return self.inner.run(argv, cwd=cwd, timeout=timeout, env=merged)


def _probe(argv: list[str], *, timeout: float = 15) -> tuple[bool, str]:
    """运行一个只读版本/可导入性探针，返回 (成功, 输出首行)。"""
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout, env=sanitized_environment())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    lines = [line.strip() for line in (result.stdout or "").splitlines()
             if line.strip()]
    if not lines:
        lines = [line.strip() for line in (result.stderr or "").splitlines()
                 if line.strip()]
    return True, lines[0] if lines else ""


#: run_config.resource_limits 允许覆盖的执行器资源上限（服务端登记，非客户端提交）。
_RESOURCE_LIMIT_FIELDS = frozenset({
    "cpu_seconds", "max_memory_bytes", "max_processes", "max_disk_bytes",
    "max_files", "max_output_bytes", "max_file_bytes"})


def _validated_resource_limits(config: dict[str, Any]) -> dict[str, int]:
    limits = config.get("resource_limits")
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        raise ProjectRegistryError("RUN_CONFIG_INVALID", "resource_limits must be an object")
    unknown = sorted(set(limits) - _RESOURCE_LIMIT_FIELDS)
    if unknown:
        raise ProjectRegistryError("RUN_CONFIG_INVALID",
                                   "unknown resource limits: " + ",".join(unknown))
    clean: dict[str, int] = {}
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ProjectRegistryError("RUN_CONFIG_INVALID", f"{key} must be a positive int")
        clean[key] = value
    return clean


def _executor_for(project: RegisteredProject) -> TrustedLocalExecutor:
    """按登记配置构造执行器；node 工具链需要显式放宽的内存/进程上限。"""
    overrides = _validated_resource_limits(project.run_config)
    return TrustedLocalExecutor(limits=ResourceLimits(**overrides)) if overrides \
        else TrustedLocalExecutor()


class ProjectRegistry:
    """登记项目的只读查询 + 统一诊断/执行面（供集成人挂 API）。"""

    def __init__(self, config_path: str | Path | None = _DEFAULT_CONFIG_PATH):
        """``config_path=None`` 表示纯内存登记（不读不写文件）。"""
        self.config_path = None if config_path is None else Path(config_path)
        self._projects: dict[str, RegisteredProject] = {}
        if self.config_path is not None and self.config_path.is_file():
            self._load(self.config_path)

    # ------------------------------------------------------------ 登记与查询

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectRegistryError("REGISTRY_CONFIG_INVALID", str(exc)) from exc
        if raw.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise ProjectRegistryError("REGISTRY_CONFIG_INVALID",
                                       f"schema_version must be {REGISTRY_SCHEMA_VERSION}")
        for item in raw.get("projects") or []:
            project = RegisteredProject(
                project_id=str(item["project_id"]),
                name=str(item.get("name") or item["project_id"]),
                root_path=str(item["root_path"]),
                language=str(item["language"]),
                adapter_id=str(item["adapter_id"]),
                run_config=dict(item.get("run_config") or {}),
                registered_by=str(item.get("registered_by") or ""),
                created_at=str(item.get("created_at") or ""),
            )
            # 已登记配置按原样载入（root 可能已迁移）；登记时才强校验存在性。
            if project.project_id in self._projects:
                raise ProjectRegistryError("PROJECT_DUPLICATE", project.project_id)
            self._projects[project.project_id] = project

    def save(self) -> None:
        if self.config_path is None:
            raise ProjectRegistryError("REGISTRY_PATH_REQUIRED", "no config path")
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "projects": [project.as_dict() for project in
                         sorted(self._projects.values(), key=lambda p: p.project_id)],
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def register(self, *, project_id: str, name: str, root_path: str | Path,
                 language: str, adapter_id: str, run_config: dict[str, Any] | None = None,
                 registered_by: str, created_at: str | None = None) -> RegisteredProject:
        project_id = str(project_id or "").strip()
        if not project_id or any(ch.isspace() for ch in project_id):
            raise ProjectRegistryError("PROJECT_ID_INVALID", str(project_id))
        if project_id in self._projects:
            raise ProjectRegistryError("PROJECT_DUPLICATE", project_id)
        normalized = _normalize_language(language)
        root = Path(root_path).expanduser()
        if not root.is_absolute():
            raise ProjectRegistryError("PROJECT_ROOT_NOT_ABSOLUTE", str(root_path))
        if not root.is_dir():
            raise ProjectRegistryError("PROJECT_ROOT_MISSING", str(root))
        config = dict(run_config or {})
        targets = self._validated_targets(root, normalized, config)
        # 适配器 id 的真值来自对登记根目录的真实发现结果。
        discovered = self._build_adapter(root, normalized, config)
        if getattr(discovered, "adapter_id", "") != adapter_id:
            raise ProjectRegistryError(
                "ADAPTER_MISMATCH",
                f"declared {adapter_id}, discovered {getattr(discovered, 'adapter_id', '')}")
        timeout = int(config.get("timeout_seconds") or 300)
        if not 1 <= timeout <= 7200:
            raise ProjectRegistryError("TIMEOUT_INVALID", str(timeout))
        prepare = config.get("prepare_command")
        if prepare is not None:
            if (not isinstance(prepare, (list, tuple)) or not prepare
                    or any(not isinstance(part, str) or not part for part in prepare)):
                raise ProjectRegistryError("PREPARE_COMMAND_INVALID", str(prepare))
            config["prepare_command"] = list(prepare)
        _validated_resource_limits(config)
        config["test_targets"] = list(targets)
        if "python_interpreter" in config and normalized != "python":
            raise ProjectRegistryError("RUN_CONFIG_INVALID",
                                       "python_interpreter only applies to python")
        project = RegisteredProject(
            project_id=project_id, name=str(name or project_id), root_path=str(root),
            language=normalized, adapter_id=str(adapter_id), run_config=config,
            registered_by=str(registered_by or ""),
            created_at=created_at or _utc_now(),
        )
        self._projects[project_id] = project
        return project

    def _validated_targets(self, root: Path, language: str,
                           config: dict[str, Any]) -> list[str]:
        targets = config.get("test_targets")
        if not isinstance(targets, (list, tuple)) or any(
                not isinstance(item, str) for item in targets):
            raise ProjectRegistryError("RUN_SCOPE_INVALID", "test_targets must be strings")
        targets = list(targets)
        if language in ("python", "typescript"):
            if not targets:
                raise ProjectRegistryError("RUN_SCOPE_REQUIRED",
                                           "at least one test file target is required")
            for item in targets:
                path = Path(item)
                if path.is_absolute() or ".." in path.parts:
                    raise ProjectRegistryError("RUN_SCOPE_INVALID", item)
                resolved = (root / path).resolve(strict=False)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise ProjectRegistryError("RUN_SCOPE_INVALID", item) from exc
                if not resolved.is_file():
                    raise ProjectRegistryError("RUN_SCOPE_INVALID",
                                               f"test target not found: {item}")
        elif language == "go":
            if not targets:
                raise ProjectRegistryError("RUN_SCOPE_REQUIRED",
                                           "at least one package pattern is required")
            for item in targets:
                if (item.startswith("-") or item.startswith("/") or ".." in Path(item).parts
                        or any(ch.isspace() for ch in item) or "//" in item
                        or not (item == "./..." or item.startswith("./") or item.startswith("."))):
                    raise ProjectRegistryError("RUN_SCOPE_INVALID", item)
        elif language == "java":
            if targets:
                raise ProjectRegistryError(
                    "RUN_SCOPE_INVALID",
                    "java v1 runs the declared Maven suite; test_targets must be empty")
        return targets

    def list_projects(self) -> list[dict[str, Any]]:
        return [project.as_dict() for project in
                sorted(self._projects.values(), key=lambda p: p.project_id)]

    def get(self, project_id: str) -> RegisteredProject:
        project = self._projects.get(project_id)
        if project is None:
            raise ProjectRegistryError("PROJECT_NOT_FOUND", str(project_id))
        return project

    # ------------------------------------------------------------ 适配器接线

    @staticmethod
    def _build_adapter(root: Path, language: str,
                       config: dict[str, Any]) -> Any:
        if language == "python":
            interpreter = config.get("python_interpreter")
            if interpreter:
                return PythonPytestAdapter(root, str(interpreter))
            return PythonPytestAdapter(root, shutil.which("python") or "python3")
        if language == "typescript":
            return TypeScriptProject.discover(root)
        if language == "go":
            return GoProject.discover(root)
        if language == "java":
            return JavaProject.discover(root)
        raise ProjectRegistryError("LANGUAGE_UNSUPPORTED", language)

    def adapter_for(self, project_id: str) -> Any:
        """返回该登记项目的适配器实例（供 _prepare 按语言分发）。"""
        project = self.get(project_id)
        return self._build_adapter(Path(project.root_path).expanduser(),
                                   project.language, project.run_config)

    def run_plan(self, project_id: str, *, test_ids: list[str] | None = None) -> dict[str, Any]:
        """登记范围对应的固定命令（只读预览，不执行）。

        argv 由适配器构造；预览用临时 artifact 目录占位，真实执行时回执里
        记录实际 argv。
        """
        project = self.get(project_id)
        targets = self._resolve_targets(project, test_ids)
        with tempfile.TemporaryDirectory(prefix="modou-run-plan-") as scratch:
            spec = self.adapter_for(project_id).test_command(
                targets=targets, artifact_dir=Path(scratch))
        return {"argv": list(spec.argv), "cwd": str(spec.cwd),
                "network": bool(spec.network), "shell": bool(spec.shell),
                "timeout_seconds": int(project.run_config.get("timeout_seconds") or 300)}

    def _resolve_targets(self, project: RegisteredProject,
                         test_ids: list[str] | None) -> tuple[str, ...]:
        registered = list(project.run_config.get("test_targets") or [])
        if not test_ids:
            return tuple(registered)
        unknown = [item for item in test_ids if item not in registered]
        if unknown:
            raise ProjectRegistryError("RUN_SCOPE_NOT_REGISTERED",
                                       ",".join(sorted(set(unknown))))
        ordered = [item for item in registered if item in set(test_ids)]
        return tuple(ordered)

    # ------------------------------------------------------------ 环境诊断

    def diagnose(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id)
        root = Path(project.root_path).expanduser()
        if not root.is_dir():
            return {"project_id": project.project_id, "adapter_id": project.adapter_id,
                    "runtime_present": False, "runtime_version": "",
                    "missing": [f"root_path:{project.root_path}"],
                    "capabilities_ready": ["environment_probe"],
                    "probed_at": _utc_now()}
        try:
            adapter = self.adapter_for(project_id)
        except (ValueError, OSError) as exc:
            return {"project_id": project.project_id, "adapter_id": project.adapter_id,
                    "runtime_present": False, "runtime_version": "",
                    "missing": [f"adapter_discovery:{getattr(exc, 'code', 'failed')}"],
                    "capabilities_ready": ["environment_probe"],
                    "probed_at": _utc_now()}
        missing: list[str] = []
        versions: list[str] = []
        if project.language == "python":
            interpreter = project.run_config.get("python_interpreter") or shutil.which("python")
            if not interpreter:
                return self._diagnosis(project, False, "", ["python interpreter"])
            ok, version = _probe([str(interpreter), "--version"])
            if not ok:
                return self._diagnosis(project, False, "", [f"python interpreter ({interpreter})"])
            versions.append(version)
            for module in ("pytest", "pytest_jsonreport"):
                ok, _ = _probe([str(interpreter), "-c", f"import {module}"])
                if not ok:
                    missing.append(module)
            if "pytest" not in missing:
                _, pytest_version = _probe([str(interpreter), "-m", "pytest", "--version"])
                if pytest_version:
                    versions.append(pytest_version.split(";")[0])
        elif project.language == "typescript":
            runner = adapter.test_runner
            for tool in ("node", "npm"):
                if shutil.which(tool) is None:
                    missing.append(tool)
                else:
                    _, version = _probe([tool, "--version"])
                    versions.append(f"{tool} {version}" if version else tool)
            if not (root / "node_modules" / ".bin" / runner).is_file():
                missing.append(f"node_modules/.bin/{runner} (run prepare_dependencies)")
        elif project.language == "go":
            if shutil.which("go") is None:
                missing.append("go")
            else:
                _, version = _probe(["go", "version"])
                versions.append(version or "go")
        elif project.language == "java":
            for tool in ("java", "mvn"):
                if shutil.which(tool) is None:
                    missing.append(tool)
                else:
                    _, version = _probe([tool, "-version" if tool == "java" else "--version"])
                    versions.append(f"{tool} {version}" if version else tool)
        return self._diagnosis(project, not missing, "; ".join(v for v in versions if v),
                               missing)

    @staticmethod
    def _diagnosis(project: RegisteredProject, present: bool, version: str,
                   missing: list[str]) -> dict[str, Any]:
        ready = ["environment_probe"]
        if present:
            adapter_caps = ProjectRegistry._adapter_capabilities(project)
            if adapter_caps.execution and adapter_caps.stable_test_ids:
                ready.extend(["named_tests", "candidate_tests", "adoption_recheck"])
        return {"project_id": project.project_id, "adapter_id": project.adapter_id,
                "runtime_present": present, "runtime_version": version,
                "missing": missing, "capabilities_ready": ready,
                "probed_at": _utc_now()}

    @staticmethod
    def _adapter_capabilities(project: RegisteredProject) -> AdapterCapabilities:
        adapter = ProjectRegistry._build_adapter(Path(project.root_path).expanduser(),
                                                 project.language, project.run_config)
        return adapter.capabilities()

    def declare_capabilities(self) -> dict[str, Any]:
        """按登记项目汇总能力声明；真值来自 diagnose 实测。"""
        declared: dict[str, Any] = {}
        for project_id in sorted(self._projects):
            project = self.get(project_id)
            diagnosis = self.diagnose(project_id)
            declared[project_id] = {
                "adapter_id": project.adapter_id,
                "language": project.language,
                "capabilities": list(diagnosis["capabilities_ready"]),
                "capabilities_not_ready": [name for name in CAPABILITY_NAMES
                                           if name not in diagnosis["capabilities_ready"]],
                "runtime_present": diagnosis["runtime_present"],
                "runtime_version": diagnosis["runtime_version"],
                "missing": list(diagnosis["missing"]),
                "declared_at": _utc_now(),
            }
        return {"schema_version": "modou-capabilities-v1",
                "source_of_truth": "diagnose",
                "projects": declared}

    # ------------------------------------------------------------ 依赖准备（独立）

    def prepare_dependencies(self, project_id: str, *,
                             log_dir: str | Path | None = None) -> dict[str, Any]:
        """独立执行登记的依赖准备命令并封存记录；执行入口绝不调用本方法。"""
        project = self.get(project_id)
        command = project.run_config.get("prepare_command")
        record: dict[str, Any] = {
            "schema_version": PREPARE_RECORD_SCHEMA_VERSION,
            "project_id": project.project_id,
            "adapter_id": project.adapter_id,
            "command": list(command) if command else None,
            "cwd": project.root_path,
            "started_at": _utc_now(),
        }
        if not command:
            record.update({"status": "no_prepare_command", "exit_code": None,
                           "finished_at": _utc_now(),
                           "notes": "该项目未登记依赖准备命令（如 Go/Java 工具链属运行时，非依赖）"})
            return record
        started = time.monotonic()
        try:
            result = subprocess.run(list(command), cwd=project.root_path,
                                    capture_output=True, text=True, timeout=3600,
                                    env=sanitized_environment())
            exit_code: int | None = result.returncode
            stdout, stderr = result.stdout or "", result.stderr or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            exit_code, stdout, stderr = None, "", f"{type(exc).__name__}: {exc}"
        record.update({
            "exit_code": exit_code,
            "status": "pass" if exit_code == 0 else "fail",
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
            "finished_at": _utc_now(),
        })
        if log_dir is not None:
            target = Path(log_dir)
            target.mkdir(parents=True, exist_ok=True)
            (target / "prepare-stdout.log").write_text(stdout, encoding="utf-8")
            (target / "prepare-stderr.log").write_text(stderr, encoding="utf-8")
            (target / "prepare-record.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            record["log_dir"] = str(target)
        return record

    # ------------------------------------------------------------ 统一执行入口

    def execute_tests(self, project_id: str, test_ids: list[str] | None = None, *,
                      artifact_dir: str | Path | None = None,
                      executor: CommandExecutor | None = None) -> dict[str, Any]:
        project = self.get(project_id)
        # 范围校验先于诊断：选择未登记范围是配置错误，与环境无关。
        targets = self._resolve_targets(project, test_ids)
        diagnosis = self.diagnose(project_id)
        receipt: dict[str, Any] = {
            "schema_version": RUN_RECEIPT_SCHEMA_VERSION,
            "project_id": project.project_id,
            "adapter_id": project.adapter_id,
            "language": project.language,
            "timestamp_utc": _utc_now(),
            "diagnosis": diagnosis,
        }
        if not diagnosis["runtime_present"]:
            receipt.update({"executed": False, "outcome": "env_unavailable",
                            "missing": list(diagnosis["missing"]),
                            "notes": "运行时缺失；执行未发生，不算已验收。请先补齐环境或运行 prepare_dependencies。"})
            return receipt
        root = Path(project.root_path).expanduser()
        scratch = (Path(artifact_dir).expanduser() if artifact_dir is not None else
                   Path(tempfile.mkdtemp(prefix=f"modou-project-run-{project.project_id}-")))
        scratch.mkdir(parents=True, exist_ok=True)
        timeout = float(project.run_config.get("timeout_seconds") or 300)
        inner_executor = executor if executor is not None else _executor_for(project)
        if project.language == "python":
            inner_executor = _EnvAugmentedExecutor(
                inner_executor, {"PYTEST_ADDOPTS": "--json-report"})
        receipt["artifact_dir"] = str(scratch)
        started = time.monotonic()
        try:
            adapter = self.adapter_for(project_id)
            result = adapter.run_tests(executor=inner_executor, targets=targets,
                                       artifact_dir=scratch, timeout=timeout)
        except (ValueError, OSError, RuntimeError) as exc:
            receipt.update({"executed": False, "outcome": "error",
                            "error_code": str(getattr(exc, "code", type(exc).__name__)),
                            "error_detail": str(exc)[:2000]})
            return receipt
        statuses = [case.status for case in result.tests.cases]
        summary = {name: statuses.count(name) for name in ("passed", "failed", "skipped", "todo")}
        receipt.update({
            "executed": True,
            "outcome": "pass" if (result.returncode == 0
                                  and summary["failed"] == 0
                                  and summary["skipped"] == 0) else "fail",
            "argv": list(result.command.argv),
            "cwd": str(result.command.cwd),
            "returncode": result.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "tests": [{"test_id": case.test_id, "path": case.path, "name": case.name,
                       "status": case.status,
                       "duration_ms": case.duration_ms} for case in result.tests.cases],
            "summary": summary,
            "report_sha256": result.report_sha256,
            "artifacts": [str(path) for path in result.command.artifacts],
        })
        return receipt
