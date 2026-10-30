# 架构说明（v0.1.0 / P3）

本文把 `PLAN.md` 第 4 节的目标架构落成当前仓库的实际实现，覆盖 P0–P3：每一层对应哪些
模块、运行时如何接线、测试与 benchmark 如何复现。原则是**确定性代码守住边界，模型只做
需要语言能力的部分**；无法离线验证的能力不写进报告。

## 1. 总览

```
┌─ Interfaces ─ CLI（examprep_agent/cli.py，📍 引导）                     ✅ P0–P2
│                FastAPI（interfaces/http.py，extras serve）               ✅ P2
│                MCP stdio（interfaces/mcp_server.py，extras mcp）         ✅ P2
├─ Service ───── StudyService（service/study.py，唯一业务入口）             ✅ P2
│                models/errors/ingest/subagent/costs                       ✅ P2
├─ Agent Kernel ─ provider.py   模型抽象 + FakeProvider                    ✅ P0
│                 loop.py       plan–act–observe / 工具注册表 / 预算闸门    ✅ P0
│                 context.py    token 预算 + compaction v1                 ✅ P0
│                 session.py    会话检查点 / 分支字段 / 学习事件入口        ✅ P0
│                 子 agent 隔离上下文与风险上限                             ✅ P2
├─ Learning ──── tools/         内容管线（extract/chapters/questions/…）   ✅ P0
│                 learning/modes.py      长期/突击双模式                   ✅ P0
│                 learning/learner.py    BKT 掌握概率                      ✅ P0
│                 learning/scheduler.py  FSRS-4.5 复习调度                 ✅ P0
│                 learning/graph.py      先修知识图谱                      ✅ P1
│                 learning/planner.py    到期复习/先修/预算/新学排序        ✅ P1
├─ Grounding & Safety ─ tools/guard.py  fence + 注入启发式                 ✅ P0
│                       security/permissions.py  L0–L3 权限闸门 + 审计     ✅ P0.5
│                       security/commands.py     危险命令扫描              ✅ P0.5
│                       security/filetools.py    受闸门文件工具            ✅ P2
│                       security/locking.py      工作区文件锁              ✅ P2
├─ Memory ────── study_state.json / session.json / knowledge_graph.json     ✅ P0–P1
│                 memory/episodic.py  memory/semantic.py                   ✅ P1
│                 memory/procedural.py  memory/store.py                    ✅ P1
├─ Benchmarks ── benchmarks/run_all.py 一个入口跑四层 + 性能小项           ✅ P3
│                 grounding.py / behavior.py / tutor.py / performance.py   ✅ P3
│                 longhorizon/{simulator,runner}.py 隐藏状态隔离           ✅ P3
└─ Platform ──── JSONL 事件/审计/摄取进度日志、成本聚合、pytest、CI         ✅ P0–P3
```

## 2. 内容管线 `examprep_agent/tools/`

来源：`universal-examprep-skill/coach/*`（MIT，© ZeKaiNie）。保持确定性行为与安全语义，
只做包内相对导入、类型注解与 docstring 补全；没有移植原 CLI 的界面逻辑。

| 模块 | 职责 | 关键行为 |
| --- | --- | --- |
| `extract.py` | 课程文件 → 带页码的文本页 | PDF 走可选 `pypdfium2`/`pypdf`；DOCX/PPTX 用标准库 zip+xml；MD/TXT/HTML；重复页眉页脚剔除；文件分类 |
| `chapters.py` | 文本页 → 有序章节 | 文件名/首行编号、无编号教材按内部标题切分、同号合并、作业/试卷不进章节 |
| `questions.py` | 作业/试卷 → 题库 | 题号序列规则、教材式标签配对解答文件、题型识别、客观题自动判分、章节归属 |
| `figures.py` | 配图与题面/答案裁剪 | PDF 对象聚类、扫描页识别、PPTX/DOCX 媒体抽取、纯 zlib PNG 编码 |
| `guard.py` | 材料 fence 与注入检测 | 内容派生 fence id、零宽/全角/连字符变形归一、中英文注入启发式 |
| `index.py` | BM25 检索 | 纯内存、无依赖、按章节过滤；空结果即「材料没有」 |
| `state.py` | `study_state.json` | 原子写（tmp + `os.replace`）、旧版字段迁移、progress/notebook 视图 |
| `text.py` | 文本工具 | ASCII 词 + CJK 二元组、停用词、清洗、分段、中文数字 |

## 3. 内核与学习层

### 3.1 `core/provider.py`

- `ModelProvider` Protocol：`complete(messages, tools) -> ModelResponse`。
- `OpenAICompatibleProvider`：仅标准库 HTTP，OpenAI 兼容 `/chat/completions`；默认
  DeepSeek `deepseek-flash`；API key 不落盘、不打印。
- `FakeProvider`：脚本回放，零网络；benchmark 与测试只用它。

### 3.2 `core/loop.py`

