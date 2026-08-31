"""Run the deterministic local review pipeline and build its evidence report."""
from __future__ import annotations

from pathlib import Path

from modou import inputs, label as label_mod

TOTAL_BUDGET = 300.0


class InstanceFailed(RuntimeError):
    def __init__(self, stage: str, detail: str):
        super().__init__(f"[{stage}] {detail}")
        self.stage, self.detail = stage, detail


def analyze(*args, **kwargs):
    raise ValueError("The public package accepts local repositories through analyze_patch")


def analyze_resolved(resolved: "inputs.ResolvedInput", *, quiet: bool = False,
                     three_state: bool = label_mod.THREE_STATE_DEFAULT,
                     out_root: Path | None = None,
                     freeze_sha256: str = "", official: bool = False,
                     unofficial_reason: str = "",
                     total_budget: float = TOTAL_BUDGET,
                     run_metadata: dict | None = None,
                     event_sink=None):
    """Run one resolved local input through the public review pipeline."""
    from modou.agent.session import AnalysisSession, SessionError

    session = AnalysisSession(
        resolved, quiet=quiet, three_state=three_state, out_root=out_root,
        freeze_sha256=freeze_sha256, official=official,
        unofficial_reason=unofficial_reason, total_budget=total_budget,
        run_metadata=run_metadata, event_sink=event_sink)
    try:
        return session.run_all()
    except SessionError as exc:
        raise InstanceFailed(exc.stage, exc.detail) from exc
