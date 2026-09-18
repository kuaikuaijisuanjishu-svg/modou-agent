// T06 组件先行：候选状态徽标（形状来自 docs/interfaces/candidates.md）。
// 五个维度分开表示：执行结果、签证资格、采用记录、复验、风险接受；
// 任何一枚徽标只描述自己的维度，不冒充另一个维度的结论。
import "../master-views.css";

export type VerificationStatus = "not_run" | "running" | "passed" | "failed" | "env_unavailable";
export type VisaStatus = "eligible" | "withheld_small_case" | "ineligible" | "not_applicable";
export type AdoptionStatus = "not_prepared" | "prepared" | "applied" | "recovered" | "rejected";
export type ReverificationStatus = "not_run" | "running" | "passed" | "failed";

export type CandidateChipInput = {
  candidate_id: string;
  verification?: {status: VerificationStatus};
  visa?: {status: VisaStatus; reason?: string};
  adoption?: {status: AdoptionStatus};
  reverification?: {status: ReverificationStatus};
  riskAcceptance?: {accepted: boolean; reason?: string};
  qualificationInsufficient?: boolean;
};

export type ChipTone = "info" | "ok" | "warn" | "bad";
export type CandidateChip = {key: string; label: string; tone: ChipTone; dimension: string};

export const VERIFICATION_LABELS: Record<VerificationStatus, string> = {
  not_run: "测试未执行", running: "测试执行中", passed: "测试执行通过",
  failed: "测试执行失败", env_unavailable: "环境不可用",
};
export const VISA_LABELS: Record<VisaStatus, string> = {
  eligible: "有效签证", withheld_small_case: "资格不足（小案例）",
  ineligible: "不具备签证资格", not_applicable: "签证不适用",
};
export const ADOPTION_LABELS: Record<AdoptionStatus, string> = {
  not_prepared: "未准备采用", prepared: "等待确认采用", applied: "已采用",
  recovered: "已恢复", rejected: "已拒绝",
};
export const REVERIFICATION_LABELS: Record<ReverificationStatus, string> = {
  not_run: "复验未执行", running: "复验中", passed: "复验通过", failed: "复验失败",
};

export function verificationLabel(status: string): string {
  return VERIFICATION_LABELS[status as VerificationStatus] || `未识别执行状态：${status}`;
}
export function visaLabel(status: string): string {
  return VISA_LABELS[status as VisaStatus] || `未识别签证状态：${status}`;
}
export function adoptionLabel(status: string): string {
  return ADOPTION_LABELS[status as AdoptionStatus] || `未识别采用状态：${status}`;
}
export function reverificationLabel(status: string): string {
  return REVERIFICATION_LABELS[status as ReverificationStatus] || `未识别复验状态：${status}`;
}

// 徽标推导：采用、复验、签证、风险接受各占一枚；资格不足单独成标，永不并入其他维度。
export function candidateChips(candidate: CandidateChipInput): CandidateChip[] {
  const chips: CandidateChip[] = [];
  if (candidate.adoption?.status === "applied") {
    chips.push({key: "adopted", label: ADOPTION_LABELS.applied, tone: "info", dimension: "adoption"});
  } else if (candidate.adoption?.status === "recovered") {
    chips.push({key: "recovered", label: ADOPTION_LABELS.recovered, tone: "warn", dimension: "adoption"});
  }
  const reverify = candidate.reverification?.status;
  if (reverify === "running") {
    chips.push({key: "reverify_running", label: REVERIFICATION_LABELS.running, tone: "info", dimension: "reverification"});
  } else if (reverify === "failed") {
    chips.push({key: "reverify_failed", label: REVERIFICATION_LABELS.failed, tone: "bad", dimension: "reverification"});
  } else if (reverify === "passed") {
    chips.push({key: "reverify_passed", label: REVERIFICATION_LABELS.passed, tone: "ok", dimension: "reverification"});
  }
  if (candidate.visa?.status === "eligible") {
    chips.push({key: "visa_eligible", label: VISA_LABELS.eligible, tone: "ok", dimension: "visa"});
  } else if (candidate.visa?.status === "withheld_small_case") {
    chips.push({key: "visa_withheld", label: VISA_LABELS.withheld_small_case, tone: "warn", dimension: "visa"});
  } else if (candidate.visa?.status === "ineligible") {
    chips.push({key: "visa_ineligible", label: VISA_LABELS.ineligible, tone: "bad", dimension: "visa"});
  }
  if (candidate.riskAcceptance?.accepted) {
    chips.push({key: "risk_accepted", label: "人工接受风险", tone: "warn", dimension: "risk_acceptance"});
  }
  if (candidate.qualificationInsufficient) {
    // 小案例人工采用时必须可见的独立标记；自动采用仍然拒绝。
    chips.push({key: "qualification_insufficient", label: "资格不足·人工接受", tone: "warn", dimension: "qualification"});
  }
  if (candidate.verification?.status === "failed") {
    chips.push({key: "verification_failed", label: VERIFICATION_LABELS.failed, tone: "bad", dimension: "verification"});
  } else if (candidate.verification?.status === "env_unavailable") {
    chips.push({key: "env_unavailable", label: VERIFICATION_LABELS.env_unavailable, tone: "warn", dimension: "verification"});
  }
  return chips;
}

export function CandidateStatusChips({candidate}: {candidate: CandidateChipInput}) {
  const chips = candidateChips(candidate);
  return <div className="mv-chips" aria-label={`候选 ${candidate.candidate_id} 状态`}>
    {chips.length === 0 && <span className="mv-chip mv-chip-info">尚无状态徽标（未执行、未采用）</span>}
    {chips.map(chip => <span key={chip.key}
      className={`mv-chip mv-chip-${chip.tone}`}
      data-dimension={chip.dimension}>{chip.label}</span>)}
  </div>;
}
