// 工作台底部行动时间线：四泳道把一次审查里"谁提议、谁观测、谁裁决、
// 谁质疑"摆成同一张图。纯函数映射：输入全是工作台已经装载的数据
// （账本、逐行结论、复验记录），不新起请求，不重算结论——每一条都是
// 已有记录的投影，缺数据就缺条目，绝不编造。
import type {EvidenceLine} from "./model";
import {unlabeledReasonLabel, verdictForLine, verdictSpec} from "./model";
import {admissibilityView} from "../presentation";
import {
  answeredLabel, outcomeLabel, reasonLabel, statusLabel,
  type ReverificationRecord,
} from "./reverification";

export type TimelineTone = "neutral" | "success" | "warning" | "danger";

export type TimelineItem = {
  id: string;
  title: string;
  detail: string;
  tone: TimelineTone;
  /** 可跳转时给出目标行；没有就渲染成纯文本条目。 */
  ref?: {path: string; line: number};
};

export type TimelineLanes = {
  /** 智能体提议：账本 Claim——每一条都点名自己的观测支撑。 */
  proposals: TimelineItem[];
  /** 实验观测：账本 Experiment——真实发生了什么、还原干不干净。 */
  observations: TimelineItem[];
  /** 规则裁决：逐行结论 + 质疑后的机器复验结局。 */
  verdicts: TimelineItem[];
  /** 人工：具名质疑（复验的入口永远是人的质疑）。 */
  human: TimelineItem[];
};

/** 泳道的展示顺序与说明；UI 按这个数组渲染。 */
export const TIMELINE_LANES: Array<{
  key: keyof TimelineLanes; name: string; hint: string;
}> = [
  {key: "proposals", name: "智能体提议", hint: "账本里的主张，每条都点名观测支撑"},
  {key: "observations", name: "实验观测", hint: "真实跑出来的结果与还原状态"},
  {key: "verdicts", name: "规则裁决", hint: "逐行结论与质疑后的复验结局"},
  {key: "human", name: "人工", hint: "具名质疑：复验的入口"},
];

// 账本行的 record_type / payload 是后端原样投影，这里只做防御式读取：
// 形状不对的行跳过，缺字段的条目降级为"未记录"，不抛错不打断整条泳道。
type LedgerRow = Record<string, unknown>;

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

function obj(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === "object" && !Array.isArray(v)
    ? v as Record<string, unknown> : null;
}

function anchorOf(payload: Record<string, unknown>):
    {path: string; line: number} | null {
  const a = obj(payload["anchor"]);
  const path = a ? str(a["path"]) : "";
  if (!path) return null;
  return {path, line: Math.floor(num(a ? a["line_start"] : 0))};
}

function locationText(anchor: {path: string; line: number} | null): string {
  if (!anchor) return "（位置未记录）";
  return anchor.line > 0 ? anchor.path + ":" + anchor.line : anchor.path;
}

const CLAIM_KIND_LABELS: Record<string, string> = {
  RequiredByTest: "测试要求",
  ContributesToFix: "修复贡献",
  ProtectsRegression: "回归防护",
  StaticDependency: "静态依赖",
  StructurallyReferenced: "结构引用",
};

const EXPERIMENT_STATUS: Record<string,
    {label: string; tone: TimelineTone}> = {
  COMPLETE: {label: "完成", tone: "success"},
  TIMEOUT: {label: "超时", tone: "warning"},
  INVALID: {label: "干预不合法", tone: "danger"},
  DIRTY: {label: "还原不干净", tone: "danger"},
  ABORTED: {label: "中途放弃", tone: "danger"},
  JUNIT_UNUSABLE: {label: "结果不可解析", tone: "danger"},
  OBSERVATION_FAILED: {label: "观测失败", tone: "danger"},
};

function claimItems(rows: LedgerRow[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const row of rows) {
    if (row["record_type"] !== "Claim") continue;
    const payload = obj(row["payload"]);
    if (!payload) continue;
    const kind = str(payload["kind"]);
    const anchor = anchorOf(payload);
    const provenance = Array.isArray(payload["provenance"])
      ? payload["provenance"] as unknown[] : [];
    const label = CLAIM_KIND_LABELS[kind] ?? (kind || "未知主张");
    items.push({
      id: "claim:" + (str(row["record_id"]) || items.length),
      title: label + " · " + locationText(anchor),
      detail: "凭 " + provenance.length + " 条观测支撑；没有观测就没有主张",
      tone: "neutral",
      ref: anchor && anchor.line > 0 ? anchor : undefined,
    });
  }
  return items;
}

