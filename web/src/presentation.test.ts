import { describe, expect, it } from "vitest";
import {
  classifyError, decisiveEvidenceId, eventLabel, linePresentation, normalizePastedToken,
  reasonLabel, resolveStartupToken, schedulingDetail, statusView, summarySentence,
  eventPhase, experimentStory, groupEventPhases, verdictCounts, minimizationViews,
  modeLabels, capabilityAvailable, capabilityTone, groupCapabilities, emphasizeNumbers,
  offlineReplayNotice,
  modelConclusion, modelDecisionStages, modelParticipation, workspacePresentation,
  modelLabel,
  repairStatusView,
  draftCardRows, reviewFocusLabel,
  locateEvidenceLabel, locateFallbackNotice,
  releaseGateLabel, releaseStatusView, eventDeltas, shortHash,
  eventPresentation,
  uiProbeStatusView, narrowPackageChecksView, unenforcedLimitsView,
  certificateGradeRows,
  memoryRuleViews, MEMORY_CARD_HEADLINE, MEMORY_STATUS_LABELS, memoryKindOptions,
  dispositionOptions, DISPOSITION_ANSWERS, DISPOSITION_BOUNDARY,
  planRevisionViews, PLAN_REVISION_BOUNDARY,
  admissibilityView, admissibilityContrast, visaStatusView,
  autonomyView,
  laneOf, laneEvents, evidencePassport,
  passportStatus,
  standingAuthorizationView, normalizeEvalReceiptIndex, normalizeEvalReceipt,
  type Capability,
  type ReviewDraft,
} from "./presentation";

