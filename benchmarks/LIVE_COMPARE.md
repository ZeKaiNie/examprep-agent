# LIVE_COMPARE — 真实课程真模型对照（Real-course）

实验日期：2026-10-31。材料与金标署名：MIT OpenCourseWare 6.006 Introduction to Algorithms（Spring 2020，CC BY-NC-SA 4.0）；Open Yale Courses PSYC 110 Introduction to Psychology（CC BY-NC-SA 3.0）。金标来自原 skill 仓库试点集（`full/benchmark/items/`），评分代码之外任何答题臂均未接触金标。

## 1. 实验设计

- **材料（七臂同一份）**：MIT 6.006 第 1–6 讲讲义 PDF×6（排除 q1 与 q1_sol）+ 耶鲁 PSYC 110 第 1–4 讲转录 md×4。裸读臂把材料全文（PDF 经 `pdftotext` 抽文本）塞进提示词；dsh 臂在只含材料与题目清单（仅 id+question）的工作区内自由使用文件工具；examprep 臂先经生产摄取（`StudyService.setup`）建工作区，再逐题走 `StudyService.ask(k=5, subagent=True)`（即 MCP ask 的生成路径：BM25 检索 + 检索子代理摘要）。
- **题目**：26 题 = 每门 10 道可答题 + 3 道探针（`answerable=false`，探针以拒答为对、作答为错）。探针测的是弃答纪律而非知识：例如 FFT 复杂度与《梦的解析》出版年份，两模型闭卷都能答出，但探针要求拒答。
- **评分 v2（七臂同一把尺）**：金标答案与考生答案归一化后抽内容词元（英文/数字词元长度≥2、去停用词；中文按字词元），金标词元被答案覆盖 ≥80% 判对。本实现对白天三臂逐题记录的复算结果为：60/60 个 verdict 一致，59/60 个 token_frac 精确一致（唯一偏差是 `claude_closed` 的 psyc_q5：复算 0.444、白天文件 0.333，verdict 同为错，不影响任何总数）。停用词表见 `benchmarks/live_compare_real.py` 的 `STOPWORDS`。
- **成本口径**：DeepSeek 四臂统计接口 `usage` 字段的 API 调用次数与 token 数；dsh 臂用其 `--json` 事件的 `step_end` 用量加总（`inputTokens` 计未缓存提示词，`cacheReadTokens` 计缓存提示词）。金额按 DeepSeek 官方 2026 年 10 月价目离峰价估算（`deepseek-flash`：未缓存输入 $0.15/M、缓存输入 $0.003/M、输出 $0.60/M；本实验在周六进行，全天离峰）。Claude 三臂为白天盲测子代理，无 API 用量记录，成本列记为未记录、不做估算。
- **examprep × Claude 单元格不可得**：生产管线的模型是 DeepSeek V4.1 Flash，无 Claude API 可用；表中 `Claude Agent 式` 臂是白天逐题检索+引用的近似口径，**不是** examprep 管线，特此标注（表中以 * 标记）。

## 2. 七臂总表

| 臂 | 模型 | 可答题 | 探针拒答 | 6.006 覆盖内 | 6.006 覆盖外 | API 调用 | 总 token | 成本（离峰估算） | 每答对一题成本 | 墙钟 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Claude 裸读 | Muse Spark | 12/20（60.0%） | 6/6 | 2/4 | 0/6 | — | — | 未记录 | — | 未记录 |
| Claude 闭卷 | Muse Spark | 11/20（55.0%） | 4/6 | 2/4 | 5/6 | — | — | 未记录 | — | 未记录 |
| Claude Agent 式* | Muse Spark | 11/20（55.0%） | 6/6 | 2/4 | 0/6 | — | — | 未记录 | — | 未记录 |
| DeepSeek 裸读 | deepseek-flash | 11/20（55.0%） | 6/6 | 2/4 | 0/6 | 26 | 1263182 | $0.0065 | $0.0006 | 70.1s |
| DeepSeek 闭卷 | deepseek-flash | 10/20（50.0%） | 4/6 | 2/4 | 4/6 | 26 | 3958 | $0.0016 | $0.0002 | 46.4s |
| dsh agent | deepseek-flash | 11/20（55.0%） | 6/6 | 2/4 | 0/6 | 16 | 559681 | $0.0145 | $0.0013 | 96.6s |
| examprep 管线 | deepseek-flash | 6/20（30.0%） | 6/6 | 1/4 | 0/6 | 84 | 96919 | $0.0219 | $0.0037 | 225.2s |

