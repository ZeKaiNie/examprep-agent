# P0 交付报告

- 仓库：`~/workspace/projects/examprep-agent/repo/`
- 日期：2026-10-30
- 范围：按 `P0_BRIEF.md` 完成 P0 全部交付物；`PLAN.md` 第 4 节的架构已落成可运行的 P0 子集。

## 1. 做了什么

### 1.1 工程脚手架

- `pyproject.toml`：包名 `examprep-agent`、模块 `examprep_agent`、`requires-python >=3.11`、
  运行时依赖仅 `pydantic>=2,<3`、可选组 `pdf = ["pypdfium2"]` / `dev = ["pytest"]`、
  console script `examprep = examprep_agent.cli:main`。
- `.gitignore`：Python 常规产物、工作区 `exam-cram/`、`*.local.json`、`logs/`。
- `.github/workflows/ci.yml`：push / PR 在 Python 3.11 与 3.12 上执行
  `pip install -e ".[dev]"` + `python -m pytest -q`。
- `README.md`：中英双语，定位（长期 + 突击双模式学习 Agent）、来源 skill 致谢与原仓库链接、
  特性、P0 状态、`pip install -e .` + `examprep doctor` 快速开始、模型环境变量说明。
- `ARCHITECTURE.md`（中文）：逐层映射 `PLAN.md` 架构到模块，标注 P0 已实现 / 接口预留。

### 1.2 工具层 `examprep_agent/tools/`

从 `research/skill-repo/skills/universal-exam-cram-coach/coach/` 移植：
`extract / chapters / questions / figures / guard / index / text / state`。

- 保持原确定性行为与安全语义：guard 的 fence/注入检测、index 的 BM25、state 的原子写与
  progress/notebook 视图、题目配对与自动判分、配图裁剪、页眉页脚剔除。
- 仅做包内相对导入、公开函数类型注解、模块 docstring 补全；未移植原 `coach/cli.py` 的界面逻辑。
- 每个移植文件头均注明：derived from universal-examprep-skill (MIT, (c) ZeKaiNie)。
- 额外增加了 `questions.import_question_banks()`：可导入材料目录里预置的
  `*quiz*bank*.json`（兼容 behavior-smoke 的 `references/quiz_bank.json` 结构），使
  `behavior_smoke/fixtures/mini_course` 也能完成 quiz 冒烟。

### 1.3 Agent 内核 `examprep_agent/core/`

- `provider.py`：`ModelProvider` Protocol、`ChatMessage/ToolSpec/ToolCall/ModelResponse/Usage`
  dataclass、`ProviderConfig`（pydantic v2）；`OpenAICompatibleProvider` 仅用标准库 HTTP，
  读取 `EXAMPREP_MODEL` / `EXAMPREP_BASE_URL` / `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`，
  默认 `deepseek-flash` + `https://api.deepseek.com`；`FakeProvider` 按脚本回放、零网络。
- `loop.py`：plan–act–observe 循环、`ToolRegistry`（name/schema/handler）、未知工具与工具异常
  转为观察、`max_steps` 与 `max_total_tokens` 双硬闸门（provider 未报 usage **或报 0** 时
  用 `estimate_tokens` 估算；超预算响应中的工具不执行）、工具观察截断后仍是合法 JSON、
  可选 `ContextEngine` 接线（每次模型调用前 `compact()` 并写 `compaction` 事件）、JSONL
  `EventLogger`（`session_start/model_response/tool_call/tool_result/final/budget_exhausted/
  provider_error/compaction`）。
- `context.py`：`estimate_tokens`、`ContextBudget` 三舱（system/material/history，三个比例
  必须和为 1）、`ContextEngine.compact()` 增量摘要合并 + 最近尾巴原文保留；system/material
  超预算会被确定性截断，单条超大消息也会被截断；摘要先做注入行清洗再以 fenced user 消息
  渲染（不进入 system）；有 provider 时用模型摘要，无 provider / provider 失败时用确定性
  截断摘要。
