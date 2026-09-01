"""The one vocabulary for the three mode fields every public artifact carries.

Before this module the same three enums were spelled four different ways —
the JSON Schema said ``coverage_first`` while the Bundle validator said
``coverage-first``; the validator accepted the Chinese display strings while
the schema did not; the Auditor had its own ``offline_replay`` / ``sandbox`` /
``trusted``.  The practical effect was that a scheduler mode could not be
emitted at all: underscore passed the schema and failed the validator, hyphen
did the reverse.  A replay run was worse than that, because ``isolation_mode``
legitimately becomes ``replay`` and the validator rejected it outright.

So the values live here once and everything imports them.  Writing the same
constant in two files is how the vocabularies drifted apart in the first place;
`tests/test_bundle_v2.py` reads the JSON Schema and compares it against this
module rather than restating either.

Machine values only.  Chinese display text belongs to the frontend
(`web/src/presentation.ts`), never to an artifact field, so that a stored
Bundle stays readable by a verifier that has no display layer.
"""
from __future__ import annotations

#: Whether repository code actually ran, or an existing bundle was replayed.
EXECUTION_MODES = frozenset({"live", "replay"})

#: Who chose the probe order.  The deterministic strategies cost no model call.
SCHEDULER_MODES = frozenset({"model", "fifo", "coverage_first", "cost_first"})

#: How far the executed code was held away from the host.  ``replay`` is a
#: member because replaying executes nothing at all, which is the strongest
#: isolation available and must not be reported as `trusted_local`.
ISOLATION_MODES = frozenset({"sandboxed", "trusted_local", "ci_ephemeral",
                             "replay"})

#: The deterministic strategies, i.e. every scheduler that needs no model.
DETERMINISTIC_SCHEDULERS = frozenset({"fifo", "coverage_first", "cost_first"})

#: Public default when no model is steering. It is deterministic, uses only
#: baseline coverage already collected by the run, and costs no model call.
DEFAULT_DETERMINISTIC_SCHEDULER = "coverage_first"


def execution_mode_for(isolation_mode: str) -> str:
    """Derive live-vs-replay from isolation, instead of hardcoding it.

    `_mode_fields` used to return the literal ``"live"`` for every bundle, so a
    replay run reported that it had executed code. The two fields are not
    independent: replaying is exactly the case where nothing ran.
    """
    return "replay" if isolation_mode == "replay" else "live"