* `Claude Agent 式` 为近似口径（见第 1 节），非 examprep 管线。examprep 臂墙钟为 26 次 ask 的时间，摄取另计约 4.1 秒。
dsh 臂墙钟为进程总时长（含启动）。Claude 臂墙钟未记录。

## 3. 覆盖拆分：为什么材料臂的总分上不去

试点金标按覆盖全课程的精选笔记出题，而真实材料只有 6.006 前 6 讲。按 `supporting_span` 主题拆分 6.006 的 10 道可答题：

| 组 | 题目 | 含义 |
| --- | --- | --- |
| 覆盖内（4 题） | algo_q1（归并排序）、algo_q2（二分查找）、algo_q6（完全二叉树高）、algo_q7（哈希期望 O(1)） | 排序 / 哈希 / 树基础，前 6 讲内 |
| 覆盖外（6 题） | algo_q3（BFS）、algo_q4（DFS）、algo_q5（完全图边数）、algo_q8（Dijkstra）、algo_q9（Bellman-Ford）、algo_q10（AVL） | 图搜索 / 最短路 / AVL / 图计数，第 7–13 讲或全课程笔记范围，前 6 讲无此内容 |

PSYC 金标只对应第 1 讲，转录全覆盖，无此问题。**所有材料臂在覆盖外 6 题上均为 0/6**——不是答错，而是按指令诚实拒答（`无法从材料中找到`）；在总分口径下这 6 题计为错，但按材料纪律它们是正确行为。因此总分必须与拆分表一起读：材料臂的合理天花板不是 20/20。

### 3.1 材料臂在 v2 尺下的天花板：12/20（60%）

覆盖内 4 题中，algo_q6（金标 `3`）与 algo_q7（金标 `O(1)`）在 v2 词元定义下金标词元集为空（全部词元长度 <2），覆盖率按定义记为 0，**所有臂在这两题上均为 0 分，即使表面答案正确**（例如裸读臂答出 `3` 与 `Θ(1)` 仍计错）。于是材料臂的 v2 天花板 = 覆盖内可评 2 题（q1、q2）+ PSYC 10 题 = 12/20。Claude 裸读恰好打到天花板 12/20；DeepSeek 裸读与 dsh 为 11/20，只差 1 题（见第 4 节）。**覆盖内准确率 2/4 也不应理解为“只会一半”：分母里有 2 道尺子本身判不了的题**；覆盖内可评子集（q1、q2）上，全部材料臂均为 2/2。

## 4. 因子解读

### 4.1 同架构比模型：裸读臂 Claude 12 vs DeepSeek 11——1 题之差，不具区分度

两臂在 26 题上的分歧只有 psyc_q5（Capgras 核心妄想）一题：金标词元集 9 个，Claude 裸读覆盖 8/9（0.889，判对），DeepSeek 裸读与 dsh 均为 7/9（0.778，判错）。分歧点是金标里的 framing 词 `belief` 与形态差异（`love` vs `loves`、`impostors` 有无），而非知识对错。26 题试点中 1 题 = 总分 5 个百分点，**55% 与 60% 的差距在统计上不具区分度**，不应解读为模型代差。
闭卷臂同理：Claude 11 vs DeepSeek 10，差距同样在 1–2 题量级；两模型闭卷都在算法题上强（Claude 7/10、DeepSeek 6/10）而在 PSYC 专属细节上弱（均为 4/10），且都对两道探针作答（algo_u1 FFT、psyc_u1 弗洛伊德年份），探针拒答同为 4/6。
### 4.2 同模型比架构（DeepSeek 四臂）：裸读 = dsh > 闭卷 > examprep 管线

