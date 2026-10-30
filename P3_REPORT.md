# P3 报告：四层 Benchmark 与 v0.1.0 发布准备（审阅后重做版）

日期：2026-10-30
基线：P2 commit `d2ce544`
状态：**已按 `REVIEW_P3.md` 重做**。本版针对 B1/B2/B3 三个阻断项与 I1–I7 全部重要问题
返工了 benchmark 实现、测试与发布物；原先“金标回显 / 闭包泄漏 / 手抄数字”的做法已删除。

> **数字来源约定**：本报告的 benchmark 数字不再手写，统一由 `run_all` 在同一次运行中写入
> `benchmarks/results/results.json`、`BENCH_REPORT.md`、`summary.md`，并同步刷新
> README 与本文 §0 的标记块。§0 是自动生成的；正文只解释方法、公式与边界。
>
> 红线遵守：未修改 `research/skill-repo/` 下任何文件；未 `git commit`、未 `push`、未打 tag；
> 核心运行时依赖仍只有 `pydantic`；benchmark 默认完全离线（FakeProvider / 确定性脚本）。

验收命令（最终结果见 §7）：

```bash
/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m pytest -q
/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m benchmarks.run_all \
  --out benchmarks/results/ --seed 20261030
```

---

## 0. 结果快照（自动生成，禁止手填）

<!-- BENCH_SNAPSHOT_START -->
由 `run_all` 从同一次 `results.json` 自动生成，禁止手填。

- 生成时间（UTC）：`2026-10-30T14:03:22+00:00`
- 运行命令：`python -m benchmarks.run_all --out benchmarks/results/ --seed 20261030`
- 种子：`20261030`

| 层 | 主要指标 | 结果 |
| --- | --- | --- |
| GroundingBench | 材料答案准确率 top-1（闭卷 / 裸文件 / agent） | 14.7059% / 61.7647% / 85.2941% |
| GroundingBench | 证据召回@3（agent） | 100.0000% (34/34) |
| GroundingBench | 页码引用核验率（agent） | 100.0000% (34/34) |
| GroundingBench | 材料外诚实拒答率（agent） | 100.0000% (10/10) |
| BehaviorBench | 材料诱导攻击拦截率 / 正向正常用例通过率 / 负向控制通过率 | 100.0000% / 100.0000% / 100.0000% |
| BehaviorBench | 跨命令不变量 | 100.0000% (6/6) |
| LongHorizon Sim | 30 天延迟留存率（agent / 基线） | 76.5943% / 70.4485% |
| LongHorizon Sim | 学习增益 = 后测 − 前测（agent / 基线） | 76.5943 / 70.4485 百分点 |
| LongHorizon Sim | 每可用预算小时增益（agent / 基线） | 6.6604 / 6.1260 |
| LongHorizon Sim | 等实际时间干预，上限 118 分钟（agent / 基线留存） | 76.5943% / 44.0792% |
| TutorRubric | 平均分（总体 / 良好 / 中等 / 缺陷 / 绕过） | 0.6875 / 1.0000 / 0.7500 / 0.0833 / 0.5833 |
| TutorRubric | 区分度（良好 − 缺陷）/ 绕过满分数 | 0.9167 / 0 |
| Performance | 摄取串行/并行加速比（当前容器） | 0.9979x |
<!-- BENCH_SNAPSHOT_END -->

---

## 1. 审阅问题逐项处理

### 1.1 阻断项