- `session.py`：`Session` 绑定 `study_state` + BKT + FSRS + 上下文引擎；按当前模式把
  `target_retention` 写入 FSRS（长期 0.9 / 突击 0.8），`apply_policy()` 支持切换模式；
  `study_state.json` 与 `session.json` 均原子写（tmp + fsync + `os.replace`）；`restore()`
  恢复；`branch` 字段（`id/parent/label/created`）已预留；`record_answer()` 统一写
  历史/错题/BKT/FSRS（`skip` 明确映射为 Hard，不计 lapse）。

### 1.4 学习层 `examprep_agent/learning/`

- `modes.py`：`StudyPolicy`、`LongTermPolicy`、`CramPolicy`；新学/复习配比、测验强度、
  推进掌握度阈值、目标留存率、每日新学上限均不同；`select_policy(days_left, override)`
  实现 ≥21 天长期、<21 天突击、无日期默认长期，支持 `--mode` 覆盖。目标留存率已接入
  FSRS，掌握度阈值已作为 `done` 的真实门控（`--force` 显式覆盖）。
- `learner.py`：真实四参数 BKT（先验/学习/猜测/失误），每次答题按后验 + 学习转移更新，
  支持序列化往返；P0 知识点粒度为章节（`chN`），接口接受任意 key。
- `scheduler.py`：FSRS-4.5 公开公式的真实实现：Difficulty / Stability / Retrievability、
  目标留存率反解间隔（默认 0.9）、Again/Hard/Good/Easy 更新、卡片存储与 JSON 往返。

### 1.5 CLI `examprep_agent/cli.py`

命令集：`setup / status / next / ask / quiz / check / grade / answer / done / note /
mistakes / cheatsheet / plan / goto / doctor`。

- `setup` 支持 `--mode longterm|cram|auto`，模式与选择原因写入 `study_state.json`；
  `--days/--date/--lang/--profile/--slice/--fresh/--workspace` 保留 skill 语义
  （`--profile` 的题量 small/standard/large = 1/3/5，由模式测验强度封顶）。
- 所有来自文件的文本（材料、题干、选项、答案、检索片段、错题回顾、小抄摘录）都经
  `guard.fence`；章节标题在 setup 时清洗；含注入文本的小抄摘录直接省略。预置题库的
  `id`/`type`/`source` 元数据经白名单清洗后才可出现在 fence 外；`search_materials` 工具
  返回的片段也 fence + flagged。
- 每条学习命令（含空题库的 `quiz`、无命中的 `ask`）最后一行是
  `📍 ... → examprep <下一条命令>`。
- 非 `--fresh` 的重复 `setup` 保留章节进度、BKT、FSRS、上下文摘要与计数器；`--fresh`
  才清空学习状态。工作区位于材料目录内时，扫描会排除该工作区，重复 setup 不会摄取
  自己生成的章节/题库/索引。
- 确定性命令不依赖 LLM；`next` 在配置 provider 时通过内核循环生成讲解
  （工具 `search_materials`，带上下文压缩与 `compaction` 事件，事件写入工作区
  `logs/agent-*.jsonl`），无 key 时输出材料切片并明确注明「回退为上面的材料切片」。

### 1.6 测试 `tests/`

共 140 个通过、1 个跳过。跳过项是 `test_pdf_is_readable_with_a_backend`
（`skipUnless(PDF_BACKEND)`）——它**需要** PDF 后端，当前容器未安装可选依赖
`pypdfium2`/`pypdf`，所以跳过；无后端分支的 `test_pdf_without_backend_is_reported_not_fatal`
反而在运行并通过。覆盖：

- 移植测试：guard（检测/变形/fence）、index+state、chapters、questions（抽取/配对/标签/
  预置 JSON 题库/判分）、text、extract（txt/docx/pptx/html/分类/页眉页脚/扫描）。
- CLI 端到端（结构化断言）：样例课程完整学习循环、每条命令 `rc == 0` 且末行 `📍`、
  无工作区错误、slice、模式自动选择与覆盖、预置题库导入、provider 驱动的内核讲解、
  注入防护与小抄省略、材料目录内自定义 workspace 重复 setup 不重摄取、预置题库元数据
  注入、空题库 footer、`--profile` 题量、cram 目标留存率、掌握度门控与 `--force`。
