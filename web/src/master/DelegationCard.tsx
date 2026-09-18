// 持续委托卡（职责层最小闭环的前端面）：展示一条授权的现状——受托目标、
// 授权期限、剩余复验额度、最近事件与最近回执。数据全部来自
// GET /api/v2/delegations*；创建、停止、立即检查是显式按钮动作，
// 组件本身绝不轮询触发检查（GET 不产生实验）。
// 边界如实展示："委托有效"不等于"验收通过"。
export type DelegationEvent = {seq: number; kind: string; at: number;
  details?: Record<string, unknown>};
export type DelegationView = {
  auth_id: string; task_id: string;
  requirement_version: string; requirement_round_id?: string;
  allowed_actions?: string[];
  created_at: number; expires_at: number;
  max_reverify: number; used_reverify: number;
  per_reverify_seconds?: number;
  budget_id?: string; budget_remaining?: number;
  status: string; derived_status?: string;
  stop_reason?: string;
  stopped_at?: number;
  last_check?: {at?: number; snapshot_sha256?: string; outcome?: string};
  last_job_id?: string;
  last_receipt?: {job_id?: string; receipt_status?: string; receipt_sha256?: string};
  last_abort?: {job_id?: string; reason?: string; stage?: string;
    at?: number; receipt_suppressed?: boolean};
  remaining_reverify?: number; seconds_remaining?: number;
};
export type DelegationTone = "ok" | "warn" | "danger" | "neutral";

export function delegationTone(status: string): DelegationTone {
  // 青绿=委托有效；琥珀=需要用户注意（待复验/要求已变/已过期）；
  // 红只给失败与用户停止后的终止态。与产品语义色约定一致。
  if (status === "active") return "ok";
  if (status in {expired: 1, needs_reconfirm: 1}) return "warn";
  if (status === "stopped") return "danger";
  return "neutral";
}

export function delegationStatusLabel(auth: DelegationView): string {
  const status = auth.derived_status || auth.status;
  const labels: Record<string, string> = {
    active: "委托有效",
    expired: "授权已到期",
    exhausted: "复验额度已用完",
    needs_reconfirm: "验收要求已变化，需要重新确认",
    stopped: "已停止委托",
  };
  return labels[status] || `未识别状态：${status}`;
}

const EVENT_LABELS: Record<string, string> = {
  "authorization.created": "创建授权",
  "check_no_change": "检查：源码无变化，未启动实验",
  "reverify_dispatched": "检查：发现变化，已投递复验",
  "receipt_recorded": "收到复验回执",
  "authorization.stopped": "用户停止委托",
  "authorization.stop_requested": "已请求停止委托",
  "reverify_aborted": "复验已中止，未写入正常回执",
  "authorization_expired": "授权到期",
  "attempts_exhausted": "复验额度用完",
  "requirement_changed": "验收要求变化，原授权不再覆盖",
};

export function delegationEventLabel(kind: string): string {
  return EVENT_LABELS[kind] || `事件：${kind}`;
}

export function remainingQuotaLabel(auth: DelegationView): string {
  const remaining = typeof auth.remaining_reverify === "number"
    ? auth.remaining_reverify
    : Math.max(0, auth.max_reverify - auth.used_reverify);
  return `剩余复验额度 ${remaining} / ${auth.max_reverify} 次`;
}

export function expiryLabel(auth: DelegationView): string {
  const seconds = typeof auth.seconds_remaining === "number"
    ? auth.seconds_remaining
    : Math.max(0, auth.expires_at - Date.now() / 1000);
  if (seconds <= 0) return "授权期限已过";
  const minutes = Math.ceil(seconds / 60);
  return minutes >= 60 ? `约 ${Math.floor(minutes / 60)} 小时后到期`
    : `约 ${minutes} 分钟后到期`;
}