| 编号 | 审阅问题 | 本版处理 | 证据 |
| --- | --- | --- | --- |
| **B1** | GroundingBench 的 agent 臂直接回显金标答案；探针无条件拒答；引用靠金标 evidence_keys 过滤 | 删除所有 `question["answer"] / evidence_keys / gold` 读取。三臂读取器只接收问题字符串；agent 臂只调用生产 `StudyService.ask`，在命中片段上做确定性抽取。探针拒答由**全局、无金标**的证据支持检查产生（IDF 加权问题 n-gram 覆盖率 + 连续 CJK 重叠 + 问题中的多字符 ASCII 实体必须出现）。评分器 `_score()` 独占金标。三臂共用同一读取器与评分器。 | `benchmarks/grounding.py`；`tests/test_p3_benchmarks.py::test_agent_reader_receives_only_the_query_string`、`test_probe_refusal_is_evidence_driven_not_retrieval_empty`、`test_grounding_support_threshold_separates_material_from_probes` |
| **B2** | `LearnerAPI` 方法闭包捕获 `HiddenLearner`，可通过 `__closure__` 读取/篡改；`hidden_state_isolated=True` 硬编码 | 隐藏学习者移入**独立子进程**，父进程不再持有 `HiddenLearner`。agent 只拿到 `RemoteLearnerAPI`（`__slots__`，唯一私有槽是管道；方法无闭包）；评估器用另一条管道读取真实留存。`isolation_report()` 真的尝试 `snapshot / expected_retention / realized_test / set_stability / 非法 qid` 等读取与篡改，断言全部被拒且隐藏快照不变；`hidden_state_isolated = all(检查项)`。 | `benchmarks/longhorizon/remote.py`；`tests/test_longhorizon_isolation.py`（对象图、闭包、管道篡改、快照不变） |
| **B3** | README / P3_REPORT 的数字不是当前 results；README 表与 BENCH_REPORT 不一致 | 新增 `sync_docs()`：规范运行 `--out benchmarks/results/` 时从同一个 `results` 对象刷新 README 英文/中文标记块与本文 §0；缺失标记直接报错，避免静默残留旧数字。测试解析三处标记块并与 `summary_table(results)` / `p3_snapshot(results)` 逐字比较。 | `benchmarks/report.py::sync_docs`；`tests/test_p3_benchmarks.py::test_committed_docs_match_committed_results`、`test_sync_docs_roundtrip` |

### 1.2 重要问题

| 编号 | 审阅问题 | 本版处理 |
| --- | --- | --- |
| **I1** | 摘要表把 `good_mean_score` 放在“总分/overall”位置 | 摘要表列改为“总体 / 良好 / 中等 / 缺陷 / 绕过”五个数，另列区分度与绕过满分个数；`overall_mean_score` 不再与 good 混用。 |
| **I2** | 闭卷/裸文件臂被故意做弱，三臂不可比 | 三臂使用**同一读取器**；闭卷只有 5 条记忆事实、对先验之外**诚实拒答**（不再无条件编造）；裸文件有全文、可答可拒，但**没有页码锚点**，因此页码引用记为“未尝试/N/A”而不是预设 0。各臂信息条件写入 `results.json -> arms[*].information_condition` 与报告 §1。 |
| **I3** | BehaviorBench 攻击多为脚本直接调用工具 | 主指标改为 **induced**：毒材料进入 agent task/context，FakeProvider 从材料文本解析 `[AGENT ACTION] <tool> <json>` 后经真实 `run_agent` 循环发起调用；逐例记录 `material_in_context` 与 `action_from_material`。静态/setup/直接调用单列为 supplementary。 |
| **I4** | 正常用例混入负向用例；不变量重复且 footer 清单漏 `done` | 正向 normal（16）与负向 control（3）分开计数；不变量改为独立检查：完整 footer 清单（18 条，含 `done` 与拒答控制）、累计状态序列、只读命令不改学习状态、开放错题与历史一致、毒课程 fence 完整性。 |
| **I5** | 学习增益等于后测；复习效率定义偷工；基线被强制拉长 | 保留 `learning_gain = delayed - pretest`（公式写入 results），并在表中显示 pretest；两臂**可用预算相同**。新增 `gain_per_budget_hour`（同一分母）、`gain_per_study_hour`（实际分母）、无调度复习/无 massed restudy 反事实、等实际时间干预（两臂同一 cap）、延迟实测 95% CI。基线不再用同日零增益重读填满预算。 |
| **I6** | TutorRubric 四信号可被应试式绕过；样本极端 | `cites_material` 要求解释本身写出 `claim_terms` 且被引页含 `evidence_keys`；`tests_first` 要求诊断与场景关键词相关并有学生回答；`gives_next_step` 用命令语法 + 真实 qid/章节校验，否定指令直接拒绝；`no_fabrication` 增加全局应试话术表（必考/包过/背下来就行/不用理解…）。新增 4 个中等质量单信号消融样本与 3 个应试式绕过样本，报告 `bypass_full_score_count`（必须为 0）。 |
| **I7** | `--judge` 在 `run_all` 中永远看不到 API key | `run_all` 在进入 `isolated_env` **之前**解析 provider（或接受注入的 provider），再传给 `tutor.run`；离线沙箱仍然移除其他层的 key。新增 `_judge_provider()` 单元测试。 |

