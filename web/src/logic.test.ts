import { describe, expect, it } from "vitest";

import { initialChat, reduce, type ChatState } from "./chat";
import { linkify } from "./citations";
import { segments, unplaced } from "./highlight";
import { SSEParser } from "./sse";
import type { CitationRef, StudyEvent } from "./types";

// ---------------------------------------------------------------- SSE

const SAMPLE =
  'event: tutor_delta\ndata: {"type":"tutor_delta","text":"栈是后进先出"}\n\n' +
  ": ping\n\n" +
  'event: notice\ndata: {"type":"notice",\ndata: "level":"info","text":"多行 data"}\n\n' +
  "event: end\ndata: {}\n\n";

function chunks(bytes: Uint8Array, seed: number): Uint8Array[] {
  const out: Uint8Array[] = [];
  let x = seed;
  for (let i = 0; i < bytes.length; ) {
    x = (x * 1103515245 + 12345) % 2147483648;
    const n = 1 + (x % 7);
    out.push(bytes.slice(i, i + n));
    i += n;
  }
  return out;
}

describe("SSEParser", () => {
  it("任意切块（包括切在多字节汉字中间）结果都一样", () => {
    const bytes = new TextEncoder().encode(SAMPLE);
    for (let seed = 1; seed <= 25; seed++) {
      const p = new SSEParser();
      const got = chunks(bytes, seed).flatMap((c) => p.feed(c));
      got.push(...p.end());
      expect(got.map((m) => m.event)).toEqual(["tutor_delta", "notice", "end"]);
      expect(JSON.parse(got[0]!.data)).toEqual({ type: "tutor_delta", text: "栈是后进先出" });
      expect(JSON.parse(got[1]!.data).text).toBe("多行 data");
    }
  });

  it("流结束时没有空行收尾的最后一个事件也交出去；CRLF 也能解析", () => {
    const p = new SSEParser();
    expect(p.feed(new TextEncoder().encode("event: a\r\ndata: 1\r\n\r\nevent: b\ndata: 2"))).toEqual([
      { event: "a", data: "1" },
    ]);
    expect(p.end()).toEqual([{ event: "b", data: "2" }]);
  });
});

// ---------------------------------------------------------------- 引用

const CITES: CitationRef[] = [
  { label: "E1", evidence_id: "Ea", file: "课件/第1章_线性表.md", page: 1 },
  { label: "E2", evidence_id: "Eb", file: "课件/第1章_线性表.md", page: 1 },
  { label: "E3", evidence_id: "Ec", file: "slides/lec2.pdf", page: 3 },
];

describe("linkify", () => {
  it("把渲染好的出处换成链接，同页的证据合并", () => {
    const { markdown, targets } = linkify("顺序表连续存放（第1章_线性表.md p.1）。见（第1章_线性表.md p.1；lec2.pdf p.3）。", CITES);
    expect(markdown).toBe(
      "顺序表连续存放（[第1章\\_线性表.md p.1](#cite-0)）。见（[第1章\\_线性表.md p.1](#cite-1)；[lec2.pdf p.3](#cite-2)）。",
    );
    expect(targets[0]).toEqual({ file: "课件/第1章_线性表.md", page: 1, evidence: ["Ea", "Eb"], display: "第1章_线性表.md p.1" });
    expect(targets[2]?.evidence).toEqual(["Ec"]);
  });

  it("对不上 citations 的括号原样保留", () => {
    expect(linkify("（注：见 x.md p.9）和（普通括号）", CITES).markdown).toBe("（注：见 x.md p.9）和（普通括号）");
  });
});

// ---------------------------------------------------------------- 高亮

