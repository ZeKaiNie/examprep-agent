// 事件归并：后端的 StudyEvent 流 → 界面上的对话条目。实时流和对话记录恢复走同一个 reducer。

import type { CitationRef, FigureRef, State, StudyEvent, StudyEventOf, TranscriptRow, TutorKind } from "./types";

export interface VerifyStats {
  total: number;
  supported: number;
  partial: number;
  unsupported: number;
}

export type ChatItem =
  | { kind: "student"; text: string }
  | {
      kind: "tutor";
      tutorKind: TutorKind | null; // 流式生成中还不知道是哪种
      text: string;
      citations: CitationRef[];
      unknown: string[];
      streaming: boolean;
      verify: VerifyStats | null;
    }
  | { kind: "figures"; figures: FigureRef[] }
  | { kind: "question"; ev: StudyEventOf<"question"> }
  | { kind: "graded"; ev: StudyEventOf<"graded"> }
  | { kind: "refused"; text: string }
  | { kind: "notice"; level: "info" | "warning" | "error"; text: string }
  | { kind: "note"; noteType: string; chapter: number | null }
  | { kind: "chapter_done"; chapter: number; status: string }
  | { kind: "course_done" }
  | { kind: "compacted"; ev: StudyEventOf<"compacted"> };

export interface ChatState {
  items: ChatItem[];
  suggestions: string[];
  progress: StudyEventOf<"progress"> | null;
  state: State | null;
  chapter: number | null;
}

export type ChatAction =
  | StudyEvent
  | { type: "student"; text: string }
  | { type: "restore"; rows: TranscriptRow[] }
  | { type: "local_error"; text: string };

export const initialChat: ChatState = { items: [], suggestions: [], progress: null, state: null, chapter: null };

export function reduce(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "student":
      return { ...state, items: [...state.items, { kind: "student", text: action.text }], suggestions: [] };
    case "restore": {
      let s: ChatState = { ...state, items: [] };
      for (const row of action.rows) {
        s = row.role === "student" ? reduce(s, { type: "student", text: row.text }) : reduce(s, row.event);
      }
      return s;
    }
    case "local_error":
      return push(state, { kind: "notice", level: "error", text: action.text });
    case "tutor_delta": {
      const last = state.items.at(-1);
      if (last?.kind === "tutor" && last.streaming) {
        return replaceLast(state, { ...last, text: last.text + action.text });
      }
      return push(state, {
        kind: "tutor",
        tutorKind: null,
        text: action.text,
        citations: [],
        unknown: [],
        streaming: true,
        verify: null,
      });
    }
    case "tutor_message": {
      const item: ChatItem = {
        kind: "tutor",
        tutorKind: action.kind,
        text: action.text,
        citations: action.citations,
        unknown: action.unknown,
        streaming: false,
        verify: null,
      };
      const last = state.items.at(-1);
      return last?.kind === "tutor" && last.streaming ? replaceLast(state, item) : push(state, item);
    }
    case "verified": {
      const i = findLastIndex(state.items, (x) => x.kind === "tutor" && !x.streaming);
      const target = state.items[i];
      if (target?.kind !== "tutor") return state;
      const { total, supported, partial, unsupported } = action;
      const items = state.items.slice();
      items[i] = { ...target, text: action.text, verify: { total, supported, partial, unsupported } };
      return { ...state, items };
    }
    case "progress":
      return { ...state, progress: action, chapter: action.current };
    case "suggestions":
      return { ...state, suggestions: action.items };
    case "state":
      return { ...state, state: action.state, chapter: action.chapter };
    case "figures":
      return push(state, { kind: "figures", figures: action.figures });
    case "question":
      // 重新打开会话时，后端会把还没作答的题再发一次；对话记录里已经有了就不重复显示
      return isPending(state.items, action.qid) ? state : push(state, { kind: "question", ev: action });
    case "graded":
      return push(state, { kind: "graded", ev: action });
    case "refused":
      return push(state, { kind: "refused", text: action.text });
    case "notice":
      return push(state, { kind: "notice", level: action.level, text: action.text });
    case "note":
      return push(state, { kind: "note", noteType: action.note_type, chapter: action.chapter });
    case "chapter_done":
      return push(state, { kind: "chapter_done", chapter: action.chapter, status: action.status });
    case "course_done":
      return push(state, { kind: "course_done" });
    case "compacted":
      return push(state, { kind: "compacted", ev: action });
  }
}

function push(state: ChatState, item: ChatItem): ChatState {
  return { ...state, items: [...state.items, item] };
}

function replaceLast(state: ChatState, item: ChatItem): ChatState {
  return { ...state, items: [...state.items.slice(0, -1), item] };
}

function findLastIndex<T>(xs: readonly T[], pred: (x: T) => boolean): number {
  for (let i = xs.length - 1; i >= 0; i--) {
    const x = xs[i];
    if (x !== undefined && pred(x)) return i;
  }
  return -1;
}

function isPending(items: readonly ChatItem[], qid: string): boolean {
  const q = findLastIndex(items, (x) => x.kind === "question");
  const question = items[q];
  if (question?.kind !== "question" || question.ev.qid !== qid) return false;
  return !items.slice(q + 1).some((x) => x.kind === "graded" && x.ev.qid === qid);
}