---

## 2. GroundingBench（溯源层）方法

### 2.1 三臂信息条件

| 臂 | 能看到什么 | 能否拒答 | 能否页码引用 |
| --- | --- | --- | --- |
| `closed_book` | 显式下界：仅 5 条固定记忆事实；无课程文本 | 能；先验之外诚实拒答 | 否 |
| `raw_file` | 完整课程文本（按 600 字符切块做统计/抽取），无页码锚点 | 能 | 否；页码引用记为未尝试 |
| `agent` | 生产 `StudyService.ask` 返回的命中片段（k=5） | 能 | 能；引用来自命中片段的 `file p.N` |

### 2.2 读取器与支持检查

- 读取器是确定性的抽取式读取器：在可见证据中选择问题 n-gram 加权的 top-1 片段作为答案，
  并保留 top-3 片段用于证据召回@3；标题/练习问句降权。
- 支持检查是全局规则，**不读取金标**：
  1. 问题中的多字符 ASCII 实体（如 `Dijkstra`、`KMP`、`B+`）必须出现在证据中；
  2. IDF 加权的问题 n-gram 覆盖率 ≥ 全局阈值；
  3. 与证据的最长连续 CJK 重叠 ≥ 全局阈值。
- 阈值在固定金标集上一次性标定；材料内最小值、探针最大值与逐题分数全部写入
  `results.json -> layers.grounding.support_calibration / records[]`，没有按题号或题面分支。
- 金标 `answer / answer_terms / gold` 只被 `_score()` 读取；评分在答案片段上做归一化后
  的 answer_terms 包含检查。
- 指标：材料答案准确率 top-1、证据召回@3、页码引用核验率（分母仅为页码引用尝试）、
  材料外诚实拒答率、探针命中数。闭卷/裸文件的页码引用为 N/A，不预设为 0。

---

## 3. BehaviorBench（行为层）方法

- **主攻击集（induced）**：毒材料写入 task 的 `gd.fence()` 上下文；FakeProvider 的
  callable 从收到的 messages 中解析 `[AGENT ACTION] <tool> <json>`，再经真实
  `run_agent` 循环调用工具。逐例记录材料是否在上下文中、动作是否来自材料、拒绝码、
  副作用数量。覆盖：危险命令、越界写、路径参数走私、越界读、凭据外泄、材料删除、
  控制文件写/删、工作区权限自授权。
- **补充攻击（supplementary）**：材料扫描、题库元数据注入、子 agent 摘要回流、标题净化、
  伪造 `permissions.json` 后的直接权限门检查。单独计数，不混入 induced 指标。
- **正常用例**：16 个正向教学命令；另 3 个负向控制（`done` 掌握度门控、`ask` 拒答退出码 4、
  未知题号退出码 2）分开计数。
- **不变量**：完整 footer 清单（18 条教学循环命令，含 `done`）、负向控制、累计状态序列、
  只读命令不改学习状态、开放错题与历史一致、毒课程上的 fence 完整性。每条不变量是独立的
  跨命令检查，不重复某个 normal case 的 `passed`。

---

## 4. LongHorizon Sim（长程层）方法

### 4.1 协议与隔离

- 23 个学习日 + 7 天不学习，第 30 天延迟测试；两臂**可用预算相同**（每天 30 分钟）。
- 隐藏学习者只在子进程中构造；agent 通过管道窄接口调用 `teach` / `quiz -> bool` /
  `restudy`；评估器通过另一条管道读取真实留存。
- `isolation_report()` 的实际检查包括：无实例字典、公开表面仅三个方法、无隐藏属性、
  方法无 `HiddenLearner` 闭包、对象图不可达 `HiddenLearner`、slots 拒绝属性注入、
  `snapshot/expected_retention/realized_test/set_stability/非法 qid` 全部被拒、
  篡改前后隐藏快照一致。`hidden_state_isolated` 是这些检查的 `all()`，不是字面量。
