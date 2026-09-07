#!/usr/bin/env python3
"""Run only the tests that belong to the sanitized public package."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", str(ROOT / "tests" / "test_public_smoke.py")]))
