import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { Markdown } from "./components/Markdown";
import type { StudyEvent } from "./types";

function sse(events: StudyEvent[]): Response {
  const body = events.map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join("") + "event: end\ndata: {}\n\n";
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

const CITE = { label: "E1", evidence_id: "Ea", file: "课件/第1章_线性表.md", page: 1 };
const PAGE_TEXT = "线性表是有序序列。顺序表用连续的存储单元存放元素。";

/** 假的本机服务：按路径返回数据，记录每次请求。 */
function fakeServer(overrides: Record<string, () => Response> = {}) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const path = url.split("?")[0] ?? url;
    const key = `${method} ${path}`;
    const custom = overrides[key];
    if (custom) return custom();
    switch (key) {
      case "GET /api/health":
        return json({ version: "0.1.0", model_configured: true });
      case "GET /api/courses":
        return json([{ id: "c1", name: "数据结构", language: "zh", root: "D:/课/数据结构", updated_at: "2026-11-12" }]);
      case "GET /api/study/c1/state":
        return json({ open: true, busy: false, chapter: 1, state: "TEACH", awaiting: null, context_tokens: 3200, compact_limit: 60000, auto_compact: true });
      case "GET /api/study/c1/transcript":
        return json([{ seq: 1, role: "student", text: "上次的问题" }]);
      case "POST /api/study/c1/open":
        return sse([
          {
            type: "progress",
            course: "数据结构",
            current: 1,
            mode: "cram",
            days_left: 3,
            chapters: [{ n: 1, title: "线性表", status: "teaching", parts_done: 0, parts_total: 1, questions: 2, correct: 0 }],
            open_mistakes: 0,
          },
          { type: "suggestions", items: ["继续", "考考我"] },
        ]);
      case "POST /api/study/c1/message":
        return sse([
          { type: "state", state: "TEACH", chapter: 1 },
          { type: "tutor_delta", text: "顺序表用连续的存储单元存放元素" },
          {
            type: "tutor_message",
            kind: "teach",
            text: "顺序表用连续的存储单元存放元素（第1章_线性表.md p.1）。\n\n🟡 打个比方：像排队。",
            citations: [CITE],
            unknown: [],
          },
          { type: "verified", total: 1, supported: 1, partial: 0, unsupported: 0, text: "顺序表用连续的存储单元存放元素（第1章_线性表.md p.1）。\n\n🟡 打个比方：像排队。" },
          { type: "suggestions", items: ["继续"] },
        ]);
      case "GET /api/courses/c1/page":
        return json({
          file: "课件/第1章_线性表.md",
          page: 1,
          pages: 1,
          text: PAGE_TEXT,
          is_scan: false,
          width: null,
          height: null,
          renderable: false,
          evidence: [{ id: "Ea", chapter: 1, start: 9, end: 25, text: "顺序表用连续的存储单元存放元素。" }],
        });
      default:
        return json({ detail: `没有 ${key}` }, 404);
    }
  });
  vi.stubGlobal("fetch", handler);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("Markdown", () => {
  it("出处变成按钮，🟡 段落标记为补充", () => {
    const onCite = vi.fn();
    const { container } = render(
      <Markdown text={"资料内容（第1章_线性表.md p.1）。\n\n🟡 我的补充。"} citations={[CITE]} onCite={onCite} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "第1章_线性表.md p.1" }));
    expect(onCite).toHaveBeenCalledWith({ file: CITE.file, page: 1, evidence: ["Ea"], display: "第1章_线性表.md p.1" });
    expect(container.querySelector("p.extra")?.textContent).toBe("🟡 我的补充。");
  });

  it("核对员插在段落中间的 🟡 句子单独变淡，代码里的不动", () => {
    const { container } = render(<Markdown text={"有依据的句子。🟡 没依据的句子。后面又有依据。\n\n`a 🟡 b`"} />);
    expect([...container.querySelectorAll(".extra-inline")].map((e) => e.textContent)).toEqual(["🟡 没依据的句子。"]);
    expect(container.querySelector("p")?.textContent).toBe("有依据的句子。🟡 没依据的句子。后面又有依据。");
    expect(container.querySelector("code .extra-inline")).toBeNull();
  });
});

