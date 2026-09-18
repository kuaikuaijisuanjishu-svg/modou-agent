// 质疑—复验闭环的纯逻辑层：闭合质疑原因、状态与结局的中文映射，
// 以及计划摘要推导。与服务端 REVERIFICATION_* 常量同口径，独立于
// React/Monaco，让「评论如何长成一次复验」可以被单测钉死。

export type ChallengeReason =
  | "test_did_not_execute_code" | "collection_crash_suspected"
  | "flaky_result_suspected" | "source_changed" | "test_scope_incomplete"
  | "new_test_available" | "other_needs_explanation";

export type ReverificationStatus = "planned" | "approved" | "replaying"
  | "settled" | "rejected" | "failed";

export type ReverificationOutcome =
  "claim.confirmed" | "claim.revised" | "claim.withheld";

export type ReverificationPlan = {
  replay_experiments: Array<{
    experiment_id: string;
    intervention?: Record<string, unknown> | null;
  }>;
  declared_tests?: string[];
  comparison?: string;
};

export type ReverificationRecord = {
  reverification_id: string;
  review_id: string;
  comment_id: string;
  claim_id: string;
  reason: string;
  explanation?: string;
  status: ReverificationStatus | string;
  // review-reverification-v2 策略字段；v1 记录读取时由后端补默认值。
  strategy?: string;
  answered?: boolean;
  open?: string[];
  experiments_added?: number;
  evidence_delta?: Record<string, unknown> | null;
  needs_human?: boolean;
  // 策略 4（new_test_available）随质疑带来的补测补丁与哈希。
  test_proposal?: {patch: string; patch_sha256: string} | null;
  // 计划阶段的原实验清单与结算后的最后一次复验观测。
  original_observations?: Array<Record<string, unknown>>;
  replay_observations?: Array<Record<string, unknown>>;
  plan?: ReverificationPlan;
  outcome: ReverificationOutcome | string | null;
  outcome_reason?: string;
  claim_revision?: number;
  created_at?: string;
  updated_at?: string;
};

export type ReverificationList = {
  schema_version: string;
  review_id: string;
  reverifications: ReverificationRecord[];
};

// 6.8 的闭合质疑原因：用户只能从有限集合里选，"其他"必须补充人工说明。
export const CHALLENGE_REASONS: ReadonlyArray<{
  value: ChallengeReason; label: string; needsExplanation?: boolean;
}> = [
  {value: "test_did_not_execute_code", label: "测试没有真正执行这段代码"},
  {value: "collection_crash_suspected", label: "失败可能是导入或收集崩溃"},
  {value: "flaky_result_suspected", label: "结果可能不稳定"},
  {value: "source_changed", label: "代码已经变化"},
  {value: "test_scope_incomplete", label: "测试范围不完整"},
  {value: "new_test_available", label: "我有一条新的测试"},
  {value: "other_needs_explanation", label: "其他，需要人工说明",
    needsExplanation: true},
];

export const DEFAULT_CHALLENGE_REASON: ChallengeReason =
  CHALLENGE_REASONS[0].value;

export const REVERIFICATION_STATUS_LABELS:
    Record<ReverificationStatus, string> = {
  planned: "等待确认", approved: "已确认", replaying: "复验中",
  settled: "已结算", rejected: "已否决", failed: "复验失败",
};

export const REVERIFICATION_OUTCOME_LABELS:
    Record<ReverificationOutcome, string> = {
  "claim.confirmed": "结论确认", "claim.revised": "结论修订",
  "claim.withheld": "结论扣留",
};

export function reasonLabel(reason: string): string {
  return CHALLENGE_REASONS.find(item => item.value === reason)?.label
    ?? String(reason);
}

export function statusLabel(status: string): string {
  return REVERIFICATION_STATUS_LABELS[status as ReverificationStatus]
    ?? String(status);
}

export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return "";
  return REVERIFICATION_OUTCOME_LABELS[outcome as ReverificationOutcome]
    ?? String(outcome);
}

/** 质疑是否被差异化实验回答；字段缺失（v1 记录）如实标「未回答」。 */
export function answeredLabel(record: ReverificationRecord): string {
  return record.answered ? "已回答" : "未回答";
}

/** open 条目的诚实展示：转人工如实说明；not_implemented 只会来自历史记录。 */
export function openItemLabel(item: string): string {
  if (item === "not_implemented") {
    return "历史记录：这条理由当时只复验了原实验；该原因现已有专属差异化实验";
  }
  if (item === "requires_human_judgment") {
    return "这个问题无法用实验回答，已转人工判断";
  }
  if (item.startsWith("test_visa_withheld:")) {
    // 策略 4：签证扣留原样透出，不圆场——用户看到的就是真实状态。
    const status = item.slice("test_visa_withheld:".length) || "UNKNOWN";
    return `补测签证扣留（${status}）：用户提交的测试未被判定有效，不改判原结论`;
  }
  return item;
}

