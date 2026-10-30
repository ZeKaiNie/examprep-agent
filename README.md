# examprep-agent

A dual-mode study agent — **long-term retention** for months-long preparation and
**cram mode** for exams days away — built on the deterministic content pipeline of
[Exam Cram Coach](https://github.com/ZeKaiNie/universal-examprep-skill) (MIT,
© ZeKaiNie), with a new Python agent kernel, a BKT learner model and FSRS scheduling.

## English

### What it is

`examprep-agent` turns a folder of course files into a study workspace, then runs a
grounded teaching loop on top of it:

- every answer is tied to the materials (`file p.N`), and out-of-scope questions are
  refused instead of hallucinated;
- course files are treated as **data, never instructions** (content fencing plus a
  prompt-injection detector inherited from the original skill; search-tool results and
  prebuilt question-bank metadata go through the same boundary);
- a BKT model tracks the probability that each knowledge point is known, and FSRS
  schedules reviews by difficulty, stability and retrievability;
- the same engine switches between **long-term** (retention-first, mastery-gated) and
  **cram** (exam-frequency triage, high quiz intensity) policies, automatically from
  the days left until the exam. The mastery gate is enforced by `done`; use
  `examprep done --force` to advance a chapter below the threshold.

### Features (P0)

- Deterministic tool layer ported from the skill: PDF/DOCX/PPTX/MD/TXT/HTML extraction,
  chapter grouping, question/answer extraction, BM25 retrieval, figure cropping,
  material fencing and prompt-injection heuristics, atomic `study_state.json`.
- Agent kernel: OpenAI-compatible provider (DeepSeek by default, with fully offline
  unit tests) plus a zero-network `FakeProvider`; a plan–act–observe loop with a tool
  registry, hard max-step and token budgets, and a JSONL event log.
- Context engine v1: token estimation, validated system/material/history budgets,
  incremental compaction that keeps the recent tail verbatim and summarises older
  turns; the loop compacts the context before every model call and logs each pass.
- Durable sessions: study state + learner mastery + FSRS cards + `session.json`
  checkpoint, all written atomically (fsync + replace); branch metadata reserved for
  future forking.
- Learning layer: real BKT updates and a real FSRS-4.5 implementation (not stubs);
  the active mode's target retention drives FSRS intervals (long-term 0.9, cram 0.8).
- CLI with the skill-equivalent command set and a `📍` next-step line on every study
  command; deterministic commands work with no model configured. `--profile`
  keeps the skill's quiz size (`small`/`standard`/`large` = 1/3/5), capped by the
  mode's quiz intensity.

### Quick start

```bash
python -m pip install -e ".[dev]"
examprep doctor

# Build a workspace from a materials folder (exam in 30 days → long-term mode)
examprep setup ./materials --days 30
examprep status
examprep next            # print the next material slice (+ explanation when a model is configured)
examprep quiz -n 3
examprep grade q006 B
examprep plan
```

The workspace defaults to `<materials>/exam-cram/`. Use `--workspace PATH` or the
`EXAMPREP_WORKSPACE` environment variable to put it elsewhere; a workspace placed
inside the materials folder is automatically excluded from material scanning, so
re-running `setup` never re-ingests its own generated files.

Model configuration (optional; without a key `next` falls back to the material slice
and says so):

```bash
export DEEPSEEK_API_KEY=...          # or OPENAI_API_KEY
export EXAMPREP_MODEL=deepseek-flash # default
export EXAMPREP_BASE_URL=https://api.deepseek.com  # default
```

Run the offline test suite with `python -m pytest -q` (no network, no real API key).

### P0 status

P0 delivers the foundation: package/CI scaffolding, the ported deterministic tool
layer, the minimal agent kernel, context compaction v1 wired into the loop, durable
sessions, BKT + FSRS, and a CLI equivalent to the skill's study loop. Not yet
implemented (see [ARCHITECTURE.md](ARCHITECTURE.md)): HTTP/MCP interfaces, SQLite
long-term memory, IRT calibration, prerequisite knowledge graphs,
concurrency/sub-agents, and the four-layer benchmark suite. Dual-mode target
retention and the mastery gate are wired into the CLI in P0; using FSRS due dates to
re-order `next`/`plan` remains P1.

### License and attribution

