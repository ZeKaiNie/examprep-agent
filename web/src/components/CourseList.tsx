// 首页：课程列表 + 导入资料。

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { api } from "../api";
import type { Course, IngestEvent, IngestReport } from "../types";

interface Props {
  onOpen: (cid: string) => void;
}

export function CourseList({ onOpen }: Props) {
  const [courses, setCourses] = useState<Course[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .courses()
      .then((c) => {
        setCourses(c);
        setError(null);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(load, [load]);

  return (
    <main className="home">
      <section>
        <h2>我的课程</h2>
        {error && <div className="notice error">{error}</div>}
        {courses === null && !error && <div className="meta">加载中…</div>}
        {courses?.length === 0 && <div className="meta">还没有课程。在下面导入一个资料目录。</div>}
        <ul className="course-list">
          {courses?.map((c) => (
            <li key={c.id}>
              <button type="button" onClick={() => onOpen(c.id)}>
                <span className="name">{c.name}</span>
                <span className="meta">
                  {c.language === "zh" ? "中文" : c.language === "en" ? "English" : "未知语言"} · {c.root}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
      <IngestPanel onDone={load} />
    </main>
  );
}

function IngestPanel({ onDone }: { onDone: () => void }) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [running, setRunning] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [report, setReport] = useState<IngestReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const onEvent = (ev: IngestEvent) => {
    if (ev.type === "ingest_started") {
      setLines([`开始导入「${ev.name}」：${ev.files} 个文件（新增 ${ev.new}，变化 ${ev.changed}，未变 ${ev.unchanged}）`]);
    } else if (ev.type === "file_done") {
      setLines((xs) => [...xs, `✓ ${ev.rel}（${ev.pages} 页，${ev.figures} 图）`]);
    } else if (ev.type === "file_failed") {
      setLines((xs) => [...xs, `✗ ${ev.rel}：${ev.error}`]);
    } else if (ev.type === "ingest_done") {
      setReport(ev.report);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!path.trim() || running) return;
    setRunning(true);
    setError(null);
    setReport(null);
    setLines([]);
    try {
      await api.ingest({ path: path.trim(), ...(name.trim() ? { name: name.trim() } : {}) }, onEvent);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="ingest">
      <h2>导入资料</h2>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          资料目录（本机路径）
          <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="例如 D:\课程\数据结构" />
        </label>
        <label>
          课程名（可选）
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="默认用目录名" />
        </label>
        <button type="submit" disabled={!path.trim() || running}>
          {running ? "导入中…" : "导入"}
        </button>
      </form>
      {error && <div className="notice error">{error}</div>}
      {lines.length > 0 && (
        <pre className="ingest-log">{lines.join("\n")}</pre>
      )}
      {report && (
        <div className="report">
          <div>
            导入完成：{report.name}（{report.seconds.toFixed(1)} 秒）· {report.chapters.length} 章 · {report.questions} 道题（
            {report.answered} 道有参考答案）· {report.figures} 张配图
          </div>
          {(report.warnings.length > 0 || report.injection.length > 0) && (
            <ul className="warnings">
              {report.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
              {report.injection.map((h) => (
                <li key={h.file}>
                  {h.file} 第 {h.pages.join("、")} 页有疑似对 AI 下指令的文字，讲解时会当作普通资料处理
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