/** 结局色：确认绿、修订黄、扣留灰；与评论/证据色系分层。 */
export function outcomeClass(
    outcome: string | null | undefined): string {
  if (outcome === "claim.confirmed") return "ok";
  if (outcome === "claim.revised") return "warn";
  if (outcome === "claim.withheld") return "hold";
  return "";
}

/** 一条评论可能先后发起多次复验；界面只挂最新一条。 */
export function reverificationForComment(
    records: ReverificationRecord[],
    commentId: string): ReverificationRecord | null {
  const matches = records
    .filter(record => record.comment_id === commentId)
    .sort((a, b) => a.reverification_id < b.reverification_id ? 1
      : a.reverification_id > b.reverification_id ? -1 : 0);
  return matches[0] ?? null;
}

/** 生成计划前的人工说明校验：与后端同口径，"其他"必须说明。 */
export function validateChallengeInput(reason: string,
                                        explanation: string): string {
  const spec = CHALLENGE_REASONS.find(item => item.value === reason);
  if (!spec) return "请从闭合选项中选择质疑原因";
  const normalized = explanation.trim();
  if (normalized.length > 2000) return "说明最多 2000 字";
  if (spec.needsExplanation && normalized.length < 1) {
    return "选择「其他」时必须补充人工说明";
  }
  return "";
}

// ---- v2 差异化证据的可视层 -------------------------------------------
// 后端七条策略把「跑的是哪条策略、凭什么得出这个结局」全部写进了
// strategy / evidence_delta / outcome_reason；此前 UI 一条都没读。
// 这里把机器结论翻译成屏幕上诚实的中文行，不新增任何口径。

/** 七条策略 + 历史回落的名字。空值来自 v1 旧记录，如实说明。 */
export const STRATEGY_LABELS: Record<string, string> = {
  replay_only: "原样重放（仅旧记录）",
  flaky_result_suspected: "多轮重跑对比",
  source_changed: "源码快照核对",
  coverage_check: "覆盖率核对",
  collection_check: "只收集不执行",
  scope_widen: "扩大收集范围重跑",
  human_routing: "转人工判断",
  test_visa: "补测签证验证",
};

export function strategyLabel(strategy: string | null | undefined): string {
  const key = String(strategy || "").trim();
  if (!key) return "策略未记录（旧版复验）";
  return STRATEGY_LABELS[key] ?? key;
}

// 与证书可采性同源的四等人话标签；键是后端 _admissibility_label 的返回值。
const ADMISSIBILITY_LABELS: Record<string, string> = {
  behavioural: "A 级 · 行为证据：失败测试确实执行过被删的行",
  indirect: "B 级 · 间接证据：失败测试没有执行过被删的行",
  collateral: "C 级 · 连带损坏：测试在收集或导入期就失败",
  environmental: "D 级 · 环境变动：与被测行为无关",
};

export type EvidenceDeltaRow = {label: string; value: string};

function listPreview(value: unknown[]): string {
  const names = value.map(item => String(item));
  const head = names.slice(0, 3).join("、");
  if (!head) return "（空）";
  return names.length > 3 ? head + " 等 " + names.length + " 条" : head;
}

/** evidence_delta → 屏幕行。未识别的键如实透出，绝不静默丢弃。 */
export function evidenceDeltaRows(
    record: ReverificationRecord): EvidenceDeltaRow[] {
  const delta = record.evidence_delta;
  if (!delta || typeof delta !== "object" || Array.isArray(delta)) return [];
  const data = delta as Record<string, unknown>;
  const rows: EvidenceDeltaRow[] = [];
  if (Array.isArray(data.hit_tests) && data.hit_tests.length > 0) {
    rows.push({label: "执行过被删行的测试", value: listPreview(data.hit_tests)});
  }
  if (typeof data.admissibility === "string" && data.admissibility) {
    rows.push({label: "证据等级",
      value: ADMISSIBILITY_LABELS[data.admissibility] ?? data.admissibility});
  }
  if (Array.isArray(data.divergent_experiments)
      && data.divergent_experiments.length > 0) {
    rows.push({label: "与原结果不一致的实验",
      value: listPreview(data.divergent_experiments)});
  }
  if (data.replay_runs !== undefined && data.replay_runs !== null) {
    rows.push({label: "重放轮数", value: String(data.replay_runs)});
  }
  if (data.widen_scope && typeof data.widen_scope === "object"
      && !Array.isArray(data.widen_scope)) {
    const widen = data.widen_scope as Record<string, unknown>;
    const count = String(widen.added_count ?? "?");
    rows.push({label: "扩大收集范围",
      value: widen.capped === true
        ? "需新增 " + count + " 条测试，超出预算上限，已拒绝截断执行"
        : "确定性扩跑，新增 " + count + " 条测试"});
  }
  if (typeof data.new_claim === "string" && data.new_claim) {
    rows.push({label: "新增受保护主张", value: data.new_claim});
  }
  if (typeof data.visa_status === "string" && data.visa_status) {
    rows.push({label: "补测签证",
      value: data.visa_status === "VERIFIED_EFFECTIVE"
        ? "签证通过：基线绿、干预后断言级失败、复跑一致"
        : data.visa_status});
  }
  if (data.original_claim_grade_unchanged === true) {
    rows.push({label: "原主张", value: "等级维持不变；新增主张不升级原结论"});
  }
  const named = new Set(["hit_tests", "admissibility",
    "divergent_experiments", "replay_runs", "widen_scope", "new_claim",
    "new_claim_id", "visa_status", "original_claim_grade_unchanged"]);
  for (const [key, value] of Object.entries(data)) {
    if (named.has(key) || value === null || value === undefined) continue;
    const text = typeof value === "string" ? value : JSON.stringify(value);
    if (!text) continue;
    rows.push({label: key,
      value: text.length > 120 ? text.slice(0, 119) + "…" : text});
  }
  return rows;
}

