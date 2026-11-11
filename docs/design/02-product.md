# 02 · 产品定位、形态与命名

## 1. 一句话定位

**把你自己的课件、作业和真题变成一个不编造内容的私人导师：每句话都能点回原文页码，只用真题考你，记得你哪里不会，到时间提醒你复习。**

## 2. 用户与场景

| 场景 | 典型用户 | 时间 | 核心诉求 |
|---|---|---|---|
| 期末突击 | 本科生 | 1–3 天 | 只讲会考的，按真题频次排优先级，快 |
| 期中/小测 | 本科生 | 1 周 | 讲完再练，错题回炉 |
| 考研/考证 | 备考生 | 数月 | 长期计划、间隔复习、知识点掌握度可见 |
| 长期学一门课 | 自学者 | 学期级 | 按知识依赖顺序学，学过的不忘 |

两种模式共用同一套数据：**突击模式**按考试日期和真题频次排计划；**长期模式**按知识点依赖图推进，由 FSRS 安排复习。用户可以随时切换。

## 3. 竞品与差异化

调研时间 2026-11，star 数来自 GitHub API。

| 产品 | star | 基于自己资料 | 页码溯源 | 只用真题出题 | 长期记忆 + 间隔复习 | 小模型/本地模型可用 |
|---|---|---|---|---|---|---|
| DeepTutor（HKUDS） | 58k | ✅ | ✅ | ⚠️ 无此约束 | ✅ 三层记忆，复习靠 Anki 导出 | 未专门优化 |
| OpenMAIC（清华） | 43k | ⚠️ | ❌ | ❌ | ❌ | — |
| Open Notebook | 42k | ✅ | ⚠️ | ❌ | ❌ | — |
| NotebookLM（闭源） | — | ✅ | ✅ | ❌ | ❌ | ❌ |
| 我们 | — | ✅ | ✅ 系统生成、可点击跳转、逐句校验 | ✅ 硬约束 | ✅ 原生 FSRS + 掌握度模型 | ✅ 延续 Skill 的实测优势 |

DeepTutor 是最强的竞品，功能面很广，定位是通用学习工作台。我们不在功能数量上和它比，而是在三件事上做到最好：

1. **可证明的不编造**：引用由系统生成，模型无法写出不存在的页码；每句标 🟢 的话都经过校验；资料外的问题拒答。
2. **为考试设计**：只用资料里的真题和作业出题，客观题由代码判分，主观题由独立的判分 Agent 对照参考答案判分。
3. **记得你**：掌握度、错题、困惑点、复习时间都是结构化数据，用户可见、可修改。

## 4. 产品形态

参考 opencode、cc-haha、OpenClaw 的共同做法：**一个核心服务，多个前端**。

```
                     ┌──────────── 核心服务（Python，HTTP + SSE）────────────┐
   Web App ─────────►│  Agent Runtime · 资料引擎 · 学习者模型 · 模型网关      │
   桌面版（Tauri 壳）►│                                                       │
   CLI / TUI ───────►│  本地模式：SQLite，单用户，零外部依赖                  │
   MCP Server ──────►│  服务器模式：PostgreSQL + Redis，多用户                │
   IM 机器人 ───────►│                                                       │
                     └───────────────────────────────────────────────────────┘
```

| 前端 | 面向谁 | 优先级 | 说明 |
|---|---|---|---|
| **Web App（本地优先）** | 所有学生 | P0 | `kaopu` 一条命令启动，自动打开浏览器。左侧聊天，右侧原文阅读器，点击引用跳到对应页并高亮；进度、错题、复习日历各一个面板 |
| **CLI** | 开发者、无界面环境 | P0 | 和 Web 共用服务；`kaopu ingest`、`kaopu chat`、`kaopu review` |
| **MCP Server** | 现有 Skill 用户、Claude Code/Cursor 用户 | P1 | 把检索、出题、判分、记录作为 MCP 工具暴露，延续 Skill 的生态位，可以再上 awesome 列表 |
| **IM 机器人** | 长期学习用户 | P1 | 飞书、Telegram、QQ。每天推送到期复习题，直接在聊天里答题。这是长期模式留住用户的关键 |
| **桌面版** | 不想开命令行的学生 | P2 | Tauri 包装 Web App，双击安装 |
| **移动端** | 碎片时间复习 | P2 | Web App 做成 PWA；服务器模式下手机访问 |

