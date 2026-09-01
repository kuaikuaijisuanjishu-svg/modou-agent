"""Deterministic delta debugging with explicit restoration and certification.

The algorithm proves *1-minimality relative to a frozen atomic universe and a
named target regression*.  It deliberately does not claim a globally minimum
set or semantic necessity outside the declared tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Callable, Protocol, Sequence


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNRESOLVED = "UNRESOLVED"


class ProbeStrategy(str, Enum):
    HDD_INSPIRED = "hdd_inspired"
    DDMIN = "ddmin"


@dataclass(frozen=True, order=True)
class AtomicUnit:
    """One disjoint, added-only statement in the frozen candidate universe."""

    unit_id: str
    path: str
    lines: tuple[int, ...]
    structural_path: str = ""

    def __post_init__(self) -> None:
        if not self.unit_id or not self.path or not self.lines:
            raise ValueError("atomic unit requires id, path and physical lines")
        if tuple(sorted(set(self.lines))) != self.lines or self.lines[0] < 1:
            raise ValueError("atomic unit lines must be sorted, unique and positive")


@dataclass(frozen=True)
class TrialResult:
    outcome: Outcome
    regressions: tuple[str, ...] = ()
    restored_clean: bool = True
    experiment_id: str = ""
    detail: str = ""


class DirtyRestore(RuntimeError):
    """Fail closed: no ddmin result is usable after a dirty restoration."""


class InvalidUniverse(ValueError):
    """A frozen atom set that ddmin may not run on.

    Carries a stable ``code`` because the reasons have different fixes and
    different meanings for the product: an empty universe is *applicability*
    ("this patch has no independently deletable added statement"), while a
    duplicate or overlapping id is a *defect*.  The 30-instance walk could only
    tell them apart after the fact; the router needs them apart up front.
    """

    EMPTY = "empty_universe"
    DUPLICATE = "duplicate_unit_ids"
    OVERLAP = "overlapping_units"

    def __init__(self, message: str, *, code: str = "invalid_universe"):
        super().__init__(message)
        self.code = code


class Oracle(Protocol):
    def __call__(self, units: tuple[AtomicUnit, ...]) -> TrialResult: ...


@dataclass(frozen=True)
class TrialRecord:
    units: tuple[str, ...]
    outcome: Outcome
    regressions: tuple[str, ...]
    experiment_id: str
    phase: str


@dataclass(frozen=True)
class RemovalCheck:
    removed_unit_id: str
    remaining_unit_ids: tuple[str, ...]
    outcome: Outcome
    regressions: tuple[str, ...] = ()
    experiment_id: str = ""


@dataclass(frozen=True)
class Certificate:
    schema_version: str
    target_regression: str
    frozen_unit_ids: tuple[str, ...]
    minimal_unit_ids: tuple[str, ...]
    initial_outcome: Outcome
    final_outcome: Outcome
    removal_checks: tuple[RemovalCheck, ...]
    one_minimal: bool
    scope_note: str = (
        "1-minimal only relative to the frozen atomic units, named regression, "
        "and declared test scope; not a globally minimum or uniquely causal set."
    )

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "target_regression": self.target_regression,
            "frozen_unit_ids": list(self.frozen_unit_ids),
            "minimal_unit_ids": list(self.minimal_unit_ids),
            "initial_outcome": self.initial_outcome.value,
            "final_outcome": self.final_outcome.value,
            "removal_checks": [
                {
                    "removed_unit_id": x.removed_unit_id,
                    "remaining_unit_ids": list(x.remaining_unit_ids),
                    "outcome": x.outcome.value,
                    "regressions": list(x.regressions),
                    "experiment_id": x.experiment_id,
                }
                for x in self.removal_checks
            ],
            "one_minimal": self.one_minimal,
            "scope_note": self.scope_note,
        }


@dataclass(frozen=True)
class DDMinResult:
    units: tuple[AtomicUnit, ...]
    trials: tuple[TrialRecord, ...]
    certificate: Certificate


def added_statement_units(source: str, *, path: str,
                          added_lines: set[int]) -> tuple[AtomicUnit, ...]:
    """Freeze finest non-overlapping added-only AST statements as ddmin atoms."""
    from . import astnodes
    from .mutate import InvalidTransform, delete

    roots = astnodes.build(source)
    eligible = []

    def visit(node) -> None:
        children = [child for child in node.children
                    if set(child.span) <= added_lines]
        if children:
            for child in children:
                visit(child)
            return
        if set(node.span) <= added_lines:
            try:
                delete(source, node.lines)
            except InvalidTransform:
                return
            eligible.append(node)

    for root in roots:
        visit(root)
    units = []
    source_lines = source.splitlines()
    for node in sorted(eligible, key=lambda x: (x.start, x.end, x.structural_path)):
        body = "\n".join(source_lines[node.start - 1:node.end])
        digest = hashlib.sha256(
            f"{path}\0{node.structural_path}\0{body}".encode("utf-8", "surrogateescape")
        ).hexdigest()[:16]
        units.append(AtomicUnit(digest, path, node.lines, node.structural_path))
    _validate_universe(tuple(units))
    return tuple(units)


# ---------------------------------------------------------------------------
# Applicability routing
#
# The frozen 30-instance walk (experiments/ddmin_applicability_report.json)
# produced 7 qualifying anchors out of 24 examined, and 14 of the 17 rejections
# were structural: the patch offered no independently deletable added-only
# statement, or offered exactly one.  Those are facts about *reach*, not
# failures, and they are knowable from the AST before a single test runs.
#
# So the decision is split in two, and the two are never reported as the same
# thing:
#
#   not_applicable  — decided here, statically, at zero experiment cost.
#   applicable      — ddmin may run; whether it then certifies anything is a
#                     separate, trial-costing outcome (see TRIAL_REASONS).
#
# "ddmin did not apply here, because ..." is an honest product answer.
# "ddmin failed" would not be.
# ---------------------------------------------------------------------------


class Routing(str, Enum):
    APPLICABLE = "applicable"
    NOT_APPLICABLE = "not_applicable"


#: Static rejections. Each costs zero experiments and names its own cause.
WHOLE_FILE_CANDIDATE = "whole_file_candidate"
NO_ADDED_LINE = "no_added_line"
NO_DELETABLE_ADDED_STATEMENT = "no_deletable_added_statement"
DUPLICATE_UNIT_IDS = "duplicate_unit_ids"
OVERLAPPING_UNITS = "overlapping_units"
SINGLE_ATOM = "single_atom"
SOURCE_UNREADABLE = "source_unreadable"
NO_NAMED_REGRESSION = "no_named_regression"

STATIC_REASONS: frozenset[str] = frozenset({
    WHOLE_FILE_CANDIDATE, NO_ADDED_LINE, NO_DELETABLE_ADDED_STATEMENT,
    DUPLICATE_UNIT_IDS, OVERLAPPING_UNITS, SINGLE_ATOM, SOURCE_UNREADABLE,
    NO_NAMED_REGRESSION,
})

#: Outcomes only reachable *after* ddmin was routed in and spent experiments.
#: They mean "applicable but unproven", which is not the same as "not applicable".
TRIAL_REASONS: frozenset[str] = frozenset({
    "initial_pass", "initial_unresolved", "not_minimizable",
    "certificate_incomplete", "source_changed_since_freeze",
})

_UNIVERSE_CODE_REASONS = {
    InvalidUniverse.EMPTY: NO_DELETABLE_ADDED_STATEMENT,
    InvalidUniverse.DUPLICATE: DUPLICATE_UNIT_IDS,
    InvalidUniverse.OVERLAP: OVERLAPPING_UNITS,
}


@dataclass(frozen=True)
class RouteDecision:
    """Why ddmin will or will not run on one anchor, decided before any trial."""

    routing: Routing
    reason: str
    detail: str = ""
    units: tuple[AtomicUnit, ...] = ()
    source_sha256: str = ""

    def __post_init__(self) -> None:
        if self.routing is Routing.APPLICABLE:
            if self.reason != Routing.APPLICABLE.value:
                raise ValueError("an applicable route carries no rejection reason")
            if len(self.units) < 2:
                raise ValueError("an applicable route needs at least two atoms")
        elif self.reason not in STATIC_REASONS:
            raise ValueError(f"unknown static routing reason: {self.reason}")

    @property
    def applicable(self) -> bool:
        return self.routing is Routing.APPLICABLE

    @property
    def unit_ids(self) -> tuple[str, ...]:
        return tuple(x.unit_id for x in self.units)

    def to_json(self) -> dict:
        return {
            "schema_version": "ddmin-route-v1",
            "routing": self.routing.value,
            "reason": self.reason,
            "detail": self.detail,
            "frozen_unit_ids": list(self.unit_ids),
            "experiments_spent": 0,
        }


def route(source: str, *, path: str, added_lines: set[int],
          whole_file: bool = False, has_named_regression: bool = True
          ) -> RouteDecision:
    """Decide ddmin applicability for one anchor without running any test.

    ``whole_file`` marks a brand-new-file candidate: its only atom is the file
    itself, and `trial_runner_oracle` is a single-file oracle by contract, so
    there is nothing for ddmin to narrow.  ``has_named_regression`` is false
    when the ordinary probe produced no regression — ddmin refines a finding,
    it never manufactures one.
    """
    if not has_named_regression:
        return RouteDecision(Routing.NOT_APPLICABLE, NO_NAMED_REGRESSION,
                             "ddmin narrows an existing named regression")
    if whole_file:
        return RouteDecision(Routing.NOT_APPLICABLE, WHOLE_FILE_CANDIDATE,
                             "a whole new file has no finer added-only atom")
    if not added_lines:
        return RouteDecision(Routing.NOT_APPLICABLE, NO_ADDED_LINE,
                             "the anchor contributes no added line")
    try:
        units = added_statement_units(source, path=path, added_lines=set(added_lines))
    except InvalidUniverse as exc:
        reason = _UNIVERSE_CODE_REASONS.get(getattr(exc, "code", ""),
                                            NO_DELETABLE_ADDED_STATEMENT)
        return RouteDecision(Routing.NOT_APPLICABLE, reason, str(exc)[:200])
    except (ValueError, SyntaxError) as exc:
        return RouteDecision(Routing.NOT_APPLICABLE, NO_DELETABLE_ADDED_STATEMENT,
                             f"{type(exc).__name__}: {exc}"[:200])
    digest = hashlib.sha256(source.encode("utf-8", "surrogateescape")).hexdigest()
    if len(units) < 2:
        # One atom is already minimal; a certificate over it would prove nothing.
        return RouteDecision(Routing.NOT_APPLICABLE, SINGLE_ATOM,
                             "a single atom is minimal by construction",
                             units, digest)
    return RouteDecision(Routing.APPLICABLE, Routing.APPLICABLE.value,
                         f"{len(units)} independently deletable added statements",
                         units, digest)


def unreadable_route(detail: str) -> RouteDecision:
    """The anchor's source could not be read, so applicability is undecided."""
    return RouteDecision(Routing.NOT_APPLICABLE, SOURCE_UNREADABLE, detail[:200])


