export const SESSION_TOKEN_KEY = "modou.session.token";
export const SESSION_REVIEW_KEY = "modou.session.review_id";

export const COMMON_UNCOVERED = [
  "结论只在已声明的测试范围（FAIL_TO_PASS ∪ PASS_TO_PASS）内成立",
  "不等于语义等价：未被这批测试覆盖的路径、外部副作用、跨进程行为均未验证",
];

export type StatusView = {
  label: string;
  step: number;
  total: number;
  tone: "idle" | "active" | "success" | "warning" | "danger";
};

const STATUS: Record<string, StatusView> = {
  CREATED: { label: "准备审查", step: 1, total: 8, tone: "active" },
  INTAKE_VALIDATED: { label: "输入已核验", step: 1, total: 8, tone: "active" },
  PLAN_DRAFTED: { label: "计划已生成", step: 2, total: 8, tone: "active" },
  AWAITING_APPROVAL: { label: "等待确认计划", step: 3, total: 8, tone: "warning" },
  PLAN_FROZEN: { label: "计划已确认", step: 4, total: 8, tone: "active" },
  BASELINE_RUNNING: { label: "验证测试基线", step: 5, total: 8, tone: "active" },
  EXECUTING: { label: "执行证据实验", step: 6, total: 8, tone: "active" },
  VERIFYING_RESTORE: { label: "检查完整恢复", step: 7, total: 8, tone: "active" },
  SYNTHESIZING: { label: "汇总结论", step: 8, total: 8, tone: "active" },
  CANCELLING: { label: "正在安全中止", step: 8, total: 8, tone: "warning" },
  RECOVERING: { label: "正在恢复检查", step: 7, total: 8, tone: "active" },
  CLEANUP_REQUIRED: { label: "需要清理", step: 8, total: 8, tone: "danger" },
  QUARANTINED: { label: "已隔离", step: 8, total: 8, tone: "danger" },
  COMPLETE: { label: "审查完成", step: 8, total: 8, tone: "success" },
  PARTIAL: { label: "部分完成", step: 8, total: 8, tone: "warning" },
  FAILED: { label: "审查失败", step: 8, total: 8, tone: "danger" },
  ABORTED: { label: "审查已中止", step: 8, total: 8, tone: "danger" },
};

export function statusView(status?: string | null): StatusView {
  if (!status) return { label: "尚未开始", step: 0, total: 8, tone: "idle" };
  return STATUS[status] || { label: status, step: 0, total: 8, tone: "warning" };
}

const EVENT_LABELS: Record<string, string> = {
  "review.created": "审查已创建",
  "intake.validated": "输入范围已核验",
  "repo.inspected": "仓库信息已读取",
  "patch.inspected": "补丁新增内容已解析",
  "universe.frozen": "检查对象已固定",
  "plan.drafted": "审查计划已生成",
  "plan.awaiting_approval": "等待人工确认计划",
  "plan.approved": "计划已获批准",
  "plan.frozen": "计划已确认",
  "approval.accepted": "人工批准执行",
  "tool.started": "工具开始执行",
  "tool.completed": "工具执行完成",
  "baseline.started": "基线验证开始",
  "baseline.completed": "基线验证通过",
  "test_scope.collected": "测试范围已收集",
  "ddmin.routed": "已判定哪些对象可做最小化",
  "minimization.skipped": "最小化不适用，已说明原因",
  "minimization.completed": "最小化已给出证书",
  "probe.started": "证据实验开始",
  "probe.completed": "证据实验完成",
  "restore.started": "恢复检查开始",
  "restore.completed": "工作区恢复完成",
  "restore.verified": "工作区恢复已验证",
  "claim.derived": "证据主张已签发",
  "observation.recorded": "中间观测已记录",
  "model.action": "观测后动作已决定",
  "model.fallback": "调度切换为确定性模式",
  "model.request.started": "模型调用开始",
  "model.request.completed": "模型调用完成",
  "recommendation.generated": "只读建议已生成",
  "recommendation.failed": "建议生成失败，不影响实验结论",
  "scheduler.deterministic_order": "确定性初始排序已应用",
  "scheduler.next": "继续下一个检查对象",
  "run.synthesizing": "正在汇总结论",
  "run.completed": "证据产物已发布",
  "narrator.compiled": "结论说明已编译",
  "executor.bound": "执行器已绑定",
  "review.completed": "审查完成",
  "review.failed": "审查失败",
  "review.cancel_requested": "已请求安全中止",
  "review.cancellation_observed": "执行器已观察到中止请求",
  "review.cancelled": "审查已安全中止",
  "review.resumed": "已从重启中止记录恢复",
  "review.resume_created": "已创建恢复审查",
  "repair.delivered": "修复已验证并交付",
};

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] || kind.replaceAll(".", " · ");
}

export type RepairStatusView = {
  label: string;
  detail: string;
  tone: "idle" | "active" | "success" | "warning" | "danger";
};

export function repairStatusView(status?: string | null): RepairStatusView {
  switch (status) {
    case "DELIVERED":
      return {label: "已交付", detail: "补丁已通过声明测试，并写入本地修复分支。", tone: "success"};
    case "INCOMPLETE":
      return {label: "需要清理", detail: "交付工件不完整，禁止继续提交或宣称交付成功。", tone: "danger"};
    case "EXPIRED":
      return {label: "工件已过期", detail: "源仓库快照已变化，需要重新生成审查证据。", tone: "warning"};
    case "BRANCH_CONFLICT":
      return {label: "分支冲突", detail: "目标修复分支已经存在，未覆盖任何已有分支。", tone: "warning"};
    case "CLEANUP_REQUIRED":
      return {label: "需要清理", detail: "隔离工作区仍有残留，需完成清理后才能再次交付。", tone: "danger"};
    case "NOT_DELIVERED":
    default:
      return {label: "尚未交付", detail: "只有明确授权且补丁验证通过后，才会创建本地分支。", tone: "idle"};
  }
}

