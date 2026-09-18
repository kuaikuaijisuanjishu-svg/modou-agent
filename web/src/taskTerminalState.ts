/**
 * A conservative read-only projection for the task closure panel.
 *
 * The server remains authoritative.  This function only combines the latest
 * authorization decision, the current job and the current receipt so a stale
 * green result cannot hide a stop, an unfinished run, or a moved source tree.
 */
export type TaskTerminalInput = {
  authorization?: {
    status?: string;
    derived_status?: string;
    stop_reason?: string;
    stopped_at?: number;
    expires_at?: number;
  };
  job?: {
    status?: string;
    termination_state?: string;
    stop_reason?: string;
    cancel_requested?: boolean;
    ended_at?: number;
    error?: {code?: string; detail?: string};
  };
  receipt?: {status?: string; reason?: string};
  source_snapshot_matches?: boolean;
};

export type TaskTerminalKey =
  | "idle" | "running" | "stopping" | "needs_recovery" | "stopped"
  | "expired" | "budget_exhausted" | "evidence_stale" | "regression_observed"
  | "environment_failed" | "inconclusive" | "supported" | "gap_remains";

export type TaskTerminalState = {
  key: TaskTerminalKey;
  label: string;
  detail: string;
  tone: "neutral" | "active" | "warning" | "danger" | "success";
  primaryAction: "wait" | "retry" | "configure" | "view_logs" | "reverify" | "none";
  stopReason?: string;
  stoppedAt?: number;
};

function stopReasonLabel(reason: string | undefined): string | undefined {
  return ({
    user_stop: "你停止了委托",
    user_cancel: "你请求停止本次作业",
    expired: "授权期限已到",
    attempt_exhausted: "复验额度已用完",
    requirement_changed: "验收要求已变化，需要重新确认委托",
    budget_exhausted: "本次运行预算已耗尽",
  } as Record<string, string>)[reason || ""] || reason;
}

export function deriveTaskTerminalState(input: TaskTerminalInput): TaskTerminalState {
  const authStatus = input.authorization?.derived_status || input.authorization?.status || "";
  const job = input.job || {};
  const receipt = input.receipt || {};
  const running = job.status === "queued" || job.status === "running";
  const stopping = job.status === "stopping"
    || job.termination_state === "requested"
    || job.termination_state === "unconfirmed"
    || Boolean(job.cancel_requested && running);

  if (stopping) {
    return {key: "stopping", label: "正在停止", tone: "warning", primaryAction: "wait",
      detail: "已收到停止请求，正在等待归属执行线程和进程确认退出；此时不会生成正常成功回执。",
      stopReason: stopReasonLabel(job.stop_reason || input.authorization?.stop_reason)};
  }
  if (job.status === "needs_recovery") {
    return {key: "needs_recovery", label: "需要恢复处理", tone: "danger", primaryAction: "view_logs",
      detail: "仍有归属资源或部分产物需要核对，未确认停止前不会显示为已停止或验收通过。",
      stopReason: stopReasonLabel(job.stop_reason)};
  }
  if (running) {
    return {key: "running", label: "复验运行中", tone: "active", primaryAction: "wait",
      detail: "后台正在执行复验；刷新页面会恢复当前进度。"};
  }

  // A current authorization decision outranks any historical receipt.
  if (authStatus === "stopped") {
    return {key: "stopped", label: "已停止", tone: "danger", primaryAction: "none",
      detail: "委托已停止；历史回执仍保留，但不代表当前版本继续有效。",
      stopReason: stopReasonLabel(input.authorization?.stop_reason),
      stoppedAt: input.authorization?.stopped_at};
  }
  if (authStatus === "expired") {
    return {key: "expired", label: "授权已到期", tone: "warning", primaryAction: "reverify",
      detail: "授权期限已结束，需要重新确认委托后才能继续复验。",
      stopReason: stopReasonLabel(input.authorization?.stop_reason)};
  }
  if (authStatus === "exhausted" || job.stop_reason === "budget_exhausted") {
    return {key: "budget_exhausted", label: "预算或复验额度已耗尽", tone: "warning",
      primaryAction: "configure", detail: "本次额度已按实际用量结算，未完成的结论不可当作通过。",
      stopReason: stopReasonLabel(job.stop_reason || input.authorization?.stop_reason)};
  }
  if (input.source_snapshot_matches === false) {
    return {key: "evidence_stale", label: "证据已过期", tone: "warning", primaryAction: "reverify",
      detail: "当前源码版本已变化，历史依据保留但不能覆盖当前版本；请重新复验。"};
  }
  if (job.status === "failed" && job.error) {
    return {key: "environment_failed", label: "环境失败，暂时无法判定", tone: "danger",
      primaryAction: "configure",
      detail: `执行环境未完成复验（${job.error.code || "未知错误"}），因此不可判定。请检查配置后重试，不能把这次失败当作行为回归。`};
  }
  if (receipt.status === "gap_remains" || receipt.status === "regression_observed") {
    return {key: "regression_observed", label: "已观察到行为回归", tone: "danger",
      primaryAction: "view_logs",
      detail: receipt.reason || "复验观察到目标行为不再满足要求；这是行为结果，不是环境失败。"};
  }
  if (receipt.status === "inconclusive" || receipt.status === "pending"
      || receipt.status === "pending_recheck") {
    return {key: "inconclusive", label: "尚未完成，暂时无法判定", tone: "warning",
      primaryAction: "retry", detail: receipt.reason || "本轮没有形成足够证据，需重试或补充配置。"};
  }
  if (receipt.status === "supported") {
    return {key: "supported", label: "当前版本具备测试依据", tone: "success", primaryAction: "none",
      detail: "当前源码快照与回执一致；该结论仍只覆盖声明的验收范围。"};
  }
  if (receipt.status === "gap_remains") {
    return {key: "gap_remains", label: "仍有证据缺口", tone: "warning", primaryAction: "retry",
      detail: "当前回执明确记录了缺口，但没有足够依据宣称通过。"};
  }
  return {key: "idle", label: "等待验收", tone: "neutral", primaryAction: "retry",
    detail: "尚未形成当前版本的有效回执。"};
}