def routing_summary(decisions: "dict[str, RouteDecision]") -> dict:
    """Aggregate a plan-time routing table for the report and the cockpit.

    Reported as "ddmin applies to N of M candidates", never as a success rate:
    the denominator is candidates examined, not anchors ddmin attempted.
    """
    applicable = sorted(k for k, v in decisions.items() if v.applicable)
    reasons: dict[str, int] = {}
    for decision in decisions.values():
        if not decision.applicable:
            reasons[decision.reason] = reasons.get(decision.reason, 0) + 1
    return {
        "schema_version": "ddmin-routing-summary-v1",
        "candidates": len(decisions),
        "applicable": len(applicable),
        "applicable_anchor_ids": applicable,
        "not_applicable_reasons": dict(sorted(reasons.items())),
        "experiments_spent": 0,
        "scope_note": (
            "Structural applicability only: whether this patch offers two or "
            "more independently deletable added-only statements. ddmin still "
            "runs only on anchors that a probe has already shown load-bearing, "
            "so this is neither a quality score nor a ddmin success rate."),
    }


def certificate_problems(certificate: Certificate) -> list[str]:
    """Check the closed-world proof shape without trusting its prose."""
    problems: list[str] = []
    frozen = set(certificate.frozen_unit_ids)
    minimal = set(certificate.minimal_unit_ids)
    if not minimal or not minimal <= frozen:
        problems.append("minimal units must be a non-empty subset of frozen units")
    if certificate.initial_outcome is not Outcome.FAIL:
        problems.append("frozen full set did not reproduce target")
    if certificate.final_outcome is not Outcome.FAIL:
        problems.append("reported minimal set did not reproduce target")
    removed = {check.removed_unit_id for check in certificate.removal_checks}
    if removed != minimal:
        problems.append("one removal check is required for every minimal unit")
    for check in certificate.removal_checks:
        if check.outcome is not Outcome.PASS:
            problems.append(f"removing {check.removed_unit_id} was not a conclusive PASS")
        expected = minimal - {check.removed_unit_id}
        if set(check.remaining_unit_ids) != expected:
            problems.append(f"removal check for {check.removed_unit_id} used the wrong set")
    if certificate.one_minimal != (not problems):
        problems.append("one_minimal flag disagrees with certificate evidence")
    return problems


