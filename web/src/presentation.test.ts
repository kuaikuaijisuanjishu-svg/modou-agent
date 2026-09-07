import { describe, expect, it } from "vitest";
import {
  classifyError, decisiveEvidenceId, eventLabel, linePresentation, normalizePastedToken,
  resolveStartupToken, schedulingDetail, statusView, summarySentence,
  eventPhase, experimentStory, groupEventPhases, judgeCounts, minimizationViews,
  modeLabels, capabilityAvailable, capabilityTone, groupCapabilities, emphasizeNumbers,
  modelConclusion, modelDecisionStages, modelParticipation, workspacePresentation,
  repairStatusView,
  laneOf, laneEvents, evidencePassport,
  type Capability,
} from "./presentation";

describe("Review Cockpit presentation", () => {
  it("keeps a fragment token across refresh through session storage", () => {
    expect(resolveStartupToken("#token=fresh", "old")).toBe("fresh");
    expect(resolveStartupToken("", "fresh")).toBe("fresh");
    expect(normalizePastedToken("http://127.0.0.1:8765/#token=abc%2F123")).toBe("abc/123");
  });

  it("maps internal states and events to the review story", () => {
    expect(statusView("VERIFYING_RESTORE")).toMatchObject({label: "检查完整恢复", step: 7});
    expect(statusView("COMPLETE")).toMatchObject({label: "审查完成", step: 8});
    expect(statusView("CANCELLING")).toMatchObject({label: "正在安全中止", tone: "warning"});
    expect(statusView("CLEANUP_REQUIRED")).toMatchObject({label: "需要清理", tone: "danger"});
    expect(eventLabel("claim.derived")).toBe("证据主张已签发");
    expect(eventLabel("repair.delivered")).toBe("修复已验证并交付");
  });

  it("separates recoverable errors from authentication failures", () => {
    expect(classifyError(409, "STALE_PLAN", "stale").recovery).toBe("reload-plan");
    expect(classifyError(401, "AUTH_REQUIRED", "bad").recovery).toBe("token");
    expect(classifyError(undefined, undefined, "offline").level).toBe("network");
    expect(classifyError(409, "SOURCE_SNAPSHOT_CHANGED", "changed").title).toBe("修复证据已经过期");
    expect(classifyError(409, "REPAIR_BRANCH_EXISTS", "branch").title).toBe("修复分支发生冲突");
    expect(classifyError(400, "REPAIR_VERIFICATION_FAILED", "failed").title).toBe("补丁验证未通过");
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

  it("keeps judge counts to the formal three states plus total", () => {
    expect(judgeCounts({total_added_lines: 29, by_label: {
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

  it("folds raw events into four stable judge phases without losing failures", () => {
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
    expect(report).toMatchObject({live: true, modelLabel: "deepseek-v4-pro",
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
    expect(laneOf({kind: "observation.recorded"})).toBe("测试作证");
    expect(laneOf({kind: "unknown.future_event"})).toBe("水木验码执行");
  });

  it("uses three lanes for live and two for deterministic runs", () => {
    const events = [
      {kind: "model.request.started"}, {kind: "baseline.completed"},
      {kind: "observation.recorded"},
    ];
    expect(laneEvents(events, true).map(group => [group.lane, group.events.length])).toEqual([
      ["模型建议", 1], ["水木验码执行", 1], ["测试作证", 1],
    ]);
    expect(laneEvents(events, false).map(group => group.lane)).toEqual([
      "水木验码执行", "测试作证",
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
      status: "COMPLETE", statusLabel: "验讫", addedLines: 22, loadLines: 4,
      namedFailures: 2, unevidencedLines: 3, driftLines: 2,
      restoreStatus: "verified", restoreLabel: "已验证", planFingerprint: "plan-demo-sha",
      bundleAvailable: true, live: false, recommendationLabel: "不适用（确定性运行）",
    });
  });

  it("uses the four fixed status stamps", () => {
    expect(["COMPLETE", "PARTIAL", "FAILED", "ABORTED"].map(status =>
      evidencePassport(status, null).statusLabel)).toEqual(["验讫", "部分完成", "未签发", "已终止"]);
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
        failureReason: "", failureCode: "", skippedReason: "", model: "", promptVersion: ""},
    }, true);
    expect(passport).toMatchObject({live: true, modelLabel: "deepseek-v4-pro",
      modelCalls: 3, recommendationLabel: "已生成 1 条"});
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
});