describe("Review Cockpit presentation", () => {
  it("keeps release machine failures separate from pending external gates", () => {
    const failed = releaseStatusView({
      machine_status: "MACHINE_CHECKS_FAILED",
      machine_failure_reasons: ["Vitest 失败", "资源超预算"],
      pending_gates: ["independent_linux_host", "real_developer_study"],
    });
    expect(failed.state).toBe("machine_failed");
    expect(failed.headline).toContain("2 项");
    expect(failed.detail).toContain("Vitest 失败");
    expect(failed.detail).not.toContain("独立 Linux");
    const passed = releaseStatusView({machine_status: "MACHINE_CHECKS_PASSED",
      external_gates_pending: ["real_developer_study"]});
    expect(passed.state).toBe("machine_passed_external_pending");
    expect(passed.headline).toContain("外部验证仍待完成");
    expect(passed.detail).toContain("真实开发者试用");
    // v1 协议里 pending_gates 是具名门禁映射：它不得挤掉
    // external_gates_pending 数组，否则会把没待完成的门禁也说成待完成。
    const mapped = releaseStatusView({machine_status: "MACHINE_CHECKS_PASSED",
      external_gates_pending: ["real_developer_study"],
      pending_gates: {public_repo_matrix: {status: "COMPLETE_PASS"}}});
    expect(mapped.pendingGates).toEqual(["real_developer_study"]);
    expect(mapped.detail).not.toContain("独立 Linux");
  });

  it("renders absent and failed release reads as distinct conservative states", () => {
    expect(releaseStatusView({evidence: {status: {state: "not_generated"}}}).state)
      .toBe("not_generated");
    expect(releaseStatusView({}, true).state).toBe("load_error");
    const opaque = releaseStatusView({machine_status: "MACHINE_CHECKS_FAILED",
      machine_failure_reasons: ["UNKNOWN_GATE_42"]});
    expect(opaque.detail).not.toContain("UNKNOWN_GATE_42");
    expect(opaque.machineFailureReasons).toContain("UNKNOWN_GATE_42");
  });
  it("flags a receipt that describes another commit as expired", () => {
    const stale = "a".repeat(40);
    const serving = "b".repeat(40);
    const expired = releaseStatusView({
      machine_status: "MACHINE_CHECKS_FAILED",
      machine_failure_reasons: ["public_repo_matrix_suspended"],
      source_commit: stale, serving_commit: serving, receipt_expired: true,
    });
    expect(expired.state).toBe("receipt_expired");
    expect(expired.headline).toContain("收据已过期");
    expect(expired.detail).toContain("重新生成收据后本卡自动恢复");
    // 过期是披露不是抹除：旧原因仍留在技术详情里。
    expect(expired.machineFailureReasons).toContain("public_repo_matrix_suspended");
    expect(expired.servingCommit).toBe(serving);
    // 服务端没带过期标记时不得误报。
    const fresh = releaseStatusView({machine_status: "MACHINE_CHECKS_FAILED",
      machine_failure_reasons: ["public_repo_matrix_suspended"]});
    expect(fresh.state).toBe("machine_failed");
  });
  it("keeps a fragment token across refresh through session storage", () => {
    expect(resolveStartupToken("#token=fresh", "old")).toBe("fresh");
    expect(resolveStartupToken("", "fresh")).toBe("fresh");
    expect(normalizePastedToken("http://127.0.0.1:8765/#token=abc%2F123")).toBe("abc/123");
  });

  it("builds the offline replay banner from the ledger time, never wall clock", () => {
    expect(offlineReplayNotice(false, [{occurred_at: "2026-09-14T02:00:00Z"}])).toBeNull();
    const notice = offlineReplayNotice(true, [
      {occurred_at: "2026-09-14T01:00:00Z"}, {occurred_at: "2026-09-14T02:00:00Z"}]);
    expect(notice?.label).toBe("离线回放");
    // 横幅上的时刻必须就是账本最后一条事件的那一刻（换算成本地展示格式后仍是同一瞬间）。
    const tail = (notice?.detail || "").slice("结果生成于 ".length);
    expect(new Date(tail).getTime()).toBe(new Date("2026-09-14T02:00:00Z").getTime());
    expect(offlineReplayNotice(true, [])?.detail).toBe("结果生成时间未随包记录");
    expect(offlineReplayNotice(true, [{occurred_at: "not-a-date"}])?.detail)
      .toBe("结果生成时间未随包记录");
  });

  it("maps internal states and events to the review story", () => {
    expect(statusView("VERIFYING_RESTORE")).toMatchObject({label: "检查完整恢复", step: 7});
    expect(statusView("COMPLETE")).toMatchObject({label: "审查完成", step: 8});
    expect(statusView("CANCELLING")).toMatchObject({label: "正在安全中止", tone: "warning"});
    expect(statusView("CLEANUP_REQUIRED")).toMatchObject({label: "需要清理", tone: "danger"});
    expect(eventLabel("claim.derived")).toBe("证据主张已签发");
    expect(eventLabel("repair.delivered")).toBe("修复已验证并交付");
    expect(eventLabel("test.visa")).toBe("签证判定已作出");
    expect(eventLabel("test.not_effective")).toBe("跑绿但未判定有效");
  });

  it("separates recoverable errors from authentication failures", () => {
    expect(classifyError(409, "STALE_PLAN", "stale").recovery).toBe("reload-plan");
    expect(classifyError(401, "AUTH_REQUIRED", "bad").recovery).toBe("token");
    expect(classifyError(undefined, undefined, "offline").level).toBe("network");
    expect(classifyError(409, "SOURCE_SNAPSHOT_CHANGED", "changed").title).toBe("修复证据已经过期");
    expect(classifyError(409, "REPAIR_BRANCH_EXISTS", "branch").title).toBe("修复分支发生冲突");
    expect(classifyError(400, "REPAIR_VERIFICATION_FAILED", "failed").title).toBe("补丁验证未通过");
  });

  it("maps the terminal-snapshot timeout to a retryable warning, not a network failure", () => {
    const notice = classifyError(undefined, "TERMINAL_SNAPSHOT_TIMEOUT", "timeout");
    expect(notice.level).toBe("warning");
    expect(notice.recovery).toBe("retry");
    expect(notice.title).toBe("结果还在落盘，稍后刷新即可");
  });

  it("gives every review status a Chinese label instead of echoing the raw string", () => {
    const allStatuses = ["CREATED", "DRAFTING", "INTAKE_VALIDATED", "PLAN_DRAFTED",
      "AWAITING_APPROVAL", "PLAN_FROZEN", "BASELINE_RUNNING", "EXECUTING",
      "REPLANNING", "AWAITING_HUMAN", "VERIFYING_RESTORE", "PROPOSING_REPAIR",
      "VERIFYING_REPAIR", "DELIVERING_BRANCH", "SYNTHESIZING", "CANCELLING",
      "RECOVERING", "CLEANUP_REQUIRED", "QUARANTINED", "COMPLETE", "COMPLETED",
      "PARTIAL", "FAILED", "ABORTED", "CANCELLED"];
    for (const status of allStatuses) {
      expect(statusView(status).label).not.toBe(status);
    }
  });

  it("keeps repair delivery outcomes explicit and non-success states actionable", () => {
    expect(repairStatusView("DELIVERED")).toMatchObject({label: "已交付", tone: "success"});
    expect(repairStatusView("INCOMPLETE")).toMatchObject({label: "需要清理", tone: "danger"});
    expect(repairStatusView("EXPIRED")).toMatchObject({label: "工件已过期", tone: "warning"});
    expect(repairStatusView("BRANCH_CONFLICT")).toMatchObject({label: "分支冲突", tone: "warning"});
    expect(repairStatusView("NOT_DELIVERED")).toMatchObject({label: "尚未交付", tone: "idle"});
  });

  it("shows withheld inertia independently and compiles the outcome sentence", () => {
    expect(linePresentation({label: "未标注", reason: "inert_withheld"}).badge).toBe("惰性扣下");
    expect(summarySentence({total_added_lines: 48,
      by_label: {承重: 4, 无据: 10, 游离: 11},
      by_reason: {inert_withheld: 4}})).toContain("4 行惰性结论被护栏扣下");
  });

  it("keeps three-state counts to the formal three states plus total", () => {
    expect(verdictCounts({total_added_lines: 29, by_label: {
      承重: 4, 无据: 8, 游离: 3, 惰性: 7,
    }}).map(item => [item.label, item.value])).toEqual([
      ["新增行", 29], ["承重", 4], ["无据", 8], ["游离", 3],
    ]);
  });

  it("derives the four-frame experiment from real events", () => {
    const story = experimentStory([
      {kind: "baseline.completed", data: {declared_tests: 12, all_passed: true}},
      {kind: "probe.completed", data: {anchor_id: "pkg/a.py",
        regressed_tests: ["tests/test_a.py::test_required"]}},
      {kind: "restore.verified", data: {anchor_id: "pkg/a.py", restored_clean: true}},
    ], [{位置: {file: "pkg/a.py", start: 9, end: 11}}]);
    expect(story.map(step => step.detail)).toEqual([
      "12/12 项测试通过", "pkg/a.py:9–11 · 3 行", "tests/test_a.py::test_required",
      "12/12 项测试通过，工作区已恢复",
    ]);
    expect(story.map(step => step.state)).toEqual(["complete", "complete", "failed", "complete"]);
    expect(story.map(step => step.label)).toEqual([
      "基线全绿", "临时拿走", "具名测试报警", "恢复干净",
    ]);
  });

  it("keeps unfinished experiment frames honest", () => {
    const story = experimentStory([
      {kind: "baseline.started"},
      {kind: "probe.started", data: {anchor_id: "pkg/a.py"}},
      {kind: "restore.started", data: {anchor_id: "pkg/a.py"}},
    ]);
    expect(story.map(step => step.state)).toEqual(["active", "active", "pending", "active"]);
  });

  it("folds raw events into four stable review phases without losing failures", () => {
    expect(eventPhase("probe.completed")).toBe("实验");
    expect(eventPhase("claim.derived")).toBe("证据");
    expect(eventPhase("restore.verified")).toBe("恢复");
    const groups = groupEventPhases([
      {kind: "review.created"}, {kind: "probe.completed"},
      {kind: "claim.derived"}, {kind: "review.failed"},
    ]);
    expect(groups.map(group => [group.phase, group.events.length])).toEqual([
      ["准备", 1], ["实验", 1], ["证据", 1], ["恢复", 1],
    ]);
  });

  it("keeps live/replay, scheduler and isolation labels explicit", () => {
    expect(modeLabels({execution_mode: "live", scheduler_mode: "model",
      isolation_mode: "sandboxed"})).toEqual({
      run: "实时运行", scheduler: "模型调度", isolation: "沙箱模式",
    });
    expect(modeLabels({execution_mode: "live", scheduler_mode: "fifo",
      isolation_mode: "trusted_local"}, true)).toEqual({
      run: "离线回放", scheduler: "FIFO", isolation: "受信任模式",
    });
  });

  // 运行途中还没有 bundle。若此时只看 bundle，一次真实调用模型的运行会被标成
  // 「确定性调度」——录屏画面会说谎。运行中必须用服务端回显的 request 兜底。
  it("labels an in-flight run from the echoed request, not from the missing bundle", () => {
    expect(modeLabels(null, false, {model_provider: "live", agent_level: "l2"}))
      .toMatchObject({run: "实时运行", scheduler: "模型调度 · L2"});
    expect(modeLabels(null, false, {model_provider: "deterministic"}))
      .toMatchObject({run: "实时运行", scheduler: "确定性调度"});
    // bundle 到位后以 bundle 为准，request 兜底不得覆盖已发布的证据包。
    expect(modeLabels({scheduler_mode: "coverage_first"}, false,
      {model_provider: "live"})).toMatchObject({scheduler: "覆盖优先"});
  });

  // 结论句只做视觉提取：切出来的片段拼回去必须逐字等于原句，
  // 不允许在"强调数字"的名义下改写或省略任何一个字。
  it("splits the summary for emphasis without altering a single character", () => {
    const sentence = "本次审查覆盖 22 行新增代码：6 行获得测试承重证据，1 行惰性结论被护栏扣下。";
    const parts = emphasizeNumbers(sentence);
    expect(parts.map(p => p.text).join("")).toBe(sentence);
    expect(parts.filter(p => p.number).map(p => p.text)).toEqual(["22", "6", "1"]);
    expect(emphasizeNumbers("")).toEqual([]);
    expect(emphasizeNumbers("没有数字").map(p => p.number)).toEqual([false]);
  });

  it("shows only the scheduling change and concise reason", () => {
    expect(schedulingDetail("scheduler.next", {
      previous_next_anchor: "a.py", actual_next_anchor: "b.py",
      priority_reason: "观测后优先检查异常路径",
    })).toContain("a.py → b.py");
    expect(schedulingDetail("policy.rejected", {code: "ORDER_DUPLICATE"}))
      .toContain("ORDER_DUPLICATE");
  });

  it("reports live-model participation with adopted reorder actions only", () => {
    const report = modelParticipation(
      {provider: {kind: "openai-compatible", model_id: "deepseek-v4-pro"},
       model_metrics: {requests: 2, fallbacks: 0, policy_rejections: 0},
       request: {model_provider: "live", agent_level: "l2"}},
      [
        {kind: "baseline.completed"},
        {kind: "scheduler.next", data: {selection: "model_reprioritized",
          previous_next_anchor: "pkg/headers.py", actual_next_anchor: "pkg/pool.py",
          priority_reason: "剩余预算更低、预计成本更小"}},
        {kind: "scheduler.next", data: {selection: "frozen_coverage_first",
          previous_next_anchor: "pkg/pool.py", actual_next_anchor: "pkg/pool.py"}},
      ]);
    expect(report).toMatchObject({live: true, modelLabel: "DeepSeek",
      agentLevel: "L2", calls: 2, reorderCount: 1,
      orderChanges: ["pkg/headers.py → pkg/pool.py"],
      reasons: ["剩余预算更低、预计成本更小"], fallbacks: 0, policyRejections: 0});
  });

  it("reports deterministic runs honestly with zeros, never inventing model work", () => {
    const report = modelParticipation(
      {provider: {kind: "deterministic"}, model_metrics: {},
       request: {model_provider: "deterministic"}},
      [{kind: "scheduler.next", data: {selection: "frozen_coverage_first"}}]);
    expect(report).toMatchObject({live: false, modelLabel: "—（确定性调度）",
      calls: 0, reorderCount: 0, orderChanges: [], reasons: [],
      fallbacks: 0, policyRejections: 0});
  });
});

