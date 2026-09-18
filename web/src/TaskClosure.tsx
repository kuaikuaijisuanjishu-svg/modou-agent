import {useEffect, useRef, useState} from "react";
import "./task-closure.css";
// N04 页面接线：四个 T06/T12 组件接入真实数据（main.tsx → TaskClosure 面板）。
// 数据全部来自服务端 API（/api/v2/tasks/*/rounds、/api/v2/jobs、/api/v2/teaching/lessons），
// 组件只做展示；确认映射不产生成功结论（服务端仍要求真实复验）。
import {CandidateStatusChips} from "./master/CandidateStatusChips";
import {DelegationCard, type DelegationEvent, type DelegationView} from "./master/DelegationCard";
import {JobProgressView, isTerminal, type JobView} from "./master/JobProgressView";
import {MappingConfirmation, type ConfirmDecision} from "./master/MappingConfirmation";
import {RoundHistoryView, type RoundOutcome, type TaskRound as RoundRow} from "./master/RoundHistoryView";
import {TeachingView, type TeachingNarration} from "./master/TeachingView";
import {deriveTaskTerminalState} from "./taskTerminalState";

export type TaskCandidate = {source: string; patch: string; patch_sha256: string;
  path?: string; code_sha256?: string; verification?: Record<string, unknown>};
export type AdoptionPlan = {plan_sha256: string; source_snapshot_sha256: string;
  patch_sha256: string; paths: string[]; test_files: string[]; budget_seconds: number;
  expires_at?: number | string; status?: string};
export type TaskCriterion = {criterion_id: string; text: string; finding_ids: string[];
  evidence_kind?: string; profile_id?: string; language?: string; adapter_id?: string;
  test_targets?: string[]; adoption_mode?: string; source_snapshot_matches?: boolean; status: string; disposition?: string;
  candidate?: TaskCandidate | null; adoption_plan?: AdoptionPlan | null;
  target_mapping?: TaskTargetMapping | null;
  followup_review_id?: string; allowed_actions: string[];
  proposal_attempt?: Record<string, unknown>; experiment?: Record<string, unknown>;
  outcome?: {status: string; improved?: boolean; reason?: string; origin_rows?: unknown[]; followup_rows?: unknown[]}};
// 服务端形状见 modou/target_mapping.py 的 MappingReport.as_dict()。
export type TaskTargetMapping = {schema_version?: string;
  mappings: Array<{mapping_id: string; status: string; reason?: string;
    origin?: {file?: string; line?: number};
    candidates?: Array<{file: string; line: number; basis: string; confidence?: number}>}>;
  summary?: Record<string, number>};
export type EvidenceTask = {schema_version: string; task_id: string; origin_review_id: string;
  title: string; criteria: TaskCriterion[]; receipt_url?: string; active_round_id?: string};
type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type ExistingCandidate = {source: string; sha256: string; label: string};
type Finding = {file: string; line: number; label: string; text: string};
const STATUS: Record<string, string> = {pending: "等待选择处置", selected: "已选择处置",
  generating: "正在生成与验证", candidate_ready: "候选已封存", prepared: "等待确认采用",
  applying: "正在采用", adopted: "已采用，等待复验", reviewing: "新版本复验中",
  reviewed: "复验执行已完成", accepted_risk: "人工接受风险", recovery_required: "需要恢复处理"};
const OUTCOME: Record<string, string> = {pending: "尚无复验结论", supported: "目标具备测试依据",
  gap_remains: "目标仍有证据缺口", inconclusive: "暂时无法判定", accepted_risk: "人工接受风险，缺口保留"};
export function taskOutcomeLabel(status?: string): string {
  return status ? OUTCOME[status] || `未识别结论：${status}` : "尚无复验结论";
}
export function canTaskAction(criterion: TaskCriterion, action: string): boolean {
  return Array.isArray(criterion.allowed_actions) && criterion.allowed_actions.includes(action);
}
export function taskLink(taskId: string): string { return `#task=${encodeURIComponent(taskId)}`; }
export function taskRequirements(text: string): Array<{text: string; finding_ids: string[]}> {
  return text.split("\n").map(value => value.trim()).filter(Boolean)
    .map(value => ({text: value, finding_ids: []}));
}
function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "请求未完成，请重新读取状态后重试。";
}
function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: "application/json"}));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- 第6步任务摘要（P0-02）：先回答"在验收什么/现状/下一步" -------------
export type EvidenceTone = "ok" | "warn" | "danger" | "neutral";
export type TaskSummary = {
  goals: Array<{text: string; outcome: string}>;
  version: string;
  evidence: {status: string; label: string; tone: EvidenceTone};
  needsReverify: boolean;
  next: {label: string; kind: "scroll" | "receipt" | "check_delegation" | "reverify";
    criterion_id?: string} | null;
};
export function aggregateEvidence(task: EvidenceTask): TaskSummary["evidence"] {
  // 与服务端回执同一纪律：未完成项不因其他项通过而通过。这里是展示层的
  // 近似聚合；权威结论以 GET /receipt 为准。
  const outcomes = task.criteria.map(item => item.outcome?.status || "pending");
  if (!outcomes.length) return {status: "empty", label: "还没有验收项", tone: "neutral"};
  if (outcomes.includes("pending")) return {status: "pending", label: "处置进行中，尚无完整结论", tone: "neutral"};
  if (outcomes.every(status => status === "supported"))
    return {status: "supported", label: "当前验收项都有测试依据", tone: "ok"};
  if (outcomes.includes("accepted_risk") && outcomes.every(status => status === "supported" || status === "accepted_risk"))
    return {status: "accepted_risk", label: "缺口由人工接受风险并留档", tone: "warn"};
  if (outcomes.includes("inconclusive"))
    return {status: "inconclusive", label: "实验未完成或环境失败，暂时无法判定", tone: "warn"};
  if (outcomes.includes("gap_remains"))
    return {status: "regression_observed", label: "已观察到行为回归或证据缺口", tone: "danger"};
  return {status: "gap_remains", label: "目标仍有证据缺口", tone: "warn"};
}
export function needsReverify(task: EvidenceTask): boolean {
  return task.criteria.some(item => item.status === "pending_recheck"
    || item.source_snapshot_matches === false);
}
export function nextAction(task: EvidenceTask): TaskSummary["next"] {
  if (!task.criteria.length) return null;
  if (needsReverify(task)) return {label: "当前版本已变化，需要复验", kind: "reverify"};
  const byStatus = (status: string) => task.criteria.find(item => item.status === status);
  const prepared = byStatus("prepared");
  if (prepared) return {label: "确认采用并复验", kind: "scroll", criterion_id: prepared.criterion_id};
  const candidate = byStatus("candidate_ready");
  if (candidate) return {label: "查看候选并准备采用", kind: "scroll", criterion_id: candidate.criterion_id};
  const pending = task.criteria.find(item => !item.disposition
    && (item.status === "pending" || item.allowed_actions.includes("select_disposition")));
  if (pending) return {label: "为验收项选择处置路径", kind: "scroll", criterion_id: pending.criterion_id};
  if (aggregateEvidence(task).status === "supported")
    return {label: "领取处置回执", kind: "receipt"};
  return null;
}
export function summarizeTask(task: EvidenceTask, activeRound?: ServerRoundRow): TaskSummary {
  const version = activeRound
    ? `第 ${activeRound.round_no} 轮 · ${activeRound.requirement_version}`
    : (task.active_round_id || "初始版本");
  const summary: TaskSummary = {
    goals: task.criteria.map(item => ({text: item.text, outcome: item.outcome?.status || "pending"})),
    version,
    evidence: aggregateEvidence(task),
    needsReverify: needsReverify(task),
    next: nextAction(task),
  };
  return summary;
}

