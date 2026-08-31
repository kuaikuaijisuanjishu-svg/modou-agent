"""哪些文件允许做节点删除探测。

方案 §6.3 的默认禁区：测试文件和 conftest.py；test_patch 修改的文件；
配置、依赖、CI、迁移和生成文件；文档、数据文件和二进制文件。

这些文件的行**仍然可以命中游离**（游离靠的是结构证据，不是删除），
也仍然可以拿无据（覆盖率对它们一样有话说），
但**永远不允许通过删测试获得惰性标签**——那就是删测试刷绿。
"""
from __future__ import annotations

import posixpath
import re

_TEST_DIRS = {"tests", "test", "testing", "_test"}
_CONFIG_NAMES = {
    "setup.py", "setup.cfg", "pyproject.toml", "tox.ini", "MANIFEST.in",
    "Makefile", "conftest.py", "noxfile.py", ".pre-commit-config.yaml",
}
_CONFIG_PREFIX = (".github/", ".circleci/", "ci/", "doc/", "docs/",
                  "bin/", "scripts/", "utils/")
_DOC_DATA_EXT = {".md", ".rst", ".txt", ".cfg", ".ini", ".toml", ".yaml",
                 ".yml", ".json", ".po", ".pot", ".html", ".css", ".js"}
_GENERATED = re.compile(r"(_pb2|_generated|\.gen)\.py$")


def is_test_shaped(path: str) -> bool:
    base = posixpath.basename(path)
    parts = path.split("/")[:-1]
    return (base.startswith("test_") or base.endswith("_test.py")
            or base == "conftest.py"
            or any(p in _TEST_DIRS for p in parts))


def is_config_like(path: str) -> bool:
    base = posixpath.basename(path)
    if base in _CONFIG_NAMES:
        return True
    if base.startswith("requirements") and base.endswith(".txt"):
        return True
    if path.startswith(_CONFIG_PREFIX):
        return True
    return bool(_GENERATED.search(path))


def is_doc_or_data(path: str) -> bool:
    ext = posixpath.splitext(path)[1].lower()
    return ext in _DOC_DATA_EXT


def probe_allowed(path: str, test_patch_files: set[str]) -> tuple[bool, str]:
    """允不允许对这个文件做节点删除。返回 (允许, 不允许的理由)。"""
    if path in test_patch_files:
        return False, "test_patch 修改的文件"
    if not path.endswith(".py"):
        return False, "非 Python 文件"
    if is_test_shaped(path):
        return False, "测试文件或 conftest.py"
    if is_config_like(path):
        return False, "配置/依赖/CI/生成文件"
    if is_doc_or_data(path):
        return False, "文档或数据文件"
    return True, ""


def in_package(path: str, package_root: str) -> bool:
    return path == package_root or path.startswith(package_root + "/")