describe("工作台焦点与分权泳道", () => {
  it("next-run setup hides the old result and locks an active review", () => {
    expect(workspacePresentation("deterministic", "COMPLETE", "next_run_setup"))
      .toMatchObject({showCurrentResult: false, showModelTrack: false,
        showModelBanner: false, showRecommendations: false,
        showDeterministicPlaceholder: true, showPreviousResultNotice: true,
        configLocked: false});
    expect(workspacePresentation("live", "EXECUTING", "current_result"))
      .toMatchObject({showCurrentResult: true, showModelTrack: true, configLocked: true});
    expect(workspacePresentation("live", undefined, "next_run_setup"))
      .toMatchObject({showCurrentResult: false, showLiveSetupPlaceholder: true,
        showModelTrack: false, configLocked: false});
  });

  it("assigns known events to the actor that owns the fact", () => {
    expect(laneOf({kind: "model.request.completed"})).toBe("模型建议");
    expect(laneOf({kind: "scheduler.next", data: {selection: "model_reprioritized"}}))
      .toBe("模型建议");
    expect(laneOf({kind: "recommendation.generated"})).toBe("模型建议");
    expect(laneOf({kind: "baseline.completed"})).toBe("水木验码执行");
    expect(laneOf({kind: "restore.verified"})).toBe("水木验码执行");
    expect(laneOf({kind: "observation.recorded"})).toBe("测试执行");
    expect(laneOf({kind: "unknown.future_event"})).toBe("水木验码执行");
  });

  it("uses three lanes for live and two for deterministic runs", () => {
    const events = [
      {kind: "model.request.started"}, {kind: "baseline.completed"},
      {kind: "observation.recorded"},
    ];
    expect(laneEvents(events, true).map(group => [group.lane, group.events.length])).toEqual([
      ["模型建议", 1], ["水木验码执行", 1], ["测试执行", 1],
    ]);
    expect(laneEvents(events, false).map(group => group.lane)).toEqual([
      "水木验码执行", "测试执行",
    ]);
    expect(laneEvents(events, false)[0].events.map(event => event.kind)).not.toContain(
      "model.request.started");
  });
});

describe("证据护照", () => {
  const summary = {total_added_lines: 22, by_label: {承重: 4, 无据: 3, 游离: 2}};
  const events = [
    {kind: "probe.completed", data: {regressed_tests: ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]}},
    {kind: "observation.recorded", data: {regressed_tests: ["tests/test_a.py::test_one"]}},
    {kind: "restore.verified", data: {restored_clean: true}},
  ];

  it("aggregates Bundle fields and de-duplicates named failures", () => {
    expect(evidencePassport("COMPLETE", summary, events,
      {plan_sha256: "plan-demo-sha"}, [], undefined, true)).toMatchObject({
      status: "COMPLETE", statusLabel: "已完成", addedLines: 22, loadLines: 4,
      namedFailures: 2, unevidencedLines: 3, driftLines: 2,
      restoreStatus: "verified", restoreLabel: "已验证", planFingerprint: "plan-demo-sha",
      bundleAvailable: true, live: false, recommendationLabel: "不适用（确定性运行）",
    });
  });

  it("uses the four fixed status stamps", () => {
    expect(["COMPLETE", "PARTIAL", "FAILED", "ABORTED"].map(status =>
      evidencePassport(status, null).statusLabel)).toEqual(["已完成", "部分完成", "未签发", "已终止"]);
  });

  it("keeps deterministic passports free of model-specific claims", () => {
    const passport = evidencePassport("COMPLETE", summary, events, null, [], {
      live: false, modelLabel: "—（确定性调度）", calls: 0,
      recommendationFailed: false, recommendations: null,
    }, true);
    expect(JSON.stringify(passport)).not.toContain("DeepSeek");
    expect(passport.modelLabel).toBe("");
  });

  it("records live model participation and recommendation stage", () => {
    const passport = evidencePassport("PARTIAL", summary, events, null, [], {
      live: true, modelLabel: "deepseek-v4-pro", calls: 3,
      recommendationFailed: false,
      recommendations: {generated: true, items: [{anchor_id: "pkg/a.py", line_refs: [],
        evidence_ids: [], action: "", rationale: "", verification: ""}],
        failureReason: "", skippedReason: ""},
    }, true);
    expect(passport).toMatchObject({live: true, modelLabel: "deepseek-v4-pro",
      modelCalls: 3, recommendationLabel: "已生成 1 条"});
  });

  it("never stamps an in-progress review state as COMPLETE (full enum guard)", () => {
    // 全量枚举来自 modou/agent/review.py 的 ReviewStatus（25 态）。
    const terminal = ["COMPLETE", "COMPLETED", "PARTIAL", "FAILED", "ABORTED",
      "CANCELLED"] as const;
    const nonTerminal = ["CREATED", "DRAFTING", "INTAKE_VALIDATED",
      "PLAN_DRAFTED", "AWAITING_APPROVAL", "PLAN_FROZEN", "BASELINE_RUNNING",
      "EXECUTING", "REPLANNING", "AWAITING_HUMAN", "VERIFYING_RESTORE",
      "PROPOSING_REPAIR", "VERIFYING_REPAIR", "DELIVERING_BRANCH",
      "SYNTHESIZING", "CANCELLING", "RECOVERING", "CLEANUP_REQUIRED",
      "QUARANTINED"];
    for (const status of nonTerminal) {
      expect(passportStatus(status)).toBe("IN_PROGRESS");
    }
    for (const status of terminal) {
      expect(passportStatus(status)).toBe(status);
    }
    expect(passportStatus(undefined)).toBe("IN_PROGRESS");
    expect(passportStatus("SOMETHING_NEW")).toBe("IN_PROGRESS");
  });
});

describe("DeepSeek 参与四态与决策轨迹", () => {
  const liveBase = {
    provider: {kind: "openai-compatible", model_id: "deepseek-v4-pro"},
    request: {model_provider: "live", agent_level: "l2"},
  };

  it("未参与：确定性运行归为 not_participated", () => {
    const report = modelParticipation(
      {provider: {kind: "deterministic"}, model_metrics: {},
       request: {model_provider: "deterministic"}}, []);
    expect(report.state).toBe("not_participated");
    expect(modelConclusion(report)).toContain("未调用模型");
  });

  it("已参与并重排：reordered，且结论句只陈述事件先后", () => {
    const report = modelParticipation(
      {...liveBase, model_metrics: {requests: 5, fallbacks: 0}}, [
        {kind: "scheduler.next", data: {selection: "model_reprioritized",
          previous_next_anchor: "httpkit/headers.py",
          actual_next_anchor: "httpkit/pool.py",
          priority_reason: "目标要求找具名回归；上一步无回归；剩余预算有限"}},
      ]);
    expect(report.state).toBe("reordered");
    const conclusion = modelConclusion(report);
    expect(conclusion).toContain(
      "httpkit/pool.py 提到 httpkit/headers.py 之前");
    expect(conclusion).not.toContain("更快");
    expect(conclusion).not.toContain("节省");
  });

  it("已参与但保持顺序：kept_order 仍是真实参与", () => {
    const report = modelParticipation(
      {...liveBase, model_metrics: {requests: 4, fallbacks: 0}}, []);
    expect(report.state).toBe("kept_order");
    expect(modelConclusion(report)).toContain("保持原顺序");
  });

  it("部分参与：只有调度降级才归为 partial", () => {
    const byFallback = modelParticipation(
      {...liveBase, model_metrics: {requests: 4, fallbacks: 1}}, []);
    expect(byFallback.state).toBe("partial");
    expect(modelConclusion(byFallback)).toContain("降级");
    expect(modelConclusion(byFallback)).not.toContain("建议");
  });

  it("建议失败不再是确定性降级：调度状态不变，结论单独说明", () => {
    const byRecommendation = modelParticipation(
      {...liveBase, model_metrics: {requests: 5, fallbacks: 0},
       narration: {recommendations: {generated: false,
         failure_reason: "锚点格式不合法：anchor_id 不能包含行号",
         failure_code: "anchor_id_contains_lineno"}}}, []);
    expect(byRecommendation.state).toBe("kept_order");
    expect(byRecommendation.fallbacks).toBe(0);
    expect(byRecommendation.recommendationFailed).toBe(true);
    expect(byRecommendation.stateLabel).not.toContain("降级");
    const conclusion = modelConclusion(byRecommendation);
    expect(conclusion).toContain("保持原顺序");
    expect(conclusion).toContain("只读建议生成失败，不影响实验结论");
    expect(conclusion).not.toContain("降级为确定性策略");
  });

  it("运行中的 live 请求兜底：bundle 未取回时不误判为确定性", () => {
    const running = modelParticipation(null, [],
      {model_provider: "live", agent_level: "l2"});
    expect(running.live).toBe(true);
    expect(running.state).toBe("kept_order");
  });

  it("决策轨迹六步由真实事件推进，没有事件时停在目标传入", () => {
    const idle = modelDecisionStages([], true, false);
    expect(idle.map(stage => stage.key)).toEqual(
      ["goal", "observation", "deciding", "decided",
       "recommending", "recommended"]);
    expect(idle[0].state).toBe("active");
    expect(idle.slice(1).every(stage => stage.state === "pending")).toBe(true);

    const running = modelDecisionStages([
      {kind: "model.request.started", data: {stage: "plan"}},
      {kind: "model.request.completed", data: {stage: "plan"}},
      {kind: "model.request.started", data: {stage: "scheduling"}},
      {kind: "model.action", data: {kind: "reprioritize"}},
    ], true, false);
    expect(running[0].state).toBe("done");
    expect(running[3].state).toBe("active");   // 运行中：决定已做出但未收尾
    expect(running[2].state).toBe("done");
    expect(running[4].state).toBe("pending");  // 建议尚未开始

    const finished = modelDecisionStages([
      {kind: "model.request.started", data: {stage: "plan"}},
      {kind: "model.request.started", data: {stage: "scheduling"}},
      {kind: "model.action", data: {kind: "reprioritize"}},
      {kind: "model.request.started", data: {stage: "recommendation"}},
      {kind: "recommendation.generated", data: {count: 1}},
    ], true, true);
    expect(finished.every(stage => stage.state === "done")).toBe(true);

    const deterministic = modelDecisionStages([], false, true);
    expect(deterministic.every(stage => stage.state === "pending")).toBe(true);
  });

  it("分阶段调用从单 Review 指标读取", () => {
    const report = modelParticipation(
      {...liveBase, model_metrics: {requests: 5,
        requests_by_stage: {plan: 1, scheduling: 2, narration: 1,
          recommendation: 1}}}, []);
    expect(report.requestsByStage).toEqual(
      {plan: 1, scheduling: 2, narration: 1, recommendation: 1});
  });
});