export function TaskSummaryCard({summary, busy, onNext, delegationActive}: {
  summary: TaskSummary; busy: boolean; delegationActive: boolean;
  onNext: (next: NonNullable<TaskSummary["next"]>) => void;
}) {
  return <section className="closure-summary" aria-label="任务摘要">
    <div className="closure-summary-row">
      <div><h4>正在替我验收什么</h4>
        {summary.goals.length
          ? <ul className="mv-list">{summary.goals.map((goal, index) =>
            <li key={index}>{goal.text}</li>)}</ul>
          : <p className="field-note">这项任务还没有写明具体验收项。</p>}</div>
      <div><h4>当前版本</h4><p>{summary.version}</p>
        {summary.needsReverify && <p className="closure-summary-warn">源码已变化，旧证据要复核后才算数</p>}</div>
      <div><h4>证据状态</h4>
        <p className={`tone-${summary.evidence.tone}`}>{summary.evidence.label}</p>
        <p className="field-note">委托有效不等于验收通过；以回执为准。</p></div>
    </div>
    {summary.next && <div className="closure-summary-next">
      <span>推荐下一步：</span>
      <button className="primary" disabled={busy}
        aria-label={`推荐下一步：${delegationActive && summary.next.kind === "reverify" ? "立即检查一次" : summary.next.label}`}
        onClick={() => onNext(summary.next!)}>{delegationActive
        && summary.next.kind === "reverify" ? "委托有效：立即检查一次" : summary.next.label}</button>
    </div>}
  </section>;
}

export function TaskRequirementsCard({value, onChange, disabled}: {
  value: string; onChange: (value: string) => void; disabled: boolean;
}) {
  return <section className="task-requirements" aria-label="本次验收要求">
    <h4>本次要取得什么测试依据？</h4>
    <label htmlFor="task-requirements">验收要求（可选，每行一项）</label>
    <textarea id="task-requirements" rows={3} value={value} disabled={disabled}
      onChange={event => onChange(event.target.value)}
      placeholder="例如：新增绩点计算包含不及格课程的测试依据" />
    <p className="field-note">随新审查保存，在第 6 步关联具体发现并处置。这里只记录你确认的要求，不自动认定功能已完成。</p>
  </section>;
}

