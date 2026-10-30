# P2 报告：并发与服务化

日期：2026-10-30
基线：P1 commit `f2eeafc`（267 passed, 1 skipped）
验收：`/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m pytest -q`
→ **325 passed, 1 skipped**（跳过项：本机未安装 PDF 后端，与 P1 相同）

> **审阅整改（REVIEW_P2，2026-10-30）**：本报告已按 `REVIEW_P2.md` 修复 B1、I1–I8，
> 并尽量修复 S1–S6；所有与实现不符的旧表述已同步订正。逐项证据见 §1.1，I2 的
> 全写路径审计清单见 §1.2。
红线：未修改 `research/skill-repo/`；未 commit、未 push；核心运行时依赖仍只有 `pydantic`，
新增依赖全部是可选 extras（`serve` / `mcp` / `spike`），缺 extras 时 import 安全并给出安装提示。

本报告按 `P2_BRIEF.md` 的 8 个交付项逐项对照实现与测试，随后给出选型理由、spike 摘要、
P1 §5.1 延期项的处置与仍未做项。

---

## 1. 交付对照

| # | 任务书要求 | 实现 | 测试 |
| --- | --- | --- | --- |
| 1 | 还 P1 延期债：安全文件工具接入生产 CLI agent 注册表，全程过闸门，文件动作有审计 | `cli._explain_tools` 在真实教学循环注册 `search_materials` + `SecureFiles` 的 `read_file/list_dir/make_dir/write_file/delete_path`；风险为 L0/L1，`path_arguments` 显式声明；`ToolRegistry.execute` 先过 `PermissionGate` 再执行，allow/deny 均写 `audit-*.jsonl`；材料只读、工作区外需 L2 授权、引擎控制文件（`study_state.json`/`memory/`/`audit/`…）模型只读；删除仍只进 `.trash/` | `tests/test_p2_acceptance.py`（真实 CLI `next` + FakeProvider：一次读 + 一次写均 allow 且有审计；越界写/删控制文件 deny 且无副作用）；`tests/test_adversarial.py` 更新为 P2 边界（注册了写工具但诱导仍被拦截） |
| 2 | 核心服务层抽取，CLI 薄封装，FastAPI/MCP 共用 | 新增 `examprep_agent/service/`：`study.StudyService`（setup/status/plan/next/ask/quiz/check/grade/answer/done/note/mistakes/cheatsheet/goto/graph/permissions/costs）、`models.py`（pydantic 结构化输入输出）、`errors.py`（CLI 退出码 ↔ HTTP 语义）、`ingest.py`、`subagent.py`、`costs.py`。`cli.py` 只保留双语措辞与终端渲染，所有状态/调度/持久化决策走 service | `tests/test_service.py`（结果字段、CLI 渲染一致性、workspace 隔离、结构化错误）；既有 267 个用例全部保持通过 |
| 3 | 摄取管线并行化；结果与串行一致；单文件失败隔离；进度 JSONL | `service/ingest.py` 用 **ThreadPoolExecutor** 并行：逐文件 extract、逐文件 chapter plan、逐文件 figure、逐 chapter index chunk；合并始终按文件/章节原顺序，`workers=1` 与 `workers=N` 的**业务产物等价**（`chapters.json`/`quiz_bank.json`/`index.json`/`figures.json`/chapter md；`study_state.json`/`session.json`/日志含时间戳或耗时，本就不逐字节一致）。逐文件/逐章节异常隔离覆盖 extract、chapter plan、figures、index chunk 四类，分别记为 `Source.error`、`chapter_plan_failed`、`figures_failed`、`chunks_failed`；`figures.json` 输出路径带相对路径哈希，同名文件不再互相覆盖。逐文件事件写 `<ws>/logs/ingest-YYYY-MM-DD.jsonl`（start/file_extracted/chapter_plan_failed/chunks_failed/ingest_done，含耗时、错误） | `tests/test_ingest_parallel.py`（串行/并行 source、chapters、index、完整 setup 业务产物等价；坏 docx、chapter plan 失败、chunk 失败均不拖垮批次；`a.docx` 与 `sub/a.docx` 配图不冲突且串行/并行一致；进度事件数量与错误字段） |
| 4 | 子 agent 隔离检索/判分：独立上下文预算、只回传摘要、工具过闸门且风险不越父级、摘要过 guard | `service/subagent.py`：每次运行新建独立 `ContextEngine`（不复用父历史/材料），只返回 `SubAgentResult.summary`；`sanitize_summary` 先 `guard.neutralize` 去 fence 伪造，再用**换行归一化后的跨行扫描**删除整句（两行/三行/空行分段/中文跨行），删除后复扫，仍可疑则整段丢弃，最后硬截断；`ToolRegistry(max_risk=…)` + `.scoped()` 双保险——子注册表不暴露高于父级的工具，父级已带更低的 `max_risk` 时子级再取 min，即使直接注册也在执行前以 `risk_exceeds_parent` 拒绝并写审计；摘要进入 `AskResult.subagent_summary`/`GradeResult.feedback` 前统一 `guard.fence`；判分结论仍由确定性 `grade_answer` 决定 | `tests/test_subagent.py`（父级 L1/L0 下高一级工具不可见且不可执行、审计含 `risk_exceeds_parent`；两行/三行/空行/中文拆分注入被整句删除，operand 不回流；compaction 用量计入预算；无父上下文泄漏且预算独立；检索子 agent 只用 L0 工具；判分反馈不改变确定性 verdict） |
| 5 | FastAPI（extras `serve`），核心 REST、按 workspace 隔离、错误码与 CLI 一致、无 extras 可 import 并提示 | `interfaces/http.py`：`create_app()` 提供 `/setup /status /plan /next /ask /quiz /check/{qid} /answer /grade /done /note /mistakes /cheatsheet /goto /graph /permissions /costs /health`；每次请求按 workspace 加载持久化 Session，**写路径持 workspace 文件锁**（I5），无进程级共享会话；service 的 `exit_code` 经 `http_status_for` 映射（2→400，3/4→404），响应体保留 `exit_code`/`code`；Pydantic `RequestValidationError` 也映射为 `{ok:false, code:"validation_error", exit_code:2}`（S2）；`/ask` 的 `chars` 在 service 层生效（S1）；顶层 import 不依赖 fastapi，`create_app` 缺 extra 时抛 `ExtrasMissing("serve", "pip install 'examprep-agent[serve]'")`；`examprep serve` / `examprep-serve` 启动 uvicorn（I8） | `tests/test_http_api.py`（TestClient 冒烟：setup→status/plan/next/ask/quiz/answer/check/costs；400/404 语义；Pydantic 校验错误 400/exit 2；`chars` 截断；全部 HTTP 写路径审计；**真实子进程启动 `examprep serve` 并请求 `/health`**；缺 extras 安装提示）；无 extras 环境自动 skip |
| 6 | MCP Server（extras `mcp`），stdio，暴露指定工具，全部走 service + 闸门，离线冒烟 | `interfaces/mcp_server.py`：MCP 2.x `MCPServer`（兼容 1.x `FastMCP` 导入路径），`run(transport="stdio")`；`python -m examprep_agent.interfaces.mcp_server`、`examprep mcp`、`examprep-mcp` 三种入口（I8）；工具 `search_materials/get_status/get_plan/get_next/ask/start_quiz/submit_answer` 全部调用 `StudyService`，并传 `audit_tool="mcp.*"` 让 `PermissionGate` 记录审计（`count_throttle=False`：这是语义学习操作，不是任意文件写；该例外在 §1.2 明确列出）；材料文本出站前 `guard.fence`；`k`/`n` 非法值返回结构化错误而不是 Python 切片语义（S3） | `tests/test_mcp_server.py`（内存 transport 完成真实握手 → `tools/list` 精确等于 7 个工具；`get_status` 只读调用；`ask` 由 FakeProvider 驱动摘要且返回 fenced 文本；`submit_answer` 落盘并在审计中出现 `mcp.submit_answer`；负 `n` 返回 `validation_error`、负 `k` 被钳制；**真实子进程 `python -m ...mcp_server` 完成 stdio 握手**；缺 extras 提示） |
| 7 | LangGraph spike（§5.5），同组长会话恢复/并发工具/compaction 用例对比，产出 `SPIKE_REPORT.md`，代码隔离 `spikes/`，只装 venv | `spikes/`：`common.py`（共享负载/预算/判据/依赖统计）、`kernel_adapter.py`、`langgraph_adapter.py`、`bench.py`；20 次重复、FakeProvider 离线。**审阅后重做**：两边同负载同步数（12/40/3）、同预算与判据、JSON 状态序列化恢复；结果见 `SPIKE_REPORT.md`：三个用例成功率两边均 100%；LangGraph 只在并发 fan-out 快 2.8×，同口径小步骤延迟 8.8–9.3×、38 个已安装传递依赖、预算/摘要/审计仍须手写。**结论：保持自研**，借鉴 fan-out 模式、不引入依赖 | `tests/test_context.py` 覆盖增量确定性摘要保留最早事实；手工运行 `python -m spikes.bench --repeats 20`；`spikes/results.json` 留档。spike 不在主包、不在默认依赖 |
| 8 | Tracing 与成本核算：按会话/命令聚合 token 与成本，单价可配置，`examprep costs`，离线 | `service/costs.py` 读现有 `logs/agent-*.jsonl` 的 `model_response` **与 `compaction`** usage；按 `(session_id, command)` 分组输出、按命令聚合（`CommandCost.sessions` 是 distinct session 数）；命令来自事件 `command` 字段（`EventLogger` 新增 defaults，教学循环写入 `command=next`、`model`），旧日志回退从 session id 推断；价格优先级 CLI/HTTP 参数 → `EXAMPREP_PRICE_INPUT_PER_MTOK`/`OUTPUT` → 文档化默认值；单价必须有限非负；CLI 新增 `examprep costs [--input-price --output-price --command --json]`，不联网 | `tests/test_costs.py`（手写日志精确数值/按命令过滤/CLI 文本与 `--json`/distinct session/1e999 与 NaN/真实教学循环日志被聚合） |

