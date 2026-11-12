// 引用链接。后端已经把模型写的 [[E3]] 换成了“（第1章_线性表.md p.1；lec2.pdf p.3）”，
// 这里按 TutorMessage.citations 把它们变成可点的链接，点击后在阅读器里打开那一页并高亮被引用的证据块。

import type { CitationRef } from "./types";

export interface CiteTarget {
  file: string; // 课程里的相对路径
  page: number;
  evidence: string[]; // 这一页上被引用的证据 id
  display: string;
}

export const CITE_PREFIX = "#cite-";

export function basename(path: string): string {
  return path.split(/[\\/]/).pop() ?? path;
}

export function display(file: string, page: number): string {
  return `${basename(file)} p.${page}`;
}

export function citationTable(citations: readonly CitationRef[]): Map<string, CiteTarget> {
  const table = new Map<string, CiteTarget>();
  for (const c of citations) {
    const key = display(c.file, c.page);
    const t = table.get(key);
    if (t) {
      if (!t.evidence.includes(c.evidence_id)) t.evidence.push(c.evidence_id);
    } else {
      table.set(key, { file: c.file, page: c.page, evidence: [c.evidence_id], display: key });
    }
  }
  return table;
}

const RENDERED = /（([^（）\n]*?\bp\.\d+)）/g;

/** 把渲染好的出处换成 Markdown 链接 `[a.md p.1](#cite-0)`；对不上 citations 的括号原样保留。 */
export function linkify(text: string, citations: readonly CitationRef[]): { markdown: string; targets: CiteTarget[] } {
  const table = citationTable(citations);
  const targets: CiteTarget[] = [];
  const markdown = text.replace(RENDERED, (whole: string, inner: string) => {
    const parts = inner.split("；").map((p) => p.trim());
    const found = parts.map((p) => table.get(p));
    if (found.some((t) => t === undefined)) return whole;
    const links = found.map((t) => {
      const target = t as CiteTarget;
      targets.push(target);
      return `[${escapeLinkText(target.display)}](${CITE_PREFIX}${targets.length - 1})`;
    });
    return `（${links.join("；")}）`;
  });
  return { markdown, targets };
}

function escapeLinkText(s: string): string {
  return s.replace(/([[\]\\*_`])/g, "\\$1");
}