export function TaskClosure({reviewId, reviewComplete, offline, ranAsAgent, findings,
  preferredTaskId, api, onOpenReview, onOpenRepair, onNewAgentReview}: {
  reviewId?: string; reviewComplete: boolean; offline: boolean; ranAsAgent: boolean;
  findings: Finding[]; preferredTaskId?: string; api: Api;
  onOpenReview: (id: string) => void; onOpenRepair: () => void; onNewAgentReview: () => void;
}) {
  const [tasks, setTasks] = useState<EvidenceTask[]>([]);
  const [selected, setSelected] = useState(preferredTaskId || "");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [requirements, setRequirements] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  const [profile, setProfile] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [existingCandidates, setExistingCandidates] = useState<ExistingCandidate[]>([]);
  // N04：真实页面数据 —— 轮次历史、后台作业、教学叙述（全部来自服务端 API）。
  const [roundsData, setRoundsData] = useState<{active_round_id: string; rounds: ServerRoundRow[]} | null>(null);
  const [jobs, setJobs] = useState<JobView[]>([]);
  const [jobsLoaded, setJobsLoaded] = useState(false);
  const [jobBusy, setJobBusy] = useState("");
  const [teachingLessons, setTeachingLessons] = useState<Array<{case_id: string; narration: TeachingNarration}> | null>(null);
  const [teachingError, setTeachingError] = useState("");
  // 持续委托（P0-02）：授权与事件全部只读拉取；创建/停止/检查都是显式按钮。
  const [delegations, setDelegations] = useState<DelegationView[]>([]);
  const [delegationLoaded, setDelegationLoaded] = useState(false);
  const [delegationBusy, setDelegationBusy] = useState(false);
  const [delegationEvents, setDelegationEvents] = useState<DelegationEvent[]>([]);
  const generation = useRef(0);
  const task = tasks.find(item => item.task_id === selected) || tasks[0];
  const delegation = delegations[0] || null;
  const delegationActive = !!delegation && (delegation.derived_status || delegation.status) === "active";
  const replaceTask = (next: EvidenceTask) => {
    setTasks(current => current.some(item => item.task_id === next.task_id)
      ? current.map(item => item.task_id === next.task_id ? next : item) : [...current, next]);
    setSelected(next.task_id);
  };
  async function refresh() {
    if (!reviewId || offline) return;
    const currentGeneration = generation.current;
    setLoading(true); setError("");
    try {
      const response = await api<{tasks: EvidenceTask[]}>(`/api/v2/tasks?review_id=${encodeURIComponent(reviewId)}`);
      if (currentGeneration === generation.current) {setTasks(response.tasks); setLoaded(true);}
      const sources = await Promise.allSettled([
        api<{proposal?: {code_sha256?: string; status?: string}}>(`/api/v2/reviews/${encodeURIComponent(reviewId)}/test-proposal`),
        api<{candidate?: {patch_sha256?: string; status?: string}}>(`/api/v2/reviews/${encodeURIComponent(reviewId)}/repair`),
      ]);
      const candidates: ExistingCandidate[] = [];
      if (sources[0].status === "fulfilled" && sources[0].value.proposal?.code_sha256)
        candidates.push({source: "test_proposal", sha256: sources[0].value.proposal.code_sha256,
          label: `补测提议 · ${sources[0].value.proposal.status || "待服务端核验"}`});
      if (sources[1].status === "fulfilled" && sources[1].value.candidate?.patch_sha256)
        candidates.push({source: "repair_candidate", sha256: sources[1].value.candidate.patch_sha256,
          label: `受控修复候选 · ${sources[1].value.candidate.status || "待服务端核验"}`});
      if (currentGeneration === generation.current) setExistingCandidates(candidates);
    } catch (err) {if (currentGeneration === generation.current) setError(readableError(err));}
    finally {if (currentGeneration === generation.current) setLoading(false);}
  }
  useEffect(() => {
    generation.current += 1; setTasks([]); setSelected(preferredTaskId || ""); setLoaded(false); setExistingCandidates([]); setBusy(""); setError("");
    setDelegations([]); setDelegationEvents([]); setDelegationLoaded(false);
    void refresh();
    return () => {generation.current += 1;};
  }, [reviewId, offline]);
  useEffect(() => {if (preferredTaskId) setSelected(preferredTaskId);}, [preferredTaskId]);
  // N04：轮次历史随任务读取；任务状态推进（轮次可能新增）后一起刷新。
  const statusKey = task?.criteria.map(item => item.status).join(",") || "";
  useEffect(() => {
    if (!task?.task_id || offline) {setRoundsData(null); return;}
    const currentGeneration = generation.current;
    let active = true;
    void api<{active_round_id: string; rounds: ServerRoundRow[]}>(
      `/api/v2/tasks/${encodeURIComponent(task.task_id)}/rounds`)
      .then(data => {
        // 形状防御：响应不符合轮次契约（如旧服务端/SPA 回退页）时保持
        // null——不渲染轮次区，也绝不带着 undefined 进入渲染。
        if (active && currentGeneration === generation.current
            && data && Array.isArray(data.rounds)) {
          setRoundsData(data);
        }
      })
      .catch(() => {if (active && currentGeneration === generation.current) setRoundsData(null);});
    return () => {active = false;};
  }, [task?.task_id, offline, statusKey]);
  // N04：后台作业列表（触发分发等）。有在途作业时 2.5 秒轮询，其余静默。
  useEffect(() => {
    if (!task?.task_id || offline) {setJobs([]); setJobsLoaded(false); return;}
    const currentGeneration = generation.current;
    let active = true;
    async function loadJobs() {
      try {
        const data = await api<{jobs: JobView[]}>(`/api/v2/jobs?task_id=${encodeURIComponent(task!.task_id)}`);
        const rows = Array.isArray(data?.jobs) ? data.jobs : [];
        if (active && currentGeneration === generation.current) {setJobs(rows); setJobsLoaded(true);}
      } catch {if (active && currentGeneration === generation.current) setJobsLoaded(false);}
    }
    void loadJobs();
    const hasActive = jobs.some(job => !isTerminal(job.status));
    // 有在途作业时顺带刷新委托卡：复验完成后最近回执/额度立即跟上。
    const timer = hasActive
      ? setInterval(() => {void loadJobs(); void loadDelegations();}, 2500)
      : null;
    return () => {active = false; if (timer) clearInterval(timer);};
  }, [task?.task_id, offline, jobs.some(job => !isTerminal(job.status))]);
  async function refreshJobs() {
    if (!task?.task_id) return;
    try {
      const data = await api<{jobs: JobView[]}>(`/api/v2/jobs?task_id=${encodeURIComponent(task.task_id)}`);
      setJobs(data.jobs); setJobsLoaded(true);
    } catch (err) {if (err instanceof Error) setError(readableError(err));}
  }
  async function cancelJob(jobId: string) {
    if (jobBusy) return;
    setJobBusy(jobId); setError("");
    try {
      await api(`/api/v2/jobs/${encodeURIComponent(jobId)}/cancel`,
        {method: "POST", body: JSON.stringify({})});
    } catch (err) {setError(readableError(err));}
    finally {setJobBusy(""); await refreshJobs();}
  }
  async function toggleTeaching(open: boolean) {
    if (!open || teachingLessons || teachingError) return;
    try {
      const data = await api<{lessons: Array<{case_id: string; narration: TeachingNarration}>}>(
        "/api/v2/teaching/lessons");
      setTeachingLessons(data.lessons);
    } catch (err) {setTeachingError(readableError(err));}
  }
  // Server derives statuses and outcomes. Polling never starts a review or applies a patch.
  useEffect(() => {
    if (!task?.criteria.some(item => ["generating", "applying", "adopted", "reviewing"].includes(item.status))) return;
    const currentGeneration = generation.current;
    let active = true;
    const timer = setInterval(() => {
      void api<EvidenceTask>(`/api/v2/tasks/${encodeURIComponent(task.task_id)}`)
        .then(next => {if (active && currentGeneration === generation.current) replaceTask(next);})
        .catch(err => {if (active && currentGeneration === generation.current) setError(readableError(err));});
    }, 2500);
    return () => {active = false; clearInterval(timer);};
  }, [task?.task_id, task?.criteria.map(item => item.status).join(",")]);
  async function action(criterionId: string, kind: string, data: Record<string, unknown> = {}) {
    if (!task || busy) return;
    const currentGeneration = generation.current;
    setBusy(criterionId); setError("");
    try {
      const next = await api<EvidenceTask>(`/api/v2/tasks/${encodeURIComponent(task.task_id)}/actions`, {
        method: "POST", headers: {"Idempotency-Key": crypto.randomUUID()},
        body: JSON.stringify({action: kind, criterion_id: criterionId, ...data}),
      });
      if (currentGeneration === generation.current) replaceTask(next);
    } catch (err) {if (currentGeneration === generation.current) setError(readableError(err));}
    finally {if (currentGeneration === generation.current) setBusy("");}
  }
  async function create() {
    if (!reviewId || busy || !requirements.trim()) return;
    const currentGeneration = generation.current;
    setBusy("create"); setError("");
    try {
      const next = await api<EvidenceTask>("/api/v2/tasks", {method: "POST",
        headers: {"Idempotency-Key": crypto.randomUUID()},
        body: JSON.stringify({review_id: reviewId, title: title.trim() || "本次测试证据处置",
          criteria: taskRequirements(requirements).map(item => ({...item,
            ...(profile ? {evidence_kind: profile === "browser-memory-demo" ? "browser_behavior" : "test_execution", profile_id: profile} : {})}))}),
      });
      if (currentGeneration === generation.current) {
        replaceTask(next); setCreatingNew(false); setRequirements(""); setTitle("");
      }
    } catch (err) {if (currentGeneration === generation.current) setError(readableError(err));}
    finally {if (currentGeneration === generation.current) setBusy("");}
  }
  async function downloadReceipt() {
    if (!task) return;
    try {downloadJson(await api(`/api/v2/tasks/${encodeURIComponent(task.task_id)}/receipt`), `${task.task_id}-receipt.json`);}
    catch (err) {setError(readableError(err));}
  }
  // ---- 持续委托：只读加载 + 三个显式动作（创建/停止/立即检查） ----------
  async function loadDelegations() {
    if (!task?.task_id || offline) {setDelegations([]); setDelegationEvents([]); setDelegationLoaded(false); return;}
    try {
      const data = await api<{delegations: DelegationView[]}>(
        `/api/v2/delegations?task_id=${encodeURIComponent(task.task_id)}`);
      const rows = Array.isArray(data?.delegations) ? data.delegations : [];
      setDelegations(rows); setDelegationLoaded(true);
      const current = rows[0];
      if (current) {
        const events = await api<{events: DelegationEvent[]}>(
          `/api/v2/delegations/${encodeURIComponent(current.auth_id)}/events`);
        setDelegationEvents(Array.isArray(events?.events) ? events.events : []);
      } else setDelegationEvents([]);
    } catch {setDelegationLoaded(false);}
  }
  useEffect(() => {void loadDelegations();}, [task?.task_id, offline, statusKey]);
  async function delegationAction(kind: "create" | "stop" | "check") {
    if (!task?.task_id || delegationBusy) return;
    setDelegationBusy(true); setError("");
    try {
      if (kind === "create")
        await api("/api/v2/delegations", {method: "POST",
          headers: {"Idempotency-Key": crypto.randomUUID()},
          body: JSON.stringify({task_id: task.task_id})});
      else if (kind === "stop" && delegation)
        await api(`/api/v2/delegations/${encodeURIComponent(delegation.auth_id)}/stop`,
          {method: "POST", body: JSON.stringify({})});
      else if (kind === "check")
        await api("/api/v2/delegations/check", {method: "POST",
          body: JSON.stringify({task_id: task.task_id, reason: "user_click"})});
      await loadDelegations();
      await refreshJobs();
    } catch (err) {setError(readableError(err));}
    finally {setDelegationBusy(false);}
  }
  function runNext(next: NonNullable<TaskSummary["next"]>) {
    if (!task) return;
    if (next.kind === "receipt") {void downloadReceipt(); return;}
    if (next.kind === "reverify") {
      if (delegationActive) {void delegationAction("check"); return;}
      if (busy) return;
      const currentGeneration = generation.current;
      setBusy("reverify"); setError("");
      void api<EvidenceTask>(`/api/v2/tasks/${encodeURIComponent(task.task_id)}/reverify`,
        {method: "POST", body: JSON.stringify({})})
        .then(nextTask => {if (currentGeneration === generation.current) replaceTask(nextTask);})
        .catch(err => {if (currentGeneration === generation.current) setError(readableError(err));})
        .finally(() => {if (currentGeneration === generation.current) setBusy("");});
      return;
    }
    if (next.kind === "scroll" && next.criterion_id)
      document.getElementById(`criterion-${next.criterion_id}`)
        ?.scrollIntoView({behavior: "smooth", block: "start"});
  }
  return <section className="panel task-closure" aria-label="处置与复验">
    <div className="panel-title"><span>06</span><h3>处置与复验</h3></div>
    <p className="closure-lead">把一个测试证据缺口，推进到有版本依据的处置结果。</p>
    <p className="field-note">选择处置 → 验证精确候选 → 确认采用 → 新版本复验。候选有效、代码已采用与目标已补证分别记录。</p>
    {!reviewId ? <p className="empty">先创建并完成一次审查，再关联需要处置的发现。</p>
      : offline ? <p className="empty">当前为离线证据回放。回放可查看原始依据；采用与复验需要连接原仓库的在线审查。</p>
      : <>
        <div className="closure-toolbar">
          <button disabled={loading || !!busy} onClick={() => void refresh()}>{loading ? "正在读取…" : "重新读取处置状态"}</button>
          {task && <><button onClick={() => void downloadReceipt()}>下载处置回执</button>
            <button disabled={!!busy} onClick={() => setCreatingNew(value => !value)}>{creatingNew ? "收起新要求" : "记录另一组验收要求"}</button></>}
        </div>
        {error && <p className="closure-error" role="alert">{error}</p>}
        {tasks.length > 1 && <label>本次审查的处置任务<select value={task?.task_id || ""}
          onChange={event => setSelected(event.target.value)}>{tasks.map(item =>
          <option key={item.task_id} value={item.task_id}>{item.title}</option>)}</select></label>}
        {task ? <>
          <div className="closure-task-header"><h4>{task.title}</h4>
            <a href={taskLink(task.task_id)} onClick={() => history.replaceState(null, "", taskLink(task.task_id))}>此任务链接</a></div>
          <p className="field-note">原审查 <code>{task.origin_review_id}</code> · 任务 <code>{task.task_id}</code></p>
          {!reviewComplete && <p className="closure-notice">原审查尚未结束，先完成计划确认与实验。处置动作以服务端允许范围为准。</p>}
          <TaskSummaryCard summary={summarizeTask(task,
            roundsData?.rounds.find(row => row.round_id === (roundsData?.active_round_id || task.active_round_id)))}
            busy={!!busy || delegationBusy} delegationActive={delegationActive} onNext={runNext} />
          {(() => {
            const latestJob = jobs[jobs.length - 1];
            const terminal = deriveTaskTerminalState({
              authorization: delegation || undefined,
              job: latestJob,
              receipt: {status: aggregateEvidence(task).status},
              source_snapshot_matches: task.criteria.every(item => item.source_snapshot_matches !== false),
            });
            return <section className={`closure-terminal-state tone-${terminal.tone}`} aria-label="当前验收状态">
              <strong>{terminal.label}</strong><p>{terminal.detail}</p>
              {terminal.stopReason && <p className="field-note">原因：{terminal.stopReason}</p>}
            </section>;
          })()}
          {task.criteria.map(criterion => <CriterionCard key={criterion.criterion_id} criterion={criterion}
            findings={findings} busy={!!busy} ranAsAgent={ranAsAgent} existingCandidates={existingCandidates}
            onAction={(kind, data) => action(criterion.criterion_id, kind, data)}
            onOpenReview={onOpenReview} onOpenRepair={onOpenRepair} onNewAgentReview={onNewAgentReview} />)}
          {roundsData && <RoundHistoryView rounds={roundsData.rounds.map(adaptRoundRow)}
            activeRoundId={roundsData.active_round_id || task.active_round_id} />}
          <section className="closure-jobs" aria-label="后台作业">
            <h4>后台作业</h4>
            {!jobsLoaded ? <p className="field-note">尚未读取作业记录。</p>
              : jobs.length === 0 ? <p className="field-note">该任务当前没有后台作业；触发分发、复验等作业会在这里出现。</p>
              : jobs.slice(-3).map(job => <JobProgressView key={job.job_id} job={job}
                busy={jobBusy === job.job_id} onCancel={id => void cancelJob(id)} />)}
            <button disabled={!!jobBusy} onClick={() => void refreshJobs()}>重新读取作业</button>
          </section>
          <DelegationCard delegation={delegation} events={delegationEvents}
            loaded={delegationLoaded} busy={delegationBusy}
            requirementText={task.criteria[0]?.text || task.title}
            onCreate={() => void delegationAction("create")}
            onStop={() => void delegationAction("stop")}
            onCheckNow={() => void delegationAction("check")}
            onRefresh={() => void loadDelegations()} />
          <details className="closure-teaching" aria-label="教学视图"
            onToggle={event => void toggleTeaching((event.target as HTMLDetailsElement).open)}>
            <summary>教学视图：一次干预实验如何下结论（示例案例）</summary>
            {teachingError && <p className="closure-error" role="alert">{teachingError}</p>}
            {!teachingLessons && !teachingError && <p className="field-note">正在读取教学案例…</p>}
            {teachingLessons?.map(lesson => <TeachingView key={lesson.case_id} narration={lesson.narration} />)}
          </details>
        </> : null}
        {loaded && (!task || creatingNew) && <div className="closure-create">
          <h4>记录需要处置的验收要求</h4>
          <label>任务名称<input value={title} onChange={event => setTitle(event.target.value)} placeholder="本次测试证据处置" /></label>
          <label>验收证据类型<select value={profile} onChange={event => setProfile(event.target.value)} disabled={!!busy}>
            <option value="">Python 代码实验</option>
            <option value="ts-vitest-demo">TypeScript 测试执行（本产品实验）</option>
            <option value="js-vitest-demo">JavaScript 测试执行（本产品实验）</option>
            <option value="browser-memory-demo">浏览器规则记忆行为（本产品实验）</option>
          </select></label>
          {profile && <p className="field-note">实验配置仅适用于已登记的水木验码开发仓库，其他仓库返回不适用。执行或行为证据不等于 Python 逐行实验结论。</p>}
          <TaskRequirementsCard value={requirements} onChange={setRequirements} disabled={!!busy} />
          <button className="primary" disabled={!!busy || !requirements.trim()} onClick={() => void create()}>保存要求并开始处置</button>
        </div>}
      </>}
  </section>;
}