describe("App", () => {
  it("课程列表 → 学习：恢复对话、发消息、点引用打开原文并高亮", async () => {
    const calls = fakeServer();
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /数据结构/ }));

    expect(await screen.findByText("上次的问题")).toBeTruthy(); // 对话记录恢复
    await screen.findByRole("button", { name: "考考我" }); // 打开会话后的建议
    expect(screen.getByText("1. 线性表")).toBeTruthy(); // 进度面板
    expect(screen.getByText(/距考试 3 天/)).toBeTruthy();
    expect(screen.getByText(/上下文 3.2k \/ 60.0k token/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    const cite = await screen.findByRole("button", { name: "第1章_线性表.md p.1" });
    expect(screen.getByText("核对：1 句都有资料依据")).toBeTruthy();
    expect(calls.find((c) => c.url.endsWith("/message"))?.body).toEqual({ text: "继续" });

    fireEvent.click(cite);
    const mark = await screen.findByText("顺序表用连续的存储单元存放元素。", { selector: "mark" });
    expect(mark).toBeTruthy();
    expect(screen.getByRole("complementary", { name: "原文" }).textContent).toContain("课件/第1章_线性表.md");
    fireEvent.click(screen.getByRole("button", { name: "关闭阅读器" }));
    await waitFor(() => expect(screen.queryByRole("complementary", { name: "原文" })).toBeNull());
  });

  it("别处在学这门课时提示，而不是白屏", async () => {
    fakeServer({ "POST /api/study/c1/open": () => json({ detail: "这门课正在另一个窗口或网页里学习。" }, 423) });
    window.location.hash = "#/course/c1";
    render(<App />);
    expect(await screen.findByText("这门课正在另一个窗口或网页里学习。")).toBeTruthy();
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).disabled).toBe(true);
  });

  it("导入资料：显示进度和报告", async () => {
    const calls = fakeServer({
      "POST /api/courses/ingest": () =>
        new Response(
          [
            'event: ingest_started\ndata: {"type":"ingest_started","course_id":"c2","name":"新课","files":1,"new":1,"changed":0,"unchanged":0,"removed":0}',
            'event: file_done\ndata: {"type":"file_done","rel":"lec1.pdf","kind":"lecture","pages":3,"figures":1,"seconds":0.2}',
            `event: ingest_done\ndata: ${JSON.stringify({
              type: "ingest_done",
              report: {
                course_id: "c2", name: "新课", root: "D:/新课", language: "zh", files: { lecture: 1 },
                chapters: [{ n: 1, title: "引言", sources: ["lec1.pdf"], questions: 0 }],
                questions: 0, answered: 0, figures: 1, crops: 0, scan_pages: 0, evidence: 2, parsed: 1, reused: 0,
                removed: 0, failed: 0, warnings: [], injection: [{ file: "lec1.pdf", pages: [2] }], seconds: 0.3,
              },
            })}`,
            "event: end\ndata: {}",
            "",
          ].join("\n\n"),
        ),
    });
    render(<App />);
    fireEvent.change(await screen.findByPlaceholderText(/例如/), { target: { value: "D:/新课" } });
    fireEvent.click(screen.getByRole("button", { name: "导入" }));
    expect(await screen.findByText(/导入完成：新课/)).toBeTruthy();
    expect(screen.getByText(/✓ lec1.pdf（3 页，1 图）/)).toBeTruthy();
    expect(screen.getByText(/lec1.pdf 第 2 页有疑似对 AI 下指令的文字/)).toBeTruthy();
    expect(calls.filter((c) => c.url === "/api/courses").length).toBe(2); // 导入完刷新列表
  });
});
