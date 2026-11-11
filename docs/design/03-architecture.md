# 03 · 系统架构

## 1. 分层

借鉴 pi-mono 的三层依赖：每层只依赖下层，下层可以单独使用。

```
L4 前端        web/（React）   cli/   mcp/   gateways/（飞书、Telegram、QQ）
                       │ HTTP + SSE / WebSocket / stdio
L3 应用        examprep（学习领域）
               ├ server      FastAPI、鉴权、租户、会话路由
               ├ pedagogy    教学状态机、策略、计划器
               ├ ingest      资料引擎（移植 Skill）
               ├ knowledge   证据库、混合检索、知识点图
               ├ learner     学习者模型、FSRS、错题、困惑点
               ├ agents      Tutor / Examiner / Grader / Verifier / Planner / Curator
               └ tools       给 Agent 用的领域工具
L2 运行时      examprep-runtime（与学习无关的通用 harness）
               ├ loop        异步生成器循环、工具执行、钩子、steer/follow-up
               ├ context     上下文引擎：分层组装、预算、压缩
               ├ memory      记忆接口：工作/情景/语义/程序
               ├ session     事件溯源的会话存储、分叉、回放
               ├ subagent    子 Agent 派生、隔离、结构化回传
               ├ policy      工具权限、预算、护栏
               └ telemetry   OpenTelemetry GenAI 语义约定
L1 模型层      examprep-llm（与 Agent 无关的多厂商接口）
               ├ protocols   openai-compatible / anthropic / gemini 三种协议
               ├ registry    模型能力表：上下文、工具调用、视觉、推理、缓存、价格
               ├ gateway     限流、重试、熔断、降级链、取消
               └ tokens      计数、成本
```

**为什么分成三个包**：L1、L2 不含任何学习相关代码，可以单独发布，面试时能清楚说明“通用 harness”和“领域应用”的边界。仓库用 uv workspace 管理，版本号同步。

## 2. 仓库结构

```
examprep-agent/
├ packages/
│  ├ llm/        examprep_llm/      # L1
│  ├ runtime/    examprep_runtime/  # L2
│  └ app/        examprep/          # L3（含 server、cli、mcp）
├ web/                             # L4 Web App（Vite + React + TS）
├ skills/                          # 教学策略（程序记忆，Markdown）
├ samples/                         # 示例课程（沿用 Skill 的中文样例 + 公开课抓取脚本）
├ evals/                           # 最后阶段的 benchmark
├ deploy/                          # docker compose、Helm（后期）
├ docs/                            # 设计文档、用户文档
└ tests/                           # 跨包集成测试
```

## 3. 技术选型

| 关注点 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.11+ | 复用 Skill 代码；AI 应用岗位主流 |
| 包管理 | uv workspace | 快，支持多包，`uv tool install` 一条命令安装 |
| Web 框架 | FastAPI + Uvicorn | 原生异步、SSE、OpenAPI 自动生成前端 SDK |
| 数据校验 | Pydantic v2 | 工具参数 schema、结构化输出、配置 |
| 数据库 | SQLAlchemy 2（async）+ Alembic；本地 SQLite（WAL），服务器 PostgreSQL | 同一套模型，两种部署 |
| 向量 | 本地 sqlite-vec，服务器 pgvector | 不引入独立向量库 |
| 关键词检索 | 自研 BM25（移植并缓存），中文二元组；情景记忆用 SQLite FTS5 / Postgres 全文 | Skill 已验证 |
| 嵌入与重排 | 可选。本地 fastembed（bge-m3 / bge-small-zh ONNX），或厂商 API | 不配置也能用 BM25 跑 |
| PDF | pypdfium2；可选 RapidOCR；可选 MinerU / Docling 插件 | pdfium 非线程安全，解析放进进程池 |
| 间隔复习 | py-fsrs（MIT） | 现成、维护活跃 |
| 任务队列 | 自研轻量持久队列：数据库表 + 租约（Postgres `SKIP LOCKED`，SQLite 单写者） | 不额外引入 Celery/Redis 也能断点续跑 |
| 缓存/限流 | 本地进程内；服务器模式 Redis | |
| 前端 | Vite + React + TypeScript + Tailwind + shadcn/ui；KaTeX；PDF.js | 构建产物打进 Python wheel，本地模式无需 Node |
| TUI | Textual（后期） | |
| 桌面 | Tauri（后期） | 体积小 |
| 可观测性 | OpenTelemetry SDK，本地自带 trace 查看器，可导出到 Jaeger/Langfuse | |
| 质量 | ruff、pyright（strict）、pytest + pytest-asyncio、pre-commit、GitHub Actions | |

**不直接依赖的框架**：LangChain、LangGraph、LlamaIndex、LiteLLM。核心循环、上下文引擎、模型网关都自己写，这是项目的技术含量所在。周边能力（PDF 解析、OCR、FSRS、嵌入模型）用成熟开源库。

**参考但不复制代码**：cc-haha 基于泄露的 Claude Code 源码，许可证不是 OSI 认可的开源协议，只借鉴公开的设计思路。

## 4. 数据模型

所有表带 `tenant_id`（本地模式固定一个值）。时间统一 UTC。