function CriterionCard({criterion: c, findings, busy, ranAsAgent, existingCandidates, onAction, onOpenReview, onOpenRepair, onNewAgentReview}: {
  criterion: TaskCriterion; findings: Finding[]; busy: boolean; ranAsAgent: boolean; existingCandidates: ExistingCandidate[];
  onAction: (action: string, data?: Record<string, unknown>) => Promise<void>;
  onOpenReview: (id: string) => void; onOpenRepair: () => void; onNewAgentReview: () => void;
}) {
  const [targets, setTargets] = useState(c.finding_ids || []);
  const [disposition, setDisposition] = useState(c.disposition || "add_tests");
  const [reason, setReason] = useState("");
  const [handler, setHandler] = useState("");
  const [source, setSource] = useState(c.disposition === "controlled_repair" ? "repair_candidate" : "test_proposal");
  const [digest, setDigest] = useState("");
  const [budget, setBudget] = useState(120);
  const [confirmed, setConfirmed] = useState(false);
  const allowed = (action: string) => !busy && canTaskAction(c, action);
  useEffect(() => {setConfirmed(false);}, [c.adoption_plan?.plan_sha256]);
  const plan = c.adoption_plan;
  const existing = existingCandidates.find(item => item.source === source);
  const bindingDigest = digest.trim() || existing?.sha256 || "";
  return <article id={`criterion-${c.criterion_id}`} className="closure-criterion">
    <header><h4>{c.text}</h4><span className="closure-status">{STATUS[c.status] || c.status}</span></header>
    {c.evidence_kind && <p className="field-note">证据类型：{{python_experiment: "Python 代码实验", test_execution: "测试执行证据", browser_behavior: "浏览器行为验收"}[c.evidence_kind] || c.evidence_kind}</p>}
    {c.source_snapshot_matches === false && <p className="closure-notice" role="status">当前工作区已变化。以下为原版本的历史证据，需要新审查才能说明当前版本。</p>}
    <div className="closure-outcome" data-outcome={c.outcome?.status || "pending"}>
      <strong>{c.outcome?.status === "supported" && c.evidence_kind === "browser_behavior" ? "登记的浏览器行为验收通过"
        : c.outcome?.status === "supported" && c.evidence_kind === "test_execution" ? "登记的测试执行通过"
        : c.outcome?.status === "supported" && c.outcome.improved === true ? "原目标新增了测试依据" : taskOutcomeLabel(c.outcome?.status)}</strong><p>{c.outcome?.reason || "原有结论保留，等待处置与复验依据。"}</p>
    </div>
    {(!c.evidence_kind || c.evidence_kind === "python_experiment") && canTaskAction(c, "set_targets") && <fieldset disabled={busy}>
      <legend>关联这项要求的具体发现</legend>
      {findings.length ? <div className="closure-findings">{findings.map(finding => {
        const id = `${finding.file}:${finding.line}`;
        return <label key={id}><input type="checkbox" checked={targets.includes(id)} onChange={event =>
          setTargets(old => event.target.checked ? [...old, id] : old.filter(value => value !== id))} />
          <span><code>{id}</code> · {finding.label}<small>{finding.text}</small></span></label>;
      })}</div> : <p className="field-note">还没有可关联的逐行发现。</p>}
      <button disabled={!allowed("set_targets") || !targets.length}
        onClick={() => void onAction("set_targets", {finding_ids: targets})}>保存关联发现</button>
    </fieldset>}
    {c.finding_ids?.length > 0 && <p className="field-note">已绑定：{c.finding_ids.join("、")}</p>}
    {(!c.evidence_kind || c.evidence_kind === "python_experiment") && canTaskAction(c, "select_disposition") && <fieldset disabled={busy}>
      <legend>选择如何处置</legend>
      <div className="closure-options">{[["add_tests", "补充测试"], ["controlled_repair", "受控修复"], ["accept_risk", "接受风险"]].map(([value, label]) =>
        <label key={value}><input type="radio" name={`disposition-${c.criterion_id}`} value={value}
          checked={disposition === value} onChange={() => setDisposition(value)} />{label}</label>)}</div>
      {disposition === "accept_risk" && <>
        <p className="field-note">记录人的决定和适用版本，保留原缺口，不会把机器结论改为通过。</p>
        <label>风险负责人<input value={handler} onChange={event => setHandler(event.target.value)} /></label>
        <label>接受理由<textarea value={reason} onChange={event => setReason(event.target.value)} /></label>
      </>}
      <button disabled={!allowed("select_disposition") || (disposition === "accept_risk" && (!reason.trim() || !handler.trim()))}
        onClick={() => void onAction("select_disposition", {disposition,
          ...(disposition === "accept_risk" ? {reason: reason.trim(), handled_by: handler.trim()} : {})})}>
        {disposition === "accept_risk" ? "记录风险决定" : "确认处置路径"}</button>
    </fieldset>}
    {c.disposition === "add_tests" && canTaskAction(c, "propose_test") && <div className="closure-generate">
      {ranAsAgent ? <button className="primary" disabled={!allowed("propose_test")}
        onClick={() => void onAction("propose_test")}>生成并验证补测候选</button>
        : <><p className="field-note">本次标准审查保持零模型调用。AI 补测需另建有模型授权的智能体审查。</p>
          <button onClick={onNewAgentReview} disabled={busy}>配置新的智能体审查</button></>}
    </div>}
    {c.disposition === "controlled_repair" && !c.candidate && <p>
      <button onClick={onOpenRepair} disabled={busy}>前往受控修复候选</button>
      <span className="field-note">先在结论页准备并验证候选，再返回这里绑定。</span></p>}
    {canTaskAction(c, "bind_candidate") && <details className="closure-bind">
      <summary>绑定已验证候选</summary>
      <label>候选来源<select value={source} onChange={event => setSource(event.target.value)} disabled={busy}>
        <option value="test_proposal">已有补测提议</option><option value="repair_candidate">已有受控修复候选</option></select></label>
      {existing ? <p>{existing.label}<code className="closure-digest">{existing.sha256}</code></p>
        : <p className="field-note">当前未读取到此来源的候选。先生成或验证候选，再重新读取处置状态。</p>}
      <details><summary>指定已有候选指纹</summary><label>候选 SHA-256<input value={digest} onChange={event => setDigest(event.target.value)} placeholder="与服务端候选记录一致的完整指纹" disabled={busy} /></label></details>
      <button disabled={!allowed("bind_candidate") || !/^[a-f0-9]{64}$/i.test(bindingDigest)}
        onClick={() => void onAction("bind_candidate", {source, expected_sha256: bindingDigest})}>绑定这一份候选</button>
    </details>}
    {c.proposal_attempt && !c.candidate && <div className="closure-notice" role="status">
      本次补测未形成可采用候选。{String(c.proposal_attempt.status || c.proposal_attempt.reason || "请查看验证记录。")}
      <details><summary>查看补测尝试记录</summary><pre>{JSON.stringify(c.proposal_attempt, null, 2)}</pre></details>
    </div>}
    {c.candidate && <section className="closure-candidate" aria-label="精确候选预览">
      <h5>精确候选预览</h5>
      <CandidateStatusChips candidate={candidateChipInput(c)} />
      <p className="field-note">以下为服务端封存、后续采用使用的同一份差异。</p>
      <code className="closure-digest">{c.candidate.patch_sha256}</code>
      <pre tabIndex={0}>{c.candidate.patch}</pre>
      <p>候选验证：{String(c.candidate.verification?.status || "记录暂缺")} · {c.candidate.verification?.kind === "effective_test" ? "补测有效性" : "声明测试"}</p>
    </section>}
    {(() => {
      const request = mappingRequest(c);
      return request && canTaskAction(c, "confirm_target_mapping")
        ? <MappingConfirmation request={request} busy={busy}
          onDecide={(decision: ConfirmDecision) => void onAction("confirm_target_mapping",
            {mapping_id: decision.mapping_id, confirmed: decision.confirmed,
              operator_note: decision.operator_note})} />
        : null;
    })()}
    {canTaskAction(c, "prepare_adoption") && <div className="closure-prepare">
      <label>复验预算（秒）<input type="number" min={10} max={3600} value={budget}
        onChange={event => setBudget(Number(event.target.value))} /></label>
      <button className="primary" disabled={!allowed("prepare_adoption") || !Number.isInteger(budget) || budget < 10 || budget > 3600}
        onClick={() => void onAction("prepare_adoption", {budget_seconds: budget})}>准备采用与复验计划</button>
      <p className="field-note">先隔离验证原测试与新增测试，完成后再确认写入。</p>
    </div>}
    {plan && <section className="closure-adoption" aria-label="采用确认">
      <h5>{["applying", "adopted", "reviewing", "reviewed"].includes(c.status)
        ? "本次采用记录" : "采用这一份变更，并检查新版本"}</h5>
      <dl><dt>受影响文件</dt><dd>{plan.paths.join("、")}</dd>
        <dt>新审查测试范围</dt><dd>{plan.test_files.join("、")}</dd>
        <dt>复验预算</dt><dd>{plan.budget_seconds} 秒</dd>
        <dt>原工作区指纹</dt><dd><code>{plan.source_snapshot_sha256}</code></dd>
        <dt>计划指纹</dt><dd><code>{plan.plan_sha256}</code></dd>
        <dt>有效期</dt><dd>{typeof plan.expires_at === "number" ? new Date(plan.expires_at * 1000).toLocaleString() : plan.expires_at || "以服务端核对为准"}</dd></dl>
      {canTaskAction(c, "confirm_adoption") && <>
        <label className="closure-confirm"><input type="checkbox" checked={confirmed} disabled={busy}
          onChange={event => setConfirmed(event.target.checked)} />
          我确认把上述精确变更应用到当前工作区，并按所列范围和预算发起新审查。</label>
        <p className="field-note">保留已有改动和暂存状态；如快照已变化，将停止采用。不会自动提交或推送。</p>
        <button className="primary" disabled={!allowed("confirm_adoption") || !confirmed}
          onClick={() => void onAction("confirm_adoption", {plan_sha256: plan.plan_sha256})}>确认采用并复验</button>
      </>}
      {canTaskAction(c, "auto_confirm_adoption") && <>
        <p className="field-note">本任务已显式启用预授权实验。服务端会再次核对授权、范围、预算和当前快照，然后自动采用并发起新版本复验。</p>
        <button className="primary" disabled={!allowed("auto_confirm_adoption")}
          onClick={() => void onAction("auto_confirm_adoption")}>按预授权自动采用并复验</button>
      </>}
    </section>}
    {c.followup_review_id && <p><button onClick={() => onOpenReview(c.followup_review_id!)}>查看新版本审查</button>
      <code>{c.followup_review_id}</code></p>}
    {canTaskAction(c, "recover_adoption") && <div className="closure-notice">
      <p>采用过程曾中断。先核对实际文件与操作日志，服务端只在可以确定恢复方式时继续。</p>
      <button disabled={!allowed("recover_adoption")} onClick={() => void onAction("recover_adoption")}>核对并恢复中断的采用</button>
    </div>}
    {c.evidence_kind && c.evidence_kind !== "python_experiment" && canTaskAction(c, "run_experiment") && <div>
      <p className="field-note">使用服务端登记的 {c.profile_id || "实验"} 配置实际运行，回执单独记录执行与验收结果。</p>
      <button disabled={!allowed("run_experiment")} onClick={() => void onAction("run_experiment")}>运行已登记的验收实验</button>
    </div>}
    {c.experiment && <ExperimentReceipt receipt={c.experiment} />}
    {canTaskAction(c, "retry_review") && <button disabled={!allowed("retry_review")}
      onClick={() => void onAction("retry_review")}>重试新版本复验</button>}
    {(c.outcome?.origin_rows?.length || c.outcome?.followup_rows?.length) ? <details className="closure-comparison">
      <summary>查看目标前后证据</summary><div>
        <section><h5>原审查证据</h5><EvidenceRows rows={c.outcome.origin_rows || []} /></section>
        <section><h5>新版本证据</h5><EvidenceRows rows={c.outcome.followup_rows || []} /></section>
      </div></details> : null}
    {busy && <p role="status">正在处理，请等待服务端结果。你可以稍后重新读取状态。</p>}
  </article>;
}