### 1.1 REVIEW_P2 整改对照

| 项 | 处置 | 回归测试 |
| --- | --- | --- |
| B1 跨行摘要注入 | `guard.flagged_lines` 先在“换行归一化”的两份视图（英文以空格连接、CJK 直接连接）上扫描，再把命中句扩展到整句、返回所有被触及行；`sanitize_text` 反复扫描直到稳定，仍可疑则整段丢弃；`sanitize_summary` 与 `ContextEngine` 摘要共用该实现；`AskResult.subagent_summary`/`GradeResult.feedback` 出站前 `guard.fence` | `test_tools_guard.py`（两行/三行/空行/中文拆分、干净后继句保留）；`test_subagent.py`（两行/三行/空行/中文摘要、`run_subagent` 端到端） |
| I1 compaction 预算 | `run_agent` 把 `compaction` 事件的 provider usage 计入 `spent`，超预算立即 `token_budget` 停止；`LoopResult.usage` 汇总 `model_response + compaction` | `test_kernel_loop.py`（父循环 compaction 超预算不发起下一步）；`test_subagent.py`（子 agent `tokens >= 10k` 且 stopped=`token_budget`） |
| I2 全写路径闸门/审计 | CLI/HTTP/MCP 全部传 `audit_tool`；service 的 done/note/cheatsheet/goto/graph/permissions/mistakes/plan/next/setup 均接受并记录；cheatsheet 的 `check_path` 使用入口名；详见 §1.2 | `test_service.py`（签名覆盖 + 11 条写路径审计）；`test_cli_session.py`（`cli.*`）；`test_http_api.py`（`http.*`）；`test_adversarial.py`（setup allow + 攻击 deny） |
| I3 摄取失败隔离 | chapter plan 失败记 `chapter_plan_failed` 并跳过该 source；chunk 失败记 `chunks_failed` 并跳过该 chapter；extract/figures 原有隔离保留 | `test_ingest_parallel.py`（plan/chunk 各注入一次异常，其余文件仍产出） |
| I4 同名文件配图冲突 | `safe_stem` 追加完整相对路径的 SHA-1 短哈希；合并阶段再做路径冲突检测（冲突记录 warning 并丢弃后写者） | `test_ingest_parallel.py`（`a.docx` + `sub/a.docx`，串行/并行记录一致、两个不同路径与字节） |
| I5 HTTP 并发丢更新 | 新增 `security/locking.py`（`flock`/`msvcrt`，30s 超时报 `workspace_busy`）；service 的所有 read-modify-write 方法（setup/plan/next/grade/answer/done/note/cheatsheet/goto/graph/permissions）在锁内 load→save | `test_service.py`（两个线程并发 `answer`，两条 history 均保留） |
| I6 graph 部分失败 | 先记原边与原 `graph_edits`，任一 edit 失败则回滚内存图/编辑列表、不 save、`changed=False`、不返回成功项 | `test_service.py`（`ch1:ch3` + `ch2:ch99` 后磁盘图与 `graph_edits` 不变） |
| I7 spike 口径 | **审阅后重做**：共享负载/预算/判据、同调用次数、JSON 状态序列化恢复、marker 生效的已安装依赖统计；结论只保留数据支持的部分 | `spikes/results.json`；`test_context.py`（增量确定性摘要保留最早事实，重做中发现的问题） |
| I8 启动入口 | `python -m examprep_agent.interfaces.mcp_server`、`examprep mcp`、`examprep-mcp`；`examprep serve`、`examprep serve --port`、`examprep-serve`；真实子进程冒烟 | `test_mcp_server.py`（stdio 子进程握手）；`test_http_api.py`（uvicorn 子进程 `/health`） |
| S1 `/ask chars` | 在 `StudyService.ask` 统一 `shorten`，HTTP 与 CLI 同语义 | `test_http_api.py`（chars=10 全部 ≤10，默认不截断） |
| S2 校验错误模型 | `RequestValidationError` → 400 `{ok:false, code:"validation_error", exit_code:2}` | `test_http_api.py`（非法 result、负 n） |
| S3 数值边界 | `QuizRequest.n` 加 `ge=1, le=200`；service `ask` 钳制 `k∈[1,50]`；MCP 显式钳制 k、捕获 pydantic `ValueError` | `test_http_api.py`、`test_mcp_server.py` |
| S4 costs 口径 | `CommandCost.sessions` 改为 distinct session 数；usage 捕获 `OverflowError`/NaN；单价要求有限；报告措辞订正 | `test_costs.py`（distinct sessions、1e999/NaN、inf 单价 CLI exit 2） |
| S5 CLI 回归 | `next` 恢复 examples 前空行；`permissions revoke` 空 target 不再多冒号；`study_state.json` 恢复 P1 `mode_reason` 字段 | `test_cli_session.py`（三条） |
| S6 表述/覆盖 | 报告改为“业务产物等价”、补 plan/chunk 隔离、跨行摘要、全写路径审计；测试补父级 ceiling 夹紧与 MCP 工具精确集合 | `test_subagent.py`、`test_mcp_server.py`、本报告 |

