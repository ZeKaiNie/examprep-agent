// 原文阅读器：打开引用的那一页，高亮被引用的证据块并滚动过去；PDF 可以看页面图像。

import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { CiteTarget } from "../citations";
import { segments, unplaced } from "../highlight";
import type { PageData } from "../types";

interface Props {
  cid: string;
  target: CiteTarget;
  onClose: () => void;
}

export function Reader({ cid, target, onClose }: Props) {
  const [page, setPage] = useState(target.page);
  const [data, setData] = useState<PageData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"text" | "image">("text");
  const mark = useRef<HTMLElement | null>(null);

  useEffect(() => {
    setPage(target.page);
  }, [target]);

  useEffect(() => {
    let alive = true;
    setError(null);
    api
      .page(cid, target.file, page)
      .then((d) => alive && setData(d))
      .catch((e: unknown) => alive && (setData(null), setError(e instanceof Error ? e.message : String(e))));
    return () => {
      alive = false;
    };
  }, [cid, target.file, page]);

  const ids = new Set(page === target.page ? target.evidence : []);
  useEffect(() => {
    mark.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
  }, [data, mode]);

  const segs = data ? segments(data.text, data.evidence, ids) : [];
  const missing = data ? unplaced(data.text, data.evidence, ids) : [];
  let firstMark = true;

  return (
    <aside className="reader" aria-label="原文">
      <header>
        <div className="title" title={target.file}>
          {target.file}
        </div>
        <div className="tools">
          <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="上一页">
            ‹
          </button>
          <span>
            第 {page}
            {data ? ` / ${data.pages}` : ""} 页
          </span>
          <button
            type="button"
            disabled={!data || page >= data.pages}
            onClick={() => setPage(page + 1)}
            aria-label="下一页"
          >
            ›
          </button>
          {data?.renderable && (
            <button type="button" onClick={() => setMode(mode === "text" ? "image" : "text")}>
              {mode === "text" ? "页面图像" : "文字"}
            </button>
          )}
          <button type="button" onClick={onClose} aria-label="关闭阅读器">
            ✕
          </button>
        </div>
      </header>
      <div className="reader-body">
        {error && <div className="notice error">{error}</div>}
        {data && mode === "image" && (
          <img className="page-image" src={api.renderUrl(cid, data.file, data.page)} alt={`${data.file} 第 ${data.page} 页`} />
        )}
        {data && mode === "text" && (
          <>
            {data.is_scan && <div className="notice warning">这一页是扫描件，文字可能不全，可以切到“页面图像”看原图。</div>}
            <pre className="page-text">
              {segs.map((s, i) => {
                if (!s.highlight) return <span key={i}>{s.text}</span>;
                const ref = firstMark ? (el: HTMLElement | null) => void (mark.current = el) : undefined;
                firstMark = false;
                return (
                  <mark key={i} ref={ref}>
                    {s.text}
                  </mark>
                );
              })}
            </pre>
            {missing.length > 0 && (
              <div className="unplaced">
                <div className="label">被引用的片段（在页面文字里没找到对应位置）</div>
                {missing.map((e) => (
                  <blockquote key={e.id}>{e.text}</blockquote>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  );
}
