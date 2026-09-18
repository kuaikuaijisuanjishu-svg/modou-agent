import {UNLABELED_REASONS} from "./workbench/model";

export const SESSION_TOKEN_KEY = "modou.session.token";
export const SESSION_REVIEW_KEY = "modou.session.review_id";

export const COMMON_UNCOVERED = [
  "结论只在已声明的测试范围（FAIL_TO_PASS ∪ PASS_TO_PASS）内成立",
  "不等于语义等价：未被这批测试覆盖的路径、外部副作用、跨进程行为均未验证",
];

export type StatusView = {
  label: string;
  step: number;
  total: number;
  tone: "idle" | "active" | "success" | "warning" | "danger";
};

const STATUS: Record<string, StatusView> = {
  CREATED: { label: "准备审查", step: 1, total: 8, tone: "active" },
  INTAKE_VALIDATED: { label: "输入已核验", step: 1, total: 8, tone: "active" },
  PLAN_DRAFTED: { label: "计划已生成", step: 2, total: 8, tone: "active" },
  AWAITING_APPROVAL: { label: "等待确认计划", step: 3, total: 8, tone: "warning" },
  PLAN_FROZEN: { label: "计划已确认", step: 4, total: 8, tone: "active" },
  BASELINE_RUNNING: { label: "验证测试基线", step: 5, total: 8, tone: "active" },
  EXECUTING: { label: "执行证据实验", step: 6, total: 8, tone: "active" },
  VERIFYING_RESTORE: { label: "检查完整恢复", step: 7, total: 8, tone: "active" },
  SYNTHESIZING: { label: "汇总结论", step: 8, total: 8, tone: "active" },
  CANCELLING: { label: "正在安全中止", step: 8, total: 8, tone: "warning" },
  RECOVERING: { label: "正在恢复检查", step: 7, total: 8, tone: "active" },
  CLEANUP_REQUIRED: { label: "需要清理", step: 8, total: 8, tone: "danger" },
  QUARANTINED: { label: "已隔离", step: 8, total: 8, tone: "danger" },
  DRAFTING: { label: "正在起草计划", step: 1, total: 8, tone: "active" },
  REPLANNING: { label: "正在调整计划", step: 6, total: 8, tone: "warning" },
  AWAITING_HUMAN: { label: "等待人工决策", step: 6, total: 8, tone: "warning" },
  PROPOSING_REPAIR: { label: "正在提出修复", step: 8, total: 8, tone: "active" },
  VERIFYING_REPAIR: { label: "正在验证修复", step: 8, total: 8, tone: "active" },
  DELIVERING_BRANCH: { label: "正在交付修复分支", step: 8, total: 8, tone: "active" },
  COMPLETE: { label: "审查完成", step: 8, total: 8, tone: "success" },
  COMPLETED: { label: "审查完成", step: 8, total: 8, tone: "success" },
  PARTIAL: { label: "部分完成", step: 8, total: 8, tone: "warning" },
  FAILED: { label: "审查失败", step: 8, total: 8, tone: "danger" },
  ABORTED: { label: "审查已中止", step: 8, total: 8, tone: "danger" },
  CANCELLED: { label: "审查已取消", step: 8, total: 8, tone: "danger" },
};

export function statusView(status?: string | null): StatusView {
  if (!status) return { label: "尚未开始", step: 0, total: 8, tone: "idle" };
  return STATUS[status] || { label: status, step: 0, total: 8, tone: "warning" };
}

const EVENT_LABELS: Record<string, string> = {
  "review.created": "审查已创建",
  "intake.validated": "输入范围已核验",
  "review_memory.loaded": "仓库记忆已载入",
  "repo.inspected": "仓库信息已读取",
  "patch.inspected": "补丁新增内容已解析",
  "universe.frozen": "检查对象已固定",
  "plan.drafted": "审查计划已生成",
  "plan.awaiting_approval": "等待人工确认计划",
  "plan.approved": "计划已获批准",
  "baseline.started": "基线验证开始",
  "baseline.completed": "基线验证通过",
  "test_scope.collected": "测试范围已收集",
  "ddmin.routed": "已判定哪些对象可做最小化",
  "minimization.skipped": "最小化不适用，已说明原因",
  "minimization.completed": "最小化已给出证书",
  "probe.started": "证据实验开始",
  "probe.completed": "证据实验完成",
  "restore.started": "恢复检查开始",
  "restore.completed": "工作区恢复完成",
  "restore.verified": "工作区恢复已验证",
  "claim.derived": "证据主张已签发",
  "observation.recorded": "中间观测已记录",
  "model.action": "观测后动作已决定",
  "scheduler.action": "调度决策已记录",
  "model.fallback": "调度切换为确定性模式",
  "model.request.started": "模型调用开始",
  "model.request.completed": "模型调用完成",
  "recommendation.generated": "只读建议已生成",
  "recommendation.failed": "建议生成失败，不影响实验结论",
  "scheduler.deterministic_order": "确定性初始排序已应用",
  "scheduler.next": "继续下一个检查对象",
  "run.synthesizing": "正在汇总结论",
  "run.completed": "证据产物已发布",
  "narrator.compiled": "结论说明已编译",
  "executor.bound": "执行器已绑定",
  "review.completed": "审查完成",
  "review.failed": "审查失败",
  "review.cancel_requested": "已请求安全中止",
  "review.cancellation_observed": "执行器已观察到中止请求",
  "review.cancelled": "审查已安全中止",
  "review.resumed": "已从重启中止记录恢复",
  "review.resume_created": "已创建恢复审查",
  "review.reverification.requested": "复验已发起",
  "review.reverification.approved": "复验已确认，开始重放",
  "repair.delivered": "修复已验证并交付",
  "repair.candidate_proposed": "修复候选已提议",
  "test.proposed": "已提议一条测试",
  "test.run_failed": "候选测试隔离执行未跑绿",
  "test.revised": "候选测试已按失败摘录修订",
  "test.visa": "签证判定已作出",
  "test.not_effective": "跑绿但未判定有效",
  "test.passed": "候选测试通过并归档",
  "test.needs_human": "测试提议需要人工决定",
  "autonomy.write_routed": "自主写入已路由到仪器",
  "autonomy.write_skipped": "自主写入跳过：无合格发现",
  "autonomy.write_unrouted": "自主写入停止：发现路由不了",
  "approval.standing_authorization_used": "常驻授权自动批准",
  "approval.standing_authorization_missed": "常驻授权未覆盖，转人工批准",
  "human.disposition.recorded": "人工处置已留痕",
  "plan.revised": "计划已按你的修订重出一稿",
  "plan.superseded": "上一稿计划已作废",
  "agent.action.accepted": "智能体建议的动作已接受执行",
  "agent.action.deduplicated": "重复的智能体动作已去重跳过",
  "agent.observation.recorded": "智能体观测已记录",
  "approval.reexecuted_after_plan_drift": "计划漂移后原批准作废，新计划需重新批准",
  "claim.confirmed": "复验确认了这条主张",
  "claim.revised": "复验修订了这条主张",
  "claim.withheld": "复验无法回答，这条主张被扣留",
  "claim.protected.added": "用户测试通过签证，新增一条受保护主张",
  "decision.requested": "审查暂停，等待人工决策",
  "decision.answered": "人工决策已作答",
  "decision.defaulted": "人工决策超时，按默认停止处理",
  "edit_session.opened": "编辑会话已开启",
  "edit_session.transitioned": "编辑会话状态已推进",
  "experiment.replayed": "复验重放了一轮实验",
  "executor.process_recovered": "执行进程异常退出后已恢复",
  "human.claim.challenged": "用户对主张发起质疑",
  "human.comment.created": "用户评论已记录",
  "human.comment.outdated": "用户评论已标记为过时",
  "human.comment.superseded": "用户评论已被新评论取代",
  "narrator.fallback": "结论说明改用确定性模板生成",
  "policy.forced_stop": "预算用尽，策略强制停止实验",
  "policy.rejected": "边界策略拒绝了这次动作",
  "repair.candidate.recorded": "修复候选已留痕（未交付）",
  "repair.delivery_approved": "修复交付已获五指纹批准",
  "repair.delivery_exported": "修复交付内容清单已导出",
  "repository.lease_acquired": "仓库执行租约已获取",
  "repository.lease_released": "仓库执行租约已释放",
  "repository_security.accepted": "仓库安全边界检查已通过",
  "review.cleanup_requested": "已请求清理工作区",
  "review.cleanup_deferred": "工作区清理已延后",
  "review.cleanup_completed": "工作区清理已完成",
  "review.recovered": "审查从进程中断中恢复",
  "review.state_changed": "审查状态已变更",
  "review.state_transition_intent": "已记录一次状态迁移意图",
  "review_spec.compiled": "审查规格已编译锁定",
  "standard_mode.model_path_blocked": "标准模式按承诺阻断了模型调用",
  "standard_mode.provider_drift": "标准模式检测到模型配置与承诺不符",
  "scope.expansion_cancelled": "计划修订试图扩大审查范围，已中止执行",
};

// ---- 计划修订：多轮气泡 ----------------------------------------------
// 这里有一条必须说清楚的产品语义：修订**不是在原计划上打补丁**。后端
// revise_v2 会把旧稿置为 ABORTED/plan_superseded，另发一个新的 review_id 和
// 新的计划指纹。把它画成"同一份计划被改了"会让人以为指纹还是原来那个——
// 而指纹正是人批准时签的那个东西。
export const PLAN_REVISION_BOUNDARY =
  "每次修订都会作废上一稿、另出一份计划和新的指纹；你批准的永远是当前这一稿。";

export type PlanRevisionView = {
  review_id: string; round: number; reason: string;
  fingerprint: string; status: string; current: boolean;
};

export function planRevisionViews(raw: unknown, currentId?: string): PlanRevisionView[] {
  const rows = (raw && typeof raw === "object"
    ? (raw as Record<string, unknown>).revisions : null);
  if (!Array.isArray(rows)) return [];
  const views: PlanRevisionView[] = [];
  rows.forEach((item, index) => {
    const record = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
    const review_id = String(record.review_id || "").trim();
    if (!review_id) return;
    const fingerprint = String(record.review_spec_sha256 || "");
    views.push({
      review_id, round: index + 1,
      reason: String(record.revision_reason || "").trim(),
      // 指纹只显示前 12 位：够用来对照，又不会把一行撑爆。全站同一个口径。
      fingerprint: shortHash(fingerprint).short,
      status: String(record.status || ""),
      current: Boolean(currentId) && review_id === currentId,
    });
  });
  // 只有一稿时不画气泡：一条"第 1 稿"的时间线什么也没说。
  return views.length > 1 ? views : [];
}

// 人工处置的答案词表，与后端 modou/server/control.py 的 _DISPOSITION_ANSWERS
// 逐字对齐。**不是自由输入**：答案是封闭词表，界面发明不出后端不认的答案。
// 两边分叉的话，人在界面上选完会收到一个 DISPOSITION_ANSWER_INVALID，而界面
// 并不知道自己错在哪——所以这张表有测试钉住。
export const DISPOSITION_ANSWERS: Record<string, {value: string; label: string}[]> = {
  "test.needs_human": [
    {value: "write_manually", label: "我来手写这条测试"},
    {value: "retry_different_approach", label: "换个思路重试"},
    {value: "accept_gap", label: "接受这个缺口，不补测试"},
  ],
  "decision.requested": [
    {value: "continue", label: "继续"},
    {value: "stop", label: "停下"},
  ],
};

/** 这个事件能不能由人在界面上回应。 */
export function dispositionOptions(kind: string): {value: string; label: string}[] {
  return DISPOSITION_ANSWERS[kind] || [];
}

