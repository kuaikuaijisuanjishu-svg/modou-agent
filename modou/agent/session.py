"""Incremental deterministic evidence session behind the eight typed tools.

The evidence engines remain deterministic.  This class only exposes safe phase
boundaries so the product workflow can pause for approval and, after a real
observation, choose whether to continue or stop.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any

from modou import (budget, certificate, coverage as cov_mod, diffstat, filekinds,
                   label as label_mod, ledger, paths, render, runroot, trial)
from modou.adapters import declared_tests, map_test_ids, TestIdError
from modou import ddmin as ddmin_mod
from modou.engines import drift, hdd, unevidenced
from modou.gitinfo import current_tool_commit
from modou.models import (EvidenceUnit, RunManifest, TestStatus,
                          TestVector, sha256_text)
from modou.testrange import (collect_nodeids, JUnitUnusable, run_declared,
                             TestRangeError)
from modou.workspace import prepare, WorkspaceError

from .models import CandidateSummary, Observation, StopReason, ToolRisk


def probe_cost_units(*, new_file: bool, pending: int) -> int:
    """Declared-test-range runs one probe needs, read off the two probe paths.

    A whole-new-file candidate goes through `drift.evaluate`: one collection
    comparison plus one declared-range run, so two units.  A statement
    candidate goes through `hdd.run`, whose group search over the pending
    lines costs about ``ceil(log2(pending + 1))`` runs, at least one.

    Derived from the code paths, never fitted to the measured `cost_s`.  The
    scheduler must not see a probe's real duration before it runs it, so this
    estimate may only use pre-probe observables.
    """
    if new_file:
        return 2
    return max(1, math.ceil(math.log2(pending + 1)))


class SessionError(RuntimeError):
    def __init__(self, stage: str, detail: str):
        super().__init__(f"[{stage}] {detail}")
        self.stage = stage
        self.detail = detail


def baseline_partition(statuses: dict) -> tuple[list, list]:
    """把基线状态分成「拦下来的」和「退出分母的」两堆。

    跳过不是失败。真实仓库按平台、可选依赖和外部服务跳过测试是常态，把它
    算作基线不绿，等于宣布大多数真实项目构建不起来。跳过的项同时退出声明
    分母：一个没有执行过的测试无从回归，留在分母里只会让「无据」看起来更多，
    却没有任何证据支撑它。

    失败、错误与 MISSING 仍然一律拦下——那些是真的不知道发生了什么。
    """
    bad = [(t, st.value) for t, st in sorted(statuses.items())
           if st not in (TestStatus.PASSED, TestStatus.SKIPPED)]
    skipped = sorted(t for t, st in statuses.items() if st is TestStatus.SKIPPED)
    return bad, skipped


class SessionPhase(str, Enum):
    CREATED = "CREATED"
    REPO_INSPECTED = "REPO_INSPECTED"
    PATCH_INSPECTED = "PATCH_INSPECTED"
    UNIVERSE_FROZEN = "UNIVERSE_FROZEN"
    TEST_SCOPE_COLLECTED = "TEST_SCOPE_COLLECTED"
    BASELINE_COMPLETE = "BASELINE_COMPLETE"
    PROBING = "PROBING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


_RISK_ALLOWED = {
    "inspect_repo": ToolRisk.READ_ONLY,
    "inspect_patch": ToolRisk.READ_ONLY,
    "build_eligible_universe": ToolRisk.READ_ONLY,
    "collect_test_scope": ToolRisk.EXECUTES_REPO,
    "run_baseline": ToolRisk.EXECUTES_REPO,
    "counterfactual_probe": ToolRisk.MUTATES,
    "read_evidence": ToolRisk.READ_ONLY,
    "finish_run": ToolRisk.READ_ONLY,
}


class AnalysisSession:
    """One resumable-in-process analysis, driven only through ToolRouter."""

    def __init__(self, resolved, *, quiet: bool = False,
                 three_state: bool = label_mod.THREE_STATE_DEFAULT,
                 out_root: Path | None = None, freeze_sha256: str = "",
                 official: bool = False, unofficial_reason: str = "",
                 total_budget: float = 300.0,
                 run_metadata: dict | None = None,
                 event_sink=None,
                 probe_strategy: str = ddmin_mod.ProbeStrategy.HDD_INSPIRED.value):
        self.resolved = resolved
        self.quiet = quiet
        self.three_state = three_state
        self.out_root = Path(out_root) if out_root is not None else None
        self.freeze_sha256 = freeze_sha256
        self.official = official
        self.unofficial_reason = unofficial_reason
        self.total_budget = float(total_budget)
        self.run_metadata = dict(run_metadata or {})
        self.event_sink = event_sink
        # ddmin never replaces labelling: the three-state projection and every
        # EvidenceUnit still come from `hdd.run`. When selected it runs as an
        # extra pass over anchors that already produced a named regression, and
        # narrows them to a 1-minimal statement set. That keeps the published
        # verdicts identical whichever strategy is chosen.
        self.probe_strategy = ddmin_mod.ProbeStrategy(probe_strategy)
        self.minimizations: list[dict] = []
        # Applicability is decided once, at baseline, before any probe —
        # see `_route_ddmin`. Anchors ddmin cannot reach are named as such
        # instead of being run and reported as failures.
        self.ddmin_routes: dict[str, ddmin_mod.RouteDecision] = {}
        self.ddmin_routing: dict = {}
        self.phase = SessionPhase.CREATED
        self.t_start = time.time()

        self.instance_id = resolved.instance_id
        self.meta = resolved.meta
        self.ai_patch = resolved.ai_patch
        self.adapter = resolved.adapter
        self.scaffold = resolved.scaffold or resolved.slug
        self.slug = f"{resolved.slug}__{self.instance_id}"
        self.run_dir = ((self.out_root / self.slug) if self.out_root is not None
                        else paths.run_dir(self.slug))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = RunManifest(
            instance_id=self.instance_id, repo=self.meta["repo"],
            base_commit=self.meta["base_commit"], scaffold=self.scaffold,
            patch_sha256=sha256_text(self.ai_patch),
            test_patch_sha256=sha256_text(self.meta.get("test_patch") or ""),
            tool_commit=current_tool_commit(), adapter_version=self.adapter.version,
            contaminated=self.instance_id in paths.CONTAMINATED,
            freeze_sha256=freeze_sha256, official=official,
            unofficial_reason=unofficial_reason)

        self.ws = None
        #: 基线里被跳过、因而退出声明分母的测试项。
        self.skipped_excluded: list[str] = []
        self.declared: list[str] = []
        self.test_files: list[str] = []
        self.collected: list[str] = []
        self.nodeids: list[str] = []
        self.added: dict = {}
        self.new_files: set[str] = set()
        self.tp_files: set[str] = set()
        self.universe: tuple[str, ...] = ()
        self.completed: set[str] = set()
        self.base = None
        self.cov = None
        self.runner = None
        self.bud = None
        self.recs: list = []
        self.units: list[EvidenceUnit] = []
        self.base_lines: list = []
        self.per_file: dict[str, list] = {}
        self.drift_files: set[str] = set()
        self.drift_rejected: dict[str, str] = {}
        self._result: dict | None = None
        self.evidence_root = runroot.RunRoot(
            self.run_dir, official=False, unofficial_reason="review_evidence")
        self.evidence_started = False

    def _emit(self, kind: str, data: dict | None = None) -> None:
        if self.event_sink is not None:
            self.event_sink(kind, dict(data or {}))

    def _expect(self, *phases: SessionPhase) -> None:
        if self.phase not in phases:
            raise SessionError("tool_order", f"phase={self.phase.value}, expected={[p.value for p in phases]}")

    def _fail(self, stage: str, detail: str, exc: Exception | None = None):
        self.phase = SessionPhase.FAILED
        self._emit("run.failed", {"stage": stage, "detail": detail[:400]})
        if self.evidence_started:
            try:
                self.evidence_root.finish(False, reason=f"{stage}:{detail[:200]}")
            except Exception:
                pass
        err = SessionError(stage, detail[:400])
        if exc is not None:
            raise err from exc
        raise err

    def inspect_repo(self) -> dict:
        self._expect(SessionPhase.CREATED)
        try:
            self.declared = list(declared_tests(self.meta))
            self.test_files = list(self.resolved.test_files)
            if not self.test_files:
                self._fail("test_files", "定位不到声明测试文件")
        except TestIdError as exc:
            self._fail("prepare", str(exc)[:200], exc)
        self.phase = SessionPhase.REPO_INSPECTED
        data = {
            "repo": self.meta.get("repo", ""),
            "base_commit": self.meta.get("base_commit", ""),
            "adapter": self.adapter.repo,
            "languages": sorted({
                {".py": "Python", ".js": "JavaScript", ".ts": "TypeScript"}.get(
                    Path(path).suffix.lower(), Path(path).suffix.lower().lstrip(".") or "unknown")
                for path in diffstat.added_by_file(self.ai_patch)
            }),
            "test_framework": "pytest",
            "test_files": list(self.test_files),
        }
        self._emit("repo.inspected", data)
        return data

    def inspect_patch(self) -> dict:
        self._expect(SessionPhase.REPO_INSPECTED)
        self.added = diffstat.added_by_file(self.ai_patch)
        self.new_files = diffstat.new_files(self.ai_patch)
        self.tp_files = {f.path for f in diffstat.parse(self.meta.get("test_patch") or "")}
        hunks = sum(1 for line in self.ai_patch.splitlines() if line.startswith("@@"))
        self.phase = SessionPhase.PATCH_INSPECTED
        data = {
            "files": len(self.added),
            "added_lines": sum(len(v) for v in self.added.values()),
            "hunks": hunks,
            "new_files": sorted(self.new_files),
            "anchors": [
                {"anchor_id": path, "path": path,
                 "added_lines": [line.lineno for line in lines],
                 "new_file": path in self.new_files}
                for path, lines in sorted(self.added.items())
            ],
        }
        self._emit("patch.inspected", data)
        return data

    def build_eligible_universe(self) -> dict:
        self._expect(SessionPhase.PATCH_INSPECTED)
        # Coarse anchors are files.  HDD-inspired child selection remains inside
        # the deterministic Evidence Plane when that file is probed.
        eligible = []
        excluded = []
        for path in sorted(self.added):
            allowed, reason = filekinds.probe_allowed(path, self.tp_files)
            if path in self.new_files or allowed:
                eligible.append(path)
            else:
                excluded.append({"path": path, "reason": reason})
        self.universe = tuple(eligible)
        self.phase = SessionPhase.UNIVERSE_FROZEN
        data = {"anchor_ids": list(self.universe), "count": len(self.universe),
                "universe_sha256": sha256_text("\n".join(self.universe)),
                "excluded": excluded}
        self._emit("universe.frozen", data)
        return data

    def collect_test_scope(self, paths: list[str] | tuple[str, ...] | None = None) -> dict:
        self._expect(SessionPhase.UNIVERSE_FROZEN)
        if paths is not None and tuple(paths) != tuple(self.test_files):
            self._fail("test_scope", "tool paths must exactly match the declared server scope")
        try:
            self.evidence_root.begin(cli={"source": self.resolved.kind,
                                          "session": "analysis-session-v1"})
            self.evidence_started = True
            # Worktree creation is deliberately delayed until after plan approval.
            # Drafting a plan therefore never leaves executable scratch state.
            self.ws = prepare(
                self.instance_id, self.meta, self.adapter, self.ai_patch,
                slug=self.resolved.slug, repo_root=self.resolved.repo_root,
                python=self.resolved.python)
            self.collected = collect_nodeids(
                self.adapter, self.ws.python, self.ws.path, self.test_files)
            if self.resolved.declare_all_collected:
                if not self.collected:
                    raise TestIdError(f"{self.test_files} 一个测试项都没收集到")
                self.declared = list(self.collected)
                mapping = {t: t for t in self.collected}
            else:
                mapping = map_test_ids(self.declared, self.collected)
            self.nodeids = [mapping[d] for d in self.declared]
            self.manifest.declared_tests = tuple(self.nodeids)
        except WorkspaceError as exc:
            self._fail("prepare", str(exc)[:200], exc)
        except TestIdError as exc:
            self._fail("id_mapping", f"{type(exc).__name__}: {exc}"[:200], exc)
        except TestRangeError as exc:
            self._fail("collect", str(exc)[:200], exc)
        self.phase = SessionPhase.TEST_SCOPE_COLLECTED
        data = {"collected": list(self.collected), "declared": list(self.nodeids)}
        self._emit("test_scope.collected", {"collected_count": len(self.collected),
                                             "declared_count": len(self.nodeids)})
        return data

    def run_baseline(self) -> dict:
        self._expect(SessionPhase.TEST_SCOPE_COLLECTED)
        data_file = cov_mod.fresh_data_file(self.run_dir, "baseline")
        rcfile = cov_mod.write_rcfile(self.run_dir)
        t0 = time.time()
        try:
            self.base = run_declared(
                self.adapter, self.ws.python, self.ws.path, self.nodeids,
                self.run_dir / "baseline.xml", deadline=self.total_budget,
                coverage_file=str(data_file), coverage_rcfile=str(rcfile),
                coverage_source=self.adapter.coverage_source(self.test_files))
        except JUnitUnusable as exc:
            self._fail("junit", str(exc)[:200], exc)
        except TestRangeError as exc:
            self._fail("baseline_run", str(exc)[:200], exc)
        statuses = self.base.vector.as_dict()
        bad, skipped = baseline_partition(statuses)
        if bad:
            self._fail("baseline_green",
                       f"{len(bad)}/{len(self.base.vector)} 项失败或缺失：{bad[:3]}")
        # 跳过的测试同时退出声明分母：一个没有执行过的测试无从回归，
        # 留在分母里只会让「无据」的比例看起来更差，却没有任何证据支撑。
        self.skipped_excluded = skipped
        if self.skipped_excluded:
            excluded = set(self.skipped_excluded)
            self.nodeids = [n for n in self.nodeids if n not in excluded]
            if not self.nodeids:
                self._fail("baseline_green",
                           f"声明范围内 {len(self.skipped_excluded)} 项全部被跳过，"
                           f"没有可回归的测试")
            self.base = replace(self.base, vector=TestVector.of(
                {t: st for t, st in statuses.items() if t not in excluded}))
            self._emit("baseline.skipped_excluded",
                       {"excluded_count": len(self.skipped_excluded),
                        "remaining_declared": len(self.nodeids)})
        try:
            self.cov = cov_mod.collect(
                self.ws.python, self.ws.path, data_file, t0, self.test_files,
                coverage_cmd_ok=True, junit_ok=True, rcfile=rcfile)
        except cov_mod.CoverageUnavailable as exc:
            self._fail("coverage", str(exc)[:200], exc)

        self.recs.append(ledger.observe.collect_set(self.collected, run_id=self.slug))
        self.runner = trial.TrialRunner(
            ws=self.ws, adapter=self.adapter, nodeids=self.nodeids,
            run_dir=self.run_dir, baseline=self.base.vector,
            run_id=self.slug, collector=self.recs)
        for path, lines in sorted(self.added.items()):
            allowed, why = filekinds.probe_allowed(path, self.tp_files)
            self.recs.append(ledger.observe.file_kind(
                path, probe_allowed=allowed, reason=why, run_id=self.slug))
            self.recs.append(ledger.observe.line_context(
                path, [a.lineno for a in lines], self.cov,
                non_executable={a.lineno for a in lines if a.non_executable},
                run_id=self.slug))
            rs = unevidenced.label_lines(path, lines, self.cov, supported=allowed)
            self.per_file[path] = rs
            self.base_lines += rs
        self.bud = budget.Budget.start(self.base.seconds, started=self.t_start)
        self.phase = SessionPhase.BASELINE_COMPLETE
        data = {"seconds": round(self.base.seconds, 2),
                "declared_tests": len(self.nodeids), "all_passed": True}
        self._emit("baseline.completed", data)
        # Deliberately after the budget starts and after `baseline.completed`:
        # routing reads source and parses ASTs, it never runs a test, so it must
        # not move the measured baseline cost the scheduler reads.
        self._route_ddmin()
        return data

    def candidate_summaries(self) -> tuple[CandidateSummary, ...]:
        self._expect(SessionPhase.BASELINE_COMPLETE, SessionPhase.PROBING)
        baseline_cost = float(self.base.seconds if self.base else 0.0)
        rows = []
        for anchor_id in self.universe:
            added_lines = {line.lineno for line in self.added.get(anchor_id, [])}
            covered = set((self.cov.executed if self.cov else {}).get(anchor_id, set()))
            new_file = anchor_id in self.new_files
            pending = 0 if new_file else len(
                unevidenced.pending_probe(self.per_file.get(anchor_id, [])))
            rows.append(CandidateSummary(
                anchor_id=anchor_id, path=anchor_id,
                added_lines=len(added_lines), new_file=new_file,
                covered_added_lines=len(added_lines & covered),
                probe_pending_lines=pending,
                estimated_cost_s=round(
                    baseline_cost * probe_cost_units(
                        new_file=new_file, pending=pending), 3)))
        return tuple(rows)

    def _route_ddmin(self) -> dict:
        """Freeze ddmin's atom universe and applicability, at zero trial cost.

        The frozen 30-instance walk found that most rejections were structural —
        a real AI patch usually edits inside existing statements, so it offers
        no independently deletable added-only statement.  That is a fact about
        ddmin's reach, and it is readable from the AST before any test runs.
        Deciding it here means an inapplicable anchor is *named*, not probed and
        then written up as a failure.
        """
        if self.probe_strategy is not ddmin_mod.ProbeStrategy.DDMIN:
            return {}
        for anchor_id in self.universe:
            added = {a.lineno for a in self.added.get(anchor_id, [])}
            try:
                source = (self.ws.path / anchor_id).read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                self.ddmin_routes[anchor_id] = ddmin_mod.unreadable_route(
                    f"{type(exc).__name__}: {exc}")
                continue
            self.ddmin_routes[anchor_id] = ddmin_mod.route(
                source, path=anchor_id, added_lines=added,
                whole_file=anchor_id in self.new_files)
        self.ddmin_routing = ddmin_mod.routing_summary(self.ddmin_routes)
        self._emit("ddmin.routed", {
            **self.ddmin_routing,
            "routes": {anchor_id: decision.to_json()
                       for anchor_id, decision in sorted(self.ddmin_routes.items())},
        })
        return self.ddmin_routing

    def _minimize(self, anchor_id: str, regressions: set[str]) -> None:
        """Narrow an already load-bearing anchor to a 1-minimal statement set.

        Only runs for `ddmin`, and only once the ordinary probe has already
        produced a named regression — ddmin refines a finding, it does not make
        one.

        Two rejections are kept strictly apart, because conflating them is how a
        tool ends up overstating itself:

          not_applicable — decided by `_route_ddmin` from the AST, zero trials.
          applicable but unproven — ddmin ran, spent trials, and still could not
                                    certify; the record says which trial outcome.

        Deliberately called after `probe.completed` is emitted: these trials must
        not land in `Observation.cost_s`, which the A2 oracle reads. Folding them
        in would make turning ddmin on silently change the measured cost of every
        candidate.
        """
        if self.probe_strategy is not ddmin_mod.ProbeStrategy.DDMIN:
            return
        if not regressions:
            return
        route = self.ddmin_routes.get(anchor_id)
        if route is None:
            # Routing covers the whole frozen universe; a gap here means the
            # probed anchor was never frozen, which invalidates the run.
            self._fail("ddmin_route_missing", anchor_id)
        target = sorted(regressions)[0]
        started = time.time()
        record: dict = {"anchor_id": anchor_id, "target_regression": target,
                        "strategy": self.probe_strategy.value,
                        "applicability": route.routing.value,
                        "route_reason": route.reason,
                        "frozen_unit_ids": list(route.unit_ids)}

        def done(experiments: int = 0) -> None:
            """Stamp cost, including the giving-up paths.

            "It produced no certificate" and "it spent two minutes producing no
            certificate" are different findings, and the 12-task evaluation
            compares durations between strategies.
            """
            record["experiments"] = experiments
            record["duration_s"] = round(time.time() - started, 3)
            self.minimizations.append(record)

        if not route.applicable:
            record.update(complete=False, reason=route.reason,
                          route_detail=route.detail)
            done()
            self._emit("minimization.skipped", {
                "anchor_id": anchor_id, "strategy": self.probe_strategy.value,
                "target_regression": target, "reason": route.reason,
                "detail": route.detail, "experiments": 0,
            })
            return

        try:
            source = (self.ws.path / anchor_id).read_text(
                encoding="utf-8", errors="replace")
        except OSError as exc:
            record.update(complete=False, reason="source_changed_since_freeze",
                          route_detail=f"{type(exc).__name__}: {exc}"[:200])
            done()
            return
        if sha256_text(source) != route.source_sha256:
            # The atoms were frozen at baseline against this exact source. If the
            # restored file no longer matches, the certificate would be about a
            # universe that no longer exists, so no certificate is issued.
            record.update(complete=False, reason="source_changed_since_freeze",
                          route_detail="restored source differs from the frozen atoms")
            done()
            return
        atoms = route.units

        span = sorted({a.lineno for a in self.added.get(anchor_id, [])})
        body = "\n".join(source.splitlines()[span[0] - 1:span[-1]])
        unit = ledger.anchors.unit_anchor(
            "S2", anchor_id, structural_path=f"ddmin:{anchor_id}",
            source=body, line_start=span[0], line_end=span[-1])
        # ddmin re-runs interventions the labelling pass already performed —
        # its one-element removal checks are, by construction, deletions hdd
        # tried. The ledger is content addressed and rejects a duplicate
        # record_id, so sharing the collector fails the whole run. These trials
        # are exploration, not published evidence: the certificate carries every
        # trial and every removal check and is independently checkable. So they
        # get their own collector and stay out of the ledger, while still
        # executing through the same runner (same restore, same budget).
        explore = replace(self.runner, collector=[])
        oracle = ddmin_mod.trial_runner_oracle(
            explore, anchor=unit,
            deadline=self.bud.probe_deadline, target_regression=target,
            spend=self.bud.spend)
        # Run the full-set trial here rather than letting `minimize` raise, so a
        # routed-in anchor records *why* it still produced nothing: PASS means
        # the frozen atoms are not what carries the regression, UNRESOLVED means
        # the trial never produced a usable verdict.
        try:
            initial = oracle(atoms)
        except ddmin_mod.DirtyRestore as exc:
            self._fail("restore_dirty", str(exc)[:300], exc)
        if initial.outcome is not ddmin_mod.Outcome.FAIL:
            record.update(complete=False,
                          reason=f"initial_{initial.outcome.value.lower()}",
                          initial_regressions=sorted(initial.regressions),
                          initial_detail=(initial.detail or "")[:200])
            done(1)
            return
        try:
            result = ddmin_mod.minimize(atoms, oracle, target_regression=target)
        except ddmin_mod.DirtyRestore as exc:
            # A dirty workspace invalidates every later conclusion, so this is
            # the one ddmin outcome that stops the whole run.
            self._fail("restore_dirty", str(exc)[:300], exc)
        except (ddmin_mod.InvalidUniverse, ValueError) as exc:
            record.update(complete=False, reason="not_minimizable",
                          route_detail=f"{type(exc).__name__}: {exc}"[:200])
            done(1)
            return
        record.update(complete=True, trials=len(result.trials),
                      certificate=result.certificate.to_json())
        done(1 + len(result.trials))
        self._emit("minimization.completed", {
            "anchor_id": anchor_id, "strategy": self.probe_strategy.value,
            "target_regression": target,
            "frozen_units": len(result.certificate.frozen_unit_ids),
            "minimal_units": len(result.certificate.minimal_unit_ids),
            "one_minimal": result.certificate.one_minimal,
            "trials": len(result.trials),
        })

    def counterfactual_probe(self, anchor_id: str) -> Observation:
        self._expect(SessionPhase.BASELINE_COMPLETE, SessionPhase.PROBING)
        if anchor_id not in self.universe:
            self._fail("anchor_not_frozen", f"unknown anchor_id: {anchor_id}")
        if anchor_id in self.completed:
            self._fail("anchor_already_completed", anchor_id)
        self.phase = SessionPhase.PROBING
        t0 = time.time()
        before_units = len(self.units)
        before_records = len(self.recs)
        try:
            if anchor_id in self.new_files:
                ok, unit, why = drift.evaluate(
                    self.ws, self.adapter, anchor_id,
                    baseline=self.base.vector, nodeids=self.nodeids,
                    test_files=self.test_files, run_dir=self.run_dir,
                    baseline_collected=self.collected, runner=self.runner,
                    deadline=max(10.0, self.total_budget - (time.time() - self.t_start)))
                if ok and unit:
                    self.drift_files.add(anchor_id)
                    self.units.append(unit)
                else:
                    self.drift_rejected[anchor_id] = why

            if anchor_id not in self.drift_files:
                allowed, _ = filekinds.probe_allowed(anchor_id, self.tp_files)
                rs = self.per_file.get(anchor_id, [])
                pending = set(unevidenced.pending_probe(rs))
                if allowed and pending:
                    u3, unresolved = hdd.run(
                        self.ws, self.adapter, path=anchor_id,
                        added={a.lineno for a in self.added[anchor_id]},
                        pending=pending, baseline=self.base.vector,
                        nodeids=self.nodeids, run_dir=self.run_dir,
                        covered=self.cov.executed.get(anchor_id, set()),
                        budget=self.bud, runner=self.runner)
                    self.units += u3
                    for result in rs:
                        if result.lineno in unresolved:
                            result.reason = unresolved[result.lineno]
        except ledger.claim_builder.ClaimDerivationError as exc:
            self._fail("claim_derivation", str(exc)[:300], exc)
        except trial.TrialRestoreDirty as exc:
            self._fail("restore_dirty", exc.detail[:300], exc)

        self.completed.add(anchor_id)
        new_units = self.units[before_units:]
        regressions: set[str] = set()
        identical: bool | None = None
        evidence_id = ""
        for unit in new_units:
            if unit.mutated is not None:
                identical = unit.mutated.identical_to(self.base.vector)
                regressions.update(tid for tid, _, _ in
                                   unit.mutated.regressions(self.base.vector))
            evidence_id = unit.experiment_id or evidence_id
        obs = Observation(
            anchor_id=anchor_id, status="COMPLETE",
            identical_to_baseline=identical,
            regressed_tests=tuple(sorted(regressions)),
            cost_s=time.time() - t0, evidence_id=evidence_id,
            observation_id=f"{self.slug}:{len(self.completed):04d}:{anchor_id}")
        self._emit("probe.completed", {
            "anchor_id": anchor_id, "status": obs.status,
            "identical_to_baseline": obs.identical_to_baseline,
            "regressed_tests": list(obs.regressed_tests),
            "cost_s": round(obs.cost_s, 3), "evidence_id": evidence_id,
            "units": len(new_units),
        })
        self._minimize(anchor_id, regressions)
        self._emit("restore.verified", {"anchor_id": anchor_id,
                                         "restored_clean": True})
        for record in self.recs[before_records:]:
            if getattr(record, "record_type", "") == ledger.records.CLAIM:
                self._emit("claim.derived", {
                    "claim_id": record.record_id,
                    "claim_type": record.payload.get("kind"),
                    "anchor_id": anchor_id,
                    "provenance": list(record.payload.get("provenance") or []),
                })
        return obs

    def read_evidence(self, evidence_id: str) -> dict:
        self._expect(SessionPhase.BASELINE_COMPLETE, SessionPhase.PROBING,
                     SessionPhase.FINISHED)
        for record in self.recs:
            if getattr(record, "record_id", "") == evidence_id:
                return record.to_json()
        if self.phase is SessionPhase.FINISHED:
            rows = ledger.store.read(self.run_dir / ledger.store.FILENAME)
            for row in rows:
                if row.get("record_id") == evidence_id:
                    return row
        raise SessionError("evidence_not_found", evidence_id)

    def finish_run(self, stop_reason: str = "no_eligible_left") -> dict:
        self._expect(SessionPhase.BASELINE_COMPLETE, SessionPhase.PROBING)
        try:
            stop_reason = StopReason(stop_reason).value
        except ValueError as exc:
            raise SessionError("stop_reason_invalid", str(stop_reason)) from exc
        self._emit("run.synthesizing", {"stop_reason": stop_reason})
        results = label_mod.merge(self.base_lines, self.units, self.drift_files)
        if self.three_state:
            results = label_mod.withhold_inert(results)
        summary = label_mod.summarize(results)
        partial = len(self.completed) < len(self.universe)
        summary["three_state"] = self.three_state
        summary["binary_files"] = diffstat.binary_files(self.ai_patch)
        summary["drift_rejected"] = self.drift_rejected
        summary["seconds"] = round(time.time() - self.t_start, 1)
        summary["baseline_seconds"] = round(self.base.seconds, 2)
        summary["declared_tests"] = len(self.nodeids)
        summary["analysis_completion"] = "partial" if partial else "complete"
        summary["stop_reason"] = stop_reason
        summary["probe_strategy"] = self.probe_strategy.value
        if self.ddmin_routing:
            attempted = [x for x in self.minimizations
                         if x.get("applicability") == ddmin_mod.Routing.APPLICABLE.value]
            summary["ddmin_routing"] = {
                **self.ddmin_routing,
                "load_bearing_and_applicable": len(attempted),
                "certified": sum(1 for x in attempted if x.get("complete")),
                "experiments_spent": sum(int(x.get("experiments") or 0)
                                         for x in self.minimizations),
            }
        summary["frozen_candidates"] = len(self.universe)
        summary["completed_candidates"] = len(self.completed)

        try:
            self.ws.restore()
        except (WorkspaceError, RuntimeError) as exc:
            self._fail("restore_dirty", str(exc)[:300], exc)
        source = {p: (self.ws.path / p).read_text(
            encoding="utf-8", errors="replace").splitlines()
                  for p in self.added if (self.ws.path / p).exists()}
        self.recs += ledger.legacy.mirror(
            run_id=self.slug, units=self.units, lines=results)
        lv = ledger.validate.validate(self.recs, run_id=self.slug)
        if lv:
            self._fail("ledger_invalid", "；".join(lv[:4])[:400])
        pr = ledger.parity.check(self.recs, results)
        if not pr.ok:
            self._fail("ledger_parity", pr.summary()[:400])
        dp = ledger.parity.check_derived(
            self.recs, results, three_state=self.three_state)
        if not dp.ok:
            self._fail("derive_parity", dp.summary()[:400])
        summary["projection_parity"] = {"ok": True, "checked": pr.checked}
        summary["ledger_validated"] = True
        summary["derive_parity"] = {"ok": True, "checked": dp.checked}
        summary["evidence_coverage"] = ledger.parity.evidence_coverage(
            self.recs, three_state=self.three_state)
        summary["claim_derivation_version"] = ledger.claim_builder.DERIVATION_VERSION
        summary["legacy_verdict_dependency"] = False
        summary["restore_protocol_version"] = "per-trial-git-restore-v1"
        summary.update(self.run_metadata)

        # 逐行证据 ID 由账本推导给出（与 derive_parity 用的是同一次推导），
        # UI 才能把「这一行」和「它凭什么」连起来。
        evidence_by_line = {
            (d.path, d.lineno): d.evidence_ids
            for d in ledger.derive.derive(
                [r.to_json() for r in self.recs], three_state=self.three_state)}
        model = render.build_model(results, self.units, source, summary,
                                   evidence_by_line=evidence_by_line)
        payload = certificate.build_payload(
            self.manifest, self.units, results, summary,
            cmd_hint="pytest", render_model=model,
            minimizations=self.minimizations or None)
        report, _ = runroot.publish_bundle(self.run_dir, [
            (self.run_dir / "report.json",
             json.dumps(payload, ensure_ascii=False, indent=2)),
            (self.run_dir / ledger.store.FILENAME,
             ledger.store.serialize(self.recs, self.slug)),
        ], run_id=self.slug)
        self.evidence_root.finish(
            True, reason="analysis_completion=partial" if partial else "")
        if not self.quiet:
            print(render.render_report(model))
            print("\nJSON 证书已生成（保存在本地运行目录）")
        self.phase = SessionPhase.FINISHED
        self._result = {
            "instance": self.instance_id, "scaffold": self.scaffold,
            "ok": True, "summary": summary, "units": self.units,
            "results": results, "report": str(report),
        }
        self._emit("run.completed", {
            "summary": summary, "report": str(report),
            "analysis_completion": summary["analysis_completion"],
        })
        return self._result

    def run_all(self) -> dict:
        """Compatibility path: the historical deterministic full run."""
        try:
            self.inspect_repo()
            self.inspect_patch()
            self.build_eligible_universe()
            self.collect_test_scope()
            self.run_baseline()
            for anchor_id in self.universe:
                self.counterfactual_probe(anchor_id)
            return self.finish_run("no_eligible_left")
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        if self.ws is not None:
            self.ws.cleanup()


class ToolRouter:
    """The only dispatch path to AnalysisSession handlers."""

    def __init__(self, session: AnalysisSession, catalog):
        self.session = session
        self.catalog = catalog

    def call(self, name: str, args: dict | None = None):
        args = dict(args or {})
        spec = self.catalog.check(name, args)
        if _RISK_ALLOWED[name] is not spec.risk:
            raise SessionError("tool_risk_mismatch", name)
        handler = getattr(self.session, name)
        return handler(**args)