- 内核：FakeProvider 驱动一次「检索 → 出题 → 讲解+测验」剧本、工具调用/未知工具/参数错误、
  零 usage 不能绕过 token 闸门、超预算响应不执行工具、观察截断后仍是合法 JSON、
  上下文压缩接入调用前、JSONL 事件日志、provider 错误结构化停止。
- 上下文：token 估算、三舱比例与总和校验、压缩触发、尾巴原文保真、system/material 超预算
  截断、单条超大消息截断、tool 配对不拆散、摘要注入行清洗且不进入 system、增量合并、
  provider 失败回退。
- 学习层：双模式边界（21/20 天）、BKT 参考值与方向、FSRS-4.5 参考向量（首评/次评的
  S/D/interval）、权重持久化、间隔单调、目标留存率、连续 Good、Again 重置、skip→Hard。
- 会话：原子保存/恢复、目标留存率接线、BKT/FSRS/摘要/分支往返、损坏检查点降级、掌握度门控。
- provider（纯离线）：`ProviderConfig` 校验、`from_env`、消息 payload、请求构造、
  响应解析（缺失 usage / 坏 arguments / 坏 JSON）、HTTP 与传输错误。

## 2. 验收自测结果

### 2.1 测试套件

```
$ cd repo && python3 -m pytest -q
........................................................................ [ 51%]
.............................s.....................................      [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_tools_extract.py:58: no PDF backend installed
140 passed, 1 skipped in 1.32s
```

（跳过原因：该用例要求已安装 PDF 后端，本容器未装可选的 PDF 依赖；见 1.6。）

全部测试离线：`tests/conftest.py` 在每条用例前清除 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` /
`EXAMPREP_*` / `EXAM_CRAM_WORKSPACE`，并设置 `EXAMPREP_OFFLINE=1`；FakeProvider 零网络。

### 2.2 `examprep doctor`

```
$ examprep doctor
examprep-agent 0.1.0 | python 3.12.3 | Linux-7.0.0-39-generic-x86_64-with-glibc2.39
pydantic: 2.14.2
pdf: no backend (DOCX/PPTX/MD/TXT work; install 'examprep-agent[pdf]' for PDF)
provider: not configured (offline fallback; set DEEPSEEK_API_KEY or OPENAI_API_KEY)
workspace: (none)
offline tests: python -m pytest -q
```

（当前容器未安装可选 PDF 后端，因此 PDF 文本/配图不可用；DOCX/PPTX/MD/TXT 不受影响。
`pdf: no backend` 是如实报告，不是失败。）

### 2.3 样例课程冒烟

为遵守「不得修改 research/skill-repo 下任何文件」的红线，冒烟分两种方式跑：
（a）直接以只读的 `research/skill-repo/samples/zh-data-structures` 为材料目录，用
`--workspace /tmp/.../ws` 把工作区放到外部；（b）使用同一目录的临时副本，工作区在副本内。
两种方式结果一致，跑完后 `research/skill-repo` 的 `git status --short` 仍为空。

```
setup=0  status=0  next=0  ask=0  quiz=0

