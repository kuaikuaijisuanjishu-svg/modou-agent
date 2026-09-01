import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import {
  COMMON_UNCOVERED, SESSION_REVIEW_KEY, SESSION_TOKEN_KEY, classifyError, eventLabel,
  linePresentation, normalizePastedToken, resolveStartupToken,
  schedulingDetail, statusView, summarySentence, decisiveEvidenceId,
  experimentStory, groupEventPhases, judgeCounts, minimizationViews,
  modeLabels, capabilityAvailable, capabilityTone, groupCapabilities,
  type Capability, type UiNotice,
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
  last_seq: number;
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
  model_metrics?: Record<string, unknown>; narration?: {
    scope_note?: string; blocks?: Array<Record<string, unknown>>;
  };
  evidence_bundle?: {report?: Report; ledger?: Array<Record<string, unknown>>};
  execution_mode?: string; scheduler_mode?: string; isolation_mode?: string;
};

const fragmentToken = new URLSearchParams(location.hash.slice(1)).get("token") || "";
let sessionToken = resolveStartupToken(location.hash, sessionStorage.getItem(SESSION_TOKEN_KEY));
if (fragmentToken) sessionStorage.setItem(SESSION_TOKEN_KEY, fragmentToken);
if (location.hash) history.replaceState(null, "", location.pathname + location.search);

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

const terminal = new Set(["COMPLETE", "PARTIAL", "FAILED", "ABORTED"]);

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