// 服务端 rounds 行（_rounds_view）→ RoundHistoryView 契约形状。
export type ServerRoundRow = {round_id: string; round_no: number; status: string;
  superseded_by?: string; supersede_reason?: string; requirement_version: string;
  created_at: number; closed_at?: number;
  criteria_outcomes: Array<{criterion_id: string; status: string; outcome_status: string}>};
export function adaptRoundRow(row: ServerRoundRow): RoundRow {
  return {round_id: row.round_id, round_no: row.round_no,
    status: row.status === "superseded" ? "superseded" : "active",
    superseded_by: row.superseded_by || undefined,
    supersede_reason: (row.supersede_reason || undefined) as RoundRow["supersede_reason"],
    requirement_version: row.requirement_version,
    created_at: row.created_at, closed_at: row.closed_at || undefined,
    criteria: (row.criteria_outcomes || []).map(item => ({
      criterion_id: item.criterion_id,
      outcome: (item.outcome_status || "pending") as RoundOutcome}))};
}

// 候选与采用状态 → CandidateStatusChips 的五维度输入。只映射服务端真实
// 记录的状态，不猜维度：visa 只在候选携带签证记录时出现。
export function candidateChipInput(c: TaskCriterion) {
  const verification = (c.candidate?.verification || {}) as
    {status?: string; visa?: {status?: string}};
  const verificationStatus =
    verification.status === "VERIFIED_EFFECTIVE" ? "passed"
    : verification.status === "failed" ? "failed" : "not_run";
  const visa = verification.visa?.status
    ? {status: (verification.visa.status === "VERIFIED_EFFECTIVE" ? "eligible"
      : "ineligible") as "eligible" | "ineligible"}
    : undefined;
  const adoptionStatus =
    ["adopted", "reviewing", "reviewed"].includes(c.status) ? "applied"
    : c.status === "prepared" ? "prepared" : "not_prepared";
  return {candidate_id: (c.candidate?.patch_sha256 || c.criterion_id || "").slice(0, 18),
    verification: {status: verificationStatus as "passed" | "failed" | "not_run"},
    visa,
    adoption: {status: adoptionStatus as "applied" | "prepared" | "not_prepared"}};
}