def _partition(items: tuple[AtomicUnit, ...], n: int) -> list[tuple[AtomicUnit, ...]]:
    """Split deterministically into n non-empty, almost equally sized chunks."""
    n = max(1, min(n, len(items)))
    width, extra = divmod(len(items), n)
    out: list[tuple[AtomicUnit, ...]] = []
    start = 0
    for i in range(n):
        size = width + (1 if i < extra else 0)
        out.append(items[start:start + size])
        start += size
    return out


def _complement(items: tuple[AtomicUnit, ...], subset: Sequence[AtomicUnit]
                ) -> tuple[AtomicUnit, ...]:
    removed = {x.unit_id for x in subset}
    return tuple(x for x in items if x.unit_id not in removed)


def _validate_universe(units: tuple[AtomicUnit, ...]) -> None:
    ids = [u.unit_id for u in units]
    # Kept apart: "the patch offered no deletable added-only statement" and
    # "two atoms hashed to the same id" are different findings with different
    # fixes; one message for both would make failures hard to diagnose.
    if not units:
        raise InvalidUniverse(
            "frozen universe is empty: no deletable added-only statement",
            code=InvalidUniverse.EMPTY)
    if len(ids) != len(set(ids)):
        duplicates = sorted({x for x in ids if ids.count(x) > 1})
        raise InvalidUniverse(f"frozen universe has duplicate unit ids: {duplicates}",
                              code=InvalidUniverse.DUPLICATE)
    occupied: dict[tuple[str, int], str] = {}
    for unit in units:
        for line in unit.lines:
            key = (unit.path, line)
            if key in occupied:
                raise InvalidUniverse(
                    f"atomic units {occupied[key]!r} and {unit.unit_id!r} overlap at "
                    f"{unit.path}:{line}", code=InvalidUniverse.OVERLAP)
            occupied[key] = unit.unit_id