### 1.2 写路径闸门/审计清单（I2）

“模型写配额”（写熔断）只约束模型可控制的任意文件写；host 的语义写以同一
`PermissionGate` 记录 allow/deny，但 `count_throttle=False`，这是有意的例外。

| 入口 / 命令 | service 方法 | `audit_tool` | access | 记录方式 |
| --- | --- | --- | --- | --- |
| CLI `setup` | `setup` | `cli.setup` | write | `_audit_tool`（L1，不占模型写配额） |
| CLI `status/plan/next/ask/quiz/check/grade/answer/done/note/mistakes/cheatsheet/goto/graph/permissions` | 同名方法 | `cli.<command>` | 读路径 read；plan/next/grade/answer/done/note/cheatsheet/goto/graph/permissions 为 write | `_audit_tool` / `check_path` |
| CLI `next` 的模型讲解写回（counters/memory/session） | `_run_explanation` → `session.save()` | `cli.explain` | write | `gate.check(..., count_throttle=False)` |
| CLI `costs` | `costs` | —（无需加载 Session） | 只读日志 | 无闸门；已在 §5 说明 |
| HTTP `POST /setup` | `setup` | `http.setup` | write | `_audit_tool` |
| HTTP `GET /status /plan /next /ask /check /mistakes /costs` | 同名方法 | `http.status/plan/next/ask/check/mistakes`；costs 无 | read | `_audit_tool` |
| HTTP `POST /quiz /answer /grade /done /note /cheatsheet /goto /graph /permissions` | 同名方法 | `http.<command>` | quiz read；其余 write | `_audit_tool` / `check_path` |
| HTTP `GET /permissions` | `permissions(action=list)` | `http.permissions` | read | `_audit_tool` |
| MCP 7 工具 | `ask/status/plan/next/quiz/answer` | `mcp.<tool>` | 读工具 read；`submit_answer` write | `_audit_tool`，`count_throttle=False` |
| 生产 agent 文件工具 | `search_materials/read_file/list_dir/make_dir/write_file/delete_path` | 工具名 | L0 read / L1 write | `ToolRegistry` → `PermissionGate`；写操作计入写熔断 |
| 子 agent 工具 | 同父级注册表（`.scoped()`） | 工具名 | ≤ 父级 ceiling | 同上；越级写 `risk_exceeds_parent` 审计 |

