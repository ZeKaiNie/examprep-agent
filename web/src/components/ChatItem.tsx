// 一条对话条目：学生消息、讲解、题目、判分、提示、拒答、压缩结果……

import type { ChatItem } from "../chat";
import type { CiteTarget } from "../citations";
import type { FigureRef } from "../types";
import { Markdown } from "./Markdown";

const KIND_NAME: Record<string, string> = {
  teach: "讲解",
  reteach: "换个讲法",
  check: "检查一下",
  feedback: "讲评",
  hint: "提示",
  answer: "回答",
  wrapup: "本章小结",
};

const VERDICT: Record<string, { label: string; tone: string }> = {
  right: { label: "答对了", tone: "good" },
  partial: { label: "部分正确", tone: "warn" },
  wrong: { label: "答错了", tone: "bad" },
  unsure: { label: "无法自动判分", tone: "warn" },
  skip: { label: "已跳过", tone: "muted" },
};

const GRADER: Record<string, string> = { rule: "规则判分", llm: "AI 判分", self: "自评" };
const STATUS: Record<string, string> = { verified: "已验证", covered: "已讲完" };

interface Props {
  item: ChatItem;
  assetUrl: (path: string) => string;
  onCite: (target: CiteTarget) => void;
}

function Figures({ figures, assetUrl, caption }: { figures: FigureRef[]; assetUrl: (p: string) => string; caption: string }) {
  if (!figures.length) return null;
  return (
    <div className="figures">
      {figures.map((f) => (
        <figure key={f.path}>
          <img src={assetUrl(f.path)} alt={`${caption} ${f.file} p.${f.page}`} loading="lazy" />
          <figcaption>
            {caption} · {f.file} p.{f.page}
          </figcaption>
        </figure>
      ))}
    </div>
  );
}

export function ChatItemView({ item, assetUrl, onCite }: Props) {
  switch (item.kind) {
    case "student":
      return <div className="msg student">{item.text}</div>;
    case "tutor": {
      const v = item.verify;
      return (
        <div className={`msg tutor${item.streaming ? " streaming" : ""}`}>
          <div className="msg-head">{item.tutorKind ? KIND_NAME[item.tutorKind] : "正在生成…"}</div>
          {item.streaming ? (
            <div className="md plain">{item.text}</div>
          ) : (
            <Markdown text={item.text} citations={item.citations} onCite={onCite} />
          )}
          {(v || item.unknown.length > 0) && (
            <div className="msg-foot">
              {v &&
                (v.unsupported || v.partial
                  ? `核对：${v.supported} 句有依据，${v.partial} 句部分有依据，${v.unsupported} 句在资料里找不到依据（已标 🟡）`
                  : `核对：${v.supported} 句都有资料依据`)}
              {item.unknown.length > 0 && ` · 已删除 ${item.unknown.length} 个资料里不存在的引用`}
            </div>
          )}
        </div>
      );
    }
    case "figures":
      return <Figures figures={item.figures} assetUrl={assetUrl} caption="配图" />;
    case "question": {
      const q = item.ev;
      return (
        <div className="card question">
          <div className="card-head">
            第 {q.index}/{q.total} 题{q.points ? `（${q.points} 分）` : ""}
            <span className="src">
              出处：{q.source_file} p.{q.source_page}
            </span>
          </div>
          <div className="stem">{q.stem}</div>
          {q.options.length > 0 && (
            <ul className="options">
              {q.options.map((o) => (
                <li key={o}>{o}</li>
              ))}
            </ul>
          )}
          <Figures figures={q.figures} assetUrl={assetUrl} caption="题图" />
          {!q.has_answer && <div className="hint-line">资料里没有这道题的参考答案，判分仅供参考</div>}
        </div>
      );
    }
    case "graded": {
      const g = item.ev;
      const v = VERDICT[g.verdict] ?? { label: g.verdict, tone: "muted" };
      return (
        <div className={`card graded ${v.tone}`}>
          <div className="card-head">
            {v.label}
            <span className="src">{GRADER[g.grader] ?? g.grader}</span>
          </div>
          {g.reason && <div className="reason">{g.reason}</div>}
          {g.reference && (
            <div className="reference">
              <div className="label">参考答案{g.reference_source ? `（${g.reference_source}）` : ""}</div>
              <div className="ref-text">{g.reference}</div>
            </div>
          )}
          <Figures figures={g.answer_figures} assetUrl={assetUrl} caption="答案图" />
        </div>
      );
    }
    case "refused":
      return <div className="msg tutor refused">{item.text}</div>;
    case "notice":
      return <div className={`notice ${item.level}`}>{item.text}</div>;
    case "note":
      return <div className="note">已记为{item.noteType === "confusion" ? "困惑点" : item.noteType === "summary" ? "本章小结" : "笔记"}</div>;
    case "chapter_done":
      return (
        <div className="milestone">
          🏁 第 {item.chapter} 章完成：{STATUS[item.status] ?? item.status}
        </div>
      );
    case "course_done":
      return <div className="milestone">🎉 整门课学完了！</div>;
    case "compacted": {
      const c = item.ev;
      if (!c.ok) return null; // 失败时后端另有提示
      return (
        <details className="compacted">
          <summary>
            🗜 已{c.manual ? "" : "自动"}压缩上下文：约 {c.tokens_before} → {c.tokens_after} token（点开看保留下来的要点）
          </summary>
          <Markdown text={c.summary} />
        </details>
      );
    }
  }
}