MIT. The deterministic tool layer under `examprep_agent/tools/` is derived from
[universal-examprep-skill / Exam Cram Coach](https://github.com/ZeKaiNie/universal-examprep-skill)
(MIT, © ZeKaiNie); each ported module carries a header saying so. The bundled sample
course under `tests/fixtures/zh-data-structures/` also comes from that project.

---

## 中文

### 这是什么

`examprep-agent` 是一个**长期 + 短期双模式学习 Agent**：面向数月周期的长期备考/学习，
也面向数天后的期末考试突击。它把 `Exam Cram Coach` skill（MIT，© ZeKaiNie）已经验证过的
确定性内容管线（切章、题目抽取、BM25 检索、溯源与提示注入防护）改造成工具层，再在其上
构建自己的 Python Agent 内核、BKT 学习者模型与 FSRS 复习调度。

- **溯源防幻觉**：回答强制引用 `文件 p.页码`；材料外问题如实拒答，不编造。
- **材料即数据**：课程文件用内容派生 ID 的 fence 包裹（SHA-256 前缀），并启发式检测
  「忽略以上指令」类文本；检索工具结果与预置题库元数据也走同一防线。
- **长期记忆**：BKT 维护每个知识点的掌握概率；FSRS 按难度/稳定性/可检索性安排复习。
- **双模式**：距考试 ≥21 天自动进入长期模式（留存优先、掌握度门控），<21 天进入突击模式
  （真题密度优先、高强度测验），也可用 `--mode` 手动覆盖。掌握度门控真实拦截 `done`，
  可用 `examprep done --force` 显式跳过。

### 主要特性（P0）

- 工具层：PDF/DOCX/PPTX/MD/TXT/HTML 抽取、章节归并、题目/答案配对、BM25、配图裁剪、
  材料 fence 与注入检测、原子写 `study_state.json`。
- Agent 内核：OpenAI 兼容 provider（默认 DeepSeek，含纯离线单测）+ 零网络 `FakeProvider`；
  plan–act–observe 循环、工具注册表、最大步数与 token 预算硬闸门、JSONL 结构化事件日志。
- 上下文引擎 v1：token 估算、校验过总和的 system/material/history 三舱预算、增量压缩
  （旧历史摘要合并、最近尾巴原文保留）；循环在每次模型调用前自动压缩并记录 `compaction` 事件。
- 会话与检查点：学习状态 + BKT + FSRS 卡片 + `session.json` 原子落盘（fsync + replace）；
  预留会话分支字段。
- 学习层：真实 BKT 四参数更新与真实 FSRS-4.5 公式计算（非占位常数）；当前模式的目标留存率
  直接驱动 FSRS 间隔（长期 0.9、突击 0.8）。
- CLI：与 skill 等价的命令集，每条学习命令末尾带 `📍` 下一步引导；无模型也可运行确定性命令。
  `--profile` 保留 skill 的题量语义（small/standard/large = 1/3/5），并由模式测验强度封顶。

### 快速开始

```bash
python -m pip install -e ".[dev]"
examprep doctor

# 用材料文件夹建立工作区（30 天后考试 → 长期模式）
examprep setup ./materials --days 30
examprep status
examprep next            # 打印下一段材料；配置了模型时附带内核讲解
examprep quiz -n 3
examprep grade q006 B
examprep plan
```

工作区默认在 `<材料目录>/exam-cram/`；可用 `--workspace PATH` 或环境变量
`EXAMPREP_WORKSPACE` 指定其他位置。工作区若位于材料目录内，会被自动排除在材料扫描之外，
重复 `setup` 不会再把自己的产物当课程材料。配置模型（可选）：

```bash
export DEEPSEEK_API_KEY=...          # 或 OPENAI_API_KEY
export EXAMPREP_MODEL=deepseek-flash # 默认
export EXAMPREP_BASE_URL=https://api.deepseek.com  # 默认
```

未配置 key 时，`next` 会回退为材料切片并明确提示，不会假装有模型。测试全部离线：
`python -m pytest -q`（无网络、无真实 API key）。

### P0 状态

P0 交付地基：工程脚手架与 CI、移植后的确定性工具层、最小 Agent 内核、接入循环的上下文
压缩 v1、持久化会话、BKT + FSRS，以及与 skill 等价的学习循环 CLI。尚未实现（详见
[ARCHITECTURE.md](ARCHITECTURE.md)）：HTTP/MCP 接口、SQLite 长期记忆、IRT 难度校准、
先修知识图谱、并发/子 Agent，以及四层 benchmark。双模式的目标留存率与掌握度门控已在
P0 接入 CLI；用 FSRS 到期队列反哺 `next`/`plan` 的内容排序属于 P1。

### 许可证与致谢

MIT。`examprep_agent/tools/` 下的确定性工具层移植自
[universal-examprep-skill / Exam Cram Coach](https://github.com/ZeKaiNie/universal-examprep-skill)
（MIT，© ZeKaiNie），每个移植模块文件头均有说明。`tests/fixtures/zh-data-structures/`
样例课程同样来自该项目。
