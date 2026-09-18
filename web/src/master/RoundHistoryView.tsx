// T06 组件先行：任务轮次历史（纯展示，props 形状来自 docs/interfaces/task-rounds.md）。
// 不接真实后台；接线时由宿主页面把 GET /api/v2/tasks/{id}/rounds 的响应传进来。
import "../master-views.css";

export type RoundStatus = "active" | "superseded";
export type SupersedeReason =
  | "adoption_applied" | "requirement_changed" | "reverify_new_snapshot" | "resumed_legacy";
export type RoundOutcome =
  | "pending" | "pending_recheck" | "supported" | "gap_remains" | "inconclusive" | "accepted_risk";

export type RoundCriterionSummary = {criterion_id: string; outcome: RoundOutcome};
export type TaskRound = {
  round_id: string;
  round_no: number;
  status: RoundStatus;
  superseded_by?: string;
  supersede_reason?: SupersedeReason;
  requirement_version: string;
  source_snapshot?: {head: string; dirty: boolean; snapshot_sha256: string};
  criteria: RoundCriterionSummary[];
  created_at: number;
  closed_at?: number;
};

export const SUPERSEDE_REASON_LABELS: Record<SupersedeReason, string> = {
  adoption_applied: "采用成功，进入新轮次",
  requirement_changed: "要求修改，创建新版本",
  reverify_new_snapshot: "当前版本重新核验且快照已变",
  resumed_legacy: "旧任务首次继续，升级为轮次1",
};
const OUTCOME_LABELS: Record<RoundOutcome, string> = {
  pending: "待检查",
  pending_recheck: "待复核（新轮次保守标记）",
  supported: "具备测试依据",
  gap_remains: "仍有证据缺口",
  inconclusive: "暂时无法判定",
  accepted_risk: "人工接受风险",
};

export function supersedeReasonLabel(reason?: string): string {
  if (!reason) return "未记录取代原因";
  return SUPERSEDE_REASON_LABELS[reason as SupersedeReason] || `未识别原因：${reason}`;
}

export function outcomeLabel(outcome: string): string {
  return OUTCOME_LABELS[outcome as RoundOutcome] || `未识别结论：${outcome}`;
}

// 冻结聚合规则：未检查项（pending/pending_recheck）不能因其他项通过而完成。
export function roundPendingRechecks(round: TaskRound): RoundCriterionSummary[] {
  return round.criteria.filter(item => item.outcome === "pending" || item.outcome === "pending_recheck");
}

export function roundConclusion(round: TaskRound): {
  level: "complete" | "inconclusive" | "empty"; counts: Record<string, number>; label: string;
} {
  const counts: Record<string, number> = {};
  for (const item of round.criteria) counts[item.outcome] = (counts[item.outcome] || 0) + 1;
  const undecided = roundPendingRechecks(round).length > 0 || (counts.inconclusive || 0) > 0;
  if (round.criteria.length === 0) {
    return {level: "empty", counts, label: "本轮没有验收项"};
  }
  return undecided
    ? {level: "inconclusive", counts,
       label: `本轮任务级结论：暂时无法判定（${roundPendingRechecks(round).length} 项待定）`}
    : {level: "complete", counts, label: "本轮全部验收项已有结论"};
}

export type RoundsView = {
  active: TaskRound | null;
  history: TaskRound[];          // superseded rounds, newest first
  invalidReason: string | null;  // 数据不符合契约时如实提示，不猜测
};

export function roundsView(rounds: TaskRound[], activeRoundId?: string): RoundsView {
  const activeRounds = rounds.filter(item => item.status === "active");
  let active: TaskRound | null = null;
  let invalidReason: string | null = null;
  if (activeRounds.length > 1) {
    invalidReason = "存在多个 active 轮次，数据不符合轮次契约";
  } else if (activeRounds.length === 1) {
    active = activeRounds[0];
    if (activeRoundId && active.round_id !== activeRoundId) {
      invalidReason = `active_round_id(${activeRoundId}) 与轮次列表不一致`;
    }
  } else if (rounds.length > 0) {
    invalidReason = "没有 active 轮次";
  }
  const history = rounds
    .filter(item => item.status === "superseded")
    .sort((left, right) => right.round_no - left.round_no);
  return {active, history, invalidReason};
}

