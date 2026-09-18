// T06 组件先行：后台作业进度（纯展示，props 形状来自 docs/interfaces/jobs.md）。
// 不接真实后台；接线时由宿主页面轮询 GET /api/v2/jobs/{id} 并传入。
import "../master-views.css";

export type JobKind =
  | "candidate_generation" | "isolated_verification" | "reverification"
  | "domain_experiment" | "adoption_apply" | "trigger_dispatch" | "delegation_check";
export type JobStatus =
  | "queued" | "running" | "completed" | "failed" | "cancelled"
  | "stopping" | "interrupted" | "needs_recovery";
export type JobStopReason = "user_cancel" | "timeout" | "budget_exhausted" | "error"
  | "cancelled_upstream" | "requirement_changed" | "source_snapshot_changed" | "";
export type JobPhase = {name: string; started_at: number; ended_at: number;
  status: "running" | "done" | "failed" | "cancelled"};
export type JobBudget = {budget_id: string; reserved: number; settled: number;
  currency: "seconds" | "units"};

export type JobView = {
  job_id: string;
  kind: JobKind;
  status: JobStatus;
  phases: JobPhase[];
  budget?: JobBudget;
  stop_reason?: JobStopReason;
  error?: {code: string; detail: string};
  termination_state?: "none" | "running" | "requested" | "confirmed" | "unconfirmed";
  termination?: {worker_alive?: boolean; process_alive?: boolean; terminated_at?: number};
  recovery?: {required?: boolean; reason?: string};
  cancel_requested?: boolean;
  created_at: number;
  started_at?: number;
  ended_at?: number;
};

export const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  queued: "排队中", running: "运行中", completed: "已完成", failed: "已失败",
  cancelled: "已取消", stopping: "正在停止（等待资源退出）",
  interrupted: "已中断（待恢复判定）", needs_recovery: "需要恢复处理",
};
export const JOB_STOP_REASON_LABELS: Record<JobStopReason, string> = {
  user_cancel: "用户取消", timeout: "超时", budget_exhausted: "预算耗尽",
  error: "执行错误", cancelled_upstream: "上游取消",
  requirement_changed: "验收要求已变化", source_snapshot_changed: "源码版本已变化", "": "",
};
const JOB_KIND_LABELS: Record<JobKind, string> = {
  candidate_generation: "候选生成", isolated_verification: "隔离验证",
  reverification: "复验", domain_experiment: "领域实验",
  adoption_apply: "补丁采用", trigger_dispatch: "触发分发", delegation_check: "委托复验",
};
const TERMINAL: JobStatus[] = ["completed", "failed", "cancelled", "interrupted", "needs_recovery"];
const CANCELLABLE: JobStatus[] = ["queued", "running"];

export function jobStatusLabel(status: string): string {
  return JOB_STATUS_LABELS[status as JobStatus] || `未识别状态：${status}`;
}
export function jobKindLabel(kind: string): string {
  return JOB_KIND_LABELS[kind as JobKind] || `未识别作业类型：${kind}`;
}
export function jobStopReasonLabel(reason?: string): string | null {
  if (reason === undefined) return null;
  if (reason === "") return null;
  return JOB_STOP_REASON_LABELS[reason as JobStopReason] || `未识别停止原因：${reason}`;
}

export function budgetView(budget?: JobBudget): {
  reserved: number; settled: number; remaining: number; overSettled: boolean; label: string;
} | null {
  if (!budget || typeof budget.reserved !== "number" || typeof budget.settled !== "number") {
    return null;
  }
  const unit = budget.currency === "seconds" ? "秒" : "个单位";
  const remaining = budget.reserved - budget.settled;
  return {
    reserved: budget.reserved, settled: budget.settled, remaining,
    overSettled: remaining < 0,
    label: `共享预算 ${budget.budget_id}：预留 ${budget.reserved} ${unit}，已结算 ${budget.settled} ${unit}` +
      (remaining < 0 ? `（结算超出预留 ${-remaining} ${unit}，需要人工核对）` : `，剩余 ${remaining} ${unit}`),
  };
}

export function jobPhaseRows(phases: JobPhase[] | undefined): Array<
  {name: string; statusLabel: string; durationMs: number; running: boolean}
> {
  return (phases || []).map(phase => ({
    name: phase.name,
    statusLabel: {running: "进行中", done: "完成", failed: "失败", cancelled: "取消"}[phase.status],
    durationMs: phase.ended_at > phase.started_at ? phase.ended_at - phase.started_at : 0,
    running: phase.status === "running",
  }));
}

export function isTerminal(status: JobStatus): boolean {
  return TERMINAL.includes(status);
}
export function canCancel(job: JobView): boolean {
  return CANCELLABLE.includes(job.status);
}
// interrupted 只有在存在部分写入时才升级为 needs_recovery；恢复动作只对 needs_recovery 开放。
export function canResume(job: JobView): boolean {
  return job.status === "needs_recovery";
}
export function recoveryHint(job: JobView): string | null {
  if (job.status === "needs_recovery") {
    return "恢复前先核对实际文件状态，再决定继续、恢复或转人工；不会盲目重放写入。";
  }
  if (job.status === "interrupted") {
    return "作业被中断（服务重启或进程退出）；等待系统核对是否存在部分写入。";
  }
  if (job.status === "stopping") {
    return "已收到停止请求，等待归属线程和进程确认退出；确认前不显示为已停止。";
  }
  return null;
}

export function JobProgressView({job, onCancel, onResume, busy}: {
  job: JobView;
  onCancel?: (jobId: string) => void;
  onResume?: (jobId: string) => void;
  busy?: boolean;
}) {
  const budget = budgetView(job.budget);
  const stopReason = jobStopReasonLabel(job.stop_reason);
  return <section className="mv-section" aria-label="后台作业进度">
    <h4>{jobKindLabel(job.kind)} · {jobStatusLabel(job.status)}</h4>
    <details className="mv-note"><summary>查看技术标识</summary><code>{job.job_id}</code></details>
    {job.phases.length > 0 && <ol className="mv-phases" aria-label="作业阶段">
      {jobPhaseRows(job.phases).map(phase => <li key={phase.name}
        className={phase.running ? "mv-phase-running" : ""}>
        {phase.name}：{phase.statusLabel}
        {phase.durationMs > 0 ? `（${phase.durationMs} ms）` : ""}
      </li>)}
    </ol>}
    {budget && <p className="mv-budget" aria-label="共享预算">{budget.label}</p>}
    {stopReason && <p className="mv-stop" role="status">停止原因：{stopReason}</p>}
    {job.status === "stopping" && <p className="mv-stop" role="status">
      {job.termination?.worker_alive ? "归属执行线程仍在退出" : "正在确认归属资源已退出"}
    </p>}
    {job.error && <p className="mv-invalid" role="alert">错误 {job.error.code}：{job.error.detail}</p>}
    {recoveryHint(job) && <p className="mv-note" role="status">{recoveryHint(job)}</p>}
    <div className="mv-actions">
      {canCancel(job) && onCancel &&
        <button type="button" disabled={busy}
          onClick={() => onCancel(job.job_id)}>取消作业</button>}
      {canResume(job) && onResume &&
        <button type="button" disabled={busy}
          onClick={() => onResume(job.job_id)}>恢复处理</button>}
      {isTerminal(job.status) && <span className="mv-note">作业已结束，刷新页面可重新读取结果。</span>}
    </div>
  </section>;
}
