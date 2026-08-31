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
  "scheduler.next": "继续下一个检查对象",
  "run.synthesizing": "正在汇总结论",
  "run.completed": "证据产物已发布",
  "narrator.compiled": "结论说明已编译",
  "executor.bound": "执行器已绑定",
  "review.completed": "审查完成",
  "review.failed": "审查失败",
};

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] || kind.replaceAll(".", " · ");
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
  label: string; detail: string; state: "complete" | "failed" | "pending"};

type StoryEvent = {kind: string; data?: Record<string, unknown>};

export function experimentStory(events: StoryEvent[]): ExperimentStage[] {
  const baseline = events.find(event => event.kind === "baseline.completed");
  const probe = events.find(event => event.kind === "probe.completed"
    && Array.isArray(event.data?.regressed_tests)
    && (event.data?.regressed_tests as unknown[]).length > 0);
  const fallbackProbe = events.find(event => event.kind === "probe.completed");
  const chosen = probe || fallbackProbe;
  const anchor = String(chosen?.data?.anchor_id || "候选代码");
  const regressions = (chosen?.data?.regressed_tests || []) as unknown[];
  const restore = events.find(event => event.kind === "restore.verified"
    && (!chosen || event.data?.anchor_id === chosen.data?.anchor_id));
  const declared = Number(baseline?.data?.declared_tests || 0);
  const baselinePassed = baseline?.data?.all_passed === true;
  const restored = restore?.data?.restored_clean === true;
  return [
    {key: "baseline", label: "基线通过",
      detail: baseline ? `${declared}/${declared} 项测试通过` : "等待基线测试",
      state: baselinePassed ? "complete" : baseline ? "failed" : "pending"},
    {key: "remove", label: "临时移除",
      detail: chosen ? anchor : "等待可恢复实验",
      state: chosen ? "complete" : "pending"},
    {key: "regression", label: "具名失败",
      detail: regressions.length ? String(regressions[0])
        : chosen ? "未观察到具名失败" : "等待测试观测",
      state: regressions.length ? "failed" : "pending"},
    {key: "restore", label: "恢复验证",
      detail: restore ? (restored ? `${declared}/${declared} 项测试通过，工作区已恢复`
        : "恢复校验失败") : "等待恢复校验",
      state: restored ? "complete" : restore ? "failed" : "pending"},
  ];
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
                           offlineReplay = false) {
  const request = (bundle?.request || {}) as Record<string, unknown>;
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
  source_changed_since_freeze: "源码与计划时的原子不再一致",
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