MCP `submit_answer`、HTTP/cli 的语义写属于**受信 host 写**：它们不消耗模型写配额，
但每一次都有 `permission_decision` 记录；这与 P1 报告 §4.11 的口径一致。

### 验收标准逐条

- `pytest` 全绿：**325 passed, 1 skipped**。
- 并行摄取与串行**业务产物**等价、extract/plan/figures/chunk 四类失败隔离：`test_ingest_parallel.py`（完整 setup 的 chapters/quiz_bank/index/figures/chapter md 比较；含时间戳的文件不参与逐字节比较）。
- 子 agent 摘要过 guard（含跨行/分段）且子级权限不越父级：`test_subagent.py`。
- service 层与 CLI 输出一致性：`test_service.py`。
- FastAPI TestClient 冒烟 + 真实 `examprep serve` 子进程启动（缺 extras skip）：`test_http_api.py`。
- MCP 工具精确清单、只读/写调用与真实 `python -m` stdio 子进程握手（缺 extras skip）：`test_mcp_server.py`。
- costs 聚合数值正确：`test_costs.py`。
- 端到端文件工具：`test_p2_acceptance.py`（真实 CLI `next` + FakeProvider，读到材料、写入工作区，两条 allow 审计；越界写与控制文件删除被拒且无副作用）。
- 并发写不丢更新：`test_service.py::test_concurrent_answers_do_not_lose_an_update`。

