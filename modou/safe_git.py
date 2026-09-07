"""Deterministic Git invocation for untrusted repository workflows.

Repository configuration is input, not authority.  Every control-plane Git
command therefore ignores system/global configuration, disables hooks,
credential helpers, fsmonitor and signing, and refuses interactive prompts.
The caller still has to reject repository-local filters before checkout; Git
cannot disable an arbitrary filter name without first knowing that name.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


SAFE_GIT_CONFIG = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "submodule.recurse=false",
    "-c", "protocol.file.allow=never",
    "-c", "user.name=Shuimu Yanma",
    "-c", "user.email=shuimu@local.invalid",
)

_DROP_ENV = frozenset({
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_ASKPASS", "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_SYSTEM", "GIT_DIR", "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF",
    "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_SSH", "GIT_SSH_COMMAND",
    "GIT_WORK_TREE", "SSH_ASKPASS", "SSH_AUTH_SOCK",
})


def safe_git_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a non-interactive environment with no inherited Git indirection."""
    env = dict(os.environ if source is None else source)
    for key in _DROP_ENV:
        env.pop(key, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GCM_INTERACTIVE": "Never",
    })
    return env


def git_argv(args: Sequence[str]) -> list[str]:
    return ["git", *SAFE_GIT_CONFIG, *map(str, args)]


def run_git(args: Sequence[str], *, cwd: Path, timeout: float = 120,
            input: str | bytes | None = None, text: bool = True,
            check: bool = False) -> subprocess.CompletedProcess:
    """Run Git with the closed configuration used by the review control plane."""
    return subprocess.run(
        git_argv(args), cwd=str(cwd), capture_output=True, text=text,
        timeout=timeout, input=input, check=check, env=safe_git_env())