export function DelegationCard({delegation, events, loaded, busy, requirementText,
  onCreate, onStop, onCheckNow, onRefresh}: {
  delegation: DelegationView | null;
  events: DelegationEvent[];
  loaded: boolean; busy: boolean;
  requirementText?: string;
  onCreate: () => void; onStop: () => void; onCheckNow: () => void;
  onRefresh: () => void;
}) {
  const active = delegation && (delegation.derived_status || delegation.status) === "active";
  return <section className="closure-delegation" aria-label="持续委托">
    <header className="closure-delegation-header">
      <h4>持续委托：改动后继续检查这条要求的依据</h4>
      <button disabled={busy} onClick={onRefresh}>重新读取委托状态</button>
    </header>
    <p className="field-note">授权只在期限内、额度内，对同一验收要求做复验；委托有效不等于验收通过。创建或停止授权始终由你在这里决定，编程入口不能代劳。</p>
    {!loaded ? <p className="field-note">尚未读取委托状态。</p>
      : !delegation ? <div className="closure-delegation-empty">
        <p className="field-note">该任务还没有持续委托。委托后，源码变化会自动安排复验并更新回执；没有变化就不会重复实验。</p>
        <button className="primary" disabled={busy} onClick={onCreate}>
          委托持续检查（1 小时 · 最多 3 次复验）</button>
      </div>
      : <>
        <div className={`closure-delegation-state tone-${delegationTone(delegation.derived_status || delegation.status)}`}>
          <strong>{delegationStatusLabel(delegation)}</strong>
          {(delegation.derived_status || delegation.status) === "active"
            ? <span>{expiryLabel(delegation)} · {remainingQuotaLabel(delegation)}</span>
            : <span className="field-note">停止/到期时间 {new Date((delegation.stopped_at || delegation.expires_at) * 1000).toLocaleString()}
              · 已用额度 {delegation.used_reverify} / {delegation.max_reverify} 次（历史信息）</span>}
        </div>
        <dl className="closure-delegation-facts">
          <dt>受托目标</dt><dd>
            {requirementText ? <span>{requirementText}（</span> : null}
            验收要求版本 <code>{delegation.requirement_version}</code>
            {requirementText ? <span>）</span> : null}</dd>
          <dt>允许动作</dt><dd>{(delegation.allowed_actions || ["reverify"]).join("、") === "reverify" ? "仅复验（不修改代码、不扩大范围）" : (delegation.allowed_actions || []).join("、")}</dd>
          <dt>最近检查</dt><dd>{delegation.last_check?.at
            ? `${new Date(delegation.last_check.at * 1000).toLocaleString()} · ${{no_change: "无变化", dispatched: "已投递复验"}[delegation.last_check.outcome || ""] || delegation.last_check.outcome || "已记录"}`
            : "还没有检查记录"}</dd>
        <dt>最近回执</dt><dd>{delegation.last_receipt?.receipt_status
            ? `${delegation.last_receipt.receipt_status} · 作业 ${delegation.last_receipt.job_id}`
            : delegation.last_job_id ? `复验进行中 · 作业 ${delegation.last_job_id}` : "还没有回执"}</dd>
        </dl>
        {delegation.last_abort?.receipt_suppressed && <p className="field-note" role="status">
          最近一次复验在{delegation.last_abort.stage === "before_reverify" ? "启动前" : "执行中"}中止，未写入正常成功回执。
        </p>}
        {delegation.stop_reason && delegation.derived_status !== "active"
          && <p className="field-note">停止原因：{{user_stop: "你停止了委托",
            expired: "授权到期", attempt_exhausted: "复验额度用完",
            requirement_changed: "验收要求变化，需要重新确认委托"}[delegation.stop_reason] || delegation.stop_reason}</p>}
        {events.length > 0 && <details className="closure-delegation-events">
          <summary>最近委托事件（{events.length}）</summary>
          <ul className="mv-list">{events.slice(-8).reverse().map(event =>
            <li key={event.seq}>{new Date(event.at * 1000).toLocaleString()} · {delegationEventLabel(event.kind)}</li>)}</ul>
        </details>}
        <div className="closure-delegation-actions">
          <button className="primary" disabled={busy || !active} onClick={onCheckNow}>
            立即检查一次</button>
          <button disabled={busy || !active} onClick={onStop}>停止委托</button>
        </div>
      </>}
  </section>;
}
