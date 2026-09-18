// 证据工作台的纯逻辑层：装饰映射、文件树构建与初始定位优先级。
// 放在独立模块里是为了让"哪种结论用哪种视觉符号"可以被单测钉死——
// 颜色只承载一半语义，另一半必须在图标形状和文字名称里。

export type SourceTreeEntry = {
  path: string;
  name: string;
  kind: "file" | "directory";
  language: string;
  changed: boolean;
  evidence_count: number;
  comment_count: number;
};

export type SourceTree = {
  schema_version: string;
  review_id: string;
  source_snapshot_sha256: string;
  truncated: boolean;
  counts: Record<string, number>;
  entries: SourceTreeEntry[];
};

export type SourceFile = {
  schema_version: string;
  review_id: string;
  path: string;
  content: string;
  language: string;
  encoding: string;
  line_count: number;
  blob_sha256: string;
  source_snapshot_sha256: string;
  etag: string;
  read_only_reason: string;
};

export type EvidenceLine = {
  file: string;
  line: number;
  text?: string;
  label?: string;
  reason?: string | null;
  unit_id?: string | null;
  /** 所属证据单元的最强可采性等级（A/B/C/D）；老报告与未定级时缺省。 */
  admissibility?: string | null;
  evidence_ids?: string[];
};

export type Verdict = "load" | "unevidenced" | "drift" | "failure" | "ai"
  | "comment" | "hollow" | "unlabeled";

export type VerdictSpec = {
  /** gutter 图标：形状语义，不只靠颜色。 */
  glyph: string;
  className: string;
  /** 屏幕阅读器与图例共用的文字名称；三态与主界面共用一套词。 */
  name: string;
  description: string;
  /** 初始定位优先级，越小越优先。 */
  priority: number;
};

export const VERDICT_SPECS: Record<Verdict, VerdictSpec> = {
  load: {
    glyph: "⛨", className: "evg-load", name: "承重",
    description: "删掉这行，至少一条具名测试立刻失败；实验后代码已恢复。",
    priority: 0,
  },
  failure: {
    glyph: "✖", className: "evg-failure", name: "观察到失败",
    description: "本次实验在这行附近观察到测试失败，细节见证据记录。",
    priority: 1,
  },
  unevidenced: {
    glyph: "◆", className: "evg-unevidenced", name: "无据",
    description: "当前声明的测试范围内，没有测试真正执行到这行。",
    priority: 2,
  },
  drift: {
    glyph: "⊘", className: "evg-drift", name: "游离",
    description: "整个文件不被任何声明测试收集或引用。",
    priority: 3,
  },
  ai: {
    glyph: "★", className: "evg-ai", name: "AI 建议未验证",
    description: "模型建议，尚未经过实验验证，不能当作证据。",
    priority: 4,
  },
  comment: {
    glyph: "◉", className: "evg-comment", name: "人工评论",
    description: "有人在这行留下了评论；评论不改变机器结论。",
    priority: 5,
  },
  hollow: {
    glyph: "○", className: "evg-hollow", name: "证据不足或已过期",
    description: "证据不足、已过期或无法核验，这里不下结论。",
    priority: 6,
  },
  unlabeled: {
    glyph: "◌", className: "evg-unlabeled", name: "未标注",
    description: "本轮未能形成结论，原因见逐行说明。",
    priority: 7,
  },
};

/** render_model 的中文结论 → 工作台装饰语义。 */
export function verdictForLine(line: EvidenceLine): Verdict {
  if (line.label === "承重") return "load";
  if (line.label === "无据") return "unevidenced";
  if (line.label === "游离") return "drift";
  if (line.label === "惰性" || line.reason === "inert_withheld") return "unlabeled";
  // 未标注行按原因分泳道，让"未标注"不再是一个不透光的盒子：
  // · 没探过（结构性原因）→ ai：模型建议，尚未经过实验验证；
  // · 探了、观察到失败但无法归因 → failure；
  // · 探了、证据本身不可信 → hollow；
  // · 无行为可验证或纪律扣留 → 维持 unlabeled。
  const reason = line.reason;
  if (reason === "not_measured" || reason === "unsupported_file"
    || reason === "budget_exhausted" || reason === "no_valid_transform") return "ai";
  if (reason === "not_isolated" || reason === "collateral_breakage") return "failure";
  if (reason === "flaky_or_dirty_restore" || reason === "probe_timeout"
    || reason === "environment_shift") return "hollow";
  return "unlabeled";
}