- `ToolRegistry`：工具名/描述/JSON schema/handler + 风险级 + `path_arguments`；执行前先过
  `PermissionGate`，allow/deny 都写审计。
- 双闸门：`max_steps` 与 `max_total_tokens`；超预算的那轮响应中的工具一律不执行。
- `run_agent(context=...)`：每次模型调用前 `ContextEngine.compact()`，记录 compaction 事件。
- `EventLogger`：JSONL 落盘到 `<workspace>/logs/agent-YYYY-MM-DD.jsonl`。

### 3.3 `core/context.py`

三舱预算（system/material/history），任一舱或总预算超限即压缩；history 保留最近尾巴原文，
旧消息交给 provider 合并进滚动摘要；无 provider 时确定性截断。摘要以内容派生 fence 的
user 消息回填，绝不进入 system prompt。

### 3.4 `core/session.py`

`Session` 绑定 study state、`BKTModel`、`FSRSScheduler`、`ContextEngine`、知识图谱、三层
记忆与安全上下文。`record_answer()` 是学习事件唯一入口：写 history/错题 → 更新 BKT →
更新 FSRS（`right=Good, wrong=Again, skip=Hard`）→ 写长期记忆。可选 `today` 参数供离线
长程模拟注入模拟时钟（生产默认真实日期）。

### 3.5 `learning/`

- `modes.py`：`LongTermPolicy`（留存 0.9、掌握门槛 0.8、复习优先）与 `CramPolicy`（留存
  0.8、门槛 0.55、真题密度优先）；`select_policy(days_left)` 以 21 天为界自动切换。
- `learner.py`：四参数 BKT（p_init/p_learn/p_guess/p_slip），先验、后验、学习转移与数值夹紧。
- `scheduler.py`：FSRS-4.5 公式（`R(t,S)=(1+19/81·t/S)^-0.5`），卡片 Difficulty/Stability/
  due/reps/lapses；目标留存率反解间隔。
- `graph.py`：章节级知识点（`chN`），顺序边 + 显式引用边，用户编辑事务化回滚，先修阻断。
- `planner.py`：优先级 = 到期复习（长期模式）→ 未满足先修 → 每日新学预算 → 新学/继续；
  突击模式按 `urgency × 真题频率 × (1−掌握度)` 排序。

## 4. 服务层与接口

`service/study.py` 的 `StudyService` 拥有全部业务决策与持久化；CLI/HTTP/MCP 只做参数解析、
双语措辞与渲染。

| 模块 | 职责 |
| --- | --- |
| `service/models.py` | pydantic 请求/响应模型，带 `exit_code` |
| `service/errors.py` | `ServiceError` + CLI 退出码 ↔ HTTP 语义 |
| `service/ingest.py` | 线程池摄取（extract/chapters/figures/index），逐文件失败隔离，进度 JSONL |
| `service/subagent.py` | 隔离子 agent：独立上下文预算、摘要 sanitize + fence、风险上限不越父级 |
| `service/costs.py` | 从现有 JSONL `model_response`/`compaction` usage 聚合 token/成本 |
| `interfaces/http.py` | FastAPI（extras `serve`），每请求按 workspace 加载，写路径持锁 |
| `interfaces/mcp_server.py` | MCP stdio（extras `mcp`），7 个工具全部走 `StudyService` + 闸门 |

安全边界（P0.5/P2）：

- 材料目录只读；工作区外读写需 L2 显式 grant；授权文件在用户配置目录，不在模型可写区；
- 引擎控制文件（`study_state.json`、`memory/`、`audit/`、`.snapshots/`、`.trash/`、
  `logs/` 等）对模型写/删只读；
- 删除只进 `.trash/`；危险命令静态扫描直接拒绝；凭据出现在参数中拒绝并脱敏审计；
- 写熔断与工作区文件锁；每个 allow/deny 都进 `audit/audit-*.jsonl`。

## 5. Benchmark 套件 `benchmarks/`

一个入口：

```bash
python -m benchmarks.run_all --out benchmarks/results/
```

写出 `results.json`、`BENCH_REPORT.md`、`summary.md`；报告由同一份 results 对象渲染，禁止
手填。规范运行还会把同一张摘要表同步到 README 的英文/中文标记块与 `P3_REPORT.md` 的快照块。
默认强制 `EXAMPREP_OFFLINE=1` 并使用临时配置目录，不读取真实 API key；唯一的例外是
`--judge`，其 provider 在进入离线沙箱前显式解析并注入，其他层仍然看不到 key。

