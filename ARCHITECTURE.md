# 架构说明（P0）

本文把 `PLAN.md` 第 4 节的目标架构落成当前仓库的实际实现：每一层对应哪些模块、
P0 已实现到什么程度、哪些只是接口预留。P0 的原则是**地基可运行、行为可测试、演进不返工**，
而不是把所有层一次做满。

## 1. 总览

```
┌─ Interfaces ─ CLI（examprep_agent/cli.py）                    ✅ P0
│                HTTP API / MCP Server / Web UI                 ⬜ 预留
├─ Agent Kernel ─ provider.py   模型供应商抽象 + OpenAI 兼容 + Fake ✅ P0
│                 loop.py       plan–act–observe 循环 / 工具注册表  ✅ P0
│                 context.py    token 预算 + compaction v1          ✅ P0
│                 session.py    会话 / 检查点 / 分支字段预留        ✅ P0
│                 并发引擎 / 子 agent / 作业队列                    ⬜ 预留（P2）
├─ Learning Engine ─ tools/     内容管线（extract/chapters/questions/figures）✅ P0
│                    learning/modes.py      双模式策略             ✅ P0
│                    learning/learner.py    BKT 掌握概率           ✅ P0
│                    learning/scheduler.py  FSRS 复习调度          ✅ P0
│                    IRT / 先修知识图谱 / 教学提示阶梯              ⬜ 预留（P1+）
├─ Grounding & Safety ─ tools/guard.py  fence + 注入启发式          ✅ P0
│                       CLI 打印路径强制 fence                       ✅ P0
│                       引用校验器 / 独立审计日志                    ⬜ 预留（P1+）
├─ Memory ─ study_state.json（进度/错题/笔记）+ session.json          ✅ P0
│            learner 与 reviews 持久化                                  ✅ P0
│            SQLite 长期记忆 / 学习者画像 / 可导出                       ⬜ 预留（P1+）
└─ Platform ─ 结构化事件日志 JSONL / pytest / GitHub Actions CI        ✅ P0
              OpenTelemetry / 成本核算 / Docker / 服务化                 ⬜ 预留
```

## 2. 工具层 `examprep_agent/tools/`

来源：`universal-examprep-skill/coach/*`（MIT，© ZeKaiNie），保持确定性行为与安全语义，
只做包内相对导入、类型注解与 docstring 补全；**没有**移植原 `coach/cli.py` 的界面逻辑。

| 模块 | 职责 | 关键行为 |
| --- | --- | --- |
| `extract.py` | 课程文件 → 带页码的文本页 | PDF 走可选 `pypdfium2`/`pypdf`；DOCX/PPTX 用标准库 zip+xml；MD/TXT/HTML；重复页眉页脚剔除；文件分类 |
| `chapters.py` | 文本页 → 有序章节 | 文件名/首行编号、无编号教材按内部标题切分、同号合并、作业/试卷不进章节 |
| `questions.py` | 作业/试卷 → 题库 | 题号序列规则、教材式标签配对解答文件、题型识别、客观题自动判分、章节归属 |
| `figures.py` | 配图与题面/答案裁剪 | PDF 对象聚类、扫描页识别、PPTX/DOCX 媒体抽取、纯 zlib PNG 编码 |
| `guard.py` | 材料 fence 与注入检测 | 内容派生 fence id、零宽/全角/连字符变形归一、中英文注入启发式 |
| `index.py` | BM25 检索 | 纯内存、无依赖、按章节过滤；空结果即「材料没有」 |
| `state.py` | `study_state.json` | 原子写（tmp + `os.replace`）、旧版 `exam_days` 迁移、progress/notebook 视图 |
| `text.py` | 文本工具 | ASCII 词 + CJK 二元组、停用词、清洗、分段、中文数字 |

## 3. Agent 内核 `examprep_agent/core/`

### 3.1 `provider.py`

- `ModelProvider` Protocol：`complete(messages, tools) -> ModelResponse`。
- `OpenAICompatibleProvider`：仅标准库 HTTP，POST `{base_url}/chat/completions`；环境变量
  `EXAMPREP_MODEL`（默认 `deepseek-flash`）、`EXAMPREP_BASE_URL`（默认 `https://api.deepseek.com`）、
  `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`。API key 不落盘、不打印。
- `FakeProvider`：按脚本回放 `ModelResponse` / 字符串 / dict（含 `tool_calls`）/ 可调用对象，
  零网络，供全部内核测试使用。

### 3.2 `loop.py`

一次运行 = 调模型 → 执行工具 → 回填观察 → 再调模型，直到模型给出 final 回答或闸门触发：