/** 未标注原因的唯一权威表：与后端 modou/models.py 的 Unlabeled 枚举一一对应；
 * 报告按原因单独计数，界面负责把同一原因用人话讲出来。
 * noun 是短名词（徽章、汇总句用），why 是一句话解释（逐行说明用）；
 * 主界面与工作台都从这里取词，不允许再抄第二份。 */
export const UNLABELED_REASONS: Record<string, {noun: string; why: string}> = {
  non_executable: {noun: "空行、注释等非执行行", why: "空行或注释，无行为可验证"},
  not_measured: {noun: "未被覆盖率测量", why: "文件未被覆盖率测量到（多半没人引用）"},
  budget_exhausted: {noun: "因预算耗尽未完成判定", why: "预算用尽，本轮未探测"},
  no_valid_transform: {noun: "无法形成合法反事实变换",
    why: "无法形成语法合法的删除实验"},
  not_isolated: {noun: "未能隔离归因", why: "观察到变化，但无法单独归因到这一行"},
  flaky_or_dirty_restore: {noun: "因不稳定或恢复异常未发布结论",
    why: "回滚后向量对不回基线，证据不可信"},
  unsupported_file: {noun: "属于不支持探测的文件", why: "测试/配置/文档等文件不做探测"},
  probe_timeout: {noun: "因探测超时未完成", why: "单次探测超时，证据不完整"},
  inert_withheld: {noun: "判为惰性的结论（不出证据）", why: "探测判为惰性，因此不出证据"},
  collateral_breakage: {noun: "因测试收集或导入失败未执行",
    why: "测试在收集期就失败，不是行为约束"},
  environment_shift: {noun: "因跳过条件改变未执行", why: "跳过条件变化，与被测行为无关"},
};

export function unlabeledReasonLabel(reason?: string | null): string | null {
  if (!reason) return null;
  return UNLABELED_REASONS[reason]?.why ?? reason;
}

export function verdictSpec(line: EvidenceLine): VerdictSpec {
  return VERDICT_SPECS[verdictForLine(line)];
}

export type TreeNode = {
  path: string;
  name: string;
  kind: "file" | "directory";
  language: string;
  changed: boolean;
  evidenceCount: number;
  commentCount: number;
  children: TreeNode[];
};