function experimentItems(rows: LedgerRow[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const row of rows) {
    if (row["record_type"] !== "Experiment") continue;
    const payload = obj(row["payload"]);
    if (!payload) continue;
    const iv = obj(payload["intervention"]);
    const path = iv ? str(iv["path"]) : "";
    const lines = iv && Array.isArray(iv["lines"])
      ? (iv["lines"] as unknown[]).map(l => num(l)).filter(l => l > 0) : [];
    const where = path
      ? (lines.length ? path + ":" + lines[0] : path) : "（位置未记录）";
    const status = str(payload["status"]);
    const known = EXPERIMENT_STATUS[status];
    const clean = payload["restored_clean"];
    const secs = num(payload["cost_s"]);
    const describe = iv ? str(iv["describe"]) : "";
    const parts = [
      describe || "干预",
      known ? known.label : (status || "状态未记录"),
      typeof clean === "boolean" ? (clean ? "还原干净" : "还原不干净") : "",
      secs > 0 ? secs.toFixed(1) + "s" : "",
    ].filter(Boolean);
    items.push({
      id: "exp:" + (str(row["record_id"]) || items.length),
      title: where,
      detail: parts.join(" · "),
      tone: known ? known.tone : "neutral",
      ref: path ? {path, line: lines.length ? lines[0] : 0} : undefined,
    });
  }
  return items;
}

function lineTone(line: EvidenceLine): TimelineTone {
  switch (verdictForLine(line)) {
    case "load": return "success";
    case "failure": case "hollow": return "danger";
    case "unevidenced": case "ai": return "warning";
    default: return "neutral";
  }
}

function verdictItems(lines: EvidenceLine[],
                      records: ReverificationRecord[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const line of lines) {
    const spec = verdictSpec(line);
    const reason = unlabeledReasonLabel(line.reason);
    const grade = admissibilityView(line.admissibility);
    const parts = [reason ?? "",
      grade ? "可采性 " + grade.label : ""].filter(Boolean);
    items.push({
      id: "line:" + line.file + ":" + line.line + ":"
        + (line.evidence_ids?.[0] ?? ""),
      title: spec.name + " · " + line.file + ":" + line.line,
      detail: parts.join(" · ") || spec.description,
      tone: lineTone(line),
      ref: {path: line.file, line: line.line},
    });
  }
  // 质疑后的复验结局也是规则裁决：复验之后重新下的结论。
  for (const rec of records) {
    if (!rec.outcome) continue;
    items.push({
      id: "rv:" + rec.reverification_id,
      title: "复验" + outcomeLabel(rec.outcome)
        + " · 结论第 " + (rec.claim_revision ?? 1) + " 版",
      detail: "质疑：" + reasonLabel(rec.reason) + " · " + answeredLabel(rec),
      tone: rec.outcome === "claim.confirmed" ? "success"
        : rec.outcome === "claim.revised" ? "warning" : "danger",
    });
  }
  return items;
}

function humanItems(records: ReverificationRecord[]): TimelineItem[] {
  return records.map(rec => ({
    id: "hr:" + rec.reverification_id,
    title: "质疑：" + reasonLabel(rec.reason),
    detail: [rec.explanation ?? "", statusLabel(rec.status)]
      .filter(Boolean).join(" · "),
    tone: rec.status === "settled" ? "neutral" : "warning",
  }));
}

export function buildTimeline(input: {
  ledger?: Array<Record<string, unknown>> | null;
  lines?: EvidenceLine[] | null;
  reverifications?: ReverificationRecord[] | null;
}): TimelineLanes {
  const rows = input.ledger ?? [];
  const records = input.reverifications ?? [];
  return {
    proposals: claimItems(rows),
    observations: experimentItems(rows),
    verdicts: verdictItems(input.lines ?? [], records),
    human: humanItems(records),
  };
}