- `ToolRegistry`：`name` / `description` / JSON schema / `handler`；未知工具与工具异常转成
  观察文本，不会中断循环；观察超过 `max_observation_chars` 时截断内层 `result` 并标记
  `"truncated": true`，始终是合法 JSON。
- 双闸门：`LoopLimits.max_steps`（模型调用次数）与 `max_total_tokens`（累计 token）。
  provider 未报 usage **或报 0** 时用 `context.estimate_tokens` 估算；某轮响应把预算推过
  上限时，该轮响应中的工具一律不执行（工具可能有副作用），直接以 `token_budget` 停止。
- `run_agent(context=...)`：传入 `ContextEngine` 时由它持有 system/history/material，
  每次 `provider.complete()` 前调用 `compact()`，发送压缩后的消息并记录 `compaction` 事件。
- `EventLogger`：JSONL 逐行落盘到 `<workspace>/logs/agent-YYYY-MM-DD.jsonl`，事件类型包括
  `session_start` / `model_response` / `tool_call` / `tool_result` / `final` / `budget_exhausted` /
  `provider_error` / `compaction`。

### 3.3 `context.py`

- `estimate_tokens`：CJK 字符按 1 token、其余按约 4 字符/token 估算。
- `ContextBudget.from_total`：system/material/history 三舱（默认 15% / 45% / 40%），
  三个比例必须和为 1；每舱按自己的比例计算，history 只吸收取整余数。
- `ContextEngine.compact`：任一舱或总预算超限即触发。system/material 确定性截断到本舱
  预算；history 保留最近 4 条（受尾巴预算约束）原文，旧消息交给 provider 合并进滚动摘要
  （不会把 tool 结果和它的 assistant tool_call 拆开）；单条超大消息也会被截断，保证压缩后
  一定回到预算内。无 provider 或 provider 失败时使用**确定性截断摘要**，离线永远可压缩。
- 摘要按数据渲染：先剔除/替换疑似注入行，再以内容派生 fence 的 **user 消息**放入上下文，
  绝不拼进 system prompt，因此摘要不会获得系统指令优先级。

### 3.4 `session.py`

- `Session` 绑定 `tools.state` 的学习状态 + `BKTModel` + `FSRSScheduler` + `ContextEngine`；
  构造/`apply_policy()` 时把当前模式的 `target_retention` 写入 FSRS（长期 0.9、突击 0.8）。
- `save()`：`study_state.json`（含 `mode` / `learner` / `reviews`，`reviews` 也保存 FSRS
  weights）与 `session.json` 均原子写（tmp + fsync + `os.replace`；并发写锁留待 P1）。
- `session.json` 保存 mode、滚动摘要、branch 元数据（`id/parent/label/created`，为后续会话
  分支预留）与计数器；`restore()` 是显式恢复入口。
- `record_answer()` 是学习事件唯一入口：写 history/错题、更新 BKT（right/wrong）、更新 FSRS
  卡片（right=Good、wrong=Again、skip=Hard）。

## 4. 学习层 `examprep_agent/learning/`

### 4.1 `modes.py`

`StudyPolicy` 定义新学/复习配比、测验强度、推进掌握度阈值、目标留存率、每日新学上限等；
`LongTermPolicy` 与 `CramPolicy` 给出两套默认值。`select_policy(days_left, override)`：

- `override` 为 `longterm` / `cram` 时直接采用；
- `auto`：距考试 ≥21 天 → 长期；<21 天 → 突击；无考试日期 → 长期。

`setup --mode` 把结果写入 `study_state.json` 的 `mode` / `policy` / `mode_reason`；
`Session` 据此设置 FSRS 目标留存率。掌握度门控是**真门控**：`done` 在掌握度低于
`advance_mastery` 时不改状态、不推进，并提示先 `quiz`；`done --force` 是显式覆盖。

### 4.2 `learner.py`

真实四参数 BKT：先验 `p_init`、学习率 `p_learn`、猜测 `p_guess`、失误 `p_slip`。
一次答题先算后验，再做学习转移并夹在 `[1e-4, 1-1e-4]`。P0 的知识点粒度是章节（`chN`），
接口支持任意字符串 key，P1 可细化到知识点/题目。

### 4.3 `scheduler.py`

