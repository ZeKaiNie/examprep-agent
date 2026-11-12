// 阅读器高亮：把一页文字按被引用证据块的字符区间切成普通段和高亮段。

import type { PageEvidence } from "./types";

export interface Segment {
  text: string;
  highlight: boolean;
}

/** 证据块在页文字里的区间；后端没给区间时按原文查找，找不到返回 null。 */
export function spanOf(page: string, ev: PageEvidence): [number, number] | null {
  if (ev.start !== null && ev.end !== null && ev.start >= 0 && ev.end <= page.length && ev.start < ev.end) {
    return [ev.start, ev.end];
  }
  const i = page.indexOf(ev.text);
  return i >= 0 ? [i, i + ev.text.length] : null;
}

export function segments(page: string, evidence: readonly PageEvidence[], ids: ReadonlySet<string>): Segment[] {
  const spans = evidence
    .filter((e) => ids.has(e.id))
    .map((e) => spanOf(page, e))
    .filter((s): s is [number, number] => s !== null)
    .sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [s, e] of spans) {
    const last = merged.at(-1);
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else merged.push([s, e]);
  }
  const out: Segment[] = [];
  let pos = 0;
  for (const [s, e] of merged) {
    if (s > pos) out.push({ text: page.slice(pos, s), highlight: false });
    out.push({ text: page.slice(s, e), highlight: true });
    pos = e;
  }
  if (pos < page.length) out.push({ text: page.slice(pos), highlight: false });
  return out;
}

/** 被引用、但在页文字里定位不到的证据块（阅读器单独列出来）。 */
export function unplaced(page: string, evidence: readonly PageEvidence[], ids: ReadonlySet<string>): PageEvidence[] {
  return evidence.filter((e) => ids.has(e.id) && spanOf(page, e) === null);
}
