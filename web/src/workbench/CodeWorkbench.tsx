// 只读证据工作台：文件树 + Monaco 只读视图 + 本文件证据列表 + 人工评论。
// 这个模块只在用户点击"打开代码工作台"后被懒加载，Monaco 主包与
// worker 永远不进首屏。人工评论与机器证据分层：评论只进独立注释包，
// 不改写机器结论；gutter 上机器符号走 glyph 泳道，人工评论走行标泳道。
import {useEffect, useMemo, useRef, useState, type ReactNode} from "react";
import type * as Monaco from "monaco-editor";
import {baseModelUri, loadMonaco} from "./monaco-env";
import {
  VERDICT_SPECS, buildTree, defaultExpandedPaths, initialFocus,
  testsForEvidence, unlabeledReasonLabel, verdictForLine, verdictSpec,
  type EvidenceLine, type FocusTarget, type SourceFile, type SourceTree,
  type TreeNode, type Verdict,
} from "./model";
import {
  COMMENT_KIND_LABELS, COMMENT_TOOLBAR_KINDS,
  anchorPreview, groupThreads, kindLabel, markForLine, statusLabel,
  validateCommentText,
  type CommentKind, type CommentList, type CommentRecord,
  type NewCommentInput,
} from "./comments";
import {admissibilityView} from "../presentation";
import {
  answeredLabel, CHALLENGE_REASONS, DEFAULT_CHALLENGE_REASON,
  evidenceDeltaRows, observationsCount, openItemLabel, outcomeClass,
  outcomeLabel, outcomeReasonView, outcomeSummary, reasonLabel,
  reverificationForComment,
  statusLabel as revStatusLabel,
  strategyLabel, testProposalView, validateChallengeInput,
  type ChallengeReason,
  type ReverificationList, type ReverificationRecord,
} from "./reverification";
import {buildTimeline} from "./timeline";
import {OfficialLockup} from "../OfficialBrand";

export type ReplyInput = {author: string; body: string; kind: CommentKind};
export type StatusInput =
  {revision: number; status: "resolved" | "open" | "withdrawn"};

type Props = {
  reviewId: string;
  lines: EvidenceLine[];
  ledger?: Array<Record<string, unknown>>;
  focus: FocusTarget | null;
  theme: "beige" | "dark";
  fetchTree: () => Promise<SourceTree>;
  fetchFile: (path: string) => Promise<SourceFile>;
  fetchComments: (path: string) => Promise<CommentList>;
  createComment: (input: NewCommentInput) => Promise<CommentRecord>;
  replyComment: (commentId: string, input: ReplyInput)
    => Promise<CommentRecord>;
  updateComment: (commentId: string, input: StatusInput)
    => Promise<CommentRecord>;
  fetchReverifications: () => Promise<ReverificationList>;
  requestReverification: (input: {comment_id: string; reason: string;
    explanation?: string}) => Promise<ReverificationRecord>;
  approveReverification: (reverificationId: string)
    => Promise<ReverificationRecord>;
  rejectReverification: (reverificationId: string, note?: string)
    => Promise<ReverificationRecord>;
  onOpenTimeline?: () => void;
  onOpenEvidence: (line: EvidenceLine) => void;
  onClose: () => void;
};

// #42 选中行的滚条标记颜色：Monaco 的 overview ruler 画在 canvas 上，
// 不认 CSS 变量，只能按主题 prop 写死具体色值。两个值分别是两套色板
// 里 --violet-bright 的原值（styles.css 的 :root 与 [data-theme="beige"]）；
// 改 token 时必须同步这里，否则滚条标记会掉出主题。
const SELECTED_RULER_COLOR = {beige: "#1d5a62", dark: "#a98cff"} as const;

type LoadState = {phase: "loading"} | {phase: "error"; message: string} | null;

const VERDICT_ORDER: Verdict[] = ["load", "failure", "unevidenced", "drift",
  "ai", "comment", "hollow", "unlabeled"];
const PRIMARY_VERDICTS: Verdict[] = ["load", "unevidenced", "drift", "unlabeled"];