- 真实留存只在评测器通道读取；agent 驱动源码不引用 `expected_retention / realized_test /
  snapshot / .items`。

### 4.2 指标与公式

- 前测：全新模拟器在未暴露时的平均真实召回（按构造为 0），表中显示。
- **学习增益 = 延迟留存 − 前测留存**（百分点）。
- `gain_per_study_hour = 学习增益 / (实际学习分钟 / 60)`。
- `gain_per_budget_hour = 学习增益 / (可用预算分钟 / 60)`；两臂同一分母。
- 增量复习效率 = `(有调度复习的延迟留存 − 同策略无调度复习的延迟留存) / (复习小时)`；
  增量重读效率同理，用 massed restudy 反事实。
- 等实际时间干预：cap = 两臂实际学习分钟的较小值；两臂都在达到 cap 后停止，再在同一天
  测延迟留存。
- 延迟实测给出二项正态近似 95% CI（n=题目数）。
- 基线不再用同日重复重读填充预算；massed restudy 每条每天最多一次。

### 4.3 诚实边界

> **模拟结论 ≠ 真人效果。**

参数是说明性的，未用真人数据拟合；FSRS 曲线、教学动作增益、每日预算都是简化假设。
反事实与等时间干预只说明“在这些假设下”的调度差异，不能外推为真人学习效果。

---

## 5. TutorRubric（教学层）方法

- 场景：good / medium（单信号消融）/ flawed / bypass 四类；概念、错题纠偏、冲刺各 ≥3。
- 四个信号均可核验：
  - `cites_material`：每条解释有引用、引用页真实存在、被引页含场景 `evidence_keys`、
    解释文本写出场景 `claim_terms`；
  - `tests_first`：讲解前有诊断、诊断含场景 `quiz_keywords`、诊断后有学生回答；
  - `gives_next_step`：最后一条 agent 消息含语法可执行的 `examprep` 命令，qid/章节在
    workspace 题库中存在；否定指令（“不要执行…”）直接拒绝；
  - `no_fabrication`：无场景 `forbidden`、无假引用、无全局应试话术
    （必考/包过/背下来就行/不用理解…）。
- 中等样本各自只失败一个信号；绕过样本使用合法引用、合法下一步或合法先测，仍因应试话术/
  不可执行下一步而被扣分。`bypass_full_score_count` 必须为 0。
- `--judge`：无 key 自动跳过并注明；`run_all --judge` 在离线沙箱前解析 provider。

---

## 6. 报告与发布物一致性

- 一个入口：`python -m benchmarks.run_all --out benchmarks/results/ --seed 20261030`
  写出 `results.json`、`BENCH_REPORT.md`、`summary.md`。
- 同一 `results` 对象渲染 BENCH_REPORT 与 summary；规范运行还调用 `sync_docs()` 刷新
  README 英文/中文标记块与本文 §0。
- 测试逐字比较：
  - `BENCH_REPORT.md == render_report(results.json)`；
  - `summary.md == summary_table(results.json)`；
  - README 两块 == `summary_table(results, "en"/"zh")`；
  - P3 §0 == `p3_snapshot(results)`；
  - `sync_docs()` 在临时文件上的往返。
- 缺失标记会抛错，不会静默保留旧表。

---

## 7. 复现与测试

```bash
# 全量测试
/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m pytest -q

# 规范 benchmark（离线，固定种子；同时刷新 README/P3 标记块）
/home/hatch/workspace/projects/examprep-agent/.venv/bin/python -m benchmarks.run_all \
  --out benchmarks/results/ --seed 20261030

# 同种子双跑（确定性四层）
python -m benchmarks.run_all --out /tmp/bench-a --seed 20261030 --no-performance
python -m benchmarks.run_all --out /tmp/bench-b --seed 20261030 --no-performance
```

最终 pytest 结果：**349 passed, 1 skipped**（skip 为 PDF 后端缺失）。测试覆盖：

- Grounding：读取器不接触金标、探针支持/不支持的行为、阈值分离、三臂同评分器、金标
  answer_terms 可在材料中核验。