function App() {
  const [uiMode, setUiMode] = useState<"judge" | "research">("judge");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [presetId, setPresetId] = useState("");
  const [repoId, setRepoId] = useState("");
  const [tests, setTests] = useState("tests/test_backoff.py");
  const [goal, setGoal] = useState("审查这次补丁中新增代码的证据边界");
  const [budget, setBudget] = useState(300);
  const [provider, setProvider] = useState("deterministic");
  const [probeStrategy, setProbeStrategy] = useState("hdd_inspired");
  const [providerInfo, setProviderInfo] = useState<Record<string, unknown>>({});
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [review, setReview] = useState<Review | null>(null);
  const [reviewBundle, setReviewBundle] = useState<ReviewBundle | null>(null);
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
  const lastEvent = useRef("");

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
      setCapabilities(capabilityResponse.capabilities);
      setRepos(r.repos); setRepoId(old => old || r.repos[0]?.repo_id || "");
      setPresets(presetResponse.presets);
      setPresetId(old => old || presetResponse.presets[0]?.preset_id || "");
      setProviderInfo(p); setError(null);
      const remembered = sessionStorage.getItem(SESSION_REVIEW_KEY);
      if (remembered) {
        try {
          setReview(await api<Review>(`/api/v1/reviews/${remembered}`));
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

  useEffect(() => {
    if (!review || terminal.has(review.state.status)) return;
    const controller = new AbortController();
    consumeEvents(review.review_id, lastEvent.current, event => {
      lastEvent.current = event.event_id;
      setEvents(old => old.some(x => x.event_id === event.event_id) ? old : [...old, event]);
      if (event.kind === "review.completed" || event.kind === "review.failed") {
        api<Review>(`/api/v1/reviews/${review.review_id}`).then(setReview)
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
  const phaseGroups = useMemo(() => groupEventPhases(grouped), [grouped]);
  const currentStatus = statusView(review?.state.status);
  const report = reviewBundle?.evidence_bundle?.report;
  const summary = report?.summary || null;
  const uncovered = report?.口径声明?.length ? report.口径声明 : COMMON_UNCOVERED;
  const isTerminal = !!review && terminal.has(review.state.status);
  const labels = modeLabels(reviewBundle as unknown as Record<string, unknown>, offlineReplay);
  const executionMode = labels.isolation;
  const schedulingMode = labels.scheduler;
  const runMode = labels.run;
  const completionLabel = summary?.analysis_completion === "complete" ? "完整审查" :
    summary?.analysis_completion === "partial" ? "部分审查" : String(review?.state.status || "—");

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

  async function createReview(preset?: Preset) {
    setError(null); setBusy(true); setEvents([]); setDiffLines([]); setReviewBundle(null);
    setSelected(null); setEvidenceDetail(null); setOfflineReplay(false); lastEvent.current = "";
    const requestRepo = preset?.repo_id || repoId;
    const requestTests = preset?.test_files || tests.split("\n").map(x => x.trim()).filter(Boolean);
    try {
      const next = await api<Review>("/api/v1/reviews", {
        method: "POST",
        body: JSON.stringify({
          source: {kind: "local", repo_id: requestRepo},
          test_files: requestTests, declared_tests: [], goal: preset?.goal || goal,
          budget_seconds: preset?.budget_seconds || budget,
          probe_strategy: probeStrategy,
          model_provider: preset?.model_provider || provider,
        }),
      });
      sessionStorage.setItem(SESSION_REVIEW_KEY, next.review_id);
      setReview(next);
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
  }

  async function approve() {
    if (!review?.plan?.plan_sha256) return;
    setBusy(true); setError(null);
    try {
      setReview(await api<Review>(`/api/v1/reviews/${review.review_id}/approval`, {
        method: "POST", body: JSON.stringify({plan_sha256: review.plan.plan_sha256}),
      }));
    } catch (e) { setError(noticeFor(e)); }
    finally { setBusy(false); }
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
        plan: bundle.plan, last_seq: (bundle.events || []).length});
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
      <div className="brand-lockup"><img src="/brand/shuimu-yancode-mark.svg" alt="水木验码图形标识" />
        <div><h1>水木验码</h1><p>SHUIMU YANMA · VERIFIABLE TEST PROTECTION</p></div>
        <span className="version-badge">v0.1.0</span>
      </div>
      <div className="header-controls">
        <div className="view-switch" aria-label="界面模式">
          <button className={uiMode === "judge" ? "active" : ""} onClick={() => setUiMode("judge")}>评委模式</button>
          <button className={uiMode === "research" ? "active" : ""} onClick={() => setUiMode("research")}>研究员模式</button>
        </div>
        <div className="mode-strip" aria-label="运行边界">
          <span>{runMode}</span><span>{schedulingMode}</span><span>{executionMode}</span>
        </div>
      </div>
    </header>

    <section className={`hero ${review ? "compact" : ""}`}>
      <div className="hero-copy"><p className="eyebrow">给代码审查者一份能复查的答案</p>
        <h2>AI 写的代码测试全绿，<em>哪几行真的有测试撑着？</em></h2>
        <p className="hero-description"><strong>“有测试保护”不等于代码绝对正确。</strong>它指的是：代码被拿掉或改坏时，会有一条具体测试报警。水木验码用可恢复的小实验，把这条关系找出来。</p>
        <div className="plain-process" aria-label="水木验码的工作方法">
          <span><b>1</b><strong>先跑一遍</strong><small>确认原测试本来是绿的</small></span>
          <span><b>2</b><strong>临时拿走代码</strong><small>做一次可恢复的小实验</small></span>
          <span><b>3</b><strong>看谁会报警</strong><small>把失败测试和代码对应起来</small></span>
        </div>
      </div>
      <div className={`status-card tone-${currentStatus.tone}`}>
        <span>当前进度</span><strong>{currentStatus.label}</strong>
        <small>第 {currentStatus.step} 步 / 共 {currentStatus.total} 步 · Evidence {review?.state.evidence_status || "尚未启动"}</small>
        <div className="progress"><i style={{width: `${currentStatus.step / currentStatus.total * 100}%`}} /></div>
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

    {uiMode === "judge" && summary && <section className="judge-overview" aria-label="评委结果概览">
      <div className="overview-heading"><div><span>先看懂，再看明细</span><h2>这次实验说明了什么？</h2></div>
        <p>红色不是“坏代码”，而是<strong>找到了具名测试保护</strong>；黄色和灰色表示现有测试还不能给出同样强的答案。</p></div>
      <div className="judge-counts">{judgeCounts(summary).map(item => {
        const loadLine = item.key === "load" ? diffLines.find(line => line.label === "承重") : undefined;
        const explanations = {added: "本次检查范围", load: "拿掉后测试会失败",
          unevidenced: "声明测试范围内未执行", drift: "未被测试或代码引用"};
        return <button key={item.key} className={`metric metric-${item.key}`}
          disabled={!loadLine} onClick={() => loadLine && inspectLine(loadLine)}
          title={loadLine ? "打开一条承重结论的证据" : item.label}>
          <span>{item.label}</span><strong>{item.value}</strong><small>{explanations[item.key]}</small>
        </button>;
      })}</div>
      <h3 className="story-heading">它是怎么得到这个答案的？</h3>
      <div className="experiment-story">{experimentStory(events).map((stage, index) =>
        <div key={stage.key} className={`story-stage story-${stage.state}`}>
          <span>{String(index + 1).padStart(2, "0")}</span><strong>{stage.label}</strong><small>{stage.detail}</small>
        </div>)}</div>
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
      <p className="judge-thesis"><strong>一句话：</strong>水木验码临时拿掉新增代码，观察哪个具名测试失败，再把代码恢复；所以评委看到的是一场可以复查的小实验，不是模型直接猜测。</p>
    </section>}

    <div className={`workspace workspace-${uiMode}`}>
      <aside className="panel intake">
        <div className="panel-title"><span>01</span><h3>{uiMode === "judge" ? "亲手跑一个真实案例" : "审查输入"}</h3></div>
        {uiMode === "judge" ? <>
          {presets.length ? <>
            <label htmlFor="preset">预置案例</label>
            <select id="preset" value={presetId} onChange={e => setPresetId(e.target.value)}>
              {presets.map(preset => <option key={preset.preset_id} value={preset.preset_id}>{preset.display_name}</option>)}
            </select>
            <p className="preset-description">{presets.find(p => p.preset_id === presetId)?.description}</p>
            <button className="primary" disabled={busy || !presetId || (!!review && !terminal.has(review.state.status))}
              onClick={() => { const preset = presets.find(p => p.preset_id === presetId); if (preset) void createReview(preset); }}>
              一键运行案例
            </button>
            {review?.state.status === "AWAITING_APPROVAL" && <div className="judge-approval">
              <strong>请确认这次审查的边界</strong>
              <ul>
                <li>已确定候选 {(review.plan?.priorities as string[] | undefined)?.length ?? 0} 项，运行中不得增删</li>
                <li>声明测试范围 {(review.plan?.scope as string[] | undefined)?.length ?? 0} 项</li>
                <li>预算 {String(review.plan?.budget_seconds ?? "")} 秒</li>
              </ul>
              <button className="approve" disabled={busy} onClick={approve}>确认计划并开始审查</button>
              <small>水木验码不会自动删代码：它临时移除、观察具名测试、随后恢复。未经这一步不会执行任何实验。</small>
            </div>}
          </> : <div className="empty">服务器尚未配置公开案例。仍可打开离线 ReviewBundle 进行可复核回放。</div>}
        </> : <>
        <label htmlFor="repo">已授权仓库</label><select id="repo" value={repoId} onChange={e => setRepoId(e.target.value)}>
          {repos.map(r => <option key={r.repo_id} value={r.repo_id}>{r.display_name}</option>)}
        </select>
        <label htmlFor="test-files">pytest 测试文件</label><textarea id="test-files" rows={3} value={tests}
          placeholder="每行一个仓库内测试路径" onChange={e => setTests(e.target.value)} />
        <label htmlFor="goal">审查目标</label><textarea id="goal" rows={3} value={goal} onChange={e => setGoal(e.target.value)} />
        <div className="row"><div><label htmlFor="budget">预算 / 秒</label><input id="budget" type="number" min="1" max="3600"
          value={budget} onChange={e => setBudget(Number(e.target.value))}/></div>
          <div><label htmlFor="provider">调度方式</label><select id="provider" value={provider} onChange={e => setProvider(e.target.value)}>
            <option value="deterministic">确定性调度（默认）</option>
            <option value="live" disabled={!providerInfo.live_available}>模型调度（需 API）</option>
          </select></div></div>
        <label htmlFor="probe-strategy">最小化策略</label>
        <select id="probe-strategy" value={probeStrategy}
          onChange={e => setProbeStrategy(e.target.value)}>
          <option value="hdd_inspired">HDD 启发式（默认）</option>
          <option value="ddmin" disabled={!capabilityAvailable(capabilities, "ddmin")}>
            完整 ddmin（额外实验，产出 1-minimal 证书）</option>
        </select>
        <p className="field-note">能力级别由服务器绑定；L2 可在真实观测后严格重排，模型始终不能改写证据结论。
          最小化策略只影响是否额外签发 1-minimal 证书，不改变逐行三态结论。</p>
        <button className="primary" disabled={busy || !repoId || !tests.trim() || (!!review && !terminal.has(review.state.status))}
          onClick={() => void createReview()}>生成审查计划</button>
        </>}
        <label className="replay">载入离线 ReviewBundle<input type="file" accept="application/json"
          onChange={e => e.target.files?.[0] && loadReplay(e.target.files[0])}/></label>
      </aside>

      {uiMode === "research" && <section className="panel plan">
        <div className="panel-title"><span>02</span><h3>执行计划与边界</h3></div>
        {review?.plan ? <>
          <div className="hash"><small>计划指纹 · SHA-256</small><code>{String(review.plan.plan_sha256 || "计划草案")}</code></div>
          <dl><div><dt>检查对象</dt><dd>{Array.isArray(review.plan.scope) ? review.plan.scope.length : 0}</dd></div>
            <div><dt>允许工具</dt><dd>{Array.isArray(review.plan.requested_tools) ? review.plan.requested_tools.length : 0}</dd></div>
            <div><dt>最大预算</dt><dd>{String(review.plan.budget_seconds || budget)}s</dd></div></dl>
          <div className="anchors">{((review.plan.priorities as string[]) || []).map((a, i) =>
            <div key={a}><b>{String(i + 1).padStart(2, "0")}</b><span>{a}</span></div>)}</div>
          {review.state.status === "AWAITING_APPROVAL" && <button className="approve" disabled={busy}
            onClick={approve}>确认计划并开始审查</button>}
        </> : <div className="empty">提交输入后，这里会展示检查对象、允许工具、预算和计划指纹；未经确认不会执行仓库代码。</div>}
      </section>}

      <section className="panel timeline">
        <div className="panel-title"><span>03</span><h3>{uiMode === "judge" ? "实验进行到哪一步" : "证据时间线"}</h3><small>{events.length} 条记录</small></div>
        {uiMode === "judge" ? <div className="phase-list">{phaseGroups.map(group => <details key={group.phase}
          open={group.events.some(event => event.kind.includes("failed") || event.kind === "restore.verified")}>
          <summary><span>{group.phase}</span><strong>{group.events.length} 条</strong></summary>
          <div className="event-list">{group.events.map(event =>
            <button key={event.event_id} onClick={() => inspectEvent(event)}
              className={selected?.event_id === event.event_id ? "active" : ""}>
              <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
              <span>{event.label}</span><b>#{event.seq}</b>
            </button>)}</div>
        </details>)}</div> : <div className="event-list">{grouped.length ? grouped.map(event =>
          <button key={event.event_id} onClick={() => inspectEvent(event)}
            className={selected?.event_id === event.event_id ? "active" : ""}>
            <i className={event.kind.includes("failed") ? "bad" : ""}/><time>{event.time}</time>
            <span>{event.label}{schedulingDetail(event.kind, event.data) &&
              <small>{schedulingDetail(event.kind, event.data)}</small>}</span><b>#{event.seq}</b>
          </button>) : <div className="empty">确认计划后，基线验证、证据实验、恢复检查和结论签发会依次出现在这里。</div>}</div>}
      </section>
    </div>

    {isTerminal && <section className={`completion-panel ${review?.state.status === "COMPLETE" ? "complete" : "incomplete"}`}>
      <div className="panel-title"><span>04</span><h3>本次审查结果</h3><small>{currentStatus.label}</small></div>
      {summary ? <>
        <p className="result-kicker">先看评委需要带走的结论</p>
        <h2>在 {String(summary.total_added_lines || 0)} 行新增代码里，<em>{String(((summary.by_label || {}) as Record<string, number>)["承重"] || 0)} 行</em>已经找到具名测试保护，也就是测试承重证据。</h2>
        <details className="full-accounting"><summary>查看完整数字口径</summary><p>{summarySentence(summary).replace("测试承重证据", "正式承重结论")}</p></details>
      </> : <h2>{review?.state.reason || "本次审查没有生成可发布的证据包。"}</h2>}
      <div className="completion-grid">
        <div><span>运行来源</span><strong>{runMode}</strong></div>
        <div><span>调度方式</span><strong>{schedulingMode}</strong></div>
        <div><span>完成范围</span><strong>{completionLabel}</strong></div>
        <div><span>工作区恢复</span><strong>{summary?.restore_protocol_version ? "逐实验校验" : "—"}</strong></div>
      </div>
      <div className="completion-boundary"><h4>结论边界</h4><ul>{uncovered.map((item, i) => <li key={i}>{item}</li>)}</ul></div>
      <div className="completion-actions">
        {reviewBundle && <button className="download" onClick={downloadBundle}>下载完整 ReviewBundle</button>}
        <label className="replay">打开离线 ReviewBundle<input type="file" accept="application/json"
          onChange={e => e.target.files?.[0] && loadReplay(e.target.files[0])}/></label>
      </div>
    </section>}

    {diffLines.length > 0 && <section className="diff-panel">
      <div className="panel-title"><span>05</span><h3>逐行证据视图 · 技术明细</h3><small>{diffLines.length} 行新增代码</small></div>
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

    {uiMode === "research" && capabilities.length > 0 &&
      <section className="panel capabilities">
      <div className="panel-title"><span>06</span><h3>能力状态</h3>
        <small>先确定门槛，再测量</small></div>
      <p className="field-note">状态由预先确定的门槛判定。没过门槛的能力保留为负结果或关闭，
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

    {selected && <aside className="drawer"><button aria-label="关闭证据抽屉" onClick={() => setSelected(null)}>×</button>
      <p>{eventLabel(selected.kind)}</p><h3>证据记录 #{selected.seq}</h3>
      <div className="event-summary"><span>发生时间</span><strong>{new Date(selected.occurred_at).toLocaleString("zh-CN")}</strong>
        <span>事件编号</span><strong>{selected.event_id}</strong></div>
      {evidenceDetail ? <EvidenceCard record={evidenceDetail} bundle={reviewBundle}/>
        : <details><summary>展开事件数据</summary><pre>{JSON.stringify(selected.data, null, 2)}</pre></details>}
    </aside>}
    <footer><span>水木验码 v0.1.0 · 水木验码技术内核</span><span>运行来源 · {runMode}</span><span>执行模式 · {executionMode}</span><span>调度方式 · {schedulingMode}</span>
      <span>结论边界 · 仅限已声明测试范围，不代表语义等价</span></footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