export default function CodeWorkbench(props: Props) {
  const {reviewId, lines, ledger, focus, theme, fetchTree, fetchFile,
    fetchComments, createComment, replyComment, updateComment,
    fetchReverifications, requestReverification, approveReverification,
    rejectReverification, onOpenTimeline,
    onOpenEvidence, onClose} = props;
  const [treeState, setTreeState] = useState<LoadState>({phase: "loading"});
  const [tree, setTree] = useState<SourceTree | null>(null);
  const [treeFilter, setTreeFilter] = useState("");
  const [openPath, setOpenPath] = useState("");
  const [fileState, setFileState] = useState<LoadState | null>(null);
  const [file, setFile] = useState<SourceFile | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [selectedLine, setSelectedLine] = useState<EvidenceLine | null>(null);
  const [commentsList, setCommentsList] = useState<CommentList | null>(null);
  const [commentsState, setCommentsState] = useState<LoadState | null>(null);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [selection, setSelection] = useState<{start: number; end: number} | null>(null);
  const [composer, setComposer] = useState
    <{kind: CommentKind; start: number; end: number} | null>(null);
  const [author, setAuthor] = useState("");
  const [body, setBody] = useState("");
  const [composerError, setComposerError] = useState("");
  const [busy, setBusy] = useState(false);
  const [replyFor, setReplyFor] = useState<string | null>(null);
  const [replyBody, setReplyBody] = useState("");
  const [replyKind, setReplyKind] = useState<CommentKind>("note");
  const [threadError, setThreadError] = useState("");
  const [reverifications, setReverifications] =
    useState<ReverificationList | null>(null);
  const [revState, setRevState] = useState<LoadState | null>(null);
  const [planFor, setPlanFor] = useState<string | null>(null);
  const [planReason, setPlanReason] = useState(DEFAULT_CHALLENGE_REASON);
  const [planExplanation, setPlanExplanation] = useState("");
  const [planError, setPlanError] = useState("");
  const hostRef = useRef<HTMLDivElement | null>(null);
  const authorRef = useRef<HTMLInputElement | null>(null);
  const composerTriggerRef = useRef<HTMLButtonElement | null>(null);
  const editorRef = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const monacoRef = useRef<typeof Monaco | null>(null);
  const modelsRef = useRef(new Map<string, Monaco.editor.ITextModel>());
  const filesRef = useRef(new Map<string, SourceFile>());
  const decorationsRef =
    useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const revealRef = useRef<{path: string; line: number} | null>(null);
  // Monaco 挂载 effect 只跑一次，闭包里的 openPath/lines/commentsList 会
  // 冻结在首次渲染；这里统一走"最新值 ref"，事件回调与装饰重算都读它。
  const latestRef = useRef({openPath, lines, onOpenEvidence});
  latestRef.current = {openPath, lines, onOpenEvidence};
  const commentsRef = useRef<CommentList | null>(null);
  commentsRef.current = commentsList;
  const selectedLineRef = useRef<EvidenceLine | null>(null);
  selectedLineRef.current = selectedLine;

  const nodes = useMemo(() => tree ? buildTree(tree.entries) : [], [tree]);
  const filteredNodes = useMemo(() => {
    const query = treeFilter.trim().toLowerCase();
    if (!query) return nodes;
    const filter = (items: TreeNode[]): TreeNode[] => items.flatMap(node => {
      if (node.kind === "file") return node.path.toLowerCase().includes(query) ? [node] : [];
      const children = filter(node.children);
      return node.path.toLowerCase().includes(query) || children.length
        ? [{...node, children}] : [];
    });
    return filter(nodes);
  }, [nodes, treeFilter]);
  const fileLines = useMemo(
    () => lines.filter(line => line.file === openPath), [lines, openPath]);
  const threads = useMemo(
    () => groupThreads(commentsList?.comments ?? []), [commentsList]);
  // 行动时间线：把已装载的账本/结论/复验记录投影成四泳道，
  // 不新起请求、不重算结论——每条都是既有记录的转写。
  const timeline = useMemo(() => buildTimeline({
    ledger, lines,
    reverifications: reverifications?.reverifications ?? [],
  }), [ledger, lines, reverifications]);
  const relatedTimeline = useMemo(() => Object.values(timeline).flat()
    .filter(item => !item.ref || !openPath || item.ref.path === openPath),
    [timeline, openPath]);
  // 撰写器覆盖的行里有机器证据行时，评论自动绑定它的主张与证据。
  const boundEvidence = useMemo(() => {
    if (!composer) return null;
    return lines.find(line => line.file === openPath
      && line.line >= composer.start && line.line <= composer.end) ?? null;
  }, [composer, lines, openPath]);
  const boundClaimId = boundEvidence
    ? (boundEvidence.unit_id ?? boundEvidence.evidence_ids?.[0] ?? null) : null;
  const boundEvidenceIds = boundEvidence?.evidence_ids ?? [];
  // 可采性徽标：模型自带的 A/B/C/D 等级，前端只翻译不反推。
  const selectedGrade = selectedLine
    ? admissibilityView(selectedLine.admissibility) : null;

  const openFile = async (path: string, revealLine?: number) => {
    if (revealLine) revealRef.current = {path, line: revealLine};
    const cached = filesRef.current.get(path);
    if (cached) {
      if (openPath !== path) {
        setOpenPath(path);
        setSelectedLine(null);
        setSelection(null);
        setComposer(null);
        setReplyFor(null);
        setFile(cached);
        setFileState(null);
        mountModel(path, revealLine);
      } else if (revealLine) {
        mountModel(path, revealLine);
      }
      return;
    }
    setOpenPath(path);
    setSelectedLine(null);
    setSelection(null);
    setComposer(null);
    setReplyFor(null);
    setFileState({phase: "loading"});
    setFile(null);
    try {
      const payload = await fetchFile(path);
      filesRef.current.set(path, payload);
      setFile(payload);
      setFileState(null);
    } catch (error) {
      setFileState({phase: "error",
        message: error instanceof Error ? error.message : String(error)});
    }
  };

  // 首次装载：取文件树，按"修改 > 证据 > 评论"展开并打开初始定位文件。
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const payload = await fetchTree();
        if (cancelled) return;
        setTree(payload);
        setTreeState(null);
        const built = buildTree(payload.entries);
        setExpanded(defaultExpandedPaths(built));
        const target = focus ?? initialFocus(built, lines);
        if (target) await openFile(target.path, target.line);
      } catch (error) {
        if (!cancelled) setTreeState({phase: "error",
          message: error instanceof Error ? error.message : String(error)});
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 外部跳转请求（结果行 / 具名测试 / 评论锚点）到达时换文件并定位。
  useEffect(() => {
    if (!focus) return;
    void openFile(focus.path, focus.line);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.nonce]);

  // 本文件评论：打开文件与每次写操作后重取；错误降级为侧栏提示。
  useEffect(() => {
    if (!openPath) return;
    let cancelled = false;
    setCommentsState({phase: "loading"});
    void (async () => {
      try {
        const payload = await fetchComments(openPath);
        if (cancelled) return;
        setCommentsList(payload);
        setCommentsState(null);
      } catch (error) {
        if (!cancelled) setCommentsState({phase: "error",
          message: error instanceof Error ? error.message : String(error)});
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openPath, refreshNonce]);

  // 复验记录随工作台装载，并在每次评论/复验写操作后与评论一起刷新。
  // 读取失败降级为侧栏提示：评论仍然可用，只是暂时挂不上复验状态。
  useEffect(() => {
    let cancelled = false;
    setRevState({phase: "loading"});
    void (async () => {
      try {
        const payload = await fetchReverifications();
        if (cancelled) return;
        setReverifications(payload);
        setRevState(null);
      } catch (error) {
        if (!cancelled) setRevState({phase: "error",
          message: error instanceof Error ? error.message : String(error)});
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshNonce]);

  // 装载 Monaco 并创建只读编辑器。
  useEffect(() => {
    let disposed = false;
    let editor: Monaco.editor.IStandaloneCodeEditor | null = null;
    void (async () => {
      const monaco = await loadMonaco();
      if (disposed || !hostRef.current) return;
      monacoRef.current = monaco;
      editor = monaco.editor.create(hostRef.current, {
        value: "",
        language: "python",
        theme: theme === "dark" ? "vs-dark" : "vs",
        readOnly: true,
        domReadOnly: true,
        glyphMargin: true,
        minimap: {enabled: false},
        scrollBeyondLastLine: false,
        automaticLayout: true,
        fontSize: 16,
        lineHeight: 24,
        lineNumbersMinChars: 4,
        renderWhitespace: "none",
        scrollbar: {alwaysConsumeMouseWheel: false},
      });
      editorRef.current = editor;
      editor.onMouseDown(event => {
        const target = event.target;
        const lineNumber = target.position?.lineNumber
          ?? (target.range ? target.range.startLineNumber : undefined);
        if (!lineNumber) return;
        const latest = latestRef.current;
        const hit = latest.lines.find(line => line.file === latest.openPath
          && line.line === lineNumber);
        if (!hit) return;
        // gutter、行号或行内任意点击都算"在代码上核验这条证据"。
        setSelectedLine(hit);
        latest.onOpenEvidence(hit);
      });
      // 光标/选区变化驱动评论工具条：点一行或选中多行都能锚定。
      editor.onDidChangeCursorSelection(event => {
        const range = event.selection;
        const start = Math.min(range.startLineNumber, range.endLineNumber);
        const end = Math.max(range.startLineNumber, range.endLineNumber);
        const lineCount = editorRef.current?.getModel()?.getLineCount() ?? 0;
        if (lineCount <= 0 || start < 1 || end > lineCount) return;
        setSelection({start, end});
      });
      const pending = revealRef.current;
      if (pending && filesRef.current.has(pending.path)) {
        mountModel(pending.path, pending.line);
      }
    })();
    return () => {
      disposed = true;
      decorationsRef.current?.clear();
      decorationsRef.current = null;
      editor?.dispose();
      editorRef.current = null;
      for (const model of modelsRef.current.values()) model.dispose();
      modelsRef.current.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    editorRef.current?.updateOptions(
      {theme: theme === "dark" ? "vs-dark" : "vs"});
  }, [theme]);

  function mountModel(path: string, revealLine?: number) {
    const monaco = monacoRef.current;
    const editor = editorRef.current;
    const payload = filesRef.current.get(path);
    if (!monaco || !editor || !payload) return;
    const uri = baseModelUri(reviewId, path, payload.blob_sha256);
    const parsed = monaco.Uri.parse(uri);
    let model = monaco.editor.getModel(parsed);
    if (!model) {
      model = monaco.editor.createModel(payload.content,
        payload.language || "text", parsed);
      modelsRef.current.set(uri, model);
    }
    editor.setModel(model);
    applyDecorations(path);
    if (revealLine && revealLine >= 1 && revealLine <= model.getLineCount()) {
      // #42 只在目标行看不见时才滚：从右栏点击近处行号时视窗不该跳动。
      editor.revealLineInCenterIfOutsideViewport(revealLine);
      editor.setSelection({startLineNumber: revealLine, startColumn: 1,
        endLineNumber: revealLine, endColumn: 1});
    }
  }

  function applyDecorations(path: string) {
    const monaco = monacoRef.current;
  const editor = editorRef.current;
  if (!monaco || !editor) return;
  const model = editor.getModel();
  const forFile = latestRef.current.lines.filter(line => line.file === path);
  const machineLines = new Set(forFile.map(line => line.line));
  // 快照行号越界的证据行不许进装饰：Monaco 会把越界 range 收敛到最后一
  // 行，多条 isWholeLine 背景叠在同一行上，把代码文字糊成一条深色横带。
  const lineCount = model?.getLineCount()
    ?? filesRef.current.get(path)?.line_count ?? 0;
  const decorations: Array<Monaco.editor.IModelDeltaDecoration> = [];
  for (const line of forFile) {
    if (line.line < 1 || line.line > lineCount) continue;
    const spec = verdictSpec(line);
      const reasonText = unlabeledReasonLabel(line.reason);
      const grade = admissibilityView(line.admissibility);
      decorations.push({
        range: new monaco.Range(line.line, 1, line.line, 1),
        options: {
          isWholeLine: true,
          className: "wb-line-" + verdictForLine(line),
          glyphMarginClassName: "wb-glyph " + spec.className,
          glyphMarginHoverMessage: {
            value: "**" + spec.name + "**　" + spec.description
              + (reasonText ? "\n\n原因：" + reasonText : "")
              + (grade ? "\n\n可采性：" + grade.label : "")},
          linesDecorationsClassName: "wb-line-mark " + spec.className,
        },
      });
    }
    // 人工评论泳道：行标区里的实心/空心蓝点，与机器 glyph 分层；
    // 只有该行没有机器结论底色时才加评论底色，避免两层背景打架。
    const fileComments = commentsRef.current?.comments ?? [];
    for (let line = 1; line <= lineCount; line += 1) {
      const mark = markForLine(fileComments, path, line);
      if (!mark) continue;
      decorations.push({
        range: new monaco.Range(line, 1, line, 1),
        options: {
          isWholeLine: true,
          className: mark === "comment" && !machineLines.has(line)
            ? "wb-line-comment" : undefined,
          linesDecorationsClassName: "wb-cmark "
            + (mark === "comment" ? "evg-comment" : "evg-hollow"),
        },
      });
    }
    // #42 选中行：独立装饰，只挂行标竖条与滚条标记，不占底色——
    // 三态颜色是产品核心输出，不能被选中态淹掉。越界行照旧跳过，
    // 理由同上：Monaco 会把越界 range 收敛到最后一行。
    const selected = selectedLineRef.current;
    if (selected && selected.file === path && selected.line >= 1
      && selected.line <= lineCount) {
      decorations.push({
        range: new monaco.Range(selected.line, 1, selected.line, 1),
        options: {
          linesDecorationsClassName: "wb-selected-mark",
          overviewRuler: {
            color: SELECTED_RULER_COLOR[theme],
            position: monaco.editor.OverviewRulerLane.Center,
          },
        },
      });
    }
    if (!decorationsRef.current) {
      decorationsRef.current = editor.createDecorationsCollection(decorations);
    } else {
      decorationsRef.current.set(decorations);
    }
  }

  // 文件内容就绪后挂模型（编辑器可能还没建好，由上面的 pending 补挂）。
  useEffect(() => {
    if (!file) return;
    mountModel(file.path, revealRef.current?.line);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file]);

  useEffect(() => {
    if (openPath) applyDecorations(openPath);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lines, openPath, commentsList, selectedLine, theme]);

  // #42 选中行自动滚到：只在同一文件里补一次 reveal（跨文件的选择
  // 已由 openFile → mountModel 处理过）。只在看不见时才滚，不抢视窗。
  useEffect(() => {
    if (!selectedLine || selectedLine.file !== openPath) return;
    editorRef.current?.revealLineInCenterIfOutsideViewport(selectedLine.line);
  }, [selectedLine, openPath]);

  const reloadTree = async () => {
    try {
      const payload = await fetchTree();
      setTree(payload);
      // 只增不减：保留用户手动展开的目录，刷新评论计数徽章。
      setExpanded(current => new Set(
        [...current, ...defaultExpandedPaths(buildTree(payload.entries))]));
    } catch {
      // 计数刷新失败不打断评论流程，下次重开工作台会重新读取。
    }
  };

  const afterMutation = () => {
    setRefreshNonce(nonce => nonce + 1);
    void reloadTree();
  };

  const openComposer = (kind: CommentKind) => {
    if (!selection || !openPath) return;
    setComposer({kind, start: selection.start, end: selection.end});
    setBody("");
    setComposerError("");
  };

  const closeComposer = () => {
    setComposer(null);
    // Return focus to the toolbar action that opened the form. This is also
    // used by Escape and keeps keyboard users at the original code anchor.
    window.setTimeout(() => composerTriggerRef.current?.focus(), 0);
  };

  useEffect(() => {
    if (!composer) return;
    authorRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeComposer();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [composer]);

  const submitComment = async () => {
    if (!composer || !openPath) return;
    const errors = validateCommentText(author, body);
    if (errors.author || errors.body) {
      setComposerError(errors.author ?? errors.body ?? "评论内容无效");
      return;
    }
    setBusy(true);
    setComposerError("");
    try {
      const input: NewCommentInput = {
        author: author.trim(), body: body.trim(), kind: composer.kind,
        path: openPath, start_line: composer.start, end_line: composer.end,
        claim_id: boundClaimId ?? undefined,
        evidence_ids: boundEvidenceIds.length > 0
          ? [...boundEvidenceIds] : undefined,
      };
      await createComment(input);
      const revealLine = composer.start;
      closeComposer();
      afterMutation();
      // 提交后焦点回到锚定的代码行（6.12 无障碍要求）。
      mountModel(openPath, revealLine);
      editorRef.current?.focus();
    } catch (error) {
      setComposerError(
        error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const submitReply = async (parent: CommentRecord) => {
    const errors = validateCommentText(author, replyBody);
    if (errors.author || errors.body) {
      setThreadError(errors.author ?? errors.body ?? "回复内容无效");
      return;
    }
    setBusy(true);
    setThreadError("");
    try {
      await replyComment(parent.comment_id,
        {author: author.trim(), body: replyBody.trim(), kind: replyKind});
      setReplyFor(null);
      setReplyBody("");
      afterMutation();
      editorRef.current?.focus();
    } catch (error) {
      setThreadError(
        error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const changeStatus = async (record: CommentRecord,
                              status: StatusInput["status"]) => {
    setBusy(true);
    setThreadError("");
    try {
      await updateComment(record.comment_id,
        {revision: record.revision, status});
      afterMutation();
    } catch (error) {
      setThreadError(
        error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const submitReverificationPlan = async (comment: CommentRecord) => {
    const problem = validateChallengeInput(planReason, planExplanation);
    if (problem) {
      setPlanError(problem);
      return;
    }
    setBusy(true);
    setPlanError("");
    try {
      await requestReverification({comment_id: comment.comment_id,
        reason: planReason,
        explanation: planExplanation.trim() || undefined});
      setPlanFor(null);
      setPlanExplanation("");
      afterMutation();
    } catch (error) {
      setPlanError(
        error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const settleReverification = async (reverification: ReverificationRecord,
                                      action: "approve" | "reject") => {
    setBusy(true);
    setPlanError("");
    try {
      if (action === "approve") {
        await approveReverification(reverification.reverification_id);
      } else {
        await rejectReverification(reverification.reverification_id);
      }
      afterMutation();
    } catch (error) {
      setPlanError(
        error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  // 质疑线程专属：请求复验（闭合原因）→ 计划 → 人工确认复验 → 结局。
  // 评论永远不直接改机器结论；这里能做的只有"让实验再跑一次"。
  const renderReverification = (comment: CommentRecord) => {
    const record = reverificationForComment(
      reverifications?.reverifications ?? [], comment.comment_id);
    if (!record) {
      if (planFor !== comment.comment_id) {
        return <div className="wb-reverify">
          <button type="button" className="wb-reverify-open" disabled={busy}
            title="从这条质疑生成复验计划；确认后才会复验实验"
            onClick={() => {
              setPlanFor(comment.comment_id);
              setPlanReason(DEFAULT_CHALLENGE_REASON);
              setPlanExplanation("");
              setPlanError("");
            }}>请求复验</button>
        </div>;
      }
      return <form className="wb-reverify wb-reverify-form"
        onSubmit={event => {
          event.preventDefault();
          void submitReverificationPlan(comment);
        }}>
        <header>
          <b>生成复验计划</b>
          <button type="button" aria-label="取消复验计划"
            onClick={() => setPlanFor(null)}>×</button>
        </header>
        <label>质疑原因
          <select aria-label="质疑原因" value={planReason}
            onChange={event =>
              setPlanReason(event.target.value as ChallengeReason)}>
            {CHALLENGE_REASONS.map(item =>
              <option key={item.value} value={item.value}>
                {item.label}</option>)}
          </select>
        </label>
        <textarea aria-label="人工说明" rows={2} value={planExplanation}
          placeholder="补充说明（选「其他」时必填，最多 2000 字）"
          maxLength={2200}
          onChange={event => setPlanExplanation(event.target.value)} />
        <footer>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "生成中…" : "生成复验计划"}</button>
          <span>计划生成后需人工确认才会复验实验。</span>
        </footer>
        {planError &&
          <p className="wb-form-error" role="alert">{planError}</p>}
      </form>;
    }
    const planCount = record.plan?.replay_experiments?.length ?? 0;
    return <div className={"wb-reverify s-" + record.status}
      data-reverification-id={record.reverification_id}>
      <div className="wb-reverify-head">
        <i className={"wb-rvstatus s-" + record.status}>
          {revStatusLabel(record.status)}</i>
        {record.outcome &&
          <i className={"wb-rvoutcome " + outcomeClass(record.outcome)}>
            {outcomeLabel(record.outcome)}</i>}
        <code>{record.reverification_id}</code>
      </div>
      <p className="wb-reverify-reason">
        质疑原因：{reasonLabel(record.reason)}</p>
      {record.strategy !== undefined &&
        <p className="wb-reverify-strategy">
          执行策略：{strategyLabel(record.strategy)}</p>}
      {record.plan && <p className="wb-reverify-plan">
        计划复验 {planCount} 条实验
        {record.plan.comparison === "vector_diff_against_original_experiment"
          ? " · 逐测试状态向量对比" : ""}；确认后执行。</p>}
      {(() => {
        const proposal = testProposalView(record);
        if (!proposal) return null;
        return <details className="wb-reverify-patch">
          <summary>质疑附带的测试补丁{proposal.sha
            ? "（指纹 " + proposal.sha.slice(0, 12) + "…）" : ""}</summary>
          <pre>{proposal.patch}</pre>
        </details>;
      })()}
      {record.status === "planned" &&
        <div className="wb-reverify-actions">
          <button type="button" className="primary" disabled={busy}
            onClick={() => void settleReverification(record, "approve")}>
            {busy ? "复验中…" : "确认并复验"}</button>
          <button type="button" disabled={busy}
            onClick={() => void settleReverification(record, "reject")}>
            否决计划</button>
        </div>}
      {record.status === "settled" && record.outcome &&
        <p className="wb-reverify-outcome">
          {outcomeSummary(record)}
          {typeof record.claim_revision === "number"
            ? `（新 revision ${record.claim_revision}）` : ""}</p>}
      {(record.status === "settled" || record.status === "failed") &&
        (() => {
          const engine = outcomeReasonView(record.outcome_reason);
          if (!engine.label) return null;
          return <p className={"wb-reverify-engine-reason"
            + (engine.technical ? " technical" : "")}>
            {engine.technical
              ? <>实验引擎原始说明：<code>{engine.label}</code></>
              : engine.label}
          </p>;
        })()}
      {(() => {
        const deltaRows = evidenceDeltaRows(record);
        if (!deltaRows.length) return null;
        return <ul className="wb-reverify-delta">
          {deltaRows.map(row =>
            <li key={row.label}><b>{row.label}</b><span>{row.value}</span></li>)}
        </ul>;
      })()}
      {(observationsCount(record.original_observations) > 0
        || observationsCount(record.replay_observations) > 0) &&
        <details className="wb-reverify-obs">
          <summary>复验的原始记录（原实验
            {" " + observationsCount(record.original_observations) + " 条 · 复验观测 "
            + observationsCount(record.replay_observations) + " 条"}）</summary>
          <pre>{JSON.stringify({original_observations:
            record.original_observations ?? [], replay_observations:
            record.replay_observations ?? []}, null, 2)}</pre>
        </details>}
      {record.status === "settled" &&
        <p className="wb-reverify-answered">
          质疑{answeredLabel(record)}
          {typeof record.experiments_added === "number"
            && record.experiments_added > 0
            ? `，复验 ${record.experiments_added} 次` : ""}。
        </p>}
      {(record.open?.length ?? 0) > 0 &&
        <ul className="wb-reverify-open">
          {record.open!.map(item =>
            <li key={item}>{openItemLabel(item)}</li>)}
        </ul>}
      {record.status === "settled" && record.needs_human &&
        <p className="wb-reverify-outcome">需要人工重新审查。</p>}
      {record.status === "failed" &&
        <p className="wb-reverify-outcome">
          复验本身失败；不确定只能扣留，不能确认。</p>}
      {record.status === "rejected" &&
        <p className="wb-reverify-outcome">
          计划被否决：质疑保留，主张保持不变，不因质疑被改写。</p>}
    </div>;
  };

  const renderTree = (items: TreeNode[]): ReactNode[] =>
    items.map(node => node.kind === "directory"
      ? <li key={"d:" + node.path} className="wb-dir">
          <button type="button" className="wb-dir-toggle"
            aria-expanded={expanded.has(node.path)}
            onClick={() => setExpanded(current => {
              const next = new Set(current);
              if (next.has(node.path)) next.delete(node.path);
              else next.add(node.path);
              return next;
            })}>
            <span aria-hidden="true">{expanded.has(node.path) ? "▾" : "▸"}</span>
            {node.name}
            {(node.changed || node.evidenceCount > 0) &&
              <i className="wb-dir-mark">{node.changed ? "修改" : "证据"}</i>}
          </button>
          {expanded.has(node.path) && <ul>{renderTree(node.children)}</ul>}
        </li>
      : <li key={"f:" + node.path} className="wb-file">
          <button type="button"
            className={node.path === openPath ? "active" : ""}
            aria-current={node.path === openPath ? "true" : undefined}
            onClick={() => void openFile(node.path)}>
            <span aria-hidden="true">▤</span>
            {node.name}
            {node.changed && <i className="wb-flag changed">修改</i>}
            {node.evidenceCount > 0 &&
              <i className="wb-flag evidence">{node.evidenceCount}</i>}
            {node.commentCount > 0 &&
              <i className="wb-flag comment">{node.commentCount}</i>}
          </button>
        </li>);

  return <section className="workbench" data-readonly="true"
    aria-label="只读证据代码工作台">
    <header className="wb-head">
      <OfficialLockup />
      <div className="wb-head-copy"><h4>证据代码工作台</h4>
        <span>只读 · 绑定本次审查的源码快照 · 浏览器编辑不能写入工作区</span></div>
      <button type="button" className="wb-close" onClick={onClose}
        aria-label="关闭代码工作台">×</button>
    </header>
    <div className="wb-legend" aria-label="图例：每种标记的形状与含义">
      {PRIMARY_VERDICTS.map(verdict => {
        const spec = VERDICT_SPECS[verdict];
        return <span key={verdict} className={"wb-legend-item " + spec.className}>
          <b aria-hidden="true">{spec.glyph}</b>{spec.name}
        </span>;
      })}
      <details className="wb-legend-more"><summary>更多标记</summary><div>
        {VERDICT_ORDER.filter(verdict => !PRIMARY_VERDICTS.includes(verdict)).map(verdict => {
          const spec = VERDICT_SPECS[verdict];
          return <span key={verdict} className={"wb-legend-item " + spec.className}>
            <b aria-hidden="true">{spec.glyph}</b>{spec.name}
          </span>;
        })}
      </div></details>
    </div>
    <div className="wb-body">
      <nav className="wb-tree" aria-label="审查仓库文件树">
        {treeState?.phase === "loading" && <p className="wb-empty">正在读取文件树…</p>}
        {treeState?.phase === "error" && <div className="wb-error" role="alert">
          <strong>文件树不可用</strong><span>{treeState.message}</span></div>}
        {tree && <>
          {tree.truncated && <p className="wb-tree-truncated" role="note">
            文件树已截断：只显示前 20000 项，其余文件未列出。</p>}
          <label className="wb-tree-filter" htmlFor="wb-file-filter">筛选文件</label>
          <input id="wb-file-filter" className="wb-tree-filter-input"
            value={treeFilter} placeholder="按文件名或路径筛选"
            onChange={event => setTreeFilter(event.target.value)} />
          <ul>{renderTree(filteredNodes)}</ul>
        </>}
      </nav>
      <div className="wb-editor-col">
        <div className="wb-path" aria-live="polite">
          <span>{openPath || "（未打开文件）"}</span>
          {file && <small>完整文件 · {file.line_count} 行 · {fileLines.length} 行有证据标记</small>}
        </div>
        {fileState?.phase === "loading" &&
          <p className="wb-empty">正在读取文件…</p>}
        {fileState?.phase === "error" && <div className="wb-error" role="alert">
          <strong>文件读取被拒绝</strong><span>{fileState.message}</span></div>}
        {file && selection && !composer &&
          <div className="wb-toolbar" role="toolbar"
            aria-label="评论工具条：把评论锚定到选中行">
            <span className="wb-toolbar-anchor">评论锚定
              <code>{anchorPreview({path: openPath,
                start_line: selection.start, end_line: selection.end})}</code>
            </span>
            {COMMENT_TOOLBAR_KINDS.map(kind =>
              <button key={kind} type="button" data-kind={kind}
                onClick={event => {
                  composerTriggerRef.current = event.currentTarget;
                  openComposer(kind);
                }}>
                {kindLabel(kind)}
              </button>)}
          </div>}
        <div className="wb-editor-host" ref={hostRef}
          role="application" aria-label="只读证据代码视图"/>
      </div>
      {composer && <button type="button" className="wb-composer-scrim"
        aria-label="关闭评论编辑器" onClick={closeComposer} />}
      <aside className="wb-side" aria-label="本文件证据与评论">
        {composer && file &&
          <form className="wb-composer" onSubmit={event => {
            event.preventDefault();
            void submitComment();
          }}>
            <header>
              <b>{kindLabel(composer.kind)}</b>
              <span>锚定
                <code>{anchorPreview({path: openPath,
                  start_line: composer.start, end_line: composer.end})}</code>
              </span>
              <button type="button" className="wb-composer-close"
                aria-label="取消评论" onClick={closeComposer}>×</button>
            </header>
            {(boundClaimId || boundEvidenceIds.length > 0) &&
              <div className="wb-bindings">
                {boundClaimId && <span>机器主张 <code>{boundClaimId}</code></span>}
                {boundEvidenceIds.map(id => <span key={id}>证据 <code>{id}</code></span>)}
                <small>评论不改机器结论；质疑与请求证据进入注释包等待复验。</small>
              </div>}
            <input ref={authorRef} type="text" value={author} aria-label="评论署名"
              placeholder="署名（1-100 字）" maxLength={120}
              onChange={event => setAuthor(event.target.value)} />
            <textarea value={body} aria-label="评论内容" rows={3}
              placeholder="纯文本，最多 2000 字；不会发送给模型。" maxLength={2200}
              onChange={event => setBody(event.target.value)} />
            {composerError && <p className="wb-form-error" role="alert">{composerError}</p>}
            <footer>
              <button type="submit" className="primary" disabled={busy}>
                {busy ? "提交中…" : "提交评论"}</button>
              <span>评论只写入独立注释包，不重封机器证据包。</span>
            </footer>
          </form>}
        {selectedLine && <div className="wb-line-detail">
          <p className="wb-verdict">
            <b aria-hidden="true">{verdictSpec(selectedLine).glyph}</b>
            {verdictSpec(selectedLine).name}
          </p>
          <code>{selectedLine.file}:{selectedLine.line}</code>
          <span>{verdictSpec(selectedLine).description}</span>
          {unlabeledReasonLabel(selectedLine.reason) &&
            <small className="wb-reason">
              原因：{unlabeledReasonLabel(selectedLine.reason)}</small>}
          {selectedGrade &&
            <small className={"admissibility-badge tone-" + selectedGrade.tone}
              title={selectedGrade.detail}>{selectedGrade.label}</small>}
          <div className="wb-tests">
            {testsForEvidence(ledger, selectedLine.evidence_ids).map(testId =>
              <button key={testId} type="button" className="wb-test-link"
                title="跳到这条具名测试所在的文件"
                onClick={() => void openFile(testId.split("::")[0])}>
                {testId}
              </button>)}
          </div>
        </div>}
        <h5>本文件证据行</h5>
        {fileLines.length === 0 && <p className="wb-empty">
          这个文件没有逐行证据结论；从文件树选择带标记的文件查看。</p>}
        <ul className="wb-line-list">
          {fileLines.map(line => {
            const spec = verdictSpec(line);
            const reasonText = unlabeledReasonLabel(line.reason);
            return <li key={line.line + ":" + (line.evidence_ids?.[0] ?? "")}>
              <button type="button"
                className={selectedLine === line ? "active" : ""}
                onClick={() => {
                  setSelectedLine(line);
                  void openFile(line.file, line.line);
                }}>
                <b aria-hidden="true" className={spec.className}>{spec.glyph}</b>
                <span className="wb-name">{spec.name}</span>
                <code>{line.line}</code>
                <small>{reasonText ? reasonText + " · " : ""}{
                  line.text?.trim().slice(0, 60) || " "}</small>
              </button>
            </li>;
          })}
        </ul>
        <h5>本文件评论</h5>
        {commentsState?.phase === "loading" &&
          <p className="wb-empty">正在读取评论…</p>}
        {commentsState?.phase === "error" &&
          <div className="wb-error" role="alert">
            <strong>评论不可用</strong><span>{commentsState.message}</span>
          </div>}
        {commentsList && commentsState === null && <>
          <p className="wb-comment-counts">
            共 {commentsList.counts.total} 条 · 待处理 {commentsList.counts.open}
            · 已处理 {commentsList.counts.resolved}
            · 已过期 {commentsList.counts.outdated}
          </p>
          {threads.length === 0 && <p className="wb-empty">
            在代码上选中一行，用工具条添加第一条评论。</p>}
          <ul className="wb-threads">
            {threads.map(({root, replies}) =>
              <li key={root.comment_id} className="wb-thread"
                data-comment-id={root.comment_id}>
                <div className="wb-thread-head">
                  <i className={"wb-kind k-" + root.kind}>{kindLabel(root.kind)}</i>
                  <i className={"wb-status s-" + root.status}>
                    {statusLabel(root.status)}</i>
                  <b>{root.author}</b>
                  <button type="button" className="wb-anchor"
                    title="在编辑器中定位这条评论的锚点"
                    onClick={() =>
                      void openFile(root.anchor.path, root.anchor.start_line)}>
                    {anchorPreview(root.anchor)}
                  </button>
                </div>
                <p className="wb-thread-body">{root.body}</p>
                {(root.claim_id || (root.evidence_ids?.length ?? 0) > 0) &&
                  <div className="wb-bindings">
                    {root.claim_id &&
                      <span>机器主张 <code>{root.claim_id}</code></span>}
                    {(root.evidence_ids ?? []).map(id =>
                      <span key={id}>证据 <code>{id}</code></span>)}
                  </div>}
                {root.status === "outdated" &&
                  <p className="wb-outdated-note">
                    源码已变化，锚点保留原位置；重新锚定会生成新评论记录。</p>}
                {root.kind === "challenge" && root.claim_id &&
                  renderReverification(root)}
                {replies.map(reply =>
                  <div key={reply.comment_id} className="wb-reply">
                    <div className="wb-thread-head">
                      <i className={"wb-kind k-" + reply.kind}>
                        {kindLabel(reply.kind)}</i>
                      <i className={"wb-status s-" + reply.status}>
                        {statusLabel(reply.status)}</i>
                      <b>{reply.author}</b>
                    </div>
                    <p className="wb-thread-body">{reply.body}</p>
                  </div>)}
                <div className="wb-thread-actions">
                  {root.status !== "withdrawn" &&
                    <button type="button" disabled={busy}
                      onClick={() => {
                        setReplyFor(current =>
                          current === root.comment_id ? null : root.comment_id);
                        setReplyBody("");
                        setReplyKind("note");
                        setThreadError("");
                      }}>回复</button>}
                  {(root.status === "open" || root.status === "outdated") &&
                    <button type="button" disabled={busy}
                      onClick={() => void changeStatus(root, "resolved")}>
                      标记已处理</button>}
                  {root.status === "resolved" &&
                    <button type="button" disabled={busy}
                      onClick={() => void changeStatus(root, "open")}>
                      重新打开</button>}
                  {(root.status === "open" || root.status === "outdated") &&
                    <button type="button" disabled={busy}
                      onClick={() => void changeStatus(root, "withdrawn")}>
                      撤回</button>}
                </div>
                {replyFor === root.comment_id &&
                  <form className="wb-reply-form" onSubmit={event => {
                    event.preventDefault();
                    void submitReply(root);
                  }}>
                    <select aria-label="回复类型" value={replyKind}
                      onChange={event =>
                        setReplyKind(event.target.value as CommentKind)}>
                      {(Object.keys(COMMENT_KIND_LABELS) as CommentKind[])
                        .map(kind =>
                          <option key={kind} value={kind}>
                            {kindLabel(kind)}</option>)}
                    </select>
                    <input type="text" aria-label="评论署名" value={author}
                      placeholder="署名（1-100 字）" maxLength={120}
                      onChange={event => setAuthor(event.target.value)} />
                    <textarea aria-label="回复内容" value={replyBody} rows={2}
                      placeholder="纯文本回复，最多 2000 字。" maxLength={2200}
                      onChange={event => setReplyBody(event.target.value)} />
                    <footer>
                      <button type="submit" className="primary" disabled={busy}>
                        {busy ? "提交中…" : "提交回复"}</button>
                      <button type="button" onClick={() => setReplyFor(null)}>
                        取消</button>
                    </footer>
                  </form>}
              </li>)}
          </ul>
          {threadError &&
            <p className="wb-form-error" role="alert">{threadError}</p>}
        </>}
        <h5>复验记录</h5>
        {revState?.phase === "loading" &&
          <p className="wb-empty">正在读取复验记录…</p>}
        {revState?.phase === "error" &&
          <div className="wb-error" role="alert">
            <strong>复验记录不可用</strong><span>{revState.message}</span>
          </div>}
        {reverifications && revState === null && <>
          {reverifications.reverifications.length === 0
            ? <p className="wb-empty">
                对承重结论提出质疑后，这里会出现复验计划与结局。</p>
            : <ul className="wb-reverify-list">
                {reverifications.reverifications.map(item =>
                  <li key={item.reverification_id}
                    data-reverification-id={item.reverification_id}>
                    <i className={"wb-rvstatus s-" + item.status}>
                      {revStatusLabel(item.status)}</i>
                    {item.outcome &&
                      <i className={"wb-rvoutcome "
                        + outcomeClass(item.outcome)}>
                        {outcomeLabel(item.outcome)}</i>}
                    {item.status === "settled" &&
                      <i className={"wb-rvanswered "
                        + (item.answered ? "ok" : "warn")}>
                        {answeredLabel(item)}</i>}
                    <code>{item.claim_id}</code>
                    <small>{reasonLabel(item.reason)}</small>
                  </li>)}
              </ul>}
        </>}
      </aside>
    </div>
    <footer className="wb-timeline" aria-label="当前文件关联事件摘要">
      <div className="wb-related-head">
        <div><h6>当前文件关联事件 <small>{relatedTimeline.length}</small></h6>
          <p className="wb-lane-hint">只显示与当前文件或代码行相关的记录。</p></div>
        {onOpenTimeline && <button type="button" className="wb-timeline-link"
          onClick={onOpenTimeline}>回到完整时间线</button>}
      </div>
      <ul className="wb-related-list">
        {relatedTimeline.length === 0
          ? <li className="wb-lane-empty">当前文件暂无关联事件。</li>
          : relatedTimeline.slice(0, 12).map(item => {
            const target = item.ref;
            return <li key={item.id} data-tone={item.tone}>
              {target ? <button type="button" onClick={() => void openFile(target.path,
                target.line || undefined)}><strong>{item.title}</strong><span>{item.detail}</span></button>
                : <><strong>{item.title}</strong><span>{item.detail}</span></>}
            </li>;
          })}
        {relatedTimeline.length > 12 &&
          <li className="wb-lane-more" aria-label="还有更多关联事件">
            还有 {relatedTimeline.length - 12} 条 · <button type="button"
              onClick={onOpenTimeline}>展开全部</button>
          </li>}
      </ul>
    </footer>
  </section>;
}
