# P3b 本机服务端：任务

- [x] 1. 依赖：fastapi 0.162、uvicorn 0.54、alembic 1.20
- [x] 2. Alembic 迁移：0001 初始（autogenerate 自模型）、0002 选课租约；`Database.migrate()`；旧库 stamp；结构一致性测试（R6）
- [x] 3. `learner/lease.py` 租约 + 测试；`examprep study` 取租约（R5.2）
- [x] 4. `server/`：安全中间件、SSE、课程与资料接口、导入（R1–R3）
- [x] 5. `server/sessions.py`：会话管理、后台轮次、对话记录、取消、闲置回收；学习接口（R4、R5）
- [x] 6. `examprep serve` 命令；README
- [x] 7. 验收：覆盖率、ruff、pyright、CI；PR 合并；更新项目记忆

## 验收记录（2026-11-12）

| 项 | 结果 |
|---|---|
| 接口 | 16 个路径（健康、设置、课程列表/详情、导入 SSE、检索、页面原文与证据、PDF 渲染、图片、学习 open/message/compact/cancel/state/transcript），`/docs` 自动生成 |
| 安全 | Host 不是本机 → 403；POST 带外站 Origin → 403，带 `localhost:5173`（Vite）放行；文件参数 `../` 穿越 → 404；设置接口不含 Key |
| 互斥 | 同一门课处理中再发 → 409；另一个进程持有租约 → 网页 423、命令行退出码 3；租约过期可接手、被接手后原持有方续约失败；闲置 30 分钟回收并释放；关闭时全部释放 |
| 断开不打断 | 只读第一个事件就断开，这一轮仍跑完并写进对话记录 |
| 导入 | SSE 推送 ingest_started → file_done → ingest_done → end；目录不存在 400；正在导入 409；正在讲解 409；中途失败发 error 事件 |
| 迁移 | 新库升级到 0002 后与模型逐项一致（alembic compare_metadata 为空）；P3b 前 create_all 建的旧库自动 stamp 0001 再升级，数据保留 |
| 真实运行 | `examprep serve` 起服务后用 httpx 访问：健康、课程、检索、外站 Origin/Host 拦截、SSE 打开会话、/docs 均正常 |
| 质量 | 515 个测试通过，总覆盖率 96%，ruff、pyright 零错误 |

本机环境：Node 原来指向微信开发者工具自带的 v16（系统 PATH 排在前面）；winget 已装 Node v26.4.0，已把它的目录移到系统 PATH 最前面（原值备份在 `.tmp/machine_path_backup.txt`）。