export function RoundHistoryView({rounds, activeRoundId}: {
  rounds: TaskRound[]; activeRoundId?: string;
}) {
  const view = roundsView(rounds, activeRoundId);
  // P0-02：时间线一句话说明轮次链（原版本→采用后→再次改动），旧轮次的
  // "supported"结论显式标为历史依据，不再与当前状态并排呈现无解释的绿灯。
  // 第 N+1 轮的过渡标签 = 第 N 轮被取代的原因。
  const transitionOf = (reason?: SupersedeReason): string => ({
    adoption_applied: "采用后",
    reverify_new_snapshot: "再次改动后",
    requirement_changed: "要求更新后",
    resumed_legacy: "继续",
  }[reason || "resumed_legacy"] || "之后");
  const chain = view.history.slice().sort((left, right) => left.round_no - right.round_no);
  // 节点：第一轮=原版本；其后每个节点来自上一轮被取代的原因；当前轮标"当前"。
  const nodes: Array<{no: number; label: string; key: string}> = [];
  if (chain.length) {
    nodes.push({no: chain[0].round_no, label: "原版本", key: chain[0].round_id});
    for (const round of chain)
      nodes.push({no: round.round_no + 1, label: transitionOf(round.supersede_reason),
        key: `${round.round_id}-next`});
    if (view.active)
      nodes[nodes.length - 1] = {no: view.active.round_no,
        label: `${transitionOf(chain[chain.length - 1].supersede_reason)} · 当前`,
        key: view.active.round_id};
  } else if (view.active)
    nodes.push({no: view.active.round_no, label: "当前", key: view.active.round_id});
  return <section className="mv-section" aria-label="任务轮次">
    {view.invalidReason && <p className="mv-invalid" role="alert">{view.invalidReason}</p>}
    {nodes.length > 1 && <p className="mv-timeline">
      版本链：{nodes.map((node, index) => <span key={node.key}>
        {index > 0 && " → "}第 {node.no} 轮（{node.label}）</span>)}
    </p>}
    {view.active && <div className="mv-active-round">
      <h4>当前轮次 · 第 {view.active.round_no} 轮（{view.active.requirement_version}）</h4>
      {view.active.source_snapshot && <p className="mv-note">
        源码快照 {view.active.source_snapshot.head.slice(0, 8)}
        {view.active.source_snapshot.dirty ? "（含未提交改动）" : ""}
      </p>}
      <p>{roundConclusion(view.active).label}</p>
      {roundPendingRechecks(view.active).length > 0 && <ul className="mv-list" aria-label="待复验条件">
        {roundPendingRechecks(view.active).map(item =>
          <li key={item.criterion_id}>{item.criterion_id}：{outcomeLabel(item.outcome)}</li>)}
      </ul>}
    </div>}
    {view.history.length > 0 && <div className="mv-history">
      <h4>历史轮次（只读）</h4>
      <ul className="mv-list">
        {view.history.map(round => <li key={round.round_id}>
          第 {round.round_no} 轮 · {round.requirement_version} ·
          被 {supersedeReasonLabel(round.supersede_reason)} 取代
          {round.criteria.some(item => item.outcome === "supported")
            && <span className="mv-historical">历史依据（不代表当前版本）</span>}
          {round.closed_at ? ` · 关闭于 ${new Date(round.closed_at).toLocaleString()}` : ""}
        </li>)}
      </ul>
      <p className="mv-note">旧轮次保留只读记录，其结论不并入当前轮次；标注"历史依据"的通过结论只属于当时的版本。</p>
    </div>}
    {!view.active && !view.invalidReason && <p className="mv-note">该任务还没有轮次数据。</p>}
  </section>;
}