// 处置是审计通道，不是控制通道：留痕不改变审查状态，也不替代批准。这句话必须
// 出现在表单上——否则人会以为点完它就会接着跑。
export const DISPOSITION_BOUNDARY =
  "留痕只记录「谁、在什么时候、怎么答的」，不改变审查状态，也不替代人工批准。";

export function eventLabel(kind: string): string {
  return EVENT_LABELS[kind] || "发现一条尚未翻译的技术记录";
}

export type EventPresentation = {
  title: string;
  explanation: string;
  technicalSummary: string;
  kind: string;
};

/** 白话事件模型：主标题永远可读，原始 kind 只在技术详情出现。 */
export function eventPresentation(event: {
  kind: string; data?: Record<string, unknown> | null;
}): EventPresentation {
  const kind = String(event.kind || "");
  const data = event.data || {};
  const title = EVENT_LABELS[kind] || "发现一条尚未翻译的技术记录";
  const explanations: Record<string, string> = {
    "review.created": "审查记录已经建立，接下来会冻结范围。",
    "review_memory.loaded": "读取这份仓库已确认的规则，只作为本次审查上下文。",
    "plan.drafted": "系统根据目标和范围生成了一份待确认计划。",
    "plan.approved": "你确认了这份计划，实验边界现在固定。",
    "baseline.completed": "原来的测试在实验前正常通过，可以作为比较基线。",
    "probe.completed": "临时拿走一段新增代码并运行声明测试，记录了反应。",
    "observation.recorded": "把这次实验观察到的测试状态写入证据账本。",
    "restore.verified": "代码已经恢复，并再次核对工作区是否干净。",
    "claim.derived": "根据可复验的实验记录签发了一条证据主张。",
    "run.completed": "本次运行的证据产物已经原子发布。",
    "review.failed": "审查在终止前遇到异常，不能把它当作完成。",
    "review.cancelled": "审查按请求安全中止，未完成的结论保持缺省。",
    "review.reverification.requested":
      "你质疑了一条主张，系统已排好要重放的实验，等你确认。",
    "review.reverification.approved":
      "你确认了这次复验，实验重放开始，结果稍后回到时间线。",
    "autonomy.write_routed":
      "覆盖缺口按确定性规则派给「补测试」这台仪器：模型只提议测试，结论由隔离实验签发。",
    "autonomy.write_skipped":
      "没有合格的覆盖缺口可派发，这一步如实记为跳过，不硬造工作。",
    "autonomy.write_unrouted":
      "这条发现不属于任何已知仪器，宁可停下等人，也不把错的仪器对准错的发现。",
    "approval.standing_authorization_used":
      "这次批准来自人事先签发的常驻授权文件，不是模型自我批准。",
    "approval.standing_authorization_missed":
      "计划超出了常驻授权的范围，回到等你人工确认的老路。",
    "test.visa":
      "候选测试在隔离环境里跑出了可复验的签证判定。",
  };
  const summaryEntries = Object.entries(data).slice(0, 4).map(([key, value]) => {
    // 数组不再缩成「N 项」：先给前两项的原文，再看得到总数。
    const text = Array.isArray(value)
      ? (value.length === 0 ? "（空）"
        : value.slice(0, 2).map(item => {
          const itemText = typeof item === "object" && item !== null
            ? JSON.stringify(item) : String(item);
          return itemText.length > 24 ? itemText.slice(0, 23) + "…" : itemText;
        }).join("、") + (value.length > 2 ? ` 等 ${value.length} 项` : ""))
      : String(value);
    return `${key}=${text.length > 72 ? text.slice(0, 69) + "…" : text}`;
  });
  return {
    title,
    explanation: explanations[kind] || "这条记录已写入审计账本；完整含义请展开技术详情。",
    technicalSummary: summaryEntries.join(" · "),
    kind,
  };
}

export type RepairStatusView = {
  label: string;
  detail: string;
  tone: "idle" | "active" | "success" | "warning" | "danger";
};

export function repairStatusView(status?: string | null): RepairStatusView {
  switch (status) {
    case "DELIVERED":
      return {label: "已交付", detail: "补丁已通过声明测试，并写入本地修复分支。", tone: "success"};
    case "INCOMPLETE":
      return {label: "需要清理", detail: "交付工件不完整，禁止继续提交或宣称交付成功。", tone: "danger"};
    case "EXPIRED":
      return {label: "工件已过期", detail: "源仓库快照已变化，需要重新生成审查证据。", tone: "warning"};
    case "BRANCH_CONFLICT":
      return {label: "分支冲突", detail: "目标修复分支已经存在，未覆盖任何已有分支。", tone: "warning"};
    case "CLEANUP_REQUIRED":
      return {label: "需要清理", detail: "隔离工作区仍有残留，需完成清理后才能再次交付。", tone: "danger"};
    case "NOT_DELIVERED":
    default:
      return {label: "尚未交付", detail: "只有明确授权且补丁验证通过后，才会创建本地分支。", tone: "idle"};
  }
}

// ---- #9/#10 编辑会话与交付入账（只读展示） ------------------------------
// M1 七态状态机（modou/server/edit_sessions.py:16-22）与 M2 五指纹交付
// （modou/agent/delivery_ceremony.py）的展示映射。这里只做标签翻译：
// 不产生任何批准、贴补丁或下载动作。

export type EditSessionCandidate = {
  patch_sha256?: string;
  intent?: string;
  verdict?: string;
  test_result?: string;
  at?: string;
  outcome_kind?: string;
  error_code?: string;
  error_detail?: string;
};

export type EditSessionTransition = {
  from?: string;
  to?: string;
  at?: string;
  reason?: string;
};

export type EditSessionRecord = {
  session_id?: string;
  status?: string;
  intent?: string;
  snapshot_sha256?: string;
  candidates?: EditSessionCandidate[];
  transitions?: EditSessionTransition[];
  delivery_approval?: {
    fingerprints?: Record<string, string>;
    branch?: string;
    commit?: string;
    at?: string;
  } | null;
  delivery_export?: {
    package_dir?: string;
    manifest_sha256?: string;
    at?: string;
  } | null;
  created_at?: string;
  updated_at?: string;
};

export type EditSessionLedger = {
  schema_version?: string;
  review_id?: string;
  edit_sessions?: EditSessionRecord[];
};

// POST .../repair/candidate 的入账记录：补丁正文留在服务端
// （repair/candidate.patch），这条记录里只有指纹与规模，没有正文。
export type RepairCandidateRecord = {
  schema_version?: string;
  summary?: string;
  finding_ids?: string[];
  generated_test_ids?: string[];
  patch_sha256?: string;
  patch_bytes?: number;
  status?: string;
  retention_days?: number;
};

export type EditSessionStateView = {
  label: string;
  tone: "idle" | "success" | "warning" | "danger";
};

const EDIT_SESSION_STATE_VIEWS: Record<string, EditSessionStateView> = {
  open: {label: "已开启", tone: "idle"},
  patch_candidate: {label: "已有候选补丁", tone: "idle"},
  verified: {label: "已验证", tone: "success"},
  awaiting_delivery: {label: "待交付", tone: "idle"},
  delivered: {label: "已交付", tone: "success"},
  abandoned: {label: "已作废", tone: "warning"},
  stale: {label: "已失效", tone: "danger"},
};

export function editSessionStateView(status?: string | null): EditSessionStateView {
  const key = String(status ?? "").trim();
  return EDIT_SESSION_STATE_VIEWS[key]
    || {label: key ? `未知状态（${key}）` : "未提供状态", tone: "warning"};
}

export function editSessionTransitionText(from?: string | null,
                                          to?: string | null): string {
  return `${editSessionStateView(from).label} → ${editSessionStateView(to).label}`;
}

export function editSessionExitReasonText(reason?: string | null): string {
  const key = String(reason ?? "").trim();
  const views: Record<string, string> = {
    repair_delivered: "修复已交付",
    orphan_owner_process_gone: "开启会话的进程已中断",
    source_snapshot_changed: "源代码快照在会话开启后发生了变化",
    abandoned_by_user: "用户显式放弃",
  };
  return views[key] || (key ? `原因：${key}` : "");
}

export function candidateVerdictView(verdict?: string | null): string {
  const value = String(verdict ?? "").trim();
  if (!value) return "未提供";
  if (value === "pending") return "待验证";
  if (value === "accepted") return "已接受";
  if (value.startsWith("rejected")) {
    const code = value.includes(":") ? value.slice(value.indexOf(":") + 1) : "";
    const named = deliveryRejectionText(code);
    return named ? `已拒绝（${named}）` : "已拒绝";
  }
  return value;
}

export function candidateTestResultView(result?: string | null): string {
  const value = String(result ?? "").trim();
  if (!value) return "未提供";
  if (value === "not_run") return "未运行";
  return value;
}

// 交付重算不一致的拒绝码（modou/agent/delivery_ceremony.py:48-54 的
// MISMATCH_CODES，加上交付文档列出的越界码）。中文名只做解释，不替代码值。
export const DELIVERY_REJECTION_CODES: ReadonlyArray<string> = [
  "DELIVERY_APPROVAL_STALE",
  "DELIVERY_PATCH_MISMATCH",
  "DELIVERY_TEST_MISMATCH",
  "DELIVERY_EVIDENCE_MISMATCH",
  "DELIVERY_BRANCH_MOVED",
  "DELIVERY_SCOPE_EXPANDED",
];

export function deliveryRejectionText(code?: string | null): string {
  const key = String(code ?? "").trim();
  const views: Record<string, string> = {
    DELIVERY_APPROVAL_STALE: "批准已过期（源快照已变化）",
    DELIVERY_PATCH_MISMATCH: "补丁与批准记录不一致",
    DELIVERY_TEST_MISMATCH: "测试证据与批准记录不一致",
    DELIVERY_EVIDENCE_MISMATCH: "证据清单与批准记录不一致",
    DELIVERY_BRANCH_MOVED: "交付分支已移动",
    DELIVERY_SCOPE_EXPANDED: "补丁越界（触碰未授权路径）",
  };
  return views[key] || "";
}

export const DELIVERY_FINGERPRINT_VIEWS: ReadonlyArray<{key: string; label: string}> = [
  {key: "snapshot_sha256", label: "源快照指纹"},
  {key: "patch_sha256", label: "补丁指纹"},
  {key: "test_sha256", label: "测试证据指纹"},
  {key: "evidence_manifest_sha256", label: "证据清单指纹"},
  {key: "target_ref", label: "交付分支目标"},
];

// 交付物措辞红线（modou/agent/delivery_ceremony.py:40 DISCLAIMER 原句）。
export const DELIVERY_DISCLAIMER = "内容哈希清单，非安全签署";

export function schedulingDetail(kind: string, data: Record<string, unknown>): string {
  if (kind === "scheduler.next") {
    const before = String(data.previous_next_anchor || "");
    const after = String(data.actual_next_anchor || "");
    const reason = String(data.priority_reason || "");
    return before && after && before !== after
      ? `${before} → ${after}${reason ? ` · ${reason}` : ""}`
      : `${after}${reason ? ` · ${reason}` : ""}`;
  }
  if (kind === "policy.rejected") return `已拒绝 · ${String(data.code || "POLICY_REJECTED")}`;
  if (kind === "review_memory.loaded") {
    const count = Number(data.active_records ?? 0);
    return `已载入 ${Number.isFinite(count) ? count : 0} 条已确认规则`;
  }
  if (kind === "model.action" || kind === "scheduler.action") {
    return String(data.reason || data.kind || "");
  }
  return "";
}