export function schedulingDetail(kind: string, data: Record<string, unknown>): string {
  if (kind === "scheduler.next") {
    const before = String(data.previous_next_anchor || "");
    const after = String(data.actual_next_anchor || "");
    const reason = String(data.priority_reason || "");
    return before && after && before !== after
      ? `${before} → ${after}${reason ? ` · ${reason}` : ""}`
      : `${after}${reason ? ` · ${reason}` : ""}`;
  }
  if (kind === "policy.rejected") return `已拒绝 · ${String(data.code || "POLICY_REJECTED")}`;
  if (kind === "model.action") return String(data.reason || data.kind || "");
  return "";
}

export function resolveStartupToken(hash: string, stored: string | null): string {
  const fromFragment = new URLSearchParams(hash.replace(/^#/, "")).get("token")?.trim();
  return fromFragment || stored?.trim() || "";
}

export function normalizePastedToken(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  const marker = "#token=";
  const markerAt = value.indexOf(marker);
  if (markerAt >= 0) return decodeURIComponent(value.slice(markerAt + marker.length).split("&")[0]);
  if (value.startsWith("token=")) return decodeURIComponent(value.slice(6).split("&")[0]);
  return value;
}

export type UiNotice = {
  level: "warning" | "danger" | "network";
  title: string;
  message: string;
  code?: string;
  recovery?: "token" | "reload-plan" | "retry";
};

export function classifyError(status: number | undefined, code: string | undefined,
                              message: string): UiNotice {
  if (status === 401 || code === "AUTH_REQUIRED" || code === "TOKEN_MISSING") {
    return { level: "danger", title: "需要重新连接本次服务", message,
      code: code || "AUTH_REQUIRED", recovery: "token" };
  }
  if (status === 409 && code === "STALE_PLAN") {
    return { level: "warning", title: "计划已经更新", message,
      code, recovery: "reload-plan" };
  }
  if (code === "SOURCE_SNAPSHOT_CHANGED" || code === "REPAIR_EVIDENCE_INVALID") {
    return { level: "warning", title: "修复证据已经过期", message, code,
      recovery: "reload-plan" };
  }
  if (code === "REPAIR_BRANCH_EXISTS") {
    return { level: "warning", title: "修复分支发生冲突", message, code };
  }
  if (code === "REPAIR_VERIFICATION_FAILED" || code === "REPAIR_PATCH_REJECTED") {
    return { level: "warning", title: "补丁验证未通过", message, code };
  }
  if (code === "REPAIR_WORKTREE_DIRTY" || code === "REPAIR_CLEANUP_REQUIRED") {
    return { level: "danger", title: "隔离工作区需要清理", message, code };
  }
  if (code === "REPAIR_NOT_AUTHORIZED") {
    return { level: "warning", title: "计划没有修复交付权限", message, code };
  }
  if (status === 409) {
    return { level: "warning", title: "当前操作暂时不能执行", message, code };
  }
  if (!status) {
    return { level: "network", title: "本地服务连接中断", message,
      code: code || "NETWORK_ERROR", recovery: "retry" };
  }
  return { level: "danger", title: "审查请求未完成", message, code };
}

const NON_EXECUTABLE_TEXT = /^\s*($|#|"""|'''|[)\]},:]+\s*$)/;

export function linePresentation(
  line: {label: string; reason?: string | null; text?: string},
) {
  // 非执行行（空行、注释、纯括号）本身无所谓执行不执行；它拿到结论，
  // 是因为**继承了所在单元**（label.py 的既定设计）。不标出来的话，
  // 相邻两行同为文档字符串却一个「惰性扣下」一个「未标注」，看着像 bug。
  const inherited = Boolean(line.text) && NON_EXECUTABLE_TEXT.test(line.text || "")
    && line.reason !== "non_executable" && line.reason !== "not_measured";
  if (line.reason === "inert_withheld") {
    return { badge: "惰性扣下", className: "reason-inert-withheld", inherited };
  }
  return { badge: line.label, className: `label-${line.label}`, inherited };
}

export function summarySentence(summary?: Record<string, unknown> | null): string {
  if (!summary) return "";
  const byLabel = (summary.by_label || {}) as Record<string, number>;
  const byReason = (summary.by_reason || {}) as Record<string, number>;
  const total = Number(summary.total_added_lines || 0);
  const load = Number(byLabel["承重"] || 0);
  const unevidenced = Number(byLabel["无据"] || 0);
  const drift = Number(byLabel["游离"] || 0);
  const withheld = Number(byReason.inert_withheld || 0);
  const named = load + unevidenced + drift + withheld;
  const rest = Math.max(0, total - named);
  const reasonText: Record<string, string> = {
    non_executable: "空行、注释等非执行行",
    not_measured: "未被覆盖率测量",
    budget_exhausted: "因预算耗尽未完成判定",
    no_valid_transform: "无法形成合法反事实变换",
    not_isolated: "未能隔离归因",
    flaky_or_dirty_restore: "因不稳定或恢复异常未发布结论",
    unsupported_file: "属于不支持探测的文件",
    probe_timeout: "因探测超时未完成",
  };
  const reasonParts = Object.entries(reasonText)
    .map(([reason, text]) => ({count: Number(byReason[reason] || 0), text}))
    .filter(item => item.count > 0);
  const explained = reasonParts.reduce((sum, item) => sum + item.count, 0);
  const detail = explained === rest && reasonParts.length > 0
    ? `：${reasonParts.map(item => `${item.count} 行${item.text}`).join("，")}`
    : "";
  // 四类之外还剩多少必须说出来，但不能为了数字闭合把所有未标注原因
  // 都冒充成空行/注释；只有 by_reason 与余数一致时才展开真实分项。
  const tail = rest > 0
    ? `；其余 ${rest} 行未形成三态结论${detail}。`
    : "。";
  return `本次审查覆盖 ${total} 行新增代码：${load} 行获得测试承重证据，`
    + `${unevidenced} 行未被测试执行，${drift} 行判为游离，`
    + `${withheld} 行惰性结论被护栏扣下${tail}`;
}

export type JudgeCount = {key: "added" | "load" | "unevidenced" | "drift";
  label: string; value: number};

export function judgeCounts(summary?: Record<string, unknown> | null): JudgeCount[] {
  const byLabel = (summary?.by_label || {}) as Record<string, number>;
  return [
    {key: "added", label: "新增行", value: Number(summary?.total_added_lines || 0)},
    {key: "load", label: "承重", value: Number(byLabel["承重"] || 0)},
    {key: "unevidenced", label: "无据", value: Number(byLabel["无据"] || 0)},
    {key: "drift", label: "游离", value: Number(byLabel["游离"] || 0)},
  ];
}

export type ExperimentStage = {key: "baseline" | "remove" | "regression" | "restore";
  label: string; detail: string;
  state: "complete" | "failed" | "active" | "pending"};

type StoryEvent = {kind: string; data?: Record<string, unknown>};
type StoryCertificate = {unit_id?: string; 状态?: string;
  位置?: {file?: string; start?: number; end?: number}};

export function experimentStory(events: StoryEvent[], certificates: StoryCertificate[] = []): ExperimentStage[] {
  const baselineStarted = events.find(event => event.kind === "baseline.started");
  const baseline = events.find(event => event.kind === "baseline.completed"
    || event.kind === "baseline.failed");
  const probeStarted = events.find(event => event.kind === "probe.started");
  const probeResults = events.filter(event => event.kind === "probe.completed"
    || event.kind === "probe.failed");
  const preferredCertificate = certificates.find(cert => cert.状态 === "承重") || certificates[0];
  const preferredAnchor = String(preferredCertificate?.位置?.file || "");
  const probe = probeResults.find(event => String(event.data?.anchor_id || "") === preferredAnchor)
    || probeResults.find(event => Array.isArray(event.data?.regressed_tests)
      && (event.data?.regressed_tests as unknown[]).length > 0)
    || probeResults[0];
  const chosen = probe || probeStarted;
  const anchor = String(chosen?.data?.anchor_id || "候选代码");
  const observation = events.find(event => event.kind === "observation.recorded"
    && (!chosen || String(event.data?.anchor_id || "") === anchor)
    && Array.isArray(event.data?.regressed_tests)
    && (event.data?.regressed_tests as unknown[]).length > 0);
  const regressions = (chosen?.data?.regressed_tests || observation?.data?.regressed_tests || []) as unknown[];
  const restoreStarted = events.find(event => event.kind === "restore.started");
  const restore = events.find(event => event.kind === "restore.verified"
    && (!chosen || event.data?.anchor_id === chosen.data?.anchor_id));
  const declared = Number(baseline?.data?.declared_tests || 0);
  const baselinePassed = baseline?.data?.all_passed === true;
  const restored = restore?.data?.restored_clean === true;
  const certificate = certificates.find(cert => cert.位置?.file === anchor)
    || (preferredCertificate?.位置?.file === anchor ? preferredCertificate : undefined);
  const certificatePosition = certificate?.位置;
  const lineCount = certificatePosition?.start && certificatePosition.end
    ? Math.max(1, certificatePosition.end - certificatePosition.start + 1) : 0;
  const interventionDetail = certificatePosition?.start
    ? `${anchor}:${certificatePosition.start}${certificatePosition.end && certificatePosition.end !== certificatePosition.start
      ? `–${certificatePosition.end}` : ""} · ${lineCount} 行`
    : chosen ? `${anchor}${chosen.data?.units ? ` · ${String(chosen.data.units)} 个实验单元` : ""}` : "";
  const baselineState = baseline
    ? baselinePassed ? "complete" : "failed"
    : baselineStarted ? "active" : "pending";
  const removeState = probe
    ? probe.kind === "probe.failed" ? "failed" : "complete"
    : probeStarted ? "active" : "pending";
  const regressionState = regressions.length
    ? "failed"
    : probe?.kind === "probe.completed" ? "complete" : "pending";
  const restoreState = restore
    ? restored ? "complete" : "failed"
    : restoreStarted ? "active" : "pending";
  return [
    {key: "baseline", label: "基线全绿",
      detail: baseline ? baselinePassed ? `${declared}/${declared} 项测试通过`
        : `${declared} 项声明测试未全通过` : baselineStarted ? "基线测试进行中" : "等待基线测试",
      state: baselineState as ExperimentStage["state"]},
    {key: "remove", label: "临时拿走",
      detail: chosen ? interventionDetail : probeStarted ? "正在实施可恢复干预" : "等待可恢复实验",
      state: removeState as ExperimentStage["state"]},
    {key: "regression", label: "具名测试报警",
      detail: regressions.length ? String(regressions[0])
        : probe?.kind === "probe.completed" ? "未观察到具名测试失败" : "等待测试观测",
      state: regressionState as ExperimentStage["state"]},
    {key: "restore", label: "恢复干净",
      detail: restore ? (restored ? `${declared}/${declared} 项测试通过，工作区已恢复`
        : "恢复校验失败") : restoreStarted ? "恢复校验进行中" : "等待恢复校验",
      state: restoreState as ExperimentStage["state"]},
  ];
}

export type WorkspaceFocus = "current_result" | "next_run_setup";

export type WorkspacePresentation = {
  showCurrentResult: boolean;
  showModelTrack: boolean;
  showModelBanner: boolean;
  showRecommendations: boolean;
  showDeterministicPlaceholder: boolean;
  showLiveSetupPlaceholder: boolean;
  showPreviousResultNotice: boolean;
  configLocked: boolean;
};

const TERMINAL_REVIEW_STATES = new Set(["COMPLETE", "PARTIAL", "FAILED", "ABORTED"]);

/**
 * 工作台视觉只从这一处决定：左侧 provider 是下一次运行的配置，
 * 只有 current_result 才能读取当前 Review 的模型事实。
 */
export function workspacePresentation(
  provider: string,
  reviewStatus?: string | null,
  focus: WorkspaceFocus = "current_result",
): WorkspacePresentation {
  const hasReview = Boolean(reviewStatus);
  const showCurrentResult = hasReview && focus === "current_result";
  const liveSetup = focus === "next_run_setup" && provider === "live";
  const deterministicSetup = focus === "next_run_setup" && provider !== "live";
  const configLocked = hasReview && !TERMINAL_REVIEW_STATES.has(String(reviewStatus));
  const completed = TERMINAL_REVIEW_STATES.has(String(reviewStatus));
  return {
    showCurrentResult,
    showModelTrack: showCurrentResult && provider === "live",
    // Model facts stay inside AI 研究员 mode; judge mode is deliberately
    // conclusion-only and must not leak a model banner from a prior run.
    showModelBanner: false,
    showRecommendations: showCurrentResult && provider === "live" && completed,
    showDeterministicPlaceholder: deterministicSetup,
    showLiveSetupPlaceholder: liveSetup,
    showPreviousResultNotice: Boolean(reviewStatus) && focus === "next_run_setup",
    configLocked,
  };
}

export type EventLane = "模型建议" | "水木验码执行" | "测试作证";

/** 评委时间线的分权归位。未知事件默认落到执行方，保留 kind 供研究员审计。 */
export function laneOf(event: {kind: string; data?: Record<string, unknown>}): EventLane {
  const kind = event.kind;
  const source = String(event.data?.source || "");
  if (kind === "model.action" && source === "deterministic") return "水木验码执行";
  if (kind === "scheduler.deterministic_order") return "水木验码执行";
  if (kind === "scheduler.next" && event.data?.selection !== "model_reprioritized") {
    return "水木验码执行";
  }
  if (kind === "scheduler.next" && event.data?.selection === "model_reprioritized") {
    return "模型建议";
  }
  if (kind.startsWith("model.") || kind.startsWith("recommendation.")
      || kind === "plan.drafted") return "模型建议";
  if (kind.startsWith("test.") || kind.startsWith("pytest.")
      || kind === "observation.recorded") return "测试作证";
  return "水木验码执行";
}

export function laneEvents<T extends {kind: string; data?: Record<string, unknown>}>(
  events: T[],
  live: boolean,
): Array<{lane: EventLane; events: T[]}> {
  const lanes: EventLane[] = live
    ? ["模型建议", "水木验码执行", "测试作证"]
    : ["水木验码执行", "测试作证"];
  return lanes.map(lane => ({lane, events: events.filter(event => {
    const assigned = laneOf(event);
    // 非 AI 研究员视图不展示模型请求、模型动作或建议事件；确定性
    // scheduler/model.action 事件本身仍由 laneOf 归到执行方。
    return assigned === lane;
  })}));
}

export type EventPhase = "准备" | "实验" | "证据" | "恢复";

export function eventPhase(kind: string): EventPhase {
  if (kind.startsWith("restore.") || kind === "review.completed"
      || kind === "review.failed") return "恢复";
  if (kind.startsWith("probe.") || kind.startsWith("tool.")
      || kind.startsWith("baseline.") || kind.startsWith("scheduler.")
      || kind.startsWith("minimization.")
      || kind.startsWith("model.")) return "实验";
  if (kind.startsWith("claim.") || kind.startsWith("observation.")
      || kind.startsWith("run.") || kind.startsWith("narrator.")) return "证据";
  return "准备";
}

export function groupEventPhases<T extends {kind: string}>(events: T[]):
    Array<{phase: EventPhase; events: T[]}> {
  const phases: EventPhase[] = ["准备", "实验", "证据", "恢复"];
  return phases.map(phase => ({phase, events: events.filter(e => eventPhase(e.kind) === phase)}));
}

export function modeLabels(bundle?: Record<string, unknown> | null,
                           offlineReplay = false,
                           liveRequest?: Record<string, unknown> | null) {
  // 运行途中还没有 bundle（它只在 COMPLETE/PARTIAL 后才取回），此时若只看 bundle
  // 就会把一次**正在调用模型**的运行标成「确定性调度」——画面在说谎。
  // 服务端 describe() 会回显 request，运行中用它兜底。
  const request = (bundle?.request || liveRequest || {}) as Record<string, unknown>;
  const report = ((bundle?.evidence_bundle || {}) as Record<string, unknown>).report as
    Record<string, unknown> | undefined;
  const summary = (report?.summary || {}) as Record<string, unknown>;
  const run = offlineReplay || bundle?.execution_mode === "replay" ? "离线回放" : "实时运行";
  const schedulerRaw = String(bundle?.scheduler_mode || "");
  const schedulerMap: Record<string, string> = {
    model: "模型调度", fifo: "FIFO", coverage_first: "覆盖优先", cost_first: "成本优先",
  };
  // 兜底不再断言 FIFO：默认已是覆盖优先，而候选缺摘要时又会如实回落到 FIFO。
  // 模式未标注时说「确定性调度」，不替这次运行声称它用了哪一种。
  const scheduler = schedulerMap[schedulerRaw] || (request.model_provider === "live"
    ? `模型调度 · ${String(request.agent_level || "l1").toUpperCase()}` : "确定性调度");
  const isolationRaw = String(bundle?.isolation_mode || request.execution_mode || "trusted_local");
  const isolationMap: Record<string, string> = {
    sandboxed: "沙箱模式", trusted_local: "受信任模式",
    ci_ephemeral: "CI 临时环境", replay: "回放环境",
  };
  const isolation = isolationMap[isolationRaw]
    || String(summary.execution_mode_label || isolationRaw);
  return {run, scheduler, isolation};
}

export type ModelParticipationState =
  "reordered" | "kept_order" | "partial" | "not_participated";

export type RecommendationItemView = {
  anchor_id: string;
  line_refs: string[];
  evidence_ids: string[];
  action: string;
  rationale: string;
  verification: string;
};

export type RecommendationView = {
  generated: boolean;
  items: RecommendationItemView[];
  failureReason: string;
  failureCode: string;
  skippedReason: string;
  model: string;
  promptVersion: string;
};

export type ModelParticipation = {
  live: boolean;
  state: ModelParticipationState;
  stateLabel: string;
  modelLabel: string;
  agentLevel: string;
  calls: number;
  requestsByStage: Record<string, number>;
  reorderCount: number;
  orderChanges: string[];
  reasons: string[];
  fallbacks: number;
  policyRejections: number;
  recommendationFailed: boolean;
  recommendations: RecommendationView | null;
};

const PARTICIPATION_LABELS: Record<ModelParticipationState, string> = {
  reordered: "已参与并发生重排",
  kept_order: "已参与，保持原顺序",
  partial: "部分参与（调度发生过降级）",
  not_participated: "未参与（确定性调度）",
};

/** 结果页「模型参与记录」的数据来源：provider 元数据、模型指标和调度事件。

    只展示系统实际采用的动作和审计数据，不展示模型内部思维过程；
    deterministic 运行同样如实展示 0 和 —；这些字段只在 AI 研究员透明面板
    使用，评委和普通研究员界面不显示模型过程。 */
export function modelParticipation(
  bundle?: {provider?: Record<string, unknown>;
            model_metrics?: Record<string, unknown>;
            request?: Record<string, unknown>;
            narration?: {recommendations?: Record<string, unknown> | null}} | null,
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  liveRequest?: Record<string, unknown> | null,
): ModelParticipation {
  const provider = bundle?.provider || {};
  const metrics = bundle?.model_metrics || {};
  const request = (bundle?.request || liveRequest || {}) as Record<string, unknown>;
  const kind = String(provider.kind || "");
  const live = kind === "openai-compatible" || request.model_provider === "live";
  const reorders = events.filter(event => event.kind === "scheduler.next"
    && event.data?.selection === "model_reprioritized");
  const orderChanges = reorders.map(event =>
    `${String(event.data?.previous_next_anchor || "")} → ${String(event.data?.actual_next_anchor || "")}`)
    .filter(change => change.trim() !== "→");
  const reasons = [...new Set(reorders.map(event =>
    String(event.data?.priority_reason || "").trim()).filter(Boolean))];
  const eventFallbacks = events.filter(event => event.kind === "model.fallback").length;
  const fallbacks = bundle ? Number(metrics.fallbacks || 0) : eventFallbacks;
  const recommendations = recommendationView(bundle);
  const recommendationFailed = (recommendations?.failureReason || "").length > 0
    || events.some(event => event.kind === "recommendation.failed");
  let state: ModelParticipationState;
  if (!live) state = "not_participated";
  // 建议失败不是"降级"：调度照常由模型完成，只是建议阶段没有产出。
  // 单独用 recommendationFailed 标记，避免出现"部分参与（发生过降级）"
  // 与"降级 0 次"同屏的矛盾。
  else if (fallbacks > 0) state = "partial";
  else if (reorders.length > 0) state = "reordered";
  else state = "kept_order";
  return {
    live,
    state,
    stateLabel: PARTICIPATION_LABELS[state],
    modelLabel: live ? String(provider.model_id || "未标注模型") : "—（确定性调度）",
    agentLevel: String(request.agent_level || "l1").toUpperCase(),
    calls: Number(metrics.requests || 0),
    requestsByStage: {...(metrics.requests_by_stage || {})} as Record<string, number>,
    reorderCount: reorders.length,
    orderChanges,
    reasons,
    fallbacks,
    policyRejections: Number(metrics.policy_rejections || 0),
    recommendationFailed,
    recommendations,
  };
}

/** 从 narration.recommendations 提取只读建议；旧 Bundle 没有该字段时返回 null。 */
export function recommendationView(
  bundle?: {narration?: {recommendations?: Record<string, unknown> | null}} | null,
): RecommendationView | null {
  const raw = bundle?.narration?.recommendations;
  if (!raw) return null;
  const items = Array.isArray(raw.items) ? raw.items : [];
  return {
    generated: raw.generated === true,
    items: items.map(item => ({
      anchor_id: String(item?.anchor_id || ""),
      line_refs: Array.isArray(item?.line_refs) ? item.line_refs.map(String) : [],
      evidence_ids: Array.isArray(item?.evidence_ids) ? item.evidence_ids.map(String) : [],
      action: String(item?.action || ""),
      rationale: String(item?.rationale || ""),
      verification: String(item?.verification || ""),
    })),
    failureReason: String(raw.failure_reason || ""),
    failureCode: String(raw.failure_code || ""),
    skippedReason: String(raw.skipped_reason || ""),
    model: String(raw.model || ""),
    promptVersion: String(raw.prompt_version || ""),
  };
}

export type EvidencePassportStatus = "COMPLETE" | "PARTIAL" | "FAILED" | "ABORTED";
export type EvidencePassportRestoreStatus = "verified" | "failed" | "in_progress" | "not_recorded";

export type EvidencePassportView = {
  status: EvidencePassportStatus;
  statusLabel: string;
  addedLines: number;
  loadLines: number;
  namedFailures: number;
  unevidencedLines: number;
  driftLines: number;
  restoreStatus: EvidencePassportRestoreStatus;
  restoreLabel: string;
  planFingerprint: string;
  bundleAvailable: boolean;
  live: boolean;
  modelLabel: string;
  modelCalls: number;
  recommendationLabel: string;
};

const PASSPORT_STATUS_LABELS: Record<EvidencePassportStatus, string> = {
  COMPLETE: "验讫",
  PARTIAL: "部分完成",
  FAILED: "未签发",
  ABORTED: "已终止",
};

const PASSPORT_RESTORE_LABELS: Record<EvidencePassportRestoreStatus, string> = {
  verified: "已验证",
  failed: "校验失败",
  in_progress: "进行中",
  not_recorded: "未记录",
};

function passportStatus(status?: string | null): EvidencePassportStatus {
  return status === "PARTIAL" || status === "FAILED" || status === "ABORTED"
    ? status : "COMPLETE";
}

function addNamedFailures(value: unknown, ids: Set<string>) {
  if (!Array.isArray(value)) return;
  for (const item of value) {
    if (typeof item === "string" && item.trim()) ids.add(item.trim());
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    for (const key of ["test_id", "nodeid", "test", "name"]) {
      const id = String(row[key] || "").trim();
      if (id) { ids.add(id); break; }
    }
  }
}

function namedFailureCount(
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  ledger: Array<Record<string, unknown>> = [],
): number {
  const ids = new Set<string>();
  for (const event of events) {
    if (event.kind !== "probe.completed" && event.kind !== "probe.failed"
        && event.kind !== "observation.recorded") continue;
    const data = event.data || {};
    addNamedFailures(data.regressed_tests, ids);
    addNamedFailures(data.regressions, ids);
  }
  for (const record of ledger) {
    const payload = (record.payload || {}) as Record<string, unknown>;
    const data = (payload.data || record.data || {}) as Record<string, unknown>;
    addNamedFailures(data.regressed_tests, ids);
    addNamedFailures(data.regressions, ids);
  }
  return ids.size;
}

function restorePassportStatus(
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
): EvidencePassportRestoreStatus {
  if (events.some(event => event.kind === "restore.failed")) return "failed";
  const verified = events.filter(event => event.kind === "restore.verified");
  if (verified.some(event => event.data?.restored_clean === false)) return "failed";
  if (verified.length > 0) return "verified";
  if (events.some(event => event.kind === "restore.started"
      || event.kind === "restore.completed")) return "in_progress";
  return "not_recorded";
}

/** 完成页的最小证据护照：只汇总 Bundle 已发布的数字与事件，不扩张结论。 */
export function evidencePassport(
  status?: string | null,
  summary?: Record<string, unknown> | null,
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  plan?: Record<string, unknown> | null,
  ledger: Array<Record<string, unknown>> = [],
  participation?: Pick<ModelParticipation, "live" | "modelLabel" | "calls"
    | "recommendationFailed" | "recommendations"> | null,
  bundleAvailable = false,
): EvidencePassportView {
  const byLabel = (summary?.by_label || {}) as Record<string, unknown>;
  const live = participation?.live === true;
  const recommendationLabel = !live ? "不适用（确定性运行）"
    : participation?.recommendationFailed ? "生成失败（不影响实验结论）"
    : participation?.recommendations?.generated
      ? `已生成 ${participation.recommendations.items.length} 条` : "未生成";
  const fingerprint = String(plan?.plan_sha256 || plan?.plan_fingerprint
    || plan?.fingerprint || plan?.sha256 || "未记录");
  return {
    status: passportStatus(status),
    statusLabel: PASSPORT_STATUS_LABELS[passportStatus(status)],
    addedLines: Number(summary?.total_added_lines || 0),
    loadLines: Number(byLabel["承重"] || 0),
    namedFailures: namedFailureCount(events, ledger),
    unevidencedLines: Number(byLabel["无据"] || 0),
    driftLines: Number(byLabel["游离"] || 0),
    restoreStatus: restorePassportStatus(events),
    restoreLabel: PASSPORT_RESTORE_LABELS[restorePassportStatus(events)],
    planFingerprint: fingerprint,
    bundleAvailable,
    live,
    modelLabel: live ? String(participation?.modelLabel || "未标注模型") : "",
    modelCalls: live ? Number(participation?.calls || 0) : 0,
    recommendationLabel,
  };
}

/** 完成页首句：只陈述事件先后，不声称模型更快或更准。 */
export function modelConclusion(participation: ModelParticipation): string {
  if (!participation.live) {
    return "本次运行未调用模型，调度由确定性策略完成；实验结论来自水木验码的独立执行。";
  }
  let conclusion: string;
  if (participation.state === "partial") {
    conclusion = "DeepSeek 参与了本次运行的调度，其中发生过降级为确定性策略；"
      + "实验结论仍全部来自水木验码的独立执行。";
  } else if (participation.state === "kept_order") {
    conclusion = "DeepSeek 观察真实实验后选择保持原顺序；"
      + "随后水木验码按顺序独立执行 pytest 并生成证据。";
  } else {
    const change = participation.orderChanges[0] || "";
    const [before, after] = change.split("→").map(part => part.trim());
    if (before && after && before !== after) {
      conclusion = `DeepSeek 根据上一步真实观测和剩余预算，将 ${after} 提到 ${before} 之前；`
        + "随后水木验码独立执行 pytest 并生成证据。";
    } else {
      conclusion = "DeepSeek 根据真实观测调整了检查顺序；"
        + "随后水木验码独立执行 pytest 并生成证据。";
    }
  }
  if (participation.recommendationFailed) {
    conclusion += "本次只读建议生成失败，不影响实验结论。";
  }
  return conclusion;
}

export type DecisionStageState = "pending" | "active" | "done";
export type DecisionStage = {
  key: string;
  label: string;
  state: DecisionStageState;
  note: string;
};

/** 运行中「DeepSeek 决策轨迹」六步状态机：只由真实事件驱动，没有假进度。 */
export function modelDecisionStages(
  events: Array<{kind: string; data?: Record<string, unknown>}>,
  live: boolean,
  terminal = false,
): DecisionStage[] {
  const startedStages = new Set(events
    .filter(event => event.kind === "model.request.started")
    .map(event => String(event.data?.stage || "")));
  const hasScheduling = startedStages.has("scheduling");
  const hasAction = events.some(event => event.kind === "model.action");
  const hasRecommendationStart = startedStages.has("recommendation");
  const hasGenerated = events.some(event => event.kind === "recommendation.generated");
  const hasFailed = events.some(event => event.kind === "recommendation.failed");
  const anyStarted = startedStages.size > 0;
  const noModel = !live;
  return [
    {key: "goal", label: "目标已传入",
     state: noModel ? "pending" : anyStarted ? "done" : "active",
     note: noModel ? "确定性运行不经过模型" : "用户目标原文进入模型上下文"},
    {key: "observation", label: "等待真实实验观测",
     state: noModel ? "pending" : hasScheduling ? "done" : anyStarted ? "active" : "pending",
     note: "先实验，后决策"},
    {key: "deciding", label: "DeepSeek 正在判断下一步",
     state: noModel ? "pending" : hasAction ? "done" : hasScheduling ? "active" : "pending",
     note: "每次调用由真实观测触发"},
    {key: "decided", label: "已保持顺序或已重排",
     state: noModel ? "pending"
       : hasRecommendationStart || (terminal && hasAction) ? "done"
       : hasAction ? "active" : "pending",
     note: "只能在冻结候选内重排"},
    {key: "recommending", label: "正在生成证据建议",
     state: noModel ? "pending"
       : hasGenerated || hasFailed ? "done"
       : hasRecommendationStart ? "active" : "pending",
     note: "只读 · 最多 3 条"},
    {key: "recommended", label: "建议已生成",
     state: noModel ? "pending" : hasGenerated ? "done"
       : hasFailed ? "done" : "pending",
     note: hasFailed ? "生成失败，不影响实验结论"
       : hasGenerated ? "未执行 · 需要人工复核" : ""},
  ];
}


/** 一行有好几条证据时，先给最有说服力的那条。

    `evidence_ids` 的顺序是**推导累积顺序**（先覆盖率、再实验、最后主张），
    直接取第 0 条会让「承重」行打开一份覆盖率观测——那不是它之所以承重的理由。
    读的人想看的是「删掉它，哪个测试会红」，也就是 Claim。
    Claim > Experiment > Fact。 */
export function decisiveEvidenceId(
  line: {evidence_ids?: string[]; unit_id?: string | null},
  bundle?: {evidence_bundle?: {ledger?: Array<Record<string, unknown>>}} | null,
): string {
  const ids = line.evidence_ids || [];
  const ledger = bundle?.evidence_bundle?.ledger || [];
  if (ledger.length) {
    const rank: Record<string, number> = {Claim: 0, Experiment: 1, Fact: 2};
    const best = ids
      .map(id => ({id, row: ledger.find(r => r.record_id === id)}))
      .filter(x => x.row)
      .sort((a, b) => (rank[String(a.row!.record_type)] ?? 9)
                    - (rank[String(b.row!.record_type)] ?? 9))[0];
    if (best) return best.id;
  }
  // v2 旧 Bundle 没有 evidence_ids。unit_id 是分析单元标识，不是 ledger
  // record_id；拿它请求 evidence API 会得到 404，所以宁可明确禁用。
  return ids[0] || "";
}

export type MinimizationView = {
  anchorId: string;
  targetRegression: string;
  frozenUnits: number;
  minimalUnits: number;
  oneMinimal: boolean;
  removalChecks: number;
  scopeNote: string;
  incompleteReason: string;
  /** true 表示 ddmin 结构上就不适用，一次实验都没花。 */
  notApplicable: boolean;
  experiments: number;
};

/** 为什么这个对象没有证书。"没跑"和"跑了没证出来"必须读起来不一样。 */
const MINIMIZATION_REASONS: Record<string, string> = {
  whole_file_candidate: "整份新文件没有更细的可删原子",
  no_added_line: "该对象没有新增行",
  no_deletable_added_statement: "新增行嵌在既有语句内部，构不成可独立删除的原子",
  duplicate_unit_ids: "原子标识冲突",
  overlapping_units: "原子行范围重叠",
  single_atom: "只有一条可删语句，本来就已经最小",
  source_unreadable: "源码读取失败",
  no_named_regression: "没有具名回归可供收窄",
  initial_pass: "移除全部新增语句也没有触发目标回归",
  initial_unresolved: "试验没有给出可用判定",
  not_minimizable: "原子集合不满足最小化前提",
  certificate_incomplete: "最小性检查未全部通过",
  source_changed_since_freeze: "源码与冻结时的原子不再一致",
};

export function minimizationReason(code: string): string {
  return MINIMIZATION_REASONS[code] || code;
}

/** Project ddmin certificates for display. Never invents one.
 *
 * The judge surface shows these read-only: a certificate is something the run
 * either earned or did not. An anchor that could not be minimised is rendered
 * with its reason rather than dropped, so "no certificate" stays visible
 * instead of looking like the pass never happened.
 */
export function minimizationViews(
  report?: Record<string, unknown> | null): MinimizationView[] {
  const raw = (report?.minimizations || []) as Record<string, unknown>[];
  if (!Array.isArray(raw)) return [];
  return raw.map(row => {
    const cert = (row.certificate || {}) as Record<string, unknown>;
    const frozen = (cert.frozen_unit_ids || []) as unknown[];
    const minimal = (cert.minimal_unit_ids || []) as unknown[];
    const checks = (cert.removal_checks || []) as unknown[];
    return {
      anchorId: String(row.anchor_id || ""),
      targetRegression: String(row.target_regression || ""),
      frozenUnits: frozen.length,
      minimalUnits: minimal.length,
      oneMinimal: cert.one_minimal === true,
      removalChecks: checks.length,
      // Carried verbatim: the note is what keeps the claim inside its scope.
      scopeNote: String(cert.scope_note || ""),
      incompleteReason: row.complete === true
        ? "" : minimizationReason(String(row.reason || "未完成")),
      notApplicable: row.applicability === "not_applicable",
      experiments: Number(row.experiments || 0),
    };
  });
}

// --- 能力状态 -------------------------------------------------------------
//
// 界面上的每一个能力徽章都来自服务器的 /api/v1/capabilities，也就是发布链
// 用来校验对外材料的同一份注册表。这样"屏幕上说的"和"证据包允许说的"
// 不可能各说各话。

export type Capability = {
  id: string;
  title: string;
  state: string;
  state_label: string;
  runtime: string;
  summary: string;
  gate: string;
  evidence: string[];
};

const CAPABILITY_ORDER = ["verified", "experimental", "negative_result", "disabled"];

export function capabilityTone(state: string): "ok" | "warning" | "danger" | "idle" {
  if (state === "verified") return "ok";
  if (state === "experimental") return "warning";
  if (state === "negative_result") return "danger";
  return "idle";
}

/** 关闭的能力不可选：状态必须是产品属性，不能只是文档里的一个词。 */
export function capabilityAvailable(capabilities: Capability[], id: string): boolean {
  const found = capabilities.find(item => item.id === id);
  // 注册表还没加载完时不抢先禁用；服务器仍会在收单时再拒一次。
  return found ? found.runtime !== "unavailable" : true;
}

/** 按成熟度分组，已验证在前，未达门槛的排在后面且不会被折叠掉。 */
export function groupCapabilities(capabilities: Capability[]):
    Array<{state: string; label: string; items: Capability[]}> {
  return CAPABILITY_ORDER
    .map(state => {
      const items = capabilities.filter(item => item.state === state);
      return {state, label: items[0]?.state_label || state, items};
    })
    .filter(group => group.items.length > 0);
}

/** 结论句里的数字要能被一眼抓住。

    完成面板那句话是 70 多字排成三行宋体大字，`6 行获得测试承重证据` 里的
    `6` 和周围的字一样重，评委得逐字读完才知道结果。这里把数字段切出来，
    交给渲染层单独加重——只做视觉提取，不改写、不重排、不省略任何一个字。 */
export function emphasizeNumbers(text: string): Array<{text: string; number: boolean}> {
  if (!text) return [];
  const out: Array<{text: string; number: boolean}> = [];
  // 只提数字本身。把量词（"行"）一并拉进等宽字体，会让它和周围的宋体正文
  // 脱开一道缝，反而更难读——量词留在正文字体里。
  const pattern = /\d+(?:\.\d+)?/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) out.push({text: text.slice(last, at), number: false});
    out.push({text: match[0], number: true});
    last = at + match[0].length;
  }
  if (last < text.length) out.push({text: text.slice(last), number: false});
  return out;
}
