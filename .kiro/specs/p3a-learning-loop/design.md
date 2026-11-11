# P3a 学习闭环核心：设计

## 1. 模块

```
packages/app/src/examprep/
├ store/models.py        + Enrollment、ChapterProgress、Attempt、Note
├ learner/
│  └ records.py          选课、进度、作答、笔记的读写（只追加的 Attempt）
├ pedagogy/
│  ├ parts.py            按证据块切段（档位）
│  ├ intent.py           规则意图分类
│  ├ strategy.py         策略矩阵 → Strategy 参数
│  └ picker.py           选题顺序
├ agents/
│  ├ prompts.py          L0 系统策略与各动作的教学指令
│  ├ citations.py        E 编号映射、[[E3]] 渲染（含流式）、历史去标记
│  ├ grader.py           Grader 子 Agent（run_subagent → GradeResult）
│  └ verifier.py         Verifier 子 Agent（逐句 → VerifyResult）
├ study/
│  ├ events.py           学习事件（P3b 直接转 SSE）
│  ├ context.py          上下文组装（P1 ContextAssembler：L0 / L2 / L3 / L4 / L5）
│  └ session.py          StudySession：意图 → 状态迁移 → 调用 Tutor / Grader / Verifier → 事件 → 落库
├ settings.py            config.toml / 环境变量 → GatewayConfig 与默认模型
└ cli.py                 + study 子命令
```

## 2. 分段

- 一章的证据块（P2 已切好，约 600 字，按 `id` 顺序）依次装入段，累计字数超过档位目标即开新段；每段 = 证据块列表。
- 段内容与证据一一对应，Tutor 讲这一段时 L5 就是这些块（编号 E1…），引用天然准确。
- 档位：`pick_profile(context_window)`；目标字数按课程语言取。

## 3. 状态机

```
TEACH ──讲完一段──► (strategy.check) ? CHECK : 下一段 TEACH
CHECK ──懂了──► 下一段 TEACH；没懂且 < 2 次 ──► TEACH（换讲法重讲同一段）；第 2 次没懂 ──► 记困惑点，下一段
本章段讲完 ──► PRACTICE（有题）或 WRAPUP（无题）
PRACTICE ──出题──► 等待作答 ──作答──► FEEDBACK ──还有题──► PRACTICE；题做完 ──► WRAPUP
WRAPUP ──写小结、标 verified/covered──► 下一章 TEACH（没有下一章则课程完成）
任何状态 ──提问──► ASK ──回答后──► 回到原状态
```

- 会话状态（当前状态、待答题、提示级别、本章已练题数、当前检查问题）存在 `ChapterProgress` 与会话事件里，`StudySession.restore()` 从库与事件恢复。
- 意图优先级：明确指令（跳章、进度、看答案、提示、没懂、本章结束、考我、继续）> 等待作答时视为作答 > 等待检查回答时视为检查回答 > 以问号或疑问词结尾视为提问 > 默认“继续”以外的长句视为提问、短句（≤ 4 字）视为继续。

## 4. 引用

```python
class CitationMap:                          # 每轮新建
    def add(chunk) -> str                   # 返回 "E3"
    def resolve(label) -> Citation | None
class Citation: label, evidence_id, file, page, char_start, char_end
def render(text, cmap) -> Rendered          # text：替换后的文本；citations：按出现顺序；unknown：被删除的编号
class CitationStream:                       # 流式：缓冲未闭合的 "[[" 片段
    def feed(delta) -> str; def flush() -> str
def strip_markers(text) -> str              # 进历史前去掉 [[E..]]
```

渲染格式：`（文件名 p.N）`；同一句连续多个引用合并去重。

## 5. 子 Agent

- **Grader**：`run_subagent(gateway, GradeResult, role="grader")`，输入只有题干、选项、参考答案、学生答案、（CHECK 时）本段原文；输出 `verdict`（right / partial / wrong）、`score`、`hits`、`error_type`（concept / calculation / reading / missing_step / memory / none）、`reason`。
- **Verifier**：把 Tutor 回答切句，挑出带引用的句子，一次调用判断全部句子；输出 `[{index, verdict}]`。unsupported 的句子在最终文本里加 🟡 前缀。没有带引用的句子时不调用。
- 两者都有独立超时（默认 30 秒），失败不影响主流程。

## 5.1 实现中的补充决定

- L4（本轮任务）与 L5（证据）放进**最后一条用户消息**而不是系统消息：前面的 L0/L2 前缀在整个会话中不变，能命中 prompt cache；对话历史里只存学生原话和去掉引用标记的助手回复。
- 只引用了不存在编号的句子（全是编造编号）不问 Verifier，直接判为不支撑并标 🟡。
- 看答案、跳过记为 `skip`，与 Skill 一致进入错题回炉。
- 检查题判不了（Grader 失败）时不拦学生，按“懂了”继续，作答记为 unsure。
- `FakeProvider` 新增 `responder`：脚本用完后按请求内容回复，便于一个假模型同时扮演 Tutor、Grader、Verifier。

## 6. 上下文组装（每轮）

| 层 | 内容 | 缓存 |
|---|---|---|
| L0 | 角色、引用规则、围栏规则、🟡 规则、讲解规则（05 文档第 4 节） | 是 |
| L2 | 课程名、语言、章节目录（当前章前后各 3 章） | 是 |
| L3 | 当前章状态、本章困惑点、未解决错题数 | 否 |
| L4 | 本轮任务（讲第 k 段 / 重讲 / 出检查题 / 讲评 / 回答提问 / 写小结） | 否 |
| L5 | 证据块，每块带 `[E3]` 编号与来源，材料放在围栏里 | 否 |

近期对话保留最近 10 条（去掉引用标记）。预算按 tutor 模型上下文长度的 60%。

## 7. 事件

```
StateChanged(state, chapter) / TutorDelta(text) / TutorMessage(text, citations, unknown_labels)
QuestionShown(qid, chapter, type, stem, options, figures, source, has_answer)
Graded(qid, verdict, score, grader, reason, reference, error_type)
Verified(total, supported, partial, unsupported)
Refused(query) / NoteSaved(type, chapter, text) / ChapterDone(chapter, status) / CourseDone()
Suggestions(items: [Suggestion(label, text)]) / Progress(chapters: [...]) / Notice(text)
```

## 7.1 模型配置

`<data_dir>/config.toml`：

```toml
[providers.deepseek]          # 与预设合并；api_key 也可只放环境变量
[roles]
tutor = ["deepseek/deepseek-flash"]
grader = ["deepseek/deepseek-flash"]
verifier = ["deepseek/deepseek-flash"]
```

没有配置文件时：依次检查 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY`，用对应的默认模型填满三个角色；都没有则报错并说明如何配置。

## 8. 测试

- 纯函数：分段、意图、策略、选题、引用渲染（含流式切块随机测试）。
- 会话：FakeProvider 按脚本回复（讲解、检查题、Grader 结构化结果、Verifier 结果、小结），在中文样例上跑完整一章；断言事件序列、库内记录、verified。
- 重启恢复：新建 StudySession 后状态、段号、待答题、历史一致。
- CLI：用注入的 FakeProvider 网关跑 `study` 的脚本化输入。