def minimize(units: Sequence[AtomicUnit], oracle: Oracle, *,
             target_regression: str) -> DDMinResult:
    """Run classic ddmin and emit an independently checkable 1-minimal proof.

    The oracle must classify a set as FAIL only when the *same named target*
    regresses.  This function enforces that contract and rejects dirty restores.
    UNRESOLVED never counts as a successful reduction and prevents certification
    when it occurs in the final one-element-removal checks.
    """
    if not target_regression:
        raise ValueError("a named target regression is required")
    frozen = tuple(units)
    _validate_universe(frozen)
    log: list[TrialRecord] = []

    def test(candidate: tuple[AtomicUnit, ...], phase: str) -> TrialResult:
        result = oracle(candidate)
        if not isinstance(result, TrialResult):
            raise TypeError("ddmin oracle must return TrialResult")
        if not result.restored_clean:
            raise DirtyRestore(result.detail or "trial did not restore the workspace")
        outcome = result.outcome
        if outcome is Outcome.FAIL and target_regression not in result.regressions:
            outcome = Outcome.UNRESOLVED
            result = TrialResult(outcome, result.regressions, True,
                                 result.experiment_id,
                                 "failure did not reproduce the frozen target")
        log.append(TrialRecord(tuple(x.unit_id for x in candidate), outcome,
                               result.regressions, result.experiment_id, phase))
        return result

    initial = test(frozen, "initial")
    if initial.outcome is not Outcome.FAIL:
        raise InvalidUniverse("the frozen full set must reproduce the named regression")

    current = frozen
    n = 2
    while len(current) >= 2:
        subsets = _partition(current, n)
        reduced = False
        for subset in subsets:
            if test(subset, "subset").outcome is Outcome.FAIL:
                current = subset
                n = max(2, n - 1)
                reduced = True
                break
        if reduced:
            continue
        for subset in subsets:
            complement = _complement(current, subset)
            if complement and test(complement, "complement").outcome is Outcome.FAIL:
                current = complement
                n = max(2, n - 1)
                reduced = True
                break
        if reduced:
            continue
        if n >= len(current):
            break
        n = min(len(current), n * 2)

    final = test(current, "certificate_final")
    checks: list[RemovalCheck] = []
    one_minimal = final.outcome is Outcome.FAIL
    for removed in current:
        smaller = tuple(x for x in current if x.unit_id != removed.unit_id)
        result = test(smaller, "certificate_remove_one")
        checks.append(RemovalCheck(
            removed.unit_id, tuple(x.unit_id for x in smaller), result.outcome,
            result.regressions, result.experiment_id))
        # PASS is required: UNRESOLVED is absence of proof, never a certificate.
        one_minimal = one_minimal and result.outcome is Outcome.PASS

    certificate = Certificate(
        schema_version="ddmin-certificate-v1",
        target_regression=target_regression,
        frozen_unit_ids=tuple(x.unit_id for x in frozen),
        minimal_unit_ids=tuple(x.unit_id for x in current),
        initial_outcome=initial.outcome,
        final_outcome=final.outcome,
        removal_checks=tuple(checks),
        one_minimal=one_minimal,
    )
    return DDMinResult(current, tuple(log), certificate)