---

## 2. 选型理由

### 2.1 并行摄取：线程池而非 asyncio / 进程池

- 抽取器是**阻塞式 stdlib/第三方调用**（`zipfile`、XML、可选 `pypdfium2`）。asyncio
  包装只会把工作丢回 executor，多一层调度；线程池直接表达并行。
- 进程池需要 pickle 整个 `Source`/`Chapter`，并让 PDF backend 句柄跨进程，复杂且
  对可选依赖不友好；线程共享已加载模块，输出文件天然按文件隔离。
- 结果确定性来自“**并行算独立片段，主线程按原顺序合并**”，而不是让完成顺序决定
  产物。题目抽取仍顺序执行：解答文件与题目文件是**跨文件配对**（`hw2` ↔
  `hw2solutions`），单文件不是合适的工作单元；这部分在报告 §4 登记。

### 2.2 服务层边界

- service 拥有业务决策与持久化；CLI 只做参数解析、双语措辞、终端排版、`📍` 引导。
- FastAPI/MCP 与 CLI 调同一组方法；HTTP 与 MCP 不复制任何调度/状态逻辑。
- 预期失败用**结构化结果 + `exit_code`**（无命中=4、无题=3）或 `ServiceError`
  （未知题/章=2），CLI 原样返回退出码，HTTP 用同一语义映射，MCP 返回同构 JSON。

### 2.3 Spike 结论（审阅后重做，摘要，详见 `SPIKE_REPORT.md`）