✅ 工作区已建好: /tmp/.../course/exam-cram
章节 (3):  1 线性表 [2 题] / 2 栈和队列 [6 题] / 3 树与二叉树 [4 题]
题目: 12 (10 有参考答案)
status: 课程: course | 语言: zh | 模式: longterm | 距考试 30 天
next:  第 1 章：线性表 (段 1/1)，材料 fenced，无 key 时输出回退说明
ask:   🟢 [ch2 | 第2章_栈和队列.md p.1 | ...]
quiz:  [q006] ch1 choice ...
```

另在 `full/benchmark/behavior_smoke/fixtures/mini_course` 的临时副本上验证：
`setup` 导入 `references/quiz_bank.json` 的 10 道题，`quiz` 成功输出 `[mc_q1]`，
`grade mc_q1 A` 返回正确并记录。

## 3. 已知缺口（如实）

1. **PDF 未在本容器验证**：未安装可选 `pypdfium2`/`pypdf`，因此需要后端的 PDF 测试
   （`test_pdf_is_readable_with_a_backend`，`skipUnless(PDF_BACKEND)`）被跳过；无后端分支
   `test_pdf_without_backend_is_reported_not_fatal` 正常运行并通过。代码路径与 skill 一致，
   装了 extras 即可用。
2. **BKT 粒度为章节**：P0 以 `chN` 作为知识点 key；更细粒度（小节/题目/概念）与先修图谱属于 P1。
3. **调度参数尚未全部参与决策**：`target_retention` 与 `advance_mastery` 已接线；但
   `new_vs_review` / `daily_new_limit` / `review_first` 仍只是持久化的策略字段，P0 的
   `next`/`plan` 仍按章节顺序推进。FSRS 到期队列反哺内容排序是 P1 的首要接线工作。
4. **上下文压缩只接入讲解路径**：`run_agent(context=...)` 已在每次模型调用前压缩并记录事件，
   `next` 的讲解走这条路径；其他确定性命令本身不调用模型，因此没有长会话上下文需要压缩。
   多轮苏格拉底式的长期对话上下文仍待 P1。
5. **CLI 未实现 skill 的 `chapter/figures/figure/export` 四个辅助命令**：任务书列出的命令集
   已全部实现；这四个未在 P0 命令清单内。
6. **讲解为单轮 agent 输出**：有 provider 时走内核循环 + `search_materials`，但没有多轮苏格拉底
   追问/提示阶梯；引用目前靠系统提示 + 工具出处约束，尚无逐条事实断言的自动校验器。
7. **OpenAI 兼容 HTTP 路径未对真实端点联调**：按离线要求没有网络/真实 key 测试。现在已有
   纯离线单测覆盖 `ProviderConfig` / `from_env` / `_message_payload` / 请求构造 / 响应解析 /
   HTTP 与传输错误；真实端点联调仍留给 P1。
8. **默认工作区写在材料目录下**（继承 skill）：若材料目录只读，需要显式传
   `--workspace` 或 `EXAMPREP_WORKSPACE`。本次冒烟因此使用副本。自定义 workspace 位于
   材料目录内时已从扫描中排除，重复 setup 不再重摄取自己的产物。
9. **figures 测试尚未移植**：原 skill 的 `tests/test_figures.py` 未对应到新仓库；
   `figures.py` 的 PNG 编码、聚类、裁剪目前只被 CLI setup 间接覆盖。P0 不强制，P1 补齐。
10. **原子写没有多进程锁**：state/session/index 已加 `flush + os.fsync + os.replace`，
    但固定 `.tmp` 名不协调并发写；P1 视并发需求加锁或唯一临时文件。
11. **未做**（P1+）：HTTP/MCP/Web 接口、SQLite 长期记忆、IRT、先修知识图谱、并发摄取/子 agent、
    OpenTelemetry/成本核算/Docker、四层 benchmark。

## 4. 下一步建议

1. **P1 调度接线**：把 FSRS 到期卡片接入 `plan`/`next` 的每日队列；BKT 掌握度细化到知识点，
   让到期复习优先于新内容。
2. **多轮上下文**：`run_agent` 已在每次模型调用前压缩并记录事件；P1 增加多轮苏格拉底对话
   与跨命令的会话摘要持久化回归。
3. **引用校验器**：对模型输出逐条抽取 `file p.N` / 事实断言，校验文件存在、页码存在、片段可检索，
   失败则降级为「材料未覆盖」。
4. **真实 PDF 与多格式回归**：CI 增加 `.[pdf]` 矩阵或单独 job；补充 PPTX/DOCX 含图用例。
5. **接口层**：按 PLAN 顺序做 MCP Server（先只读工具）→ FastAPI → 异步任务池；每层复用
   现有 `Session`/`ToolRegistry`，不重写工具层。
6. **评测**：先把 behavior_smoke 的断言迁移成 pytest/基准脚本，再做 GroundingBench 三臂对照；
   长程模拟学习者留到 P3。

## 5. 红线与合规

- `research/skill-repo/` 未做任何修改（该仓库 `git status --short` 为空）；样例课程只做了
  只读复制到测试 fixture。
- 未执行 `git commit`、未执行 `git push`；所有改动仅存在于工作区（未提交）。
- 运行时依赖仅 pydantic；`pypdfium2` 仅在可选 `pdf` 组中出现。

## 6. 复现

```bash
cd ~/workspace/projects/examprep-agent/repo
python -m pip install -e ".[dev]"        # 或已安装时跳过
python -m pytest -q
examprep doctor

