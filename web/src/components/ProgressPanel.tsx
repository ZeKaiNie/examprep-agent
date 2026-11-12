// 左侧进度面板：章节状态、错题、模式、上下文用量与压缩按钮。

import type { StudyEventOf, StudyState } from "../types";

const STATUS: Record<string, string> = { todo: "未开始", teaching: "学习中", covered: "已讲完", verified: "已验证" };

interface Props {
  progress: StudyEventOf<"progress"> | null;
  state: StudyState | null;
  busy: boolean;
  onCompact: () => void;
}

function k(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function ProgressPanel({ progress, state, busy, onCompact }: Props) {
  const used = state?.context_tokens ?? 0;
  const limit = state?.compact_limit ?? 0;
  return (
    <nav className="progress" aria-label="学习进度">
      {progress ? (
        <>
          <div className="course-name">{progress.course}</div>
          <div className="meta">
            {progress.mode === "cram" ? "突击模式" : "长期模式"}
            {progress.days_left !== null && ` · 距考试 ${progress.days_left} 天`}
            {` · 待回炉错题 ${progress.open_mistakes} 道`}
          </div>
          <ol className="chapters">
            {progress.chapters.map((c) => (
              <li key={c.n} className={c.n === progress.current ? "current" : undefined}>
                <div className="ch-title">
                  {c.n}. {c.title}
                </div>
                <div className="ch-meta">
                  <span className={`status ${c.status}`}>{STATUS[c.status] ?? c.status}</span>
                  <span>
                    段 {c.parts_done}/{c.parts_total}
                  </span>
                  <span>
                    题 {c.correct}/{c.questions}
                  </span>
                </div>
              </li>
            ))}
          </ol>
        </>
      ) : (
        <div className="meta">加载中…</div>
      )}
      {state?.open && limit > 0 && (
        <div className="context">
          <div className="label">
            上下文 {k(used)} / {k(limit)} token
            {state.auto_compact ? "（到上限自动压缩）" : "（自动压缩已关闭）"}
          </div>
          <div className="bar" role="progressbar" aria-valuenow={used} aria-valuemax={limit}>
            <div style={{ width: `${Math.min(100, (used / limit) * 100)}%` }} />
          </div>
          <button type="button" disabled={busy} onClick={onCompact}>
            压缩上下文
          </button>
        </div>
      )}
    </nav>
  );
}