- 口径：两边同负载、同步数（resume 12+1、compaction 40、parallel 3）、同预算
  （800/120/360/320，摘要 112，keep 4）、同成功判据；重启用 JSON 状态序列化恢复
  （未验证 LangGraph 生产 checkpointer 后端）。
- 成功率：三个用例两边均 100%（20/20）。
- 同口径小步骤延迟：resume 自研 4.62 ms vs LangGraph 40.46 ms（8.8×）；compaction
  11.54 ms vs 107.12 ms（9.3×）。
- 并发工具：LangGraph `Send` 54.47 ms vs 自研顺序 151.06 ms（2.8× 快）——唯一实质优势。
- 体积：LangGraph 引入 **38** 个已安装、marker 生效的传递 distribution；自研核心
  1,390 行已在仓库内（adapter 144 vs 246 行，不是等价实现成本）。
- **建议：保持自研**。若要并发工具，在 `core/loop.py` 用 `asyncio.gather`/线程池实现
  受控并行（仍逐调用过闸门与审计），不引入框架。

---

## 3. 安全与能力面变化（如实）

- 生产教学循环从“只有只读 search”变为“只读 search + 受限文件工具”。这是 P2 明确
  要求的能力变化，护栏如下：
  - 材料目录永远只读（`materials_read_only`）；
  - 引擎控制文件（`study_state.json`、`memory/`、`audit/`、`.snapshots/`、`.trash/`、
    `logs/` 等）对模型写/删只读（`internal_path_read_only`）；
  - 工作区外写入/读取转 L2，需用户显式 grant；未授权返回 `outside_write_needs_grant`；
  - `delete_path` 仍只移动到 `.trash/`，不做真删；
  - 所有文件调用经 `ToolRegistry` 的 `PermissionGate`，写操作计入写熔断；
  - 审计 JSONL 记录 allow/deny、风险级、解析后的路径。
- 对抗测试更新为 P2 边界：模型能看到写工具，但毒材料诱导的越界写/控制文件删除仍
  100% 拒绝且无副作用（`tests/test_adversarial.py`、`tests/test_p2_acceptance.py`）。
- MCP 的 `submit_answer` 等语义操作是**受信 host 写**（原子写+快照），不消耗模型写
  配额，但会以 `mcp.submit_answer` 记一条权限审计；这与 P1 报告 §4.11 的口径一致。

---

## 4. P1 §5.1 延期项处置

| P1 §5.1 项 | P2 处置 |
| --- | --- |
| 把 `SecureFiles`/`register_secure_file_tools` 接入生产 CLI agent 注册表 | **已完成**（交付项 1，端到端测试） |
| 主机侧关键写入纳入统一写审计/熔断（I9） | **仍延期（审计已覆盖，配额仍分离）**。P2 任务书未要求，且需要先定义“主机写/模型写”的配额语义，避免 setup/memory 自检打满熔断。本期 CLI/HTTP/MCP 的语义写（含 setup/plan/next/done/note/cheatsheet/goto/graph/permissions）全部写入同一 `permission_decision` 审计；它们作为受信 host 写不消耗模型写配额（§1.2 逐路径列出）。触发条件：多实例/远程客户端大量写同一 workspace。 |
| `openat`/`O_NOFOLLOW` 消除符号链接 TOCTOU | **仍延期**。跨平台实现成本高；现有 realpath fence 已阻止正常路径逃逸。触发条件：引入 exec 工具或不可信并发写者。 |
| 消费 `new_vs_review` 比例（I5 遗留） | **仍延期**。需要每日时间预算/题量模型配合；本期未改变调度语义。 |
| 审计 JSONL 轮转/配额/哈希链 | **仍延期**。属运维/防篡改增强；本期新增的 CLI/HTTP/MCP 审计沿用同一 JSONL。 |

---

## 5. 仍未做 / 已知限制

1. **题目抽取仍顺序**：解答与题目跨文件配对，单文件并行会破坏配对语义；文本抽取、
   章节切分、配图、索引已并行。若以后材料规模大，可按“题目文件族”分组并行。
