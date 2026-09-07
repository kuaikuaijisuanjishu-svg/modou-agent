import { StrictMode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import {
  COMMON_UNCOVERED, SESSION_REVIEW_KEY, SESSION_TOKEN_KEY, classifyError, eventLabel,
  linePresentation, normalizePastedToken, resolveStartupToken,
  schedulingDetail, statusView, summarySentence, decisiveEvidenceId, emphasizeNumbers,
  experimentStory, judgeCounts, minimizationViews, laneEvents, workspacePresentation,
  modeLabels, capabilityAvailable, capabilityTone, groupCapabilities, modelParticipation,
  modelConclusion, modelDecisionStages, evidencePassport, repairStatusView,
  type Capability, type UiNotice, type WorkspaceFocus,
} from "./presentation";

type Repo = { repo_id: string; display_name: string };
type Preset = {preset_id: string; display_name: string; description: string;
  repo_id: string; test_files: string[]; goal: string; budget_seconds: number;
  model_provider: string};
type EventRecord = {
  schema_version: string; review_id: string; event_id: string; seq: number;
  kind: string; occurred_at: string; data: Record<string, unknown>;
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
type Certificate = {
  unit_id?: string; 状态?: string; 主张?: string; 依据?: string[]; 方法?: string;
  口径?: string; 未覆盖?: string[]; 位置?: {file?: string; start?: number; end?: number};
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
  execution_mode?: string; scheduler_mode?: string; isolation_mode?: string;
};
type UiMode = "judge" | "research" | "model_research";
type ModelCall = {call: number; stage: string; messages: Array<{role: string; content: unknown}>;
  raw_response: string; http_status: number; latency_ms?: number; error_type?: string;
  request_sha256?: string; response_sha256?: string};
type ReleaseStatus = {
  schema_version: string; machine_status: "MACHINE_CHECKS_PASSED" | "MACHINE_CHECKS_FAILED";
  release_status: "INTERNAL_ONLY"; stable_eligible: false;
  external_gates_pending: string[]; source_commit: string;
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
    ? raw.machine_status : "MACHINE_CHECKS_FAILED";
  const pending = Array.isArray(raw.external_gates_pending)
    ? raw.external_gates_pending.filter(item => typeof item === "string")
    : [];
  return {
    schema_version: String(raw.schema_version || "v02-release-status-v1"),
    machine_status: machine,
    release_status: "INTERNAL_ONLY",
    stable_eligible: false,
    external_gates_pending: pending.length ? pending : EXTERNAL_GATES_FALLBACK,
    source_commit: String(raw.source_commit || ""),
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
  uiMode: UiMode;
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
};

const fragmentToken = new URLSearchParams(location.hash.slice(1)).get("token") || "";
let sessionToken = resolveStartupToken(location.hash, sessionStorage.getItem(SESSION_TOKEN_KEY));
if (fragmentToken) sessionStorage.setItem(SESSION_TOKEN_KEY, fragmentToken);
if (location.hash) history.replaceState(null, "", location.pathname + location.search);

// 结构化审查重点：只影响确定性的检查顺序，不能指挥三态结论。
const GOAL_PRESETS = [
  {id: "evidence-boundary", label: "综合检查新增代码的证据边界",
   goal: "综合检查这次补丁中新增代码的证据边界"},
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
  throw new Error("服务端已发出完成事件，但终态快照未及时可见");
}

function findCertificate(record: Record<string, unknown>, bundle: ReviewBundle | null): Certificate | undefined {
  const payload = (record.payload || {}) as Record<string, unknown>;
  const anchor = (payload.anchor || {}) as Record<string, unknown>;
  const path = String(anchor.path || "");
  const line = Number(anchor.line_start || 0);
  return bundle?.evidence_bundle?.report?.certificates?.find(cert => {
    const at = cert.位置 || {};
    return at.file === path && (!line || (Number(at.start || 0) <= line && line <= Number(at.end || 0)));
  });
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
    <section><h4>主张</h4><p>{cert?.主张 || String(narration?.text ||
      `在声明测试范围内，干预 ${String(anchor.path || "该代码单元")} 后观察到具名测试回归。`)}</p></section>
    <section><h4>依据</h4>{basis.length ? <ul>{basis.map((item, i) => <li key={i}>{item}</li>)}</ul> : <p>关联证据见下方证据编号。</p>}</section>
    <section><h4>方法</h4><p>{cert?.方法 || "对该代码单元实施可恢复干预，逐项比较声明测试状态向量，并复核工作区恢复。"}</p></section>
    <section><h4>口径</h4><p>{cert?.口径 || String(scope["范围"] || "结论仅适用于本次声明测试范围。")}</p></section>
    <section className="uncovered"><h4>未覆盖</h4><ul>{uncovered.map((item, i) => <li key={i}>{item}</li>)}</ul></section>
    <section><h4>状态</h4><p>{data.restore_clean === true ? "实验完成，恢复校验通过。" : "以证据账本中的实验终态为准。"}</p></section>
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
  const [uiMode, setUiMode] = useState<UiMode>("judge");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [repoId, setRepoId] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [tests, setTests] = useState("tests/test_backoff.py");
  const [goal, setGoal] = useState("综合检查这次补丁中新增代码的证据边界");
  const [goalPreset, setGoalPreset] = useState<string>("evidence-boundary");
  const [budget, setBudget] = useState(300);
  const [provider, setProvider] = useState("deterministic");
  const [probeStrategy, setProbeStrategy] = useState("hdd_inspired");
  const [allowRepairBranch, setAllowRepairBranch] = useState(false);
  const [repairPatch, setRepairPatch] = useState("");
  const [repairBusy, setRepairBusy] = useState(false);
  const [providerInfo, setProviderInfo] = useState<Record<string, unknown>>({});
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [releaseStatus, setReleaseStatus] = useState<ReleaseStatus | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [reviewBundle, setReviewBundle] = useState<ReviewBundle | null>(null);
  const [modelCalls, setModelCalls] = useState<ModelCall[]>([]);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [selected, setSelected] = useState<EventRecord | null>(null);
  const [selectedLine, setSelectedLine] = useState<DiffLine | null>(null);
  const [evidenceDetail, setEvidenceDetail] = useState<Record<string, unknown> | null>(null);
  const [replayEvidence, setReplayEvidence] = useState<Record<string, Record<string, unknown>>>({});
  const [diffLines, setDiffLines] = useState<DiffLine[]>([]);
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

  async function loadConfiguration() {
    if (!sessionToken) {
      setError(classifyError(401, "TOKEN_MISSING",
        "请粘贴终端启动信息中的完整地址或 #token= 后面的内容。"));
      return;
    }
    try {
      const [r, p, presetResponse, capabilityResponse, releaseResponse] = await Promise.all([
        api<{repos: Repo[]}>("/api/v1/repos"),
        api<Record<string, unknown>>("/api/v1/providers"),
        api<{presets: Preset[]}>("/api/v1/presets"),
        api<{capabilities: Capability[]}>("/api/v1/capabilities"),
        api<ReleaseStatus>("/api/v2/release-status"),
      ]);
      setCapabilities(capabilityResponse.capabilities);
      setRepos(r.repos); setRepoId(old => old || r.repos[0]?.repo_id || "");
      setPresets(presetResponse.presets);
      setPresetId(old => old || presetResponse.presets[0]?.preset_id || "");
      setProviderInfo(p); setError(null);
      setReleaseStatus(normalizeReleaseStatus(releaseResponse));
      const remembered = sessionStorage.getItem(SESSION_REVIEW_KEY);
      if (remembered) {
        try {
          setReview(await api<Review>(`/api/v1/reviews/${remembered}`));
          setWorkspaceFocus("current_result");
        } catch (restoreError) {
          if (restoreError instanceof HttpError && restoreError.status === 404) {
            sessionStorage.removeItem(SESSION_REVIEW_KEY);
          } else {
            throw restoreError;
          }
        }
      }
    } catch (e) { setError(noticeFor(e)); }
  }

  useEffect(() => { void loadConfiguration(); }, []);

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
  }, [review?.review_id, review?.state.status]);

  // 游离按文件折叠，其余保持逐行。分组只做展示，不改变任何结论。
  const diffGroups = useMemo(() => {
    const out: Array<{kind: "drift" | "lines"; file: string; lines: DiffLine[]}> = [];
    for (const line of diffLines) {
      const kind: "drift" | "lines" = line.label === "游离" ? "drift" : "lines";
      const last = out[out.length - 1];
      if (last && last.kind === kind && last.file === line.file) { last.lines.push(line); continue; }
      out.push({kind, file: line.file, lines: [line]});
    }
    return out;
  }, [diffLines]);

  const grouped = useMemo(() => events.map(event => ({
    ...event, label: eventLabel(event.kind),
    time: new Date(event.occurred_at).toLocaleTimeString("zh-CN", {hour12: false}),
  })), [events]);
  const visibleGrouped = useMemo(() => {
    // A historical live bundle can still be opened in judge/ordinary mode.
    // Keep those modes free of model calls, model actions and recommendation
    // output; AI 研究员 is the explicit opt-in transparency surface.
    if (uiMode === "model_research") return grouped;
    return grouped.filter(event => {
      if (event.kind.startsWith("model.") || event.kind.startsWith("recommendation.")) {
        return false;
      }
      return !(event.kind === "scheduler.next"
        && event.data?.selection === "model_reprioritized");
    });
  }, [grouped, uiMode]);
  const currentStatus = statusView(review?.state.status);
  const report = reviewBundle?.evidence_bundle?.report;
  const summary = report?.summary || null;
  const uncovered = report?.口径声明?.length ? report.口径声明 : COMMON_UNCOVERED;
  const isTerminal = !!review && terminal.has(review.state.status);
  const currentResultProvider = String(reviewBundle?.request?.model_provider
    || review?.request?.model_provider
    || (reviewBundle?.provider?.kind === "openai-compatible" ? "live" : "deterministic"));
  const presentation = workspacePresentation(
    workspaceFocus === "current_result" ? currentResultProvider : provider,
    review?.state.status,
    workspaceFocus,
  );
  const currentLanes = useMemo(() => laneEvents(visibleGrouped,
    uiMode === "model_research" && currentResultProvider === "live"),
    [visibleGrouped, currentResultProvider, uiMode]);
  const labels = modeLabels(reviewBundle as unknown as Record<string, unknown>, offlineReplay,
    review?.request);
  const executionMode = labels.isolation;
  const schedulingMode = labels.scheduler;
  const runMode = labels.run;
  const displaySchedulingMode = currentResultProvider === "live" && uiMode !== "model_research"
    ? "已生成证据" : schedulingMode;
  const setupLabels = modeLabels(null, false, {
    model_provider: provider,
    agent_level: providerInfo.agent_level,
    execution_mode: providerInfo.execution_mode,
  });
  const configLocked = presentation.configLocked;
  const canClearWorkbench = Boolean(!busy && review &&
    (terminal.has(review.state.status) || offlineReplay));
  const completionLabel = summary?.analysis_completion === "complete" ? "完整审查" :
    summary?.analysis_completion === "partial" ? "部分审查" : String(review?.state.status || "—");
  const participation = modelParticipation(reviewBundle as
    {provider?: Record<string, unknown>; model_metrics?: Record<string, unknown>;
     request?: Record<string, unknown>} | null, events,
    review?.request as Record<string, unknown> | undefined);
  const passport = evidencePassport(review?.state.status, summary, events,
    review?.plan || reviewBundle?.plan, reviewBundle?.evidence_bundle?.ledger || [],
    participation, Boolean(reviewBundle));
  const selectedPreset = presets.find(item => item.preset_id === presetId);
  const visiblePresets = presets.filter(item => uiMode === "model_research"
    ? item.model_provider === "live" : item.model_provider !== "live");
  const selectedPresetRepo = repos.find(item => item.repo_id === selectedPreset?.repo_id)?.display_name;
  const selectedRepoName = repos.find(item => item.repo_id === repoId)?.display_name || "";
  const demoGoalFits = DEMO_GOAL_FIT[selectedRepoName];
  const decisionStages = modelDecisionStages(events, participation.live, isTerminal);
  const modelActions = useMemo(() => events.filter(event =>
    // 纯确定性运行也写 model.action 事件（source=deterministic）用于审计；
    // DeepSeek 决策轨迹只消费 live 调用与真实降级两类。
    event.kind === "model.action" && event.data?.source !== "deterministic"), [events]);
  const latestModelAction = modelActions[modelActions.length - 1] || null;
  const planDraftEvent = events.find(event => event.kind === "plan.drafted");
  const planSource = (() => {
    const reason = String(planDraftEvent?.data?.fallback_reason || "");
    if (!planDraftEvent) return "尚未生成";
    if (currentResultProvider !== "live") return "确定性规则草拟";
    if (!reason) return "DeepSeek 模型草拟";
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
  const activeConstraints = (activeReviewSpec.constraints || {}) as Record<string, unknown>;
  const activeDataPolicy = (activeReviewSpec.data_policy || {}) as Record<string, unknown>;
  const activeOutputPolicy = (activeReviewSpec.output_policy || {}) as Record<string, unknown>;
  const activeScope = (activeReviewSpec.scope || {}) as Record<string, unknown>;
  const repairAuthorized = repairAuthorization?.allow_repair_branch === true;
  const repairView = repairStatusView(review?.repair?.status);
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
      workspaceFocus, uiMode, presetId, repoId, tests, goal, goalPreset, budget,
      provider, probeStrategy, allowRepairBranch, repairPatch, lastEvent: lastEvent.current,
      resultScrollTop: workspaceFocus === "current_result" ? window.scrollY : resultScrollTop.current,
    };
    if (undoTimer.current !== null) window.clearTimeout(undoTimer.current);
    undoTimer.current = window.setTimeout(() => {
      undoSnapshot.current = null;
      undoTimer.current = null;
      setUndoVisible(false);
    }, 10_000);
    setUndoVisible(true);
    setReview(null); setReviewBundle(null); setEvents([]); setDiffLines([]); setModelCalls([]);
    setSelected(null); setSelectedLine(null); setEvidenceDetail(null);
    setReplayEvidence({}); setOfflineReplay(false); setError(null); setBusy(false);
    setWorkspaceFocus("next_run_setup");
    setUiMode("judge"); setPresetId(presets[0]?.preset_id || "");
    setRepoId(repos[0]?.repo_id || ""); setTests("tests/test_backoff.py");
    setGoal("综合检查这次补丁中新增代码的证据边界"); setGoalPreset("evidence-boundary");
    setBudget(300); setProvider("deterministic"); setProbeStrategy("hdd_inspired");
    setAllowRepairBranch(false); setRepairPatch("");
    lastEvent.current = "";
    sessionStorage.removeItem(SESSION_REVIEW_KEY);
  }

  function undoClearWorkbench() {
    const snapshot = undoSnapshot.current;
    if (!snapshot) return;
    setReview(snapshot.review); setReviewBundle(snapshot.reviewBundle);
    setEvents(snapshot.events); setSelected(snapshot.selected);
    setSelectedLine(snapshot.selectedLine); setEvidenceDetail(snapshot.evidenceDetail);
    setReplayEvidence(snapshot.replayEvidence); setDiffLines(snapshot.diffLines);
    setError(snapshot.error); setOfflineReplay(snapshot.offlineReplay);
    setWorkspaceFocus(snapshot.workspaceFocus); setUiMode(snapshot.uiMode);
    setPresetId(snapshot.presetId); setRepoId(snapshot.repoId); setTests(snapshot.tests);
    setGoal(snapshot.goal); setGoalPreset(snapshot.goalPreset); setBudget(snapshot.budget);
    setProvider(snapshot.provider); setProbeStrategy(snapshot.probeStrategy);
    setAllowRepairBranch(snapshot.allowRepairBranch); setRepairPatch(snapshot.repairPatch);
    resultScrollTop.current = snapshot.resultScrollTop;
    lastEvent.current = snapshot.lastEvent;
    if (snapshot.review) sessionStorage.setItem(SESSION_REVIEW_KEY, snapshot.review.review_id);
    else sessionStorage.removeItem(SESSION_REVIEW_KEY);
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

  function changeUiMode(nextMode: UiMode) {
    setUiMode(nextMode);
    const wantsModel = nextMode === "model_research";
    setProvider(wantsModel ? (providerInfo.live_available ? "live" : "deterministic") : "deterministic");
    const candidate = presets.find(item => wantsModel
      ? item.model_provider === "live" : item.model_provider !== "live");
    if (candidate) setPresetId(candidate.preset_id);
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
    const useV2 = uiMode === "model_research" || allowRepairBranch;
    try {
      const next = await api<Review>(useV2 ? "/api/v2/reviews" : "/api/v1/reviews", {
        method: "POST",
        headers: {"Idempotency-Key": requestKey("review")},
        body: JSON.stringify(useV2 ? {
          instruction: goal,
          source: {kind: "local", repo_id: requestRepo},
          scope: requestTests.length ? {test_files: requestTests} : {},
          constraints: {budget_seconds: budget},
          autonomy_policy: {model_provider: provider, allow_repair_branch: allowRepairBranch,
                            allow_generated_tests: false},
          review_focus: focusForGoalText(goal, goalPreset),
          language: "python",
        } : {
          source: {kind: "local", repo_id: requestRepo},
          test_files: requestTests, declared_tests: [], goal: preset?.goal || goal,
          budget_seconds: preset?.budget_seconds || budget,
          probe_strategy: probeStrategy,
          review_focus: focusForGoalText(preset?.goal || goal, goalPreset),
          ui_mode: uiMode,
          // UI modes are execution contracts, not presentation skins:
          // judge + ordinary researcher are always zero-model runs.
          model_provider: "deterministic",
        }),
      });
      sessionStorage.setItem(SESSION_REVIEW_KEY, next.review_id);
      cancelUndoWindow();
      resultScrollTop.current = 0;
      setWorkspaceFocus("current_result");
      setReview(next);
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  function openCustomReview() {
    if (review && !terminal.has(review.state.status)) return;
    enterNextRunSetup();
    const starting = selectedPreset || presets.find(item => item.repo_id === repoId);
    if (starting) {
      setRepoId(starting.repo_id);
      setTests(starting.test_files.join("\n"));
      setGoal(starting.goal);
      setGoalPreset(GOAL_PRESETS.find(item => item.goal === starting.goal)?.id || "custom");
      setBudget(starting.budget_seconds);
      setProvider(starting.model_provider);
    } else {
      setTests("");
      setGoal("综合检查这次补丁中新增代码的证据边界");
      setGoalPreset("evidence-boundary");
      setBudget(300);
      setProvider("deterministic");
    }
    setProbeStrategy("hdd_inspired");
    setUiMode("research");
  }

  function selectCustomRepo(nextRepoId: string) {
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
    setProvider(uiMode === "model_research" ? "live" : "deterministic");
  }

  async function approve() {
    if (!review?.plan?.plan_sha256) return;
    setBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      setReview(await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/approval`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("approval")},
        body: JSON.stringify({plan_sha256: review.plan.plan_sha256}),
      }));
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
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

  async function deliverRepair() {
    if (!review || !repairPatch.trim() || review.state.status === "ABORTED") return;
    setRepairBusy(true); setError(null);
    try {
      const apiVersion = review.request?.schema_version === "review-request-v2" ? "v2" : "v1";
      const next = await api<Review>(`/api/${apiVersion}/reviews/${review.review_id}/repair`, {
        method: "POST", headers: {"Idempotency-Key": requestKey("repair")},
        body: JSON.stringify({patch: repairPatch}),
      });
      setReview(next); setRepairPatch("");
      const bundle = await api<ReviewBundle>(`/api/${apiVersion}/reviews/${review.review_id}/bundle`);
      setReviewBundle(bundle); setEvents(bundle.events || events);
      setDiffLines(bundle.evidence_bundle?.report?.render_model?.lines || diffLines);
      setError(null);
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
      setError(null); setSelected(null); setEvidenceDetail(null); setOfflineReplay(true);
    }).catch(e => setError(classifyError(400, "REPLAY_INVALID", `回放文件无效：${e.message}`)));
  }

  async function inspectEvent(event: EventRecord) {
    setSelected(event); setSelectedLine(null); setEvidenceDetail(null);
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
    // 时间线可点、结论不可点，那个交互是反的：用户看到的是结论，
    // 想追的也是结论。这里让每一行直接指回它的账本记录。
    setSelectedLine(line); setSelected(null); setEvidenceDetail(null);
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

  return <main>
    <header>
      <div className="brand-lockup"><img src="/brand/shuimu-yancode-mark.svg" alt="" width={44} height={44} />
        <div><h1>水木验码</h1><p>VERIFIABLE TEST PROTECTION</p></div>
        <span className="implementation-mark">TECH · SHUIMU YANMA</span>
      </div>
      <div className="header-controls">
        <div className="view-switch" aria-label="界面模式">
          <button className={uiMode === "judge" ? "active" : ""} onClick={() => changeUiMode("judge")}>评委模式</button>
          <button className={uiMode === "research" ? "active" : ""} onClick={() => changeUiMode("research")}>研究员模式</button>
          <button className={uiMode === "model_research" ? "active" : ""}
            disabled={!providerInfo.live_available}
            title={providerInfo.live_available ? "自由目标与 DeepSeek 完整透明记录" : "启动时接入 DeepSeek API 后可用"}
            onClick={() => changeUiMode("model_research")}>AI 研究员</button>
        </div>
        <div className="mode-strip" aria-label="运行边界">
          <span>{presentation.showPreviousResultNotice ? "下一次尚未运行" : runMode}</span>
          <span>{presentation.showPreviousResultNotice ? "正在准备下一次运行"
            : review || offlineReplay ? `当前结果：${displaySchedulingMode}` : setupLabels.scheduler}</span>
          <span>{presentation.showPreviousResultNotice ? setupLabels.isolation : executionMode}</span>
        </div>
        {review && <div className="workbench-actions">
          <button className="clear-workbench" disabled={!canClearWorkbench}
            title={canClearWorkbench ? "只清空当前页面，不删除审查记录或证据包"
              : "审查尚未结束，不能清空正在运行的工作台"}
            onClick={clearWorkbench}>清空工作台</button>
          <small>只清空当前页面，不删除审查记录或证据包</small>
        </div>}
      </div>
    </header>

    {presentation.showPreviousResultNotice && <section className="focus-strip" role="status">
      <div><strong>正在准备下一次运行</strong><span>上一轮结果已收起</span></div>
      <button onClick={showPreviousResult}>查看上一轮结果</button>
    </section>}
    {undoVisible && <div className="undo-toast" role="status" aria-live="polite">
      <span>工作台已清空，审查记录仍保留</span>
      <button onClick={undoClearWorkbench}>撤销</button>
    </div>}

    <section className={`hero ${review || uiMode !== "judge" ? "compact" : ""}`}>
      <div className="hero-copy"><p className="eyebrow">REVERSIBLE EVIDENCE · TEST PROTECTION</p>
        <h2>现有测试，<em>真的能发现问题吗？</em></h2>
        <p className="hero-context">代码生成得越来越快，但现有测试真的能发现问题吗？</p>
        <p className="hero-description"><strong>水木验码</strong>，用一次可逆实验，检查新增代码是不是真的有测试保护。</p>
        <div className="experiment-loop" aria-label="可逆实验步骤">
          <span>拿走代码</span><i>→</i><span>测试报警</span><i>→</i><span>恢复代码</span>
        </div>
      </div>
      <div className="hero-aside">
        <img className="hero-mark" src="/brand/shuimu-yancode-mark.svg"
          width={132} height={132} alt="水木验码图形标识" />
        {/* 还没开跑时，「尚未开始 · 第 0 步 / 共 8 步」配一条空进度条占了首屏一大块，
            却一个字都没告诉人。这个位置换成「这次实验会做什么」，等真的开跑再让位给进度。 */}
        {review && presentation.showCurrentResult ? <div className={`status-card tone-${currentStatus.tone}`}>
          <span>当前进度</span><strong>{currentStatus.label}</strong>
          <small>第 {currentStatus.step} 步 / 共 {currentStatus.total} 步 · Evidence {review.state.evidence_status || "尚未启动"}</small>
          <div className="progress"><i style={{width: `${currentStatus.step / currentStatus.total * 100}%`}} /></div>
          {review && !terminal.has(review.state.status) && !["CANCELLING", "RECOVERING"].includes(review.state.status) && <button className="status-action cancel-action"
            disabled={busy} onClick={() => void cancelReview()}>安全中止本次审查</button>}
          {review?.state.status === "ABORTED" && String(review.state.reason || "").includes("process_restart") &&
            <button className="status-action resume-action" disabled={busy}
              onClick={() => void resumeReview()}>从重启中止记录恢复</button>}
        </div> : review ? <div className="next-run-card">
          <span>下一次运行</span><strong>{provider === "live" ? "DeepSeek 受限调度" : "确定性调度"}</strong>
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
          <small>不会自动删代码，也不会替你提交；删不删由你决定。</small>
        </div>}
        {releaseStatus && <div className="release-status-card" role="status" aria-label="发布状态">
          <span>内部候选状态</span><strong>{releaseStatus.machine_status}</strong>
          <small>release_status: {releaseStatus.release_status} · stable_eligible: false</small>
          <small>待外部门禁：{releaseStatus.external_gates_pending.join("、")}</small>
        </div>}
      </div>
    </section>

    {error && <section className={`notice notice-${error.level}`}>
      <div><span>{error.code || "NOTICE"}</span><h3>{error.title}</h3><p>{error.message}</p></div>
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

    {uiMode === "judge" && summary && presentation.showCurrentResult && <section className="judge-overview" aria-label="评委结果概览">
      <div className="judge-counts">{judgeCounts(summary).map(item => {
        const loadLine = item.key === "load" ? diffLines.find(line => line.label === "承重") : undefined;
        return <button key={item.key} className={`metric metric-${item.key}`}
          disabled={!loadLine} onClick={() => loadLine && inspectLine(loadLine)}
          title={loadLine ? "打开一条承重结论的证据" : item.label}>
          <span>{item.label}</span><strong>{item.value}</strong>
        </button>;
      })}</div>
      <div className="experiment-story">{experimentStory(events, report?.certificates || []).map((stage, index) =>
        <button key={stage.key} type="button" className={`story-stage story-${stage.key} story-${stage.state}`}
          disabled={!storyEvent(stage.key)} onClick={() => {
            const event = storyEvent(stage.key);
            if (event) void inspectEvent(event);
          }} title={storyEvent(stage.key) ? "打开对应证据事件" : "本帧尚无对应事件"}>
          <span>{String(index + 1).padStart(2, "0")}</span><strong>{stage.label}</strong><small>{stage.detail}</small>
        </button>)}</div>
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
      <p className="judge-thesis">水木验码临时拿掉新增代码，观察哪个具名测试失败，再把代码恢复；结论来自可复核实验，不是模型直接猜测。</p>
    </section>}

    <div className={`workspace workspace-${uiMode === "judge" ? "judge" : "research"}`}>
      <aside className="panel intake">
        <div className="panel-title"><span>01</span><h3>{uiMode === "judge" ? "选择审查方式" : "配置已授权仓库的审查"}</h3></div>
        {uiMode === "judge" ? <>
          {visiblePresets.length ? <>
            <label htmlFor="preset">准备好的演示案例</label>
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
                <span>{selectedPreset.model_provider === "live" ? "DeepSeek 受限调度 · L2" : "确定性调度"}</span>
              </div>}
            </div>
            <button className="primary" disabled={busy || !presetId || configLocked}
              onClick={() => { const preset = visiblePresets.find(p => p.preset_id === presetId); if (preset) void createReview(preset); }}>
              运行这个演示案例
            </button>
            {review?.state.status === "AWAITING_APPROVAL" && <div className="judge-approval">
              <strong>请确认这次审查的边界</strong>
              <ul>
                <li>冻结候选 {(review.plan?.priorities as string[] | undefined)?.length ?? 0} 项，运行中不得增删</li>
                <li>声明测试范围 {(review.plan?.scope as string[] | undefined)?.length ?? 0} 项</li>
                <li>预算 {String(review.plan?.budget_seconds ?? "")} 秒</li>
              </ul>
              <button className="approve" disabled={busy} onClick={approve}>确认计划并开始审查</button>
              <small>水木验码不会自动删代码：它临时移除、观察具名测试、随后恢复。未经这一步不会执行任何实验。</small>
            </div>}
            <div className="entry-divider"><span>或者</span></div>
            <button className="custom-entry" disabled={busy || configLocked}
              onClick={openCustomReview}>＋ 配置已授权仓库的审查案例</button>
            <p className="custom-entry-note">选择本次启动时已授权的仓库，填写测试文件、审查目标和预算，再真实运行审查。</p>
          </> : <div className="empty">服务器尚未配置公开案例。仍可打开离线 ReviewBundle 进行可复核回放。</div>}
        </> : <>
        {review && <p className="field-note next-run-note">
          {presentation.showPreviousResultNotice
            ? "以下配置用于下一次运行；上一轮结果已收起，确认后会生成新的 Review。"
            : "以下配置用于下一次运行；当前结果只读取本次 Review 的真实记录。"}</p>}
        {configLocked && <p className="field-note locked-note" role="status">
          当前审查进行中，结束后可配置下一次运行。</p>}
        <label htmlFor="repo">已授权仓库</label><select id="repo" value={repoId} disabled={configLocked}
          onChange={e => selectCustomRepo(e.target.value)}>
          {repos.map(r => <option key={r.repo_id} value={r.repo_id}>{r.display_name}</option>)}
        </select>
        <p className="repo-scope-note">列表只显示已经授权的 Git 仓库；授权只在本次本地服务期间有效。</p>
        <label htmlFor="repo-path">增加本地仓库</label>
        <div className="repo-add"><input id="repo-path" value={repoPath} disabled={configLocked}
          placeholder="粘贴 Git 仓库根目录的完整路径" onChange={e => setRepoPath(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") void authorizeRepo(); }}/>
          <button type="button" disabled={busy || configLocked || !repoPath.trim()}
            onClick={() => void authorizeRepo()}>授权并加入</button></div>
        <label htmlFor="test-files">{uiMode === "model_research" ? "测试文件（可选）" : "pytest 测试文件"}</label><textarea id="test-files" rows={2} value={tests}
          disabled={configLocked} placeholder={"tests/test_backoff.py\ntests/integration/test_api.py"}
          onChange={e => setTests(e.target.value)} />
        <div className="field-rules">
          <p>{uiMode === "model_research" ? "留空时，智能体仅自动发现仓库内的 Python 测试文件；也可每行手动指定一个路径。" : "每行填写一个仓库内的 pytest 文件路径，从仓库根目录开始，例如 "}<code>tests/test_backoff.py</code>。</p>
          <p>不能填写绝对路径、文件夹、<code>../</code> 或 pytest 命令。</p>
        </div>
        <label htmlFor="goal-preset">{uiMode === "model_research" ? "推荐审查目标" : "结构化审查重点"}</label>
        <select id="goal-preset" value={goalPreset} disabled={configLocked} onChange={e => {
          const next = e.target.value;
          setGoalPreset(next);
          const recommended = GOAL_PRESETS.find(option => option.id === next);
          if (recommended) setGoal(recommended.goal);
        }}>
          {GOAL_PRESETS.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
          <option value="custom">{uiMode === "model_research" ? "自己输入目标" : "仅填写备注"}</option>
        </select>
        {demoGoalFits && goalPreset !== "custom" && !demoGoalFits.includes(goalPreset) &&
          <p className="field-note goal-fit-warning" role="status">这个示例仓库不是该目标的推荐演示：可以运行，
            但可能没有相应候选或无法形成有意义的排序。建议换仓库或改用“综合检查”。</p>}
        <label htmlFor="goal">{uiMode === "model_research" ? "审查目标" : "审查备注（可选）"}</label><textarea id="goal" rows={2} value={goal} disabled={configLocked}
          placeholder={uiMode === "model_research" ? "描述你希望 DeepSeek 优先关注的内容" : "补充背景、风险或希望人工留意的内容（不会改变确定性排序）"}
          onChange={e => { setGoal(e.target.value); setGoalPreset("custom"); }} />
        <p className="field-note">{uiMode === "model_research"
          ? "审查目标会交给 DeepSeek 作为排序上下文；三态结论只来自真实测试和恢复实验。"
          : "结构化审查重点会映射到确定性排序；自由备注只留档，不会被语义理解。三态结论只来自真实测试和恢复实验。"}</p>
        {uiMode === "model_research" && provider === "live" && <p className="field-note goal-live-note">
          这个目标将原样交给 DeepSeek。</p>}
        <div className="row"><div><label htmlFor="budget">预算 / 秒</label><input id="budget" type="number" min="1" max="3600"
          value={budget} disabled={configLocked} onChange={e => setBudget(Number(e.target.value))}/></div>
          <div><label>调度方式</label><div className="fixed-provider">
            {uiMode === "model_research" ? "DeepSeek 受限调度 · L2" : "确定性调度 · 不调用模型"}
          </div></div></div>
        <p className="field-note provider-note">{uiMode === "model_research"
          ? "AI 研究员会调用 DeepSeek；模型只调整检查顺序并生成只读建议。"
          : "评委与普通研究员模式固定为零模型调用。"}</p>
        <label className="permission-toggle" htmlFor="allow-repair-branch">
          <input id="allow-repair-branch" type="checkbox" checked={allowRepairBranch}
            disabled={configLocked} onChange={e => setAllowRepairBranch(e.target.checked)} />
          <span>验证通过后允许创建本地修复分支</span>
        </label>
        <p className="field-note">这是显式授权：只接受你随后粘贴的封闭补丁，验证失败、快照过期或分支冲突时不会提交。</p>
        {presentation.showDeterministicPlaceholder && <div className="setup-placeholder" role="status">
          <strong>本次将使用确定性调度，不调用模型</strong>
          <span>上一轮结果已收起；新的模型轨迹和建议不会从历史事件继承。</span>
        </div>}
        {uiMode === "model_research" && provider === "live" && <div className="model-entry" aria-label="DeepSeek 受限调度说明">
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
          <p className="disclosure">建议阶段会向 DeepSeek 发送最多 80 条、总计不超过 12KB
            的相关新增代码行；确认计划即表示同意本次发送范围。</p>
          {presentation.showLiveSetupPlaceholder && <div className="setup-placeholder live-setup" role="status">
            <strong>尚未开始模型调用</strong>
            <span>创建并确认新的 Review 后，决策轨迹只由本轮真实事件推进。</span>
          </div>}
        </div>}
        <label htmlFor="probe-strategy">最小化策略</label>
        <select id="probe-strategy" value={probeStrategy} disabled={configLocked}
          onChange={e => setProbeStrategy(e.target.value)}>
          <option value="hdd_inspired">HDD 启发式（默认）</option>
          <option value="ddmin" disabled={!capabilityAvailable(capabilities, "ddmin")}>
            完整 ddmin（额外实验，产出 1-minimal 证书）</option>
        </select>
        <p className="field-note">只影响是否额外签发 1-minimal 证书，不改变逐行三态结论。</p>
        <button className="primary" disabled={busy || !repoId || (!tests.trim() && uiMode !== "model_research") || configLocked}
          onClick={() => void createReview()}>生成审查计划</button>
        </>}
        <div className="history-entry">
          <span>已有审查记录</span>
          <small>只查看历史结果，不会重新运行代码或调用模型。</small>
        </div>
        <label className="replay">查看 ReviewBundle（JSON）<input type="file" accept="application/json"
          onChange={e => e.target.files?.[0] && loadReplay(e.target.files[0])}/></label>
      </aside>

      {(!review || presentation.showCurrentResult) && uiMode !== "judge" && <section className="panel plan">
        <div className="panel-title"><span>02</span><h3>执行计划与边界</h3></div>
        {review?.plan ? <>
          <div className="hash"><small>计划指纹 · SHA-256</small><code>{String(review.plan.plan_sha256 || "计划草案")}</code></div>
          <dl><div><dt>检查对象</dt><dd>{Array.isArray(review.plan.scope) ? review.plan.scope.length : 0}</dd></div>
            <div><dt>允许工具</dt><dd>{Array.isArray(review.plan.requested_tools) ? review.plan.requested_tools.length : 0}</dd></div>
            <div><dt>最大预算</dt><dd>{String(review.plan.budget_seconds || budget)}s</dd></div></dl>
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
              aria-label="DeepSeek 决策契约">
            <h4>DeepSeek 决策契约</h4>
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
            <small>完成后可展开完整 system／user 输入与 DeepSeek 原始输出；不含 API Key、请求头或服务地址，内部思维过程不公开。</small>
          </div>}
          <h4 className="order-heading">冻结候选全集</h4>
          <div className="anchors frozen">{((review.plan.priorities as string[]) || []).map((a, i) =>
            <div key={a}><b>{String(i + 1).padStart(2, "0")}</b><span>{a}</span></div>)}</div>
          <h4 className="order-heading">当前检查顺序{currentOrder.join("")
            !== ((review.plan.priorities as string[]) || []).join("")
            ? " · 已由调度更新" : ""}</h4>
          <div className="anchors live-order">{currentOrder.map((a, i) =>
            <div key={a} ref={orderRef(a)}><b>{String(i + 1).padStart(2, "0")}</b><span>{a}</span></div>)}</div>
          {review.state.status === "AWAITING_APPROVAL" && <button className="approve" disabled={busy}
            onClick={approve}>确认计划并开始审查</button>}
        </> : <div className="empty">提交输入后，这里会展示检查对象、允许工具、预算和计划指纹；未经确认不会执行仓库代码。</div>}
      </section>}

      {presentation.showModelTrack && uiMode === "model_research" && participation.live && <section className="panel decision-track"
        aria-label="DeepSeek 决策轨迹">
        <div className="panel-title"><span>03</span><h3>DeepSeek 决策轨迹</h3>
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

      {(!review || presentation.showCurrentResult) && <section className="panel timeline">
        <div className="panel-title"><span>04</span><h3>证据时间线</h3><small>{events.length} 条记录</small></div>
        {uiMode === "judge" ? <div className="lane-list">{currentLanes.map(group => <section
          key={group.lane} className={`lane lane-${group.lane === "模型建议" ? "model" :
            group.lane === "测试作证" ? "tests" : "executor"}`}>
          <div className="lane-heading"><h4>{group.lane}</h4><span>{group.events.length} 条</span></div>
          <div className="event-list">{group.events.length ? group.events.map(event =>
            <button key={event.event_id} onClick={() => inspectEvent(event)}
              className={selected?.event_id === event.event_id ? "active" : ""}>
              <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
              <span>{event.label}</span><b>#{event.seq}</b>
            </button>) : <div className="empty">本轮尚无该方事件。</div>}</div>
        </section>)}</div> : <div className="event-list">{visibleGrouped.length ? visibleGrouped.map(event =>
          <button key={event.event_id} onClick={() => inspectEvent(event)}
            className={selected?.event_id === event.event_id ? "active" : ""}>
            <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
            <span>{event.label}{schedulingDetail(event.kind, event.data) &&
              <small>{schedulingDetail(event.kind, event.data)}</small>}</span><b>#{event.seq}</b>
          </button>) : <div className="empty">确认计划后，基线验证、证据实验、恢复检查和结论签发会依次出现在这里。</div>}</div>}
      </section>}
    </div>

    {presentation.showCurrentResult && isTerminal && <section className={`completion-panel ${review?.state.status === "COMPLETE" ? "complete" : "incomplete"}`}>
      <div className="completion-heading"><div className="panel-title"><span>05</span><h3>本次审查结果</h3><small>{currentStatus.label}</small></div>
        <div className="result-signature"><img src="/brand/shuimu-yancode-mark.svg" alt="" width={28} height={28} /><span>水木验码</span></div>
      </div>
      {summary ? <h2>{emphasizeNumbers(summarySentence(summary)).map((part, i) =>
        part.number ? <b key={i}>{part.text}</b> : <span key={i}>{part.text}</span>)}</h2>
        : <h2>{review?.state.reason || "本次审查没有生成可发布的证据包。"}</h2>}
      {uiMode === "model_research" && <p className="model-conclusion">{modelConclusion(participation)}</p>}
      <div className="completion-grid">
        <div><span>运行来源</span><strong>{runMode}</strong></div>
        <div><span>调度方式</span><strong>{displaySchedulingMode}</strong></div>
        <div><span>完成范围</span><strong>{completionLabel}</strong></div>
        <div><span>工作区恢复</span><strong>{summary?.restore_protocol_version ? "逐实验校验" : "—"}</strong></div>
      </div>
      <section className="evidence-passport" aria-label="证据护照">
        <div className="passport-heading">
          <div><span className="passport-kicker">REVIEW EVIDENCE PASSPORT</span>
            <h4>证据护照</h4>
            <p>把本次 Review 的可复核事实压缩成一张可带走的结果卡。</p></div>
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
        {uiMode === "model_research" && passport.live ? <div className="passport-model" aria-label="证据护照模型参与">
          <div><span>模型参与</span><strong>是 · {passport.modelLabel}</strong></div>
          <div><span>模型调用</span><strong>{passport.modelCalls} 次</strong></div>
          <div><span>建议阶段</span><strong>{passport.recommendationLabel}</strong></div>
        </div> : !passport.live && <div className="passport-deterministic">本次未调用模型 · 确定性调度</div>}
        <div className="passport-footer">
          <span>{passport.bundleAvailable ? "完整 ReviewBundle 已装载，可下载留存。" : "本次未装载完整 ReviewBundle。"}</span>
          {reviewBundle && <button className="download" onClick={downloadBundle}>下载完整 ReviewBundle</button>}
        </div>
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
              <div><dt>EvidenceManifest</dt><dd><code>{String(review.repair.evidence_manifest_sha256 || review.repair.delivery_record?.evidence_manifest_sha256 || "—")}</code></dd></div>
              <div><dt>源提交</dt><dd><code>{String(review.repair.evidence_manifest?.source_commit || "—")}</code></dd></div>
            </dl>
            <div className="repair-actions">
              <button className="download" onClick={downloadRepairEvidence}>下载交付证据</button>
              <button className="download" onClick={() => void refreshRepairStatus()}>重新读取状态</button>
            </div>
          </> : review.repair?.status === "INCOMPLETE" ? <div className="repair-warning">
            交付工件缺失或不一致。请先清理隔离残留，再重新发起一次完整审查；系统不会覆盖已有分支。
          </div> : !repairAuthorized ? <div className="repair-warning">
            本次冻结计划没有授权创建修复分支。若需要交付，请返回配置并明确勾选“验证通过后允许创建本地修复分支”。
          </div> : <>
            <label htmlFor="repair-patch">粘贴已审阅的封闭补丁（unified diff）</label>
            <textarea id="repair-patch" rows={7} value={repairPatch} disabled={repairBusy}
              placeholder="仅接受 1–5 个文本文件的 unified diff；不会执行补丁中的命令。"
              onChange={e => setRepairPatch(e.target.value)} />
            <div className="repair-actions">
              <button className="primary" disabled={repairBusy || !repairPatch.trim()}
                onClick={() => void deliverRepair()}>验证补丁并创建本地分支</button>
              <button className="download" disabled={repairBusy} onClick={() => void refreshRepairStatus()}>重新读取状态</button>
            </div>
            <small className="repair-note">系统会再次运行本次声明测试，并在提交前后确认源工作区指纹未改变；不会 push、merge 或切换你的当前 checkout。</small>
          </>}
        </section>}
      {uiMode === "model_research" && <div className={`model-participation ${participation.live ? "is-live" : "is-deterministic"}`} aria-label="模型参与记录">
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
          <div><dt>策略拒绝</dt><dd>{participation.policyRejections} 次</dd></div>
          <div><dt>建议阶段</dt><dd>{participation.recommendationFailed
            ? "生成失败（不影响实验结论）"
            : participation.recommendations?.generated
              ? `已生成 ${participation.recommendations.items.length} 条`
              : "未生成"}</dd></div>
        </dl> : <div className="deterministic-participation">
          <strong>本次未调用模型</strong>
          <span>{schedulingMode} · 0 次模型调用 · 0 次降级</span>
        </div>}
        <p>模型只调整检查顺序；测试结果、证据结论和恢复校验由水木验码执行。</p>
      </div>}
      {uiMode === "model_research" && participation.live && <section className="model-transparency" aria-label="模型透明记录">
        <div className="model-transcript">
          <h4>DeepSeek 完整聊天记录</h4>
          <p className="recommendations-note">逐次展示完整输入与原始输出 · 不含 API Key、请求头或服务地址</p>
          {modelCalls.length > 0 ? modelCalls.map(call => <details key={call.call}>
            <summary>调用 #{call.call} · {({plan: "计划", scheduling: "运行中调度",
              narration: "证据编排", recommendation: "改进建议"} as Record<string, string>)[call.stage] || call.stage}
              <span>{call.http_status || call.error_type || "—"} · {String(call.latency_ms || 0)}ms</span></summary>
            {call.messages.map((message, index) => <div className="chat-message" key={index}>
              <strong>{message.role === "system" ? "系统输入" : "用户输入"}</strong>
              <pre>{typeof message.content === "string" ? message.content : JSON.stringify(message.content, null, 2)}</pre>
            </div>)}
            <div className="chat-message response"><strong>DeepSeek 原始输出</strong>
              <pre>{call.raw_response || `调用失败：${call.error_type || "无响应"}`}</pre></div>
          </details>) : <div className="empty">这份结果没有可用的完整聊天记录；旧版运行只保存了摘要。</div>}
        </div>
      {presentation.showRecommendations && <div className="recommendations" aria-label="水木验码安全建议栏">
        <h4>水木验码安全建议栏</h4>
        <p className="recommendations-note">DeepSeek 原始建议经水木验码证据白名单校验后呈现 · 最多 3 条 · 未执行</p>
        <p className="recommendations-boundary">AI 建议 · 未执行 · 不是三态结论 · 需要人工复核</p>
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
              <p className="verification">复验：{item.verification}</p>
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

    {presentation.showCurrentResult && diffLines.length > 0 && <section className="diff-panel">
      <div className="panel-title"><span>06</span><h3>逐行证据视图</h3><small>{diffLines.length} 行新增代码</small></div>
      <div className="diff-head"><div><div className="legend"><i className="dot-load"/>承重<i className="dot-unevidenced"/>无据
        <i className="dot-drift"/>游离<i className="dot-unlabeled"/>未标注</div>
        {Number(((summary?.by_reason || {}) as Record<string, number>).inert_withheld || 0) > 0 &&
          <div className="guardrail"><strong>护栏拦截的能力</strong><span>{String(((summary?.by_reason || {}) as Record<string, number>).inert_withheld)} 行惰性结论已扣下，不进入正式三态结论。</span></div>}
        </div>
        <div className="scope-boundary"><strong>始终记住</strong>{uncovered.slice(0, 2).map((item, i) => <span key={i}>{item}</span>)}</div></div>
      <div className="diff-lines">{diffGroups.map(group => group.kind === "drift"
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
              className={`diff-line ${presentation.className}${evidence ? "" : " inert"}${
                selectedLine?.file === line.file && selectedLine?.line === line.line ? " active" : ""}`}
              disabled={!evidence} onClick={() => inspectLine(line)}
              title={evidence ? "查看这一行的证据记录" : "旧 Bundle 或该行没有逐行证据链接"}>
              <b>{presentation.badge}{presentation.inherited &&
                <i className="inherit" title="非执行行，继承所在单元的结论">继承</i>}</b>
              <code>{line.file}:{line.line}</code><span className="line-code">{line.text || " "}</span>
              <small>{line.reason || evidence || ""}</small>
            </button>;
          }))}</div>
    </section>}

    {uiMode !== "judge" && capabilities.length > 0 &&
      <section className="panel capabilities">
      <div className="panel-title"><span>07</span><h3>能力状态</h3>
        <small>门槛先冻结，再测量</small></div>
      <p className="field-note">状态由预先冻结的门槛判定。没过门槛的能力保留为负结果或关闭，
        既不改写成通过，也不从这里删掉。发布链用同一份状态校验对外材料的措辞，
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
    </section>}

    {selectedLine && <button className="drawer-scrim" aria-label="关闭证据抽屉"
      onClick={() => setSelectedLine(null)} />}
    {selectedLine && <aside className="drawer">
      <button aria-label="关闭证据抽屉" onClick={() => setSelectedLine(null)}>×</button>
      <p>{linePresentation(selectedLine).badge}</p>
      <h3>{selectedLine.file}:{selectedLine.line}</h3>
      <pre className="drawer-code">{selectedLine.text || " "}</pre>
      {evidenceDetail ? <EvidenceCard record={evidenceDetail} bundle={reviewBundle}/>
        : <p className="empty">这一行没有对应的账本记录{
            selectedLine.reason ? `（${selectedLine.reason}）` : ""}。</p>}
      <div className="scope-boundary">{uncovered.slice(0, 2).map((item, i) =>
        <span key={i}>{item}</span>)}</div>
    </aside>}

    {selected && <button className="drawer-scrim" aria-label="关闭证据抽屉"
      onClick={() => setSelected(null)} />}
    {selected && <aside className="drawer"><button aria-label="关闭证据抽屉" onClick={() => setSelected(null)}>×</button>
      <p>{eventLabel(selected.kind)}</p><h3>证据记录 #{selected.seq}</h3>
      <div className="event-summary"><span>发生时间</span><strong>{new Date(selected.occurred_at).toLocaleString("zh-CN")}</strong>
        <span>事件编号</span><strong>{selected.event_id}</strong></div>
      {evidenceDetail ? <EvidenceCard record={evidenceDetail} bundle={reviewBundle}/>
        : <details><summary>展开事件数据</summary><pre>{JSON.stringify(selected.data, null, 2)}</pre></details>}
    </aside>}
    <footer><div className="footer-runtime"><span>运行来源 · {runMode}</span><span>执行模式 · {executionMode}</span>
      <span>调度方式 · {displaySchedulingMode}</span><span>结论边界 · 仅限已声明测试范围，不代表语义等价</span></div>
      <div className="footer-meta">
        <p className="footer-author">作者 杨佩立（清华大学）· © 2026 杨佩立 版权所有</p>
        <p><strong>水木验码</strong>为学生参赛项目；Modou／水木验码为代码与历史实验沿用的技术标识。<br />
          本项目不代表清华大学官方产品、授权或合作背书。</p>
      </div></footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