describe("模型名称映射（6.2.3 跟随后端）", () => {
  it("按 provider 型号归一品牌，未知型号显示真实 model_id", () => {
    expect(modelLabel({model_id: "deepseek-v4-pro"})).toBe("DeepSeek");
    expect(modelLabel({model_id: "DeepSeek-Chat"})).toBe("DeepSeek");
    expect(modelLabel({model_id: "GLM-4.6"})).toBe("GLM");
    expect(modelLabel({model_id: "glm-5-air"})).toBe("GLM");
    expect(modelLabel({model_id: "qwen3-coder"})).toBe("qwen3-coder");
  });

  it("拿不到型号时用中性词，不硬编码任何品牌", () => {
    expect(modelLabel(null)).toBe("模型");
    expect(modelLabel(undefined)).toBe("模型");
    expect(modelLabel({})).toBe("模型");
    expect(modelLabel({model_id: "  "})).toBe("模型");
  });

  it("结论语与决策步骤使用传入的模型名", () => {
    const report = modelParticipation(
      {provider: {kind: "openai-compatible", model_id: "glm-4.6"},
       model_metrics: {requests: 4, fallbacks: 0},
       request: {model_provider: "live", agent_level: "l2"}}, []);
    expect(modelConclusion(report, "GLM")).toContain("GLM 观察真实实验后选择保持原顺序");
    expect(modelDecisionStages([], true, false, "GLM")[2].label)
      .toBe("GLM 正在判断下一步");
  });
});

describe("本轮修正", () => {
  it("完成语必须交代掉每一行，否则读者会自己做减法", () => {
    const s = summarySentence({
      total_added_lines: 48,
      by_label: {承重: 4, 无据: 10, 游离: 11},
      by_reason: {inert_withheld: 4},
    });
    expect(s).toContain("其余 19 行");
    expect(s).not.toContain("19 行为空行");
  });

  it("完成语按真实未标注原因解释余数", () => {
    const s = summarySentence({
      total_added_lines: 48,
      by_label: {承重: 4, 无据: 10, 游离: 11},
      by_reason: {inert_withheld: 4, non_executable: 11,
        unsupported_file: 7, not_isolated: 1},
    });
    expect(s).toContain("其余 19 行未形成三态结论");
    expect(s).toContain("11 行空行、注释等非执行行");
    expect(s).toContain("7 行属于不支持探测的文件");
    expect(s).toContain("1 行未能隔离归因");
  });

  it("四类刚好占满时不再画蛇添足", () => {
    const s = summarySentence({
      total_added_lines: 10,
      by_label: {承重: 4, 无据: 3, 游离: 3},
      by_reason: {},
    });
    expect(s).not.toContain("其余");
  });

  it("未标注原因的每个枚举值都必须有中文说明，不裸露英文标识符", () => {
    const reasons = ["non_executable", "not_measured", "budget_exhausted",
      "no_valid_transform", "not_isolated", "flaky_or_dirty_restore",
      "unsupported_file", "probe_timeout", "inert_withheld",
      "collateral_breakage", "environment_shift"];
    for (const reason of reasons) {
      expect(reasonLabel(reason)).not.toBe("");
      expect(reasonLabel(reason)).not.toMatch(/^[a-z_]+$/);
    }
    expect(reasonLabel("unknown_reason")).toBe("");
    expect(reasonLabel("collateral_breakage")).toContain("收集或导入失败");
    expect(reasonLabel("environment_shift")).toContain("跳过条件");
  });

  it("承重行要指回主张，不是指回覆盖率观测", () => {
    const bundle = {evidence_bundle: {ledger: [
      {record_id: "f1", record_type: "Fact"},
      {record_id: "c1", record_type: "Claim"},
      {record_id: "x1", record_type: "Experiment"},
    ]}};
    // 推导累积顺序是「先覆盖率、再实验、最后主张」，直接取第 0 条会开出一份
    // 覆盖率观测——那不是它之所以承重的理由。
    expect(decisiveEvidenceId({evidence_ids: ["f1", "x1", "c1"]}, bundle)).toBe("c1");
  });

  it("账本里查不到时退回第一条，旧 Bundle 不把 unit_id 冒充证据", () => {
    expect(decisiveEvidenceId({evidence_ids: ["f1"]}, null)).toBe("f1");
    expect(decisiveEvidenceId({evidence_ids: [], unit_id: "u9"}, null)).toBe("");
  });

  it("非执行行继承所在单元的结论时要标出来", () => {
    const doc = linePresentation({label: "未标注", reason: "inert_withheld",
                                  text: '    """服务端让我们等多久。"""'});
    expect(doc.inherited).toBe(true);
    const real = linePresentation({label: "承重", reason: null,
                                   text: "    base = exponential_delay(attempt)"});
    expect(real.inherited).toBe(false);
  });

  it("执行器绑定事件不再直出英文", () => {
    expect(eventLabel("executor.bound")).toBe("执行器已绑定");
  });
});

describe("ddmin 证书投影", () => {
  it("完整证书按原样投影且带作用域说明", () => {
    const views = minimizationViews({minimizations: [{
      anchor_id: "pkg/m.py", target_regression: "tests/test_m.py::test_needed",
      complete: true, certificate: {
        frozen_unit_ids: ["a", "b"], minimal_unit_ids: ["a"],
        one_minimal: true, removal_checks: [{removed_unit_id: "a"}],
        scope_note: "1-minimal only relative to the frozen atomic units",
      },
    }]});
    expect(views).toHaveLength(1);
    expect(views[0].frozenUnits).toBe(2);
    expect(views[0].minimalUnits).toBe(1);
    expect(views[0].oneMinimal).toBe(true);
    expect(views[0].removalChecks).toBe(1);
    expect(views[0].scopeNote).toContain("1-minimal only relative to");
    expect(views[0].incompleteReason).toBe("");
  });

  it("没能最小化的锚点保留原因而不是被丢掉", () => {
    const views = minimizationViews({minimizations: [{
      anchor_id: "pkg/m.py", target_regression: "t::x",
      complete: false, reason: "fewer_than_two_atoms",
    }]});
    expect(views[0].oneMinimal).toBe(false);
    expect(views[0].incompleteReason).toBe("fewer_than_two_atoms");
  });

  it("没跑 ddmin 时不凭空造证书", () => {
    expect(minimizationViews({})).toEqual([]);
    expect(minimizationViews(null)).toEqual([]);
  });
});