// ---- 仓库审查记忆：只读卡的中文呈现 ----------------
// 规则内容与词表由 modou/agent/memory.py 把守；这里只做展示层翻译。
// 记忆是上下文，不是授权：卡片永远与红线说明同屏，不提供写入路径。
const MEMORY_KINDS: Record<string, string> = {
  review_preference: "审查偏好",
  known_baseline: "已知基线",
  test_convention: "测试约定",
  architecture_constraint: "架构约束",
};

export type MemoryRuleView = {
  memory_id: string; rule: string; kind: string; applies_to: string[];
  status: string;
};

export function memoryRuleViews(raw: unknown): MemoryRuleView[] {
  if (!Array.isArray(raw)) return [];
  const views: MemoryRuleView[] = [];
  for (const item of raw) {
    const record = (item && typeof item === "object" ? item : {}) as Record<string, unknown>;
    const memory_id = String(record.memory_id || "").trim();
    const rule = String(record.rule || "").trim();
    if (!memory_id || !rule) continue;
    const kindKey = String(record.kind || "");
    views.push({memory_id, rule,
      kind: MEMORY_KINDS[kindKey] || kindKey || "规则",
      applies_to: Array.isArray(record.applies_to)
        ? record.applies_to.map(path => String(path)).filter(Boolean) : [],
      // intake 注入的 active_rules() 不带 status；写入端点返回的全量记录带。
      status: String(record.status || "active")});
  }
  return views;
}

/** 只读卡标题：同一仓库再次审查时，确认过的记忆规则已经生效。 */
export const MEMORY_CARD_HEADLINE = "上次你确认的规则，这次已生效";

/** 记忆写入表单的种类封闭词表，与 modou/agent/memory.py 的 _KINDS 同源。 */
export function memoryKindOptions(): Array<{value: string; label: string}> {
  return Object.entries(MEMORY_KINDS).map(([value, label]) => ({value, label}));
}

/** 规则状态中文标签；未知状态原样展示，不编造。 */
export const MEMORY_STATUS_LABELS: Record<string, string> = {
  active: "生效中", deprecated: "已弃用", revoked: "已撤销",
};