def trial_runner_oracle(runner, *, anchor, deadline: Callable[[], float],
                        target_regression: str,
                        spend: Callable[[], None] | None = None) -> Oracle:
    """Adapt the existing TrialRunner for a frozen single-file ddmin universe.

    Multi-file intervention sets are reported UNRESOLVED rather than partially
    applied.  The generic ddmin core remains multi-file capable when supplied an
    atomic multi-file oracle by the application layer.
    """
    from .trial import INVALID, RAN, TIMEOUT, TrialRestoreDirty

    counter = 0

    def probe(units: tuple[AtomicUnit, ...]) -> TrialResult:
        nonlocal counter
        counter += 1
        if not units:
            # Empty intervention is the already-established green baseline.
            return TrialResult(Outcome.PASS, restored_clean=True,
                               detail="empty intervention equals baseline")
        paths = {u.path for u in units}
        if len(paths) != 1:
            return TrialResult(Outcome.UNRESOLVED, detail="multi-file set requires atomic application oracle")
        lines = tuple(sorted({line for u in units for line in u.lines}))
        try:
            out = runner.delete_lines(
                next(iter(paths)), lines, anchor=anchor, deadline=deadline(),
                junit_name=f"ddmin__{counter:04d}.xml", on_spend=spend)
        except TrialRestoreDirty as exc:
            raise DirtyRestore(str(exc)) from exc
        regressions = tuple(r[0] for r in out.vector.regressions(runner.baseline)) \
            if out.vector is not None else ()
        if out.status == RAN:
            outcome = Outcome.FAIL if target_regression in regressions else Outcome.PASS
        elif out.status in {INVALID, TIMEOUT}:
            outcome = Outcome.UNRESOLVED
        else:
            outcome = Outcome.UNRESOLVED
        return TrialResult(outcome, regressions, out.status != "DIRTY",
                           out.experiment_id, out.abort_reason)

    return probe
