from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from modou.executor import TrustedLocalExecutor, sanitized_environment
from modou.review_bundle import build_review_bundle_v2


def test_public_bundle_removes_private_material():
    bundle = build_review_bundle_v2(
        review_id="review-test-id",
        request={
            "repo_path": "/Users/example/private/repo",
            "source": {"repo_id": "opaque-private-id"},
        },
        plan={"requested_tools": ["pytest"]},
        events=[{
            "seq": 1,
            "kind": "review.completed",
            "data": {"report": "/private/tmp/claude-501/-Users-example-Desktop-private/run/report.json"},
        }],
        scheduler_trace=[{"command": "pytest tests/test_private.py", "stdout": "secret response"}],
        evidence_run_id="run-test-id",
        evidence_bundle={
            "run_id": "run-test-id",
            "report": {"manifest": {"tool_commit": "a" * 40}},
            "lines": [{"path": "private.py", "text": "def hidden(): return 'secret'"}],
            "raw_model_response": "private model output",
        },
        narration={"summary": "公开摘要"},
        provider={"kind": "deterministic", "endpoint": "https://private.example.test/v1"},
        model_metrics={},
        evidence_valid=True,
    )
    rendered = json.dumps(bundle, ensure_ascii=False)
    assert "/Users/" not in rendered
    assert "-Users-example-" not in rendered
    assert "hidden" not in rendered
    assert "private model output" not in rendered
    assert "private.example.test" not in rendered
    policy = bundle["evaluation_context"]["resource_policy"]
    assert policy["timeout_cleanup"] == "process_group"
    assert policy["memory_limit"] == "not_enforced"
    assert policy["process_count_limit"] == "not_enforced"


def test_public_executor_kills_timeout_process_group():
    if os.name == "nt":
        return
    root = Path(tempfile.mkdtemp())
    pid_file = root / "child.pid"
    code = (
        "import subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); time.sleep(30)"
    )
    try:
        try:
            TrustedLocalExecutor().run(
                [sys.executable, "-c", code], cwd=root, timeout=0.5,
                env=sanitized_environment())
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("timeout must raise TimeoutExpired")
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("timed-out child process is still alive")
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
