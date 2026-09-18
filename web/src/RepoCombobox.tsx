/* 可访问的仓库下拉。替换原生 <select>，为的是每个选项能有两行
   （中文用户名称 + 英文技术 slug），原生 <option> 做不到这件事。
   交互与状态推进的逻辑全部抽成本文件顶部的纯函数，便于在没有 DOM
   测试环境的情况下单测；组件只负责把这些纯函数接到 React 状态上。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactElement } from "react";
import "./repo-combobox.css";

export type RepoOption = { repo_id: string; display_name: string; technical_name?: string };

/** 列表为空时触发器上写死的话术；同时被组件与测试引用。 */
export const REPO_COMBOBOX_EMPTY = "暂无已授权仓库";
/** value 为空串（还没选）时的占位话术。 */
export const REPO_COMBOBOX_PLACEHOLDER = "请选择仓库";
/** 连打合并窗口：500ms 内的连续按键并成一个前缀。 */
export const TYPEAHEAD_WINDOW_MS = 500;

// ---------------------------------------------------------------------------
// 纯函数层：可单测，不碰 DOM
// ---------------------------------------------------------------------------

export type HighlightMove = "down" | "up" | "home" | "end";

/** 键盘意图。组件把它翻译成状态变更，测试只断言意图本身。 */
export type KeyIntent =
  | { kind: "none" }
  /** 收起状态下需要展开；move 为 null 表示「展开并高亮当前项」 */
  | { kind: "open"; move: HighlightMove | null }
  | { kind: "move"; move: HighlightMove }
  /** Enter / Space：选中高亮项并收起 */
  | { kind: "commit" }
  /** Escape：收起、不改值、焦点还给触发器 */
  | { kind: "dismiss" }
  /** Tab：收起，但不拦截焦点移动 */
  | { kind: "close" }
  /** 可打印字符：走首字母连打 */
  | { kind: "typeahead"; char: string };

/**
 * 把一次按键翻译成意图。
 * @param key        KeyboardEvent.key
 * @param expanded   当前列表是否展开
 * @param withModifier ctrl/meta/alt 是否按下（按下时一律不处理，让浏览器快捷键过去）
 */
export function comboKeyIntent(key: string, expanded: boolean, withModifier = false): KeyIntent {
  if (withModifier) return { kind: "none" };
  if (!expanded) {
    // 收起时方向键 / Enter / Space 都只负责展开并高亮当前项，不移动高亮。
    if (key === "ArrowDown" || key === "ArrowUp" || key === "Enter" || key === " ") {
      return { kind: "open", move: null };
    }
    if (key === "Home") return { kind: "open", move: "home" };
    if (key === "End") return { kind: "open", move: "end" };
    if (isTypeaheadChar(key)) return { kind: "typeahead", char: key };
    return { kind: "none" };
  }
  switch (key) {
    case "ArrowDown": return { kind: "move", move: "down" };
    case "ArrowUp": return { kind: "move", move: "up" };
    case "Home": return { kind: "move", move: "home" };
    case "End": return { kind: "move", move: "end" };
    case "Enter":
    case " ": return { kind: "commit" };
    case "Escape": return { kind: "dismiss" };
    case "Tab": return { kind: "close" };
    default:
      return isTypeaheadChar(key) ? { kind: "typeahead", char: key } : { kind: "none" };
  }
}

/** 可打印单字符（空格除外——展开时空格是「选中」）。 */
export function isTypeaheadChar(key: string): boolean {
  return Array.from(key).length === 1 && key !== " ";
}

/**
 * 推进高亮下标。上下方向环绕；count 为 0 时恒为 -1（没有可高亮项）。
 * current 传 -1 表示当前没有高亮，此时向下去首项、向上去末项。
 */
export function nextHighlight(current: number, count: number, move: HighlightMove): number {
  if (count <= 0) return -1;
  const at = current >= 0 && current < count ? current : -1;
  switch (move) {
    case "home": return 0;
    case "end": return count - 1;
    case "down": return at < 0 ? 0 : (at + 1) % count;
    case "up": return at < 0 ? count - 1 : (at - 1 + count) % count;
  }
}

