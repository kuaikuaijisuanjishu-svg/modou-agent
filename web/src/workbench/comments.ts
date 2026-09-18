// 人工评论的纯逻辑层：类型、标签映射、线程分组、锚定判定与提交前校验。
// 与 model.ts 一样独立于 React/Monaco，让「评论如何绑定代码与机器主张」
// 可以被单测钉死，而不是散落在组件里。

export type CommentKind =
  | "note" | "question" | "challenge" | "evidence_request"
  | "change_request";

export type CommentStatus = "open" | "resolved" | "withdrawn" | "outdated";

export type CommentAnchor = {
  source_snapshot_sha256: string;
  target_commit: string;
  path: string;
  side: string;
  start_line: number;
  end_line: number;
  blob_sha256: string;
  context_sha256: string;
};

export type CommentRecord = {
  comment_id: string;
  review_id: string;
  author: string;
  body: string;
  kind: CommentKind;
  claim_id?: string | null;
  anchor: CommentAnchor;
  evidence_ids?: string[];
  parent_comment_id?: string | null;
  supersedes_comment_id?: string | null;
  superseded_by?: string | null;
  status: CommentStatus;
  created_at: string;
  updated_at: string;
  revision: number;
};

export type CommentList = {
  schema_version: string;
  review_id: string;
  source_snapshot_sha256?: string;
  counts: {total: number; open: number; resolved: number;
    withdrawn: number; outdated: number};
  comments: CommentRecord[];
};

export type CommentThread = {root: CommentRecord; replies: CommentRecord[]};

export type NewCommentInput = {
  author: string;
  body: string;
  kind: CommentKind;
  path: string;
  start_line: number;
  end_line: number;
  claim_id?: string | null;
  evidence_ids?: string[];
};

export const COMMENT_KIND_LABELS: Record<CommentKind, string> = {
  note: "说明", question: "疑问", challenge: "质疑结论",
  evidence_request: "请求证据", change_request: "提议修改",
};

export const COMMENT_STATUS_LABELS: Record<CommentStatus, string> = {
  open: "待处理", resolved: "已处理", withdrawn: "已撤回",
  outdated: "已过期",
};

// 工具条四入口（6.6.3）：question 是合法评论类型，但不占首层工具条。
// 一个 key 只有一个标签：工具条按钮、编辑器标题与评论徽章都读
// COMMENT_KIND_LABELS，不再维护第二份动作措辞。
export const COMMENT_TOOLBAR_KINDS: readonly CommentKind[] = [
  "note", "challenge", "evidence_request", "change_request",
];

export function kindLabel(kind: string): string {
  return COMMENT_KIND_LABELS[kind as CommentKind] ?? String(kind);
}

export function statusLabel(status: string): string {
  return COMMENT_STATUS_LABELS[status as CommentStatus] ?? String(status);
}

export function anchorCoversLine(anchor: Partial<CommentAnchor> | undefined,
                                 path: string, line: number): boolean {
  if (!anchor || anchor.path !== path) return false;
  const start = Number(anchor.start_line);
  const end = Number(anchor.end_line);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return false;
  return start <= line && line <= end;
}

export function commentsAtLine(records: CommentRecord[], path: string,
                               line: number): CommentRecord[] {
  return records.filter(record =>
    anchorCoversLine(record.anchor, path, line));
}

/**
 * 行标记：覆盖该行的评论里只要还有 open/resolved → 实心蓝点；
 * 只剩 withdrawn/outdated → 空心点；没有评论 → null。
 * 实心/空心承载「这里是否还有活着的讨论」，与机器结论的盾牌分层。
 */
export function markForLine(records: CommentRecord[], path: string,
                            line: number): "comment" | "hollow" | null {
  const covering = commentsAtLine(records, path, line);
  if (covering.length === 0) return null;
  const alive = covering.some(record =>
    record.status === "open" || record.status === "resolved");
  return alive ? "comment" : "hollow";
}

/** 根评论按锚点（起始行、结束行、id）排序；回复按 id 挂到自己的根下。 */
export function groupThreads(records: CommentRecord[]): CommentThread[] {
  const byId = new Map(records.map(record => [record.comment_id, record]));
  const replies = new Map<string, CommentRecord[]>();
  const roots: CommentRecord[] = [];
  for (const record of records) {
    const parent = record.parent_comment_id;
    if (parent && byId.has(parent)) {
      const bucket = replies.get(parent) ?? [];
      bucket.push(record);
      replies.set(parent, bucket);
    } else {
      roots.push(record);
    }
  }
  const anchorKey = (record: CommentRecord): string => {
    const anchor = record.anchor || {start_line: 0, end_line: 0};
    return [String(anchor.start_line ?? 0).padStart(6, "0"),
      String(anchor.end_line ?? 0).padStart(6, "0"),
      record.comment_id].join(":");
  };
  roots.sort((a, b) => anchorKey(a) < anchorKey(b) ? -1
    : anchorKey(a) > anchorKey(b) ? 1 : 0);
  for (const bucket of replies.values()) {
    bucket.sort((a, b) => a.comment_id < b.comment_id ? -1
      : a.comment_id > b.comment_id ? 1 : 0);
  }
  return roots.map(root =>
    ({root, replies: replies.get(root.comment_id) ?? []}));
}

/** 锚点预览：pkg/core.py:9 或 pkg/core.py:9-12。 */
export function anchorPreview(
    anchor: Partial<CommentAnchor> | undefined): string {
  if (!anchor) return "";
  const path = String(anchor.path || "");
  const start = Number(anchor.start_line);
  const end = Number(anchor.end_line);
  if (!path || !Number.isFinite(start) || !Number.isFinite(end)) return path;
  return start === end ? `${path}:${start}` : `${path}:${start}-${end}`;
}

export type CommentInputError = {author?: string; body?: string};

/** 与服务端同口径的提交前校验：署名 1-100 字，正文 1-2000 纯文本。 */
export function validateCommentText(author: string,
                                    body: string): CommentInputError {
  const errors: CommentInputError = {};
  const trimmedAuthor = author.trim();
  if (trimmedAuthor.length < 1) {
    errors.author = "署名不能为空";
  } else if (trimmedAuthor.length > 100) {
    errors.author = "署名最多 100 字";
  }
  const normalized = body.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (normalized.length < 1) {
    errors.body = "评论内容不能为空";
  } else if (normalized.length > 2000) {
    errors.body = "评论内容最多 2000 字";
  } else if (Array.from(normalized).some(ch =>
    ch.charCodeAt(0) < 0x20 && ch !== "\n" && ch !== "\t")) {
    errors.body = "评论内容包含控制字符";
  }
  return errors;
}
