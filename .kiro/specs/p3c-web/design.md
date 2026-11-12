# P3c Web 界面：设计

## 结构

```
web/
  index.html  vite.config.ts  tsconfig.json  package.json
  src/
    main.tsx  App.tsx  styles.css
    types.ts        后端事件与接口数据的类型（对应 study/events.py、ingest/events.py）
    api.ts          fetch 封装；postSSE() 用 ReadableStream 读 POST 的事件流
    sse.ts          SSE 增量解析（跨块缓冲）
    chat.ts         事件归并：StudyEvent[] → 对话条目；对话记录恢复用同一个 reducer
    citations.ts    “（a.md p.1；b.pdf p.3）”→ 可点的链接；按 TutorMessage.citations 找到文件与证据 id
    highlight.ts    页面文本按证据区间切成普通段和高亮段
    components/
      CourseList.tsx  IngestPanel.tsx  StudyView.tsx  ChatItem.tsx  Markdown.tsx
      Reader.tsx  ProgressPanel.tsx
```

## 数据流

- 打开课程：`POST /api/study/{cid}/open`（进度、待答题、建议）+ `GET /transcript`（恢复对话）+ `GET /api/courses/{cid}`（章节）。
- 发消息：先在本地加一条学生消息，再读 `POST /message` 的流，每个事件交给 reducer；结束后 `GET /state` 刷新上下文用量。
- reducer 规则：`tutor_delta` 追加到“正在生成”条目；`tutor_message` 把它换成正式条目（带 citations）；`verified` 更新最近一条讲解的文本和核对结果；`progress` 只更新左侧面板；`suggestions` 只更新建议按钮；`state` 不显示；其余事件各自成一个条目。
- 引用：后端已经把 `[[E3]]` 渲染成“（文件名 p.N）”。前端按 citations 建 `basename(file) p.page → {file, page, evidence_ids}` 的表，把匹配的括号换成 Markdown 链接 `cite:序号`，自定义链接组件点击后打开阅读器。
- 阅读器：`GET /page?file&page` 取文字和证据区间（相对整页文字的字符位置），高亮被引用的块；PDF 用 `/render` 的 PNG。

## 开发与构建

- `cd web; npm install; npm run dev`（另开终端 `uv run examprep serve`），浏览器开 `http://localhost:5173`。
- `npm run build` → `packages/app/src/examprep/web/`（gitignore），然后 `examprep serve` 在 8765 端口直接提供界面。