describe("恢复失败", () => {
  const base = [
    {kind: "baseline.completed", data: {declared_tests: 12, all_passed: true}},
    {kind: "probe.completed",
      data: {anchor_id: "pkg/core.py", regressed_tests: ["tests/t.py::test_x"]}},
  ];

  it("恢复未清干净时必须显示失败，不得显示为通过", () => {
    const stages = experimentStory([...base,
      {kind: "restore.verified",
        data: {anchor_id: "pkg/core.py", restored_clean: false}}]);
    const restore = stages.find(s => s.key === "restore")!;
    expect(restore.state).toBe("failed");
    expect(restore.detail).toContain("失败");
    expect(restore.detail).not.toContain("已恢复");
  });

  it("完全没有恢复事件时是待定，不是通过", () => {
    const restore = experimentStory(base).find(s => s.key === "restore")!;
    expect(restore.state).toBe("pending");
    expect(restore.detail).toContain("等待");
  });

  it("恢复干净时才算通过", () => {
    const stages = experimentStory([...base,
      {kind: "restore.verified",
        data: {anchor_id: "pkg/core.py", restored_clean: true}}]);
    expect(stages.find(s => s.key === "restore")!.state).toBe("complete");
  });
});

describe("能力状态", () => {
  const registry: Capability[] = [
    {id: "counterfactual_probe", title: "反事实实验", state: "verified",
     state_label: "已验证", runtime: "default", summary: "", gate: "", evidence: []},
    {id: "ddmin", title: "ddmin", state: "experimental",
     state_label: "实验性", runtime: "opt_in", summary: "", gate: "", evidence: []},
    {id: "evidence_auditor", title: "Auditor", state: "disabled",
     state_label: "已关闭", runtime: "unavailable", summary: "", gate: "", evidence: []},
    {id: "model_scheduling", title: "模型调度", state: "negative_result",
     state_label: "负结果", runtime: "opt_in", summary: "", gate: "", evidence: []},
  ];

  it("未达门槛的能力不会被折叠掉，而且排在已验证之后", () => {
    const groups = groupCapabilities(registry);
    expect(groups.map((g: {state: string}) => g.state)).toEqual(
      ["verified", "experimental", "negative_result", "disabled"]);
    expect(groups.every((g: {items: unknown[]}) => g.items.length > 0)).toBe(true);
  });

  it("关闭的能力在界面上就不可选", () => {
    expect(capabilityAvailable(registry, "evidence_auditor")).toBe(false);
    expect(capabilityAvailable(registry, "ddmin")).toBe(true);
    // 注册表尚未到达时不抢先禁用，服务器仍会再拒一次。
    expect(capabilityAvailable([], "ddmin")).toBe(true);
  });

  it("负结果与实验性用不同的色调，不能都渲染成通过", () => {
    expect(capabilityTone("verified")).toBe("ok");
    expect(capabilityTone("experimental")).toBe("warning");
    expect(capabilityTone("negative_result")).toBe("danger");
    expect(capabilityTone("disabled")).toBe("idle");
  });

  it("把一句话草案逐字段成行，默认预算必须标出来", () => {
    const draft: ReviewDraft = {
      repo_id: "repo-1", repo_display_name: "演示仓库",
      goal: "检查调度器改动的覆盖缺口", review_focus: "coverage-gap",
      budget_seconds: 300, success_conditions: ["具名测试保持通过"],
      non_goals: ["不修改代码"],
    };
    const rows = draftCardRows(draft, {budget_seconds: "default"});
    expect(rows.map(row => row.label)).toEqual(
      ["仓库", "审查目标", "审查侧重", "预算", "成功条件", "不做什么"]);
    expect(rows[2].value).toBe("检查覆盖缺口");
    expect(rows[3].value).toBe("300 秒（默认值，这句话没提预算）");
    expect(draftCardRows({...draft, budget_seconds: 600},
      {budget_seconds: "model"})[3].value).toBe("600 秒");
    expect(draftCardRows({...draft, success_conditions: [], non_goals: []}).length)
      .toBe(4);
    expect(reviewFocusLabel("made-up")).toBe("made-up");
  });

  it("locate 命中证据逐类翻译成中文，未识别的格式原样透出", () => {
    expect(locateEvidenceLabel("symbol", "symbol 'scaled' defined at line 4"))
      .toBe("符号 scaled 定义于第 4 行");
    expect(locateEvidenceLabel("filename", "filename contains 'calc'"))
      .toBe("文件名包含「calc」");
    expect(locateEvidenceLabel("directory", "directory contains 'pkg'"))
      .toBe("目录名包含「pkg」");
    // 后端目录命中实际发的是 kind="filename" + directory detail（modou/locate.py）。
    expect(locateEvidenceLabel("filename", "directory contains 'pkg'"))
      .toBe("目录名包含「pkg」");
    expect(locateEvidenceLabel("content", "content contains 'scaled' at line 2"))
      .toBe("第 2 行内容包含「scaled」");
    // 证据翻译错了比不翻译更糟：不认识的格式绝不编造中文。
    expect(locateEvidenceLabel("symbol", "unexpected format"))
      .toBe("unexpected format");
    expect(locateEvidenceLabel("future-kind", "future detail"))
      .toBe("future detail");
  });

  it("定位回退原因都有诚实的退回手动选仓库话术", () => {
    expect(locateFallbackNotice("no_hits")).toContain("手动选择仓库");
    expect(locateFallbackNotice("no_searchable_tokens")).toContain("检索的词");
    expect(locateFallbackNotice("unknown_future_reason")).toContain("手动选择仓库");
  });

  it("记忆卡翻译规则种类与状态，坏数据跳过不编造", () => {
    expect(memoryRuleViews([
      {memory_id: "backoff-tests", rule: "以 tests/test_backoff.py 为承重范围。",
       kind: "test_convention", applies_to: ["tests/test_backoff.py"]},
      {memory_id: "future-kind", rule: "新种类规则原文。", kind: "memory_kind_future",
       applies_to: []},
      {memory_id: "old-rule", rule: "已撤销的规则原文。", kind: "test_convention",
       applies_to: [], status: "revoked"},
      {memory_id: "", rule: "缺 id 的记录不展示"},
      {rule: "缺 id"},
      "junk",
    ])).toEqual([
      {memory_id: "backoff-tests", rule: "以 tests/test_backoff.py 为承重范围。",
       kind: "测试约定", applies_to: ["tests/test_backoff.py"], status: "active"},
      {memory_id: "future-kind", rule: "新种类规则原文。", kind: "memory_kind_future",
       applies_to: [], status: "active"},
      {memory_id: "old-rule", rule: "已撤销的规则原文。", kind: "测试约定",
       applies_to: [], status: "revoked"},
    ]);
    expect(memoryRuleViews(null)).toEqual([]);
    expect(memoryRuleViews("junk")).toEqual([]);
    expect(MEMORY_CARD_HEADLINE).toBe("上次你确认的规则，这次已生效");
    expect(memoryKindOptions().map(option => option.value)).toEqual(
      ["review_preference", "known_baseline", "test_convention", "architecture_constraint"]);
    expect(MEMORY_STATUS_LABELS.revoked).toBe("已撤销");
  });

  it("review_memory.loaded 事件有中文标签与条数小字", () => {
    expect(eventLabel("review_memory.loaded")).toBe("仓库记忆已载入");
    expect(schedulingDetail("review_memory.loaded", {active_records: 1}))
      .toBe("已载入 1 条已确认规则");
    expect(schedulingDetail("review_memory.loaded", {})).toBe("已载入 0 条已确认规则");
    // 事件行与卡片标题措辞不同：两处同屏时不会撞 e2e 唯一短语钩子。
    expect(schedulingDetail("review_memory.loaded", {active_records: 1}))
      .not.toContain("上次你确认的规则");
  });

  it("可采性四级有固定的人话标签，缺失时不编造", () => {
    expect(admissibilityView("A")).toMatchObject(
      {label: "A 级 · 行为证据", tone: "success"});
    expect(admissibilityView("B")).toMatchObject(
      {label: "B 级 · 间接证据", tone: "warning"});
    expect(admissibilityView("C")).toMatchObject(
      {label: "C 级 · 连带损坏", tone: "danger"});
    expect(admissibilityView("D")).toMatchObject(
      {label: "D 级 · 环境变动", tone: "warning"});
    expect(admissibilityView(null)).toBeNull();
    expect(admissibilityView(undefined)).toBeNull();
    expect(admissibilityView("X")).toBeNull();
  });

  it("旧口径承重而新判据 C/D 级时，同屏给出对比；其余不触发", () => {
    expect(admissibilityContrast({状态: "承重", 可采性: "C",
      可采性说明: "C：连带损坏：这条测试没有被执行（导入失败或收集期错误）"}))
      .toBe("旧口径：承重✅ · 新判据：C 级 · 连带损坏——连带损坏：这条测试没有被执行（导入失败或收集期错误）");
    expect(admissibilityContrast({状态: "承重", 可采性: "D"}))
      .toBe("旧口径：承重✅ · 新判据：D 级 · 环境变动");
    expect(admissibilityContrast({状态: "承重", 可采性: "A"})).toBeNull();
    expect(admissibilityContrast({状态: "惰性", 可采性: "C"})).toBeNull();
    expect(admissibilityContrast({状态: "承重"})).toBeNull();
  });

  it("签证与提议状态映射到固定措辞，未知状态退回未提议而不是猜", () => {
    expect(visaStatusView("VERIFIED_EFFECTIVE")).toMatchObject(
      {label: "已判定有效", tone: "success"});
    expect(visaStatusView("PASSED_NOT_EFFECTIVE")).toMatchObject(
      {label: "跑绿但未判定有效", tone: "warning"});
    expect(visaStatusView("WITHHELD_SMALL_CASE").label).toBe("保留判定 · 案例过小");
    expect(visaStatusView("WITHHELD_NO_HOLDOUT").label).toBe("保留判定 · 无保留集");
    expect(visaStatusView("VISA_ERROR")).toMatchObject(
      {label: "签证器故障", tone: "danger"});
    expect(visaStatusView("PASSED_WITH_REVISION").label).toBe("修订后通过");
    expect(visaStatusView("NEEDS_HUMAN").label).toBe("需要人工决定");
    expect(visaStatusView("NOT_PROPOSED").label).toBe("尚未提议");
    expect(visaStatusView(undefined).label).toBe("尚未提议");
  });
});

