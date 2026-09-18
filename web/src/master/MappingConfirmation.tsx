// T06 组件先行：目标歧义确认对话框（T02 needs_confirmation + 候选列表；动作来自
// task-rounds.md 的 confirm_target_mapping）。确认的只是目标身份，不产生成功结论。
import "../master-views.css";

export type MappingBasis = "patch_location" | "function_scope" | "context" | "unknown";
export type MappingCandidate = {
  target_id: string;
  path: string;
  start_line?: number;
  end_line?: number;
  basis: string;
};
export type MappingRequest = {
  mapping_id: string;
  needs_confirmation: true;
  reason: string; // duplicate_code | target_deleted | rename_unclear | cross_function_move | ...
  original: {path: string; start_line: number; end_line: number};
  candidates: MappingCandidate[];
};
export type ConfirmDecision = {mapping_id: string; confirmed: boolean; operator_note: string};

export const MAPPING_REASON_LABELS: Record<string, string> = {
  duplicate_code: "重复代码无法唯一对应",
  target_deleted: "目标可能已被删除",
  rename_unclear: "重命名后指向不明",
  cross_function_move: "跨函数移动",
};
export const CONFIRMATION_BOUNDARY_NOTE =
  "确认的只是目标身份；确认后仍必须真实复验，不会直接生成成功结论。";

export function mappingReasonLabel(reason: string): string {
  return MAPPING_REASON_LABELS[reason] || `需人工确认：${reason}`;
}

export function mappingCandidatesView(request: MappingRequest): {
  rows: Array<{target_id: string; label: string; basis: string}>;
  empty: boolean;
} {
  const rows = (request.candidates || []).map(candidate => ({
    target_id: candidate.target_id,
    label: `${candidate.path}` +
      (candidate.start_line !== undefined ? `:${candidate.start_line}` : "") +
      (candidate.end_line !== undefined && candidate.end_line !== candidate.start_line
        ? `-${candidate.end_line}` : ""),
    basis: candidate.basis,
  }));
  return {rows, empty: rows.length === 0};
}

export function confirmPayload(request: MappingRequest, confirmed: boolean,
  operatorNote: string): ConfirmDecision {
  return {mapping_id: request.mapping_id, confirmed, operator_note: operatorNote.trim()};
}

export function MappingConfirmation({request, onDecide, busy}: {
  request: MappingRequest;
  onDecide: (decision: ConfirmDecision) => void;
  busy?: boolean;
}) {
  const view = mappingCandidatesView(request);
  return <section className="mv-section mv-dialog" aria-label="目标歧义确认">
    <h4>目标映射需要人工确认</h4>
    <p>{mappingReasonLabel(request.reason)}</p>
    <p className="mv-note">原位置：{request.original.path}:{request.original.start_line}-
      {request.original.end_line}</p>
    {view.empty
      ? <p className="mv-invalid" role="alert">没有可确认的候选匹配；请回到代码工作台核对。</p>
      : <ul className="mv-list" aria-label="候选匹配列表">
        {view.rows.map(row => <li key={row.target_id}>
          <label>
            <input type="radio" name={`mapping-${request.mapping_id}`} value={row.target_id} />
            {row.label}（定位依据：{row.basis}）
          </label>
        </li>)}
      </ul>}
    <p className="mv-note" role="note">{CONFIRMATION_BOUNDARY_NOTE}</p>
    <div className="mv-actions">
      <button type="button" disabled={busy || view.empty}
        onClick={() => onDecide(confirmPayload(request, true, ""))}>确认目标身份</button>
      <button type="button" disabled={busy}
        onClick={() => onDecide(confirmPayload(request, false, ""))}>拒绝本次映射</button>
    </div>
  </section>;
}