- **裸读 11 = dsh 11，且错题集合完全相同**（6.006 覆盖外 6 题 + q6/q7 尺子题 + psyc_q5）：dsh 用 16 步工具调用（16 个模型步骤、19 次工具调用：`read` + `bash`/`pdftotext`/`grep` + 自查 26 题 id 全覆盖）复现了全文提示词的效果，架构差异在 26 题上没有产生分数差异，但 dsh 成本更高（$0.0145 vs $0.0065），墙钟更长（96.6s vs 70.1s）。
- **闭卷 10**：用参数知识在 6.006 覆盖外拿到 4/6，却在 PSYC 上只拿到 4/10（第 1 讲专属细节如教材、诗人、Gage 后遗症多为猜错或拒答），净胜负抵消后比材料臂低 1 题；同时探针纪律最差（4/6）。
- **examprep 管线 6**：不是检索质量问题，而是**生产 ask 路径在 DeepSeek Flash 下大面积无法完成**（见第 5 节）。它产出的 6 份真实摘要全部判对（6/6），但 26 题中有 20 题只拿到 `[sub-agent produced no safe summary]`。

### 4.3 成本解读：四臂都极便宜，但效率排序与准确率排序相反

四臂总成本均低于 $0.022。按每答对一题成本：闭卷 $0.0002 < 裸读 $0.0006 < dsh $0.0013 < examprep $0.0037。裸读 126 万 token 中 99.6% 为缓存命中（重复提示词前缀），若无缓存，裸读成本约为 $0.19（仍便宜，但高约 30 倍）——**缓存是裸读便宜的主因，不是 token 量小**。examprep 臂成本最高、准确率最低：84 次调用大多消耗在预算耗尽的无效轮次上，是纯浪费。账户余额交叉验证：实验前 ¥7.19，实验后 ¥6.76，差额 ¥0.43（约 $0.059），覆盖四臂用量估算合计 $0.0445 与试点、已废弃的破损摄取轮次及 dsh 探针任务，量级一致。

## 5. examprep 管线臂专项：执行中暴露的问题（全部如实记录）

本臂按生产定义执行：摄取 10 个文件（MIT PDF×6 + PSYC md×4）→ 逐题 `StudyService.ask(k=5, subagent=True)`。执行中暴露的问题全部如实记录，未为美化分数而绕过：

1. **已修复 bug（计入本次提交）**：`StudyService.ask` 在命中页带有 figure 时崩溃——`figures_for_pages` 返回的是 `FigureRecord` 模型，`ask` 却用 `f["path"]` 下标访问，抛 `TypeError: 'FigureRecord' object is not subscriptable`。md 为主的旧样本课程从未触发该分支（无 figure），MIT PDF（14 个 figure）第一题即触发。修复为 `f.path`（`examprep_agent/service/study.py` 一行），并补回归测试 `tests/test_ask_figures.py`。不修复则本臂无法产生任何结果，故修复属于测量前提而非口径调整；修复内容与分数无关（只恢复命中页的 figure 路径字段）。
2. **摄取环境问题（已绕过，未改代码）**：默认 4 线程并行摄取时 pdfium 层崩溃（`double free or corruption`）；改用 `workers=1` 串行摄取后 10/10 文件成功、0 错误。摄取模块文档保证串行与并行输出字节一致，因此不影响可比性，但并行 PDF 摄取在该容器内的崩溃应另行排查。另：摄取前若环境缺 `pypdfium2`/`pypdf`，6 个 PDF 会静默失败（`pdf_support_missing`）而只剩 PSYC 内容——首轮误跑即因此作废并重跑，本报告数字均来自重跑后的有效工作区。
3. **检索子代理预算耗尽（本臂低分的主因，未修复）**：`retrieval_subagent` 的 shipped 上限为 `max_total_tokens=3000`，而 DeepSeek Flash 的工具轮次含大量推理 token（计入同一预算）。26 题中 20 题以 `budget_exhausted` 告终（已花费约 3100–3350 token），未能进入最终回答轮次，故只产出 `[sub-agent produced no safe summary]`。该字符串按拒答计：探针 6/6（因祸得福的弃答分），可答题则计错。**诊断子集（仅 6 份真实摘要）准确率为 6/6**：管线检索到的证据本身是对的，瓶颈在子代理预算与推理 token 的冲突，而非检索或知识。后续改进方向是调大子代理预算或将推理 token 排除出预算（本次未改动，以保持“生产管线原样”的测量定义）。
4. **章节合并说明**：摄取把两门课按位置配对成 6 章（如第 1 章同时含 MIT lec1 与 PSYC lecture01），这是管线对混合材料文件夹的既有章节化行为；检索本身按文件+页切块，答案不受章节标题影响，报告中不把章节标题当作课程归属。