**本地模式和服务器模式是同一套代码**，只是存储和队列的实现不同。本地模式让学生零配置上手，服务器模式体现多租户、并发、可部署这些工程能力。

### 安装方式

```bash
# 推荐：uv
uv tool install kaopu && kaopu
# 或 pip
pip install kaopu && kaopu
# Windows 一行
irm https://kaopu.dev/install.ps1 | iex
# 服务器模式
docker compose up -d
```

### 模型支持

- 云端：OpenAI、Anthropic、Gemini，以及兼容 OpenAI 协议的 DeepSeek、通义千问、智谱 GLM、Kimi、MiniMax、硅基流动、OpenRouter 等。
- 本地：Ollama、LM Studio、vLLM、llama.cpp。
- 按角色配置模型：讲解用强模型，判分、校验、抽取用便宜模型。首次启动有设置向导，只填一个 API Key 也能跑。

## 5. 命名

| 候选 | 含义 | 优点 | 缺点 |
|---|---|---|---|
| **Kaopu 靠谱**（推荐） | “靠谱”谐音“考谱” | 直接点出核心卖点“不编造”，又和“考”相关；中文用户一看就懂、好记、好传播；PyPI 上 `kaopu` 未被占用，GitHub 同名仓库只有零星无关项目 | 英文用户需要一句解释 |
| Xueba 学霸 | 学霸 | 认知度高 | GitHub 上名字含 xueba 的仓库有 325 个，难以搜索到 |
| Exam Cram Coach Agent | 延续 Skill 品牌 | 继承已有的 awesome 列表收录和老用户 | 名字长，“cram”只覆盖突击场景，和长期学习定位不符 |

推荐 **Kaopu**，仓库名 `kaopu`，英文副标题：
> **Kaopu (靠谱, "reliable")** — the study agent that cites every page, quizzes you with real exam questions, and remembers what you got wrong.

中文副标题：
> **靠谱**：只讲你资料里有的，每句话都能翻到原文那一页。

## 6. 让项目被看到

参考 DeepTutor、OpenMAIC、cc-haha、原 Skill 的做法：

- **README 首屏**：一段 GIF（点击引用 → 原文 PDF 跳到对应页并高亮那段话）、一行安装命令、与 DeepTutor/NotebookLM/ChatGPT 的对比表。中文为主，配英文版。
- **可验证的数字**：沿用 Skill 的风格，每个宣传数字都附复现命令（例如“MIT 6.006 资料，89/89 题配对”“引用幻觉 0/N”）。benchmark 完成后放进 README。
- **渠道**：原 Skill 已被 Awesome Claude Code（56k）和 Awesome Agent Skills（36k）收录，新项目的 MCP Server 可以再申请收录；学生群体在小红书、B 站、知乎聚集，用短视频演示“考前一天用它复习”。
- **原 Skill 仓库导流**：等 Agent 发布后，经你同意再在 Skill 的 README 加一行链接。本阶段不改动 Skill 仓库。
- **活跃信号**：README 顶部放 News 时间线；版本节奏固定；Issue 模板和 Discussions 开启；GitHub Pages 文档站沿用 Skill 的 SEO 做法（llms.txt、JSON-LD、中英关键词）。

## 7. 明确不做

- 不做通用聊天助手或通用 RAG 平台。
- 不联网搜索补充知识（可作为可选插件，默认关闭，输出一律标 🟡）。
- 不做课程生成（OpenMAIC 的方向）。
- 不收集用户数据，本地模式不发送任何遥测。