/**
 * 连打前缀合并：距上次按键不超过 windowMs 就接在旧前缀后面，否则重新开始。
 * @param previous 上一次累计的前缀
 * @param lastAt   上一次按键的时间戳（ms）
 * @param char     本次按键字符
 * @param now      本次按键的时间戳（ms）
 */
export function typeaheadPrefix(
  previous: string, lastAt: number, char: string, now: number,
  windowMs: number = TYPEAHEAD_WINDOW_MS,
): string {
  const within = previous !== "" && now - lastAt <= windowMs;
  return within ? previous + char : char;
}

/**
 * 按 display_name 前缀（大小写不敏感）环形查找，从 startIndex 开始。
 * 只匹配 display_name，不匹配 technical_name。找不到返回 -1。
 */
export function typeaheadMatch(options: RepoOption[], prefix: string, startIndex = 0): number {
  if (options.length === 0 || prefix === "") return -1;
  const needle = prefix.toLowerCase();
  const from = startIndex >= 0 && startIndex < options.length ? startIndex : 0;
  for (let step = 0; step < options.length; step += 1) {
    const index = (from + step) % options.length;
    if (options[index].display_name.toLowerCase().startsWith(needle)) return index;
  }
  return -1;
}

export type TriggerView = {
  /** 第一行文字 */
  label: string;
  /** 第二行文字；没有就不要渲染第二行 */
  technical?: string;
  /** 列表为空：触发器要 disabled */
  empty: boolean;
  /** label 是占位/回退文案而非真正的仓库名 */
  placeholder: boolean;
};

/**
 * 触发器上显示什么。
 * - options 为空 → 「暂无已授权仓库」，empty=true
 * - value 命中 → 该项的两行
 * - value 不在 options 里 → 原样显示 value（绝不静默当成第一项）
 * - value 为空串 → 占位文案
 */
export function triggerView(value: string, options: RepoOption[]): TriggerView {
  if (options.length === 0) return { label: REPO_COMBOBOX_EMPTY, empty: true, placeholder: true };
  const hit = options.find(option => option.repo_id === value);
  if (hit) {
    return { label: hit.display_name, technical: hit.technical_name, empty: false, placeholder: false };
  }
  if (value === "") return { label: REPO_COMBOBOX_PLACEHOLDER, empty: false, placeholder: true };
  return { label: value, empty: false, placeholder: false };
}

/** 选项的 DOM id，aria-activedescendant 与 e2e 选择器都靠它。 */
export function repoOptionDomId(id: string, index: number): string {
  return `${id}-option-${index}`;
}

/** 列表容器的 DOM id，aria-controls 指向它。 */
export function repoListDomId(id: string): string {
  return `${id}-listbox`;
}

// ---------------------------------------------------------------------------
// 组件
// ---------------------------------------------------------------------------

