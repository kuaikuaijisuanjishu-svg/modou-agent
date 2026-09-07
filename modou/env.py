"""Shell-safe environment names with compatibility for the old local spelling."""
from __future__ import annotations

import os

PREFIX = "SHUIMU_YANMA_"
LEGACY_PREFIX = "SHUIMU YANMA_"


def get(name: str, default: str = "") -> str:
    """Read a setting, preferring the shell-safe spelling."""
    suffix = name
    if suffix.startswith(PREFIX):
        suffix = suffix[len(PREFIX):]
    elif suffix.startswith(LEGACY_PREFIX):
        suffix = suffix[len(LEGACY_PREFIX):]
    for key in (PREFIX + suffix, LEGACY_PREFIX + suffix):
        value = os.environ.get(key)
        if value is not None:
            return value
    return default


def names(suffix: str) -> tuple[str, str]:
    """Return current and legacy names without exposing their values."""
    suffix = suffix.removeprefix(PREFIX).removeprefix(LEGACY_PREFIX)
    return PREFIX + suffix, LEGACY_PREFIX + suffix
