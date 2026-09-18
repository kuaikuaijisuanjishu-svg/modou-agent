import { Component, StrictMode, Suspense, lazy, useEffect, useLayoutEffect, useMemo,
  useRef, useState, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { RepoCombobox } from "./RepoCombobox";
import {TaskClosure, TaskRequirementsCard, taskRequirements, taskLink, type EvidenceTask} from "./TaskClosure";
import {OfficialLockup, OfficialMark} from "./OfficialBrand";
import {
  COMMON_UNCOVERED, SESSION_REVIEW_KEY, SESSION_TOKEN_KEY, classifyError, eventLabel,
  eventPresentation,
  linePresentation, normalizePastedToken, reasonLabel, resolveStartupToken,
  schedulingDetail, statusView, summarySentence, decisiveEvidenceId, emphasizeNumbers,
  experimentStory, verdictCounts, minimizationViews, laneEvents, workspacePresentation,
  modeLabels, capabilityAvailable, capabilityTone, groupCapabilities, modelParticipation,
  modelConclusion, modelDecisionStages, evidencePassport, repairStatusView, autonomyView,
  modelLabel, offlineReplayNotice,
  draftCardRows, admissibilityContrast, admissibilityView, visaStatusView,
  locateEvidenceLabel, locateFallbackNotice,
  releaseGateLabel, releaseStatusView, eventDeltas, shortHash,
  certificateGradeRows, uiProbeStatusView, narrowPackageChecksView,
  unenforcedLimitsView,
  standingAuthorizationView, normalizeEvalReceiptIndex, normalizeEvalReceipt,
  type StandingAuthorizationView, type EvalReceiptIndexEntry, type EvalReceiptView,
  memoryRuleViews, MEMORY_CARD_HEADLINE, MEMORY_STATUS_LABELS, memoryKindOptions,
  dispositionOptions, DISPOSITION_BOUNDARY,
  planRevisionViews, PLAN_REVISION_BOUNDARY, type PlanRevisionView,
  type Capability, type DraftPayload, type UiNotice, type WorkspaceFocus,
  editSessionStateView, editSessionTransitionText, editSessionExitReasonText,
  candidateVerdictView, candidateTestResultView, deliveryRejectionText,
  DELIVERY_REJECTION_CODES, DELIVERY_FINGERPRINT_VIEWS, DELIVERY_DISCLAIMER,
  type EditSessionLedger,
  type RepairCandidateRecord,
  type MemoryRuleView,
} from "./presentation";
import {
  EMPTY_FILTER, certificateForPoint, fileOptions, filterConclusions,
  filterCountLabel, GRADE_FILTER_OPTIONS, isFiltering, LABEL_FILTER_OPTIONS,
  passportMarkdown, stepIndex, type ConclusionFilter,
} from "./conclusion-tools";
import {testsForEvidence, type FocusTarget, type SourceFile, type SourceTree}
  from "./workbench/model";
import type {CommentList, CommentRecord} from "./workbench/comments";
import type {ReverificationList, ReverificationRecord}
  from "./workbench/reverification";

// 证据工作台整块懒加载：不点"打开代码工作台"，Monaco 主包、语言与
// worker 都不会进浏览器。
const CodeWorkbench = lazy(() => import("./workbench/CodeWorkbench"));

type Repo = { repo_id: string; display_name: string; technical_name?: string };
type LocateMatch = {repo_id: string; path: string; evidence_kind: string;
  evidence_detail: string; score: number};
type LocateDraft = {repo_id: string; repo_display_name: string; instruction: string;
  review_focus: string; budget_seconds: number; paths: string[]; language: string};
type LocatePayload = {schema_version: string; searched_repos: string[];
  tokens: string[]; status: string; matches: LocateMatch[];
  draft?: LocateDraft; note?: string; reason?: string};
type Preset = {preset_id: string; display_name: string; description: string;
  repo_id: string; test_files: string[]; goal: string; budget_seconds: number;
  model_provider: string};
type EventRecord = {
  schema_version: string; review_id: string; event_id: string; seq: number;
  kind: string; occurred_at: string; data: Record<string, unknown>;
};
// 后端 human-disposition-v1 的原样投影；字段名照抄 control.record_disposition。
type Disposition = {
  schema_version: string; review_id: string; event_id: string; event_kind: string;
  answer: string; handled_by: string; note?: string; recorded_at: string;
};
type Review = {
  review_id: string;
  state: { status: string; evidence_status?: string | null; reason?: string };
  plan?: Record<string, unknown> | null;
  repair?: RepairStatus;
  // 服务端 describe() 回显本次请求；运行途中还没有 bundle，运行模式标识靠它。
  request?: Record<string, unknown> | null;
  last_seq: number;
};
type RepairStatus = {
  status: string;
  evidence_manifest_sha256?: string;
  evidence_manifest?: Record<string, unknown> | null;
  delivery_record?: Record<string, unknown> | null;
};
type DiffLine = { file: string; line: number; text: string; label: string;
  reason?: string | null; unit_id?: string | null; evidence_ids?: string[] };
type TestProposalAttempt = {
  round: number; stage: string; path: string; code_sha256?: string;
  returncode?: number; failure?: Record<string, unknown>; visa_status?: string;
};
type TestProposalRecord = {
  schema_version: string; status: string; review_id?: string;
  path?: string; code_sha256?: string; finding_ids?: string[]; summary?: string;
  attempts?: TestProposalAttempt[]; revisions?: number;
  visa?: Record<string, unknown> | null; effective?: boolean;
  question?: string; generated_at?: string;
};
type TestProposalStatus = {status: string; proposal?: TestProposalRecord | null};
type Certificate = {
  unit_id?: string; 状态?: string; 主张?: string; 依据?: string[]; 方法?: string;
  口径?: string; 未覆盖?: string[]; 位置?: {file?: string; start?: number; end?: number};
  可采性?: string | null; 可采性说明?: string | null;
  // certificate.py to_dict 的 per-test 定级与运行事实；旧包缺字段时按
  // undefined 处理，界面各自回退，不硬造"未提供"以外的说法。
  逐条定级?: Array<{test_id?: string; before?: unknown; after?: unknown;
    等级?: string}>;
  回滚干净?: boolean | null; 耗时秒?: number;
};
type Report = {
  summary?: Record<string, unknown>; certificates?: Certificate[]; 口径声明?: string[];
  render_model?: {lines?: DiffLine[]};
};
type ReviewBundle = {
  review_id: string; request?: Record<string, unknown>; plan?: Record<string, unknown>;
  events?: EventRecord[]; provider?: Record<string, unknown>;
  repair?: RepairStatus;
  model_metrics?: Record<string, unknown>; narration?: {
    scope_note?: string; blocks?: Array<Record<string, unknown>>;
  };
  evidence_bundle?: {report?: Report; ledger?: Array<Record<string, unknown>>};
  evaluation_context?: {resource_policy?: Record<string, unknown>};
  execution_mode?: string; scheduler_mode?: string; isolation_mode?: string;
  // 证据包自带的产品模式声明（review-bundle-v2 起）。旧包没有这个字段，
  // 界面回落到 provider 推导，但绝不用"用户此刻选的模式"来替代它。
  product_mode?: string;
};
// 两模式产品词汇：standard=零模型标准审查，agent=受限模型参与。
// 新建审查统一走 v2 规格入口，v1 只保留为只读兼容层。
export type ProductMode = "standard" | "agent";
// 面板序号按**这一屏实际渲染出来的面板**生成，不写死。写死的话标准审查
// 缺了决策轨迹那一格，屏幕上就是 01 02 04 05——评委读到的是"有个模块没做
// 出来"，而不是"这个模式没有它"。同理，还没跑过时结果和逐行视图不存在，
// 它们的号也不该先占着。能力状态不是流程里的一步，不编号。
function stepNumbering(keys: readonly (string | false | null)[]): (key: string) => string {
  const shown = keys.filter((key): key is string => typeof key === "string");
  return key => {
    const index = shown.indexOf(key);
    return index < 0 ? "" : String(index + 1).padStart(2, "0");
  };
}
type ModelCall = {call: number; stage: string; messages: Array<{role: string; content: unknown}>;
  raw_response: string; http_status: number; latency_ms?: number; error_type?: string;
  request_sha256?: string; response_sha256?: string};
type ReleaseStatus = {
  schema_version: string; machine_status: "MACHINE_CHECKS_PASSED" | "MACHINE_CHECKS_FAILED" | "UNLOADED";
  release_status: "INTERNAL_ONLY"; stable_eligible: false;
  external_gates_pending: string[];
  pending_gates?: string[] | Record<string, unknown>;
  ui_probe_status?: string;
  narrow_package_checks?: Record<string, unknown>;
  pending_gate_details?: Record<
    string, {status?: string; reason?: string; resume_condition?: string}>;
  machine_failure_reasons?: string[]; evidence?: Record<string, unknown>;
  generated_at?: string; source_commit: string;
  receipt_expired?: boolean; serving_commit?: string;
};

const EXTERNAL_GATES_FALLBACK = [
  "independent_hidden_qa", "independent_linux_host",
  "authorized_private_repo_pilot", "real_developer_study",
  "independent_security_release_signoff",
];

function normalizeReleaseStatus(raw: Partial<ReleaseStatus>): ReleaseStatus {
  // A stale/absent endpoint must fail closed without taking down the rest of
  // the workbench.  Never infer a passing or stable state from a partial
  // response (this also keeps older v1-compatible fixtures renderable).
  const machine = raw.machine_status === "MACHINE_CHECKS_PASSED"
    ? raw.machine_status : raw.machine_status === "MACHINE_CHECKS_FAILED"
      ? raw.machine_status : "UNLOADED";
  // v1 协议里 pending_gates 是「具名门禁 → 状态详情」的映射（如
  // public_repo_matrix），待完成清单的权威数组是 external_gates_pending。
  // 只有当 pending_gates 本身就是数组时才直接采用，否则不得让映射
  // 挤掉合法的 external_gates_pending——那会把没待完成的门禁也说成
  // 待完成。两个字段都缺失时才保守回退到全部门禁。
  const pendingSource = Array.isArray(raw.pending_gates)
    ? raw.pending_gates
    : Array.isArray(raw.external_gates_pending)
      ? raw.external_gates_pending : [];
  const pending = Array.isArray(pendingSource)
    ? pendingSource.filter(item => typeof item === "string")
    : [];
  // 后端的 pending_gates 是「具名门禁 → 状态详情」映射（release_status.py
  // 强制每个条目带 status/reason/resume_condition）。详情独立透传，
  // 不再像之前那样被压成扁平数组后整块丢弃。
  const namedPending = raw.pending_gates && !Array.isArray(raw.pending_gates)
    && typeof raw.pending_gates === "object"
    ? raw.pending_gates as Record<string, Record<string, unknown>> : {};
  const pendingGateDetails: NonNullable<ReleaseStatus["pending_gate_details"]> = {};
  for (const [gate, detail] of Object.entries(namedPending)) {
    const item = detail && typeof detail === "object" ? detail : {};
    pendingGateDetails[gate] = {
      status: item.status === undefined ? undefined : String(item.status),
      reason: item.reason === undefined ? undefined : String(item.reason),
      resume_condition: item.resume_condition === undefined
        ? undefined : String(item.resume_condition),
    };
  }
  return {
    schema_version: String(raw.schema_version || "v02-release-status-v1"),
    machine_status: machine,
    release_status: "INTERNAL_ONLY",
    stable_eligible: false,
    external_gates_pending: pending.length ? pending : EXTERNAL_GATES_FALLBACK,
    pending_gates: pending.length ? pending : EXTERNAL_GATES_FALLBACK,
    machine_failure_reasons: Array.isArray(raw.machine_failure_reasons)
      ? raw.machine_failure_reasons.filter(item => typeof item === "string") : [],
    evidence: raw.evidence && typeof raw.evidence === "object"
      ? raw.evidence as Record<string, unknown> : {},
    generated_at: String(raw.generated_at || ""),
    source_commit: String(raw.source_commit || ""),
    receipt_expired: raw.receipt_expired === true,
    serving_commit: String(raw.serving_commit || ""),
    ui_probe_status: raw.ui_probe_status === undefined
      ? undefined : String(raw.ui_probe_status),
    narrow_package_checks: raw.narrow_package_checks
      && typeof raw.narrow_package_checks === "object"
      ? raw.narrow_package_checks as Record<string, unknown> : undefined,
    pending_gate_details: Object.keys(pendingGateDetails).length
      ? pendingGateDetails : undefined,
  };
}

type WorkbenchSnapshot = {
  review: Review | null;
  reviewBundle: ReviewBundle | null;
  events: EventRecord[];
  selected: EventRecord | null;
  selectedLine: DiffLine | null;
  evidenceDetail: Record<string, unknown> | null;
  replayEvidence: Record<string, Record<string, unknown>>;
  diffLines: DiffLine[];
  error: UiNotice | null;
  offlineReplay: boolean;
  workspaceFocus: WorkspaceFocus;
  productMode: ProductMode;
  presetId: string;
  repoId: string;
  tests: string;
  goal: string;
  goalPreset: string;
  budget: number;
  provider: string;
  probeStrategy: string;
  allowRepairBranch: boolean;
  repairPatch: string;
  lastEvent: string;
  resultScrollTop: number;
  activeStage: number;
  autoFollow: boolean;
  timelineExpanded: boolean;
};

// #44 教程路由是 hash 的第二个、也是唯一可分享的用途：#/tutorial。
// token hash 加载后照旧剥除（安全凭据不留在地址栏）；教程 hash 不剥，
// 刷新与分享都靠它留在地址栏里，而它本身不含任何凭据。
const TUTORIAL_ROUTE_HASH = "#/tutorial";
const isTutorialRoute = (hash: string) => hash === TUTORIAL_ROUTE_HASH;
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const initialTaskId = new URLSearchParams(location.hash.slice(1)).get("task") || "";
let sessionToken = resolveStartupToken(location.hash, sessionStorage.getItem(SESSION_TOKEN_KEY));
if (fragmentToken) sessionStorage.setItem(SESSION_TOKEN_KEY, fragmentToken);
if (location.hash && !isTutorialRoute(location.hash))
  history.replaceState(null, "", location.pathname + location.search + (initialTaskId ? taskLink(initialTaskId) : ""));

// 结构化审查重点：只影响确定性的检查顺序，不能指挥三态结论。
// 默认审查目标：01 区预填、切回「证据边界」预设、以及该预设自己的 goal 必须
// 是同一句话，所以只留一个来源。
const DEFAULT_REVIEW_GOAL = "综合检查这次补丁中新增代码的证据边界";

const GOAL_PRESETS = [
  {id: "evidence-boundary", label: "综合检查新增代码的证据边界",
   goal: DEFAULT_REVIEW_GOAL},
  {id: "named-regression", label: "优先寻找会触发具名测试失败的代码",
   goal: "优先寻找并验证会触发具名测试失败的新增代码"},
  {id: "coverage-gap", label: "优先检查测试覆盖缺口",
   goal: "优先检查新增代码中未被声明测试执行的覆盖缺口"},
  {id: "call-path", label: "检查新增文件是否已经接入调用路径",
   goal: "检查新增文件是否已经接入应用的调用路径与测试收集范围"},
  {id: "budget-first", label: "在有限预算内优先检查低成本、高证据概率对象",
   goal: "在冻结候选和有限预算内，优先检查预计成本更低且更可能产生具名回归证据的对象"},
] as const;

// The six bundled repositories are teaching fixtures, not interchangeable
// benchmarks. This note prevents a goal selector from implying that every
// fixture can produce every kind of evidence.
const DEMO_GOAL_FIT: Record<string, readonly string[]> = {
  retry_demo: ["evidence-boundary", "named-regression"],
  uncovered_demo: ["evidence-boundary", "coverage-gap"],
  orphan_demo: ["evidence-boundary", "call-path"],
  tri_state_demo: ["evidence-boundary", "named-regression", "coverage-gap", "call-path", "budget-first"],
  dependency_demo: ["evidence-boundary", "named-regression", "call-path", "budget-first"],
  scheduler_demo: ["evidence-boundary", "named-regression", "coverage-gap", "call-path", "budget-first"],
};

// #46 引导式入口的固定问题。问题集封闭、顺序固定，答案直接填进下方
// 表单字段；最后仍由人按「生成审查计划」和「确认计划并开始审查」。
// 红线：不做自由对话框——已提交文案的原句是「一个会做实验的智能体，
// 而不是一个会聊天的助手」，聊天台正好拆自己的台。
const GUIDE_STEPS = [
  {key: "what", short: "审什么"},
  {key: "where", short: "文件在哪"},
  {key: "goal", short: "目标是什么"},
  {key: "budget", short: "预算多少"},
] as const;

const BUDGET_CHOICES: Array<[number, string]> = [
  [300, "5 分钟"], [600, "10 分钟"], [1800, "30 分钟"],
];

// 旧响应可能只有 display_name（历史上就是英文目录名）；technical_name
// 到位后以它为准，保证演示目标适配不被中文名打断。
function repoSlug(repo?: Repo): string {
  return repo?.technical_name || repo?.display_name || "";
}

function focusForGoal(goalPreset: string): string {
  return GOAL_PRESETS.some(option => option.id === goalPreset)
    ? goalPreset : "evidence-boundary";
}

function focusForGoalText(goal: string, fallback: string): string {
  return focusForGoal(GOAL_PRESETS.find(option => option.goal === goal)?.id || fallback);
}

class HttpError extends Error {
  constructor(public status: number | undefined, public code: string | undefined,
              message: string) { super(message); }
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${sessionToken}`);
  if (init.body) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(path, {...init, headers, cache: "no-store"});
  } catch (error) {
    throw new HttpError(undefined, "NETWORK_ERROR",
      error instanceof Error ? error.message : "无法连接本地服务");
  }
  const raw = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new HttpError(response.status, raw.code,
      raw.message || raw.code || `HTTP ${response.status}`);
  }
  return raw as T;
}

function requestKey(prefix: string): string {
  const id = globalThis.crypto?.randomUUID?.()
    || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${id}`;
}

function noticeFor(error: unknown): UiNotice {
  if (error instanceof HttpError) return classifyError(error.status, error.code, error.message);
  return classifyError(undefined, undefined,
    error instanceof Error ? error.message : String(error));
}

function configurationArray<T>(payload: unknown, key: string): T[] {
  const value = payload && typeof payload === "object"
    ? (payload as Record<string, unknown>)[key] : undefined;
  if (!Array.isArray(value)) {
    throw new Error(`配置接口返回异常：${key} 必须是数组。请重新加载配置。`);
  }
  return value as T[];
}

function focusExplanation(line: DiffLine): string {
  if (line.reason === "collateral_breakage" || line.reason === "not_isolated") {
    return "测试没有真正开始运行，因此全红也不能证明代码受保护。";
  }
  if (line.label === "承重") {
    return "临时拿走这行所在的新增单元后，真实运行的具名测试报警；恢复代码后测试再次通过。";
  }
  if (line.label === "无据") {
    return "声明测试能够运行，但没有因这行被拿走而出现具名回归，所以当前证据不能证明它受保护。";
  }
  if (line.label === "游离") {
    return "这行所在文件没有进入测试收集或调用路径，现有声明测试看不到它。";
  }
  return reasonLabel(line.reason)
    ? `${reasonLabel(line.reason)}，因此系统没有替这行签发三态结论。`
    : "现有证据不足，系统没有替这行签发三态结论。";
}

const EXPERIMENT_EXPLANATIONS: Record<string, string> = {
  baseline: "原测试全部通过，先确认基线可信。",
  removed: "临时拿走代码，只在隔离副本里做实验。",
  observed: "真实运行的具名测试报警，记录因果变化。",
  restored: "恢复后再次通过，确认工作区没有被实验污染。",
};

class AppErrorBoundary extends Component<{children: ReactNode}, {error: Error | null}> {
  state: {error: Error | null} = {error: null};
  static getDerivedStateFromError(error: Error) { return {error}; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("水木验码界面异常", error, info.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return <main className="fatal-error-page">
      <section className="notice notice-danger" role="alert">
        <div><h1>界面没有正常完成加载</h1>
          <p>数据仍然保留在本地服务中。请重新加载页面；若问题持续，可先打开教程确认连接方式。</p>
          <details><summary>技术详情</summary><code>{this.state.error.message}</code></details>
        </div>
        <div className="fatal-error-actions">
          <button type="button" onClick={() => location.reload()}>重新加载配置</button>
          <button type="button" onClick={() => { location.hash = TUTORIAL_ROUTE_HASH; location.reload(); }}>打开教程</button>
        </div>
      </section>
    </main>;
  }
}

// locate 候选路径里只有真正的 pytest 文件能当 test_files 预填；判定口径
// 与后端 discover_python_test_files 一致（test_*.py / *_test.py）。
function isTestishPath(path: string): boolean {
  const base = path.split("/").pop() || "";
  return base.startsWith("test_") || base.endsWith("_test.py");
}

// 贯穿入口→计划→结果页的常驻边界声明：这句话是「为什么不全自动」的
// 屏幕版答案，三个屏幕各出现一次，用户走到哪都能读到同一条纪律。
// 标准审查一次模型都不调，"模型只提议"在那里是句说不通的话——它反而让人
// 以为有模型参与。两个模式各说各的那半句，后半句（结论由谁签发）不变。
const BOUNDARY_LINE_AGENT = "模型只提议 · 结论由确定性实验签发";
const BOUNDARY_LINE_STANDARD = "不调用模型 · 结论由确定性实验签发";
// 同一种事件反复出现时，屏幕上重复第二遍完整字段没有信息量，只有噪声。
// 首次给全量，之后只给变化项，没有变化就整行不出现——完整原始事件永远
// 在抽屉里一点就有，所以这里省掉的是重复，不是证据。
function EventDeltaLine({delta}: {delta?: {fields: string[]; first: boolean}}) {
  if (!delta || delta.fields.length === 0) return null;
  return <small className={`event-delta ${delta.first ? "is-first" : "is-change"}`}>
    {delta.first ? "" : "变化 · "}{delta.fields.join(" · ")}</small>;
}

function BoundaryLine({agent}: {agent: boolean}) {
  return <p className="boundary-line">
    {agent ? BOUNDARY_LINE_AGENT : BOUNDARY_LINE_STANDARD}</p>;
}

function BrandLockup({context}: {context?: string}) {
  return <OfficialLockup context={context} />;
}

// #43 术语表：每个词条一句话，点击展开。不用 tooltip——展台有触屏，
// title 属性在触屏上不可见。词条名必须用主界面同一套词（第二波定名）：
// 三态短名、可采性四级标签、复验、计划指纹、保留集、隔离工作树。
const TERM_NOTES: Array<{term: string; note: string}> = [
  {term: "承重", note: "删掉这行，至少一条具名测试立刻失败；实验后代码已恢复。"},
  {term: "无据", note: "当前声明的测试范围内，没有测试真正执行到这行。"},
  {term: "游离", note: "整个文件不被任何声明测试收集或引用。"},
  {term: "未标注", note: "本轮未能形成三态结论，原因会逐行标注。"},
  {term: "连带损坏", note: "测试没有真正执行（导入或收集失败）就报失败；这种失败不算承重证据。"},
  {term: "可采性", note: "证据强度分四级：A 级·行为证据、B 级·间接证据、C 级·连带损坏、D 级·环境变动；只有 A、B 级能支撑承重。"},
  {term: "签证", note: "补测在保留集上复现失败后换得的有效性凭证。"},
  {term: "复验", note: "有人质疑结论后重新做实验核对，确认原结论仍然成立。"},
  {term: "计划指纹", note: "本次审查计划内容的 SHA-256 摘要，用来核对执行有没有偏离已确认的计划。"},
  {term: "保留集", note: "预先冻结、模型永远看不到的测试集。"},
  {term: "隔离工作树", note: "一次性建立的临时工作副本；候选代码只在里面运行，不进你的 Git 历史。"},
  {term: "惰性扣下", note: "删了也没测试失败，未过护栏不呈现。"},
  {term: "三态", note: "每行只有承重、无据、游离三种正式结论。"},
];

// #44 独立教程页：不用截图（截图会过期，PPT 已经吃过这个亏），全部用
// 真实组件与纯文字描述；功能名与主界面同一套词（第二波定名），词条
// 解释直接复用上面的 TERM_NOTES，主界面改词这里自动跟着改。
function TutorialPage({onBack}: {onBack: () => void}) {
  const termNote = (term: string): string =>
    TERM_NOTES.find(item => item.term === term)?.note || "";
  return <main className="tutorial-page">
    <header className="tutorial-header">
      <BrandLockup context="· 新手教程" />
      <button type="button" className="tutorial-back" onClick={onBack}>返回控制台</button>
    </header>
    <div className="tutorial-body">
      <section>
        <h2>这是一个什么工具</h2>
        <p>水木验码是一个不会自行签字的证据审查智能体。<strong>AI 负责提议，实验负责签字，人负责批准边界。</strong></p>
        <p>承重结论都能回到具名测试、恢复校验与封存证据。</p>
        <div className="experiment-loop" aria-label="可逆实验步骤">
          <span>拿走代码</span><i>→</i><span>测试报警</span><i>→</i><span>恢复代码</span>
        </div>
        <p>审查一次改动时，它把代码拿走、让测试报警、再把代码恢复——每一步都可逆。</p>
        <p>它不会自动删代码：临时移除、观察具名测试、随后恢复。</p>
      </section>
      <section>
        <h2>三步开始第一次审查</h2>
        <ol className="tutorial-steps">
          <li><strong>给出审查对象：</strong>
            <ul>
              <li>右侧面板里选一个演示案例直接运行；</li>
              <li>或按引导问题依次填写——审什么 → 文件在哪 → 目标是什么 → 预算多少。</li>
            </ul>
          </li>
          <li><strong>按「生成审查计划」：</strong>系统冻结候选、声明测试范围和预算，形成一份带计划指纹的计划。</li>
          <li><strong>按「确认计划并开始审查」：</strong>这一步永远是人工的。没有这一步，不会执行任何实验。</li>
        </ol>
      </section>
      <section>
        <h2>怎么读结论</h2>
        <p>每一行代码只有三种正式结论（三态）：</p>
        <dl className="tutorial-terms">
          {["承重", "无据", "游离", "未标注"].map(term =>
            <div key={term}><dt>{term}</dt><dd>{termNote(term)}</dd></div>)}
        </dl>
        <p>证据强度叫「可采性」，分四级：只有 A 级（行为证据）和 B 级（间接证据）能支撑承重。</p>
        <p>C 级是连带损坏，D 级是环境变动，都不能。</p>
      </section>
      <section>
        <h2>实验的安全边界</h2>
        <dl className="tutorial-terms">
          {["隔离工作树", "保留集", "计划指纹", "签证"].map(term =>
            <div key={term}><dt>{term}</dt><dd>{termNote(term)}</dd></div>)}
        </dl>
        <p>智能体模式里，模型只调整检查顺序并生成只读建议；三态结论仍由确定性实验签发，模型写不了结论。</p>
        <p>标准审查则全程零模型调用。</p>
      </section>
      <section>
        <h2>常用操作</h2>
        <ul className="tutorial-list">
          <li><strong>审查历史</strong>（右上角）：只列出本标签页里出现过的审查；点开只读，不会重跑代码或调用模型。</li>
          <li><strong>清空工作台</strong>：只清空当前页面，审查记录和证据包都保留，30 秒内可撤销。</li>
          <li><strong>复验</strong>：有人质疑结论后重新做实验核对，确认原结论仍然成立；工作台里能看到两次实验的差异。</li>
        </ul>
      </section>
      <section>
        <h2>术语表</h2>
        <dl className="tutorial-terms">
          {TERM_NOTES.map(item => <div key={item.term}><dt>{item.term}</dt><dd>{item.note}</dd></div>)}
        </dl>
      </section>
    </div>
  </main>;
}


// 与页头、结果签名和工作台共用锁定版品牌资产；尺寸由上下文样式控制。
function BrandMark({size, className}: {size: number; className?: string}) {
  return <OfficialMark className={`${className || ""} mark-size-${size}`} />;
}

// 计划修订的多轮气泡。
//
// 画成对话气泡是因为它**就是**一段对话：你说一句要改什么，产品回一份新计划。
// 但有一条语义不能被这个隐喻盖掉——修订不是在原计划上打补丁，而是作废上一稿、
// 另出一份计划和新指纹。所以每个气泡都带自己的指纹，边界声明常驻在下面。
function PlanRevisionThread({views, open, onOpen, instruction, onInstruction,
                             reason, onReason, busy, revisable, onSubmit}: {
  views: PlanRevisionView[]; open: boolean; onOpen: (v: boolean) => void;
  instruction: string; onInstruction: (v: string) => void;
  reason: string; onReason: (v: string) => void;
  busy: boolean; revisable: boolean; onSubmit: () => void;
}) {
  if (!revisable && !views.length) return null;
  return <div className="revision-thread" aria-label="计划修订">
    {views.length > 0 && <ol className="revision-bubbles">
      {views.map(view => <li key={view.review_id}
        className={view.current ? "current" : "superseded"}>
        <b>第 {view.round} 稿{view.current ? "（当前）" : "（已作废）"}</b>
        {view.reason ? <span>{view.reason}</span> : <span className="revision-origin">最初的计划</span>}
        {view.fingerprint && <code>{view.fingerprint}…</code>}
      </li>)}
    </ol>}
    {revisable && (open ? <div className="revision-form">
      <label htmlFor="revise-reason">要改什么</label>
      <input id="revise-reason" value={reason} maxLength={200}
        placeholder="例：范围太宽了，只看调度器那一块"
        onChange={e => onReason(e.target.value)} />
      <label htmlFor="revise-instruction">新的审查说明（可选）</label>
      <input id="revise-instruction" value={instruction} maxLength={500}
        placeholder="留空则只记录修订理由，其余沿用当前这一稿"
        onChange={e => onInstruction(e.target.value)} />
      <div className="revision-actions">
        <button type="button" className="revision-submit"
          disabled={busy || !reason.trim()} onClick={onSubmit}>
          {busy ? "重出一稿中…" : "按这个意见重出一稿"}</button>
        <button type="button" className="revision-cancel"
          disabled={busy} onClick={() => onOpen(false)}>算了</button>
      </div>
    </div> : <button type="button" className="revision-open"
      onClick={() => onOpen(true)}>这份计划要改一下</button>)}
    <small className="revision-boundary">{PLAN_REVISION_BOUNDARY}</small>
  </div>;
}

// 人工处置面板：needs_human 这类事件是产品在提问，这里是人回答它的地方。
// 在这之前，界面上根本没有回答的入口——事件只能被点开看一眼，对话回路是断的。
//
// 两条纪律写死在这里：
//   - 答案是**封闭词表**（与后端 _DISPOSITION_ANSWERS 同源），不给自由输入；
//   - 留痕是审计通道，不改变审查状态。这句话常驻在表单上，否则人会以为点完
//     它就会接着跑。
function DispositionPanel({event, recorded, answer, onAnswer, handler, onHandler,
                           note, onNote, busy, canSubmit, onSubmit}: {
  event: EventRecord; recorded: Disposition[];
  answer: string; onAnswer: (v: string) => void;
  handler: string; onHandler: (v: string) => void;
  note: string; onNote: (v: string) => void;
  busy: boolean; canSubmit: boolean; onSubmit: () => void;
}) {
  const options = dispositionOptions(event.kind);
  if (!options.length) return null;
  const label = (value: string) =>
    options.find(option => option.value === value)?.label || value;
  return <div className="disposition" aria-label="人工处置">
    <h4>它在问你</h4>
    {recorded.length > 0 && <ul className="disposition-log">
      {recorded.map(row => <li key={row.recorded_at}>
        <b>{label(row.answer)}</b>
        <span>{row.handled_by} · {new Date(row.recorded_at).toLocaleString("zh-CN")}</span>
        {row.note && <small>{row.note}</small>}
      </li>)}
    </ul>}
    {recorded.length === 0 && (canSubmit ? <>
      <div className="disposition-answers" role="radiogroup" aria-label="处置答案">
        {options.map(option => <button key={option.value} type="button"
          role="radio" aria-checked={answer === option.value}
          className={answer === option.value ? "active" : ""}
          onClick={() => onAnswer(option.value)}>{option.label}</button>)}
      </div>
      <label htmlFor="disposition-handler">处理人</label>
      <input id="disposition-handler" value={handler} maxLength={100}
        placeholder="谁做的这个决定" onChange={e => onHandler(e.target.value)} />
      <label htmlFor="disposition-note">备注（可选）</label>
      <input id="disposition-note" value={note} maxLength={500}
        placeholder="为什么这么决定" onChange={e => onNote(e.target.value)} />
      <button type="button" className="disposition-submit"
        disabled={busy || !answer || !handler.trim()} onClick={onSubmit}>
        {busy ? "留痕中…" : "记下这个决定"}</button>
    </> : <p className="disposition-offline">离线回放里没有可写入的审查，无法留痕。</p>)}
    <small className="disposition-boundary">{DISPOSITION_BOUNDARY}</small>
  </div>;
}

// 人工决策门：decision.requested 是**控制通道**——后端 _await_human 正阻塞
// 等待这个答案，最多 300 秒，超时默认 stop。它与「它在问你」的处置留痕
// 面板是两条通道：这里点了按钮，审查立即继续或停止；留痕面板只做记录，
// 从不改变审查状态。这条链此前在前端完全不存在，用户答了留痕、审查照样
// 静默超时，所以两句话必须在屏幕上把边界说穿。
function DecisionGate({event, onDecide, busy}: {
  event: EventRecord; onDecide: (decision: "continue" | "stop") => void;
  busy: boolean;
}) {
  const data = (event.data || {}) as {decision_id?: string;
    deadline_epoch?: number; default_decision?: string};
  const secondsLeft = () =>
    Math.max(0, Math.ceil(((data.deadline_epoch || 0) * 1000 - Date.now()) / 1000));
  const [remaining, setRemaining] = useState(secondsLeft);
  useEffect(() => {
    const timer = window.setInterval(() => setRemaining(secondsLeft()), 500);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data.deadline_epoch]);
  return <section className="decision-gate" role="alert" aria-label="人工决策">
    <h4>审查在等你决定</h4>
    <p>智能体走到了需要人工决策的一步：继续执行剩余的已批准实验，还是现在停止。</p>
    <p className="decision-gate-deadline">剩余 <b>{remaining}</b> 秒。
      {data.default_decision === "stop" && "超时未回答，审查会默认安全停止。"}</p>
    <div className="decision-gate-actions">
      <button type="button" className="primary" disabled={busy}
        onClick={() => onDecide("continue")}>继续执行</button>
      <button type="button" disabled={busy}
        onClick={() => onDecide("stop")}>停止审查</button>
    </div>
    <small>这是控制通道：点了按钮，审查立即继续或停止。时间线里「它在问你」的留痕面板只做记录，不改变审查状态。</small>
  </section>;
}

// 记忆规则卡：展示仓库里已确认的记忆规则（.shuimu/review-memory.yaml，
// 服务端 intake 时读出并放进 request.review_memory）。C1 起它不再只读：
// 「记住这条规则」把人确认过的规则写回仓库，撤销走同一条留痕路径。
// 两条纪律与处置面板一致：写入必须带处理人（confirmed_by）；离线回放
// 没有可写入的服务，卡片退回纯展示。
function requestRepoId(request: Record<string, unknown> | null | undefined): string {
  const source = request?.source as {repo_id?: unknown} | undefined;
  return typeof source?.repo_id === "string" ? source.repo_id : "";
}

function MemoryRulesCard({rules, repoId, writable}: {
  rules: MemoryRuleView[]; repoId: string; writable: boolean;
}) {
  const [localRules, setLocalRules] = useState(rules);
  const dirtyRef = useRef(false);
  useEffect(() => {
    // 一旦本会话写过，本地状态就比 intake 快照新，不能被旧值冲掉。
    if (!dirtyRef.current) setLocalRules(rules);
  }, [rules]);
  const [kind, setKind] = useState("review_preference");
  const [rule, setRule] = useState("");
  const [appliesTo, setAppliesTo] = useState("");
  const [handler, setHandler] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  if (!localRules.length && !writable) return null;
  const applyExport = (payload: {records?: unknown}) => {
    dirtyRef.current = true;
    setLocalRules(memoryRuleViews(payload.records));
  };
  const remember = async () => {
    if (busy || !repoId || !rule.trim() || !handler.trim()) return;
    setBusy(true); setNotice("");
    try {
      const payload = await api<{records?: unknown}>(
        `/api/v2/repos/${encodeURIComponent(repoId)}/memory`, {
          method: "POST",
          body: JSON.stringify({
            kind, rule: rule.trim(),
            applies_to: appliesTo.split(/[,，\s]+/).map(item => item.trim()).filter(Boolean),
            confirmed_by: handler.trim(),
          }),
        });
      applyExport(payload);
      setRule(""); setAppliesTo("");
      setNotice("已记住。下次审查这份规则自动生效。");
    } catch (error) {
      setNotice(noticeFor(error).message);
    } finally {
      setBusy(false);
    }
  };
  const revoke = async (memoryId: string) => {
    if (busy || !repoId) return;
    if (!handler.trim()) { setNotice("撤销前先填处理人：规则变动要留痕到人。"); return; }
    setBusy(true); setNotice("");
    try {
      const payload = await api<{records?: unknown}>(
        `/api/v2/repos/${encodeURIComponent(repoId)}/memory/${encodeURIComponent(memoryId)}`, {
          method: "PATCH",
          body: JSON.stringify({status: "revoked", confirmed_by: handler.trim()}),
        });
      applyExport(payload);
      setNotice("已撤销。这条规则下次审查不再生效。");
    } catch (error) {
      setNotice(noticeFor(error).message);
    } finally {
      setBusy(false);
    }
  };
  return <div className="memory-card" aria-label="仓库审查记忆">
    <h4>{MEMORY_CARD_HEADLINE}</h4>
    <ul>{localRules.map(item => <li key={item.memory_id}>
      <b>{item.kind}</b>
      <span>{item.rule}</span>
      {item.applies_to.length > 0 && <small>适用：{item.applies_to.join(" · ")}</small>}
      {item.status !== "active" && <small className="memory-status">
        {MEMORY_STATUS_LABELS[item.status] || item.status}</small>}
      {writable && item.status === "active" && <button type="button"
        className="memory-revoke" disabled={busy}
        onClick={() => void revoke(item.memory_id)}>撤销</button>}
    </li>)}</ul>
    {writable && <div className="memory-form">
      <label htmlFor="memory-handler">处理人</label>
      <input id="memory-handler" value={handler} maxLength={100}
        placeholder="谁在确认或撤销规则" onChange={e => setHandler(e.target.value)} />
      <label htmlFor="memory-kind">类型</label>
      <select id="memory-kind" value={kind} onChange={e => setKind(e.target.value)}>
        {memoryKindOptions().map(option =>
          <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <label htmlFor="memory-rule">规则内容（最多 500 字）</label>
      <input id="memory-rule" value={rule} maxLength={500}
        placeholder="例如：tests/ 下的夹具一律用小写状态字面量"
        onChange={e => setRule(e.target.value)} />
      <label htmlFor="memory-applies">适用路径（可选，逗号分隔）</label>
      <input id="memory-applies" value={appliesTo} maxLength={400}
        placeholder="例如：modou/agent, web/src" onChange={e => setAppliesTo(e.target.value)} />
      <button type="button" className="memory-submit"
        disabled={busy || !rule.trim() || !handler.trim()}
        onClick={() => void remember()}>{busy ? "保存中…" : "记住这条规则"}</button>
    </div>}
    {notice && <small className="memory-notice" role="status">{notice}</small>}
    <small className="memory-boundary">记忆只是上下文：不改变计划指纹与人工批准要求，也不授予任何工具或网络权限。</small>
  </div>;
}

async function consumeEvents(reviewId: string, after: string,
  onEvent: (event: EventRecord) => void, signal: AbortSignal) {
  const headers: Record<string, string> = {Authorization: `Bearer ${sessionToken}`};
  if (after) headers["Last-Event-ID"] = after;
  let response: Response;
  try {
    response = await fetch(`/api/v1/reviews/${reviewId}/events`, {headers, signal});
  } catch (error) {
    throw new HttpError(undefined, "NETWORK_ERROR",
      error instanceof Error ? error.message : "事件流连接失败");
  }
  if (!response.ok || !response.body) {
    const raw = await response.json().catch(() => ({}));
    throw new HttpError(response.status, raw.code, raw.message || `事件流连接失败：${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      if (!frame || frame.startsWith(":")) continue;
      const data = frame.split("\n").find(line => line.startsWith("data: "));
      if (data) onEvent(JSON.parse(data.slice(6)) as EventRecord);
    }
  }
}

const terminal = new Set(["COMPLETE", "COMPLETED", "PARTIAL", "FAILED", "ABORTED", "CANCELLED"]);

// 6.3 五阶段单页旅程：主区一次只展示一个阶段，左侧导航是直接入口。
// 当前页不是 URL 状态（URL hash 只留给安全 token），因此刷新/换页时用会话存储
// 记住页签；运行状态变化只允许自动推进到结论页，不会替用户打开代码页。
const JOURNEY_STAGES = [
  {key: "select", label: "选择审查对象", short: "审查对象"},
  {key: "plan", label: "确认审查计划", short: "审查计划"},
  {key: "experiment", label: "验证测试保护", short: "测试保护"},
  {key: "result", label: "理解审查结论", short: "审查结论"},
  {key: "evidence", label: "查看代码证据", short: "代码证据"},
  {key: "disposition", label: "处置与复验", short: "处置复验"},
] as const;
// 6.4 智能体在做什么：智能体五个任务相各自的一条关键输入、一条产出、
// 一个人工门。门是事实清单，不是装饰：五相只有三相有人工门（01 确认计划、
// 03 人工质疑、05 评论与记忆确认），02 证据实验是纯机器段，04 结论由证据
// 签发，都不许为了整齐编出门来。
// 步骤词汇共用一张词表：计划 / 测试保护 / 证据实验 / 质疑 / 结论 / 代码证据；
// 任务面板与阶段导航都从这里取词，不再各造一套。
const AGENT_PULSE = [
  {no: "01", key: "plan", label: "审查计划", input: "选定的仓库与审查意图",
    output: "范围、候选与预注册的实验计划", gate: "确认计划"},
  {no: "02", key: "experiment", label: "证据实验", input: "已确认的计划",
    output: "证据账本与可复验的实验记录", gate: null},
  {no: "03", key: "challenge", label: "质疑", input: "初步取证结果",
    output: "五类质疑与各自的验证实验", gate: "人工质疑"},
  {no: "04", key: "conclusion", label: "结论", input: "质疑后的完整证据",
    output: "结论、证据护照与口径声明", gate: null},
  {no: "05", key: "memory", label: "评论与记忆", input: "结论与逐行证据",
    output: "评论、处置记录与仓库审查记忆", gate: "评论与记忆确认"},
] as const;
const SESSION_STAGE_KEY = "modou.session.stage";
// #45 本会话见过的审查 id：页头「审查历史」入口的数据源。后端没有
// 列出审查的接口，只能客户端记；随标签页结束清空，上限 20 条。
// 故意不参与清空工作台的清场：清空之后还能回到上一次结果，正是它的职责。
const REVIEW_HISTORY_KEY = "modou.session.review_history";
const REVIEW_HISTORY_LIMIT = 20;
const STAGE_STATE_LABELS = {waiting: "等待", active: "进行中", ready: "就绪", done: "已完成", error: "异常"} as const;
type StageState = keyof typeof STAGE_STATE_LABELS;

async function fetchTerminalReview(reviewId: string): Promise<Review> {
  // The completion event is journaled immediately before the terminal state
  // transition.  A client can therefore observe the event in the tiny window
  // between those two durable writes.  Poll briefly instead of treating that
  // intermediate snapshot as final; this closes the race without weakening
  // the server's event-first recovery semantics.
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const next = await api<Review>(`/api/v1/reviews/${reviewId}`);
    if (terminal.has(next.state.status)) return next;
    await new Promise(resolve => window.setTimeout(resolve, 25));
  }
  // 专用错误码，而不是裸 Error：裸 Error 会被 noticeFor 归成
  // classifyError(undefined) → 「本地服务连接中断」，把"服务在跑、
  // 只是终态快照还没落盘"谎报成"连接断了"。
  throw new HttpError(undefined, "TERMINAL_SNAPSHOT_TIMEOUT",
    "服务端已发出完成事件，但终态快照未及时可见");
}

// 终态 reason → 中文标题：结果页最大的字不允许出现裸英文串。
// 键与 modou/server/control.py 的 transition reason 一一对应；映射不到
// 的 reason（多为 SessionError 的 "stage:detail" 或异常类名）一律落
// 「审查未正常完成」，原始串收进技术记录行，不再顶到大标题。
const TERMINAL_REASON_TITLES: Record<string, string> = {
  "review_cancelled:user": "审查已按你的要求中止。",
  "review_aborted:process_restart": "本地服务重启，审查已中止。",
  "review_aborted:manual_cleanup": "工作区已人工清理，审查已中止。",
  "plan_superseded": "计划已被修订版本取代，本次审查已归档。",
  "event_journal_invalid": "事件账本校验未通过，审查已隔离。",
  "manual_cleanup_requested": "工作区需要人工清理。",
  "manual_cleanup_failed": "自动清理未完成，工作区需要人工处理。",
  "manual_cleanup_verified": "人工清理已确认。",
  "manual_cleanup": "工作区已人工清理。",
};

function findCertificate(record: Record<string, unknown>, bundle: ReviewBundle | null): Certificate | undefined {
  const payload = (record.payload || {}) as Record<string, unknown>;
  const anchor = (payload.anchor || {}) as Record<string, unknown>;
  // 落点规则只有一份，放在 conclusion-tools 里：证据抽屉与逐行筛选必须同一条规则，
  // 否则「抽屉里说 A 级」和「按 A 级筛得出来」会各说各话。
  return certificateForPoint(bundle?.evidence_bundle?.report?.certificates, {
    path: String(anchor.path || ""), line: Number(anchor.line_start || 0)});
}

function EvidenceCard({record, bundle}: {record: Record<string, unknown>; bundle: ReviewBundle | null}) {
  const payload = (record.payload || {}) as Record<string, unknown>;
  const data = (payload.data || {}) as Record<string, unknown>;
  const anchor = (payload.anchor || {}) as Record<string, unknown>;
  const scope = (payload.scope || {}) as Record<string, unknown>;
  const cert = findCertificate(record, bundle);
  const claimId = String(record.record_id || "");
  const narration = bundle?.narration?.blocks?.find(block => block.claim_id === claimId);
  const regressions = (data.regressions || []) as Array<Record<string, unknown>>;
  const basis = cert?.依据?.length ? cert.依据 : regressions.map(row =>
    `${String(row.test_id || "具名测试")}：${String(row.before || "?")} → ${String(row.after || "?")}`);
  const uncovered = cert?.未覆盖?.length ? cert.未覆盖 :
    bundle?.evidence_bundle?.report?.口径声明 || COMMON_UNCOVERED;
  const grade = admissibilityView(cert?.可采性);
  const contrast = cert ? admissibilityContrast(cert) : null;
  const gradeRows = certificateGradeRows(cert as unknown as Record<string, unknown>);
  if (record.record_type !== "Claim") {
    return <div className="record-overview">
      <dl><div><dt>记录类型</dt><dd>{String(record.record_type || "记录")}</dd></div>
        <div><dt>记录编号</dt><dd>{String(record.record_id || "—")}</dd></div></dl>
      <details><summary>展开原始记录</summary><pre>{JSON.stringify(record, null, 2)}</pre></details>
    </div>;
  }
  return <div className="claim-card">
    <div className="claim-status"><span>{cert?.状态 || "证据主张"}</span>
      <small>{String(payload.kind || "Claim")}</small></div>
    {grade && <div className={`admissibility-badge tone-${grade.tone}`}>
      <b>{grade.label}</b><span>{grade.detail}</span></div>}
    {contrast && <div className="admissibility-contrast">{contrast}</div>}
    <section><h4>主张</h4><p>{cert?.主张 || String(narration?.text ||
      `在声明测试范围内，干预 ${String(anchor.path || "该代码单元")} 后观察到具名测试回归。`)}</p></section>
    <section><h4>依据</h4>{basis.length ? <ul>{basis.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>关联证据见下方证据编号。</p>}</section>
    {gradeRows.length > 0 && <section className="cert-grades"><h4>逐条定级</h4>
      <ul>{gradeRows.map(row => <li key={row.testId}>
        <code>{row.testId}</code>：{row.before || "—"} → {row.after || "—"}
        （{row.grade || "未标注"}）
      </li>)}</ul></section>}
    <section><h4>方法</h4><p>{cert?.方法 || "对该代码单元实施可恢复干预，逐项比较声明测试状态向量，并检查工作区恢复。"}</p></section>
    <section><h4>口径</h4><p>{cert?.口径 || String(scope["范围"] || "结论仅适用于本次声明测试范围。")}</p></section>
    <section className="uncovered"><h4>未覆盖</h4><ul>{uncovered.map((item, i) => <li key={i}>{item}</li>)}</ul></section>
    <section><h4>状态</h4><p>{data.restore_clean === true ? "实验完成，恢复校验通过。" : "以证据账本中的实验终态为准。"}
      {cert?.["回滚干净"] === true ? " 回滚干净。" : cert?.["回滚干净"] === false
        ? " 回滚有残留，需人工检查。" : ""}
      {typeof cert?.["耗时秒"] === "number" ? ` 定级耗时 ${cert["耗时秒"]} 秒。` : ""}</p></section>
    <div className="provenance"><h4>证据编号</h4>{((payload.provenance || []) as string[]).map(id => <code key={id}>{id}</code>)}</div>
    <details><summary>展开原始记录</summary><pre>{JSON.stringify(record, null, 2)}</pre></details>
  </div>;
}

// 候选顺序变化时的位置过渡：记录变更前后的位置差，用 transform 平移回去
// 再放开，形成"headers.py 挪到 pool.py 前面"的直观画面。尊重系统的
// 减少动态效果设置；数据没变就不动。
function useFlipReorder(order: string[]) {
  const refs = useRef(new Map<string, HTMLElement>());
  const prevBoxes = useRef(new Map<string, {left: number; top: number}>());
  const key = order.join("|");
  const setRef = (id: string) => (el: HTMLElement | null) => {
    if (el) refs.current.set(id, el);
    else refs.current.delete(id);
  };
  function measure(): Map<string, {left: number; top: number}> {
    const boxes = new Map<string, {left: number; top: number}>();
    for (const [id, el] of refs.current) {
      const rect = el.getBoundingClientRect();
      boxes.set(id, {left: rect.left, top: rect.top});
    }
    return boxes;
  }
  useLayoutEffect(() => {
    const reduced = typeof matchMedia === "function"
      && matchMedia("(prefers-reduced-motion: reduce)").matches;
    const current = measure();
    if (!reduced && prevBoxes.current.size > 0) {
      for (const [id, box] of current) {
        const before = prevBoxes.current.get(id);
        if (!before) continue;
        const dx = before.left - box.left, dy = before.top - box.top;
        if (!dx && !dy) continue;
        const el = refs.current.get(id);
        if (!el) continue;
        el.style.transition = "none";
        el.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          el.style.transition = "transform 480ms cubic-bezier(.2,.8,.2,1)";
          el.style.transform = "";
        });
      }
    }
    prevBoxes.current = current;
  }, [key]);
  return setRef;
}

function App() {
  const [productMode, setProductMode] = useState<ProductMode>("standard");
  // 标准审查把仓库、测试范围、预算、最小化策略收进「高级设置」；
  // 智能体审查复用同一面板，只是默认展开全部配置。
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [repos, setRepos] = useState<Repo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [theme, setTheme] = useState<"dark" | "beige">(() => {
    // 答辩默认使用深色；曾主动选择浅色的用户继续沿用自己的选择。
    try { return localStorage.getItem("shuimu-theme") === "beige" ? "beige" : "dark"; }
    catch { return "dark"; }
  });

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "beige") root.dataset.theme = "beige";
    else root.removeAttribute("data-theme");
    document.querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", theme === "beige" ? "#f3eee2" : "#0b0714");
    try { localStorage.setItem("shuimu-theme", theme); } catch { /* 隐私模式下静默降级 */ }
  }, [theme]);
  const [repoId, setRepoId] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [tests, setTests] = useState("tests/test_backoff.py");
  const [goal, setGoal] = useState(DEFAULT_REVIEW_GOAL);
  const [goalPreset, setGoalPreset] = useState<string>("evidence-boundary");
  const [budget, setBudget] = useState(300);
  const [provider, setProvider] = useState("deterministic");
  const [probeStrategy, setProbeStrategy] = useState("hdd_inspired");
  const [allowRepairBranch, setAllowRepairBranch] = useState(false);
  const [repairPatch, setRepairPatch] = useState("");
  const [repairBusy, setRepairBusy] = useState(false);
  const [testProposal, setTestProposal] = useState<TestProposalStatus | null>(null);
  const [proposalBusy, setProposalBusy] = useState(false);
  const [proposalNotice, setProposalNotice] = useState("");
  // #9/#10 只读入账：编辑会话与五指纹交付记录的展示数据。
  const [editSessions, setEditSessions] = useState<EditSessionLedger | null>(null);
  const [editSessionsNotice, setEditSessionsNotice] = useState("");
  // M2 第二步：五指纹批准的表单状态；批准只冻结比对基线，不应用改动。
  const [approvalNote, setApprovalNote] = useState("");
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [approvalNotice, setApprovalNotice] = useState("");
  // M2 第四步：导出内容哈希清单。导出前服务端会重算五指纹并与
  // 批准基线逐一比对；界面只发一个空请求，不带任何自有判断。
  const [exportBusyId, setExportBusyId] = useState("");
  const [exportNotice, setExportNotice] = useState("");
  // M1 第三步：编辑会话的写路径——开启会话、显式放弃、生成候选补丁。
  // 生成与应用是两件事：候选正文只留在服务端，交付仍要人贴补丁确认。
  const [sessionIntent, setSessionIntent] = useState("");
  const [sessionBusy, setSessionBusy] = useState(false);
  const [sessionNotice, setSessionNotice] = useState("");
  const [abandonBusyId, setAbandonBusyId] = useState("");
  const [candidateFindingIds, setCandidateFindingIds] = useState("");
  const [snippetDrafts, setSnippetDrafts] = useState<
    Array<{path: string; start: string; text: string}>>([{path: "", start: "", text: ""}]);
  const [candidateBusy, setCandidateBusy] = useState(false);
  const [candidateNotice, setCandidateNotice] = useState("");
  const [repairCandidate, setRepairCandidate] = useState<RepairCandidateRecord | null>(null);
  const [repairSessionId, setRepairSessionId] = useState("");
  const [providerInfo, setProviderInfo] = useState<Record<string, unknown>>({});
  const [nlText, setNlText] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [draftResult, setDraftResult] = useState<DraftPayload | null>(null);
  const [draftNotice, setDraftNotice] = useState("");
  const [locating, setLocating] = useState(false);
  const [locateResult, setLocateResult] = useState<LocatePayload | null>(null);
  const [locateNotice, setLocateNotice] = useState("");
  // #46 引导式入口：固定问题依次问；4 表示四问答完，显示汇总。
  const [guideStep, setGuideStep] = useState(0);
  // #44 独立教程页开关：整页替换主界面，不是首屏引导层。路由化后开关
  // 只跟地址栏走：直接落地 #/tutorial 也能打开教程；地址不含 token，
  // 分享出去不会带走会话凭据。
  const [tutorialOpen, setTutorialOpen] = useState(() => isTutorialRoute(location.hash));
  const [scopeInclude, setScopeInclude] = useState<string[]>([]);
  const [focusOverride, setFocusOverride] = useState("");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [releaseStatus, setReleaseStatus] = useState<ReleaseStatus>(() => ({
    schema_version: "v02-release-status-v1", machine_status: "UNLOADED",
    release_status: "INTERNAL_ONLY", stable_eligible: false,
    external_gates_pending: EXTERNAL_GATES_FALLBACK, source_commit: "",
  }));
  // 常驻授权与评测回执都按 release-status 的解耦模式取：授权面或证据
  // 读不到时如实显示"未加载/暂缺"，绝不拖垮选仓库、建审查的主流程。
  const [standingAuth, setStandingAuth] =
    useState<StandingAuthorizationView>(() => standingAuthorizationView(null));
  const [receiptIndex, setReceiptIndex] = useState<EvalReceiptIndexEntry[]>([]);
  const [receiptDetail, setReceiptDetail] = useState<EvalReceiptView | null>(null);
  const [receiptSelectedId, setReceiptSelectedId] = useState("");
  const [review, setReview] = useState<Review | null>(null);
  const [taskRequirementDraft, setTaskRequirementDraft] = useState("");
  const [preferredTaskId, setPreferredTaskId] = useState(initialTaskId);
  const [reviewBundle, setReviewBundle] = useState<ReviewBundle | null>(null);
  const [modelCalls, setModelCalls] = useState<ModelCall[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [selected, setSelected] = useState<EventRecord | null>(null);
  const [selectedLine, setSelectedLine] = useState<DiffLine | null>(null);
  const [evidenceDetail, setEvidenceDetail] = useState<Record<string, unknown> | null>(null);
  const [dispositions, setDispositions] = useState<Disposition[]>([]);
  const [dispositionAnswer, setDispositionAnswer] = useState("");
  const [dispositionHandler, setDispositionHandler] = useState("");
  const [dispositionNote, setDispositionNote] = useState("");
  const [dispositionBusy, setDispositionBusy] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  // 最后一条没有被回答/超时的 decision.requested：当前挂着的人工决策门。
  // decision.answered / decision.defaulted 各自带 decision_id，按 id 配对
  // 关门，避免旧决策事件把新门提前关掉。
  const pendingDecision = useMemo(() => {
    let requested: EventRecord | null = null;
    for (const event of events) {
      const data = (event.data || {}) as {decision_id?: string};
      if (event.kind === "decision.requested") requested = event;
      else if (requested && (event.kind === "decision.answered"
          || event.kind === "decision.defaulted")
        && data.decision_id
          === ((requested.data || {}) as {decision_id?: string}).decision_id) {
        requested = null;
      }
    }
    return requested;
  }, [events]);
  const [planRevisions, setPlanRevisions] = useState<Record<string, unknown> | null>(null);
  const [reviseOpen, setReviseOpen] = useState(false);
  const [reviseInstruction, setReviseInstruction] = useState("");
  const [reviseReason, setReviseReason] = useState("");
  const [reviseBusy, setReviseBusy] = useState(false);
  const [replayEvidence, setReplayEvidence] = useState<Record<string, Record<string, unknown>>>({});
  const [diffLines, setDiffLines] = useState<DiffLine[]>([]);
  // #44 结论筛选：三态 / 文件 / 可采性等级。筛选只影响显示，行数与总数一起报。
  const [conclusionFilter, setConclusionFilter] = useState<ConclusionFilter>(EMPTY_FILTER);
  // #44 键盘导航的落点：j/k 只在**能打开证据**的行之间移动。
  const [navLine, setNavLine] = useState<DiffLine | null>(null);
  const [summaryNotice, setSummaryNotice] = useState("");
  const [summaryFallback, setSummaryFallback] = useState("");
  const [workbenchOpen, setWorkbenchOpen] = useState(false);
  const [workbenchFocus, setWorkbenchFocus] = useState<FocusTarget | null>(null);
  const [error, setError] = useState<UiNotice | null>(null);
  const [tokenInput, setTokenInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [offlineReplay, setOfflineReplay] = useState(false);
  const [workspaceFocus, setWorkspaceFocus] = useState<WorkspaceFocus>("next_run_setup");
  const [undoVisible, setUndoVisible] = useState(false);
  const lastEvent = useRef("");
  const resultScrollTop = useRef(0);
  const undoSnapshot = useRef<WorkbenchSnapshot | null>(null);
  const undoTimer = useRef<number | null>(null);
  const workbenchReturnFocus = useRef<HTMLElement | null>(null);
  const workbenchScrollTop = useRef(0);

  useEffect(() => {
    if (!workbenchOpen) return;
    workbenchReturnFocus.current = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    workbenchScrollTop.current = window.scrollY;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
      window.scrollTo({top: workbenchScrollTop.current, behavior: "auto"});
      workbenchReturnFocus.current?.focus({preventScroll: true});
    };
  }, [workbenchOpen]);

  // v2 审查到达终态时读一次提议状态；换审查或回放离线包时清空，避免串台。
  useEffect(() => {
    if (!review || review.request?.schema_version !== "review-request-v2"
        || offlineReplay || !terminal.has(review.state.status)) {
      setTestProposal(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const status = await api<TestProposalStatus>(
          `/api/v2/reviews/${review.review_id}/test-proposal`);
        if (!cancelled) setTestProposal(status);
      } catch { /* 面板有手动重读按钮；这里静默即可 */ }
    })();
    return () => { cancelled = true; };
  }, [review?.review_id, review?.state.status, review?.request?.schema_version, offlineReplay]);

  async function loadConfiguration() {
    if (!sessionToken) {
      setError(classifyError(401, "TOKEN_MISSING",
        "请粘贴终端启动信息中的完整地址或 #token= 后面的内容。"));
      return;
    }
    try {
      const [r, p, presetResponse, capabilityResponse] = await Promise.all([
        api<{repos: Repo[]}>("/api/v1/repos"),
        api<Record<string, unknown>>("/api/v1/providers"),
        api<{presets: Preset[]}>("/api/v1/presets"),
        api<{capabilities: Capability[]}>("/api/v1/capabilities"),
      ]);
      const nextRepos = configurationArray<Repo>(r, "repos");
      const nextPresets = configurationArray<Preset>(presetResponse, "presets");
      const nextCapabilities = configurationArray<Capability>(capabilityResponse, "capabilities");
      setCapabilities(nextCapabilities);
      setRepos(nextRepos); setRepoId(old => old || nextRepos[0]?.repo_id || "");
      setPresets(nextPresets);
      setPresetId(old => old || nextPresets[0]?.preset_id || "");
      setProviderInfo(p); setError(null);
      // Release evidence is deliberately decoupled from the core intake
      // requests. A stale/missing release record must not prevent choosing a
      // repository or creating a review.
      api<ReleaseStatus>("/api/v2/release-status")
        .then(response => setReleaseStatus(normalizeReleaseStatus(response)))
        .catch(() => setReleaseStatus(current => ({...current, machine_status: "UNLOADED",
          evidence: {status: {state: "load_error"}}})));
      // 授权卡同样与主流程解耦：读不到就如实显示"未加载常驻授权"。
      api<Record<string, unknown>>("/api/v1/standing-authorization")
        .then(response => setStandingAuth(standingAuthorizationView(response)))
        .catch(() => setStandingAuth(standingAuthorizationView(null)));
      api<{receipts: unknown[]}>("/api/v1/eval-receipts")
        .then(response => {
          const entries = normalizeEvalReceiptIndex(response);
          setReceiptIndex(entries);
          // 默认展示最新一份回执；读不到明细时保持空，不伪造证据。
          const newest = entries[0];
          if (newest) return api<Record<string, unknown>>(
            `/api/v1/eval-receipts/${encodeURIComponent(newest.id)}`)
            .then(detail => {
              const view = normalizeEvalReceipt(detail);
              if (view) {
                setReceiptDetail({...view, id: newest.id});
                setReceiptSelectedId(newest.id);
              }
            })
            .catch(() => setReceiptDetail(null));
        })
        .catch(() => setReceiptIndex([]));
      if (initialTaskId) {
        await openTaskLink(initialTaskId);
        return;
      }
      const remembered = sessionStorage.getItem(SESSION_REVIEW_KEY);
      if (remembered) {
        try {
          setReview(await api<Review>(`/api/v1/reviews/${remembered}`));
          rememberReviewId(remembered);
          setWorkspaceFocus("current_result");
        } catch (restoreError) {
          if (restoreError instanceof HttpError && restoreError.status === 404) {
            sessionStorage.removeItem(SESSION_REVIEW_KEY);
          } else {
            throw restoreError;
          }
        }
      }
    } catch (e) {
      const notice = noticeFor(e);
      setError({...notice, title: notice.level === "network" ? "配置暂时不可用" : notice.title,
        recovery: notice.recovery || "retry"});
    }
  }

  useEffect(() => { void loadConfiguration(); }, []);

  // 回执浏览器只在两份回执之间切换视角，证据本身永远只读。
  const showReceipt = (id: string) => {
    if (!id || id === receiptSelectedId) return;
    setReceiptSelectedId(id);
    setReceiptDetail(null);
    void api<Record<string, unknown>>(`/api/v1/eval-receipts/${encodeURIComponent(id)}`)
      .then(detail => {
        const view = normalizeEvalReceipt(detail);
        if (view) setReceiptDetail({...view, id});
      })
      .catch(() => setReceiptDetail(null));
  };

  useEffect(() => {
    const syncTutorialRoute = () => {
      setTutorialOpen(isTutorialRoute(location.hash));
      const params = new URLSearchParams(location.hash.slice(1));
      const taskId = params.get("task");
      const routeToken = params.get("token");
      if (routeToken) {
        sessionToken = resolveStartupToken(location.hash, sessionToken);
        sessionStorage.setItem(SESSION_TOKEN_KEY, sessionToken);
        history.replaceState(null, "", location.pathname + location.search + (taskId ? taskLink(taskId) : ""));
      }
      if (taskId) void openTaskLink(taskId).catch(e => setError(noticeFor(e)));
    };
    window.addEventListener("hashchange", syncTutorialRoute);
    return () => window.removeEventListener("hashchange", syncTutorialRoute);
  }, []);

  useLayoutEffect(() => {
    if (!review || workspaceFocus !== "current_result") return;
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({top: resultScrollTop.current, behavior: "auto"});
    });
    return () => window.cancelAnimationFrame(frame);
  }, [workspaceFocus, review?.review_id]);

  useEffect(() => {
    if (!review || terminal.has(review.state.status)) return;
    const controller = new AbortController();
    consumeEvents(review.review_id, lastEvent.current, event => {
      lastEvent.current = event.event_id;
      setEvents(old => old.some(x => x.event_id === event.event_id) ? old : [...old, event]);
      if (event.kind === "review.completed" || event.kind === "review.failed") {
        fetchTerminalReview(review.review_id).then(setReview)
          .catch(e => setError(noticeFor(e)));
      }
    }, controller.signal).catch(e => {
      if (e.name !== "AbortError") setError(noticeFor(e));
    });
    return () => controller.abort();
  }, [review?.review_id, review?.state.status]);

  // 父链在审查换 id 时重取：修订会换 review_id，旧链条属于旧稿。
  useEffect(() => {
    if (!review || review.request?.schema_version !== "review-request-v2") {
      setPlanRevisions(null);
      return;
    }
    void loadPlanRevisions(review.review_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [review?.review_id]);

  useEffect(() => {
    if (!review || !["COMPLETE", "PARTIAL"].includes(review.state.status) ||
        reviewBundle?.review_id === review.review_id) return;
    api<ReviewBundle>(`/api/v1/reviews/${review.review_id}/bundle`).then(bundle => {
      setReviewBundle(bundle);
      setDiffLines(bundle.evidence_bundle?.report?.render_model?.lines || []);
      setEvents(bundle.events || []);
      const rows = bundle.evidence_bundle?.ledger || [];
      setReplayEvidence(Object.fromEntries(rows.map(row => [String(row.record_id), row])));
      if (String(bundle.request?.model_provider || "") === "live") {
        api<{calls: ModelCall[]}>(`/api/v1/reviews/${review.review_id}/model-transcript`)
          .then(transcript => setModelCalls(transcript.calls || []))
          .catch(e => setError(noticeFor(e)));
      } else setModelCalls([]);
    }).catch(e => setError(noticeFor(e)));
  }, [review?.review_id, review?.state.status, reviewBundle?.review_id]);

  // 可采性按证书的落点定级；证书缺失的行如实算「未定级」，不猜等级。
  const certificates = reviewBundle?.evidence_bundle?.report?.certificates;
  const visibleLines = useMemo(
    () => filterConclusions(diffLines, conclusionFilter, certificates),
    [diffLines, conclusionFilter, certificates]);

  // 键盘导航的候选只有「能打开证据」的行：点了没反应的行不该被键盘选中。
  const navLines = useMemo(
    () => diffLines.filter(line => Boolean(decisiveEvidenceId(line, reviewBundle))),
    [diffLines, reviewBundle]);

  // 游离按文件折叠，其余保持逐行。分组只做展示，不改变任何结论。
  const diffGroups = useMemo(() => {
    const out: Array<{kind: "drift" | "lines"; file: string; lines: DiffLine[]}> = [];
    // 游离是文件级结论：同一文件不管被多少非游离行隔开，都折进同一条带，
    // 否则既出现两条同名文件带，React 的 key（drift:文件）也会撞车。
    const driftIndex = new Map<string, number>();
    for (const line of visibleLines) {
      const kind: "drift" | "lines" = line.label === "游离" ? "drift" : "lines";
      if (kind === "drift") {
        const at = driftIndex.get(line.file);
        if (at !== undefined) { out[at].lines.push(line); continue; }
        driftIndex.set(line.file, out.length);
        out.push({kind, file: line.file, lines: [line]});
        continue;
      }
      const last = out[out.length - 1];
      if (last && last.kind === "lines" && last.file === line.file) { last.lines.push(line); continue; }
      out.push({kind, file: line.file, lines: [line]});
    }
    return out;
  }, [visibleLines]);

  const featuredLine = useMemo(() => {
    const candidates = visibleLines.length ? visibleLines : diffLines;
    return candidates.find(line => line.label === "承重"
      && Boolean(decisiveEvidenceId(line, reviewBundle)))
      || candidates.find(line => Boolean(decisiveEvidenceId(line, reviewBundle)))
      || candidates[0];
  }, [visibleLines, diffLines, reviewBundle]);
  const featuredTests = featuredLine
    ? testsForEvidence(reviewBundle?.evidence_bundle?.ledger, featuredLine.evidence_ids) : [];
  const featuredCertificate = featuredLine
    ? certificateForPoint(certificates, {path: featuredLine.file, line: featuredLine.line}) : undefined;
  const featuredGrade = admissibilityView(featuredCertificate?.可采性);
  const [featuredSourceLine, setFeaturedSourceLine] = useState<string | null>(null);
  useEffect(() => {
    setFeaturedSourceLine(null);
    if (!review || offlineReplay || !featuredLine
      || featuredLine.text?.trim()) return;
    let live = true;
    void api<SourceFile>(`/api/v2/reviews/${review.review_id}/source/file?path=${
      encodeURIComponent(featuredLine.file)}`)
      .then(file => {
        if (live) setFeaturedSourceLine(file.content.split(/\r?\n/)[featuredLine.line - 1] || "");
      })
      .catch(() => { if (live) setFeaturedSourceLine(""); });
    return () => { live = false; };
  }, [review?.review_id, offlineReplay,
    featuredLine?.file, featuredLine?.line, featuredLine?.text]);

  const grouped = useMemo(() => events.map(event => ({
    ...event, ...eventPresentation(event), label: eventLabel(event.kind),
    time: new Date(event.occurred_at).toLocaleTimeString("zh-CN", {hour12: false}),
  })), [events]);
  // 6.1.3 结果运行模式：先算出"这份结果当时以什么身份跑"，再渲染任何
  // 结果区域。下面的时间线、泳道、结论与模型面板一律读它，不读当前
  // 切换按钮——切换按钮只属于下一次运行。
  const currentResultProvider = String(reviewBundle?.request?.model_provider
    || review?.request?.model_provider
    || (reviewBundle?.provider?.kind === "openai-compatible" ? "live" : "deterministic"));
  const resultProductMode = String(reviewBundle?.product_mode
    || reviewBundle?.request?.product_mode
    || review?.request?.product_mode || "");
  const resultRanAsAgent = resultProductMode
    ? resultProductMode === "agent" : currentResultProvider === "live";
  // 6.2.3 模型名称跟随服务端：结果区用证据包里的 provider，下一次运行
  // 的说明用 /api/v1/provider 回来的配置；都不再硬编码品牌。
  const resultModelLabel = modelLabel(reviewBundle?.provider as {model_id?: unknown});
  const setupModelLabel = modelLabel(providerInfo);
  const visibleGrouped = useMemo(() => {
    // A historical live bundle can still be opened in standard mode. Keep
    // standard reviews free of model calls, model actions and recommendation
    // output; the agent lane is the explicit opt-in transparency surface.
    if (resultRanAsAgent) return grouped;
    return grouped.filter(event => {
      if (event.kind.startsWith("model.") || event.kind.startsWith("recommendation.")) {
        return false;
      }
      // 标准审查的调度决策不再占用 model.* 命名空间；它与
      // model.action 一样不进标准审查时间线，两阶段界面统一时再上屏。
      if (event.kind === "scheduler.action") return false;
      return !(event.kind === "scheduler.next"
        && event.data?.selection === "model_reprioritized");
    });
  }, [grouped, resultRanAsAgent]);
  // 时间线的展示层归纳。只影响按钮上那行摘要，不改账本、不改抽屉内容。
  const deltas = useMemo(() => eventDeltas(visibleGrouped), [visibleGrouped]);
  const currentStatus = statusView(review?.state.status);
  const report = reviewBundle?.evidence_bundle?.report;
  const summary = report?.summary || null;
  const uncovered = report?.口径声明?.length ? report.口径声明 : COMMON_UNCOVERED;
  const isTerminal = !!review && terminal.has(review.state.status);
  const presentation = workspacePresentation(
    workspaceFocus === "current_result" ? currentResultProvider : provider,
    review?.state.status,
    workspaceFocus,
  );
  const currentLanes = useMemo(() => laneEvents(visibleGrouped, resultRanAsAgent
    && currentResultProvider === "live"),
    [visibleGrouped, currentResultProvider, resultRanAsAgent]);
  const labels = modeLabels(reviewBundle as unknown as Record<string, unknown>, offlineReplay,
    review?.request);
  // 离线回放横幅：完成标题与证据护照共用一份提示（B3）。
  const offlineNotice = useMemo(() => offlineReplayNotice(offlineReplay, events),
    [offlineReplay, events]);
  const executionMode = labels.isolation;
  const schedulingMode = labels.scheduler;
  const runMode = labels.run;
  // 屏幕上有 review/证据包时，边界声明讲的是**那一次运行**；什么都没有时
  // 它讲的是你接下来要跑的模式。两者都不是"用户此刻点了哪个按钮"。
  const boundaryAgent = (review || reviewBundle) ? resultRanAsAgent : productMode === "agent";
  // 一句话起草会真的发一次模型请求，所以它只属于智能体审查。
  const draftAvailable = productMode === "agent" && providerInfo.live_available === true;
  const displaySchedulingMode = currentResultProvider === "live" && !resultRanAsAgent
    ? "已生成证据" : schedulingMode;
  const setupLabels = modeLabels(null, false, {
    model_provider: provider,
    agent_level: providerInfo.agent_level,
    execution_mode: providerInfo.execution_mode,
  });
  const configLocked = presentation.configLocked;
  const canClearWorkbench = Boolean(!busy && review &&
    (terminal.has(review.state.status) || offlineReplay));
  // 6.1.3 运行期间锁定模式切换：切换只属于下一次运行，不能边跑边改写
  // 当前结果的呈现身份。运行结束后可切，但要说明"仅影响下一次审查"。
  const runInProgress = !!review && !terminal.has(review.state.status);
  const modeSwitchLocked = busy || runInProgress;
  // ---- 6.3 五阶段单页旅程：状态与自动推进 ----------------------------
  const [autoFollow, setAutoFollow] = useState(true);
  const autoFollowRef = useRef(true);
  const manualNavigationRef = useRef(false);
  const updateAutoFollow = (enabled: boolean) => {
    autoFollowRef.current = enabled;
    setAutoFollow(enabled);
  };
  const [timelineExpanded, setTimelineExpanded] = useState(false);
  const [reviewHistory, setReviewHistory] = useState<string[]>(() => {
    try {
      const parsed: unknown = JSON.parse(sessionStorage.getItem(REVIEW_HISTORY_KEY) || "[]");
      return Array.isArray(parsed)
        ? parsed.filter((x): x is string => typeof x === "string")
          .slice(0, REVIEW_HISTORY_LIMIT)
        : [];
    } catch { return []; }
  });
  const historyMenuRef = useRef<HTMLDetailsElement | null>(null);
  const [activeStage, setActiveStage] = useState(() => {
    try {
      const saved = JSON.parse(sessionStorage.getItem(SESSION_STAGE_KEY) || "null") as
        {review_id?: unknown; stage?: unknown} | null;
      const stage = Number(saved?.stage);
      return saved?.review_id && Number.isInteger(stage) && stage >= 0
        && stage < JOURNEY_STAGES.length ? stage : 0;
    } catch { return 0; }
  });
  const reviewStatusValue = review?.state.status || "";
  const showJourney = presentation.showCurrentResult;
  const journeyPhase = !review || !showJourney ? "setup"
    : reviewStatusValue === "AWAITING_APPROVAL" ? "awaiting"
    : terminal.has(reviewStatusValue) ? "terminal" : "running";
  const stageStatuses: StageState[] = [
    review && showJourney ? "done" : "active",
    !review ? "waiting" : reviewStatusValue === "AWAITING_APPROVAL" ? "active" : "done",
    !review || reviewStatusValue === "AWAITING_APPROVAL" ? "waiting"
      : terminal.has(reviewStatusValue)
        ? (["FAILED", "ABORTED"].includes(reviewStatusValue) ? "error" : "done")
        : "active",
    !review || !terminal.has(reviewStatusValue) ? "waiting"
      : ["FAILED", "ABORTED"].includes(reviewStatusValue) ? "error" : "done",
    diffLines.length > 0 ? "ready" : "waiting",
    review && terminal.has(reviewStatusValue) && !offlineReplay ? "ready" : "waiting",
  ];
  // 任务面板相状态：从旅程状态推导。02/03 都跟随实验相（质疑实验就发生
  // 在实验段里）；05 只走到"就绪"——确认永远是用户自己的动作。
  const pulseStates: StageState[] = [
    stageStatuses[1], stageStatuses[2], stageStatuses[2], stageStatuses[3],
    stageStatuses[4],
  ];
  const goToStage = (index: number, manual = true) => {
    const next = Math.max(0, Math.min(JOURNEY_STAGES.length - 1, index));
    setActiveStage(next);
    // 默认自动跟随；但手动浏览时不应被旧审查的异步状态抢回原页。
    // 新审查创建和计划确认会自动恢复跟随，无需用户操作开关。
    const nextFollow = !manual;
    manualNavigationRef.current = manual;
    updateAutoFollow(nextFollow);
    try {
      if (review?.review_id) sessionStorage.setItem(SESSION_STAGE_KEY, JSON.stringify({
        review_id: review.review_id, stage: next, auto_follow: nextFollow,
      }));
    } catch { /* private mode */ }
    window.scrollTo({top: 0, behavior: "auto"});
  };
  useEffect(() => {
    if (!review?.review_id) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem(SESSION_STAGE_KEY) || "null") as
        {review_id?: unknown; stage?: unknown; auto_follow?: unknown} | null;
      if (saved?.review_id !== review.review_id) return;
      // 服务恢复可能晚于用户手动点 01；此时旧缓存不能盖掉刚选的页面。
      if (manualNavigationRef.current) return;
      const stage = Number(saved.stage);
      if (Number.isInteger(stage) && stage >= 0 && stage < JOURNEY_STAGES.length) setActiveStage(stage);
      // 首次进入默认跟随；恢复动作不能替用户重新开启已暂停的跟随。
    } catch { /* malformed/old global cache is intentionally ignored */ }
  }, [review?.review_id]);

  // #44 键盘导航：j/k 在结论之间移动、Enter 打开这一行的证据抽屉、Esc 关闭。
  // 只在 05 逐行证据面板上有结论时生效，且不抢输入框与带修饰键的按键。
  useEffect(() => {
    if (activeStage !== 4 || navLines.length === 0) return;
    const onNavKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true']")) return;
      if (event.key === "Escape") {
        if (selectedLine || selected) {
          event.preventDefault(); setSelectedLine(null); setSelected(null);
        }
        return;
      }
      if (event.key === "Enter") {
        if (navLine) { event.preventDefault(); void inspectLine(navLine); }
        return;
      }
      if (event.key !== "j" && event.key !== "k") return;
      const current = navLine
        ? navLines.findIndex(line => line.file === navLine.file && line.line === navLine.line)
        : -1;
      const at = navLines[stepIndex(current, event.key === "j" ? 1 : -1, navLines.length)];
      if (!at) return;
      event.preventDefault();
      setNavLine(at);
    };
    window.addEventListener("keydown", onNavKey);
    return () => window.removeEventListener("keydown", onNavKey);
    // inspectLine 每次渲染都是新函数，这里只按导航状态重新挂。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeStage, navLines, navLine, selectedLine, selected]);

  // 键盘选中哪一行就把它滚进视窗，否则 j 到底了屏幕还停在原处。
  useEffect(() => {
    if (!navLine) return;
    document.querySelector(`[data-nav-line="${navLine.file}:${navLine.line}"]`)
      ?.scrollIntoView({block: "nearest"});
  }, [navLine]);
  const journeyPhaseRef = useRef(journeyPhase);
  useEffect(() => {
    const previous = journeyPhaseRef.current;
    journeyPhaseRef.current = journeyPhase;
    if (previous === journeyPhase || !autoFollowRef.current) return;
    const next = journeyPhase === "awaiting" ? 1
      : journeyPhase === "running" ? 2 : journeyPhase === "terminal" ? 3 : 0;
    goToStage(next, false);
    // 自动跟随只推进到结论页；代码页必须由用户手动打开。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [journeyPhase]);
  useEffect(() => {
    if (review?.state.status === "AWAITING_APPROVAL" && activeStage === 0 && autoFollowRef.current)
      goToStage(1, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [review?.state.status]);
  // #9/#10 只读入账跟随当前审查加载；面板里的「重新读取」可手动刷新。
  useEffect(() => {
    void refreshEditSessions();
    // 函数本体每次渲染重建，只按审查 id 触发即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [review?.review_id]);
  const completionLabel = summary?.analysis_completion === "complete" ? "完整审查" :
    summary?.analysis_completion === "partial" ? "部分审查" : String(review?.state.status || "—");
  const participation = modelParticipation(reviewBundle as
    {provider?: Record<string, unknown>; model_metrics?: Record<string, unknown>;
     request?: Record<string, unknown>} | null, events,
    review?.request as Record<string, unknown> | undefined);
  // 升级率分母（D1.2）：直接读证据包 narration.autonomy；旧包没有
  // 该字段时是 null，界面如实标「未记录」，不伪造成 0。
  const autonomy = autonomyView(reviewBundle as
    {narration?: {autonomy?: Record<string, unknown> | null}} | null);
  // 结果里实际使用的那台模型：优先证据包 provider 的型号，旧包回落到
  // 服务端当前配置。只用于展示名称，不参与任何判断。
  const displayModelLabel = participation.live && participation.modelLabel !== "未标注模型"
    ? participation.modelLabel : resultModelLabel;
  // #9/#10 只读入账的派生视图：收割告警与交付记录列表。
  const sessionLedger = editSessions?.edit_sessions || [];
  const sweptSessions = sessionLedger.filter(session =>
    session.status === "abandoned" || session.status === "stale");
  // 已交付但未批准的会话也要进交付区：那里是批准仪式的入口。
  const deliveryRecords = sessionLedger.filter(session =>
    session.status === "delivered"
    || Boolean(session.delivery_approval || session.delivery_export));
  // 还能承接交付的活动会话（open/patch_candidate）：交付绑定用。
  const activeSessions = sessionLedger.filter(session =>
    session.status === "open" || session.status === "patch_candidate");
  // 候选片段的体积口径与后端一致：只算片段正文的 UTF-8 字节，上限 12 KiB。
  const candidateSnippetBytes = snippetDrafts.reduce(
    (total, draft) => total + new TextEncoder().encode(draft.text).length, 0);
  // 网格按**实际渲染出来的面板**排，不按模式名排。智能体模式下没有真实模型
  // 参与时决策轨迹不上屏，若网格仍为它留一行，那一行会被 intake 列的高度撑开
  // 成一块纯空白——投影上就是"这里少做了一块"。
  const showDecisionTrack = presentation.showModelTrack
    && resultRanAsAgent && participation.live;
  const passport = evidencePassport(review?.state.status, summary, events,
    review?.plan || reviewBundle?.plan, reviewBundle?.evidence_bundle?.ledger || [],
    participation, Boolean(reviewBundle));
  const stepNo = stepNumbering([
    "intake",
    (!review || presentation.showCurrentResult) && "plan",
    showDecisionTrack && "decision",
    (!review || presentation.showCurrentResult) && "timeline",
    presentation.showCurrentResult && isTerminal && "result",
    presentation.showCurrentResult && diffLines.length > 0 && "diff",
  ]);
  const selectedPreset = presets.find(item => item.preset_id === presetId);
  // 6.2.2 案例与运行模式解耦：案例只负责仓库、目标、范围和预算，
  // 同一个案例可在两种模式中运行，切模式不换案例。
  const visiblePresets = presets;
  // 智能体审查在无可用模型通道时创建失败关闭：可以查看说明，但不能发起。
  const agentModelUnavailable = productMode === "agent" && providerInfo.live_available !== true;
  const selectedPresetRepo = repos.find(item => item.repo_id === selectedPreset?.repo_id)?.display_name;
  const demoGoalFits = DEMO_GOAL_FIT[repoSlug(repos.find(item => item.repo_id === repoId))];
  const decisionStages = modelDecisionStages(events, participation.live, isTerminal, displayModelLabel);
  const modelActions = useMemo(() => events.filter(event =>
    // 纯确定性运行也写 model.action 事件（source=deterministic）用于审计；
    // 模型决策轨迹只消费 live 调用与真实降级两类。
    event.kind === "model.action" && event.data?.source !== "deterministic"), [events]);
  const latestModelAction = modelActions[modelActions.length - 1] || null;
  const planDraftEvent = events.find(event => event.kind === "plan.drafted");
  const planSource = (() => {
    const reason = String(planDraftEvent?.data?.fallback_reason || "");
    if (!planDraftEvent) return "尚未生成";
    if (currentResultProvider !== "live") return "确定性规则草拟";
    if (!reason) return resultModelLabel + " 模型草拟";
    if (reason.startsWith("model_failed")) return "模型失败 · 确定性回退";
    if (reason.startsWith("policy_rejected")) return "策略拒绝 · 确定性回退";
    return "确定性草拟";
  })();
  // 当前检查顺序：先取最近一次模型动作后的实际顺序，其次确定性排序，
  // 最后回到冻结序。冻结全集始终不变——重排只是位置变化。
  const currentOrder = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const data = events[i].data || {};
      if (events[i].kind === "model.action" && Array.isArray(data.actual_order)
          && data.actual_order.length > 0) return data.actual_order as string[];
    }
    const deterministic = events.find(event =>
      event.kind === "scheduler.deterministic_order")?.data;
    if (Array.isArray(deterministic?.applied_order)
        && deterministic.applied_order.length > 0) {
      return deterministic.applied_order as string[];
    }
    return ((review?.plan?.priorities as string[]) || []);
  }, [events, review]);
  const orderRef = useFlipReorder(currentOrder);
  const recommendations = participation.recommendations;
  const legacyBundle = !!reviewBundle && participation.live
    && Object.keys(participation.requestsByStage).length === 0
    && !events.some(event => event.kind === "model.action");
  const repairAuthorization = ((review?.request?.review_spec || {}) as Record<string, unknown>)
    .autonomy_policy as Record<string, unknown> | undefined;
  const activeReviewSpec = (review?.request?.review_spec || {}) as Record<string, unknown>;
  const memoryRules = useMemo(
    () => memoryRuleViews(review?.request?.review_memory), [review?.request?.review_memory]);
  // 记忆写入目标是仓库本身（不是某次审查），所以 repo id 从请求回显里取。
  const memoryRepoId = requestRepoId(review?.request);
  const memoryWritable = !offlineReplay && !!memoryRepoId;
  const activeConstraints = (activeReviewSpec.constraints || {}) as Record<string, unknown>;
  const activeDataPolicy = (activeReviewSpec.data_policy || {}) as Record<string, unknown>;
  const activeOutputPolicy = (activeReviewSpec.output_policy || {}) as Record<string, unknown>;
  const activeScope = (activeReviewSpec.scope || {}) as Record<string, unknown>;
  const repairAuthorized = repairAuthorization?.allow_repair_branch === true;
  const repairView = repairStatusView(review?.repair?.status);
  const proposalEligible = Boolean(review && review.request?.schema_version === "review-request-v2"
    && !offlineReplay && terminal.has(review.state.status) && resultRanAsAgent);
  const proposalRecord = testProposal?.proposal || null;
  const proposalView = visaStatusView(proposalRecord?.status || testProposal?.status);
  const proposalVisa = (proposalRecord?.visa || null) as Record<string, unknown> | null;
  const storyEvent = (key: string): EventRecord | undefined => {
    if (key === "baseline") return events.find(event => event.kind === "baseline.completed"
      || event.kind === "baseline.failed" || event.kind === "baseline.started");
    if (key === "remove") return events.find(event => event.kind === "probe.completed"
      || event.kind === "probe.failed" || event.kind === "probe.started");
    if (key === "regression") return events.find(event => event.kind === "observation.recorded"
      && Array.isArray(event.data?.regressed_tests)
      && (event.data.regressed_tests as unknown[]).length > 0)
      || events.find(event => event.kind === "probe.completed");
    return events.find(event => event.kind === "restore.verified"
      || event.kind === "restore.completed" || event.kind === "restore.started");
  };
  const experimentStages = useMemo(
    () => experimentStory(events, report?.certificates || []),
    [events, report?.certificates],
  );

  async function applyManualToken() {
    const token = normalizePastedToken(tokenInput);
    if (!token) {
      setError(classifyError(401, "TOKEN_MISSING", "没有识别到 token，请重新粘贴。"));
      return;
    }
    sessionToken = token;
    sessionStorage.setItem(SESSION_TOKEN_KEY, token);
    setTokenInput("");
    await loadConfiguration();
  }

  async function reloadPlan() {
    if (!review) return;
    try { setReview(await api<Review>(`/api/v1/reviews/${review.review_id}`)); setError(null); }
    catch (e) { setError(noticeFor(e)); }
  }

  // #45 历史记录：最近见过的排最前，去重，封顶 20 条。
  function rememberReviewId(id: string) {
    setReviewHistory(current => {
      const next = [id, ...current.filter(x => x !== id)]
        .slice(0, REVIEW_HISTORY_LIMIT);
      try { sessionStorage.setItem(REVIEW_HISTORY_KEY, JSON.stringify(next)); }
      catch { /* private mode */ }
      return next;
    });
  }

  function forgetReviewId(id: string) {
    setReviewHistory(current => {
      const next = current.filter(x => x !== id);
      try { sessionStorage.setItem(REVIEW_HISTORY_KEY, JSON.stringify(next)); }
      catch { /* private mode */ }
      return next;
    });
  }

  // #45 从页头历史入口回到一次旧审查：只读恢复，不重跑、不调模型。
  // 服务重启后旧 id 会 404，这时把它从列表移掉，入口不指向死记录。
  async function openTaskLink(taskId: string) {
    const linkedTask = await api<EvidenceTask>(`/api/v2/tasks/${encodeURIComponent(taskId)}`);
    const linkedReview = await api<Review>(`/api/v1/reviews/${linkedTask.origin_review_id}`);
    setPreferredTaskId(taskId); setReview(linkedReview); rememberReviewId(linkedReview.review_id);
    sessionStorage.setItem(SESSION_REVIEW_KEY, linkedReview.review_id);
    setReviewBundle(null); setEvents([]); setDiffLines([]); setModelCalls([]);
    setSelected(null); setSelectedLine(null); setEvidenceDetail(null);
    setOfflineReplay(false); lastEvent.current = "";
    setWorkspaceFocus("current_result"); manualNavigationRef.current = true;
    setActiveStage(terminal.has(linkedReview.state.status) ? 5
      : linkedReview.state.status === "AWAITING_APPROVAL" ? 1 : 2);
    updateAutoFollow(!terminal.has(linkedReview.state.status));
  }

  async function openHistoryReview(id: string) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const restored = await api<Review>(`/api/v1/reviews/${id}`);
      rememberReviewId(id);
      sessionStorage.setItem(SESSION_REVIEW_KEY, id);
      history.replaceState(null, "", location.pathname + location.search);
      setPreferredTaskId("");
      setReview(restored); setReviewBundle(null); setEvents([]); setDiffLines([]);
      setModelCalls([]); setSelected(null); setSelectedLine(null);
      setEvidenceDetail(null); setDispositions([]); setReplayEvidence({});
      setOfflineReplay(false); lastEvent.current = "";
      setWorkspaceFocus("current_result");
      manualNavigationRef.current = true;
      setActiveStage(3); updateAutoFollow(false);
      historyMenuRef.current?.removeAttribute("open");
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) forgetReviewId(id);
      setError(noticeFor(e));
    } finally { setBusy(false); }
  }

  function cancelUndoWindow() {
    if (undoTimer.current !== null) {
      window.clearTimeout(undoTimer.current);
      undoTimer.current = null;
    }
    undoSnapshot.current = null;
    setUndoVisible(false);
  }

  function clearWorkbench() {
    if (!review || (!terminal.has(review.state.status) && !offlineReplay)) return;
    undoSnapshot.current = {
      review, reviewBundle, events: [...events], selected, selectedLine, evidenceDetail,
      replayEvidence: {...replayEvidence}, diffLines: [...diffLines], error, offlineReplay,
      workspaceFocus, productMode, presetId, repoId, tests, goal, goalPreset, budget,
      provider, probeStrategy, allowRepairBranch, repairPatch, lastEvent: lastEvent.current,
      resultScrollTop: workspaceFocus === "current_result" ? window.scrollY : resultScrollTop.current,
      activeStage, autoFollow, timelineExpanded,
    };
    if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
    undoTimer.current = window.setTimeout(() => {
      undoSnapshot.current = null;
      undoTimer.current = null;
      setUndoVisible(false);
    // #45 十秒对"我刚才点错了"偏短；历史入口上线后撤销窗口延到 30 秒。
    }, 30_000);
    setUndoVisible(true);
    setReview(null); setReviewBundle(null); setEvents([]); setDiffLines([]); setModelCalls([]);
    setSelected(null); setSelectedLine(null); setEvidenceDetail(null);
    setReplayEvidence({}); setOfflineReplay(false); setError(null); setBusy(false);
    setWorkspaceFocus("next_run_setup");
    manualNavigationRef.current = false;
    setActiveStage(0); updateAutoFollow(true); setTimelineExpanded(false);
    setProductMode("standard"); setAdvancedOpen(false); setPresetId(presets[0]?.preset_id || "");
    setRepoId(repos[0]?.repo_id || ""); setTests("tests/test_backoff.py");
    setGoal(DEFAULT_REVIEW_GOAL); setGoalPreset("evidence-boundary");
    setBudget(300); setProvider("deterministic"); setProbeStrategy("hdd_inspired");
    setAllowRepairBranch(false); setRepairPatch("");
    lastEvent.current = "";
    sessionStorage.removeItem(SESSION_REVIEW_KEY);
    sessionStorage.removeItem(SESSION_STAGE_KEY);
  }

  function undoClearWorkbench() {
    const snapshot = undoSnapshot.current;
    if (!snapshot) return;
    setReview(snapshot.review); setReviewBundle(snapshot.reviewBundle);
    setEvents(snapshot.events); setSelected(snapshot.selected);
    setSelectedLine(snapshot.selectedLine); setEvidenceDetail(snapshot.evidenceDetail);
    setReplayEvidence(snapshot.replayEvidence); setDiffLines(snapshot.diffLines);
    setError(snapshot.error); setOfflineReplay(snapshot.offlineReplay);
    setWorkspaceFocus(snapshot.workspaceFocus); setProductMode(snapshot.productMode);
    setPresetId(snapshot.presetId); setRepoId(snapshot.repoId); setTests(snapshot.tests);
    setGoal(snapshot.goal); setGoalPreset(snapshot.goalPreset); setBudget(snapshot.budget);
    setProvider(snapshot.provider); setProbeStrategy(snapshot.probeStrategy);
    setAllowRepairBranch(snapshot.allowRepairBranch); setRepairPatch(snapshot.repairPatch);
    manualNavigationRef.current = !snapshot.autoFollow;
    setActiveStage(snapshot.activeStage); updateAutoFollow(snapshot.autoFollow);
    setTimelineExpanded(snapshot.timelineExpanded);
    resultScrollTop.current = snapshot.resultScrollTop;
    lastEvent.current = snapshot.lastEvent;
    if (snapshot.review) sessionStorage.setItem(SESSION_REVIEW_KEY, snapshot.review.review_id);
    else sessionStorage.removeItem(SESSION_REVIEW_KEY);
    if (snapshot.review) sessionStorage.setItem(SESSION_STAGE_KEY, JSON.stringify({
      review_id: snapshot.review.review_id, stage: snapshot.activeStage,
      auto_follow: snapshot.autoFollow,
    }));
    else sessionStorage.removeItem(SESSION_STAGE_KEY);
    if (snapshot.review) rememberReviewId(snapshot.review.review_id);
    cancelUndoWindow();
  }

  function enterNextRunSetup() {
    if (review && terminal.has(review.state.status)) {
      resultScrollTop.current = window.scrollY;
      setWorkspaceFocus("next_run_setup");
    }
  }

  function showPreviousResult() {
    if (!review) return;
    setWorkspaceFocus("current_result");
  }

  function changeProvider(nextProvider: string) {
    if (configLocked) return;
    if (review && terminal.has(review.state.status) && nextProvider !== provider) {
      enterNextRunSetup();
    }
    setProvider(nextProvider);
  }

  function changePreset(nextPresetId: string) {
    if (configLocked) return;
    if (review && terminal.has(review.state.status)) enterNextRunSetup();
    setPresetId(nextPresetId);
  }

  function changeProductMode(nextMode: ProductMode) {
    if (busy || (review && !terminal.has(review.state.status))) return;
    setProductMode(nextMode);
    setProvider(nextMode === "agent"
      ? (providerInfo.live_available ? "live" : "deterministic") : "deterministic");
  }

  async function authorizeRepo() {
    if (!repoPath.trim() || configLocked) return;
    setBusy(true); setError(null);
    try {
      const added = await api<Repo>("/api/v1/repos", {
        method: "POST", body: JSON.stringify({path: repoPath.trim()}),
      });
      setRepos(old => old.some(item => item.repo_id === added.repo_id) ? old : [...old, added]);
      setRepoId(added.repo_id); setRepoPath(""); setTests("");
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function createReview(preset?: Preset) {
    setError(null); setBusy(true); setEvents([]); setDiffLines([]); setReviewBundle(null); setModelCalls([]);
    setSelected(null); setEvidenceDetail(null); setOfflineReplay(false); lastEvent.current = "";
    const requestRepo = preset?.repo_id || repoId;
    const requestTests = preset?.test_files || tests.split("\n").map(x => x.trim()).filter(Boolean);
    try {
      const next = await api<Review>("/api/v2/reviews", {
        method: "POST",
        headers: {"Idempotency-Key": requestKey("review")},
        body: JSON.stringify({
          instruction: preset?.goal || goal,
          source: {kind: "local", repo_id: requestRepo},
          scope: {
            ...(scopeInclude.length ? {include: scopeInclude} : {}),
            ...(requestTests.length ? {test_files: requestTests} : {}),
          },
          constraints: {budget_seconds: preset?.budget_seconds || budget},
          autonomy_policy: {model_provider: provider, allow_repair_branch: allowRepairBranch,
                            allow_generated_tests: false},
          review_focus: preset
            ? focusForGoalText(preset.goal, goalPreset)
            : (focusOverride || focusForGoalText(goal, goalPreset)),
          product_mode: productMode,
          probe_strategy: probeStrategy,
          language: "python",
        }),
      });
      sessionStorage.setItem(SESSION_REVIEW_KEY, next.review_id);
      rememberReviewId(next.review_id);
      cancelUndoWindow();
      resultScrollTop.current = 0;
      setWorkspaceFocus("current_result");
      manualNavigationRef.current = false;
      updateAutoFollow(true);
      setActiveStage(1);
      setReview(next);
      history.replaceState(null, "", location.pathname + location.search);
      setPreferredTaskId("");
      if (taskRequirementDraft.trim()) {
        try {
          const task = await api<EvidenceTask>("/api/v2/tasks", {
            method: "POST", headers: {"Idempotency-Key": requestKey("task")},
            body: JSON.stringify({review_id: next.review_id,
              title: preset?.goal || goal, criteria: taskRequirements(taskRequirementDraft)}),
          });
          setPreferredTaskId(task.task_id);
        } catch (taskError) {
          setError({...noticeFor(taskError), title: "审查已创建，验收要求尚未保存",
            message: "可继续审查，完成后在第 6 步重新记录要求。"});
        }
      }
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function draftFromSentence() {
    const text = nlText.trim();
    if (!text || drafting) return;
    setError(null); setDrafting(true); setDraftNotice(""); setDraftResult(null);
    setLocateResult(null); setLocateNotice("");
    try {
      const result = await api<DraftPayload>("/api/v2/reviews/draft-from-text", {
        method: "POST", body: JSON.stringify({text, product_mode: "agent"}),
      });
      setDraftResult(result);
      if (!result.generated) {
        setDraftNotice((result.failure_reason || "模型没能把这句话理解成合法草案")
          + "；可直接填写表单继续。");
      }
    } catch (e) {
      setDraftNotice(noticeFor(e).title + "；可直接填写表单继续。");
    } finally { setDrafting(false); }
  }

  function applyDraft() {
    const draft = draftResult?.draft;
    if (!draft) return;
    // 草案可能指向与之前定位不同的仓库：scope.include 一并作废。
    setScopeInclude([]);
    setGoal(draft.goal);
    setGoalPreset("custom");
    setRepoId(draft.repo_id);
    setBudget(draft.budget_seconds);
    setFocusOverride(draft.review_focus);
    // 统一 v2 后留空测试文件 = 创建时由确定性发现补齐，因此一律清空。
    setTests("");
    setDraftResult(null); setDraftNotice("");
  }

  async function locateFromSentence() {
    const text = nlText.trim();
    if (!text || locating || !repos.length) return;
    setError(null); setLocating(true); setLocateNotice(""); setLocateResult(null);
    setDraftResult(null); setDraftNotice("");
    try {
      const result = await api<LocatePayload>("/api/v2/locate-code", {
        method: "POST",
        body: JSON.stringify({text, repo_ids: repos.map(item => item.repo_id)}),
      });
      setLocateResult(result);
      if (result.status !== "matched" || !result.draft) {
        // 定位失败的退路只有一个：回到手动选仓库。绝不猜一个 repo_id。
        document.getElementById("repo")?.focus();
      }
    } catch (e) {
      setLocateNotice(noticeFor(e).title + "；请直接填写下方表单继续。");
    } finally { setLocating(false); }
  }

  function applyLocate() {
    const draft = locateResult?.draft;
    if (!draft) return;
    setGoal(draft.instruction);
    setGoalPreset("custom");
    setRepoId(draft.repo_id);
    setBudget(draft.budget_seconds);
    setFocusOverride(draft.review_focus);
    // 只有 pytest 文件能进测试文件框；其余候选走 scope.include（修复实验
    // 的路径白名单，仅 v2 有这个字段），v1 保留原有的人工测试范围。
    const testPaths = draft.paths.filter(isTestishPath);
    if (testPaths.length) {
      setTests(testPaths.join("\n"));
    } else if (productMode === "agent") {
      setTests(""); // v2 留空 = 创建时由确定性发现补齐
    } else {
      const suggestion = presets.find(item => item.repo_id === draft.repo_id
        && item.model_provider === "deterministic") ||
        presets.find(item => item.repo_id === draft.repo_id);
      if (suggestion) setTests(suggestion.test_files.join("\n"));
    }
    setScopeInclude(productMode === "agent" ? draft.paths : []);
    setLocateResult(null); setLocateNotice("");
  }

  function repoDisplayName(repoId: string): string {
    return repos.find(item => item.repo_id === repoId)?.display_name || repoId;
  }

  function selectCustomRepo(nextRepoId: string) {
    // 人工换仓库意味着放弃定位带入的候选路径。
    setScopeInclude([]);
    setRepoId(nextRepoId);
    const suggestion = presets.find(item => item.repo_id === nextRepoId
      && item.model_provider === "deterministic") ||
      presets.find(item => item.repo_id === nextRepoId);
    if (!suggestion) {
      setTests("");
      return;
    }
    setTests(suggestion.test_files.join("\n"));
    setGoal(suggestion.goal);
    setGoalPreset(GOAL_PRESETS.find(item => item.goal === suggestion.goal)?.id || "custom");
    setBudget(suggestion.budget_seconds);
    setProvider(productMode === "agent" ? "live" : "deterministic");
  }

  // 计划修订：把「我想改一下」变成一份新计划。
  //
  // **返回的是一个新 review**，不是被改过的同一个。旧稿在服务端被置为
  // ABORTED/plan_superseded。所以这里必须做和 createReview 一样的全套重置——
  // 尤其是 lastEvent.current：它留着上一稿的 event_id，会被当作 Last-Event-ID
  // 发给新审查的事件流，服务端从一个根本不属于它的位点接着发，界面就静默地
  // 少掉一段事件。事件流本身靠 review_id 变化自动重挂，不用手动碰。
  // 只有 v2 的草稿能改，而且只在它还停在批准边界上的时候。这两条都是后端
  // revise_v2 的前置（否则回 PLAN_REVISION_NOT_ALLOWED）——界面不该给出一个
  // 注定被拒的入口。
  const canRevise = Boolean(review)
    && review?.state.status === "AWAITING_APPROVAL"
    && review?.request?.schema_version === "review-request-v2";

  async function loadPlanRevisions(reviewId: string) {
    try {
      setPlanRevisions(await api<Record<string, unknown>>(
        `/api/v2/reviews/${reviewId}/plan-revisions`));
    } catch { /* v1 审查没有这个端点，没有父链不是错误 */ }
  }

  async function submitRevision() {
    if (!review || !reviseReason.trim()) return;
    setReviseBusy(true); setError(null);
    try {
      const patch: Record<string, unknown> = {revision_reason: reviseReason.trim()};
      if (reviseInstruction.trim()) patch.instruction = reviseInstruction.trim();
      const next = await api<Review>(
        `/api/v2/reviews/${review.review_id}/messages`,
        {method: "POST", headers: {"Idempotency-Key": requestKey("revision")},
         body: JSON.stringify(patch)});
      setEvents([]); setDiffLines([]); setReviewBundle(null); setModelCalls([]);
      setSelected(null); setEvidenceDetail(null); setDispositions([]);
      lastEvent.current = "";
      sessionStorage.setItem(SESSION_REVIEW_KEY, next.review_id);
      rememberReviewId(next.review_id);
      setReview(next);
      setReviseOpen(false); setReviseInstruction(""); setReviseReason("");
      await loadPlanRevisions(next.review_id);
    } catch (e) { setError(noticeFor(e)); }
    finally { setReviseBusy(false); }
  }

  // 人工处置：把 needs_human 这类升级事件的回答记进账本。
  // **这是审计通道，不是控制通道**——后端 record_disposition 明确不改变审查状态，
  // 所以这里也不碰 review，只刷新处置列表。
  async function loadDispositions(reviewId: string) {
    try {
      const answered = await api<{dispositions?: Disposition[]}>(
        `/api/v2/reviews/${reviewId}/dispositions`);
      setDispositions(answered.dispositions || []);
    } catch { /* 没有处置端点或还没有记录都不是错误，静默即可 */ }
  }

  async function recordDisposition(event: EventRecord) {
    if (!review || !dispositionAnswer || !dispositionHandler.trim()) return;
    setDispositionBusy(true); setError(null);
    try {
      // 入参恰好这几个键：后端多一个键就整条拒。note 为空时不发，而不是发空串。
      const payload: Record<string, string> = {
        event_id: event.event_id, answer: dispositionAnswer,
        handled_by: dispositionHandler.trim(),
      };
      if (dispositionNote.trim()) payload.note = dispositionNote.trim();
      await api<Disposition>(`/api/v2/reviews/${review.review_id}/dispositions`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("disposition")},
        body: JSON.stringify(payload),
      });
      setDispositionAnswer(""); setDispositionHandler(""); setDispositionNote("");
      await loadDispositions(review.review_id);
    } catch (e) { setError(noticeFor(e)); }
    finally { setDispositionBusy(false); }
  }

  async function approve() {
    if (!review?.plan?.plan_sha256) return;
    setBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      const nextReview = await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/approval`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("approval")},
        body: JSON.stringify({plan_sha256: review.plan.plan_sha256}),
      });
      setReview(nextReview);
      // 确认后立即进入「验证测试保护」，无需再点旅程栏或跟随开关。
      goToStage(2, false);
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function answerDecision(decision: "continue" | "stop") {
    if (!review || !pendingDecision) return;
    const data = (pendingDecision.data || {}) as {decision_id?: string;
      plan_sha256?: string};
    if (!data.decision_id || !data.plan_sha256) return;
    setDecisionBusy(true);
    try {
      setReview(await api<Review>(
        `/api/v2/reviews/${review.review_id}/decisions/${data.decision_id}`,
        {method: "POST", headers: {"Idempotency-Key": requestKey("decision")},
          body: JSON.stringify({decision, plan_sha256: data.plan_sha256})}));
      setError(null);
    } catch (e) { setError(noticeFor(e)); }
    finally { setDecisionBusy(false); }
  }

  async function cancelReview() {
    if (!review || terminal.has(review.state.status)) return;
    setBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      setReview(await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/cancel`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("cancel")},
        body: "{}",
      }));
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function resumeReview() {
    if (!review || review.state.status !== "ABORTED"
        || !String(review.state.reason || "").includes("process_restart")) return;
    setBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      const next = await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/resume`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("resume")},
        body: "{}",
      });
      sessionStorage.setItem(SESSION_REVIEW_KEY, next.review_id);
      rememberReviewId(next.review_id);
      setReview(next); setReviewBundle(null); setEvents([]); setDiffLines([]);
      setModelCalls([]); lastEvent.current = ""; setWorkspaceFocus("current_result");
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function refreshRepairStatus() {
    if (!review) return;
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      const status = await api<RepairStatus>(`/api/${apiVersion}/reviews/${review.review_id}/repair`);
      setReview(old => old ? {...old, repair: status} : old);
      setError(null);
    } catch (e) { setError(noticeFor(e)); }
  }

  async function refreshTestProposal() {
    if (!review) return;
    try {
      const status = await api<TestProposalStatus>(
        `/api/v2/reviews/${review.review_id}/test-proposal`);
      setTestProposal(status);
      setError(null);
    } catch (e) { setError(noticeFor(e)); }
  }

  // #9/#10 只读取编辑会话入账；失败时在面板里明说，不静默、不假装没有。
  async function refreshEditSessions() {
    if (!review || offlineReplay
        || review.request?.schema_version !== "review-request-v2") return;
    try {
      const ledger = await api<EditSessionLedger>(
        `/api/v2/reviews/${review.review_id}/edit-sessions`);
      setEditSessions(ledger);
      setEditSessionsNotice("");
    } catch (e) {
      const notice = noticeFor(e);
      setEditSessions(null);
      setEditSessionsNotice(notice.title + (notice.message ? "；" + notice.message : ""));
    }
  }

  // M2 第二步：五指纹批准。后端会当场重算五份指纹并冻结成比对基线；
  // 界面这边要守住口径：批准不是信任，导出前还会全部重算。
  async function approveDelivery(sessionId: string) {
    if (!review || approvalBusy || !sessionId) return;
    setApprovalBusy(true); setApprovalNotice("");
    try {
      await api<{status: string}>(
        `/api/v2/reviews/${review.review_id}/edit-sessions/${sessionId}/delivery-approval`,
        {method: "POST", body: JSON.stringify({note: approvalNote.trim()})},
      );
      setApprovalNote("");
      await refreshEditSessions();
    } catch (e) {
      const notice = noticeFor(e);
      const detail = notice.title + (notice.message ? `；${notice.message}` : "");
      setApprovalNotice(detail + (notice.code ? `（错误码 ${notice.code}）` : ""));
    } finally { setApprovalBusy(false); }
  }

  // M2 第四步：导出交付清单。请求体必须是空对象；五指纹由服务端
  // 重算后与批准基线比对，任何一份对不上都会拒绝，界面不做豁免。
  async function exportDelivery(sessionId: string) {
    if (!review || exportBusyId || !sessionId) return;
    setExportBusyId(sessionId); setExportNotice("");
    try {
      await api<{status?: string; package_dir?: string}>(
        `/api/v2/reviews/${review.review_id}/edit-sessions/${sessionId}/delivery-export`,
        {method: "POST", body: JSON.stringify({})},
      );
      await refreshEditSessions();
    } catch (e) {
      const notice = noticeFor(e);
      const detail = notice.title + (notice.message ? `；${notice.message}` : "");
      setExportNotice(detail + (notice.code ? `（错误码 ${notice.code}）` : ""));
    } finally { setExportBusyId(""); }
  }

  // M1 第三步：开启编辑会话。服务端会冻结此刻的源快照当比对锚；
  // 之后源码一变，这个会话按 stale 失败关闭，不会拿旧补丁盖新树。
  async function openEditSession() {
    if (!review || sessionBusy) return;
    setSessionBusy(true); setSessionNotice("");
    try {
      await api<{session_id?: string}>(
        `/api/v2/reviews/${review.review_id}/edit-sessions`, {
          method: "POST",
          headers: {"Idempotency-Key": requestKey("edit-session")},
          body: JSON.stringify({intent: sessionIntent.trim()}),
        });
      setSessionIntent("");
      await refreshEditSessions();
    } catch (e) {
      const notice = noticeFor(e);
      setSessionNotice(notice.title + (notice.message ? `；${notice.message}` : "")
        + (notice.code ? `（错误码 ${notice.code}）` : ""));
    } finally { setSessionBusy(false); }
  }

  // 显式放弃一个活动会话；终态会话后端不会再放行迁移。
  async function abandonEditSession(sessionId: string) {
    if (!review || !sessionId || abandonBusyId) return;
    setAbandonBusyId(sessionId);
    try {
      await api<unknown>(
        `/api/v2/reviews/${review.review_id}/edit-sessions/${sessionId}/abandon`,
        {method: "POST", body: JSON.stringify({})});
      await refreshEditSessions();
    } catch (e) {
      const notice = noticeFor(e);
      setSessionNotice(notice.title + (notice.message ? `；${notice.message}` : "")
        + (notice.code ? `（错误码 ${notice.code}）` : ""));
    } finally { setAbandonBusyId(""); }
  }

  // 生成候选补丁：只交出发现编号和选中片段，拿回的是入账记录；
  // 正文留在服务端，交付仍然要人把认可的补丁贴进交付框再确认。
  async function generateRepairCandidate() {
    if (!review || candidateBusy) return;
    const findingIds = candidateFindingIds.split(/[\n,，、]/)
      .map(item => item.trim()).filter(Boolean);
    const snippets = snippetDrafts
      .filter(draft => draft.path.trim() || draft.start.trim() || draft.text.trim())
      .map(draft => ({path: draft.path.trim(),
        start_line: Number(draft.start.trim()), text: draft.text}));
    const complete = snippets.length > 0 && snippets.every(snippet =>
      snippet.path && Number.isInteger(snippet.start_line) && snippet.start_line >= 1
      && snippet.text.trim());
    if (findingIds.length === 0 || !complete || candidateSnippetBytes > 12 * 1024) {
      setCandidateNotice("先补全再发起：至少一个发现编号，且每段片段都有路径、"
        + "正整数起始行和非空内容，合计不超过 12 KiB。");
      return;
    }
    setCandidateBusy(true); setCandidateNotice("");
    try {
      const record = await api<RepairCandidateRecord>(
        `/api/v2/reviews/${review.review_id}/repair/candidate`, {
          method: "POST",
          headers: {"Idempotency-Key": requestKey("repair-candidate")},
          body: JSON.stringify({finding_ids: findingIds, snippets}),
        });
      setRepairCandidate(record);
    } catch (e) {
      const notice = noticeFor(e);
      setCandidateNotice(notice.title + (notice.message ? `；${notice.message}` : "")
        + (notice.code ? `（错误码 ${notice.code}）` : ""));
    } finally { setCandidateBusy(false); }
  }

  async function proposeTestForGap(ref: string) {
    if (!review || proposalBusy) return;
    setProposalBusy(true); setProposalNotice(""); setError(null);
    try {
      const record = await api<TestProposalRecord>(
        `/api/v2/reviews/${review.review_id}/test-proposal`, {
          method: "POST",
          headers: {"Idempotency-Key": requestKey("test-proposal")},
          body: JSON.stringify({finding_ids: [ref]}),
        });
      setTestProposal({status: record.status, proposal: record});
    } catch (e) {
      const notice = noticeFor(e);
      setProposalNotice(notice.title + (notice.message ? "；" + notice.message : ""));
    } finally { setProposalBusy(false); }
  }

  async function deliverRepair() {
    if (!review || !repairPatch.trim() || review.state.status === "ABORTED") return;
    setRepairBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      // 绑定会话的交付会在服务端重比会话冻结的源快照；未绑定时
      // 仍按整仓快照核对，两条路都不放行旧补丁。
      const binding = apiVersion === "v2" && repairSessionId
        ? {session_id: repairSessionId} : {};
      const next = await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/repair`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("repair")},
        body: JSON.stringify({patch: repairPatch, ...binding}),
      });
      setReview(next); setRepairPatch(""); setRepairSessionId("");
      const bundle = await api<ReviewBundle>(`/api/${apiVersion}/reviews/${review.review_id}/bundle`);
      setReviewBundle(bundle); setEvents(bundle.events || events);
      setDiffLines(bundle.evidence_bundle?.report?.render_model?.lines || diffLines);
      setError(null);
      await refreshEditSessions();
    } catch (e) { setError(noticeFor(e)); }
    finally { setRepairBusy(false); }
  }

  function downloadRepairEvidence() {
    if (!review?.repair || review.repair.status !== "DELIVERED") return;
    const payload = JSON.stringify({
      review_id: review.review_id,
      evidence_manifest: review.repair.evidence_manifest || null,
      delivery_record: review.repair.delivery_record || null,
    }, null, 2);
    const url = URL.createObjectURL(new Blob([payload], {type: "application/json"}));
    const a = document.createElement("a");
    a.href = url; a.download = `shuimu-yanma-repair-${review.review_id}.json`; a.click();
    URL.revokeObjectURL(url);
  }

  function loadReplay(file: File) {
    file.text().then(text => {
      const bundle = JSON.parse(text) as ReviewBundle;
      const rows = bundle.evidence_bundle?.ledger || [];
      setReplayEvidence(Object.fromEntries(rows.map(row => [String(row.record_id), row])));
      setReviewBundle(bundle);
      setDiffLines(bundle.evidence_bundle?.report?.render_model?.lines || []);
      setEvents(bundle.events || []);
      setReview({review_id: bundle.review_id, state: {status: "COMPLETE", evidence_status: "COMPLETE"},
        plan: bundle.plan, repair: bundle.repair, last_seq: (bundle.events || []).length});
      resultScrollTop.current = 0;
      setWorkspaceFocus("current_result");
      // 用户明确导入的是可复验的完整结果：落到 04 结论页，
      // 关闭自动跟随；工作台仍可从阶段导航进入。
      manualNavigationRef.current = true;
      setActiveStage(3); updateAutoFollow(false);
      setError(null); setSelected(null); setEvidenceDetail(null); setOfflineReplay(true);
    }).catch(e => setError(classifyError(400, "REPLAY_INVALID", `回放文件无效：${e.message}`)));
  }

  async function inspectEvent(event: EventRecord) {
    manualNavigationRef.current = true;
    updateAutoFollow(false);
    setSelected(event); setSelectedLine(null); setEvidenceDetail(null);
    setDispositionAnswer(""); setDispositionHandler(""); setDispositionNote("");
    if (review && dispositionOptions(event.kind).length) void loadDispositions(review.review_id);
    const id = String(event.data.claim_id || event.data.evidence_id || "");
    if (!id) return;
    const bundled = replayEvidence[id] || reviewBundle?.evidence_bundle?.ledger?.find(row => row.record_id === id);
    if (bundled) { setEvidenceDetail(bundled); return; }
    if (!review) return;
    try {
      setEvidenceDetail(await api<Record<string, unknown>>(
        `/api/v1/reviews/${review.review_id}/evidence/${id}`));
    } catch (e) { setError(noticeFor(e)); }
  }

  async function inspectLine(line: DiffLine) {
    manualNavigationRef.current = true;
    updateAutoFollow(false);
    // 时间线可点、结论不可点，那个交互是反的：用户看到的是结论，
    // 想追的也是结论。这里让每一行直接指回它的账本记录。
    setSelectedLine(line); setSelected(null); setEvidenceDetail(null);
    // 同步记录工作台焦点：工作台开着就立即定位到这行；还没开就
    // 作为下一次打开时的初始定位，保证"结果 → 代码"方向不断链。
    setWorkbenchFocus(f => ({path: line.file, line: line.line,
      nonce: (f?.nonce ?? 0) + 1}));
    const id = decisiveEvidenceId(line, reviewBundle);
    if (!id) return;
    const bundled = replayEvidence[id]
      || reviewBundle?.evidence_bundle?.ledger?.find(row => row.record_id === id);
    if (bundled) { setEvidenceDetail(bundled); return; }
    if (!review) return;
    try {
      setEvidenceDetail(await api<Record<string, unknown>>(
        `/api/v1/reviews/${review.review_id}/evidence/${id}`));
    } catch (e) { setError(noticeFor(e)); }
  }

  async function downloadBundle() {
    if (!review) return;
    try {
      const response = await fetch(`/api/v1/reviews/${review.review_id}/bundle`, {
        headers: {Authorization: `Bearer ${sessionToken}`},
      });
      if (!response.ok) throw new HttpError(response.status, "DOWNLOAD_FAILED", `下载失败：${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const a = document.createElement("a");
      a.href = url; a.download = `shuimu-yanma-review-${review.review_id}.json`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) { setError(noticeFor(e)); }
  }

  // #44 可分享的结论摘要：只导出证据护照上已有的字段，一键复制。剪贴板不可写时
  // 不假装成功——把同一段文本摊在只读框里，让人手动复制。
  async function copyPassportMarkdown() {
    const text = passportMarkdown(passport);
    setSummaryFallback("");
    try {
      await navigator.clipboard.writeText(text);
      setSummaryNotice("已复制证据护照的 Markdown 摘要。");
    } catch {
      setSummaryFallback(text);
      setSummaryNotice("这个浏览器不允许写入剪贴板：下面是同一段文本，请手动复制。");
    }
  }

  // #44 独立教程页：整页替换主界面，返回时原样回到控制台。
  if (tutorialOpen) return <TutorialPage onBack={() => {
    // 返回控制台时清掉路由：replaceState 不触发 hashchange，状态手动同步。
    history.replaceState(null, "", location.pathname + location.search);
    setTutorialOpen(false);
  }}/>;

  return <main>
    <header>
      {/* 页头只留一枚字标为主的小标识：首屏下方 132px 的 .agent-core 才是
          标识的主场，页头再挂 44px 的大图标就是两枚抢注意力。 */}
      <BrandLockup />
      {/* 页头拆两层：上层是你可以动的（主题、模式、清空），下层是这一次
          运行的既成事实（三枚运行标签 + 审查 id，全部只读）。运行标签原来
          挤在控制按钮旁边，看起来像第四个开关，但它不响应任何点击——
          搬进事实层，把它"只是记录"的身份说清楚。 */}
      <div className="header-layers">
        <div className="header-controls" aria-label="任务控制">
          <button className="theme-toggle"
            onClick={() => setTheme(theme === "beige" ? "dark" : "beige")}
            title={theme === "beige" ? "切换到深色界面" : "切换到浅色界面"}
            aria-label={theme === "beige" ? "切换到深色界面" : "切换到浅色界面"}>
            {theme === "beige" ? "深色" : "浅色"}
          </button>
          <button type="button" className="theme-toggle"
            onClick={() => { location.hash = TUTORIAL_ROUTE_HASH; }}
            title="五分钟看懂这一页怎么用（独立教程页）"
            aria-label="打开新手教程">教程</button>
          <div className="view-switch" aria-label="产品模式">
            <button className={productMode === "standard" ? "active" : ""}
              onClick={() => changeProductMode("standard")}
              disabled={modeSwitchLocked}
              title="零模型调用：三态结论只来自真实测试与恢复实验">标准审查</button>
            <button className={productMode === "agent" ? "active" : ""}
              onClick={() => changeProductMode("agent")}
              disabled={modeSwitchLocked}
              title={providerInfo.live_available ? "受限模型参与：调度与建议可由模型辅助，透明记录全程可查"
                : "启动时接入模型 API 后才能创建；当前仅可查看说明"}>智能体审查</button>
            {review && !runInProgress && <small className="mode-switch-note">仅影响下一次审查</small>}
            {/* 锁着的按钮必须自己解释为什么点不动，不能只有变灰。 */}
            {modeSwitchLocked && <small className="mode-switch-note">
              {runInProgress ? "审查运行中 · 结束后可切换" : "操作进行中 · 稍后可切换"}</small>}
          </div>
          {/* #45 历史审查入口：后端没有列出审查的接口，这里只列本标签页
              里真正出现过的审查 id；列表为空时整个入口不出现。 */}
          {reviewHistory.length > 0 && <details className="history-menu" ref={historyMenuRef}>
            <summary title="只查看历史结果，不会重新运行代码或调用模型"
              aria-label={`审查历史，本会话 ${reviewHistory.length} 条记录`}>审查历史</summary>
            <div className="history-list">
              {reviewHistory.map(id => <button key={id} type="button"
                disabled={busy} onClick={() => void openHistoryReview(id)}>{id}</button>)}
              <small>只查看历史结果，不会重新运行代码或调用模型</small>
            </div>
          </details>}
          {review && <div className="workbench-actions">
            <button className="clear-workbench" disabled={!canClearWorkbench}
              title={canClearWorkbench ? undefined
                : "审查尚未结束，不能清空正在运行的工作台"}
              onClick={clearWorkbench}>清空工作台</button>
            <small>只清空当前页面，不删除审查记录或证据包</small>
          </div>}
        </div>
        <div className="run-facts" aria-label="本次运行">
          {(() => {
            // F4：三枚运行事实合成一枚可展开的条目。summary 永远完整带出
            // 三条事实（合并前后信息量不减），展开只补充各条的含义标签。
            const factRun = presentation.showPreviousResultNotice ? "下一次尚未运行" : runMode;
            const factSchedule = presentation.showPreviousResultNotice ? "正在准备下一次运行"
              : review || offlineReplay ? `当前结果：${displaySchedulingMode}` : setupLabels.scheduler;
            const factIsolation = presentation.showPreviousResultNotice ? setupLabels.isolation : executionMode;
            return <details className="mode-strip" aria-label="运行边界">
              <summary>{factRun} · {factSchedule} · {factIsolation}</summary>
              <div className="run-facts-detail">
                <small><b>运行方式</b>{factRun}</small>
                <small><b>调度</b>{factSchedule}</small>
                <small><b>隔离</b>{factIsolation}</small>
              </div>
            </details>;
          })()}
          {review && <span className="run-review-id">审查 {review.review_id}</span>}
          {(() => {
            const limits = unenforcedLimitsView(
              reviewBundle?.evaluation_context?.resource_policy);
            if (!limits.length) return null;
            return <details className="unenforced-limits">
              <summary>执行限制有 {limits.length} 项未强制</summary>
              {limits.map(item => <small key={item}>{item}</small>)}
              <small>这些上限在当前隔离方式下只是记录，没有被强制执行。</small>
            </details>;
          })()}
        </div>
      </div>
    </header>

    {/* 6.3 常驻六阶段导航：每一项都可直接进入，读屏软件始终读得到
        完整编号、名称和状态；窄屏由 CSS 改成页头后的粘性栏。 */}
    <nav className="journey-nav" aria-label="审查阶段导航">
      <span className="journey-nav-title">审查旅程</span>
      {JOURNEY_STAGES.map((rail, index) => <button key={rail.key} type="button"
        className={`stage-dot stage-${stageStatuses[index]}${stageStatuses[index] === "ready" ? " stage-active" : ""}${activeStage === index ? " is-current" : ""}`}
        aria-current={activeStage === index ? "step" : undefined}
        onClick={() => goToStage(index)}>
        <i aria-hidden="true" />
        <span className="stage-label">{String(index + 1).padStart(2, "0")} {rail.label}
          <b> · {STAGE_STATE_LABELS[stageStatuses[index]]}</b></span>
      </button>)}
      <div className={`stage-follow ${autoFollow ? "is-on" : "is-paused"}`}
        role="status" aria-label={autoFollow ? "自动跟随已开启" : "正在手动查看，自动跟随已暂停"}
        title={autoFollow
          ? "确认计划和主要审查状态变化时，界面会自动进入对应步骤"
          : "新审查创建或确认计划后会自动恢复跟随"}>
        {autoFollow ? "自动跟随已开启" : "正在手动查看"}
      </div>
    </nav>

    {presentation.showPreviousResultNotice && <section className="focus-strip" role="status">
      <div><strong>正在准备下一次运行</strong><span>上一轮结果已收起</span></div>
      <button onClick={showPreviousResult}>查看上一轮结果</button>
    </section>}
    {undoVisible && <div className="undo-toast" role="status" aria-live="polite">
      <span>工作台已清空，审查记录仍保留</span>
      <button onClick={undoClearWorkbench}>撤销</button>
    </div>}

    <section className={`hero ${review ? "compact" : ""}${activeStage === 4
      && (offlineReplay || (review && terminal.has(review.state.status)))
        ? " evidence-focus" : ""}`}>
      <div className="hero-copy"><p className="eyebrow">EVIDENCE-GATED REVIEW AGENT</p>
        <h2>{review ? <em>让实验为结论签字。</em>
          : <>让智能体追问每一行，<em>让实验<wbr />为结论签字。</em></>}</h2>
        <p className="hero-context">水木验码是一个不会自行签字的证据审查智能体：它理解意图、编排候选、提出质疑，但只有可逆实验能确认事实。</p>
        <p className="hero-description"><strong>AI 负责提议，实验负责签字，人负责批准边界。</strong>承重结论都能回到具名测试、恢复校验与封存证据。</p>
        {boundaryAgent && <div className="agent-role-map" aria-label="审查智能体职责边界">
          <span><b>01</b><strong>智能体</strong><small>理解 · 编排 · 追问</small></span>
          <i aria-hidden="true">→</i>
          <span><b>02</b><strong>证据引擎</strong><small>干预 · 观测 · 恢复</small></span>
          <i aria-hidden="true">→</i>
          <span><b>03</b><strong>人</strong><small>批准 · 质疑 · 决定</small></span>
        </div>}
        <div className="experiment-loop" aria-label="可逆实验步骤">
          <span>拿走代码</span><i>→</i><span>测试报警</span><i>→</i><span>恢复代码</span>
        </div>
      </div>
      <div className="hero-aside">
        <div className="agent-core" aria-label={boundaryAgent
          ? "智能体审查：受限模型参与，实验签发结论"
          : "标准审查：零模型调用，实验签发结论"}>
          <span className="agent-core-label">{boundaryAgent ? "EVIDENCE AGENT" : "STANDARD REVIEW"}</span>
          <BrandMark className="hero-mark" size={132} />
          <span className="agent-orbit orbit-propose">{boundaryAgent ? "提议" : "零模型"}</span>
          <span className="agent-orbit orbit-prove">实验</span>
          <span className="agent-orbit orbit-approve">{boundaryAgent ? "人工门" : "确定性"}</span>
        </div>
        {/* 还没开跑时，「尚未开始 · 第 0 步 / 共 8 步」配一条空进度条占了首屏一大块，
            却一个字都没告诉人。这个位置换成「这次实验会做什么」，等真的开跑再让位给进度。 */}
        {review && presentation.showCurrentResult ? <div className={`status-card tone-${currentStatus.tone}`}>
          <span>当前页面</span><strong>{String(activeStage + 1).padStart(2, "0")}／06 · {JOURNEY_STAGES[activeStage].label}</strong>
          <details className="internal-progress"><summary>内部执行进度</summary>
            <small>{currentStatus.label} · Evidence {review.state.evidence_status || "尚未启动"}</small>
            <div className="progress"><i style={{width: `${currentStatus.step / currentStatus.total * 100}%`}} /></div>
          </details>
          {review && !terminal.has(review.state.status) && !["CANCELLING", "RECOVERING"].includes(review.state.status) && <button className="status-action cancel-action"
            disabled={busy} onClick={() => void cancelReview()}>安全中止本次审查</button>}
          {review?.state.status === "ABORTED" && String(review.state.reason || "").includes("process_restart") &&
            <button className="status-action resume-action" disabled={busy}
              onClick={() => void resumeReview()}>从重启中止记录恢复</button>}
        </div> : review ? <div className="next-run-card">
          <span>下一次运行</span><strong>{provider === "live" ? setupModelLabel + " 受限调度" : "确定性调度"}</strong>
          <small>上一轮真实结果已收起；修改完成后可生成新的审查计划。</small>
          {review.state.status === "ABORTED" && String(review.state.reason || "").includes("process_restart") &&
            <button className="status-action resume-action" disabled={busy}
              onClick={() => void resumeReview()}>从重启中止记录恢复</button>}
        </div> : <div className="intro-card">
          <span>这次实验会做什么</span>
          <ol>
            <li>冻结候选，确认测试范围与预算，等你按下确认</li>
            <li>跑通基线，再把新增代码临时拿走一段</li>
            <li>看哪条具名测试从通过变失败，随后原样恢复并校验</li>
          </ol>
          <small>这次实验本身不提交；只有你显式批准的修复交付才会创建本地分支并提交。</small>
        </div>}
        {releaseStatus && (() => {
          const view = releaseStatusView(releaseStatus as unknown as Record<string, unknown>);
          const tone = view.state === "machine_failed" || view.state === "receipt_expired"
            ? "failed"
            : view.state === "machine_passed_external_pending" ? "passed" : "unloaded";
          const uiProbe = uiProbeStatusView(releaseStatus.ui_probe_status);
          const narrowChecks = narrowPackageChecksView(releaseStatus.narrow_package_checks);
          const gateDetails = Object.entries(releaseStatus.pending_gate_details || {});
          return <div className={`release-status-card ${tone}`} role="status" aria-label="发布状态">
            <span>内部候选状态</span><strong>{view.headline}</strong>
            <small>{view.detail}</small>
            {view.state === "machine_failed" && view.machineFailureReasons.length === 0 &&
              <small>下一步：重新读取发布检查记录，确认机器失败原因。</small>}
            {gateDetails.length > 0 && <details className="release-gate-reasons">
              <summary>待完成门禁的原因与恢复条件</summary>
              {gateDetails.map(([gate, detail]) => <small key={gate}>
                {releaseGateLabel(gate)}：{detail.reason || detail.status || "未提供原因"}
                {detail.resume_condition ? `；恢复条件：${detail.resume_condition}` : ""}
              </small>)}
            </details>}
            <details className="release-tech"><summary>查看技术依据（技术详情）</summary>
              <small>machine_status: {view.rawMachineStatus || "—"}</small>
              <small>release_status: {releaseStatus.release_status} · stable_eligible: false</small>
              {view.machineFailureReasons.length > 0 &&
                <small>machine_failure_reasons: {view.machineFailureReasons.join("、")}</small>}
              {view.pendingGates.length > 0 &&
                <small>待完成门禁：{view.pendingGates.map(gate => releaseGateLabel(gate)).join("、")}</small>}
              {uiProbe && <small>界面探针：{uiProbe}</small>}
              {narrowChecks.length > 0 && <small>
                窄口径包自检：{narrowChecks.map(item => `${item.label}${item.state}`).join("、")}</small>}
              {releaseStatus.generated_at && <small>generated_at: {releaseStatus.generated_at}</small>}
              {releaseStatus.source_commit && <small>source_commit: {releaseStatus.source_commit}</small>}
              {view.state === "receipt_expired" && releaseStatus.serving_commit &&
                <small>serving_commit: {releaseStatus.serving_commit}</small>}
            </details>
          </div>;
        })()}
        <div className={`standing-auth-card ${standingAuth.loaded ? (standingAuth.expired ? "expired" : "loaded") : "unloaded"}`}
          role="status" aria-label="常驻授权状态">
          <span>常驻授权</span>
          {standingAuth.loaded
            ? <>
              <strong>{standingAuth.expired ? "已过期" : "已加载"}</strong>
              <small>{standingAuth.expired
                ? "授权已过有效期，L3 无人值守不可用，等待重新签发。"
                : `授权人 ${standingAuth.authorizedBy || "—"} · ${standingAuth.repos.length} 个仓库 · 有效期至 ${standingAuth.validUntil || "—"}`}</small>
              <details className="release-tech"><summary>查看授权细节（技术详情）</summary>
                <small>source_sha256（前 12 位）：{standingAuth.sourcePrefix || "—"}</small>
                <small>仓库白名单：{standingAuth.repos.length
                  ? standingAuth.repos.join("、") : "—"}</small>
                <small>单次写入预算上限：{standingAuth.budgetSecondsMax} 秒</small>
                <small>每审查最多 {standingAuth.maxWritesPerReview} 次写入 · 每小时最多 {standingAuth.maxWritesPerHour} 次</small>
              </details>
            </>
            : <>
              <strong>未加载</strong>
              <small>未加载常驻授权，L3 无人值守不可用。</small>
            </>}
        </div>
      </div>
    </section>

    {error && <section className={`notice notice-${error.level}`}>
      {/* 默认只给中文原因和下一步动作。异常码和 HTTP 状态回答的是
          "服务端怎么说的"，不回答"我现在该做什么"，所以收进技术详情。 */}
      <div><h3>{error.title}</h3><p>{error.message}</p>
        {(error.code || error.status) && <details className="error-tech">
          <summary>技术详情</summary>
          {error.code && <small>异常码：{error.code}</small>}
          {error.status ? <small>HTTP 状态：{error.status}</small> : null}
        </details>}
      </div>
      {error.recovery === "token" && <div className="token-recovery">
        <label htmlFor="manual-token">粘贴完整启动地址或本次 token</label>
        <div><input id="manual-token" type="password" value={tokenInput}
          onChange={e => setTokenInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") void applyManualToken(); }}
          placeholder="http://127.0.0.1:8765/#token=…" />
          <button onClick={applyManualToken}>重新连接</button></div>
      </div>}
      {error.recovery === "reload-plan" && <button onClick={reloadPlan}>载入最新计划</button>}
      {error.recovery === "retry" && <button onClick={loadConfiguration}>重新连接服务</button>}
    </section>}

    {activeStage === 3 && summary && presentation.showCurrentResult && <section className="result-overview" aria-label="结果概览">
      <BoundaryLine agent={boundaryAgent}/>
      <div className="result-counts">{verdictCounts(summary).map(item => {
        const loadLine = item.key === "load" ? diffLines.find(line => line.label === "承重") : undefined;
        return <button key={item.key} className={`metric metric-${item.key}`}
          disabled={!loadLine} onClick={() => loadLine && inspectLine(loadLine)}
          title={loadLine ? "打开一条承重结论的证据" : item.label}>
          <span>{item.label}</span><strong>{item.value}</strong>
        </button>;
      })}</div>
      {/* 「为什么相信它」单屏：三栏回答同一个问题——这个结论凭什么可信。
          左：结论本身；中：实验因果链（点任何一帧都能落到原始证据）；
          右：实验没有回答、要由人决定是否接受的问题。三栏数据全是
          既有字段（thesis / experimentStory / 口径声明），没有新造口径。 */}
      <div className="trust-grid" aria-label="为什么相信它">
        <div className="trust-col trust-thesis">
          <h3>结论</h3>
          <p className="result-thesis">水木验码临时拿掉新增代码，观察哪个具名测试失败，再把代码恢复；结论来自可复验实验，不是模型直接猜测。</p>
        </div>
        <div className="trust-col trust-story">
          <h3>实验因果链</h3>
          <div className="experiment-story">{experimentStory(events, report?.certificates || []).map((stage, index) =>
            <button key={stage.key} type="button" className={`story-stage story-${stage.key} story-${stage.state}`}
              disabled={!storyEvent(stage.key)} onClick={() => {
                const event = storyEvent(stage.key);
                if (event) void inspectEvent(event);
              }} title={storyEvent(stage.key) ? "打开对应证据事件" : "本帧尚无对应事件"}>
              <span>{String(index + 1).padStart(2, "0")}</span><strong>{stage.label}</strong><small>{stage.detail}</small>
            </button>)}</div>
        </div>
        <div className="trust-col trust-open">
          <h3>未回答的问题与人工决定</h3>
          <p className="trust-open-note">实验没有回答下面这些；是否采信结论，由人决定。</p>
          <ul className="trust-uncovered">{uncovered.map((item, i) => <li key={i}>{item}</li>)}</ul>
        </div>
      </div>
      {minimizationViews(summary && reviewBundle
        ? (reviewBundle.evidence_bundle as Record<string, unknown> | undefined)?.report as
          Record<string, unknown> | undefined
        : null).length > 0 &&
        <div className="minimization" aria-label="最小回归触发集合">
          {minimizationViews(((reviewBundle?.evidence_bundle as Record<string, unknown>)
            ?.report) as Record<string, unknown>).map(view =>
            <div key={view.anchorId} className="minimization-row">
              <strong>{view.anchorId}</strong>
              {view.incompleteReason
                ? <span className="minimization-none">{view.notApplicable
                    ? `最小化不适用：${view.incompleteReason}（未消耗实验）`
                    : `已尝试但未取得证书（${view.incompleteReason}）`}</span>
                : <>
                  <span>{view.frozenUnits} 条新增语句 → {view.minimalUnits} 条触发
                    <code>{view.targetRegression}</code></span>
                  <small>{view.removalChecks} 次逐一移除检查通过{view.oneMinimal ? "，已签发 1-minimal 证书" : ""}</small>
                  <small className="scope-note">{view.scopeNote}</small>
                </>}
            </div>)}
        </div>}
      {review && diffLines.length > 0 && <button type="button" className="workbench-open result-workbench-open"
        onClick={() => { goToStage(4); setWorkbenchOpen(true); }}>
        打开代码工作台</button>}
    </section>}

    {pendingDecision && review && !terminal.has(review.state.status)
      && <DecisionGate event={pendingDecision} busy={decisionBusy}
        onDecide={decision => void answerDecision(decision)}/>}

    <div className={`journey-workspace journey-stage-${activeStage}${activeStage <= 2 ? " journey-has-inline-stage" : ""}`}>
      {activeStage === 0 && <aside className="panel intake">
        <div className="panel-title"><span>01</span><h3>选择审查对象</h3></div>
        <BoundaryLine agent={productMode === "agent"}/>
        <TaskRequirementsCard value={taskRequirementDraft} onChange={setTaskRequirementDraft} disabled={configLocked || busy} />
        {visiblePresets.length > 0 && <>
            <label htmlFor="preset">准备好的演示案例</label>
            {productMode === "agent" && <p className="field-note preset-agent-note" role="note">
              智能体审查：模型观测实验结果后可重排检查顺序；三态结论仍由确定性实验签发。</p>}
            <select id="preset" value={presetId} disabled={configLocked}
              onChange={e => changePreset(e.target.value)}>
              {visiblePresets.map(preset => <option key={preset.preset_id} value={preset.preset_id}>{preset.display_name}</option>)}
            </select>
            <div className="preset-description">
              <p>{selectedPreset?.description}</p>
              {selectedPreset && <div className="preset-meta">
                <span>{selectedPresetRepo || "已授权演示仓库"}</span>
                <span>{selectedPreset.test_files.length} 个测试文件</span>
                <span>{selectedPreset.budget_seconds} 秒</span>
                <span>{selectedPreset.model_provider === "live" ? "推荐智能体审查" : "推荐标准审查"}
                  · 本次将{productMode === "agent" ? "由模型观测后重排" : "使用确定性调度"}</span>
              </div>}
            </div>
            <button className="primary" disabled={busy || !presetId || configLocked || agentModelUnavailable}
              onClick={() => { const preset = visiblePresets.find(p => p.preset_id === presetId); if (preset) void createReview(preset); }}>
              运行这个演示案例
            </button>
        </>}
        <div className="entry-divider"><span>或者</span></div>
        {review && <p className="field-note next-run-note">
          {presentation.showPreviousResultNotice
            ? "以下配置用于下一次运行；上一轮结果已收起，确认后会生成新的 Review。"
            : "以下配置用于下一次运行；当前结果只读取本次 Review 的真实记录。"}</p>}
        {configLocked && <p className="field-note locked-note" role="status">
          当前审查进行中，结束后可配置下一次运行。</p>}
        <div className="nl-draft" aria-label="引导式开始审查">
          {/* #46 引导式入口：单个文本框换成固定问题依次问（审什么 →
              文件在哪 → 目标是什么 → 预算多少），每答一步填进现有表单
              字段；最后仍由人按确认。不是聊天台，问题集是封闭的。 */}
          <div className="guide-head">
            <span className="guide-progress">{guideStep < GUIDE_STEPS.length
              ? "第 " + (guideStep + 1) + " 步 · 共 " + GUIDE_STEPS.length + " 步"
              : "四问已答完 · 汇总"}</span>
            <ol className="guide-steps" aria-label="引导问题">
              {GUIDE_STEPS.map((step, index) => <li key={step.key}>
                <button type="button" disabled={index > guideStep}
                  className={index === guideStep ? "current" : ""}
                  onClick={() => setGuideStep(index)}>{index + 1}. {step.short}</button>
              </li>)}
            </ol>
          </div>
          <div className="guide-body">
          {guideStep === 0 && <>
          <label htmlFor="nl-text">审什么？用一句话描述这次审查（可选）</label>
          <div className="nl-draft-row">
            <input id="nl-text" value={nlText}
              disabled={configLocked || drafting || locating}
              placeholder="例：检查这次调度器改动的测试覆盖缺口，预算十分钟"
              onChange={e => setNlText(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") {
                e.preventDefault();
                if (draftAvailable) void draftFromSentence();
                else void locateFromSentence();
              } }} />
            {/* 标准审查对外承诺的是"零模型调用"。起草虽然发生在创建审查之前，
                但它确实会把这句话发给模型——在一个写着"固定为零模型调用"的
                页面上留一个亮着的模型按钮，是自己打自己的脸。所以标准模式
                只留确定性的「定位代码」，起草留给智能体模式。 */}
            <button type="button"
              disabled={!draftAvailable || configLocked || drafting || !nlText.trim()}
              title={productMode === "agent" ? "把这句话交给模型，预填下面的表单"
                : "标准审查不调用模型；改用「定位代码」，或切到智能体审查"}
              onClick={() => void draftFromSentence()}>
              {drafting ? "起草中…" : "让模型起草"}</button>
            <button type="button" className="locate-button"
              disabled={configLocked || drafting || locating || !nlText.trim() || !repos.length}
              title={repos.length ? "在已授权仓库里做确定性检索，不需要联网"
                : "先授权仓库后可用"}
              onClick={() => void locateFromSentence()}>
              {locating ? "定位中…" : "定位代码"}</button>
          </div>
          {productMode !== "agent"
            ? <>
              <p className="field-note">标准审查全程不调用模型，所以这里只提供「定位代码」：在已授权仓库里做确定性检索，命中带证据，找不到会明说。</p>
              <p className="field-note">需要模型帮你把一句话变成计划，请切到智能体审查。</p>
            </>
            : providerInfo.live_available === true
            ? <p className="field-note">模型只根据这句话预填下面的表单；它不读代码、不下结论，确认之前不会创建任何审查。「定位代码」不需要联网：在已授权仓库里做确定性检索，命中带证据，找不到会明说。</p>
            : <p className="field-note">模型通道未连接：一句话起草需要联网的模型通道。
              「定位代码」不需要联网，仍可用；或直接填写下方表单，确定性审查不受影响。</p>}
          {productMode === "agent" && providerInfo.live_available === true &&
            <p className="field-note guide-risk" role="note">封闭问题集不等于封闭输入：
              「审什么」是自由文本，「让模型起草」会把它原样交给一次真实模型调用。
              注入面没有消失，只是被拆成了几段；标准审查默认只走确定性检索，零模型。</p>}
          </>}
          {guideStep === 1 && <>
            <label htmlFor="guide-repo">文件在哪？从已授权仓库里选一个</label>
            <select id="guide-repo" value={repoId} disabled={configLocked}
              onChange={e => selectCustomRepo(e.target.value)}>
              <option value="">请选择仓库</option>
              {repos.map(item => <option key={item.repo_id} value={item.repo_id}>{item.display_name}</option>)}
            </select>
            <p className="field-note">列表只显示本次本地服务已授权的仓库；要增加仓库，展开下方「高级设置」→「增加本地仓库」。</p>
          </>}
          {guideStep === 2 && <>
            <label>目标是什么？选一个方向，或自己写</label>
            <div className="guide-chips">
              {GOAL_PRESETS.map(option => <button key={option.id} type="button"
                className={goalPreset === option.id ? "chip active" : "chip"}
                disabled={configLocked}
                onClick={() => { setGoalPreset(option.id); setFocusOverride(""); setGoal(option.goal); }}>
                {option.label}</button>)}
            </div>
            <label htmlFor="guide-goal">或自己写（与下方「审查目标」同步）</label>
            <textarea id="guide-goal" rows={2} value={goal} disabled={configLocked}
              onChange={e => { setGoal(e.target.value); setGoalPreset("custom"); setFocusOverride(""); }}/>
            <p className="field-note">{productMode === "agent"
              ? "智能体模式下，这个目标会作为排序上下文原样交给模型。"
              : "标准审查里，结构化重点映射到确定性排序；自由备注只留档。"}</p>
          </>}
          {guideStep === 3 && <>
            <label htmlFor="guide-budget">预算多少？</label>
            <div className="guide-chips">
              {BUDGET_CHOICES.map(([seconds, label]) =>
                <button key={label} type="button" className={budget === seconds ? "chip active" : "chip"}
                  disabled={configLocked} onClick={() => setBudget(seconds)}>{label}</button>)}
            </div>
            <div className="guide-budget-row">
              <input id="guide-budget" type="number" min="1" max="3600" value={budget}
                disabled={configLocked} onChange={e => setBudget(Number(e.target.value))}/>
              <span>秒</span>
            </div>
            <p className="field-note">预算只限制实验总时长；预算耗尽不会把任何一行抬高成「承重」。</p>
          </>}
          {guideStep === 4 && <div className="guide-summary" role="status">
            <strong>四个问题都答完了</strong>
            <dl>
              <div><dt>审什么</dt><dd>{nlText.trim() || "（没填——可回第 1 步补充，或直接用下方表单）"}</dd></div>
              <div><dt>文件在哪</dt><dd>{repoId ? repoDisplayName(repoId) : "（还没选仓库——第 2 步必填）"}</dd></div>
              <div><dt>目标是什么</dt><dd>{goal || "—"}</dd></div>
              <div><dt>预算多少</dt><dd>{budget} 秒</dd></div>
            </dl>
            <p className="field-note">以上答案已同步进下方表单。接下来仍由人来按：
              先「生成审查计划」，再在计划页按「确认计划并开始审查」——没有这两步，不会执行任何实验。</p>
            {productMode === "agent"
              ? <p className="field-note guide-risk" role="note">封闭问题集不等于封闭输入：
                  「审什么」「目标是什么」是自由文本，智能体模式下会交给一次真实模型调用。
                  输入面没有消失，只是被拆成了几段。</p>
              : <p className="field-note">标准审查全程零模型调用；「审什么」只用于确定性检索，不经过任何模型。</p>}
          </div>}
          </div>
          {draftNotice && <p className="field-note draft-notice" role="status">{draftNotice}</p>}
          {draftResult?.generated && draftResult.draft && <div className="draft-card" role="status">
            <strong>{draftResult.note || "这是模型的理解，不是结论"}</strong>
            <dl>{draftCardRows(draftResult.draft, draftResult.sources).map(row =>
              <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}
            </dl>
            <div className="draft-card-actions">
              <button type="button" className="approve" disabled={configLocked || busy}
                onClick={applyDraft}>照这个理解填入表单</button>
              <button type="button" disabled={configLocked}
                title="丢弃这份草案，表单里已有的设置原样保留"
                onClick={() => { setDraftResult(null); setDraftNotice(""); }}>保留当前设置</button>
            </div>
            {productMode === "agent" && draftResult.test_files_preview?.length
              ? <small>确认后测试文件留空，创建时自动发现（本仓库预计 {draftResult.test_files_preview.length} 个测试文件）。</small>
              : undefined}
          </div>}
          {locateNotice && <p className="field-note draft-notice" role="status">{locateNotice}</p>}
          {locateResult && <div className="locate-card" role="status">
            {locateResult.status === "matched" && locateResult.draft ? <>
              <strong>{locateResult.note || "定位来自仓库内确定性检索，不是模型判断"}</strong>
              {locateResult.tokens.length > 0 &&
                <p className="locate-tokens">检索词：{locateResult.tokens.join("、")}</p>}
              <ul className="locate-matches">
                {locateResult.matches.map((match, index) => <li
                  key={`${match.repo_id}-${match.path}-${index}`}>
                  <code>{match.path}</code>
                  <span className="locate-evidence">
                    {locateEvidenceLabel(match.evidence_kind, match.evidence_detail)}
                  </span>
                  <small>{repoDisplayName(match.repo_id)}</small>
                </li>)}
              </ul>
              <div className="draft-card-actions">
                <button type="button" className="approve" disabled={configLocked || busy}
                  onClick={applyLocate}>照这个定位填入表单</button>
                <button type="button" disabled={configLocked}
                  title="丢弃这次定位结果，回到下面的表单自己填"
                  onClick={() => { setLocateResult(null); setLocateNotice(""); }}>返回手动填写</button>
              </div>
            </> : <>
              <strong>定位没有可用的候选</strong>
              {locateResult.tokens.length > 0 &&
                <p className="locate-tokens">检索词：{locateResult.tokens.join("、")}</p>}
              <p className="locate-fallback">{locateFallbackNotice(locateResult.reason || "")}</p>
            </>}
          </div>}
          <div className="guide-nav">
            {guideStep > 0 && <button type="button" onClick={() => setGuideStep(guideStep - 1)}>上一步</button>}
            {guideStep < GUIDE_STEPS.length &&
              <button type="button" className="guide-next"
                disabled={guideStep === 1 && !repoId}
                title={guideStep === 1 && !repoId ? "先选一个仓库才能继续" : undefined}
                onClick={() => setGuideStep(guideStep + 1)}>
                {guideStep === GUIDE_STEPS.length - 1 ? "查看汇总" : "下一步"}</button>}
          </div>
        </div>
        <label htmlFor="goal-preset">{productMode === "agent" ? "推荐审查目标" : "结构化审查重点"}</label>
        <select id="goal-preset" value={goalPreset} disabled={configLocked} onChange={e => {
          const next = e.target.value;
          setGoalPreset(next);
          setFocusOverride("");
          const recommended = GOAL_PRESETS.find(option => option.id === next);
          if (recommended) setGoal(recommended.goal);
        }}>
          {GOAL_PRESETS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
          <option value="custom">{productMode === "agent" ? "自己输入目标" : "仅填写备注"}</option>
        </select>
        {demoGoalFits && goalPreset !== "custom" && !demoGoalFits.includes(goalPreset) &&
          <p className="field-note goal-fit-warning" role="status">这个示例仓库不是该目标的推荐演示：可以运行，
            但可能没有相应候选或无法形成有意义的排序。建议换仓库或改用「综合检查」。</p>}
        <label htmlFor="goal">{productMode === "agent" ? "审查目标" : "审查备注（可选）"}</label><textarea id="goal" rows={2} value={goal} disabled={configLocked}
          placeholder={productMode === "agent" ? "描述你希望 " + setupModelLabel + " 优先关注的内容" : "补充背景、风险或希望人工留意的内容（不会改变确定性排序）"}
          onChange={e => { setGoal(e.target.value); setGoalPreset("custom"); setFocusOverride(""); }} />
        <p className="field-note">{productMode === "agent"
          ? "审查目标会交给 " + setupModelLabel + " 作为排序上下文；三态结论只来自真实测试和恢复实验。"
          : "结构化审查重点会映射到确定性排序；自由备注只留档，不会被语义理解。三态结论只来自真实测试和恢复实验。"}</p>
        {productMode === "agent" && provider === "live" && <p className="field-note goal-live-note">
          这个目标将原样交给 {setupModelLabel}。</p>}
        <details className="advanced-settings" open={advancedOpen || productMode === "agent"}>
          <summary>高级设置<small>仓库 · 测试范围 · 预算 · 修复授权 · 最小化策略</small></summary>
          <div className="advanced-settings-body">
            {/* 原生 <option> 放不下两行：中文用户名称在上，英文目录名在下。
                仓库名一旦中文化，只显示一个名字就会让人对不上磁盘上的目录。 */}
            <label id="repo-label" htmlFor="repo">已授权仓库</label>
            <RepoCombobox id="repo" value={repoId} options={repos}
              disabled={configLocked} onChange={selectCustomRepo}/>
            <p className="repo-scope-note">列表只显示已经授权的 Git 仓库；授权只在本次本地服务期间有效。</p>
            <label htmlFor="repo-path">增加本地仓库</label>
            <div className="repo-add"><input id="repo-path" value={repoPath} disabled={configLocked}
              placeholder="粘贴 Git 仓库根目录的完整路径" onChange={e => setRepoPath(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") void authorizeRepo(); }}/>
              <button type="button" disabled={busy || configLocked || !repoPath.trim()}
                onClick={() => void authorizeRepo()}>授权并加入</button></div>
            <label htmlFor="test-files">{productMode === "agent" ? "测试文件（可选）" : "pytest 测试文件"}</label><textarea id="test-files" rows={2} value={tests}
              disabled={configLocked} placeholder={"tests/test_backoff.py\ntests/integration/test_api.py"}
              onChange={e => setTests(e.target.value)} />
            <div className="field-rules">
              <p>{productMode === "agent" ? "留空时，智能体仅自动发现仓库内的 Python 测试文件；也可每行手动指定一个路径，例如 " : "每行填写一个仓库内的 pytest 文件路径，从仓库根目录开始，例如 "}<code>tests/test_backoff.py</code>。</p>
              <p>不能填写绝对路径、文件夹、<code>../</code> 或 pytest 命令。</p>
            </div>
            {scopeInclude.length > 0 && <p className="field-note locate-include-note">
              定位候选路径已带入（scope.include，作为修复实验的路径白名单）：{scopeInclude.join("、")}</p>}
            <div className="row"><div><label htmlFor="budget">预算 / 秒</label><input id="budget" type="number" min="1" max="3600"
              value={budget} disabled={configLocked} onChange={e => setBudget(Number(e.target.value))}/></div>
              <div><label>调度方式</label><div className="fixed-provider">
                {productMode === "agent" ? setupModelLabel + " 受限调度 · L2" : "确定性调度 · 不调用模型"}
              </div></div></div>
            <label className="permission-toggle" htmlFor="allow-repair-branch">
              <input id="allow-repair-branch" type="checkbox" checked={allowRepairBranch}
                disabled={configLocked} onChange={e => setAllowRepairBranch(e.target.checked)} />
              <span>验证通过后允许创建本地修复分支</span>
            </label>
            <p className="field-note">这是显式授权：只接受你随后粘贴的封闭补丁，验证失败、快照过期或分支冲突时不会提交。</p>
            <label htmlFor="probe-strategy">最小化策略</label>
            <select id="probe-strategy" value={probeStrategy} disabled={configLocked}
              onChange={e => setProbeStrategy(e.target.value)}>
              <option value="hdd_inspired">HDD 启发式（默认）</option>
              <option value="ddmin" disabled={!capabilityAvailable(capabilities, "ddmin")}>
                完整 ddmin（额外实验，产出 1-minimal 证书）</option>
            </select>
            <p className="field-note">只影响是否额外签发 1-minimal 证书，不改变逐行三态结论。</p>
          </div>
        </details>
        <p className="field-note provider-note">{productMode === "agent"
          ? "智能体审查会调用 " + setupModelLabel + "；模型只调整检查顺序并生成只读建议。"
          : "标准审查固定为零模型调用。"}</p>
        {presentation.showDeterministicPlaceholder && <div className="setup-placeholder" role="status">
          <strong>本次将使用确定性调度，不调用模型</strong>
          <span>上一轮结果已收起；新的模型轨迹和建议不会从历史事件继承。</span>
        </div>}
        {productMode === "agent" && provider === "live" && <div className="model-entry" aria-label={setupModelLabel + " 受限调度说明"}>
          <div className="model-badges">
            <span>真实 API</span><span>读取用户目标</span>
            <span>观测后可重排</span><span>完成后生成只读建议</span>
          </div>
          <div className="permission-card">
            <div><h4>可以</h4><ul>
              <li>理解目标</li><li>安排顺序</li><li>观察结果</li>
              <li>有限重排</li><li>解释理由</li><li>生成只读建议</li>
            </ul></div>
            <div><h4>不可以</h4><ul>
              <li>修改候选范围</li><li>伪造 pytest</li><li>写三态结论</li>
              <li>修改代码</li><li>commit 或 push</li>
            </ul></div>
          </div>
          <p className="disclosure">建议阶段会向 {setupModelLabel} 发送最多 80 条、总计不超过 12KB
            的相关新增代码行；确认计划即表示同意本次发送范围。</p>
          {presentation.showLiveSetupPlaceholder && <div className="setup-placeholder live-setup" role="status">
            <strong>尚未开始模型调用</strong>
            <span>创建并确认新的 Review 后，决策轨迹只由本轮真实事件推进。</span>
          </div>}
        </div>}
        {agentModelUnavailable && <div className="agent-unavailable" role="alert" aria-label="智能体审查暂不可创建">
          <strong>智能体审查需要已配置的模型服务</strong>
          <p>当前本地服务没有可用的模型通道，创建操作已关闭。你可以先查看智能体审查的说明；标准审查不受影响，随时可用。</p>
          <div className="agent-unavailable-actions">
            <details className="model-config-hint">
              <summary>配置模型</summary>
              <p>在启动本地服务前设置模型环境变量（详见 docs/live-model-demo.md）：</p>
              <ul>
                <li><code>SHUIMU_YANMA_MODEL_BASE_URL</code></li>
                <li><code>SHUIMU_YANMA_MODEL_API_KEY</code></li>
                <li><code>SHUIMU_YANMA_MODEL_ID</code></li>
              </ul>
              <p>重启服务后这里会恢复可创建。</p>
            </details>
            <button type="button" onClick={() => changeProductMode("standard")}>返回标准审查</button>
          </div>
        </div>}
        <button className="primary" disabled={busy || !repoId || (!tests.trim() && productMode !== "agent") || configLocked || agentModelUnavailable}
          onClick={() => void createReview()}>生成审查计划</button>
        <label className="replay">查看 ReviewBundle（JSON）<input type="file" accept="application/json"
          onChange={e => e.target.files?.[0] && loadReplay(e.target.files[0])}/></label>
      </aside>}

      {activeStage === 1 && <section className="panel plan">
        {/* 两个模式都默认展开。折叠是用户的选择，不是默认布局：
            这块面板在网格里由 intake 列的高度撑着，折起来不会变矮，
            只会变成一个横跨整列的空方框。 */}
        <details className="panel-fold" open>
          <summary className="panel-title"><span>02</span><h3>确认审查计划</h3></summary>
          <BoundaryLine agent={boundaryAgent}/>
        {review?.state.status === "AWAITING_APPROVAL" && <div className="plan-approval">
          <strong>请确认这次审查的边界</strong>
          <ul>
            <li>冻结候选 {(review.plan?.priorities as string[] | undefined)?.length ?? 0} 项，运行中不得增删</li>
            <li>声明测试范围 {(review.plan?.scope as string[] | undefined)?.length ?? 0} 项</li>
            <li>预算 {String(review.plan?.budget_seconds ?? "")} 秒</li>
          </ul>
          <MemoryRulesCard rules={memoryRules} repoId={memoryRepoId}
            writable={memoryWritable}/>
          <PlanRevisionThread
            views={planRevisionViews(planRevisions, review.review_id)}
            open={reviseOpen} onOpen={setReviseOpen}
            instruction={reviseInstruction} onInstruction={setReviseInstruction}
            reason={reviseReason} onReason={setReviseReason}
            busy={reviseBusy} revisable={canRevise}
            onSubmit={() => void submitRevision()} />
          <button className="approve" disabled={busy} onClick={approve}>确认计划并开始审查</button>
          <small>水木验码不会自动删代码：它临时移除、观察具名测试、随后恢复。未经这一步不会执行任何实验。</small>
        </div>}
        {review?.plan ? <>
          <details className="plan-fingerprint"><summary>技术指纹</summary>
            <div className="hash"><small>计划指纹 · SHA-256</small>
              <code>{shortHash(review.plan.plan_sha256).full || "计划草案"}</code></div>
          </details>
          <dl className="plan-summary-cards">
            <div><dt>审查对象</dt><dd>{Array.isArray(review.plan.scope) ? review.plan.scope.length : 0} 个冻结候选</dd></div>
            <div><dt>测试范围</dt><dd>{Array.isArray(review.request?.test_files)
              ? (review.request?.test_files as unknown[]).length : 0} 个声明文件</dd></div>
            <div><dt>实验预算</dt><dd>{String(review.plan.budget_seconds || budget)} 秒</dd></div>
            <div><dt>运行模式</dt><dd>{review.request?.model_provider === "live" ? "智能体审查" : "标准审查"}</dd></div>
          </dl>
          {Object.keys(activeReviewSpec).length > 0 && <div className="decision-contract"
              aria-label="审查授权摘要">
            <h4>本次授权与数据边界</h4>
            <dl>
              <div><dt>风险等级</dt><dd>{String(repairAuthorization?.max_risk || "L2")}
                {repairAuthorization?.allow_repair_branch === true ? " · 可生成并验证本地候选" : " · 仅隔离分析与实验"}</dd></div>
              <div><dt>修改上限</dt><dd>最多 {String(activeScope.max_modified_files || 5)} 个文件 / {String(activeScope.max_changed_lines || 400)} 行</dd></div>
              <div><dt>网络与安装</dt><dd>{activeConstraints.allow_network === true ? "按计划允许受限网络" : "断网"} · {activeConstraints.allow_dependency_install === true ? "允许锁定依赖" : "不安装依赖"}</dd></div>
              <div><dt>模型预算</dt><dd>总 token ≤ {String(activeConstraints.max_total_tokens || 20000)} · 费用 ≤ ¥{(Number(activeConstraints.max_cost_cny_fen || 0) / 100).toFixed(2)}</dd></div>
              <div><dt>将发送的数据</dt><dd>{Array.isArray(activeDataPolicy.model_data_categories)
                ? (activeDataPolicy.model_data_categories as string[]).join(" · ") : "最小结构化事实"}；不发送私有源码全文</dd></div>
              <div><dt>输出</dt><dd>{activeOutputPolicy.review_bundle !== false ? "证据包" : ""} {activeOutputPolicy.human_report !== false ? "· 可读报告" : ""} {activeOutputPolicy.local_branch === true ? "· 本地分支与提交" : "· 不创建分支"}</dd></div>
              <div><dt>会再次询问</dt><dd>范围、预算、风险、网络、数据外发、依赖安装、HEAD 或工作区指纹发生实质变化</dd></div>
              <div><dt>明确未检查</dt><dd>{Array.isArray(activeScope.exclude) && activeScope.exclude.length
                ? (activeScope.exclude as string[]).join(" · ") : "计划范围之外、未声明测试及未启用适配器"}</dd></div>
            </dl>
          </div>}
          {review.request?.model_provider === "live" && <div className="decision-contract"
              aria-label="模型决策契约">
            <h4>模型决策契约</h4>
            <p className="contract-model-note">本次使用：{displayModelLabel}</p>
            <dl>
              <div><dt>用户目标原文</dt><dd>{String(review.request.goal || "—")}</dd></div>
              <div><dt>冻结候选数量</dt><dd>{Array.isArray(review.plan.scope)
                ? review.plan.scope.length : 0} 项，运行中不得增删</dd></div>
              <div><dt>模型可执行动作</dt><dd>保持顺序 · 有限重排 · 合法停止</dd></div>
              <div><dt>将发送的数据范围</dt><dd>结构化目标、预算、候选摘要、最新观测；
                建议阶段另发最多 80 行 / 12KB 相关新增代码</dd></div>
              <div><dt>人工确认状态</dt><dd>{review.state.status === "AWAITING_APPROVAL"
                ? "等待确认" : "已确认"}</dd></div>
              <div><dt>计划来源</dt><dd>{planSource}</dd></div>
              <div><dt>提示词版本</dt><dd>{((providerInfo.prompt_versions as string[])
                || []).join(" · ")}</dd></div>
            </dl>
            <small>完成后可展开完整 system／user 输入与模型原始输出；不含 API Key、请求头或服务地址，内部思维过程不公开。</small>
          </div>}
          {review.state.status !== "AWAITING_APPROVAL" &&
            <MemoryRulesCard rules={memoryRules} repoId={memoryRepoId}
              writable={memoryWritable}/>}
          <h4 className="order-heading">冻结候选全集</h4>
          <div className="anchors frozen">{((review.plan.priorities as string[]) || []).map((a, i) =>
            <div key={a}><b>{String(i + 1).padStart(2, "0")}</b><span>{a}</span></div>)}</div>
          <h4 className="order-heading">当前检查顺序{currentOrder.join("")
            !== ((review.plan.priorities as string[]) || []).join("")
            ? " · 已由调度更新" : ""}</h4>
          <div className="anchors live-order">{currentOrder.map((a, i) =>
            <div key={a} ref={orderRef(a)}><b>{String(i + 1).padStart(2, "0")}</b><span>{a}</span></div>)}</div>
          {/* 确认入口只留一个。左栏的「请确认这次审查的边界」卡片带着边界清单、
              记忆规则和修订入口，是真正要读的那一个；这里再放一颗同名主按钮，
              屏幕上就有两颗一模一样的确认键，读屏软件里也是两条同名项。 */}
          {review.state.status === "AWAITING_APPROVAL" && <p className="field-note approve-pointer">
            这份计划的确认入口在「请确认这次审查的边界」卡片里，和边界清单放在一起。</p>}
        </> : <div className="empty">提交输入后，这里会展示检查对象、允许工具、预算和计划指纹；未经确认不会执行仓库代码。</div>}
        </details>
      </section>}

      {activeStage === 2 && showDecisionTrack && <section className="panel decision-track"
        aria-label="模型决策轨迹">
        <div className="panel-title"><span>{stepNo("decision")}</span><h3>模型决策轨迹</h3>
          <small>真实事件驱动</small></div>
        <ol className="decision-steps">
          {decisionStages.map(stage => <li key={stage.key}
            className={`stage-${stage.state}`}>
            <i/><span>{stage.label}</span>
            {stage.note && <small>{stage.note}</small>}
          </li>)}
        </ol>
        {latestModelAction ? (() => {
          const d = latestModelAction.data || {};
          const obs = (d.last_observation || {}) as Record<string, unknown>;
          const regressed = (obs.regressed_tests || []) as unknown[];
          const original = (d.original_order || []) as string[];
          const actual = (d.actual_order || []) as string[];
          const candidates = (d.remaining_candidates || []) as Array<Record<string, unknown>>;
          return <div className="reorder-card">
            <div className="reorder-head">
              <strong>{d.kind === "reprioritize" ? "已重排" : d.kind === "stop" ? "已请求停止" : "保持顺序"}</strong>
              <span className={d.source === "live_model" ? "source-live" : "source-fallback"}>
                {d.source === "live_model" ? "live_model"
                  : d.source === "deterministic" ? "确定性" : "确定性降级"}</span>
            </div>
            <dl>
              <div><dt>上一步观察</dt><dd>{String(obs.anchor_id || "—")} ·
                {String(obs.policy_branch || "—")}{regressed.length
                  ? ` · ${regressed.length} 项具名回归` : " · 无具名回归"}</dd></div>
              <div><dt>预算</dt><dd>已花 {String(d.spent_seconds ?? "—")}s / 剩余
                {String(d.budget_left_seconds ?? "—")}s</dd></div>
              <div><dt>顺序变化</dt><dd><code>{original.join(" → ")}</code>
                <b>⇒</b><code>{actual.join(" → ")}</code></dd></div>
              <div><dt>候选成本与覆盖</dt><dd>{candidates.map(c =>
                `${String(c.anchor_id)} ≈${String(c.estimated_cost_s ?? "?")}s（覆盖
                ${String(c.covered_added_lines ?? "?")}/${String(c.added_lines ?? "?")} 行）`).join("；")
                || "—"}</dd></div>
              <div><dt>模型理由</dt><dd>{String(d.reason || "—")}</dd></div>
            </dl>
          </div>;
        })() : <div className="empty">等待第一次真实模型调用。</div>}
      </section>}

      {activeStage === 2 && <section className="panel timeline">
        <div className="panel-title"><span>03</span><h3>验证测试保护</h3><small>{events.length} 条技术记录</small></div>
        <div className="timeline-toolbar">
          <button type="button" className="technical-toggle"
            aria-expanded={timelineExpanded}
            onClick={() => setTimelineExpanded(value => !value)}>
            {timelineExpanded ? "收起技术详情" : "展开技术详情"}
          </button>
          <span>{timelineExpanded ? "显示全部事件、对象、结果与原始数据入口" : `已折叠 ${Math.max(0, events.length - 4)} 条技术记录（点开看每一步的原始记录）`}</span>
        </div>
        {!timelineExpanded ? <div className="timeline-summary" aria-label="四步实验摘要">
          {experimentStages.map(stage => <button key={stage.key} type="button"
            className={`timeline-summary-step story-${stage.state}`}
            disabled={!storyEvent(stage.key)}
            onClick={() => { const event = storyEvent(stage.key); if (event) void inspectEvent(event); }}>
            <strong>{stage.label}</strong><span>{stage.detail}</span>
            <small>{EXPERIMENT_EXPLANATIONS[stage.key] || "查看这一阶段的原始记录。"}</small>
          </button>)}
        </div> : !resultRanAsAgent ? <div className="lane-list">{currentLanes.map(group => <section
          key={group.lane} className={`lane lane-${group.lane === "模型建议" ? "model" :
            group.lane === "测试执行" ? "tests" : "executor"}`}>
          <div className="lane-heading"><h4>{group.lane}</h4><span>{group.events.length} 条</span></div>
          <div className="event-list">{group.events.length ? group.events.map(event =>
            <button key={event.event_id} onClick={() => inspectEvent(event)}
              className={selected?.event_id === event.event_id ? "active" : ""}>
              <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
              <span><strong>{event.title}</strong><small>{event.explanation}</small>
                {event.technicalSummary && <small className="event-technical">{event.technicalSummary}</small>}
                <EventDeltaLine delta={deltas[event.event_id]}/></span>
              <b>#{event.seq}</b>
            </button>) : <div className="empty">本轮尚无该方事件。</div>}</div>
        </section>)}</div> : <div className="event-list">{visibleGrouped.length ? visibleGrouped.map(event =>
          <button key={event.event_id} onClick={() => inspectEvent(event)}
            className={selected?.event_id === event.event_id ? "active" : ""}>
            <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
            <span><strong>{event.title}</strong><small>{event.explanation}</small>
              {(event.technicalSummary || schedulingDetail(event.kind, event.data)) &&
                <small className="event-technical">{event.technicalSummary || schedulingDetail(event.kind, event.data)}</small>}
              <EventDeltaLine delta={deltas[event.event_id]}/></span><b>#{event.seq}</b>
          </button>) : <div className="empty">确认计划后，基线验证、证据实验、恢复检查和结论签发会依次出现在这里。</div>}</div>}
      </section>}
    </div>

    {activeStage === 3 && <section className={`completion-panel ${review?.state.status === "COMPLETE" ? "complete" : "incomplete"}`}>
      {offlineNotice && <div className="offline-banner" role="status">
        <strong>{offlineNotice.label}</strong><span>{offlineNotice.detail}</span>
      </div>}
      <div className="completion-heading"><div className="panel-title"><span>04</span><h3>理解审查结论</h3><small>{currentStatus.label}</small></div>
        <div className="result-signature"><OfficialLockup compact /></div>
      </div>
      <BoundaryLine agent={boundaryAgent}/>
      {/* 「降级」是对**这份结果**的描述，不是对当前 UI 模式的描述。
          切到智能体模式去看一份标准审查跑出来的结果，它并没有降级——
          它从来就没打算调用模型。把条件挂在当前模式上，屏幕上会同时出现
          「智能体已降级」和「0 次降级」两句互相打脸的话。 */}
      {resultRanAsAgent && isTerminal
        && (!participation.live || participation.fallbacks > 0) &&
        <div className="agent-degraded" role="status" aria-label="智能体已降级">
          <strong>智能体已降级</strong>
          <span>三态结论仍来自确定性实验</span>
        </div>}
      {isTerminal && !resultRanAsAgent &&
        <div className="agent-standard-result" role="status" aria-label="这次结果来自标准审查">
          <strong>这次结果来自标准审查</strong>
          <span>全程零模型调用；下面没有模型轨迹可看，不是智能体运行失败。</span>
        </div>}
      {summary ? <h2>{emphasizeNumbers(summarySentence(summary)).map((part, i) =>
        part.number ? <b key={i}>{part.text}</b> : <span key={i}>{part.text}</span>)}</h2>
        : (() => {
          const rawReason = String(review?.state.reason || "").trim();
          const known = TERMINAL_REASON_TITLES[rawReason]
            ?? (rawReason.includes("process_restart")
              ? "本地服务重启，审查已中止。" : "");
          const headline = !rawReason ? "本次审查没有生成可发布的证据包。"
            : known || "审查未正常完成；结论没有通过校验，不能当作完成。";
          return <>
            <h2>{headline}</h2>
            {rawReason && <p className="terminal-reason-raw">技术记录：{rawReason}</p>}
          </>;
        })()}
      {resultRanAsAgent && <p className="model-conclusion">{modelConclusion(participation, displayModelLabel)}</p>}
      <div className="completion-grid">
        <div><span>运行来源</span><strong>{runMode}</strong></div>
        <div><span>调度方式</span><strong>{displaySchedulingMode}</strong></div>
        <div><span>完成范围</span><strong>{completionLabel}</strong></div>
        <div><span>工作区恢复</span><strong>{summary?.restore_protocol_version ? "逐实验校验" : "—"}</strong></div>
      </div>
      <section className="evidence-passport" aria-label="证据护照">
        {/* 完成标题与证据护照各保留一条横幅，离线回放时两处都醒目。 */}
        {offlineNotice && <div className="offline-banner" role="status">
          <strong>{offlineNotice.label}</strong><span>{offlineNotice.detail}</span>
        </div>}
        <div className="passport-heading">
          <div><span className="passport-kicker">REVIEW EVIDENCE PASSPORT</span>
            <h4>证据护照</h4>
            <p>把本次 Review 的可复验事实压缩成一张可带走的结果卡。</p></div>
          <strong className={`passport-stamp passport-${passport.status.toLowerCase()}`}>
            {passport.statusLabel}
          </strong>
        </div>
        <div className="passport-fields">
          <div><span>新增代码行数</span><strong>{passport.addedLines} 行</strong></div>
          <div><span>承重行数</span><strong>{passport.loadLines} 行</strong></div>
          <div><span>具名失败测试数</span><strong>{passport.namedFailures} 个</strong></div>
          <div><span>无据行数</span><strong>{passport.unevidencedLines} 行</strong></div>
          <div><span>游离行数</span><strong>{passport.driftLines} 行</strong></div>
          <div><span>恢复状态</span><strong>{passport.restoreLabel}</strong></div>
          <div className="passport-fingerprint"><span>计划指纹</span>
            <code>{passport.planFingerprint}</code></div>
        </div>
        {passport.live ? <div className="passport-model" aria-label="证据护照模型参与">
          <div><span>模型参与</span><strong>是 · {passport.modelLabel}</strong></div>
          <div><span>模型调用</span><strong>{passport.modelCalls} 次</strong></div>
          <div><span>建议阶段</span><strong>{passport.recommendationLabel}</strong></div>
        </div> : !passport.live && <div className="passport-deterministic">本次未调用模型 · 确定性调度</div>}
        <div className="passport-footer">
          <span>{passport.bundleAvailable ? "完整 ReviewBundle 已装载，可下载留存。" : "本次未装载完整 ReviewBundle。"}</span>
          {reviewBundle && <button className="download" onClick={downloadBundle}>下载完整 ReviewBundle</button>}
          <button className="download" onClick={() => void copyPassportMarkdown()}
            title="把这张卡上的字段导出成 Markdown，只含已有字段">复制摘要（Markdown）</button>
        </div>
        {summaryNotice && <small className="passport-copy-note" role="status">{summaryNotice}</small>}
        {summaryFallback && <textarea className="passport-copy-fallback" readOnly rows={8}
          aria-label="证据护照 Markdown（可手动复制）" value={summaryFallback} />}
      </section>
      {review?.request?.schema_version === "review-request-v2" && !offlineReplay &&
        <section className="repair-delivery" aria-label="本地修复交付">
          <div className="repair-heading">
            <div><span className="passport-kicker">VERIFIED REPAIR DELIVERY</span>
              <h4>本地修复交付</h4>
              <p>交付记录只读；补丁必须在隔离 worktree 通过声明测试后才会创建本地分支。</p></div>
            <strong className={`repair-stamp repair-${repairView.tone}`}>{repairView.label}</strong>
          </div>
          <p className="repair-detail">{repairView.detail}</p>
          {review.repair?.status === "DELIVERED" ? <>
            <dl className="repair-record">
              <div><dt>修复分支</dt><dd><code>{String(review.repair.delivery_record?.branch || "—")}</code></dd></div>
              <div><dt>提交 ID</dt><dd><code>{String(review.repair.delivery_record?.commit || "—")}</code></dd></div>
              <div><dt>EvidenceManifest</dt><dd>{(() => {
                const digest = shortHash(review.repair.evidence_manifest_sha256
                  || review.repair.delivery_record?.evidence_manifest_sha256 || "—");
                return <code title={digest.full}>{digest.short}{digest.truncated ? "…" : ""}</code>;
              })()}</dd></div>
              <div><dt>源提交</dt><dd><code>{String(review.repair.evidence_manifest?.source_commit || "—")}</code></dd></div>
            </dl>
            <div className="repair-actions">
              <button className="download" onClick={downloadRepairEvidence}>下载交付证据</button>
              <button className="download" onClick={() => void refreshRepairStatus()}>重新读取状态</button>
            </div>
          </> : review.repair?.status === "INCOMPLETE" ? <div className="repair-warning">
            交付工件缺失或不一致。请先清理隔离残留，再重新发起一次完整审查；系统不会覆盖已有分支。
          </div> : !repairAuthorized ? <div className="repair-warning">
            本次冻结计划没有授权创建修复分支。若需要交付，请返回配置并明确勾选「验证通过后允许创建本地修复分支」。
          </div> : <>
            <label htmlFor="repair-patch">粘贴已审阅的封闭补丁（unified diff）</label>
            <textarea id="repair-patch" rows={7} value={repairPatch} disabled={repairBusy}
              placeholder="仅接受 1–5 个文本文件的 unified diff；不会执行补丁中的命令。"
              onChange={e => setRepairPatch(e.target.value)} />
            <label htmlFor="repair-session">绑定编辑会话（可选）</label>
            <select id="repair-session" value={repairSessionId} disabled={repairBusy}
              onChange={e => setRepairSessionId(e.target.value)}>
              <option value="">不绑定会话，直接交付</option>
              {activeSessions.map(session => <option key={session.session_id || ""}
                value={session.session_id || ""}>
                {session.session_id}（{editSessionStateView(session.status).label}）
              </option>)}
            </select>
            <small className="repair-note">绑定后，交付前会重新比对会话开启时冻结的源快照；源码变过就拒绝交付，不会把旧补丁盖到新树上。</small>
            <div className="repair-actions">
              <button className="primary" disabled={repairBusy || !repairPatch.trim()}
                onClick={() => void deliverRepair()}>验证补丁并创建本地分支</button>
              <button className="download" disabled={repairBusy} onClick={() => void refreshRepairStatus()}>重新读取状态</button>
            </div>
            <small className="repair-note">系统会再次运行本次声明测试，并在提交前后确认源工作区指纹未改变；不会 push、merge 或切换你的当前 checkout。</small>
          </>}
        </section>}
      {review?.request?.schema_version === "review-request-v2" && !offlineReplay &&
        <section className="repair-delivery edit-sessions" aria-label="编辑会话与交付记录">
          <div className="repair-heading">
            <div><span className="passport-kicker">EDIT SESSIONS LEDGER</span>
              <h4>编辑会话与交付记录</h4>
              <p>会话与候选补丁在这里入账：可以开启会话、生成候选补丁、放弃不再需要的会话。</p>
              <p>已交付的会话在下方交付记录里批准五指纹比对基线——批准只冻结基线，不应用任何改动。</p></div>
            <button className="download" onClick={() => void refreshEditSessions()}>重新读取</button>
          </div>
          {editSessionsNotice
            && <div className="repair-warning" role="alert">暂时读不到编辑会话与交付记录：{editSessionsNotice}。可以点「重新读取」再试；拿到记录之前，这里不会假装没有这回事。</div>}
          {sweptSessions.length > 0 && <div className="repair-warning" role="alert">
            后台检查发现 {sweptSessions.length} 条编辑会话已经收尾，不会再被使用，里面的补丁也不会自动交付：
            <ul className="es-swept">
              {sweptSessions.map((session, index) => <li key={session.session_id || `swept-${index}`} >
                <code>{session.session_id}</code>{session.status === "stale"
                  ? "已失效：会话锁定的源代码在开启后又发生了变化，冻结补丁已拒绝交付。"
                  : "已作废：开启会话的进程中断，系统收回该会话。"}
              </li>)}
            </ul>
          </div>}
          <form className="es-approval" onSubmit={event => {
            event.preventDefault();
            void openEditSession();
          }}>
            <label htmlFor="edit-session-intent">开启编辑会话：写明这次修改的目的（可选，最多 2000 字）</label>
            <textarea id="edit-session-intent" rows={2} maxLength={2000}
              value={sessionIntent} disabled={sessionBusy}
              placeholder="例如：给 tests/test_calc.py 补上缺失的边界断言"
              onChange={e => setSessionIntent(e.target.value)} />
            <div className="repair-actions">
              <button className="primary" type="submit" disabled={sessionBusy}>开启编辑会话</button>
            </div>
            {sessionNotice
              && <div className="repair-warning" role="alert">会话操作没有完成：{sessionNotice}。台账没有变化，这里不会假装操作已经生效。</div>}
            <small className="repair-note">开启时会冻结当前源码快照作为比对锚；之后源码再变化，这个会话自动失效，旧补丁不会盖到新树上。</small>
          </form>
          {sessionLedger.length === 0
            ? <p className="empty">还没有编辑会话记录。要走受控修改流程，先用上方表单开启一个会话。</p>
            : sessionLedger.map((session, index) => {
              const stateView = editSessionStateView(session.status);
              const candidates = session.candidates || [];
              const transitions = session.transitions || [];
              return <article className="es-card" key={session.session_id || `es-${index}`} >
                <header>
                  <code>{session.session_id}</code>
                  <strong className={`repair-stamp repair-${stateView.tone}`}>{stateView.label}</strong>
                  {(session.status === "open" || session.status === "patch_candidate") && session.session_id
                    && <button className="download" type="button" disabled={Boolean(abandonBusyId)}
                        onClick={() => void abandonEditSession(String(session.session_id))}>
                        {abandonBusyId === session.session_id ? "正在放弃…" : "放弃这个会话"}
                      </button>}
                </header>
                <dl className="repair-record">
                  <div><dt>这次修改的目的</dt><dd>{session.intent || "—"}</dd></div>
                  <div><dt>锁定的源快照指纹</dt><dd>{(() => {
                    const digest = shortHash(session.snapshot_sha256 || "—");
                    return <code title={digest.full}>{digest.short}{digest.truncated ? "…" : ""}</code>;
                  })()}</dd></div>
                </dl>
                {candidates.length > 0 ? candidates.map((candidate, itemIndex) => {
                  const verdictText = String(candidate.verdict || "");
                  const rejectionText = verdictText.startsWith("rejected")
                    ? deliveryRejectionText(verdictText.slice(verdictText.indexOf(":") + 1)) : "";
                  return <div className="es-candidate" key={candidate.patch_sha256 || itemIndex}>
                    <b>候选补丁 {itemIndex + 1}</b>
                    <span>补丁指纹 <code title={candidate.patch_sha256}>{(candidate.patch_sha256 || "—").slice(0, 12)}{(candidate.patch_sha256 || "").length > 12 ? "…" : ""}</code></span>
                    <span>判定 {candidateVerdictView(candidate.verdict)}{rejectionText ? `（${rejectionText}）` : ""}</span>
                    <span>测试结果 {candidateTestResultView(candidate.test_result)}</span>
                  </div>;
                }) : <p className="empty">这个会话还没有候选补丁入账。</p>}
                {transitions.length > 0 && <p className="es-transitions">状态流转：{transitions.map((transition, transitionIndex) => {
                  const reason = editSessionExitReasonText(transition.reason);
                  return <span key={transitionIndex}>{transitionIndex > 0 ? "；" : ""}{editSessionTransitionText(transition.from, transition.to)}{reason ? `（${reason}）` : ""}</span>;
                })}</p>}
              </article>;
            })}
          {repairAuthorized
            ? <form className="es-approval" onSubmit={event => {
                event.preventDefault();
                void generateRepairCandidate();
              }}>
              <h5>生成候选补丁</h5>
              <p className="repair-detail">把发现编号和选中的代码片段交给模型，得到的是一份候选记录；生成与应用是两件事，候选不会自动变成交付。</p>
              <label htmlFor="candidate-finding-ids">发现编号（每行一个，或用逗号分隔）</label>
              <textarea id="candidate-finding-ids" rows={2} value={candidateFindingIds}
                disabled={candidateBusy} placeholder="例如：claim-1"
                onChange={e => setCandidateFindingIds(e.target.value)} />
              <label>代码片段（路径必须在本机审查范围内）</label>
              {snippetDrafts.map((draft, index) => <div className="es-snippet" key={index}>
                <input value={draft.path} disabled={candidateBusy} placeholder="pkg/core.py"
                  aria-label={`片段 ${index + 1} 的文件路径`}
                  onChange={e => setSnippetDrafts(list => list.map((item, itemIndex) =>
                    itemIndex === index ? {...item, path: e.target.value} : item))} />
                <input value={draft.start} disabled={candidateBusy} inputMode="numeric"
                  aria-label={`片段 ${index + 1} 的起始行`} placeholder="起始行"
                  onChange={e => setSnippetDrafts(list => list.map((item, itemIndex) =>
                    itemIndex === index ? {...item, start: e.target.value} : item))} />
                <textarea rows={3} value={draft.text} disabled={candidateBusy}
                  aria-label={`片段 ${index + 1} 的内容`} placeholder="选中要交给模型的代码"
                  onChange={e => setSnippetDrafts(list => list.map((item, itemIndex) =>
                    itemIndex === index ? {...item, text: e.target.value} : item))} />
                {snippetDrafts.length > 1 && <button type="button" className="download"
                    onClick={() => setSnippetDrafts(list =>
                      list.filter((_, itemIndex) => itemIndex !== index))}>移除这段</button>}
              </div>)}
              <div className="repair-actions">
                <button type="button" className="download" disabled={candidateBusy}
                  onClick={() => setSnippetDrafts(list => [...list, {path: "", start: "", text: ""}])}>再添一段</button>
                <button className="primary" type="submit"
                  disabled={candidateBusy || candidateSnippetBytes > 12 * 1024}>生成候选补丁</button>
              </div>
              <small className="repair-note">片段内容合计 {candidateSnippetBytes} / 12288 字节；超过 12 KiB 会被服务端整包拒绝。</small>
              {candidateNotice
                && <div className="repair-warning" role="alert">候选没有生成：{candidateNotice}。没有新的入账，这里不会假装生成过。</div>}
            </form>
            : <div className="repair-warning">本次冻结计划没有授权创建修复分支，不能在这里生成候选补丁；需要授权时请回到审查配置勾选后重新发起。</div>}
          {repairCandidate && <div className="es-delivery">
            <h5>最新候选补丁记录</h5>
            <dl className="repair-record">
              <div><dt>候选摘要</dt><dd>{repairCandidate.summary || "—"}</dd></div>
              <div><dt>补丁指纹</dt><dd>{(() => {
                const digest = shortHash(repairCandidate.patch_sha256 || "—");
                return <code title={digest.full}>{digest.short}{digest.truncated ? "…" : ""}</code>;
              })()}</dd></div>
              <div><dt>补丁字节数</dt><dd>{repairCandidate.patch_bytes ?? "—"}</dd></div>
              <div><dt>保留时限</dt><dd>{repairCandidate.retention_days != null ? `${repairCandidate.retention_days} 天` : "—"}</dd></div>
              <div><dt>候选状态</dt><dd>{repairCandidate.status || "—"}</dd></div>
            </dl>
            <p className="repair-note">补丁正文保存在服务端 repair/candidate.patch，界面不直接展示。核对后把你认可的补丁粘贴到上方「本地修复交付」的输入框，人工点击交付才会创建本地分支。</p>
          </div>}
          {deliveryRecords.length > 0 ? deliveryRecords.map(session => {
            const approval = session.delivery_approval || null;
            const fingerprints = (approval?.fingerprints || {}) as Record<string, string>;
            return <div className="es-delivery" key={`${session.session_id || "session"}-delivery`}>
              <h5>交付记录（{session.session_id}）</h5>
              {!approval
                ? <form className="es-approval" onSubmit={event => {
                    event.preventDefault();
                    void approveDelivery(String(session.session_id || ""));
                  }}>
                  <p className="repair-detail">这次交付还没有批准记录。点击下方按钮会把当前五份指纹冻结成比对基线：只记下基线，不应用改动。</p>
                  <p className="repair-detail">之后导出时会全部重算，任何一份对不上都会拒绝交付。</p>
                  <label htmlFor="delivery-approval-note">批准备注（可选，最多 200 字）</label>
                  <textarea id="delivery-approval-note" rows={3} maxLength={200}
                    value={approvalNote} disabled={approvalBusy}
                    placeholder="例如：已逐份核对五指纹与交付分支（可选）"
                    onChange={e => setApprovalNote(e.target.value)} />
                  <div className="repair-actions">
                    <button className="primary" type="submit" disabled={approvalBusy}>批准五指纹比对基线</button>
                  </div>
                  {approvalNotice
                    && <div className="repair-warning" role="alert">批准没有完成：{approvalNotice}。五指纹没有被冻结，这里不会假装批准过。</div>}
                </form>
                : null}
              {approval && <>
                <dl className="repair-record">
                  <div><dt>批准时间</dt><dd>{approval.at || "—"}</dd></div>
                  <div><dt>交付分支</dt><dd><code>{approval.branch || "—"}</code></dd></div>
                  <div><dt>交付提交</dt><dd><code>{approval.commit || "—"}</code></dd></div>
                </dl>
                <dl className="es-fingerprints">
                  {DELIVERY_FINGERPRINT_VIEWS.map(field => {
                    const digest = shortHash(fingerprints[field.key] || "—");
                    return <div key={field.key}><dt>{field.label}</dt>
                      <dd><code title={digest.full}>{digest.short}{digest.truncated ? "…" : ""}</code></dd></div>;
                  })}
                </dl>
                <p className="repair-note">以上是批准时记下的五份指纹；真正交付时会全部重新计算并逐一比对，任何一份对不上都会拒绝。</p>
                {!session.delivery_export
                  && <form className="es-approval" onSubmit={event => {
                    event.preventDefault();
                    void exportDelivery(String(session.session_id || ""));
                  }}>
                    <p className="repair-detail">点击导出前，服务端会重新计算五份指纹，并与上面的批准基线逐一比对；全部一致才生成内容哈希清单。</p>
                    <div className="repair-actions">
                      <button className="primary" type="submit"
                        disabled={exportBusyId !== ""}>
                        {exportBusyId === String(session.session_id || "")
                          ? "正在导出…" : "导出交付清单"}
                      </button>
                    </div>
                    {exportNotice
                      && <div className="repair-warning" role="alert">导出没有完成：{exportNotice}。清单没有生成，这里不会假装导出过。</div>}
                  </form>}
              </>}
              <p className="repair-note">{session.delivery_export
                ? <>已导出交付清单（清单指纹 <code>{(session.delivery_export.manifest_sha256 || "—").slice(0, 12)}…</code>）。导出物是{DELIVERY_DISCLAIMER}。</>
                : <>交付物是{DELIVERY_DISCLAIMER}；清单尚未导出。</>}</p>
            </div>;
          })
            : <p className="empty">尚无交付记录。只有你在完整流程里明确批准、且五份指纹全部比对一致后，这里才会出现交付入账。</p>}
          <details className="es-codes">
            <summary>交付被拒时的拒绝码对照</summary>
            <ul>
              {DELIVERY_REJECTION_CODES.map(code => <li key={code}>
                <code>{code}</code>：{deliveryRejectionText(code) || "（该码暂无中文对照）"}
              </li>)}
            </ul>
          </details>
        </section>}
      {review?.request?.schema_version === "review-request-v2" && !offlineReplay &&
        <section className="visa-delivery" aria-label="补测签证">
          <div className="repair-heading">
            <div><span className="passport-kicker">EFFECTIVE TEST VISA</span>
              <h4>补测签证</h4>
              <p>跑绿只是第一道闸：模型提议的测试还必须在冻结的保留集干预上复现断言级失败（k=2 一致）才算有效补测。</p>
              <p>生成与修订请求永远看不到保留集。</p></div>
            <strong className={`repair-stamp visa-${proposalView.tone}`}>{proposalView.label}</strong>
          </div>
          <p className="repair-detail">{proposalView.detail}</p>
          {proposalRecord ? <>
            <dl className="repair-record">
              <div><dt>候选文件</dt><dd><code>{String(proposalRecord.path || "—")}</code></dd></div>
              <div><dt>执行轮次</dt><dd>{String((proposalRecord.attempts || []).length)} 轮（含修订 {String(proposalRecord.revisions || 0)} 次）</dd></div>
              {proposalVisa && <div><dt>保留集复现</dt><dd>{
                String((proposalVisa.holdout_stage as Record<string, unknown> | undefined)?.signed ?? "—")
                + " / " + String((proposalVisa.holdout_stage as Record<string, unknown> | undefined)?.denominator ?? "—")
                + " 条干预签 A"}</dd></div>}
              <div><dt>有效补测</dt><dd>{proposalRecord.effective === true ? "是 · 已判定有效"
                : proposalRecord.effective === false ? "否" : "未判定"}</dd></div>
            </dl>
            {proposalRecord.question && <div className="repair-warning">{proposalRecord.question}</div>}
            <div className="visa-attempts">
              {(proposalRecord.attempts || []).map(attempt => <div key={attempt.round}
                className={`visa-attempt tone-${attempt.visa_status ? visaStatusView(attempt.visa_status).tone : "idle"}`}>
                <b>第 {attempt.round} 轮</b>
                <code>{attempt.path}</code>
                <span>{attempt.returncode === 0 ? "隔离执行跑绿"
                  : `隔离执行未跑绿（退出码 ${String(attempt.returncode ?? "?")}）`}</span>
                <em>{attempt.visa_status ? visaStatusView(attempt.visa_status).label : "未进入签证"}</em>
              </div>)}
            </div>
          </> : <p className="empty">这次审查还没有测试提议记录。{proposalEligible
            ? "在下方逐行证据视图点开「无据」行即可发起。"
            : "本次运行未调用模型，不能发起测试提议。"}</p>}
          <div className="repair-actions">
            <button className="download" disabled={proposalBusy} onClick={() => void refreshTestProposal()}>重新读取状态</button>
          </div>
          <small className="repair-note">候选代码只存在于一次性隔离工作树，Git 永不为其建 commit。</small>
          <small className="repair-note">未通过补测签证的候选如实标「无效」，不会改写成通过。</small>
        </section>}
      {resultRanAsAgent && <div className={`model-participation ${participation.live ? "is-live" : "is-deterministic"}`} aria-label="模型参与记录">
        <h4>模型参与记录</h4>
        {participation.live ? <dl>
          <div><dt>参与状态</dt><dd>{participation.stateLabel}</dd></div>
          <div><dt>模型参与</dt><dd>{participation.live ? "是" : "否（确定性调度）"}</dd></div>
          <div><dt>模型</dt><dd>{participation.modelLabel}</dd></div>
          <div><dt>Agent 级别</dt><dd>{participation.agentLevel}</dd></div>
          <div><dt>模型调用</dt><dd>{participation.calls} 次</dd></div>
          <div className="stage-calls"><dt>分阶段调用</dt><dd>
            {["plan", "scheduling", "narration", "recommendation"].map(stage =>
              <span key={stage}>{({plan: "计划", scheduling: "运行中调度",
                narration: "证据编排", recommendation: "改进建议"} as Record<string, string>)[stage]}
                {Number(participation.requestsByStage[stage] || 0)}</span>)}
          </dd></div>
          <div><dt>调度动作</dt><dd>重排 {participation.reorderCount} 次</dd></div>
          <div><dt>顺序变化</dt><dd>{participation.orderChanges.join("；") || "—"}</dd></div>
          <div><dt>依据</dt><dd>{participation.reasons.join("；") || "—"}</dd></div>
          <div><dt>降级</dt><dd>{participation.fallbacks} 次</dd></div>
          <div><dt>自主与升级</dt><dd title={autonomy?.definition || undefined}>{autonomy
            ? `自主 ${autonomy.autonomous_decisions} 次 · 升级 ${autonomy.escalations} 次 · 升级率 ${(autonomy.escalation_rate * 100).toFixed(1)}%`
            : "未记录（旧版证据包）"}</dd></div>
          <div><dt>策略拒绝</dt><dd>{participation.policyRejections} 次</dd></div>
          <div><dt>建议阶段</dt><dd>{participation.recommendationFailed
            ? "生成失败（不影响实验结论）"
            : participation.recommendations?.generated
              ? `已生成 ${participation.recommendations.items.length} 条`
              : "未生成"}</dd></div>
        </dl> : <div className="deterministic-participation">
          <strong>本次未调用模型</strong>
          <span>{schedulingMode} · 0 次模型调用 · 0 次降级</span>
          {autonomy && <span>自主 {autonomy.autonomous_decisions} 次 · 升级 {autonomy.escalations} 次</span>}
        </div>}
        <p>模型只调整检查顺序；测试结果、证据结论和恢复校验由水木验码执行。</p>
      </div>}
      {resultRanAsAgent && participation.live && <section className="model-transparency" aria-label="模型透明记录">
        <div className="model-transcript">
          <h4>模型完整聊天记录</h4>
          <p className="transcript-model-note">本次使用：{displayModelLabel}</p>
          <p className="recommendations-note">逐次展示完整输入与原始输出 · 不含 API Key、请求头或服务地址</p>
          {modelCalls.length > 0 ? modelCalls.map(call => <details key={call.call}>
            <summary>调用 #{call.call} · {({plan: "计划", scheduling: "运行中调度",
              narration: "证据编排", recommendation: "改进建议"} as Record<string, string>)[call.stage] || call.stage}
              <span>{call.http_status || call.error_type || "—"} · {String(call.latency_ms || 0)}ms</span></summary>
            {call.messages.map((message, index) => <div className="chat-message" key={index}>
              <strong>{message.role === "system" ? "系统输入" : "用户输入"}</strong>
              <pre>{typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2)}</pre>
            </div>)}
            <div className="chat-message response"><strong>模型原始输出</strong>
              <pre>{call.raw_response || `调用失败：${call.error_type || "无响应"}`}</pre></div>
          </details>) : <div className="empty">这份结果没有可用的完整聊天记录；旧版运行只保存了摘要。</div>}
        </div>
      {presentation.showRecommendations && resultRanAsAgent &&
        <div className="recommendations" aria-label="模型改进建议（只读）">
        <h4>模型改进建议（只读）</h4>
        <p className="recommendations-note">模型的原始建议，界面逐字呈现、不做过滤 · 最多 3 条 · 未执行</p>
        <p className="recommendations-boundary">AI 建议 · 未执行 · 不是三态结论 · 需要人工审阅</p>
        {legacyBundle ? <div className="legacy-note">旧版证据包未记录模型决策上下文。</div>
          : recommendations?.failureReason ? <div className="failed">
            建议生成失败，不影响实验结论（{recommendations.failureReason}）。</div>
          : recommendations && recommendations.items.length > 0 ? <ol>
            {recommendations.items.map((item, index) => <li key={index}>
              <strong>{item.action}</strong>
              <code>{item.anchor_id}</code>
              {item.line_refs.length > 0 && <span>{item.line_refs.join("、")}</span>}
              <small>证据：{item.evidence_ids.join("、") || "—"}</small>
              <p>{item.rationale}</p>
              <p className="verification">验证：{item.verification}</p>
            </li>)}
          </ol>
          : <div className="empty">{recommendations?.skippedReason === "deterministic_run"
            ? "确定性运行不生成模型建议。" : "现有证据不足以支撑行动建议，未生成建议。"}</div>}
      </div>}
      </section>}
      <div className="completion-boundary"><h4>结论边界</h4><ul>{uncovered.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
      <div className="completion-actions">
        <label className="replay">打开离线 ReviewBundle<input type="file" accept="application/json"
          onChange={e => e.target.files?.[0] && loadReplay(e.target.files[0])}/></label>
      </div>
    </section>}

    {activeStage === 4 && <section className="diff-panel">
      <div className="panel-title"><span>05</span><h3>查看代码证据</h3><small>{diffLines.length} 行新增代码</small></div>
      {featuredLine && <section className="evidence-spotlight" aria-label="第一条可追溯结论">
        <div className="spotlight-code">
          <div><span>第一条可追溯结论</span><code>{featuredLine.file}:{featuredLine.line}</code></div>
          <pre><b>{String(featuredLine.line).padStart(3, " ")}</b>
            <span>{(featuredLine.text?.trim() ? featuredLine.text : featuredSourceLine)
              || (offlineReplay
                ? "离线摘要未包含源码文本；如需查看该行，请载入本次审查的源码快照。"
                : featuredSourceLine === null
                  ? "正在读取本次审查的只读源码快照…"
                  : "源码快照未返回这一行；请进入只读工作台核对。")}</span></pre>
        </div>
        <div className="spotlight-explanation">
          <span className={`spotlight-verdict ${linePresentation(featuredLine).className}`}>
            {linePresentation(featuredLine).badge}</span>
          <h4>结论是什么</h4>
          <p>{featuredLine.label === "承重"
            ? "这行新增代码拥有可复验的测试承重证据。"
            : `这行当前被判为“${linePresentation(featuredLine).badge}”。`}</p>
          <h4>为什么</h4><p>{focusExplanation(featuredLine)}</p>
          <dl>
            <div><dt>具名测试</dt><dd>{featuredTests[0] || "当前证据没有签发具名测试"}</dd></div>
            <div><dt>证据等级</dt><dd className="spotlight-grade">
              <strong>{featuredGrade
                ? `${featuredGrade.grade}（${featuredGrade.label.replace(/^. 级 · /, "")}）`
                : "未定级"}</strong>
              {featuredGrade && <small>{featuredGrade.detail}</small>}
            </dd></div>
          </dl>
          {review && <button type="button" className="workbench-open spotlight-open"
            onClick={() => {
              setWorkbenchFocus(current => ({path: featuredLine.file, line: featuredLine.line,
                nonce: (current?.nonce ?? 0) + 1}));
              setWorkbenchOpen(true);
            }}>进入全屏工作台核验</button>}
        </div>
      </section>}
      <section className="review-next" aria-label="核验证据之后">
        <div>
          <strong>核验证据之后</strong>
          <p>有疑问，可在全屏工作台逐行质疑；需要处置，可进入第 6 步选择补测、受控修复或接受风险。采用精确变更需要你明确确认，随后生成新版本复验记录。</p>
        </div>
        <div className="review-next-actions">
          {review && reviewBundle && <button type="button" onClick={downloadBundle}>下载完整证据包</button>}
          <button type="button" className="primary" onClick={() => goToStage(5)}>进入处置与复验</button>
          <button type="button" onClick={() => goToStage(3)}>查看结论与交付</button>
        </div>
      </section>
      <details className="diff-secondary" open>
        <summary>全部逐行证据与筛选 <span>{filterCountLabel(diffLines.length, visibleLines.length)}</span></summary>
      <div className="diff-head"><div><div className="legend"><i className="dot-load"/>承重<i className="dot-unevidenced"/>无据
        <i className="dot-drift"/>游离<i className="dot-unlabeled"/>未标注</div>
        <div className="term-notes" aria-label="术语说明">
          {/* #43 术语表：点击展开一句话解释；不用 tooltip（触屏不可见）。 */}
          {TERM_NOTES.map(item => <details key={item.term} className="term-note">
            <summary><b>{item.term}</b></summary>
            <p>{item.note}</p>
          </details>)}
        </div>
        {Number(((summary?.by_reason || {}) as Record<string, number>).inert_withheld || 0) > 0 &&
          <div className="guardrail"><strong>护栏拦截的能力</strong><span>{String(((summary?.by_reason || {}) as Record<string, number>).inert_withheld)} 行惰性结论已扣下，不进入正式三态结论。</span></div>}
        </div>
        <div className="diff-filters" role="group" aria-label="结论筛选">
          <label>三态
            <select aria-label="按三态筛选" value={conclusionFilter.label}
              onChange={event => setConclusionFilter(
                {...conclusionFilter, label: event.target.value})}>
              {LABEL_FILTER_OPTIONS.map(option =>
                <option key={option || "all"} value={option}>{option || "全部"}</option>)}
            </select></label>
          <label>文件
            <select aria-label="按文件筛选" value={conclusionFilter.file}
              onChange={event => setConclusionFilter(
                {...conclusionFilter, file: event.target.value})}>
              <option value="">全部</option>
              {fileOptions(diffLines).map(file =>
                <option key={file} value={file}>{file}</option>)}
            </select></label>
          <label>可采性
            <select aria-label="按可采性筛选" value={conclusionFilter.grade}
              onChange={event => setConclusionFilter({...conclusionFilter,
                grade: event.target.value as ConclusionFilter["grade"]})}>
              {GRADE_FILTER_OPTIONS.map(option =>
                <option key={option.value || "all"} value={option.value}>{option.label}</option>)}
            </select></label>
          <small className="diff-filter-count">
            {filterCountLabel(diffLines.length, visibleLines.length)}</small>
          {isFiltering(conclusionFilter) && <button type="button" className="diff-filter-reset"
            onClick={() => setConclusionFilter(EMPTY_FILTER)}>清除筛选</button>}
        </div>
        <p className="diff-kbd-hint">键盘：j / k 在结论间移动 · Enter 打开证据 · Esc 关闭</p>
        <div className="diff-head-actions">
          <div className="scope-boundary"><strong>始终记住</strong>{uncovered.slice(0, 2).map((item, i) => <span key={i}>{item}</span>)}
            {uncovered.length > 2 && <span>还有 {uncovered.length - 2} 条，完整清单见「04 理解审查结论」</span>}</div>
        </div></div>
      <div className="diff-lines">{diffLines.length === 0 && <div className="empty">完成审查后，这里会显示完整文件入口、问题行与对应证据；当前还没有逐行证据。</div>}{diffGroups.map(group => group.kind === "drift"
        // 游离是**文件级**结论：整份文件不在测试与引用图里。逐行重复同一个
        // 证据 ID 十几次既是视觉噪音，也让「N 行判为游离」显得虚。折成一条带。
        ? <button key={`drift:${group.file}`} type="button" className="diff-file-band label-游离"
            disabled={!decisiveEvidenceId(group.lines[0], reviewBundle)}
            onClick={() => inspectLine(group.lines[0])}
            title={decisiveEvidenceId(group.lines[0], reviewBundle)
              ? "查看这个游离文件的证据记录" : "旧 Bundle 不含逐行证据链接"}>
            <b>游离</b><code>{group.file}</code>
            <span>整个文件不被测试收集、无静态引用 · {group.lines.length} 行</span>
            <small>{decisiveEvidenceId(group.lines[0], reviewBundle) || "无逐行证据链接"}</small>
          </button>
        : group.lines.map((line, index) => {
            const presentation = linePresentation(line);
            const evidence = decisiveEvidenceId(line, reviewBundle);
            return <button type="button" key={`${line.file}:${line.line}:${index}`}
              data-nav-line={`${line.file}:${line.line}`}
              className={`diff-line ${presentation.className}${evidence ? "" : " inert"}${
                selectedLine?.file === line.file && selectedLine?.line === line.line ? " active" : ""}${
                navLine?.file === line.file && navLine?.line === line.line ? " kbd-focus" : ""}`}
              disabled={!evidence} onClick={() => inspectLine(line)}
              title={evidence ? "查看这一行的证据记录" : "旧 Bundle 或该行没有逐行证据链接"}>
              <b>{presentation.badge}{presentation.inherited &&
                <i className="inherit" title="非执行行，继承所在单元的结论">继承</i>}</b>
              <code>{line.file}:{line.line}</code><span className="line-code">{line.text || " "}</span>
              <small>{reasonLabel(line.reason) || evidence || ""}</small>
            </button>;
          }))}</div>
      </details>
    </section>}

    {workbenchOpen && review && (
      <div className={activeStage === 4 && presentation.showCurrentResult
        ? "workbench-overlay" : "journey-hidden"} role="dialog" aria-modal="true"
        aria-label="证据代码工作台聚焦层">
        <Suspense fallback={<section className="workbench" aria-busy="true"
          aria-label="正在装载只读代码工作台">正在装载只读代码工作台…</section>}>
          <CodeWorkbench
          reviewId={review.review_id} lines={diffLines}
          ledger={reviewBundle?.evidence_bundle?.ledger} focus={workbenchFocus}
          theme={theme}
          fetchTree={() => api<SourceTree>("/api/v2/reviews/" + review.review_id + "/source/tree")}
          fetchFile={path => api<SourceFile>("/api/v2/reviews/" + review.review_id
            + "/source/file?path=" + encodeURIComponent(path))}
          fetchComments={path => api<CommentList>("/api/v2/reviews/"
            + review.review_id + "/comments?path=" + encodeURIComponent(path))}
          createComment={input => api<CommentRecord>(
            "/api/v2/reviews/" + review.review_id + "/comments",
            {method: "POST", body: JSON.stringify(input)})}
          replyComment={(commentId, input) => api<CommentRecord>(
            "/api/v2/reviews/" + review.review_id + "/comments/"
            + commentId + "/replies",
            {method: "POST", body: JSON.stringify(input)})}
          updateComment={(commentId, input) => api<CommentRecord>(
            "/api/v2/reviews/" + review.review_id + "/comments/"
            + commentId,
            {method: "PATCH", body: JSON.stringify(input)})}
          fetchReverifications={() => api<ReverificationList>(
            "/api/v2/reviews/" + review.review_id + "/reverifications")}
          requestReverification={input => api<ReverificationRecord>(
            "/api/v2/reviews/" + review.review_id + "/reverifications",
            {method: "POST", body: JSON.stringify(input)})}
          approveReverification={reverificationId =>
            api<ReverificationRecord>("/api/v2/reviews/" + review.review_id
              + "/reverifications/" + reverificationId + "/approval",
              {method: "POST", body: "{}"})}
          rejectReverification={(reverificationId, note) =>
            api<ReverificationRecord>("/api/v2/reviews/" + review.review_id
              + "/reverifications/" + reverificationId + "/rejection",
              {method: "POST", body: JSON.stringify(note ? {note} : {})})}
          onOpenTimeline={() => goToStage(2)}
          onOpenEvidence={line => void inspectLine(
            {...line, text: line.text ?? "", label: line.label ?? ""})}
            onClose={() => setWorkbenchOpen(false)} />
        </Suspense>
      </div>)}

    {activeStage === 5 && <TaskClosure key={review?.review_id || "no-review"}
      reviewId={review?.review_id} reviewComplete={!!review && terminal.has(review.state.status)}
      offline={offlineReplay} ranAsAgent={resultRanAsAgent} findings={diffLines}
      preferredTaskId={preferredTaskId} api={api}
      onOpenReview={id => void openHistoryReview(id)} onOpenRepair={() => goToStage(3)}
      onNewAgentReview={() => {changeProductMode("agent"); enterNextRunSetup(); goToStage(0);}} />}

    {capabilities.length > 0 &&
      <section className="panel capabilities">
      <details className="panel-fold" open={productMode === "agent"}>
        <summary className="panel-title"><h3>能力状态</h3>
          <small>门槛先冻结，再测量</small></summary>
      <p className="field-note">状态由预先冻结的门槛判定。没过门槛的能力保留为负结果或关闭，
        既不改写成通过，也不从这里删掉。</p>
      <p className="field-note">发布链用同一份状态校验对外材料的措辞，
        所以这块屏幕不可能比证据包说得多。</p>
      {groupCapabilities(capabilities).map(group =>
        <div key={group.state} className="capability-group">
          <h4><span className={`capability-badge tone-${capabilityTone(group.state)}`}>
            {group.label}</span><b>{group.items.length} 项</b></h4>
          {group.items.map(item => <div key={item.id} className="capability-row">
            <strong>{item.title}</strong>
            {item.runtime === "unavailable" && <em>不可开启</em>}
            <span>{item.summary}</span>
            <small>门槛：{item.gate}</small>
          </div>)}
        </div>)}
      </details>
    </section>}

    <nav className="journey-pager" aria-label="审查阶段翻页">
      <button type="button" disabled={activeStage === 0}
        onClick={() => goToStage(activeStage - 1)}>上一步</button>
      <span>{String(activeStage + 1).padStart(2, "0")} / 06 · {JOURNEY_STAGES[activeStage].label}</span>
      <button type="button" disabled={activeStage === JOURNEY_STAGES.length - 1}
        onClick={() => goToStage(activeStage + 1)}>下一步</button>
    </nav>

    {/* 6.4 智能体在做什么：五相各一条关键输入、一条产出、一个人工门，
        宽窄屏都放在翻页器旁——右侧轨道收起来时，这份事实不跟着消失。 */}
    <section className="stage-pulse" aria-label="智能体在做什么">
      <h3 className="pulse-title">智能体在做什么</h3>
      <ol className="pulse-list">
        {AGENT_PULSE.map((phase, index) => <li key={phase.key}
          className={`pulse-phase pulse-${pulseStates[index]}`}>
          <span className="pulse-no" aria-hidden="true"><i />{phase.no}</span>
          <div className="pulse-body">
            <strong>{phase.label}
              <b className="pulse-state">{STAGE_STATE_LABELS[pulseStates[index]]}</b></strong>
            <p><span className="pulse-k">输入</span>{phase.input}</p>
            <p><span className="pulse-k">产出</span>{phase.output}</p>
            <p className={`pulse-gate${phase.gate ? "" : " is-none"}`}>
              <span className="pulse-k">人工门</span>
              {phase.gate ?? (phase.no === "02" ? "无 · 纯机器段" : "无")}</p>
          </div>
        </li>)}
      </ol>
    </section>

    {selectedLine && <button className="drawer-scrim" aria-label="关闭证据抽屉"
      onClick={() => setSelectedLine(null)} />}
    {selectedLine && <aside className="drawer">
      <button aria-label="关闭证据抽屉" onClick={() => setSelectedLine(null)}>×</button>
      <p>{linePresentation(selectedLine).badge}</p>
      <h3>{selectedLine.file}:{selectedLine.line}</h3>
      <pre className="drawer-code">{selectedLine.text || " "}</pre>
      {review && <button type="button" className="workbench-jump"
        title="在只读代码工作台中打开这一行，查看 gutter 标记与具名测试"
        onClick={() => {
          setWorkbenchFocus(f => ({path: selectedLine.file,
            line: selectedLine.line, nonce: (f?.nonce ?? 0) + 1}));
          setWorkbenchOpen(true); setSelectedLine(null);
        }}>在代码工作台中查看</button>}
      {evidenceDetail ? <EvidenceCard record={evidenceDetail} bundle={reviewBundle}/>
        : <p className="empty">这一行没有对应的账本记录{
            reasonLabel(selectedLine.reason) ? `（${reasonLabel(selectedLine.reason)}）` : ""}。</p>}
      {(selectedLine.label === "无据" || selectedLine.label === "未标注") && <div className="gap-action">
        <button type="button" className="primary" disabled={proposalBusy || !proposalEligible}
          title={proposalEligible ? "提议 → 隔离执行 → 保留集签证"
            : "需要智能体审查模式下已完成的 v2 审查"}
          onClick={() => void proposeTestForGap(`${selectedLine.file}:${selectedLine.line}`)}>
          {proposalBusy ? "正在生成并验证…" : "为这个缺口生成并验证测试"}
        </button>
        <small>{proposalEligible
          ? "模型只提议；候选要在隔离工作树跑绿，并在冻结的保留集干预上复现断言级失败（k=2 一致）才算有效补测。全程只读你的 checkout。"
          : "测试提议需要智能体审查（live provider）且审查已完成；确定性运行与离线回放不发起。"}</small>
        {proposalNotice && <span className="gap-notice">{proposalNotice}</span>}
      </div>}
      <div className="scope-boundary">{uncovered.slice(0, 2).map((item, i) =>
        <span key={i}>{item}</span>)}
        {uncovered.length > 2 && <span>还有 {uncovered.length - 2} 条，完整清单见「04 理解审查结论」</span>}</div>
    </aside>}

    {selected && <button className="drawer-scrim" aria-label="关闭证据抽屉"
      onClick={() => setSelected(null)} />}
    {selected && <aside className="drawer"><button aria-label="关闭证据抽屉" onClick={() => setSelected(null)}>×</button>
      <p>{eventLabel(selected.kind)}</p><h3>证据记录 #{selected.seq}</h3>
      <div className="event-summary"><span>发生时间</span><strong>{new Date(selected.occurred_at).toLocaleString("zh-CN")}</strong>
        <span>事件编号</span><strong>{selected.event_id}</strong></div>
      {evidenceDetail && <EvidenceCard record={evidenceDetail} bundle={reviewBundle}/>}
      <details><summary>原始事件数据</summary>
        <pre>{JSON.stringify({event: selected, evidence: evidenceDetail || null}, null, 2)}</pre>
      </details>
      <DispositionPanel
        event={selected}
        recorded={dispositions.filter(row => row.event_id === selected.event_id)}
        answer={dispositionAnswer} onAnswer={setDispositionAnswer}
        handler={dispositionHandler} onHandler={setDispositionHandler}
        note={dispositionNote} onNote={setDispositionNote}
        busy={dispositionBusy} canSubmit={Boolean(review)}
        onSubmit={() => void recordDisposition(selected)} />
    </aside>}

    {/* 评测回执 · 只读证据：回执是冻结评测的产物，这里只有索引与明细
        两种读取，没有任何写操作；FAIL 也原样展示，不粉饰。 */}
    <section className="receipt-browser" aria-label="评测回执">
      <div className="panel-title"><span>证</span><h3>评测回执 · 只读证据</h3>
        <small>{receiptIndex.length} 份冻结评测回执</small></div>
      <div className="receipt-body">
        <div className="receipt-index" role="list">
          {receiptIndex.map(entry => <button key={entry.id} type="button" role="listitem"
            className={entry.id === receiptSelectedId ? "active" : ""}
            onClick={() => showReceipt(entry.id)}>
            <strong>{entry.verdict || "—"}</strong>
            <span>{entry.id}</span>
            <small>{entry.criteriaPassed}/{entry.criteriaTotal} 判据通过 · {entry.finishedAt || "时间未知"}</small>
          </button>)}
          {receiptIndex.length === 0 && <div className="empty">暂无可展示的评测回执。</div>}
        </div>
        {receiptDetail ? <div className="receipt-detail">
          <div className={`receipt-verdict ${receiptDetail.verdict === "PASS" ? "pass" : "fail"}`}>
            <span>verdict</span><strong>{receiptDetail.verdictLabel}（{receiptDetail.verdict || "—"}）</strong>
            <small>{receiptDetail.criteriaPassed}/{receiptDetail.criteria.length} 判据通过
              {receiptDetail.smoke ? " · 冒烟轮" : ""}</small>
          </div>
          <div className="receipt-freeze">
            <span>冻结配置</span>
            <small>{receiptDetail.freezePath || "—"} @ {shortHash(receiptDetail.freezeSha256).short}</small>
            <small>{receiptDetail.startedAt || "—"} → {receiptDetail.finishedAt || "—"}</small>
          </div>
          <table className="receipt-criteria">
            <thead><tr><th>判据</th><th>observed</th><th>threshold</th><th>结果</th></tr></thead>
            <tbody>{receiptDetail.criteria.map(criterion => <tr key={criterion.id}
              className={criterion.passed ? "pass" : "fail"}>
              <td title={criterion.id}>{criterion.text}</td>
              <td>{criterion.observed}</td>
              <td>{criterion.comparison}</td>
              <td>{criterion.passed ? "通过" : "未通过"}</td>
            </tr>)}</tbody>
          </table>
          <table className="receipt-arms" aria-label="双臂对照">
            <thead><tr><th>对照臂</th><th>审查</th><th>人工批准</th><th>写入尝试</th><th>写入 verified</th></tr></thead>
            <tbody>{receiptDetail.arms.map(arm => <tr key={arm.arm}>
              <td>{arm.label}</td><td>{arm.reviews}</td>
              <td>{arm.humanApprovals}</td><td>{arm.writesAttempted}</td><td>{arm.writesVerified}</td>
            </tr>)}</tbody>
          </table>
        </div> : receiptSelectedId
          ? <div className="receipt-detail empty">正在读取回执明细…</div>
          : <div className="receipt-detail empty">选择左侧一份回执查看明细。</div>}
      </div>
    </section>
    <footer><div className="footer-runtime"><span>运行来源 · {runMode}</span><span>执行模式 · {executionMode}</span>
      <span>调度方式 · {displaySchedulingMode}</span><span>结论边界 · 仅限已声明测试范围，不代表语义等价</span></div>
      <div className="footer-meta">
        <p className="footer-author">作者 杨佩立（清华大学）· © 2026 杨佩立 版权所有</p>
        <p><strong>水木验码</strong>为学生参赛项目；Modou／水木验码为代码与历史实验沿用的技术标识。<br />
          本项目不代表清华大学官方产品、授权或合作背书。</p>
      </div></footer>


  </main>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><AppErrorBoundary><App /></AppErrorBoundary></StrictMode>);