// 服务端映射报告 → MappingConfirmation 的请求形状（needs_confirmation 的项）。
export function mappingRequest(c: TaskCriterion) {
  const entry = (c.target_mapping?.mappings || [])
    .find(item => item.status === "needs_confirmation");
  if (!entry) return null;
  return {
    mapping_id: entry.mapping_id,
    needs_confirmation: true as const,
    reason: entry.reason || "duplicate_code",
    original: {path: entry.origin?.file || "", start_line: entry.origin?.line || 0,
      end_line: entry.origin?.line || 0},
    candidates: (entry.candidates || []).map((candidate, index) => ({
      target_id: `${entry.mapping_id}-${index}`,
      path: candidate.file, start_line: candidate.line, end_line: candidate.line,
      basis: candidate.basis})),
  };
}

function EvidenceRows({rows}: {rows: unknown[]}) {  return rows.length ? <div>{rows.map((value, index) => {
    const row = value && typeof value === "object" ? value as Record<string, unknown> : {};
    return <div key={index} className="closure-evidence-row">
      <strong>{String(row.label || "未标注")}</strong> <code>{String(row.file || "—")}:{String(row.line || "—")}</code>
      <pre>{String(row.text || "原片段暂缺")}</pre>
      <p className="field-note">{String(row.reason || "依据见原审查证据包")}</p>
      {Array.isArray(row.evidence_ids) && <small>证据：{row.evidence_ids.join("、") || "暂无"}</small>}
    </div>;
  })}</div> : <p className="field-note">未取得对应目标的证据记录。</p>;
}
function ExperimentReceipt({receipt}: {receipt: Record<string, unknown>}) {
  return <section className="closure-experiment" aria-label="独立验收实验回执">
    <h5>独立验收实验回执</h5>
    <p>执行状态：{String(receipt.execution_status || receipt.status || "未提供")}</p>
    <p>验收结果：{String(receipt.acceptance_result || receipt.result || "以原始回执为准")}</p>
    <p className="field-note">{String(receipt.reason || receipt.boundary || "本结果只说明本次登记的测试执行或浏览器行为，不签发 Python 逐行证据。")}</p>
    <button onClick={() => downloadJson(receipt, "independent-experiment-receipt.json")}>下载实验原始回执</button>
    <details><summary>查看完整实验记录</summary><pre>{JSON.stringify(receipt, null, 2)}</pre></details>
  </section>;
}