# 样例冒烟（在副本上，避免写入只读材料目录）
cp -r ../research/skill-repo/samples/zh-data-structures /tmp/course
examprep setup /tmp/course --days 30 --workspace /tmp/course/exam-cram
examprep status --workspace /tmp/course/exam-cram
examprep next --workspace /tmp/course/exam-cram
examprep ask "循环队列 判满" --workspace /tmp/course/exam-cram
examprep quiz -n 3 --workspace /tmp/course/exam-cram
```

## 7. 审阅修复记录（REVIEW_P0）

本节记录 `REVIEW_P0.md` 全部【阻断】【重要】项的修复与对应回归测试。每条都针对
“旧实现会失败”的行为补了用例；其中 B1、B2、I1、I7 另用审阅报告的复现步骤在修复前
确认过失败、修复后确认通过（见 7.4）。

### 7.1 阻断

| 编号 | 修复 | 回归测试 |
| --- | --- | --- |
| **B1** token 闸门可绕过 | `response.usage is None` **或** `total_tokens <= 0` 都视为缺少 usage，改用 `estimate_tokens`；`spent` 累加后若已达/超预算，该响应中的工具一律不执行，直接 `budget_exhausted`；provider 在响应无 `usage` 字段时返回 `None` | `test_zero_usage_from_the_provider_cannot_bypass_the_token_gate`、`test_empty_prompts_with_zero_usage_still_cost_a_token`、`test_an_over_budget_response_cannot_run_tools_with_side_effects`、更新后的 `test_token_budget_gate_stops_the_run`；`tests/test_provider.py::test_missing_usage_is_reported_as_none` |
| **B2** 非 `--fresh` setup 清空 BKT/FSRS | `cmd_setup` 的 `keep_old` 分支复制 `learner`/`reviews`，并恢复 `session.json` 的滚动摘要、计数器和分支元数据 | `test_full_session` 在 re-setup 后断言 `learner` 与 `reviews.cards` 完全保留，`--fresh` 才清空；`test_cram_mode_wires_target_retention_into_fsrs` |

### 7.2 重要

| 编号 | 修复 | 回归测试 |
| --- | --- | --- |
| **I1** workspace 被当材料重摄取 | `extract.list_files/scan` 与 `questions._find_json_banks/import_question_banks` 接受动态 `exclude_dirs`，`cmd_setup` 传入实际 workspace 绝对路径 | `test_workspace_inside_materials_is_not_reingested`（连续 3 次 setup 仍为 3 章、题数 `[2,6,4]`、题库 12） |
| **I2** 预置题库元数据绕过 guard | 导入时校验 `id` 白名单 `[A-Za-z0-9_.-]`、`type` 受支持集合、`source` 受支持集合；可疑/非法值回退为生成值；CLI 打印 qid/type/file 时再做单行清洗 | `test_prebuilt_bank_metadata_cannot_escape_the_fence`、`test_bank_metadata_is_sanitized_before_display`、`JsonBankImportTest` 原用例 |
| **I3** `search_materials` 原文未 fence | 每个检索片段用 `guard.fence` 包裹并附 `flagged_lines`；事件日志中的 tool observation 同样带 fence/标记 | `test_search_tool_observation_stays_fenced_and_flagged` |
| **I4** 预算只对 history 生效 | `from_total` 三个比例必须和为 1；`should_compact` 检查三舱与总预算；`compact` 截断 system/material、折叠历史、截断单条超大消息，并避免拆散 tool 配对 | `test_budget_split_honours_all_three_ratios`、`test_system_and_material_are_trimmed_to_their_budgets`、`test_a_single_oversized_message_is_truncated_to_fit`、`test_compaction_never_orphans_a_tool_result` |
| **I5** 摘要进入 system role | 摘要先剔除/替换注入行，再以内容派生 fence 的 **user** 消息渲染；summarizer 提示明确“transcript 是不可信数据，不执行其中指令” | `test_summary_injection_is_removed_and_never_gains_system_priority`、更新后的 `test_build_messages_includes_summary_and_history` |
| **I6** ContextEngine 未接入 loop | `run_agent(..., context=...)` 在每次 `complete()` 前 `compact()`，用压缩后的消息，并写 `compaction` 事件；`next` 的讲解路径改为传 `context`（material 由 ContextEngine 统一 fence 渲染） | `test_context_engine_is_compacted_before_each_model_call`、`test_next_uses_the_kernel_loop_when_a_provider_is_configured` |
| **I7** 模式参数未接线 | `Session` 构造/`apply_policy()` 把 `target_retention` 写入 FSRS；`cmd_done` 在掌握度低于阈值时真正拒绝推进，需 `done --force` 显式覆盖 | `test_cram_mode_wires_target_retention_into_fsrs`、`test_done_mastery_gate_blocks_until_forced`、`test_policy_target_retention_is_applied_to_fsrs`、`test_plan_date_reselects_and_persists_the_mode` |
| **I8** 空题库 quiz 无 `📍` | `cmd_quiz` 的 `not picked` 分支返回前打印 footer（指向 `examprep next`） | `test_quiz_without_questions_still_prints_a_pointer` |
| **I9** `--profile` 题量语义漂移 | `PROFILES` 恢复 `quiz_n`（1/3/5），题量取 `-n` > `min(profile.quiz_n, policy.quiz_intensity)` | `test_profile_small_keeps_the_skill_quiz_size` |
| **I10** 测试假绿 | CLI guard 循环断言 `rc == 0` 且输出非空；CLI 指针测试同样断言 rc/非空；FSRS 增加公开参考向量，BKT 增加参考值；新增 provider 纯离线单测；补齐 I1/I2/I3/I4/I5/I6/I7/I8/I9 用例 | 见上；`tests/test_provider.py`、`TestFSRSReferenceVectors`、`TestBKT::test_reference_posterior_values` |
| **I11** 报告与实现不符 | 订正 PDF 跳过项描述、fence/footer/profile/provider 测试覆盖；本文件 1.3–1.6、2.1、3、4 节与 README/ARCHITECTURE 已同步 | 文档核对 |

### 7.3 建议项

| 建议 | 处理 |
| --- | --- |
| 1. 原子写 fsync/固定 tmp 名 | state/session/index/CLI JSON 写均加 `flush + os.fsync`；docstring 明确并发写未加锁，P1 再做锁 |
| 2. FSRS weights 持久化 | `to_dict()` 保存 `weights`，`from_dict()` 校验 ≥17 并恢复；`test_custom_weights_survive_serialization` |
| 3. `skip` → Hard | 行为保留，在 `Session.record_answer` docstring/ARCHITECTURE 明确，并加 `test_skip_maps_to_hard_without_a_lapse` |
| 4. 观察截断破坏 JSON | 改为截断内层 `result` 并标记 `"truncated": true`，始终合法 JSON；`test_oversized_observation_is_valid_json_and_marked_truncated` |
| 5. `ask` 无命中 footer 指向自身 | 改为指向 `examprep next` |
| 6. figures 测试缺失 | 未修，登记为 P1（见 3.9） |
| 7. “随机 fence”表述 | README 改为“内容派生 ID（SHA-256 前缀）” |
| 8. README/ARCH gate/fence 表述 | 已按真实实现重写（真门控 + `--force`；检索/题库元数据边界） |

### 7.4 复验

```
$ python3 -m pytest -q
140 passed, 1 skipped in 1.32s

$ python3 - <<'PY'  # REVIEW_P0 的 B1/B2/I1/I7 复现脚本
...
B1 zero-usage: token_budget calls 1 usage 74
B1 side-effects: [] stopped token_budget ['session_start', 'model_response', 'budget_exhausted']
B2 learner preserved: True cards preserved: True mastery 0.1932
I1 attempt 1/2/3: chapters 3 [2, 6, 4] bank 12
I7 setup retention: 0.8
I7 persisted retention: 0.8
PY
```

红线复核：`research/skill-repo/` 未修改；未执行 `git commit` / `git push`。
