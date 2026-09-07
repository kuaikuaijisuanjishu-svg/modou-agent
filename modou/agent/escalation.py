"""Deterministic detection of material plan changes that invalidate approval."""
from __future__ import annotations

from .spec import ReviewSpec


def material_changes(before: ReviewSpec, after: ReviewSpec) -> tuple[str, ...]:
    changes = []
    if before.source != after.source:
        changes.append("source_or_workspace")
    if before.scope != after.scope:
        changes.append("file_or_test_scope")
    if before.constraints != after.constraints:
        changes.append("resource_or_network_budget")
    if before.autonomy_policy != after.autonomy_policy:
        changes.append("autonomy_or_risk")
    if before.data_policy != after.data_policy:
        changes.append("data_egress")
    if before.memory_snapshot_sha256 != after.memory_snapshot_sha256:
        changes.append("repository_memory")
    if before.model_policy != after.model_policy:
        changes.append("model_policy")
    return tuple(changes)


def requires_human(before: ReviewSpec, after: ReviewSpec) -> bool:
    return bool(material_changes(before, after))