export function RepoCombobox(props: {
  id: string;
  value: string;
  options: RepoOption[];
  disabled?: boolean;
  onChange: (repoId: string) => void;
}): ReactElement {
  const { id, value, options, disabled, onChange } = props;
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);
  const typeahead = useRef<{ prefix: string; at: number }>({ prefix: "", at: 0 });

  const view = useMemo(() => triggerView(value, options), [value, options]);
  const listId = repoListDomId(id);
  const isDisabled = Boolean(disabled) || view.empty;
  const selectedIndex = options.findIndex(option => option.repo_id === value);

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    typeahead.current = { prefix: "", at: 0 };
    if (refocus) triggerRef.current?.focus();
  }, []);

  const expand = useCallback((move: HighlightMove | null) => {
    const start = selectedIndex >= 0 ? selectedIndex : 0;
    setHighlight(move ? nextHighlight(-1, options.length, move) : (options.length ? start : -1));
    setOpen(true);
  }, [options.length, selectedIndex]);

  const commit = useCallback((index: number) => {
    const picked = options[index];
    if (picked) onChange(picked.repo_id);
    close(true);
  }, [options, onChange, close]);

  // 外部点击收起。
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent | MouseEvent) => {
      const node = event.target as Node | null;
      if (node && rootRef.current && !rootRef.current.contains(node)) close(false);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [open, close]);

  // 变成 disabled 或选项被清空时不能留着一个开着的列表。
  useEffect(() => { if (isDisabled && open) close(false); }, [isDisabled, open, close]);

  // 高亮项滚进可视区。scrollIntoView 默认是瞬时的，不违反 reduced-motion。
  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>('[data-highlighted="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [open, highlight]);

  const runTypeahead = useCallback((char: string) => {
    const now = Date.now();
    const prefix = typeaheadPrefix(typeahead.current.prefix, typeahead.current.at, char, now);
    typeahead.current = { prefix, at: now };
    if (options.length === 0) return;
    const base = open ? highlight : selectedIndex;
    // 前缀还在累积时从当前项本身开始找，这样 "de" 不会跳过已经高亮的 "demo"；
    // 新前缀的第一个字符则从下一项开始，让同字母的仓库能轮着走。
    const from = base < 0 ? 0
      : prefix.length > 1 ? base
      : (base + 1) % options.length;
    const found = typeaheadMatch(options, prefix, from);
    if (found < 0) return;
    setHighlight(found);
    setOpen(true);
  }, [open, highlight, selectedIndex, options]);

  const onKeyDown = useCallback((event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const withModifier = event.ctrlKey || event.metaKey || event.altKey;
    const intent = comboKeyIntent(event.key, open, withModifier);
    switch (intent.kind) {
      case "open":
        event.preventDefault();
        expand(intent.move);
        return;
      case "move":
        event.preventDefault();
        setHighlight(current => nextHighlight(current, options.length, intent.move));
        return;
      case "commit":
        event.preventDefault();
        if (highlight >= 0) commit(highlight); else close(true);
        return;
      case "dismiss":
        event.preventDefault();
        event.stopPropagation();
        close(true);
        return;
      case "close":
        // Tab 不拦截：收起后让焦点正常走到下一个控件。
        close(false);
        return;
      case "typeahead":
        event.preventDefault();
        runTypeahead(intent.char);
        return;
      case "none":
        return;
    }
  }, [open, options.length, highlight, expand, commit, close, runTypeahead]);

  const activeId = open && highlight >= 0 ? repoOptionDomId(id, highlight) : undefined;

  return (
    <div className="repo-combobox" ref={rootRef}>
      {/* 可读名 = 外部 <label id={`${id}-label`}> 的字 + 当前取值。<button> 虽然
          是 labelable 元素，但实现普遍拿内容当可读名，外面那条 <label> 不会自动
          接上，所以这里显式指过去。 */}
      <button
        type="button"
        id={id}
        ref={triggerRef}
        role="combobox"
        aria-labelledby={`${id}-label ${id}`}
        aria-expanded={open}
        aria-controls={listId}
        aria-haspopup="listbox"
        aria-activedescendant={activeId}
        disabled={isDisabled}
        className={`repo-combobox-trigger${view.placeholder ? " repo-combobox-placeholder" : ""}`}
        onClick={() => (open ? close(false) : expand(null))}
        onKeyDown={onKeyDown}
      >
        <span className="repo-combobox-lines">
          <span className="repo-combobox-name" title={view.label}>{view.label}</span>
          {view.technical
            ? <span className="repo-combobox-tech" title={view.technical}>{view.technical}</span>
            : null}
        </span>
        <span className="repo-combobox-caret" aria-hidden="true">▾</span>
      </button>
      <ul
        id={listId}
        ref={listRef}
        role="listbox"
        aria-labelledby={id}
        hidden={!open}
        className="repo-combobox-list"
      >
        {options.map((option, index) => (
          <li
            key={option.repo_id}
            id={repoOptionDomId(id, index)}
            role="option"
            aria-selected={option.repo_id === value}
            data-highlighted={index === highlight}
            className="repo-combobox-option"
            onMouseEnter={() => setHighlight(index)}
            onClick={() => commit(index)}
          >
            <span className="repo-combobox-check" aria-hidden="true">
              {option.repo_id === value ? "✓" : ""}
            </span>
            <span className="repo-combobox-lines">
              <span className="repo-combobox-name" title={option.display_name}>
                {option.display_name}
              </span>
              {option.technical_name
                ? <span className="repo-combobox-tech" title={option.technical_name}>
                    {option.technical_name}
                  </span>
                : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
