from __future__ import annotations

import json

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
