# P3b 本机服务端：设计

## 模块

```
examprep/
  store/migrations/        Alembic：env.py + versions/（0001 初始、0002 选课租约）
  store/db.py              Database.migrate()：升级到 head；无 alembic_version 的旧库先 stamp 0001
  learner/lease.py         课程学习租约：acquire / renew / release（原子 UPDATE … WHERE 空闲或过期或自己）
  server/
    app.py                 create_app(data_dir, gateway=…)：路由、生命周期、静态前端
    security.py            Host / Origin 检查中间件；current_user 依赖（鉴权占位）
    sse.py                 Pydantic 事件 → text/event-stream
    sessions.py            SessionManager：每门课一个 StudySession + 一把 asyncio.Lock + 租约续约 + 闲置回收；
                           一轮在后台任务里跑，事件写入对话记录并推给订阅者
    files.py               路径校验、页面渲染
  cli.py                   examprep serve；examprep study 取租约
```

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 版本、是否已配置模型 |
| GET | `/api/settings` | 各角色模型、思考强度、压缩策略（不含 Key） |
| GET | `/api/courses` | 课程列表 |
| POST | `/api/courses/ingest` | `{path, name?, lang?}` → SSE：IngestEvent… |
| GET | `/api/courses/{cid}` | 课程详情：章节、资料文件、题数、配图数 |
| GET | `/api/courses/{cid}/search?q=&k=&chapter=` | 检索 |
| GET | `/api/courses/{cid}/page?file=&page=` | 页面文本 + 本页证据块（id、字符区间） |
| GET | `/api/courses/{cid}/render?file=&page=&scale=` | PDF 页面 PNG |
| GET | `/api/courses/{cid}/assets/{path}` | 配图、题目裁图 |
| POST | `/api/study/{cid}/open` | SSE：开场事件 |
| POST | `/api/study/{cid}/message` | `{text}` → SSE：本轮 StudyEvent |
| POST | `/api/study/{cid}/compact` | SSE：压缩事件 |
| POST | `/api/study/{cid}/cancel` | 取消正在进行的一轮 |
| GET | `/api/study/{cid}/state` | 当前章、状态、等待什么、上下文用量、压缩阈值、是否忙 |
| GET | `/api/study/{cid}/transcript?limit=` | 最近的对话记录 |

错误：400 参数错误、403 Host/Origin 不允许、404 课程或文件不存在、409 正在处理/正在导入、423 被其他进程占用、503 没配置模型。错误体 `{"detail": "中文说明"}`。

## SSE

每个事件一条：`event: <type>`、`data: <json>`；结束时发 `event: end`。一轮在后台任务里跑，事件进 `asyncio.Queue`，HTTP 响应只是订阅者；浏览器断开时订阅取消，后台任务继续跑完并落库。

## 对话记录

会话事件存储里加 `ui` 类型：`{"role": "student", "text": …}` 或 `{"role": "agent", "event": {…}}`（不含 TutorDelta）。`restore_segment` 只看 `segment` / `evidence` / `message`，不受影响。

## 租约

`enrollment.lock_owner`（进程 id，uuid）+ `lock_until`。

- acquire：`UPDATE enrollment SET lock_owner=:me, lock_until=:t WHERE id=:id AND (lock_owner IS NULL OR lock_owner=:me OR lock_until < :now)`，影响 1 行即成功。SQLite 单写者，语句本身原子。
- 有效期 2 分钟，持有方每 30 秒续约；release 只清自己的。
- 命令行：开学前取，退出时放；服务端：打开会话时取，闲置回收和关闭时放。
- `save_enrollment` 不写这两列，不会互相覆盖。

## 导入与学习

导入某门课前：这门课有一轮正在处理 → 409；否则关闭内存里的会话（释放租约），导入完成后下次打开重新加载资料。导入本身的并发仍由 P2 的任务租约保证（IngestBusyError → 409）。