/** 把后端的平面条目装配成嵌套树；目录按名排序，文件按优先级再按名排序。 */
export function buildTree(entries: SourceTreeEntry[]): TreeNode[] {
  const root: TreeNode = {path: "", name: "", kind: "directory", language: "",
    changed: false, evidenceCount: 0, commentCount: 0, children: []};
  const directories = new Map<string, TreeNode>([["", root]]);
  const ensureDirectory = (path: string): TreeNode => {
    if (!directories.has(path)) {
      const node: TreeNode = {path, name: path.split("/").pop() || path,
        kind: "directory", language: "", changed: false, evidenceCount: 0,
        commentCount: 0, children: []};
      directories.set(path, node);
      // 新目录要挂回自己的父目录，否则只存在于 Map 里，树上是断的。
      const parentPath = path.includes("/")
        ? path.slice(0, path.lastIndexOf("/")) : "";
      ensureDirectory(parentPath).children.push(node);
    }
    return directories.get(path)!;
  };
  for (const entry of [...entries].sort((a, b) => a.path.localeCompare(b.path))) {
    if (entry.kind === "directory") {
      const node = ensureDirectory(entry.path);
      node.changed = entry.changed;
      node.evidenceCount = entry.evidence_count;
      node.commentCount = entry.comment_count;
      continue;
    }
    const parentPath = entry.path.includes("/")
      ? entry.path.slice(0, entry.path.lastIndexOf("/")) : "";
    const parent = ensureDirectory(parentPath);
    parent.children.push({
      path: entry.path, name: entry.name, kind: "file",
      language: entry.language, changed: entry.changed,
      evidenceCount: entry.evidence_count, commentCount: entry.comment_count,
      children: [],
    });
  }
  for (const node of directories.values()) {
    if (node.kind !== "directory") continue;
    // 目录携带聚合信号：只要里面有修改/证据/评论，目录就带上标记，
    // 首屏展开时能顺着标记找到本次补丁。
    for (const child of node.children) {
      node.changed = node.changed || child.changed;
      node.evidenceCount += child.evidenceCount;
      node.commentCount += child.commentCount;
    }
  }
  const sortNodes = (nodes: TreeNode[]): TreeNode[] => {
    nodes.sort((a, b) => {
      const rank = (node: TreeNode) => node.changed ? 0
        : node.evidenceCount > 0 ? 1 : node.commentCount > 0 ? 2 : 3;
      const byRank = rank(a) - rank(b);
      if (byRank !== 0) return byRank;
      const byKind = a.kind === b.kind ? 0 : a.kind === "directory" ? -1 : 1;
      return byKind !== 0 ? byKind : a.name.localeCompare(b.name);
    });
    for (const node of nodes) sortNodes(node.children);
    return nodes;
  };
  return sortNodes(root.children);
}

/** 首次展开：只展开通向"有修改或有证据"文件的目录，其余折叠。 */
export function defaultExpandedPaths(nodes: TreeNode[]): Set<string> {
  const expanded = new Set<string>();
  const walk = (node: TreeNode, ancestors: string[]): boolean => {
    let relevant = node.changed || node.evidenceCount > 0 || node.commentCount > 0;
    for (const child of node.children) {
      if (walk(child, node.path ? [...ancestors, node.path] : ancestors)) {
        relevant = true;
      }
    }
    if (relevant && node.path) expanded.add(node.path);
    return relevant;
  };
  for (const node of nodes) walk(node, []);
  return expanded;
}

export type FocusTarget = {path: string; line: number; nonce: number};

/** 打开工作台时先看哪一行：按结论优先级，再按文件优先级与行号。 */
export function initialFocus(nodes: TreeNode[],
                             lines: EvidenceLine[]): FocusTarget | null {
  const fileRank = new Map<string, number>();
  const walk = (node: TreeNode) => {
    if (node.kind === "file") {
      fileRank.set(node.path, node.changed ? 0 : node.evidenceCount > 0 ? 1 : 2);
    }
    node.children.forEach(walk);
  };
  nodes.forEach(walk);
  let best: {target: FocusTarget; order: [number, number, number]} | null = null;
  for (const line of lines) {
    const spec = verdictSpec(line);
    const order: [number, number, number] = [
      spec.priority, fileRank.get(line.file) ?? 3, line.line];
    if (!best || order.join(",") < best.order.join(",")) {
      best = {target: {path: line.file, line: line.line, nonce: 0}, order};
    }
  }
  return best?.target ?? null;
}

/** 账本记录 → 具名测试清单：工作台里点击测试名跳到测试文件。 */
export function testsForEvidence(
  ledger: Array<Record<string, unknown>> | undefined,
  evidenceIds: string[] | undefined,
): string[] {
  const wanted = new Set(evidenceIds || []);
  const out: string[] = [];
  for (const record of ledger || []) {
    const id = String(record.record_id || "");
    if (!wanted.has(id)) continue;
    const payload = record.payload as Record<string, unknown> | undefined;
    const data = payload?.data as Record<string, unknown> | undefined;
    for (const regression of (data?.regressions || []) as Array<Record<string, unknown>>) {
      const testId = String(regression.test_id || "");
      if (testId && !out.includes(testId)) out.push(testId);
    }
  }
  return out;
}
