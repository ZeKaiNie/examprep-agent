# LangGraph spike 报告（P2，限时实验）

> **审阅后重做（2026-10-30）**：本版按 `REVIEW_P2.md` 的 I7 重做了 benchmark 与结论。
> 旧版存在以下问题，本版已修正：两边每个用例的 compact/invoke 次数不同、成功阈值
> 不同、compaction 用例负载不同、摘要规则不同、“重启”只是同进程重新 compile、
> 依赖统计把未安装/不适用 marker 的 distribution 也计入。**旧版的量化数字与
> “恢复能力打平 / compaction 慢 4.6×”表述已被本版取代。**

日期：2026-10-30
环境：Python 3.12.3，`langgraph 1.2.18`（仅装在 venv 的 `spike` extra 中），
所有模型轮次由 `FakeProvider` 脚本驱动，**全程离线**，每用例重复 20 次。
复现：`/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m spikes.bench --repeats 20`
原始数据：`spikes/results.json`（含每轮 outcome 与 detail）。

## 1. 实验设置（两边同口径）

工作负载、预算与成功判据集中定义在 `spikes/common.py`，两个 adapter 不得各写一份：

| 用例 | 相同负载 | 相同步数（两边一致） | 相同成功判据 |
| --- | --- | --- | --- |
| `resume` | 12 条 `fact-i` 消息；每步 add+compact / 每次 graph invoke 处理一条；随后把状态 JSON 序列化/反序列化到全新运行时，再处理 1 条 `fact-after-resume` | 12 + 1 | `fact-0` 在摘要中、`fact-after-resume` 在历史中、三个 compartment 均在预算内、总量不超总预算 |
| `parallel_tools` | 一次模型响应请求 3 个各 sleep 50 ms 的工具 | 3 | 3 个结果全部返回 |
| `compaction` | 40 条 `fact-i` 消息 + 超长 system/material；每步 add+compact / graph invoke | 40 | `fact-0` 在摘要中、system/material/history/总量均在预算内 |

共享预算（两边完全相同，取自 `ContextBudget.from_total(800)`）：

```
max_tokens=800  system=120  material=360  history=320
summary_budget=int(320*0.35)=112  keep_recent_messages=4
```

实现口径：

- **调用次数**：kernel 每步执行一次 `ContextEngine.add` + `compact()`；LangGraph 每步
  执行一次 `app.invoke()`（单节点图）。因此 `resume` 两边各 12 次，`compaction`
  两边各 40 次，`parallel_tools` 两边各 3 个工具。
- **成功判据**：两边直接调用 `common.resume_success` / `common.compaction_success`，
  不再各写阈值。
- **摘要规则**：LangGraph 侧不再自设 `TAIL_MESSAGES=3` / `SUMMARY_BUDGET=400`，而是
  调用与 kernel 相同的 `_truncate_to_tokens`，并用 kernel 的确定性摘要合并函数处理
  被淘汰消息；两边的 tail 选择与摘要预算一致。
- **重启**：两边都只保留 JSON 字节：kernel 序列化 `system/material/summary/history`；
  LangGraph 从 `get_state().values` 取同样字段、JSON 序列化后灌入全新 graph + 新
  thread。**这验证的是“状态可序列化并恢复后继续”，不是 LangGraph checkpointer
  后端（Sqlite/Postgres）的跨进程能力**——本 spike 未验证后者。
- **依赖统计**：使用 `packaging` 评估 `sys_platform`/`python_version` 等 marker，并且
  只统计 `importlib.metadata.version` 能查到的已安装 distribution；未安装的名字不再
  计入。
- **代码量**：分别列出 kernel 核心运行时代码与两个 adapter；adapter 行数不是等价的
  “实现成本”比较（LangGraph 侧仍要手写预算/摘要/状态字段），仅作规模参考。

## 2. 量化结果（20 次重复，中位延迟）

| 用例 | 步数 | 自研内核 成功率 | 自研内核 中位延迟 | LangGraph 成功率 | LangGraph 中位延迟 | 比值 |
| --- | --- | --- | --- | --- | --- | --- |
| resume（长会话恢复） | 12+1 | **100%** | **4.62 ms** | **100%** | 40.46 ms | LangGraph 8.8× 慢 |
| parallel_tools（并发工具） | 3 | **100%** | 151.06 ms | **100%** | **54.47 ms** | LangGraph 2.8× 快 |
| compaction（预算内压缩） | 40 | **100%** | **11.54 ms** | **100%** | 107.12 ms | LangGraph 9.3× 慢 |

其他量化项：

| 指标 | 自研内核 | LangGraph |
| --- | --- | --- |
| 本 spike 适配代码 | 144 行 | 246 行（需手写与 kernel 相同的预算/摘要规则） |
| 运行所需核心代码 | 1,390 行（`core/loop.py` + `core/context.py` + `core/session.py`，已存在） | 框架承担图执行，但预算/摘要/审计/工具闸门仍需自己写 |
| 传递依赖（已安装、marker 生效） | 0（仅 pydantic，项目原有） | **38 个 distribution**（含 langchain-core、langsmith、httpx、websockets、zstandard 等） |
| 默认依赖影响 | 无 | 无（只在 `[spike]` extra；主包不 import） |

