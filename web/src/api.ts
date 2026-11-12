// 后端接口封装。所有路径都是相对的：开发时由 Vite 代理到 examprep serve，构建后与界面同源。

import { SSEParser } from "./sse";
import type {
  Course,
  CourseDetail,
  Health,
  IngestEvent,
  PageData,
  StudyEvent,
  StudyState,
  TranscriptRow,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function detail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return "请求参数不对";
  } catch {
    // 不是 JSON
  }
  return `请求失败（HTTP ${res.status}）`;
}

async function getJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return (await res.json()) as T;
}

/** 读 POST 返回的事件流；每个事件回调一次。`end` 事件之前流断开也算正常结束。 */
export async function postSSE<T>(
  url: string,
  body: unknown,
  onEvent: (ev: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  if (!res.body) return;
  const reader = res.body.getReader();
  const parser = new SSEParser();
  const handle = (messages: ReturnType<SSEParser["feed"]>): void => {
    for (const m of messages) {
      if (m.event === "end") continue;
      const data = JSON.parse(m.data) as unknown;
      if (m.event === "error") {
        throw new ApiError(500, (data as { detail?: string }).detail ?? "处理时出错");
      }
      onEvent(data as T);
    }
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    handle(parser.feed(value));
  }
  handle(parser.end());
}

export const api = {
  health: (): Promise<Health> => getJSON("/api/health"),
  courses: (): Promise<Course[]> => getJSON("/api/courses"),
  course: (cid: string): Promise<CourseDetail> => getJSON(`/api/courses/${encodeURIComponent(cid)}`),
  page: (cid: string, file: string, page: number): Promise<PageData> =>
    getJSON(`/api/courses/${encodeURIComponent(cid)}/page?${new URLSearchParams({ file, page: String(page) })}`),
  renderUrl: (cid: string, file: string, page: number, scale = 1.5): string =>
    `/api/courses/${encodeURIComponent(cid)}/render?${new URLSearchParams({ file, page: String(page), scale: String(scale) })}`,
  assetUrl: (cid: string, path: string): string =>
    `/api/courses/${encodeURIComponent(cid)}/assets/${path.split("/").map(encodeURIComponent).join("/")}`,
  ingest: (body: { path: string; name?: string }, onEvent: (ev: IngestEvent) => void): Promise<void> =>
    postSSE("/api/courses/ingest", body, onEvent),
  open: (cid: string, onEvent: (ev: StudyEvent) => void): Promise<void> =>
    postSSE(`/api/study/${encodeURIComponent(cid)}/open`, undefined, onEvent),
  message: (cid: string, text: string, onEvent: (ev: StudyEvent) => void): Promise<void> =>
    postSSE(`/api/study/${encodeURIComponent(cid)}/message`, { text }, onEvent),
  compact: (cid: string, onEvent: (ev: StudyEvent) => void): Promise<void> =>
    postSSE(`/api/study/${encodeURIComponent(cid)}/compact`, undefined, onEvent),
  cancel: async (cid: string): Promise<boolean> => {
    const res = await fetch(`/api/study/${encodeURIComponent(cid)}/cancel`, { method: "POST" });
    if (!res.ok) throw new ApiError(res.status, await detail(res));
    return ((await res.json()) as { cancelled: boolean }).cancelled;
  },
  state: (cid: string): Promise<StudyState> => getJSON(`/api/study/${encodeURIComponent(cid)}/state`),
  transcript: (cid: string, limit = 300): Promise<TranscriptRow[]> =>
    getJSON(`/api/study/${encodeURIComponent(cid)}/transcript?limit=${limit}`),
};