### 资料侧（课程级，只读于 Agent）

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `Course` | id, name, language, mode(cram/longterm), exam_date, settings | 一门课 |
| `Material` | id, course_id, rel_path, sha256, kind(lecture/notes/homework/exam/solution/figure), status, version | 文件；哈希变化即增量重建 |
| `Page` | material_id, n, text, quality(fast/recover/review), is_scan, width, height | 页 |
| `Chapter` | course_id, n, title, sources | Skill 的章节规则 |
| `Evidence` | id, course_id, material_id, page, char_span, bbox?, chapter_n, text, tokens, embedding? | 可被引用的最小单元，约 600 字 |
| `Figure` | id, material_id, page, bbox, path, kind(figure/question/answer), question_id? | 裁出的图 |
| `KnowledgePoint` | id, course_id, chapter_n, name, summary, evidence_ids, prerequisites | 知识点；必须带证据 |
| `Question` | id, course_id, source_ref, answer_ref, type(6 种), stem, options, answer, rubric, points, difficulty, kp_ids, chapter_n, chapter_guessed, is_exam | 题库，只来自资料 |

### 学习者侧（用户级，可写）

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `Enrollment` | user_id, course_id, current_chapter, current_part, plan | 学到哪 |
| `Attempt` | id, user_id, question_id, answer, verdict(right/wrong/partial/skip), score, rubric_hits, error_type, grader(rule/llm), evidence | 每次作答，只追加 |
| `Mastery` | user_id, kp_id, p_known, n_obs, updated_at | BKT 估计的掌握概率 |
| `Card` | id, user_id, kind(question/kp_recall), ref_id, fsrs_state | FSRS 卡片 |
| `ReviewLog` | card_id, rating, reviewed_at, elapsed_days, session_id | 只追加 |
| `Misconception` | id, user_id, kp_id, description, attempt_ids, status(open/resolved) | 错因 |
| `Note` | id, user_id, course_id, chapter_n, type(summary/confusion/note), text | 笔记 |
| `ProfileBlock` | user_id, key(preferences/goals), text, version | 有字数上限的画像，用户可见可改 |

### 运行时

| 实体 | 关键字段 | 说明 |
|---|---|---|
| `Session` | id, user_id, course_id, parent_id, fork_at_seq, status | 支持分叉（换一种讲法重来） |
| `Event` | session_id, seq, type, payload, created_at | 事件溯源：用户消息、助手消息、工具调用、工具结果、状态迁移、压缩 |
| `Job` | id, type, payload, status, attempts, lease_until, parent_id | 资料导入任务 DAG |
| `Span` | trace_id, span_id, name, attrs, start, end | 本地 trace，可导出 |

## 5. 关键流程

### 5.1 资料导入

```
上传/指定文件夹
 → 扫描并算哈希，与已有 Material 比较（新增/变更/删除）
 → 每个变更文件一个 parse 任务（进程池并发）：抽文本 → 页眉页脚去除 → 页面质量打分 → 扫描页检测
     └ 低质量页（可选）→ OCR / VLM / MinerU
 → 每个 PDF 一个 figures 任务（进程池，按页分片）
 → 课程级 barrier：章节划分 → 切 Evidence → 建 BM25（+ 可选嵌入，异步批量）
 → 题目抽取与配对 → 题目图裁剪 → 难度分 → 章节归属
 → LLM 任务（并发受模型网关限流）：
     ├ 每章抽知识点（每个知识点必须引用 Evidence id，否则丢弃）
     ├ 知识点先修关系
     ├ 题目 → 知识点标注
     └ 无编号题目兜底抽取（标记为 llm_extracted，需人工确认后才进入真题池）
 → 生成导入报告（同 Skill 的 setup 输出：章节、题数、图数、警告、注入告警）
```

前端通过 SSE 实时显示每个任务进度。任务失败可重试，进程崩溃后从未完成的任务继续。规则部分（Skill 已有的能力）不依赖 LLM，没配置模型也能完成导入。

### 5.2 一轮讲解

```
用户：“继续”
 → 会话 lane 串行入队
 → 教学状态机：当前状态 TEACH，策略决定动作 = 讲第 k 段
 → 工具 next_slice 取原文段 + 图 + 相关 Evidence id
 → 上下文引擎组装（见 04 文档）
 → Tutor 流式生成，引用写成 [[E123]] 标记
 → 同时并行：Verifier 逐句校验；预取下一段；为本段知识点预生成复习卡
 → 渲染：[[E123]] 换成 “(lec2.pdf p.3)” 可点击引用；校验失败的句子降级为 🟡 或重写
 → 事件写入，状态机前进，返回下一步建议（替代 Skill 的 📍 行）
```

### 5.3 练习与判分

```
状态 PRACTICE → Examiner 选题（错题优先 → 未做过的真题 → 其他，叠加难度与掌握度）
 → 先展示题面图，再出题
 → 学生作答
 → 规则判分（移植 grade_answer：选项、对错、数值、短填空）
     └ 不确定 → Grader 子 Agent（独立上下文：只有题目、参考答案、评分要点、学生答案）
               → 结构化结果：得分、命中要点、错误类型、理由
 → 写 Attempt → 更新 Mastery（BKT）→ 更新 Card（FSRS）→ 错题本/错因
 → Tutor 讲评（可按提示阶梯先给提示再揭晓答案）
```

### 5.4 主动复习（长期模式）

```
调度器每天按用户时区运行：
 → 查到期 Card、临近考试的高频知识点
 → Planner 生成今日任务
 → 推送到 Web 通知 / IM 机器人
 → 用户在 IM 里直接作答 → 同 5.3 判分 → 回写
```