FSRS-4.5 公开公式的紧凑实现：`Card(Difficulty, Stability, due, reps, lapses)`，
`R(t,S) = (1 + 19/81 · t/S)^(-0.5)`，按目标留存率反解下次间隔；
`FSRSScheduler` 提供卡片存取、`due()` 与 JSON 往返（连同自定义 weights 一起持久化）。
测试覆盖一组 FSRS-4.5 参考向量（固定 S/D/rating/elapsed 的预期 S/D/interval）、间隔单调性、
连续 Good 间隔递增、Again 重置为 1 天并计 lapse。`skip` 按产品语义映射为 Hard（计一次复习、
不计 lapse），已在 `Session.record_answer` 文档与测试中明确。

## 5. CLI `examprep_agent/cli.py`

命令集与 skill 等价：`setup / status / next / ask / quiz / check / grade / answer / done /
note / mistakes / cheatsheet / plan / goto / doctor`。约定：

- 每条学习命令最后一行以 `📍` 开头，并给出下一条可执行 `examprep ...` 命令；空题库/空结果
  分支同样输出 `📍`。
- `setup` 是唯一的重管线：扫描 → 配图 → 章节 → 题库（含可选预置 `*quiz*bank*.json` 导入）
  → BM25 索引 → 状态/会话落盘；标题会经过 guard 清洗；实际工作区路径（可能在材料目录内）
  会从扫描中排除。非 `--fresh` 重新 setup 保留章节进度、BKT、FSRS、上下文摘要与计数器。
- 来自文件的文本（材料、题干、选项、答案、检索片段）都经 `guard.fence` 输出；预置题库的
  `id`/`type`/`source` 元数据经过白名单清洗，印刷在 fence 外的 qid 也只来自安全取值。
- `quiz` 题量 = `-n` > `min(profile.quiz_n, policy.quiz_intensity)`，保留 skill 的
  `--profile` 语义，同时让模式强度封顶。
- 确定性命令不调用模型；`next` 在配置了 provider 时用内核循环生成讲解（工具：
  `search_materials` 返回 fenced + flagged 片段，事件写入 `logs/`），无 key 时输出材料切片
  并显式注明回退。

## 6. 工作区文件

| 文件 | 内容 |
| --- | --- |
| `study_state.json` | 课程、语言、考试日期、模式、章节进度、history、错题、笔记、BKT、FSRS |
| `chapters.json` / `quiz_bank.json` / `figures.json` / `index.json` | 内容管线产物 |
| `session.json` | 内核检查点：mode、滚动摘要、分支元数据、计数器 |
| `progress.md` / `notebook.md` | 人读视图（由 state 渲染） |
| `chapters/chNN*.md` | 按教学切片导出的可读章节 |
| `logs/agent-*.jsonl` | 内核结构化事件日志（仅在有 provider 时产生） |
| `exam-cram/` | 默认工作区目录，已在 `.gitignore` 中 |

## 7. 测试与 CI

`tests/` 全部离线：工具层移植测试（guard/index/state/chapters/questions/text/extract）、
CLI 端到端（样例课程完整学习循环、结构化的 rc/footer 断言、注入防护、材料目录内自定义
workspace 重复 setup、预置题库元数据注入、空题库 footer、模式选择、profile 题量）、内核测试
（FakeProvider 剧本、零 usage/超预算工具硬闸门、合法 JSON 截断、事件日志、上下文压缩接线）、
上下文压缩（三舱比例、material/system 截断、单条超大消息、摘要注入隔离、保真、增量合并、
失败回退）、BKT 参考值、FSRS-4.5 参考向量与 weights 往返、provider 纯离线单测（配置/请求
payload/响应解析/HTTP 与传输错误）、会话检查点与目标留存率接线。`.github/workflows/ci.yml`
在 Python 3.11 / 3.12 上运行 `python -m pytest -q`。

## 8. 明确未做（P0 边界）

- 没有 HTTP/MCP/Web 接口；没有 SQLite/Postgres 长期记忆；没有 IRT、先修图谱、苏格拉底
  提示阶梯；没有并发摄取/子 agent/作业队列；没有 OpenTelemetry、成本核算、Docker；
  没有 GroundingBench / BehaviorBench / LongHorizon Sim / TutorRubric。
- 双模式策略与 BKT/FSRS 已实现并持久化，目标留存率与掌握度门控已接线；但 FSRS 到期队列
  还没有反哺 `next`/`plan` 的内容排序，这是 P1 的首要工作。
- 引用校验目前由提示词约束 + 工具层 fence/出处标注保证，尚未做逐条事实断言的自动校验器。
- 原子写已加 fsync，但固定 `.tmp` 名不提供多进程写锁；figures 模块的 PNG 编码/聚类/裁剪
  仍只有 CLI setup 的间接覆盖；这些列为 P1。