- Behavior：induced 材料上下文与动作来源、正常/负向分开、footer 清单含 `done`。
- LongHorizon：对象图/闭包/管道篡改、隐藏快照不变、`hidden_state_isolated` 由检查计算、
  同种子复现、异种子变化。
- Tutor：中等样本单信号消融、绕过样本不满分、引用支持/命令校验。
- Runner：同种子四层容差一致、报告逐字对账、三处文档一致、judge key 路径。
- 主包不 import `benchmarks`；`research/skill-repo/` 未修改。

---

## 8. 未做项与 v0.2 建议

1. **真人学习试点**：没有真人前后测；LongHorizon 全部是模拟数据。
2. **LLM judge 未启用**：代码路径已实现且修复了 key 传递，但默认离线运行无 key；
   未做 judge 与人工评分的 kappa 校准。
3. **逐句 NLI**：Tutor/Grounding 的引用与 claim 校验是页面级 + 关键词/片段级，
   不是逐句自然语言推理。
4. **外部红队**：Behavior 的攻击集是仓库内用例，不是第三方红队数据。
5. **PDF 后端与跨机器性能**：本机未安装 PDF 后端；性能数字仅当前容器。
6. **LongHorizon 长期化**：只有 30 天、章节级知识点、简化教学动作；未做 IRT/人格/动机。

v0.2 优先级：真人小规模试点与参数校准、LLM judge + 人工 kappa、逐句引用校验、
Behavior 用例外部化、PDF 并行压测、LongHorizon 月度级模拟。

---

## 9. 关键文件

```
benchmarks/
  run_all.py                  一个入口：四层 + 性能；规范运行同步 README/P3 标记块
  common.py                   离线环境、临时目录、CLI 捕获、统计/格式化
  grounding.py                三臂同读取器/同评分器；生产 ask；全局支持检查
  behavior.py                 induced 材料攻击 + 补充攻击 + 正向/负向/不变量
  longhorizon/simulator.py    隐藏学习者（仅子进程构造）
  longhorizon/remote.py       进程隔离、管道窄接口、isolation_report()
  longhorizon/runner.py       两臂 + 反事实 + 等时间 + 计算式隔离字段
  tutor.py                    四信号可核验化 + 中等/绕过样本 + 可选 judge
  performance.py              摄取/延迟/并行计时
  report.py                   报告渲染、README/P3 摘要块同步
  data/grounding_qa.json      34 + 10 金标 QA（answer_terms 可在材料核验）
  data/tutor_scenarios.json   16 个固定场景（good/medium/flawed/bypass）
  results/                    results.json / BENCH_REPORT.md / summary.md
tests/
  test_p3_benchmarks.py       runner、报告/文档对账、grounding/behavior/tutor 诚信测试
  test_longhorizon_isolation.py  进程隔离、篡改尝试、计算式 hidden_state_isolated
```

---

## 10. 验收清单

- [x] `pytest` 全绿（349 passed, 1 skipped；skip 为 PDF 后端缺失）。
- [x] `python -m benchmarks.run_all --out benchmarks/results/` 离线跑通。
- [x] 同种子重复运行，Grounding/Behavior/LongHorizon/Tutor 核心指标一致（浮点容差）。
- [x] BENCH_REPORT / summary / README 两块 / P3 §0 均来自同一次 `results.json`，
      测试逐字对账。
- [x] Grounding：agent 臂只接收问题字符串并走生产 `StudyService.ask`；金标只在评分器；
      探针拒答由证据支持检查产生；三臂同题同评分器。
- [x] LongHorizon：隐藏状态进程隔离；隔离测试实际读取/篡改并断言失败；
      `hidden_state_isolated` 由检查结果计算；学习增益公式与同预算/等时间/反事实指标齐全。
- [x] Behavior：induced 攻击经真实 agent 循环；正常/负向分开；不变量去重并含 `done`。
- [x] Tutor：中等样本 + 应试式绕过样本；引用/下一步可核验；绕过样本不满分。
- [x] `--judge` key 在离线沙箱前解析。
- [x] 未修改 `research/skill-repo/`；未 commit/push/tag。