## 6. v1 尺废弃说明

v1 为逐字包含尺（考生答案须逐字包含金标字符串）。它系统性低估生成式答案：例如金标 `A stroke`，正确答案 `It is typically the result of some sort of stroke` 并不逐字包含 `A stroke`，v1 判错；同理 `O(n log n)` 的 `Θ(n log n)` 表述、带解释的短答案都会被误杀。v2 改为词元集覆盖（≥80%），保留逐题明细与阈值，七臂全部用 v2 重算/并入，白天三臂的 v2 结果直接采用其文件值（复算 verdict 60/60 一致，见第 1 节）。

## 7. 局限与不可得项

- **26 题试点规模**：可答题仅 20 道，1 题 = 5 个百分点；除 examprep 预算问题（20/26 的系统性模式）外，臂间 1–2 题的差距均不具区分度，结论应读作“同档”而非排名。
- **v2 尺残留缺陷**：algo_q6、algo_q7 金标词元集为空，全臂 0 分；psyc_q5 的 `belief` 一词决定对错。
- **材料覆盖局限**：6.006 只给了前 6 讲，覆盖外 6 题对材料臂天然计错；PSYC 只覆盖第 1 讲金标，转录 2–4 讲未被金标使用。
- **不可得项**：examprep × Claude 单元格（无 Claude API）；Claude 三臂成本与墙钟（白天盲测未记录）；dsh 成本为事件用量估算（非账单）；金额为离峰价估算，实际账单以余额为准（本实验差额已交叉验证，见第 4.3 节）。
- **Claude Agent 式臂**为近似口径（逐题检索+引用），不是 examprep 管线；跨模型比较请以裸读臂与闭卷臂为准。

## 8. 逐题矩阵（✓=判对；✗=判错；R=拒答，可答题计错；✓R=探针拒答正确。examprep 臂的 R 含预算耗尽弃答，见第 5 节）

探针行（`_u`）的 ✓ 表示拒答正确；可答题行的 R 表示拒答（计错）。完整逐题答案与 token_frac 见 `benchmarks/results/live_compare_real.json` 的 `arms.<name>.records`。

| 题目 | Claude 裸读 | Claude 闭卷 | Claude Agent 式* | DeepSeek 裸读 | DeepSeek 闭卷 | dsh agent | examprep 管线 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| algo_q1 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| algo_q2 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | R |
| algo_q3 | R | ✓ | R | R | ✓ | R | R |
| algo_q4 | R | ✓ | R | R | ✓ | R | R |
| algo_q5 | R | ✓ | R | R | ✓ | R | R |
| algo_q6 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | R |
| algo_q7 | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | R |
| algo_q8 | R | ✓ | R | R | ✓ | R | R |
| algo_q9 | R | ✓ | R | R | ✗ | R | R |
| algo_q10 | R | ✗ | R | R | ✗ | R | R |
| algo_u1 | ✓R | ✗ | ✓R | ✓R | ✗ | ✓R | ✓R |
| algo_u2 | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R |
| algo_u3 | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R |
| psyc_q1 | ✓ | R | ✓ | ✓ | ✓ | ✓ | R |
| psyc_q2 | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | R |
| psyc_q3 | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | R |
| psyc_q4 | ✓ | ✗ | ✓ | ✓ | ✗ | ✓ | ✓ |
| psyc_q5 | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | R |
| psyc_q6 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| psyc_q7 | ✓ | ✗ | ✓ | ✓ | R | ✓ | ✓ |
| psyc_q8 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | R |
| psyc_q9 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| psyc_q10 | ✓ | ✓ | ✓ | ✓ | R | ✓ | ✓ |
| psyc_u1 | ✓R | ✗ | ✓R | ✓R | ✗ | ✓R | ✓R |
| psyc_u2 | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R |
| psyc_u3 | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R | ✓R |

---

方法代码：`benchmarks/live_compare_real.py`（v2 评分器与成本核算）；跑臂脚本与原始答案见项目工作区 `live_compare_work/`（`run_arms.py`、`score_arms.py`、`build_results.py`、`answers/`、`dsh_ws/`、`examprep_ws/`）；Claude 三臂原始文件为项目根目录 `live_compare_claude_real.json`（白天盲测，v2 评分，本报告直接并入未重跑）。
