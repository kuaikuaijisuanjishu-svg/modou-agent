// T12 教学视图：一次代码干预实验的教学叙述（纯展示，props 形状来自
// modou/teaching_view.py 的 narrate() 输出）。不做自动评分、不做代写检测。
import "../master-views.css";

export type TeachingOutcome =
  | "conclusive" | "not_covering" | "partially_covering" | "inconclusive_collection_failure";

export type TeachingNarration = {
  narrative_id?: string;
  took_away: {file: string; lines: number[]; excerpt: string};
  tests_changed: {id: string; before: string; after: string}[];
  related_tests: string[];
  outcome: TeachingOutcome;
  why_conclusive: string;
  conclusion_boundary: string;
};

export const OUTCOME_LABELS: Record<TeachingOutcome, string> = {
  conclusive: "可以下结论",
  not_covering: "不能下结论（测试未覆盖）",
  partially_covering: "部分覆盖",
  inconclusive_collection_failure: "实验无效（收集失败）",
};

export function teachingOutcomeLabel(outcome: TeachingOutcome): string {
  return OUTCOME_LABELS[outcome] ?? outcome;
}

export function TeachingView({narration}: {narration: TeachingNarration}) {
  const undetermined = narration.outcome !== "conclusive";
  return (
    <section className="master-teaching" data-outcome={narration.outcome}>
      <h3>教学视图：一次代码干预实验</h3>
      <div className="master-teaching__took">
        <strong>拿走了什么代码</strong>
        <code>
          {narration.took_away.file}
          {narration.took_away.lines.length > 0
            ? `:${narration.took_away.lines[0]}-${narration.took_away.lines[narration.took_away.lines.length - 1]}`
            : ""}
        </code>
        {narration.took_away.excerpt && <pre>{narration.took_away.excerpt}</pre>}
      </div>
      <div className="master-teaching__changes">
        <strong>哪些测试变了</strong>
        {narration.tests_changed.length === 0 && <p>相关测试状态没有变化。</p>}
        <ul>
          {narration.tests_changed.map((t) => (
            <li key={t.id}>
              <code>{t.id}</code>：{t.before} → {t.after}
            </li>
          ))}
        </ul>
      </div>
      <div className="master-teaching__why" data-undetermined={undetermined}>
        <strong>为何能／不能下结论</strong>
        <p data-outcome-label={teachingOutcomeLabel(narration.outcome)}>
          {teachingOutcomeLabel(narration.outcome)}：{narration.why_conclusive}
        </p>
        {undetermined && <p className="master-teaching__warn">本例不能作为测试证据结论。</p>}
      </div>
      <footer className="master-teaching__boundary">{narration.conclusion_boundary}</footer>
    </section>
  );
}

export default TeachingView;