describe("人工处置", () => {
  it("答案词表与后端 _DISPOSITION_ANSWERS 逐字对齐", () => {
    // 这张表是界面能给出的全部答案。它和后端分叉的话，人在界面上选完会收到
    // 一个 DISPOSITION_ANSWER_INVALID，而界面并不知道自己错在哪。
    expect(Object.keys(DISPOSITION_ANSWERS).sort())
      .toEqual(["decision.requested", "test.needs_human"]);
    expect(dispositionOptions("test.needs_human").map(option => option.value))
      .toEqual(["write_manually", "retry_different_approach", "accept_gap"]);
    expect(dispositionOptions("decision.requested").map(option => option.value))
      .toEqual(["continue", "stop"]);
  });

  it("不能回应的事件不给入口", () => {
    // 面板靠这个返回空数组来决定不渲染自己：任何事件都能留痕的话，
    // 「它在问你」这句话就没有意义了。
    expect(dispositionOptions("plan.approved")).toEqual([]);
    expect(dispositionOptions("baseline.completed")).toEqual([]);
    expect(dispositionOptions("")).toEqual([]);
  });

  it("边界声明说清楚留痕不是控制", () => {
    // 后端 record_disposition 明确不改变审查状态。界面必须把这件事说出来，
    // 否则人会以为点完它就会接着跑。
    expect(DISPOSITION_BOUNDARY).toContain("不改变审查状态");
    expect(DISPOSITION_BOUNDARY).toContain("不替代人工批准");
  });

  it("升级与留痕两个事件都有中文文案", () => {
    expect(eventLabel("test.needs_human")).toBe("测试提议需要人工决定");
    expect(eventLabel("human.disposition.recorded")).toBe("人工处置已留痕");
  });
});

describe("计划修订气泡", () => {
  const chain = {
    schema_version: "review-plan-revisions-v1",
    revisions: [
      {review_id: "r1", review_spec_sha256: "a".repeat(64), status: "ABORTED",
       revision_reason: ""},
      {review_id: "r2", review_spec_sha256: "b".repeat(64), status: "ABORTED",
       revision_reason: "预算写错了"},
      {review_id: "r3", review_spec_sha256: "c".repeat(64),
       status: "AWAITING_APPROVAL", revision_reason: "范围还是太宽"},
    ],
  };

  it("按稿次排列，并标出当前那一稿", () => {
    const views = planRevisionViews(chain, "r3");
    expect(views.map(v => v.round)).toEqual([1, 2, 3]);
    expect(views.map(v => v.current)).toEqual([false, false, true]);
    expect(views.map(v => v.reason)).toEqual(["", "预算写错了", "范围还是太宽"]);
  });

  it("每一稿带自己的指纹，截断到能对照的长度", () => {
    // 指纹是人批准时签的东西。每稿一个不同的指纹，才画得出"这是另一份计划"。
    const views = planRevisionViews(chain, "r3");
    expect(views.map(v => v.fingerprint))
      .toEqual(["a".repeat(12), "b".repeat(12), "c".repeat(12)]);
    expect(new Set(views.map(v => v.fingerprint)).size).toBe(3);
  });

  it("只有一稿时不画气泡", () => {
    // 一条「第 1 稿」的时间线什么也没说，反而让人以为发生过修订。
    expect(planRevisionViews({revisions: [chain.revisions[0]]}, "r1")).toEqual([]);
    expect(planRevisionViews({revisions: []}, "r1")).toEqual([]);
    expect(planRevisionViews(null)).toEqual([]);
    expect(planRevisionViews({revisions: "nope"})).toEqual([]);
  });

  it("边界声明说清楚修订会作废上一稿而不是打补丁", () => {
    expect(PLAN_REVISION_BOUNDARY).toContain("作废上一稿");
    expect(PLAN_REVISION_BOUNDARY).toContain("新的指纹");
  });

  it("修订相关的两个事件都有中文文案", () => {
    expect(eventLabel("plan.revised")).toBe("计划已按你的修订重出一稿");
    expect(eventLabel("plan.superseded")).toBe("上一稿计划已作废");
  });
});

describe("eventDeltas", () => {
  const rows = [
    {event_id: "a", kind: "probe.completed",
      data: {anchor_id: "pkg/core.py", regressed_tests: ["t::one"], spent: 3}},
    {event_id: "b", kind: "probe.completed",
      data: {anchor_id: "pkg/pool.py", regressed_tests: ["t::one"], spent: 3}},
    {event_id: "c", kind: "probe.completed",
      data: {anchor_id: "pkg/pool.py", regressed_tests: ["t::one"], spent: 3}},
    {event_id: "d", kind: "restore.verified", data: {restored_clean: true}},
  ];

  it("第一次出现给完整字段，并标成首次", () => {
    const out = eventDeltas(rows);
    expect(out.a.first).toBe(true);
    expect(out.a.fields).toContain("anchor_id pkg/core.py");
    expect(out.a.fields).toContain("spent 3");
  });

  it("同类事件之后只列变化的字段", () => {
    const out = eventDeltas(rows);
    expect(out.b.first).toBe(false);
    expect(out.b.fields).toEqual(["anchor_id pkg/pool.py"]);
  });

  it("完全没有变化时一个字段都不列", () => {
    expect(eventDeltas(rows).c.fields).toEqual([]);
  });

  it("换一种事件就重新按首次处理", () => {
    const out = eventDeltas(rows);
    expect(out.d.first).toBe(true);
    expect(out.d.fields).toEqual(["restored_clean true"]);
  });

  it("长数组折成计数，不把整条 payload 铺到时间线上", () => {
    const out = eventDeltas([{event_id: "x", kind: "k",
      data: {tests: ["a", "b", "c", "d"]}}]);
    expect(out.x.fields).toEqual(["tests a、b 等 4 项"]);
  });

  it("超长取值截断，不撑破按钮", () => {
    const out = eventDeltas([{event_id: "y", kind: "k", data: {note: "长".repeat(80)}}]);
    expect(out.y.fields[0].length).toBeLessThanOrEqual(50);
    expect(out.y.fields[0].endsWith("…")).toBe(true);
  });
});