export type OutcomeReasonView = {label: string; technical: boolean};

/** 结算理由：稳定错误码翻成人话；其余是引擎原始说明，原样透出。 */
export function outcomeReasonView(
    reason: string | null | undefined): OutcomeReasonView {
  const text = String(reason || "").trim();
  if (!text) return {label: "", technical: false};
  if (text === "requires_human_judgment") {
    return {label: "这个问题无法用实验回答，已转人工判断", technical: false};
  }
  if (text.startsWith("coverage_unavailable:")) {
    return {label: "覆盖率数据不可用，无法核对："
      + text.slice("coverage_unavailable:".length).trim(), technical: false};
  }
  if (text.startsWith("scope_budget_exceeded:")) {
    return {label: "扩大范围超出预算上限，已拒绝截断执行："
      + text.slice("scope_budget_exceeded:".length).trim(), technical: false};
  }
  if (text.startsWith("test_visa_withheld:")) {
    return {label: openItemLabel(text), technical: false};
  }
  return {label: text, technical: true};
}

/**
 * 结局的一句话：由「哪条策略」+「哪个结局」共同决定。
 * 一套通用话盖住七条策略，等于把差异化实验又抹平——覆盖率核对的
 * 「没有一条测试执行过被删的行」和重放的「向量与原观测不一致」是两件
 * 事，写成一个句子总有一边在说假话。策略未记录的 v1 旧记录回落到
 * 重放时代的说法。
 */
export function outcomeSummary(record: ReverificationRecord): string {
  const strategy = String(record.strategy || "");
  if (record.outcome === "claim.confirmed") {
    if (strategy === "coverage_check") {
      return "失败的测试确实执行过被删的行：失败是这段代码的行为证据，原主张被确认。";
    }
    if (strategy === "collection_check") {
      return "干预下收集仍然干净：失败是行为性的，不是收集崩溃，原主张被确认。";
    }
    if (strategy === "scope_widen") {
      return "原回归在扩大的收集范围里全部复现：原主张不变，扩大范围不改它的等级。";
    }
    if (strategy === "source_changed") {
      return "工作区哈希仍等于受审快照，封存实验精确绑定这份源码，无需复跑。";
    }
    if (strategy === "test_visa") {
      return "用户提交的测试挣到了补测签证：原主张不变，另外追加一条受保护的新主张。";
    }
    return "复验观察到与原实验一致的状态向量，原主张被确认。";
  }
  if (record.outcome === "claim.revised") {
    if (strategy === "coverage_check") {
      return "没有任何失败的测试执行过被删的行：失败只能算间接证据，主张按新 revision 修订，旧结论保留。";
    }
    if (strategy === "collection_check") {
      return "干预下收集即崩溃：失败是连带损坏而非行为，主张按新 revision 修订，旧结论保留。";
    }
    return "复验结果与原观测不一致，主张按新 revision 修订；旧结论保留。";
  }
  if (record.outcome === "claim.withheld") {
    if (strategy === "test_visa") {
      return "用户提交的测试未通过补测签证，原主张没有被推翻：结论扣留并转人工，旧结论保留为历史。";
    }
    if (strategy === "human_routing") {
      return "这个问题无法用实验回答，已转人工判断：结论扣留，旧结论保留为历史。";
    }
    if (strategy === "coverage_check") {
      return "覆盖率不可用，「哪些测试执行过被删的行」核对不了：不确定只能扣留并转人工，旧结论保留为历史。";
    }
    return "复验未能确认原主张，结论被扣留；旧结论保留为历史。";
  }
  return "";
}
/** 观测清单只报数量；完整内容在抽屉里展开原始 JSON。 */
export function observationsCount(
    records: Array<Record<string, unknown>> | null | undefined): number {
  return Array.isArray(records) ? records.length : 0;
}

export type TestProposalView = {sha: string; patch: string};

export function testProposalView(
    record: ReverificationRecord): TestProposalView | null {
  const proposal = record.test_proposal;
  if (!proposal || typeof proposal !== "object" || !proposal.patch) return null;
  return {sha: String(proposal.patch_sha256 || ""), patch: String(proposal.patch)};
}
