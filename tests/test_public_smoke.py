from __future__ import annotations

import json
import os
import shutil
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _tiny_repo(root: Path) -> Path:
    """A minimal git repo whose declared scope contains a skipped test."""
    repo = root / "tiny"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "import sys\n"
        "import pytest\n"
        "from pkg.calc import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(1, 2) == 3\n"
        "\n"
        "\n"
        '@pytest.mark.skipif(sys.platform != "win32", reason="windows only")\n'
        "def test_windows_only():\n"
        "    assert add(1, 1) == 2\n",
        encoding="utf-8")
    _git(repo, "init", "-q", ".")
    _git(repo, "config", "user.email", "smoke@example.invalid")
    _git(repo, "config", "user.name", "smoke")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    # The uncommitted change under review: new code plus a test that reaches it.
    (repo / "pkg" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
        encoding="utf-8")
    with (repo / "tests" / "test_calc.py").open("a", encoding="utf-8") as handle:
        handle.write("\n\ndef test_mul():\n"
                     "    from pkg.calc import mul\n"
                     "    assert mul(2, 3) == 6\n")
    return repo


def test_public_review_runs_against_a_repo_with_a_skipped_test():
    """A `skipif` in the declared scope must not abort the whole review.

    Real repositories skip on platform and optional dependencies constantly.
    Requiring every declared test to be `passed` rejected them at the baseline
    gate, before a single counterfactual experiment ran. Skipped tests stay in
    the declared scope and in every vector comparison; they simply cannot carry
    evidence, so the summary names them instead of failing the run.

    This also covers the public entry point itself: `analyze_patch` must reach
    the published pipeline, not a module that only exists in the private
    research workspace.
    """
    from modou.application import AnalysisRequest, ExecutionMode, analyze_patch

    root = Path(tempfile.mkdtemp())
    try:
        repo = _tiny_repo(root)
        handle = analyze_patch(AnalysisRequest(
            repo_path=repo, test_files=("tests/test_calc.py",),
            python=sys.executable, budget_seconds=180.0,
            mode=ExecutionMode.TRUSTED_LOCAL, goal="smoke", quiet=True))
        assert handle.ok, f"[{handle.failure_stage}] {handle.failure_detail}"
        summary = handle.summary
        assert summary["declared_tests"] == 3
        assert summary["baseline_skipped_tests"] == [
            "tests/test_calc.py::test_windows_only"]
        assert summary["evidence_bearing_tests"] == 2
        assert summary["by_label"]["承重"] >= 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repo_snapshot_changes_when_an_already_modified_file_is_edited_again():
    """`git status` text alone cannot bind the review target.

    Editing a file that was already modified leaves the porcelain line
    byte-identical, so a snapshot built only from the status text reports "no
    change". Both the approval gate and repair delivery refuse stale work using
    this digest, so it has to follow working-tree content.
    """
    from modou.server.control import _repo_snapshot

    root = Path(tempfile.mkdtemp())
    try:
        repo = root / "snap"
        repo.mkdir()
        (repo / "a.py").write_text("v1\n", encoding="utf-8")
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "smoke@example.invalid")
        _git(repo, "config", "user.name", "smoke")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")

        (repo / "a.py").write_text("edit one\n", encoding="utf-8")
        first = _repo_snapshot(repo)
        assert _repo_snapshot(repo)["snapshot_sha256"] == first["snapshot_sha256"]

        (repo / "a.py").write_text("edit two, quite different\n", encoding="utf-8")
        second = _repo_snapshot(repo)
        assert second["status_sha256"] == first["status_sha256"], (
            "the porcelain status is expected to be unchanged; that is the point")
        assert second["snapshot_sha256"] != first["snapshot_sha256"]

        (repo / "b.py").write_text("x = 1\n", encoding="utf-8")
        third = _repo_snapshot(repo)
        assert third["snapshot_sha256"] != second["snapshot_sha256"]
        (repo / "b.py").write_text("x = 2\n", encoding="utf-8")
        assert _repo_snapshot(repo)["snapshot_sha256"] != third["snapshot_sha256"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repair_generation_reads_a_data_policy_field_that_exists():
    """The candidate-generation gate must name a real DataPolicy field.

    `generate_repair` refuses to send snippets to a model unless the approved
    data categories include `selected_snippets`. Reading that from a field the
    dataclass does not define raised AttributeError instead, so the gate could
    never pass and candidate generation was unreachable.
    """
    from modou.agent.spec import DataPolicy

    policy = DataPolicy.parse({
        "model_data_categories": ["metadata", "selected_snippets"]})
    assert "selected_snippets" in policy.model_data_categories
    assert not hasattr(policy, "allowed_categories")

    source = (Path(__file__).resolve().parents[1] /
              "modou" / "server" / "control.py").read_text(encoding="utf-8")
    assert "data_policy.allowed_categories" not in source


def test_public_ref_resolves_on_a_pull_request_checkout():
    """The release check has to work on a branch, not only on main.

    A pull-request build checks out the merge ref with a single branch, so
    there is no local `main`, and an ordinary feature branch carries no merge
    commit to read a public baseline out of. Both assumptions were baked in,
    and the first pull request this repository ever received failed the check
    with a bare `CalledProcessError`.
    """
    import tools.public_release_check as check

    root = Path(tempfile.mkdtemp())
    try:
        repo = root / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main", ".")
        _git(repo, "config", "user.email", "smoke@example.invalid")
        _git(repo, "config", "user.name", "smoke")
        (repo / "a.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "base")
        main_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True).stdout.strip()
        # A linear branch off main, exactly the shape of a contributor's PR.
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "a.txt").write_text("changed\n", encoding="utf-8")
        _git(repo, "commit", "-qam", "change")
        branch_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True).stdout.strip()
        # The pull-request checkout has no local `main` at all.
        _git(repo, "branch", "-D", "main")

        original_root = check.ROOT
        try:
            check.ROOT = repo
            # Stands in for the remote-tracking ref a real CI checkout carries.
            assert check._pinned_public_ref(branch_head, main_head) == main_head
        finally:
            check.ROOT = original_root
    finally:
        shutil.rmtree(root, ignore_errors=True)