describe("classifyError 的两模式失败关闭码", () => {
  it("模式与调度不一致时给中文原因和下一步，不是裸码", () => {
    const notice = classifyError(400, "PRODUCT_MODE_PROVIDER_MISMATCH", "mismatch");
    expect(notice.title).toBe("审查模式与调度方式不一致");
    expect(notice.message).toContain("切换");
    expect(notice.code).toBe("PRODUCT_MODE_PROVIDER_MISMATCH");
    expect(notice.status).toBe(400);
  });

  it("标准审查里被拒的模型入口，要指出去哪个模式做，而不是只说被拒", () => {
    const notice = classifyError(400, "STANDARD_MODE_MODEL_FORBIDDEN", "forbidden");
    expect(notice.title).toBe("这个动作需要智能体审查");
    expect(notice.message).toContain("智能体审查");
  });

  it("非法模式取值给中文原因", () => {
    expect(classifyError(400, "PRODUCT_MODE_INVALID", "bad").title)
      .toBe("审查模式取值不合法");
  });

  it("证据包校验的问题码不冒充 HTTP 错误文案", () => {
    // bundle_v2.verify() 的问题码不是 IntakeError，服务端不会以 HTTP 发出。
    // 给它们预置文案，等于界面备好了一段永远走不到的话。
    const notice = classifyError(409, "STANDARD_MODE_MODEL_ACTIVITY", "model activity");
    expect(notice.title).toBe("当前操作暂时不能执行");
  });
});

describe("releaseGateLabel", () => {
  it("已知门禁给中文摘要", () => {
    expect(releaseGateLabel("independent_hidden_qa")).toBe("独立隐藏质量验证");
  });

  it("没收录的键原样回显，不吞掉", () => {
    expect(releaseGateLabel("some_future_gate")).toBe("some_future_gate");
  });
});

describe("shortHash", () => {
  const digest = "a".repeat(64);

  it("真摘要统一切到 12 位，并把全量留在 full 里", () => {
    const out = shortHash(digest);
    expect(out.short).toHaveLength(12);
    expect(out.full).toBe(digest);
    expect(out.truncated).toBe(true);
  });

  it("短的占位值原样保留——切了会变成一个像真摘要的假值", () => {
    const out = shortHash("plan-demo-sha");
    expect(out.short).toBe("plan-demo-sha");
    expect(out.truncated).toBe(false);
  });

  it("不是十六进制的长字符串也不切", () => {
    const out = shortHash("这是一段很长的中文说明而不是一个摘要值不应该被当成摘要切断");
    expect(out.truncated).toBe(false);
  });

  it("空值不会变成 undefined 字样", () => {
    expect(shortHash(null).short).toBe("");
    expect(shortHash(undefined).full).toBe("");
  });
});

describe("第四波接出来的展示层字段", () => {
  it("两条安全行为事件有了中文名，不再落进未翻译兜底", () => {
    const reexecuted = eventPresentation({
      kind: "approval.reexecuted_after_plan_drift",
    });
    const cancelled = eventPresentation({kind: "scope.expansion_cancelled"});
    expect(reexecuted.title).toBe("计划漂移后原批准作废，新计划需重新批准");
    expect(cancelled.title).toBe("计划修订试图扩大审查范围，已中止执行");
    expect(reexecuted.title).not.toContain("尚未翻译");
    expect(cancelled.title).not.toContain("尚未翻译");
  });

  it("两条复验事件有了中文名，不再落进未翻译兜底", () => {
    const requested = eventPresentation({kind: "review.reverification.requested"});
    const approved = eventPresentation({kind: "review.reverification.approved"});
    expect(requested.title).toBe("复验已发起");
    expect(approved.title).toBe("复验已确认，开始重放");
    expect(requested.explanation).toContain("等你确认");
    expect(approved.explanation).toContain("重放");
    expect(requested.title).not.toContain("尚未翻译");
    expect(approved.title).not.toContain("尚未翻译");
  });

  it("数组摘要给前两项原文，超过两项才补总数", () => {
    const summaryOf = (data: Record<string, unknown>) => eventPresentation({
      kind: "experiment.replayed", data,
    }).technicalSummary;
    const shown = eventPresentation({
      kind: "experiment.replayed",
      data: {tests: ["test_login_ok", "test_order_total", "test_refund_case"]},
    }).technicalSummary;
    expect(shown).toContain("test_login_ok");
    expect(shown).toContain("等 3 项");
    const pair = summaryOf({tests: ["a", "b"]});
    expect(pair).toContain("a、b");
    expect(pair).not.toContain("等 2 项");
  });

  it("未强制执行限制给中文名，未知键原样透出", () => {
    const sandbox = unenforcedLimitsView({
      unenforced_limits: ["max_memory_bytes", "max_processes", "max_disk_bytes",
        "max_files", "max_output_bytes"],
    });
    expect(sandbox).toEqual(["内存用量上限", "进程数上限", "磁盘写入上限",
      "可写文件数上限", "输出大小上限"]);
    expect(unenforcedLimitsView({unenforced_limits: ["future_limit_key"]}))
      .toEqual(["future_limit_key"]);
    expect(unenforcedLimitsView(null)).toEqual([]);
    expect(unenforcedLimitsView({})).toEqual([]);
  });

  it("证书逐条定级按 per-test 行映射，缺字段回空数组", () => {
    const rows = certificateGradeRows({
      "逐条定级": [
        {test_id: "test_login_ok", before: "passed", after: "failed", "等级": "承重"},
        {test_id: "test_order_total", before: "passed", after: "passed", "等级": "无据"},
      ],
    });
    expect(rows).toEqual([
      {testId: "test_login_ok", before: "passed", after: "failed", grade: "承重"},
      {testId: "test_order_total", before: "passed", after: "passed", grade: "无据"},
    ]);
    expect(certificateGradeRows(null)).toEqual([]);
    expect(certificateGradeRows({})).toEqual([]);
    expect(certificateGradeRows({"逐条定级": "不是数组"})).toEqual([]);
  });

  it("界面探针状态翻中文，未知值不猜", () => {
    expect(uiProbeStatusView("PENDING_REPREFLIGHT"))
      .toBe("待重新预检（界面探针尚未执行）");
    expect(uiProbeStatusView("PASSED")).toBe("界面探针已通过");
    expect(uiProbeStatusView("FAILED")).toBe("界面探针未通过");
    expect(uiProbeStatusView("SOME_NEW_STATE")).toBe("SOME_NEW_STATE");
    expect(uiProbeStatusView(null)).toBe("");
    expect(uiProbeStatusView(undefined)).toBe("");
  });

  it("窄口径包自检只列后端给过的项，三态分明", () => {
    const checks = narrowPackageChecksView({
      build: true, tests: false, license_audit: "pending",
    });
    expect(checks).toEqual([
      {label: "构建", state: "通过"},
      {label: "测试", state: "未通过"},
      {label: "许可证审计", state: "未提供"},
    ]);
    expect(narrowPackageChecksView(null)).toEqual([]);
    expect(narrowPackageChecksView({sbom: true})).toEqual([{label: "SBOM", state: "通过"}]);
  });

  it("门禁名走中文名映射，未知名不硬造", () => {
    expect(releaseGateLabel("independent_hidden_qa")).toBe("独立隐藏质量验证");
    expect(releaseGateLabel("future_gate")).toBe("future_gate");
  });
});