2. **子 agent 未默认接管 CLI**：`retrieval_subagent`/`grading_subagent` 是显式 API，
   service 的 `ask(subagent=True)`/`grade(subagent=True)` 与 MCP `ask`（传入 provider）
   会使用；默认 CLI 仍单循环，避免每个教学动作多一次模型调用与成本。
3. **FastAPI 无鉴权/多租户**：按任务书明确不做账号体系；Workspace 通过请求参数
   选择，适合本机/内网单用户，不应直接暴露公网。
4. **MCP 只实现 stdio**：任务书只要求 stdio；HTTP/SSE 传输未做。启动方式为
   `examprep mcp`、`examprep-mcp` 或 `python -m examprep_agent.interfaces.mcp_server`；
   FastAPI 为 `examprep serve [--host --port]` 或 `examprep-serve`。
5. **成本单价是占位默认值**（0.27/1.10 USD per Mtok）：真实计费请用
   `--input-price/--output-price` 或环境变量覆盖；`costs` 统计 `model_response` **与
   `compaction`** 两类事件（compaction 的 provider 调用也是真实花费）；单价必须为
   有限非负数，NaN/inf 返回 usage 错误而不是输出非标准 JSON。
6. **并行度默认按 CPU 数**：`EXAMPREP_INGEST_WORKERS` 或 `--workers` 可覆盖；
   极端大文件/网络盘下的最优值需实测。
7. **PDF 后端的线程安全取决于 pypdfium2**：本机未安装 PDF 后端，未做多线程 PDF
   压测；若实际材料以 PDF 为主并出现并发崩溃，可把 `--workers` 设为 1 或改用进程池。
8. **进度 JSONL 只做追加**：未做轮转/配额（见 §4）。
9. **Windows 行为未在本机实测**：路径 fence/大小写归一化沿用 P1 测试口径；线程池
   与 pypdfium2 的兼容性只在 Linux Python 3.12 验证。workspace 文件锁在 POSIX 用
   `fcntl.flock`，Windows 分支用 `msvcrt.locking`，但 Windows 未实测。
10. **MCP SDK 以 2.x 为主**：代码保留 1.x `FastMCP` 导入回退，但离线冒烟在
    `mcp 2.3.0` 上完成；1.x 的完整握手未在本机验证。
11. **spike 指标是本地 FakeProvider 微基准**：不代表真实模型端到端延迟；它只用于
    回答“是否引入框架”的结构性问题。
12. **Redis 未引入**（任务书明确不做）：单机单用户默认 SQLite/文件态足够。触发条件：
    多实例共享作业队列、跨进程会话恢复、或写吞吐超过单机文件锁能力时，再引入 Redis
    作为可选作业队列/状态后端（保持默认零配置）。
13. **BehaviorBench 尚未并入**：P2 只完成其前置条件（生产工具面接线 + 对抗集保持
    100% 拦截）；四层 benchmark 流水线属 P3。

---

## 6. 关键文件

```
examprep_agent/service/
  study.py        StudyService：全部核心命令的业务实现
  models.py       pydantic 请求/响应模型
  errors.py       ServiceError + CLI 退出码 ↔ HTTP 映射
  ingest.py       线程池摄取（extract/chapters/figures/index + JSONL 进度）
  subagent.py     隔离子 agent、摘要 guard、风险上限
  costs.py        离线 token/成本聚合
examprep_agent/interfaces/
  http.py         FastAPI（extras serve）
  mcp_server.py   MCP stdio server（extras mcp）
examprep_agent/core/loop.py
  ToolRegistry.max_risk / scoped()、EventLogger defaults、session_start/model_response 元数据
examprep_agent/cli.py
  薄封装：参数 → service → 渲染；文件工具接入教学循环（_explain_tools）
spikes/
  common.py / kernel_adapter.py / langgraph_adapter.py / bench.py / results.json
tests/
  test_service.py test_ingest_parallel.py test_subagent.py test_http_api.py
  test_mcp_server.py test_costs.py test_p2_acceptance.py
```

运行测试：

```bash
/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m pytest -q
# 可选 extras 已装时可跑全部 325 项；CI 安装 .[dev,serve,mcp]，spike 不计入 pytest
```
