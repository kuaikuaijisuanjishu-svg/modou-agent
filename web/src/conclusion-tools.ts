// 功能对齐（任务单第 4 项）的三块只读逻辑：逐行结论的筛选、证据护照的
// Markdown 导出、以及 j/k 的步进规则。三块都只搬运界面上已经存在的字段，
// 不新造事实；筛选尤其不能把结论悄悄藏起来——被筛掉多少行必须明写出来。

import {admissibilityView} from "./presentation";

/** 逐行结论里筛选用得到的字段。真正的 DiffLine 在 main.tsx，这边只认这些。 */
export type ConclusionRow = {file: string; line: number; label: string};

/** 证书的落点：文件 + 行区间。与 main.tsx 里 findCertificate 同一条规则。 */
export type CertificateAnchor = {
  可采性?: string | null;
  位置?: {file?: string; start?: number; end?: number};
};

export type GradeFilter = "" | "A" | "B" | "C" | "D";

export type ConclusionFilter = {
  label: string;      // "" = 三态全部
  file: string;       // "" = 文件全部
  grade: GradeFilter; // "" = 等级全部
};

export const EMPTY_FILTER: ConclusionFilter = {label: "", file: "", grade: ""};

/** 三态取值与主界面同词：承重 / 无据 / 游离 / 未标注。 */
export const LABEL_FILTER_OPTIONS = ["", "承重", "无据", "游离", "未标注"] as const;

/** 等级选项复用 presentation 的四级标签，不另起一套措辞。 */
export const GRADE_FILTER_OPTIONS: Array<{value: GradeFilter; label: string}> = [
  {value: "", label: "全部等级"},
  ...(["A", "B", "C", "D"] as const).map(grade => ({
    value: grade as GradeFilter,
    label: admissibilityView(grade)?.label || grade,
  })),
];

export function isFiltering(filter: ConclusionFilter): boolean {
  return Boolean(filter.label || filter.file || filter.grade);
}

export function certificateForPoint<T extends CertificateAnchor>(
    certificates: T[] | undefined | null,
    point: {path: string; line: number}): T | undefined {
  return (certificates || []).find(cert => {
    const at = cert.位置 || {};
    return at.file === point.path
      && (!point.line
        || (Number(at.start || 0) <= point.line && point.line <= Number(at.end || 0)));
  });
}

/**
 * 这一行的可采性等级；没有证书或证书没定级时返回 ""，界面如实显示「未定级」，
 * 绝不因为「拿不到」就把它算进某个等级里。
 */
export function gradeForRow(
    certificates: CertificateAnchor[] | undefined | null,
    row: ConclusionRow): GradeFilter {
  const grade = String(certificateForPoint(certificates,
    {path: row.file, line: row.line})?.可采性 || "").toUpperCase();
  return (["A", "B", "C", "D"].includes(grade) ? grade : "") as GradeFilter;
}

export function fileOptions(rows: ConclusionRow[]): string[] {
  return Array.from(new Set(rows.map(row => row.file))).sort();
}

export function matchesFilter(
    certificates: CertificateAnchor[] | undefined | null,
    row: ConclusionRow, filter: ConclusionFilter): boolean {
  if (filter.label && row.label !== filter.label) return false;
  if (filter.file && row.file !== filter.file) return false;
  if (filter.grade && gradeForRow(certificates, row) !== filter.grade) return false;
  return true;
}

export function filterConclusions<T extends ConclusionRow>(
    rows: T[], filter: ConclusionFilter,
    certificates?: CertificateAnchor[] | null): T[] {
  if (!isFiltering(filter)) return rows;
  return rows.filter(row => matchesFilter(certificates, row, filter));
}

/** 筛选栏必须同时报「看见几行」和「一共几行」——只报一个数就等于把结论藏了。 */
export function filterCountLabel(total: number, shown: number): string {
  if (shown === total) return `${total} 行`;
  return `显示 ${shown} / 共 ${total} 行（筛选只影响显示，不改任何结论）`;
}

/** 证据护照的 Markdown 导出：字段与护照卡片逐项对应，不新增解释性文案。 */
export function passportMarkdown(view: {
  statusLabel: string; addedLines: number; loadLines: number; namedFailures: number;
  unevidencedLines: number; driftLines: number; restoreLabel: string;
  planFingerprint: string; bundleAvailable: boolean; live: boolean;
  modelLabel: string; modelCalls: number; recommendationLabel: string;
}): string {
  const rows: Array<[string, string]> = [
    ["状态", view.statusLabel],
    ["新增代码行数", `${view.addedLines} 行`],
    ["承重行数", `${view.loadLines} 行`],
    ["具名失败测试数", `${view.namedFailures} 个`],
    ["无据行数", `${view.unevidencedLines} 行`],
    ["游离行数", `${view.driftLines} 行`],
    ["恢复状态", view.restoreLabel],
    ["计划指纹", view.planFingerprint],
  ];
  if (view.live) {
    rows.push(["模型参与", `是 · ${view.modelLabel}`],
      ["模型调用", `${view.modelCalls} 次`],
      ["建议阶段", view.recommendationLabel]);
  } else {
    rows.push(["模型参与", "本次未调用模型 · 确定性调度"]);
  }
  rows.push(["完整 ReviewBundle", view.bundleAvailable ? "已装载，可下载留存" : "本次未装载"]);
  return ["# 水木验码 · 证据护照", "",
    ...rows.map(([key, value]) => `- ${key}：${value}`), ""].join("\n");
}

/** j/k 的步进：到头就停，不循环——循环会让人以为跳到了别处。 */
export function stepIndex(current: number, delta: number, total: number): number {
  if (total <= 0) return -1;
  if (current < 0) return delta > 0 ? 0 : total - 1;
  return Math.min(total - 1, Math.max(0, current + delta));
}