describe("升级率度量（D1.2 autonomy 投影）", () => {
  it("新包读出自主/升级次数与升级率，字段名与服务端一致", () => {
    const definition = "autonomous=scheduler.action+model.action; "
      + "escalations=decision.requested+decision.defaulted";
    expect(autonomyView({narration: {autonomy: {
      autonomous_decisions: 7, escalations: 2, escalation_rate: 0.222222,
      definition,
    }}})).toEqual({
      autonomous_decisions: 7, escalations: 2, escalation_rate: 0.222222,
      definition,
    });
  });

  it("旧 Bundle 没有该字段时返回 null，不把缺失伪造成 0", () => {
    expect(autonomyView({narration: {scope_note: "结论仅适用于本次声明测试范围。"}})).toBeNull();
    expect(autonomyView({narration: {}})).toBeNull();
    expect(autonomyView({})).toBeNull();
    expect(autonomyView(null)).toBeNull();
    expect(autonomyView()).toBeNull();
  });

  it("确定性运行如实呈现 0/0/0.0，不粉饰成参与", () => {
    const view = autonomyView({narration: {autonomy: {
      autonomous_decisions: 0, escalations: 0, escalation_rate: 0.0,
      definition: "autonomous=scheduler.action+model.action; "
        + "escalations=decision.requested+decision.defaulted",
    }}});
    // 0/0 是真记录，不是缺失——区别在于 definition 在场。
    expect(view).not.toBeNull();
    expect(view?.autonomous_decisions).toBe(0);
    expect(view?.escalations).toBe(0);
    expect(view?.escalation_rate).toBe(0);
    expect((view?.definition || "").length).toBeGreaterThan(0);
  });
});

describe("常驻授权状态卡", () => {
  it("loaded 态如实给出授权人、仓库、期限与预算", () => {
    const view = standingAuthorizationView({
      loaded: true, authorized_by: "杨佩立",
      source_sha256_prefix: "abc123def456", valid_until: "2026-09-19T23:59:59+08:00",
      expired: false, repo_count: 3,
      repo_whitelist: ["boltons", "more-itertools", "toolz"],
      budget_seconds_max: 900, max_writes_per_review: 4, max_writes_per_hour: 30,
    });
    expect(view.loaded).toBe(true);
    expect(view.authorizedBy).toBe("杨佩立");
    expect(view.sourcePrefix).toBe("abc123def456");
    expect(view.repos).toEqual(["boltons", "more-itertools", "toolz"]);
    expect(view.budgetSecondsMax).toBe(900);
    expect(view.maxWritesPerReview).toBe(4);
    expect(view.maxWritesPerHour).toBe(30);
    expect(view.expired).toBe(false);
  });

  it("未加载或读到垃圾时显示 unloaded，不把缺却说成有", () => {
    const unloaded = standingAuthorizationView({loaded: false});
    expect(unloaded.loaded).toBe(false);
    expect(unloaded.repos).toEqual([]);
    // 端点读失败时前端用 null 兜底，也不能看起来像加载过。
    expect(standingAuthorizationView(null).loaded).toBe(false);
    expect(standingAuthorizationView(undefined).loaded).toBe(false);
    expect(standingAuthorizationView("junk").loaded).toBe(false);
  });

  it("过期标记原样透传，UI 才能换警示色", () => {
    const view = standingAuthorizationView({loaded: true, expired: true,
      authorized_by: "", valid_until: "2020-01-01T00:00:00+08:00"});
    expect(view.expired).toBe(true);
    expect(view.validUntil).toBe("2020-01-01T00:00:00+08:00");
  });
});

describe("评测回执浏览器", () => {
  it("normalizeEvalReceiptIndex 保留 verdict 与判据计数，过滤无 id 行", () => {
    const entries = normalizeEvalReceiptIndex({receipts: [
      {id: "receipt-formal-v2", verdict: "FAIL", criteria_passed: 14, criteria_total: 15,
        freeze_sha256: "6bbd915f8cc2", finished_at: "2026-09-16T17:37:00+0800",
        source: "internal receipt (path redacted)"},
      {verdict: "PASS"},
    ]});
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({id: "receipt-formal-v2", verdict: "FAIL",
      criteriaPassed: 14, criteriaTotal: 15});
    expect(normalizeEvalReceiptIndex(null)).toEqual([]);
    expect(normalizeEvalReceiptIndex({receipts: "junk"})).toEqual([]);
  });

  it("normalizeEvalReceipt 按 v2 回执真实形状渲染 FAIL 14/15 与双臂对照", () => {
    // 形状取自一次真实 L3 回执（内部路径与标识脱敏，裁剪到与
    // normalize 相关的字段）：FAIL 必须原样显示，不粉饰。
    const view = normalizeEvalReceipt({
      schema_version: "l3-unattended-receipt-v1", verdict: "FAIL", smoke: false,
      started_at: "2026-09-16T17:15:08+0800", finished_at: "2026-09-16T17:37:00+0800",
      freeze: {path: "internal-freeze/redacted.json",
        sha256: "6bbd915f8cc2d6d74c546be6937abe29a0bdab2afa9c849bd31a309720877ee4"},
      criteria: [
        {id: "l3_zero_human_approvals", metric: "l3_human_approvals", op: "<=",
          threshold: 0, text: "L3 臂零人工批准", observed: 0, passed: true},
        {id: "at_least_one_write_verified", metric: "l3_writes_verified", op: ">=",
          threshold: 1, text: "至少一次自主写入走到 verified", observed: 0, passed: false},
      ],
      arms: {
        standing_authorization: {arm: "standing_authorization",
          reviews: [{human_approvals: 0}, {human_approvals: 0}],
          writes: [{attempted: true, verify_status: "REPAIR_PATCH_ABSENT", error: "x"}]},
        human_approved: {arm: "human_approved",
          reviews: [{human_approvals: 1}, {human_approvals: 1}], writes: []},
      },
    });
    expect(view?.verdict).toBe("FAIL");
    expect(view?.verdictLabel).toBe("未通过");
    expect(view?.criteriaPassed).toBe(1);
    expect(view?.criteria[1].comparison).toBe(">= 1");
    expect(view?.criteria[1].observed).toBe("0");
    expect(view?.criteria[1].passed).toBe(false);
    expect(view?.freezeSha256).toContain("6bbd915f");
    const l3 = view?.arms.find(arm => arm.arm === "standing_authorization");
    expect(l3).toMatchObject({label: "L3 · 常驻授权臂", reviews: 2,
      humanApprovals: 0, writesAttempted: 1, writesVerified: 0});
    const l2 = view?.arms.find(arm => arm.arm === "human_approved");
    expect(l2).toMatchObject({label: "L2 · 人工批准对照臂", reviews: 2, humanApprovals: 2});
  });

  it("v3 回执的 verified 布尔与 v2 的 verify_status 两种口径都认", () => {
    const view = normalizeEvalReceipt({verdict: "PASS", criteria: [],
      arms: {standing_authorization: {reviews: [], writes: [
        {attempted: true, verified: true, verify_status: "VERIFIED"},
        {attempted: true, verified: false, verify_status: "SCHEMA_REJECTED"},
      ]}}});
    const l3 = view?.arms.find(arm => arm.arm === "standing_authorization");
    expect(l3?.writesVerified).toBe(1);
    expect(l3?.writesAttempted).toBe(2);
  });

  it("空数据返回 null，不造一份假回执", () => {
    expect(normalizeEvalReceipt(null)).toBeNull();
    expect(normalizeEvalReceipt({})).toBeNull();
    expect(normalizeEvalReceipt("junk")).toBeNull();
  });
});


describe("历史回执的版本有效性", () => {
  it("过期 PASS 不再显示当前版本通过", () => {
    const raw = {id: "receipt-old", verdict: "PASS", criteria: [], source_binding: {status: "stale"}};
    expect(normalizeEvalReceipt(raw)?.verdictLabel).toBe("历史通过");
    expect(normalizeEvalReceipt({...raw, verdict: "FAIL"})?.verdictLabel).toBe("历史未通过");
    expect(normalizeEvalReceipt(raw)?.verdict).toContain("当前版本待复验");
    expect(normalizeEvalReceiptIndex({receipts: [raw]})[0].verdict).toContain("当前版本待复验");
    expect(raw.verdict).toBe("PASS");
  });
});