说明：延迟是纯本地、FakeProvider、单进程数字，只反映框架每步开销与控制流差异，
不代表真实模型调用端到端延迟（后者主要被网络/推理时间支配）。

**重做过程中发现并修正的 kernel 问题**：`ContextEngine._deterministic_summary` 旧实现
每次合并都重新包一层 `Earlier summary:` 和标题，40 步增量压缩后固定摘要预算被重复
头部吃掉，最早的 `fact-0` 被截断丢失（重做初版两边 compaction 成功率都是 0%）。本版
改为“上一版摘要原文置前、新消息追加”，截断只会丢最新内容；两边随后都恢复到 100%。
这是确定性离线摘要的真实缺陷，不是框架差异。

## 3. 可控性与可调试性

| 维度 | 自研内核 | LangGraph |
| --- | --- | --- |
| token 预算 | 原生三 compartment 硬预算 + 估算，超预算即拒绝下一步 | 无原生预算；需在节点/钩子里手写（本 spike 即如此） |
| compaction | 增量滚动摘要、尾部原文保留、tool-call/result 配对、provider 失败确定性回退 | 框架只给消息状态；摘要、截断、保留策略全手写 |
| 权限闸门/审计 | `ToolRegistry` 内建 `PermissionGate`，每次调用先审计后执行；风险是注册属性 | 工具节点自行调用；框架不感知风险等级，审计需自己接线 |
| 可恢复性 | `session.json` 为普通 JSON（原子写+快照），可读、可 diff、可手工修复 | 本 spike 只验证“状态 JSON 可恢复”；生产需换 Sqlite/Postgres checkpointer，schema/版本由库管理 |
| 可调试性 | JSONL 事件流（session/step/usage/tool_call/tool_result）逐条可读 | 有 graph trace/studio，但需要额外服务/依赖；状态语义（invoke 输入如何 merge、reducer 覆盖规则）需要学习 |
| 并发 | 一次响应内的多个工具调用**顺序执行** | `Send` 原生 fan-out 并行（本用例快 2.8×） |
| HITL/中断 | 未实现（当前无需求） | 原生 `interrupt`/恢复，是该框架的真实优势 |
| 升级/供应链 | 自己控制 | 38 个已安装传递依赖；1.x 内部 API（checkpointer/serde）仍有迁移成本 |
| 锁定风险 | 无 | 图/状态 schema 与框架版本绑定 |

## 4. 结论与建议（只陈述数据支持的部分）

**建议：保持自研（不引入 LangGraph）。** 数据支持的理由：

1. **同口径下小步骤开销显著更高**：同样的 12 步 resume 与 40 步 compaction，LangGraph
   中位延迟分别是自研内核的 8.8× 和 9.3×。这是“每步都走图/状态合并/序列化”的固定
   成本，不是调用次数差异造成的（本版两边步数相同）。
2. **恢复成功率打平，但只在本用例的范围内**：两边 20/20 均通过同一“JSON 状态
   序列化/反序列化后继续”的判据。**不能据此推断 LangGraph 生产 checkpointer 的
   跨进程恢复能力与自研 JSON 检查点等价**；本 spike 没有验证 Sqlite/Postgres
   checkpointer，也没有跨进程重启。
3. **唯一实质优势是并发 fan-out**：相同 3×50 ms 工具负载，`Send` 让延迟从 151 ms
   降到 54 ms（2.8×）。这可以在自研循环里用 `asyncio.gather`/线程池实现，不需要
   引入框架；若 P3 的检索/判分需要同轮多工具并行，优先在 `core/loop.py` 增加受控
   并行（仍逐调用过 `PermissionGate`，保持审计顺序）。
4. **上下文治理仍要自己写**：LangGraph 不提供 token 预算、compartment 划分、摘要
   预算或材料 fence；本 spike 的 LangGraph adapter 反而比 kernel adapter 更长
   （246 vs 144 行），因为它要复刻同一套规则。
5. **成本**：38 个已安装传递 distribution，换来的是我们已具备的图执行/检查点能力。

**保留的借鉴**（不算“局部采用”，不引入依赖）：

- 借鉴 `Send` 的显式 fan-out 语义，在自研循环中设计受控的并行工具执行；
- 借鉴“检查点即一等公民”的思路，继续完善 `session.json` 的版本号与迁移；
- 若未来出现**多分支图 + 人工中断 + 分布式检查点**成为核心需求（当前没有），
  重新评估 LangGraph，触发条件：需要 HITL 审批流、需要跨进程/多副本恢复、
  或自研会话分支实现成本超过 1 人周。

## 5. 隔离与依赖纪律

- spike 代码在 `spikes/`，不在 `examprep_agent` 包内；`setuptools` 只打包
  `examprep_agent*`，因此 `pip install examprep-agent` 不会带上这些文件。
- LangGraph 只写在可选 extra `spike = ["langgraph>=1.0", "packaging>=23"]`，主依赖仍只有
  `pydantic`；运行时 import 图里没有 `langgraph`/`langchain`。
- 实验全程离线；`FakeProvider` 脚本无网络。