describe("segments", () => {
  const page = "第一段。第二段很重要。第三段。";
  const ev = [
    { id: "E1", chapter: 1, start: 4, end: 11, text: "第二段很重要。" },
    { id: "E2", chapter: 1, start: null, end: null, text: "第三段。" },
    { id: "E3", chapter: 1, start: null, end: null, text: "页面里没有这句" },
  ];

  it("按区间切分，没区间时按原文定位，重叠合并", () => {
    expect(segments(page, ev, new Set(["E1", "E2"]))).toEqual([
      { text: "第一段。", highlight: false },
      { text: "第二段很重要。第三段。", highlight: true },
    ]);
    expect(segments(page, ev, new Set())).toEqual([{ text: page, highlight: false }]);
    expect(unplaced(page, ev, new Set(["E1", "E3"])).map((e) => e.id)).toEqual(["E3"]);
  });
});

// ---------------------------------------------------------------- 事件归并

function run(events: StudyEvent[], start: ChatState = initialChat): ChatState {
  return events.reduce(reduce, start);
}

describe("reduce", () => {
  it("流式片段 → 正式讲解 → 核对后的文本", () => {
    const s = run([
      { type: "state", state: "TEACH", chapter: 1 },
      { type: "tutor_delta", text: "顺序表" },
      { type: "tutor_delta", text: "连续存放" },
    ]);
    expect(s.items).toEqual([
      { kind: "tutor", tutorKind: null, text: "顺序表连续存放", citations: [], unknown: [], streaming: true, verify: null },
    ]);
    const done = run(
      [
        { type: "tutor_message", kind: "teach", text: "顺序表连续存放（a.md p.1）", citations: CITES, unknown: ["E9"] },
        { type: "verified", total: 1, supported: 0, partial: 0, unsupported: 1, text: "🟡 顺序表连续存放（a.md p.1）" },
        { type: "suggestions", items: ["继续"] },
      ],
      s,
    );
    expect(done.items).toHaveLength(1);
    const item = done.items[0];
    expect(item?.kind === "tutor" && [item.streaming, item.tutorKind, item.text, item.verify?.unsupported]).toEqual([
      false,
      "teach",
      "🟡 顺序表连续存放（a.md p.1）",
      1,
    ]);
    expect(done.suggestions).toEqual(["继续"]);
    expect(done.state).toBe("TEACH");
  });

  it("对话记录恢复；重新打开时重发的待答题不重复显示", () => {
    const question: StudyEvent = {
      type: "question",
      qid: "q1",
      chapter: 1,
      qtype: "choice",
      stem: "题干",
      options: ["A. 1"],
      points: null,
      source_file: "卷.txt",
      source_page: 1,
      figures: [],
      has_answer: true,
      index: 1,
      total: 1,
    };
    const s = reduce(initialChat, {
      type: "restore",
      rows: [
        { seq: 1, role: "student", text: "考考我" },
        { seq: 2, role: "agent", event: question },
      ],
    });
    expect(s.items.map((x) => x.kind)).toEqual(["student", "question"]);
    expect(reduce(s, question).items).toHaveLength(2); // 打开会话时重发：不重复
    const graded: StudyEvent = {
      type: "graded",
      qid: "q1",
      verdict: "right",
      score: 1,
      grader: "rule",
      reason: null,
      error_type: null,
      reference: null,
      reference_source: null,
      answer_figures: [],
    };
    expect(run([graded, question], s).items.map((x) => x.kind)).toEqual(["student", "question", "graded", "question"]);
    expect(reduce(s, { type: "local_error", text: "断网了" }).items.at(-1)).toEqual({
      kind: "notice",
      level: "error",
      text: "断网了",
    });
  });

  it("进度只更新面板，学生发言清空建议", () => {
    const s = run([
      { type: "suggestions", items: ["继续"] },
      { type: "progress", course: "课", current: 2, mode: "cram", days_left: null, chapters: [], open_mistakes: 1 },
    ]);
    expect(s.items).toEqual([]);
    expect(s.chapter).toBe(2);
    expect(reduce(s, { type: "student", text: "继续" }).suggestions).toEqual([]);
  });
});