| 模块 | 层 | 设计 |
| --- | --- | --- |
| `grounding.py` | GroundingBench | 闭卷 / 裸文件 / agent 三臂使用**同一读取器与同一评分器**；agent 只调用生产 `StudyService.ask`；金标只在 `_score()`；探针拒答来自全局、无金标的证据支持检查（IDF n-gram 覆盖 + 连续重叠 + ASCII 实体） |
| `behavior.py` | BehaviorBench | **材料诱导**攻击（毒材料在上下文、FakeProvider 从材料解析动作、经真实 `run_agent` 发起）+ 补充静态/直调用例；16 个正向正常命令与 3 个负向控制分开计数；6 条独立跨命令不变量（footer 清单含 `done`） |
| `longhorizon/simulator.py` | LongHorizon Sim | `HiddenLearner` 保存真实稳定性/最后接触/召回概率；只在独立子进程中构造 |
| `longhorizon/remote.py` | LongHorizon Sim | `RemoteLearnerAPI` 通过管道只暴露 `teach/quiz->bool/restudy`；评估器用另一条管道读真实留存；`isolation_report()` 实际尝试读取/篡改 |
| `longhorizon/runner.py` | LongHorizon Sim | 30 天协议；两臂同可用预算；学习增益 = 后测 − 前测；增益/实际小时、增益/预算小时、无复习/无重读反事实、等实际时间干预与延迟实测 95% CI |
| `tutor.py` | TutorRubric | 16 个固定场景（good / medium 单信号消融 / flawed / bypass）；引用页、claim、先测相关性与下一步命令语法/qid 均可核验；可选 `--judge`（无 key 跳过并注明） |
| `performance.py` | 性能小项 | 摄取吞吐、串行/线程耗时与加速比、核心命令中位/p95 延迟；写入机器信息 |

诚信机制：

- **隐藏状态隔离**：隐藏学习者只在子进程中构造；agent 只拿到管道窄接口，方法无闭包。
  `isolation_report()` 真的发送 `snapshot / expected_retention / realized_test /
  set_stability / 非法 qid` 请求并断言全部被拒、隐藏快照不变；
  `hidden_state_isolated = all(检查项)`，不是字面量。
- **同种子复现**：模拟器的逐题 RNG 由 `(seed, qid)` 派生，臂内动作顺序不改变采样流；
  benchmark 其余层无随机性。
- **报告可对账**：`BENCH_REPORT.md`、`summary.md`、README 两块与 P3 快照块均由
  `results.json` 的同一对象渲染，测试逐字比较。

## 6. 工作区文件

| 文件 | 内容 |
| --- | --- |
| `study_state.json` | 课程、语言、考试日期、模式、章节进度、history、错题、笔记、BKT、FSRS、图谱编辑 |
| `chapters.json` / `quiz_bank.json` / `figures.json` / `index.json` | 内容管线产物 |
| `knowledge_graph.json` | 先修图谱节点与边 |
| `session.json` | 内核检查点：mode、滚动摘要、分支元数据、计数器 |
| `memory/` | 情景 JSONL、语义画像、程序性偏好 |
| `audit/audit-*.jsonl` | 安全审计（只读，脱敏） |
| `progress.md` / `notebook.md` | 人读视图 |
| `chapters/chNN*.md` | 按教学切片导出的可读章节 |
| `logs/agent-*.jsonl` / `logs/ingest-*.jsonl` | 内核事件 / 摄取进度 |
| `exam-cram/` | 默认工作区目录，已在 `.gitignore` 中 |

## 7. 测试与 CI

`tests/` 全部离线：

- P0：工具层移植测试、CLI 端到端、内核循环、上下文压缩、BKT/FSRS 参考值、provider 单测；
- P1：图谱/规划器、三层记忆、权限闸门对抗集、CLI 会话回归；
- P2：service 结果与 CLI 一致性、并行摄取业务产物等价、子 agent 隔离、HTTP/MCP 冒烟与
  真实子进程握手、成本聚合、端到端文件工具；
- P3：`tests/test_p3_benchmarks.py`（runner 产物、报告/README/P3 三处逐字对账、同种子复现、
  grounding/behavior/tutor 诚信测试、judge key 路径）与
  `tests/test_longhorizon_isolation.py`（对象图/闭包/管道篡改、快照不变、计算式隔离字段）。

`.github/workflows/ci.yml` 在 Python 3.11 / 3.12 上安装 `.[dev,serve,mcp]` 并运行
`python -m pytest -q`。本次验收环境：Python 3.12.3、2 vCPU 容器、未安装 PDF 后端
（1 个 PDF 用例 skip）。

## 8. 明确边界（v0.1.0）

- 没有真人学习试点，没有 IRT 校准，也没有把模拟参数拟合到真人数据；LongHorizon 的结论
  只在其参数假设内成立。
- TutorRubric 默认是确定性 rubric 信号，不是 TutorBench 式的大规模 LLM 评分；`--judge`
  需要 API key，本期未启用。
- 性能数字是当前容器实测，文本摄取受 GIL 限制，本容器未观察到并行加速（具体数字见自动
  生成的 benchmark 报告）；PDF 并发的真实收益未在本机验证（无 PDF 后端）。
- FastAPI/MCP 面向本机/单用户，无鉴权/多租户；MCP 只实现 stdio。
- 并行题目抽取仍顺序执行（解答与题目跨文件配对）；Redis 未引入。
- 引用校验目前是工具层 fence + benchmark 的逐条 `file p.N` 核验；生产回答的逐句事实
  断言校验器仍是后续工作。