export function resolveStartupToken(hash: string, stored: string | null): string {
  const fromFragment = new URLSearchParams(hash.replace(/^#/, "")).get("token")?.trim();
  return fromFragment || stored?.trim() || "";
}

export function normalizePastedToken(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  const marker = "#token=";
  const markerAt = value.indexOf(marker);
  if (markerAt >= 0) return decodeURIComponent(value.slice(markerAt + marker.length).split("&")[0]);
  if (value.startsWith("token=")) return decodeURIComponent(value.slice(6).split("&")[0]);
  return value;
}

export type UiNotice = {
  level: "warning" | "danger" | "network";
  title: string;
  message: string;
  code?: string;
  // HTTP 状态只进技术详情：它回答"服务端怎么说的"，不回答"我现在该做什么"。
  status?: number;
  recovery?: "token" | "reload-plan" | "retry";
};

export function classifyError(status: number | undefined, code: string | undefined,
                              message: string): UiNotice {
  if (status === 401 || code === "AUTH_REQUIRED" || code === "TOKEN_MISSING") {
    return { level: "danger", status, title: "需要重新连接本次服务", message,
      code: code || "AUTH_REQUIRED", recovery: "token" };
  }
  // 两个模式契约的失败关闭码：它们是产品边界，不是内部异常，
  // 所以主文案要说清"发生了什么、接下来怎么办"，码留给技术详情。
  if (code === "PRODUCT_MODE_PROVIDER_MISMATCH") {
    return { level: "warning", status, code,
      title: "审查模式与调度方式不一致",
      message: "标准审查必须走确定性调度，智能体审查必须有可用的模型通道。"
        + "请切换到与调度方式相符的模式后重试。" };
  }
  if (code === "STANDARD_MODE_MODEL_FORBIDDEN") {
    return { level: "warning", status, code,
      title: "这个动作需要智能体审查",
      message: "补测提案本身就是一次模型调用，标准审查承诺零模型参与，"
        + "所以入口在服务端就被拒绝了。要用它请切到智能体审查重新发起。" };
  }
  if (code === "PRODUCT_MODE_INVALID") {
    return { level: "danger", status, code,
      title: "审查模式取值不合法",
      message: "服务端只接受标准审查（standard）和智能体审查（agent）两种取值。"
        + "这通常意味着请求不是由当前界面发出的；请回到界面重新发起一次。" };
  }
  // 这里**没有** STANDARD_MODE_MODEL_ACTIVITY / PRODUCT_MODE_MISLABEL：
  // 它们是 bundle_v2.verify() 的问题码，不是 IntakeError，服务端不会以
  // HTTP 错误发出来。给它们写在这里会变成"界面备好了一段服务端永远走不到
  // 的文案"，后来者会照着文案反推出一个不存在的接口行为。证据包校验结果
  // 要上屏时，应当另起一条展示路径，而不是挂到 HTTP 错误分类上。
  if (status === 409 && code === "STALE_PLAN") {
    return { level: "warning", status, title: "计划已经更新", message,
      code, recovery: "reload-plan" };
  }
  if (code === "SOURCE_SNAPSHOT_CHANGED" || code === "REPAIR_EVIDENCE_INVALID") {
    return { level: "warning", status, title: "修复证据已经过期", message, code,
      recovery: "reload-plan" };
  }
  if (code === "REPAIR_BRANCH_EXISTS") {
    return { level: "warning", status, title: "修复分支发生冲突", message, code };
  }
  if (code === "REPAIR_VERIFICATION_FAILED" || code === "REPAIR_PATCH_REJECTED") {
    return { level: "warning", status, title: "补丁验证未通过", message, code };
  }
  if (code === "REPAIR_WORKTREE_DIRTY" || code === "REPAIR_CLEANUP_REQUIRED") {
    return { level: "danger", status, title: "隔离工作区需要清理", message, code };
  }
  if (code === "REPAIR_NOT_AUTHORIZED") {
    return { level: "warning", status, title: "计划没有修复交付权限", message, code };
  }
  if (status === 409) {
    return { level: "warning", status, title: "当前操作暂时不能执行", message, code };
  }
  // 终态快照轮询超时：服务在跑、连接没断，只是完成事件与终态快照之间
  // 的窗口没有闭合。必须赶在 !status 分支之前拦下，否则会被误报成
  // 「本地服务连接中断」，吓用户以为服务挂了。
  if (code === "TERMINAL_SNAPSHOT_TIMEOUT") {
    return { level: "warning", status, title: "结果还在落盘，稍后刷新即可",
      message, code, recovery: "retry" };
  }
  if (!status) {
    return { level: "network", status, title: "本地服务连接中断", message,
      code: code || "NETWORK_ERROR", recovery: "retry" };
  }
  return { level: "danger", status, title: "审查请求未完成", message, code };
}

const NON_EXECUTABLE_TEXT = /^\s*($|#|"""|'''|[)\]},:]+\s*$)/;

// 未标注原因的文字统一来自 workbench/model.ts 的 UNLABELED_REASONS 唯一权威表；
// 界面上不允许出现裸英文标识符——评审看到 collateral_breakage 不会知道
// 那是「测试在收集阶段就崩了」，而不是「这行代码有问题」。
export function reasonLabel(reason?: string | null): string {
  if (reason === "inert_withheld") return "删行后无测试失败，但未过护栏，扣下不呈现";
  return UNLABELED_REASONS[reason || ""]?.noun ?? "";
}

export function linePresentation(
  line: {label: string; reason?: string | null; text?: string},
) {
  // 非执行行（空行、注释、纯括号）本身无所谓执行不执行；它拿到结论，
  // 是因为**继承了所在单元**（label.py 的既定设计）。不标出来的话，
  // 相邻两行同为文档字符串却一个「惰性扣下」一个「未标注」，看着像 bug。
  const inherited = Boolean(line.text) && NON_EXECUTABLE_TEXT.test(line.text || "")
    && line.reason !== "non_executable" && line.reason !== "not_measured";
  if (line.reason === "inert_withheld") {
    return { badge: "惰性扣下", className: "reason-inert-withheld", inherited };
  }
  return { badge: line.label, className: `label-${line.label}`, inherited };
}

export function summarySentence(summary?: Record<string, unknown> | null): string {
  if (!summary) return "";
  const byLabel = (summary.by_label || {}) as Record<string, number>;
  const byReason = (summary.by_reason || {}) as Record<string, number>;
  const total = Number(summary.total_added_lines || 0);
  const load = Number(byLabel["承重"] || 0);
  const unevidenced = Number(byLabel["无据"] || 0);
  const drift = Number(byLabel["游离"] || 0);
  const withheld = Number(byReason.inert_withheld || 0);
  const named = load + unevidenced + drift + withheld;
  const rest = Math.max(0, total - named);
  // inert_withheld 单独以 withheld 计数，不进分项。
  const reasonParts = Object.entries(UNLABELED_REASONS)
    .filter(([reason]) => reason !== "inert_withheld")
    .map(([reason, item]) => ({count: Number(byReason[reason] || 0), text: item.noun}))
    .filter(item => item.count > 0);
  const explained = reasonParts.reduce((sum, item) => sum + item.count, 0);
  const detail = explained === rest && reasonParts.length > 0
    ? `：${reasonParts.map(item => `${item.count} 行${item.text}`).join("，")}`
    : "";
  // 四类之外还剩多少必须说出来，但不能为了数字闭合把所有未标注原因
  // 都冒充成空行/注释；只有 by_reason 与余数一致时才展开真实分项。
  const tail = rest > 0
    ? `；其余 ${rest} 行未形成三态结论${detail}。`
    : "。";
  return `本次审查覆盖 ${total} 行新增代码：${load} 行获得测试承重证据，`
    + `${unevidenced} 行未被测试执行，${drift} 行判为游离，`
    + `${withheld} 行惰性结论被护栏扣下${tail}`;
}

export type VerdictCount = {key: "added" | "load" | "unevidenced" | "drift";
  label: string; value: number};

export function verdictCounts(summary?: Record<string, unknown> | null): VerdictCount[] {
  const byLabel = (summary?.by_label || {}) as Record<string, number>;
  return [
    {key: "added", label: "新增行", value: Number(summary?.total_added_lines || 0)},
    {key: "load", label: "承重", value: Number(byLabel["承重"] || 0)},
    {key: "unevidenced", label: "无据", value: Number(byLabel["无据"] || 0)},
    {key: "drift", label: "游离", value: Number(byLabel["游离"] || 0)},
  ];
}

export type ExperimentStage = {key: "baseline" | "remove" | "regression" | "restore";
  label: string; detail: string;
  state: "complete" | "failed" | "active" | "pending"};

type StoryEvent = {kind: string; data?: Record<string, unknown>};
type StoryCertificate = {unit_id?: string; 状态?: string;
  位置?: {file?: string; start?: number; end?: number}};

export function experimentStory(events: StoryEvent[], certificates: StoryCertificate[] = []): ExperimentStage[] {
  const baselineStarted = events.find(event => event.kind === "baseline.started");
  const baseline = events.find(event => event.kind === "baseline.completed"
    || event.kind === "baseline.failed");
  const probeStarted = events.find(event => event.kind === "probe.started");
  const probeResults = events.filter(event => event.kind === "probe.completed"
    || event.kind === "probe.failed");
  const preferredCertificate = certificates.find(cert => cert.状态 === "承重") || certificates[0];
  const preferredAnchor = String(preferredCertificate?.位置?.file || "");
  const probe = probeResults.find(event => String(event.data?.anchor_id || "") === preferredAnchor)
    || probeResults.find(event => Array.isArray(event.data?.regressed_tests)
      && (event.data?.regressed_tests as unknown[]).length > 0)
    || probeResults[0];
  const chosen = probe || probeStarted;
  const anchor = String(chosen?.data?.anchor_id || "候选代码");
  const observation = events.find(event => event.kind === "observation.recorded"
    && (!chosen || String(event.data?.anchor_id || "") === anchor)
    && Array.isArray(event.data?.regressed_tests)
    && (event.data?.regressed_tests as unknown[]).length > 0);
  const regressions = (chosen?.data?.regressed_tests || observation?.data?.regressed_tests || []) as unknown[];
  const restoreStarted = events.find(event => event.kind === "restore.started");
  const restore = events.find(event => event.kind === "restore.verified"
    && (!chosen || event.data?.anchor_id === chosen.data?.anchor_id));
  const declared = Number(baseline?.data?.declared_tests || 0);
  const baselinePassed = baseline?.data?.all_passed === true;
  const restored = restore?.data?.restored_clean === true;
  const certificate = certificates.find(cert => cert.位置?.file === anchor)
    || (preferredCertificate?.位置?.file === anchor ? preferredCertificate : undefined);
  const certificatePosition = certificate?.位置;
  const lineCount = certificatePosition?.start && certificatePosition.end
    ? Math.max(1, certificatePosition.end - certificatePosition.start + 1) : 0;
  const interventionDetail = certificatePosition?.start
    ? `${anchor}:${certificatePosition.start}${certificatePosition.end && certificatePosition.end !== certificatePosition.start
      ? `–${certificatePosition.end}` : ""} · ${lineCount} 行`
    : chosen ? `${anchor}${chosen.data?.units ? ` · ${String(chosen.data.units)} 个实验单元` : ""}` : "";
  const baselineState = baseline
    ? baselinePassed ? "complete" : "failed"
    : baselineStarted ? "active" : "pending";
  const removeState = probe
    ? probe.kind === "probe.failed" ? "failed" : "complete"
    : probeStarted ? "active" : "pending";
  const regressionState = regressions.length
    ? "failed"
    : probe?.kind === "probe.completed" ? "complete" : "pending";
  const restoreState = restore
    ? restored ? "complete" : "failed"
    : restoreStarted ? "active" : "pending";
  return [
    {key: "baseline", label: "基线全绿",
      detail: baseline ? baselinePassed ? `${declared}/${declared} 项测试通过`
        : `${declared} 项声明测试未全通过` : baselineStarted ? "基线测试进行中" : "等待基线测试",
      state: baselineState as ExperimentStage["state"]},
    {key: "remove", label: "临时拿走",
      detail: chosen ? interventionDetail : probeStarted ? "正在实施可恢复干预" : "等待可恢复实验",
      state: removeState as ExperimentStage["state"]},
    {key: "regression", label: "具名测试报警",
      detail: regressions.length ? String(regressions[0])
        : probe?.kind === "probe.completed" ? "未观察到具名测试失败" : "等待测试观测",
      state: regressionState as ExperimentStage["state"]},
    {key: "restore", label: "恢复干净",
      detail: restore ? (restored ? `${declared}/${declared} 项测试通过，工作区已恢复`
        : "恢复校验失败") : restoreStarted ? "恢复校验进行中" : "等待恢复校验",
      state: restoreState as ExperimentStage["state"]},
  ];
}

export type WorkspaceFocus = "current_result" | "next_run_setup";

export type WorkspacePresentation = {
  showCurrentResult: boolean;
  showModelTrack: boolean;
  showModelBanner: boolean;
  showRecommendations: boolean;
  showDeterministicPlaceholder: boolean;
  showLiveSetupPlaceholder: boolean;
  showPreviousResultNotice: boolean;
  configLocked: boolean;
};

const TERMINAL_REVIEW_STATES = new Set(["COMPLETE", "PARTIAL", "FAILED", "ABORTED"]);

/**
 * 工作台视觉只从这一处决定：左侧 provider 是下一次运行的配置，
 * 只有 current_result 才能读取当前 Review 的模型事实。
 */
export function workspacePresentation(
  provider: string,
  reviewStatus?: string | null,
  focus: WorkspaceFocus = "current_result",
): WorkspacePresentation {
  const hasReview = Boolean(reviewStatus);
  const showCurrentResult = hasReview && focus === "current_result";
  const liveSetup = focus === "next_run_setup" && provider === "live";
  const deterministicSetup = focus === "next_run_setup" && provider !== "live";
  const configLocked = hasReview && !TERMINAL_REVIEW_STATES.has(String(reviewStatus));
  const completed = TERMINAL_REVIEW_STATES.has(String(reviewStatus));
  return {
    showCurrentResult,
    showModelTrack: showCurrentResult && provider === "live",
    // Model facts stay inside the agent lane; standard reviews are
    // conclusion-only and must not leak a model banner from a prior run.
    showModelBanner: false,
    showRecommendations: showCurrentResult && provider === "live" && completed,
    showDeterministicPlaceholder: deterministicSetup,
    showLiveSetupPlaceholder: liveSetup,
    showPreviousResultNotice: Boolean(reviewStatus) && focus === "next_run_setup",
    configLocked,
  };
}

export type EventLane = "模型建议" | "水木验码执行" | "测试执行";

/** 时间线的分权归位。未知事件默认落到执行方，保留 kind 供人工审计。 */
export function laneOf(event: {kind: string; data?: Record<string, unknown>}): EventLane {
  const kind = event.kind;
  const source = String(event.data?.source || "");
  if (kind === "scheduler.action") return "水木验码执行";
  if (kind === "model.action" && source === "deterministic") return "水木验码执行";
  if (kind === "scheduler.deterministic_order") return "水木验码执行";
  if (kind === "scheduler.next" && event.data?.selection !== "model_reprioritized") {
    return "水木验码执行";
  }
  if (kind === "scheduler.next" && event.data?.selection === "model_reprioritized") {
    return "模型建议";
  }
  if (kind.startsWith("model.") || kind.startsWith("recommendation.")
      || kind === "plan.drafted") return "模型建议";
  if (kind.startsWith("test.") || kind.startsWith("pytest.")
      || kind === "observation.recorded") return "测试执行";
  return "水木验码执行";
}

export function laneEvents<T extends {kind: string; data?: Record<string, unknown>}>(
  events: T[],
  live: boolean,
): Array<{lane: EventLane; events: T[]}> {
  const lanes: EventLane[] = live
    ? ["模型建议", "水木验码执行", "测试执行"]
    : ["水木验码执行", "测试执行"];
  return lanes.map(lane => ({lane, events: events.filter(event => {
    const assigned = laneOf(event);
    // 智能体泳道以外的视图不展示模型请求、模型动作或建议事件；
    // 确定性 scheduler/model.action 事件本身仍由 laneOf 归到执行方。
    return assigned === lane;
  })}));
}

export type EventPhase = "准备" | "实验" | "证据" | "恢复";

export function eventPhase(kind: string): EventPhase {
  if (kind.startsWith("restore.") || kind === "review.completed"
      || kind === "review.failed") return "恢复";
  if (kind.startsWith("probe.") || kind.startsWith("tool.")
      || kind.startsWith("baseline.") || kind.startsWith("scheduler.")
      || kind.startsWith("minimization.")
      || kind.startsWith("model.")) return "实验";
  if (kind.startsWith("claim.") || kind.startsWith("observation.")
      || kind.startsWith("run.") || kind.startsWith("narrator.")) return "证据";
  return "准备";
}

export function groupEventPhases<T extends {kind: string}>(events: T[]):
    Array<{phase: EventPhase; events: T[]}> {
  const phases: EventPhase[] = ["准备", "实验", "证据", "恢复"];
  return phases.map(phase => ({phase, events: events.filter(e => eventPhase(e.kind) === phase)}));
}

export function modeLabels(bundle?: Record<string, unknown> | null,
                           offlineReplay = false,
                           liveRequest?: Record<string, unknown> | null) {
  // 运行途中还没有 bundle（它只在 COMPLETE/PARTIAL 后才取回），此时若只看 bundle
  // 就会把一次**正在调用模型**的运行标成「确定性调度」——画面在说谎。
  // 服务端 describe() 会回显 request，运行中用它兜底。
  const request = (bundle?.request || liveRequest || {}) as Record<string, unknown>;
  const report = ((bundle?.evidence_bundle || {}) as Record<string, unknown>).report as
    Record<string, unknown> | undefined;
  const summary = (report?.summary || {}) as Record<string, unknown>;
  const run = offlineReplay || bundle?.execution_mode === "replay" ? "离线回放" : "实时运行";
  const schedulerRaw = String(bundle?.scheduler_mode || "");
  const schedulerMap: Record<string, string> = {
    model: "模型调度", fifo: "FIFO", coverage_first: "覆盖优先", cost_first: "成本优先",
  };
  // 兜底不再断言 FIFO：默认已是覆盖优先，而候选缺摘要时又会如实回落到 FIFO。
  // 模式未标注时说「确定性调度」，不替这次运行声称它用了哪一种。
  const scheduler = schedulerMap[schedulerRaw] || (request.model_provider === "live"
    ? `模型调度 · ${String(request.agent_level || "l1").toUpperCase()}` : "确定性调度");
  const isolationRaw = String(bundle?.isolation_mode
    || request.isolation_mode || request.execution_mode || "trusted_local");
  const isolationMap: Record<string, string> = {
    sandboxed: "沙箱模式", trusted_local: "受信任模式",
    ci_ephemeral: "CI 临时环境", replay: "回放环境",
  };
  const isolation = isolationMap[isolationRaw]
    || String(summary.execution_mode_label || isolationRaw);
  return {run, scheduler, isolation};
}

// 离线回放横幅（B3）：回放不是实时运行，这件事必须醒目到没法忽略。
// 「结果生成于」取账本最后一条事件的时间——那是证据自己的时间，
// 不是你打开文件的时刻；包里没有事件时如实说未随包记录，绝不拿当前时间充数。
export function offlineReplayNotice(
  offlineReplay: boolean,
  events: Array<{occurred_at?: string}> = [],
): {label: string; detail: string} | null {
  if (!offlineReplay) return null;
  const last = events.length ? events[events.length - 1] : undefined;
  const moment = last?.occurred_at !== undefined ? new Date(last.occurred_at) : null;
  const detail = moment !== null && !Number.isNaN(moment.getTime())
    ? `结果生成于 ${moment.toLocaleString("zh-CN")}` : "结果生成时间未随包记录";
  return {label: "离线回放", detail};
}

export type ModelParticipationState =
  "reordered" | "kept_order" | "partial" | "not_participated";

export type RecommendationItemView = {
  anchor_id: string;
  line_refs: string[];
  evidence_ids: string[];
  action: string;
  rationale: string;
  verification: string;
};

export type RecommendationView = {
  generated: boolean;
  items: RecommendationItemView[];
  failureReason: string;
  skippedReason: string;
};

export type ModelParticipation = {
  live: boolean;
  state: ModelParticipationState;
  stateLabel: string;
  modelLabel: string;
  agentLevel: string;
  calls: number;
  requestsByStage: Record<string, number>;
  reorderCount: number;
  orderChanges: string[];
  reasons: string[];
  fallbacks: number;
  policyRejections: number;
  recommendationFailed: boolean;
  recommendations: RecommendationView | null;
};

const PARTICIPATION_LABELS: Record<ModelParticipationState, string> = {
  reordered: "已参与并发生重排",
  kept_order: "已参与，保持原顺序",
  partial: "部分参与（调度发生过降级）",
  not_participated: "未参与（确定性调度）",
};

/** 结果页「模型参与记录」的数据来源：provider 元数据、模型指标和调度事件。

    只展示系统实际采用的动作和审计数据，不展示模型内部思维过程；
    deterministic 运行同样如实展示 0 和 —；这些字段只在智能体审查
    透明面板使用，标准审查不显示模型过程。 */
export function modelParticipation(
  bundle?: {provider?: Record<string, unknown>;
            model_metrics?: Record<string, unknown>;
            request?: Record<string, unknown>;
            narration?: {recommendations?: Record<string, unknown> | null}} | null,
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  liveRequest?: Record<string, unknown> | null,
): ModelParticipation {
  const provider = bundle?.provider || {};
  const metrics = bundle?.model_metrics || {};
  const request = (bundle?.request || liveRequest || {}) as Record<string, unknown>;
  const kind = String(provider.kind || "");
  const live = kind === "openai-compatible" || request.model_provider === "live";
  const reorders = events.filter(event => event.kind === "scheduler.next"
    && event.data?.selection === "model_reprioritized");
  const orderChanges = reorders.map(event =>
    `${String(event.data?.previous_next_anchor || "")} → ${String(event.data?.actual_next_anchor || "")}`)
    .filter(change => change.trim() !== "→");
  const reasons = [...new Set(reorders.map(event =>
    String(event.data?.priority_reason || "").trim()).filter(Boolean))];
  const eventFallbacks = events.filter(event => event.kind === "model.fallback").length;
  const fallbacks = bundle ? Number(metrics.fallbacks || 0) : eventFallbacks;
  const recommendations = recommendationView(bundle);
  const recommendationFailed = (recommendations?.failureReason || "").length > 0
    || events.some(event => event.kind === "recommendation.failed");
  let state: ModelParticipationState;
  if (!live) state = "not_participated";
  // 建议失败不是"降级"：调度照常由模型完成，只是建议阶段没有产出。
  // 单独用 recommendationFailed 标记，避免出现"部分参与（发生过降级）"
  // 与"降级 0 次"同屏的矛盾。
  else if (fallbacks > 0) state = "partial";
  else if (reorders.length > 0) state = "reordered";
  else state = "kept_order";
  return {
    live,
    state,
    stateLabel: PARTICIPATION_LABELS[state],
    modelLabel: live ? (String(provider.model_id || "")
      ? modelLabel(provider) : "未标注模型") : "—（确定性调度）",
    agentLevel: String(request.agent_level || "l1").toUpperCase(),
    calls: Number(metrics.requests || 0),
    requestsByStage: {...(metrics.requests_by_stage || {})} as Record<string, number>,
    reorderCount: reorders.length,
    orderChanges,
    reasons,
    fallbacks,
    policyRejections: Number(metrics.policy_rejections || 0),
    recommendationFailed,
    recommendations,
  };
}

/** 统一模型名称（6.2.3）：跟随服务端 provider 信息，不硬编码品牌。
 *
 * DeepSeek 系列显示 DeepSeek，GLM 系列显示 GLM，其余显示真实
 * model_id；什么都拿不到时显示中性词"模型"。 */
export function modelLabel(providerInfo: {model_id?: unknown} | null | undefined): string {
  const modelId = String(providerInfo?.model_id || "").trim();
  if (!modelId) return "模型";
  const lower = modelId.toLowerCase();
  if (lower.includes("deepseek")) return "DeepSeek";
  if (lower.includes("glm")) return "GLM";
  return modelId;
}

/** 从 narration.recommendations 提取只读建议；旧 Bundle 没有该字段时返回 null。 */
export function recommendationView(
  bundle?: {narration?: {recommendations?: Record<string, unknown> | null}} | null,
): RecommendationView | null {
  const raw = bundle?.narration?.recommendations;
  if (!raw) return null;
  const items = Array.isArray(raw.items) ? raw.items : [];
  return {
    generated: raw.generated === true,
    items: items.map(item => ({
      anchor_id: String(item?.anchor_id || ""),
      line_refs: Array.isArray(item?.line_refs) ? item.line_refs.map(String) : [],
      evidence_ids: Array.isArray(item?.evidence_ids) ? item.evidence_ids.map(String) : [],
      action: String(item?.action || ""),
      rationale: String(item?.rationale || ""),
      verification: String(item?.verification || ""),
    })),
    failureReason: String(raw.failure_reason || ""),
    skippedReason: String(raw.skipped_reason || ""),
  };
}

/** 升级率度量（D1.2）：narration.autonomy 的只读投影。
 *
 * 字段名与服务端一致（snake_case 直传，口径见 definition）——这是
 * 审计事实，不是界面概念，不做改名。旧 Bundle 没有该字段时返回
 * null，绝不把「没记录」伪造成 0；确定性运行服务端如实记 0/0/0.0，
 * 这里同样原样呈现。 */
export type AutonomyView = {
  autonomous_decisions: number;
  escalations: number;
  escalation_rate: number;
  definition: string;
};

export function autonomyView(
  bundle?: {narration?: {autonomy?: Record<string, unknown> | null;
    [key: string]: unknown}} | null,
): AutonomyView | null {
  const raw = bundle?.narration?.autonomy;
  if (!raw) return null;
  return {
    autonomous_decisions: Number(raw.autonomous_decisions || 0),
    escalations: Number(raw.escalations || 0),
    escalation_rate: Number(raw.escalation_rate || 0),
    definition: String(raw.definition || ""),
  };
}

export type EvidencePassportStatus =
  | "COMPLETE" | "COMPLETED" | "PARTIAL" | "FAILED" | "ABORTED"
  | "CANCELLED" | "IN_PROGRESS";
export type EvidencePassportRestoreStatus = "verified" | "failed" | "in_progress" | "not_recorded";

export type EvidencePassportView = {
  status: EvidencePassportStatus;
  statusLabel: string;
  addedLines: number;
  loadLines: number;
  namedFailures: number;
  unevidencedLines: number;
  driftLines: number;
  restoreStatus: EvidencePassportRestoreStatus;
  restoreLabel: string;
  planFingerprint: string;
  bundleAvailable: boolean;
  live: boolean;
  modelLabel: string;
  modelCalls: number;
  recommendationLabel: string;
};

const PASSPORT_STATUS_LABELS: Record<EvidencePassportStatus, string> = {
  COMPLETE: "已完成",
  COMPLETED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "未签发",
  ABORTED: "已终止",
  CANCELLED: "已取消",
  IN_PROGRESS: "进行中（未盖章）",
};

const PASSPORT_RESTORE_LABELS: Record<EvidencePassportRestoreStatus, string> = {
  verified: "已验证",
  failed: "校验失败",
  in_progress: "进行中",
  not_recorded: "未记录",
};

// 护照只在终态盖章。终态集与 modou/agent/review.py 的 TERMINAL 一一对应；
// 其余一切状态（含未知与缺省）都是"进行中"，绝不落进 COMPLETE——
// 白名单式反转会把 DRAFTING、REPLANNING、AWAITING_HUMAN 全部盖成"已完成"。
const PASSPORT_TERMINAL_STATUSES = new Set([
  "COMPLETE", "COMPLETED", "PARTIAL", "FAILED", "ABORTED", "CANCELLED",
]);

export function passportStatus(status?: string | null): EvidencePassportStatus {
  const value = String(status || "").trim().toUpperCase();
  return PASSPORT_TERMINAL_STATUSES.has(value)
    ? value as EvidencePassportStatus : "IN_PROGRESS";
}

function addNamedFailures(value: unknown, ids: Set<string>) {
  if (!Array.isArray(value)) return;
  for (const item of value) {
    if (typeof item === "string" && item.trim()) ids.add(item.trim());
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    for (const key of ["test_id", "nodeid", "test", "name"]) {
      const id = String(row[key] || "").trim();
      if (id) { ids.add(id); break; }
    }
  }
}

function namedFailureCount(
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  ledger: Array<Record<string, unknown>> = [],
): number {
  const ids = new Set<string>();
  for (const event of events) {
    if (event.kind !== "probe.completed" && event.kind !== "probe.failed"
        && event.kind !== "observation.recorded") continue;
    const data = event.data || {};
    addNamedFailures(data.regressed_tests, ids);
    addNamedFailures(data.regressions, ids);
  }
  for (const record of ledger) {
    const payload = (record.payload || {}) as Record<string, unknown>;
    const data = (payload.data || record.data || {}) as Record<string, unknown>;
    addNamedFailures(data.regressed_tests, ids);
    addNamedFailures(data.regressions, ids);
  }
  return ids.size;
}

function restorePassportStatus(
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
): EvidencePassportRestoreStatus {
  if (events.some(event => event.kind === "restore.failed")) return "failed";
  const verified = events.filter(event => event.kind === "restore.verified");
  if (verified.some(event => event.data?.restored_clean === false)) return "failed";
  if (verified.length > 0) return "verified";
  if (events.some(event => event.kind === "restore.started"
      || event.kind === "restore.completed")) return "in_progress";
  return "not_recorded";
}

/** 完成页的最小证据护照：只汇总 Bundle 已发布的数字与事件，不扩张结论。 */
export function evidencePassport(
  status?: string | null,
  summary?: Record<string, unknown> | null,
  events: Array<{kind: string; data?: Record<string, unknown>}> = [],
  plan?: Record<string, unknown> | null,
  ledger: Array<Record<string, unknown>> = [],
  participation?: Pick<ModelParticipation, "live" | "modelLabel" | "calls"
    | "recommendationFailed" | "recommendations"> | null,
  bundleAvailable = false,
): EvidencePassportView {
  const byLabel = (summary?.by_label || {}) as Record<string, unknown>;
  const live = participation?.live === true;
  const recommendationLabel = !live ? "不适用（确定性运行）"
    : participation?.recommendationFailed ? "生成失败（不影响实验结论）"
    : participation?.recommendations?.generated
      ? `已生成 ${participation.recommendations.items.length} 条` : "未生成";
  const fingerprint = String(plan?.plan_sha256
    || plan?.fingerprint || plan?.sha256 || "未记录");
  return {
    status: passportStatus(status),
    statusLabel: PASSPORT_STATUS_LABELS[passportStatus(status)],
    addedLines: Number(summary?.total_added_lines || 0),
    loadLines: Number(byLabel["承重"] || 0),
    namedFailures: namedFailureCount(events, ledger),
    unevidencedLines: Number(byLabel["无据"] || 0),
    driftLines: Number(byLabel["游离"] || 0),
    restoreStatus: restorePassportStatus(events),
    restoreLabel: PASSPORT_RESTORE_LABELS[restorePassportStatus(events)],
    planFingerprint: fingerprint,
    bundleAvailable,
    live,
    modelLabel: live ? String(participation?.modelLabel || "未标注模型") : "",
    modelCalls: live ? Number(participation?.calls || 0) : 0,
    recommendationLabel,
  };
}

/** 完成页首句：只陈述事件先后，不声称模型更快或更准。 */
export function modelConclusion(participation: ModelParticipation, label = "模型"): string {
  if (!participation.live) {
    return "本次运行未调用模型，调度由确定性策略完成；实验结论来自水木验码的独立执行。";
  }
  let conclusion: string;
  if (participation.state === "partial") {
    conclusion = label + " 参与了本次运行的调度，其中发生过降级为确定性策略；"
      + "实验结论仍全部来自水木验码的独立执行。";
  } else if (participation.state === "kept_order") {
    conclusion = label + " 观察真实实验后选择保持原顺序；"
      + "随后水木验码按顺序独立执行 pytest 并生成证据。";
  } else {
    const change = participation.orderChanges[0] || "";
    const [before, after] = change.split("→").map(part => part.trim());
    if (before && after && before !== after) {
      conclusion = `${label} 根据上一步真实观测和剩余预算，将 ${after} 提到 ${before} 之前；`
        + "随后水木验码独立执行 pytest 并生成证据。";
    } else {
      conclusion = label + " 根据真实观测调整了检查顺序；"
        + "随后水木验码独立执行 pytest 并生成证据。";
    }
  }
  if (participation.recommendationFailed) {
    conclusion += "本次只读建议生成失败，不影响实验结论。";
  }
  return conclusion;
}

export type DecisionStageState = "pending" | "active" | "done";
export type DecisionStage = {
  key: string;
  label: string;
  state: DecisionStageState;
  note: string;
};

/** 运行中「模型决策轨迹」六步状态机：只由真实事件驱动，没有假进度。 */
export function modelDecisionStages(
  events: Array<{kind: string; data?: Record<string, unknown>}>,
  live: boolean,
  terminal = false,
  label = "模型",
): DecisionStage[] {
  const startedStages = new Set(events
    .filter(event => event.kind === "model.request.started")
    .map(event => String(event.data?.stage || "")));
  const hasScheduling = startedStages.has("scheduling");
  const hasAction = events.some(event => event.kind === "model.action");
  const hasRecommendationStart = startedStages.has("recommendation");
  const hasGenerated = events.some(event => event.kind === "recommendation.generated");
  const hasFailed = events.some(event => event.kind === "recommendation.failed");
  const anyStarted = startedStages.size > 0;
  const noModel = !live;
  return [
    {key: "goal", label: "目标已传入",
     state: noModel ? "pending" : anyStarted ? "done" : "active",
     note: noModel ? "确定性运行不经过模型" : "用户目标原文进入模型上下文"},
    {key: "observation", label: "等待真实实验观测",
     state: noModel ? "pending" : hasScheduling ? "done" : anyStarted ? "active" : "pending",
     note: "先实验，后决策"},
    {key: "deciding", label: label + " 正在判断下一步",
     state: noModel ? "pending" : hasAction ? "done" : hasScheduling ? "active" : "pending",
     note: "每次调用由真实观测触发"},
    {key: "decided", label: "已保持顺序或已重排",
     state: noModel ? "pending"
       : hasRecommendationStart || (terminal && hasAction) ? "done"
       : hasAction ? "active" : "pending",
     note: "只能在冻结候选内重排"},
    {key: "recommending", label: "正在生成证据建议",
     state: noModel ? "pending"
       : hasGenerated || hasFailed ? "done"
       : hasRecommendationStart ? "active" : "pending",
     note: "只读 · 最多 3 条"},
    {key: "recommended", label: "建议已生成",
     state: noModel ? "pending" : hasGenerated ? "done"
       : hasFailed ? "done" : "pending",
     note: hasFailed ? "生成失败，不影响实验结论"
       : hasGenerated ? "未执行 · 需要人工审阅" : ""},
  ];
}


/** 一行有好几条证据时，先给最有说服力的那条。

    `evidence_ids` 的顺序是**推导累积顺序**（先覆盖率、再实验、最后主张），
    直接取第 0 条会让「承重」行打开一份覆盖率观测——那不是它之所以承重的理由。
    读的人想看的是「删掉它，哪个测试会红」，也就是 Claim。
    Claim > Experiment > Fact。 */
export function decisiveEvidenceId(
  line: {evidence_ids?: string[]; unit_id?: string | null},
  bundle?: {evidence_bundle?: {ledger?: Array<Record<string, unknown>>}} | null,
): string {
  const ids = line.evidence_ids || [];
  const ledger = bundle?.evidence_bundle?.ledger || [];
  if (ledger.length) {
    const rank: Record<string, number> = {Claim: 0, Experiment: 1, Fact: 2};
    const best = ids
      .map(id => ({id, row: ledger.find(r => r.record_id === id)}))
      .filter(x => x.row)
      .sort((a, b) => (rank[String(a.row!.record_type)] ?? 9)
                    - (rank[String(b.row!.record_type)] ?? 9))[0];
    if (best) return best.id;
  }
  // v2 旧 Bundle 没有 evidence_ids。unit_id 是分析单元标识，不是 ledger
  // record_id；拿它请求 evidence API 会得到 404，所以宁可明确禁用。
  return ids[0] || "";
}

export type MinimizationView = {
  anchorId: string;
  targetRegression: string;
  frozenUnits: number;
  minimalUnits: number;
  oneMinimal: boolean;
  removalChecks: number;
  scopeNote: string;
  incompleteReason: string;
  /** true 表示 ddmin 结构上就不适用，一次实验都没花。 */
  notApplicable: boolean;
  experiments: number;
};

/** 为什么这个对象没有证书。"没跑"和"跑了没证出来"必须读起来不一样。 */
const MINIMIZATION_REASONS: Record<string, string> = {
  whole_file_candidate: "整份新文件没有更细的可删原子",
  no_added_line: "该对象没有新增行",
  no_deletable_added_statement: "新增行嵌在既有语句内部，构不成可独立删除的原子",
  duplicate_unit_ids: "原子标识冲突",
  overlapping_units: "原子行范围重叠",
  single_atom: "只有一条可删语句，本来就已经最小",
  source_unreadable: "源码读取失败",
  no_named_regression: "没有具名回归可供收窄",
  initial_pass: "移除全部新增语句也没有触发目标回归",
  initial_unresolved: "试验没有给出可用判定",
  not_minimizable: "原子集合不满足最小化前提",
  certificate_incomplete: "最小性检查未全部通过",
  source_changed_since_freeze: "源码与冻结时的原子不再一致",
};

export function minimizationReason(code: string): string {
  return MINIMIZATION_REASONS[code] || code;
}

/** Project ddmin certificates for display. Never invents one.
 *
 * The result surface shows these read-only: a certificate is something the run
 * either earned or did not. An anchor that could not be minimised is rendered
 * with its reason rather than dropped, so "no certificate" stays visible
 * instead of looking like the pass never happened.
 */
export function minimizationViews(
  report?: Record<string, unknown> | null): MinimizationView[] {
  const raw = (report?.minimizations || []) as Record<string, unknown>[];
  if (!Array.isArray(raw)) return [];
  return raw.map(row => {
    const cert = (row.certificate || {}) as Record<string, unknown>;
    const frozen = (cert.frozen_unit_ids || []) as unknown[];
    const minimal = (cert.minimal_unit_ids || []) as unknown[];
    const checks = (cert.removal_checks || []) as unknown[];
    return {
      anchorId: String(row.anchor_id || ""),
      targetRegression: String(row.target_regression || ""),
      frozenUnits: frozen.length,
      minimalUnits: minimal.length,
      oneMinimal: cert.one_minimal === true,
      removalChecks: checks.length,
      // Carried verbatim: the note is what keeps the claim inside its scope.
      scopeNote: String(cert.scope_note || ""),
      incompleteReason: row.complete === true
        ? "" : minimizationReason(String(row.reason || "未完成")),
      notApplicable: row.applicability === "not_applicable",
      experiments: Number(row.experiments || 0),
    };
  });
}

// --- 能力状态 -------------------------------------------------------------
//
// 界面上的每一个能力徽章都来自服务器的 /api/v1/capabilities，也就是发布链
// 用来校验对外材料的同一份注册表。这样"屏幕上说的"和"证据包允许说的"
// 不可能各说各话。

export type Capability = {
  id: string;
  title: string;
  state: string;
  state_label: string;
  runtime: string;
  summary: string;
  gate: string;
  evidence: string[];
};

const CAPABILITY_ORDER = ["verified", "experimental", "negative_result", "disabled"];

export function capabilityTone(state: string): "ok" | "warning" | "danger" | "idle" {
  if (state === "verified") return "ok";
  if (state === "experimental") return "warning";
  if (state === "negative_result") return "danger";
  return "idle";
}

/** 关闭的能力不可选：状态必须是产品属性，不能只是文档里的一个词。 */
export function capabilityAvailable(capabilities: Capability[], id: string): boolean {
  const found = capabilities.find(item => item.id === id);
  // 注册表还没加载完时不抢先禁用；服务器仍会在收单时再拒一次。
  return found ? found.runtime !== "unavailable" : true;
}

/** 按成熟度分组，已验证在前，未达门槛的排在后面且不会被折叠掉。 */
export function groupCapabilities(capabilities: Capability[]):
    Array<{state: string; label: string; items: Capability[]}> {
  return CAPABILITY_ORDER
    .map(state => {
      const items = capabilities.filter(item => item.state === state);
      return {state, label: items[0]?.state_label || state, items};
    })
    .filter(group => group.items.length > 0);
}

/** 结论句里的数字要能被一眼抓住。

    完成面板那句话是 70 多字排成三行宋体大字，`6 行获得测试承重证据` 里的
    `6` 和周围的字一样重，用户得逐字读完才知道结果。这里把数字段切出来，
    交给渲染层单独加重——只做视觉提取，不改写、不重排、不省略任何一个字。 */
export function emphasizeNumbers(text: string): Array<{text: string; number: boolean}> {
  if (!text) return [];
  const out: Array<{text: string; number: boolean}> = [];
  // 只提数字本身。把量词（"行"）一并拉进等宽字体，会让它和周围的宋体正文
  // 脱开一道缝，反而更难读——量词留在正文字体里。
  const pattern = /\d+(?:\.\d+)?/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) out.push({text: text.slice(last, at), number: false});
    out.push({text: match[0], number: true});
    last = at + match[0].length;
  }
  if (last < text.length) out.push({text: text.slice(last), number: false});
  return out;
}

export type ReviewDraft = {
  repo_id: string;
  repo_display_name: string;
  goal: string;
  review_focus: string;
  budget_seconds: number;
  success_conditions?: string[];
  non_goals?: string[];
};

export type DraftPayload = {
  available?: boolean;
  generated?: boolean;
  draft?: ReviewDraft;
  sources?: Record<string, string>;
  test_files_preview?: string[];
  failure_code?: string;
  failure_reason?: string;
  note?: string;
};

const REVIEW_FOCUS_LABELS: Record<string, string> = {
  "evidence-boundary": "综合检查证据边界",
  "named-regression": "寻找具名回归",
  "coverage-gap": "检查覆盖缺口",
  "call-path": "检查调用路径",
  "budget-first": "预算优先排序",
};

export function reviewFocusLabel(focus: string): string {
  return REVIEW_FOCUS_LABELS[focus] || focus;
}

/** 草案卡片逐字段成行；预算是默认值时必须标出来，不能冒充模型的选择。 */
export function draftCardRows(draft: ReviewDraft,
    sources?: Record<string, string>): Array<{label: string; value: string}> {
  const rows = [
    {label: "仓库", value: draft.repo_display_name || draft.repo_id},
    {label: "审查目标", value: draft.goal},
    {label: "审查侧重", value: reviewFocusLabel(draft.review_focus)},
    {label: "预算",
     value: draft.budget_seconds + " 秒"
       + (sources?.budget_seconds === "default" ? "（默认值，这句话没提预算）" : "")},
  ];
  if (draft.success_conditions?.length) {
    rows.push({label: "成功条件", value: draft.success_conditions.join("；")});
  }
  if (draft.non_goals?.length) {
    rows.push({label: "不做什么", value: draft.non_goals.join("；")});
  }
  return rows;
}

// ---- 一句话定位代码：命中证据与回退原因的中文呈现 ----------------
// evidence_detail 是 modou/locate.py 的封闭英文格式；这里逐类翻译。
// 未识别的格式原样透出——证据翻译错了比不翻译更糟，绝不编造依据。
const DETAIL_SYMBOL = /^symbol '(.+)' defined at line (\d+)$/;
const DETAIL_FILENAME = /^filename contains '(.+)'$/;
const DETAIL_DIRECTORY = /^directory contains '(.+)'$/;
const DETAIL_CONTENT = /^content contains '(.+)' at line (\d+)$/;

export function locateEvidenceLabel(kind: string, detail: string): string {
  const symbol = DETAIL_SYMBOL.exec(detail);
  if (kind === "symbol" && symbol) return `符号 ${symbol[1]} 定义于第 ${symbol[2]} 行`;
  const directory = DETAIL_DIRECTORY.exec(detail);
  // 后端目录命中发的 kind 是 "filename"（见 modou/locate.py search_repo），
  // detail 才是证据本身：目录命中按 detail 分派，不依赖 kind。
  if (directory) return `目录名包含「${directory[1]}」`;
  const filename = DETAIL_FILENAME.exec(detail);
  if (kind === "filename" && filename) return `文件名包含「${filename[1]}」`;
  const content = DETAIL_CONTENT.exec(detail);
  if (kind === "content" && content) return `第 ${content[2]} 行内容包含「${content[1]}」`;
  return detail;
}

export function locateFallbackNotice(reason: string): string {
  if (reason === "no_hits")
    return "没有在已授权仓库里找到相关代码；请回退手动选择仓库，或换个说法再试。";
  if (reason === "no_searchable_tokens")
    return "这句话里没有可用于检索的词：定位需要文件名、符号或代码关键词。请换个说法，或在下方表单手动选择仓库。";
  return "定位没有给出结果；请在下方表单手动选择仓库。";
}

export type AdmissibilityView = {
  grade: string;
  label: string;
  detail: string;
  tone: "success" | "warning" | "danger" | "idle";
};

/** 可采性四级的人话标签：措辞与证书里的 _GRADE_TEXT 同源，不得强于判据。 */
export function admissibilityView(grade?: string | null): AdmissibilityView | null {
  switch (grade) {
    case "A":
      return {grade, label: "A 级 · 行为证据", tone: "success",
        detail: "这条测试在基线里执行过被干预的行，移除后才失败——失败直接指认被干预的行为。"};
    case "B":
      return {grade, label: "B 级 · 间接证据", tone: "warning",
        detail: "这条测试没有执行过被干预的行，失败只能算间接证据。"};
    case "C":
      return {grade, label: "C 级 · 连带损坏", tone: "danger",
        detail: "这条测试根本没被执行（导入失败或收集期错误），它的失败不算承重证据。"};
    case "D":
      return {grade, label: "D 级 · 环境变动", tone: "warning",
        detail: "跳过条件改变导致的状态变化，与被测行为无关。"};
    default:
      return null;
  }
}

/** 旧三态口径判「承重」、新判据只给 C/D 级时，把两张口径摆在同一行。
 *  import 连带损坏案例在屏幕上要一眼看懂「为什么以前算、现在不算」。 */
export function admissibilityContrast(cert: {
  状态?: string; 可采性?: string | null; 可采性说明?: string | null;
}): string | null {
  if (cert.状态 !== "承重" || (cert.可采性 !== "C" && cert.可采性 !== "D")) {
    return null;
  }
  const note = cert.可采性说明
    ? cert.可采性说明.replace(/^[BCD][：:]\s*/, "") : "";
  const grade = cert.可采性 === "C" ? "C 级 · 连带损坏" : "D 级 · 环境变动";
  return "旧口径：承重✅ · 新判据：" + grade + (note ? "——" + note : "");
}

export type VisaStatusView = {
  label: string;
  detail: string;
  tone: "idle" | "active" | "success" | "warning" | "danger";
};

/** 签证与提议记录的状态视图：覆盖 record.status 与 attempts[].visa_status。 */
export function visaStatusView(status?: string | null): VisaStatusView {
  switch (status) {
    case "VERIFIED_EFFECTIVE":
      return {label: "已判定有效", tone: "success",
        detail: "候选在未见过的保留集干预上复现了断言级失败（k=2 一致）——按冻结协议算有效补测。"};
    case "PASSED_NOT_EFFECTIVE":
      return {label: "跑绿但未判定有效", tone: "warning",
        detail: "候选在保留集干预上没有产生 A 级失败：绿 ≠ 有效，带着生成集反馈继续修订。"};
    case "WITHHELD_SMALL_CASE":
      return {label: "保留判定 · 案例过小", tone: "idle",
        detail: "保留集计分干预不足，按冻结协议不予判定，不冒充有效。"};
    case "WITHHELD_NO_HOLDOUT":
      return {label: "保留判定 · 无保留集", tone: "idle",
        detail: "本次审查没有可用的保留集干预，签证无从谈起。"};
    case "VISA_ERROR":
      return {label: "签证器故障", tone: "danger",
        detail: "验证器未能完成判定，按失败关闭如实落档；这不是通过。"};
    case "PASSED":
      return {label: "提议通过", tone: "success",
        detail: "候选测试在隔离工作树跑绿并完成签证判定；只有 VERIFIED_EFFECTIVE 才算有效补测。"};
    case "PASSED_WITH_REVISION":
      return {label: "修订后通过", tone: "success",
        detail: "第一轮未达标，按失败摘录或生成集反馈修订后通过；全程只读你的 checkout。"};
    case "NEEDS_HUMAN":
      return {label: "需要人工决定", tone: "warning",
        detail: "候选经最多三轮（1 提议 + 2 修订）仍未达标：人工补写、换提法重试，或接受缺口。"};
    case "NOT_PROPOSED":
    default:
      return {label: "尚未提议", tone: "idle",
        detail: "为无据的行提议一条测试，并在冻结的保留集上挣签证。"};
  }
}

/** 外部门禁的中文摘要。键名保留在展开区；主卡片只放这句人话。 */
export const RELEASE_GATE_LABELS: Record<string, string> = {
  independent_hidden_qa: "独立隐藏质量验证",
  independent_linux_host: "独立 Linux 主机验证",
  authorized_private_repo_pilot: "授权私有仓库试点",
  real_developer_study: "真实开发者试用",
  independent_security_release_signoff: "独立安全发布签署",
};

export type ReleaseMachineState =
  | "not_generated" | "machine_failed"
  | "receipt_expired" | "machine_passed_external_pending" | "load_error";

export type ReleaseStatusView = {
  state: ReleaseMachineState;
  headline: string;
  detail: string;
  machineFailureReasons: string[];
  pendingGates: string[];
  evidence: Record<string, unknown>;
  generatedAt: string;
  sourceCommit: string;
  /** 服务端随响应带上的当前服务提交；仅过期分支携带。 */
  servingCommit?: string;
  rawMachineStatus: string;
};

/**
 * Project the release-status protocol into a conservative, user-facing view.
 * Machine failures are counted only from machine_failure_reasons; external
 * gates are always a separate paragraph and can never inflate that count.
 */
export function releaseStatusView(
  raw: Record<string, unknown> | null | undefined,
  loadError = false,
): ReleaseStatusView {
  const value = raw || {};
  const evidence = value.evidence && typeof value.evidence === "object"
    && !Array.isArray(value.evidence)
    ? value.evidence as Record<string, unknown> : {};
  const evidenceStatus = evidence.status && typeof evidence.status === "object"
    && !Array.isArray(evidence.status) ? evidence.status as Record<string, unknown> : {};
  // pending_gates 在 v1 协议里是具名门禁映射（对象），不是数组；
  // 待完成清单以 external_gates_pending 数组为准。映射不得挤掉数组，
  // 否则会把未待完成的门禁渲染成待完成。
  const pendingRaw = Array.isArray(value.pending_gates)
    ? value.pending_gates : value.external_gates_pending;
  const pendingGates = Array.isArray(pendingRaw)
    ? pendingRaw.filter(item => typeof item === "string").map(String) : [];
  const reasonsRaw = value.machine_failure_reasons;
  const machineFailureReasons = Array.isArray(reasonsRaw)
    ? reasonsRaw.filter(item => typeof item === "string").map(String) : [];
  const machine = String(value.machine_status || "");
  const generatedAt = String(value.generated_at || "");
  const sourceCommit = String(value.source_commit || "");
  const servingCommit = String(value.serving_commit || "");
  const receiptExpired = value.receipt_expired === true;
  // Failure arrays are protocol data, not user copy. Keep opaque identifiers
  // in the technical disclosure and use a conservative sentence in the card.
  const readableFailures = machineFailureReasons.map(reason =>
    /^[A-Za-z0-9_.:-]+$/.test(reason) ? "有一项机器检查未通过" : reason);
  if (loadError || String(evidenceStatus.state || "") === "load_error") return {
    state: "load_error", headline: "发布记录暂时无法读取",
    detail: "发布检查服务暂时不可用；仓库、预设和审查仍可继续使用。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    rawMachineStatus: machine,
  };
  // 过期收据优先于一切状态解读：一份描述别的提交的收据，无论里面写
  // 了通过还是失败，都不能照常渲染成当前状态。
  if (receiptExpired) return {
    state: "receipt_expired",
    headline: "收据已过期，需重算",
    detail: "这份发布状态收据描述的是另一个提交，不能代表当前代码；重新生成收据后本卡自动恢复。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    servingCommit, rawMachineStatus: machine,
  };
  if (String(evidenceStatus.state || "") === "not_generated") return {
    state: "not_generated",
    headline: "尚未加载发布检查记录",
    detail: "尚未生成发布检查记录；这不等于代码失败。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    rawMachineStatus: machine,
  };
  if (machine === "MACHINE_CHECKS_FAILED") return {
    state: "machine_failed",
    headline: machineFailureReasons.length
      ? `机器检查有 ${machineFailureReasons.length} 项未通过`
      : "机器检查未通过",
    detail: machineFailureReasons.length
      ? readableFailures.join("；")
      : "发布检查记录未提供具体机器失败原因。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    rawMachineStatus: machine,
  };
  if (machine === "MACHINE_CHECKS_PASSED") return {
    state: "machine_passed_external_pending",
    headline: "机器检查已通过，外部验证仍待完成",
    detail: pendingGates.length
      ? `待完成：${pendingGates.map(releaseGateLabel).join("、")}`
      : "外部验证仍待完成。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    rawMachineStatus: machine,
  };
  return {
    state: "not_generated", headline: "尚未加载发布检查记录",
    detail: "尚未生成发布检查记录；这不等于代码失败。",
    machineFailureReasons, pendingGates, evidence, generatedAt, sourceCommit,
    rawMachineStatus: machine,
  };
}

export function releaseGateLabel(key: string): string {
  return RELEASE_GATE_LABELS[key] || key;
}

// ---- 发布状态里被 normalizer 丢掉的具名信息 --------------------------

/** 界面探针状态：已知值给中文，未知值原样透出，不猜。 */
export function uiProbeStatusView(status: string | null | undefined): string {
  const key = String(status || "").trim();
  if (!key) return "";
  if (key === "PENDING_REPREFLIGHT") return "待重新预检（界面探针尚未执行）";
  if (key === "PASSED") return "界面探针已通过";
  if (key === "FAILED") return "界面探针未通过";
  return key;
}

const NARROW_PACKAGE_CHECK_LABELS: Record<string, string> = {
  build: "构建",
  tests: "测试",
  license_audit: "许可证审计",
  sbom: "SBOM",
  sensitive_scan: "敏感信息扫描",
};

export function narrowPackageChecksView(
    checks: Record<string, unknown> | null | undefined):
    Array<{label: string; state: string}> {
  if (!checks || typeof checks !== "object") return [];
  return Object.entries(NARROW_PACKAGE_CHECK_LABELS)
    .filter(([key]) => key in checks)
    .map(([key, label]) => ({
      label,
      state: checks[key] === true ? "通过" : checks[key] === false
        ? "未通过" : "未提供",
    }));
}

const EXECUTION_LIMIT_LABELS: Record<string, string> = {
  cpu_seconds: "CPU 时间上限",
  max_memory_bytes: "内存用量上限",
  max_processes: "进程数上限",
  max_disk_bytes: "磁盘写入上限",
  max_files: "可写文件数上限",
  max_output_bytes: "输出大小上限",
  max_file_bytes: "单文件大小上限",
};

export function executionLimitLabel(key: string): string {
  return EXECUTION_LIMIT_LABELS[key] ?? key;
}

/** 资源策略里「明知未强制」的限制清单；沙箱模式会点名 5 项。 */
export function unenforcedLimitsView(policy: unknown): string[] {
  const limits = (policy as {unenforced_limits?: unknown} | null | undefined)
    ?.unenforced_limits;
  if (!Array.isArray(limits)) return [];
  return limits.map(item => executionLimitLabel(String(item)));
}

/** 证书「逐条定级」：per-test 的 before/after 与等级。 */
export type CertificateGradeRow = {
  testId: string; before: string; after: string; grade: string;
};

export function certificateGradeRows(
    cert: Record<string, unknown> | null | undefined): CertificateGradeRow[] {
  const rows = cert ? cert["逐条定级"] : null;
  if (!Array.isArray(rows)) return [];
  return rows.map(row => {
    const item = (row && typeof row === "object" ? row : {}) as Record<string, unknown>;
    return {
      testId: String(item.test_id ?? ""),
      before: String(item.before ?? ""),
      after: String(item.after ?? ""),
      grade: String(item["等级"] ?? ""),
    };
  }).filter(row => row.testId);
}

/** 时间线的展示层归纳：不碰原始账本，只在按钮里补一行字段摘要。

    同一种事件第一次出现时展示完整值；之后只列出发生变化的字段；完全没有
    变化的重复事件不再罗列任何字段。点击事件仍然打开抽屉看完整原始数据，
    所以这里可以放心地省——省掉的是重复，不是证据。 */
export type EventDelta = {fields: string[]; first: boolean};

const DELTA_FIELD_LIMIT = 4;
const DELTA_VALUE_LIMIT = 44;

function deltaValue(value: unknown): string {
  if (Array.isArray(value)) {
    const head = value.slice(0, 2).map(item => String(item)).join("、");
    return value.length > 2 ? `${head} 等 ${value.length} 项` : head || "空";
  }
  const text = typeof value === "object" && value !== null
    ? JSON.stringify(value) : String(value);
  return text.length > DELTA_VALUE_LIMIT ? text.slice(0, DELTA_VALUE_LIMIT - 1) + "…" : text;
}

export function eventDeltas(
    events: ReadonlyArray<{event_id: string; kind: string;
                           data?: Record<string, unknown> | null}>,
): Record<string, EventDelta> {
  const previous: Record<string, Record<string, unknown>> = {};
  const out: Record<string, EventDelta> = {};
  for (const event of events) {
    const data = event.data || {};
    const prior = previous[event.kind];
    previous[event.kind] = data;
    if (prior === undefined) {
      out[event.event_id] = {
        fields: Object.entries(data).slice(0, DELTA_FIELD_LIMIT)
          .map(([key, value]) => `${key} ${deltaValue(value)}`),
        first: true,
      };
      continue;
    }
    const changed = Object.keys(data).filter(key =>
      !(key in prior) || JSON.stringify(prior[key]) !== JSON.stringify(data[key]));
    out[event.event_id] = {
      fields: changed.slice(0, DELTA_FIELD_LIMIT)
        .map(key => `${key} ${deltaValue(data[key])}`),
      first: false,
    };
  }
  return out;
}

/** 摘要在屏幕上的统一写法。

    界面上原本三处 hash 三种长度：计划指纹铺满 64 位、证据护照切 12 位、
    EvidenceManifest 又是全量。对照的时候没人会去数第 37 位，长的那种只是
    把一行撑爆；但截断不能把**短的占位值**也切了（"plan-demo-sha" 截到 12 位
    会变成一个看起来像真摘要的假值）。所以只截真正的十六进制摘要。 */
export const HASH_DISPLAY_LENGTH = 12;
const HEX_DIGEST = /^[0-9a-f]{32,}$/i;

export function shortHash(value: unknown, length = HASH_DISPLAY_LENGTH):
    {short: string; full: string; truncated: boolean} {
  const full = String(value ?? "").trim();
  if (!HEX_DIGEST.test(full) || full.length <= length) {
    return {short: full, full, truncated: false};
  }
  return {short: full.slice(0, length), full, truncated: true};
}

/** 常驻授权状态卡的展示层形状。

    L3 无人值守的前提是“驱动不是批准者”：批准来自人事先签发的常驻
    授权文件。这张卡把授权的可查面（谁签的、覆盖哪些仓库、还剩多久、
    写入预算多少）如实公开；未加载时也如实说，不装作可用。 */
export type StandingAuthorizationView = {
  loaded: boolean;
  authorizedBy: string;
  sourcePrefix: string;
  validUntil: string;
  expired: boolean;
  repos: string[];
  budgetSecondsMax: number;
  maxWritesPerReview: number;
  maxWritesPerHour: number;
};

const STANDING_AUTHORIZATION_EMPTY: StandingAuthorizationView = {
  loaded: false,
  authorizedBy: "",
  sourcePrefix: "",
  validUntil: "",
  expired: false,
  repos: [],
  budgetSecondsMax: 0,
  maxWritesPerReview: 0,
  maxWritesPerHour: 0,
};

export function standingAuthorizationView(raw: unknown): StandingAuthorizationView {
  const data = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  if (data["loaded"] !== true) return {...STANDING_AUTHORIZATION_EMPTY};
  return {
    loaded: true,
    authorizedBy: String(data["authorized_by"] ?? ""),
    sourcePrefix: String(data["source_sha256_prefix"] ?? ""),
    validUntil: String(data["valid_until"] ?? ""),
    expired: data["expired"] === true,
    repos: Array.isArray(data["repo_whitelist"])
      ? data["repo_whitelist"].map(item => String(item)) : [],
    budgetSecondsMax: Number(data["budget_seconds_max"] ?? 0) || 0,
    maxWritesPerReview: Number(data["max_writes_per_review"] ?? 0) || 0,
    maxWritesPerHour: Number(data["max_writes_per_hour"] ?? 0) || 0,
  };
}

function historicalReceiptVerdict(data: Record<string, unknown>): string {
  const binding = data["source_binding"] as Record<string, unknown> | undefined;
  const verdict = String(data["verdict"] ?? "");
  if (binding?.["status"] === "stale") return `历史${verdict} · 当前版本待复验`;
  if (binding?.["status"] === "unverified") return `历史${verdict} · 版本未验证`;
  return verdict;
}

/** 评测回执索引的一行：先看 verdict 与判据计数，再按 id 取明细。 */
export type EvalReceiptIndexEntry = {
  id: string;
  verdict: string;
  criteriaPassed: number;
  criteriaTotal: number;
  freezeSha256: string;
  finishedAt: string;
  source: string;
};

export function normalizeEvalReceiptIndex(raw: unknown): EvalReceiptIndexEntry[] {
  const data = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  const receipts = Array.isArray(data["receipts"]) ? data["receipts"] : [];
  return receipts.map(item => {
    const entry = item && typeof item === "object"
      ? item as Record<string, unknown> : {};
    return {
      id: String(entry["id"] ?? ""),
      verdict: historicalReceiptVerdict(entry),
      criteriaPassed: Number(entry["criteria_passed"] ?? 0) || 0,
      criteriaTotal: Number(entry["criteria_total"] ?? 0) || 0,
      freezeSha256: String(entry["freeze_sha256"] ?? ""),
      finishedAt: String(entry["finished_at"] ?? ""),
      source: String(entry["source"] ?? ""),
    };
  }).filter(entry => entry.id);
}

/** 一条判据的展示行：observed 和 threshold 并排放，让"差多少"自己说话。 */
export type EvalReceiptCriterionView = {
  id: string;
  text: string;
  observed: string;
  comparison: string;
  passed: boolean;
};

/** 一条对照臂的展示行：只聚合回执里已有的计数字段，不推新口径。 */
export type EvalReceiptArmView = {
  arm: string;
  label: string;
  reviews: number;
  humanApprovals: number;
  writesAttempted: number;
  writesVerified: number;
};

const ARM_LABELS: Record<string, string> = {
  standing_authorization: "L3 · 常驻授权臂",
  human_approved: "L2 · 人工批准对照臂",
};

/** 评测回执明细的展示层形状：只读证据，normalize 不补任何判断。 */
export type EvalReceiptView = {
  id: string;
  schemaVersion: string;
  verdict: string;
  verdictLabel: string;
  smoke: boolean;
  startedAt: string;
  finishedAt: string;
  freezePath: string;
  freezeSha256: string;
  criteria: EvalReceiptCriterionView[];
  criteriaPassed: number;
  arms: EvalReceiptArmView[];
};

function receiptCriterionView(raw: unknown): EvalReceiptCriterionView {
  const data = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  const threshold = data["threshold"];
  const thresholdText = threshold === undefined || threshold === null
    ? "" : String(threshold);
  const op = String(data["op"] ?? "");
  return {
    id: String(data["id"] ?? ""),
    text: String(data["text"] ?? ""),
    observed: String(data["observed"] ?? ""),
    comparison: `${op} ${thresholdText}`.trim(),
    passed: data["passed"] === true,
  };
}

function receiptArmView(key: string, raw: unknown): EvalReceiptArmView {
  const data = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  const reviews = Array.isArray(data["reviews"]) ? data["reviews"] : [];
  const writes = Array.isArray(data["writes"]) ? data["writes"] : [];
  return {
    arm: key,
    label: ARM_LABELS[key] ?? key,
    reviews: reviews.length,
    humanApprovals: reviews.reduce((sum, item) => {
      const review = item && typeof item === "object"
        ? item as Record<string, unknown> : {};
      return sum + (Number(review["human_approvals"] ?? 0) || 0);
    }, 0),
    writesAttempted: writes.filter(item => {
      const write = item && typeof item === "object"
        ? item as Record<string, unknown> : {};
      return write["attempted"] === true;
    }).length,
    writesVerified: writes.filter(item => {
      const write = item && typeof item === "object"
        ? item as Record<string, unknown> : {};
      // v3 回执直接给 verified 布尔；v2 只有 verify_status——两种都认。
      return write["verified"] === true
        || write["verify_status"] === "VERIFIED";
    }).length,
  };
}

export function normalizeEvalReceipt(raw: unknown): EvalReceiptView | null {
  const data = raw && typeof raw === "object"
    ? raw as Record<string, unknown> : {};
  if (!data["verdict"] && !data["criteria"]) return null;
  const freeze = data["freeze"] && typeof data["freeze"] === "object"
    ? data["freeze"] as Record<string, unknown> : {};
  const criteria = (Array.isArray(data["criteria"]) ? data["criteria"] : [])
    .map(receiptCriterionView).filter(criterion => criterion.id);
  const armsRaw = data["arms"] && typeof data["arms"] === "object"
    ? data["arms"] as Record<string, unknown> : {};
  return {
    id: String(data["id"] ?? ""),
    schemaVersion: String(data["schema_version"] ?? ""),
    verdict: historicalReceiptVerdict(data),
    verdictLabel: historicalReceiptVerdict(data) !== String(data["verdict"] ?? "")
      ? (data["verdict"] === "PASS" ? "历史通过" : "历史未通过")
      : data["verdict"] === "PASS" ? "通过" : "未通过",
    smoke: data["smoke"] === true,
    startedAt: String(data["started_at"] ?? ""),
    finishedAt: String(data["finished_at"] ?? ""),
    freezePath: String(freeze["path"] ?? ""),
    freezeSha256: String(freeze["sha256"] ?? ""),
    criteria,
    criteriaPassed: criteria.filter(criterion => criterion.passed).length,
    arms: Object.keys(armsRaw).map(key => receiptArmView(key, armsRaw[key])),
  };
}
