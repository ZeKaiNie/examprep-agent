// 顶层：课程列表 ↔ 学习页。当前课程写在地址的 # 后面，刷新页面还在原处。

import { useEffect, useState } from "react";

import { api } from "./api";
import { CourseList } from "./components/CourseList";
import { StudyView } from "./components/StudyView";
import type { Health } from "./types";

function courseFromHash(): string | null {
  const m = /^#\/course\/([^/]+)$/.exec(window.location.hash);
  return m?.[1] ? decodeURIComponent(m[1]) : null;
}

export function App() {
  const [cid, setCid] = useState<string | null>(courseFromHash);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    const onHash = () => setCid(courseFromHash());
    window.addEventListener("hashchange", onHash);
    api.health().then(setHealth).catch(() => setHealth(null));
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (next: string | null) => {
    window.location.hash = next ? `#/course/${encodeURIComponent(next)}` : "";
    setCid(next);
  };

  return (
    <div className="app">
      <header className="topbar">
        <button type="button" className="brand" onClick={() => go(null)}>
          ExamPrep Agent <span>靠谱：只讲你资料里有的</span>
        </button>
        <div className="status">
          {health === null
            ? "连不上本机服务（先运行 examprep serve）"
            : health.model_configured
              ? `v${health.version} · 模型已配置`
              : "还没有配置模型：设置 DEEPSEEK_API_KEY 等环境变量后重启服务"}
        </div>
      </header>
      {cid ? <StudyView key={cid} cid={cid} onBack={() => go(null)} /> : <CourseList onOpen={go} />}
    </div>
  );
}
