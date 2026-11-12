# ExamPrep Agent

> 靠谱：只讲你资料里有的，每句话都能翻到原文那一页。
>
> The study agent that cites every page, quizzes you with real exam questions, and remembers what you got wrong.

[![CI](https://github.com/ZeKaiNie/examprep-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ZeKaiNie/examprep-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**状态：P1 地基、P2 资料引擎、P3 学习闭环（命令行、本机服务端、Web 界面）已完成；下一步是长期学习（FSRS 复习、错题回炉、掌握度）。**

```bash
uv sync
uv run examprep ingest samples/zh-data-structures      # 导入：章节、题目（含答案配对）、配图、证据块
uv run examprep search zh-data-structures 循环队列 判满  # 检索，带文件和页码
export DEEPSEEK_API_KEY=sk-...                          # 或 OPENAI / ANTHROPIC / GEMINI 的 Key，或本地 Ollama
uv run examprep study zh-data-structures               # 开始学：讲解 → 检查 → 练习 → 判分 → 小结
uv run examprep serve                                  # 本机网页服务 http://127.0.0.1:8765（接口说明见 /docs）
```

网页界面（需要 Node 20.19+）：先构建一次，`examprep serve` 就会在同一个地址提供界面——课程列表与导入、流式讲解、点讲解里的出处在右侧打开原文并高亮那一段、进度面板与上下文压缩：

```bash
cd web && npm install && npm run build   # 构建到 packages/app/src/examprep/web/
uv run examprep serve                    # 打开 http://127.0.0.1:8765
# 改前端时：另开终端 cd web && npm run dev，打开 http://localhost:5173（/api 自动转给 8765）
```

`examprep serve` 默认只允许本机访问：拒绝 Host 不是本机地址的请求，也拒绝其他网站借浏览器发来的请求。同一门课同一时间只能在一个地方学（命令行窗口或网页），另一边会提示“正在另一个窗口或网页里学习”。

学习时直接说“继续”“考考我”“没懂”“给个提示”“看答案”“跳到第 3 章”“进度”，或者直接提问。
讲解里的出处（文件 + 页码）由系统根据证据编号生成，模型写不出不存在的页码；资料里没有的问题会直接回答“资料里没有讲到”。

讲解、判分、核对默认都开启模型的思考模式（`high`），因为讲解里的例子要算对、判分要准。想更快、更省，可以在数据目录的 `config.toml` 里用 `[reasoning]` 按角色调整（`off` / `low` / `medium` / `high`），或者只调这一次学习的讲解：`uv run examprep study zh-data-structures --reasoning low`。

对话太长时会自动压缩上下文（默认开启，超过模型窗口的 70% 或 6 万 token 时触发），学习时说“压缩上下文”也能随时手动压缩；学习进度、错题和小结存在数据库里，压缩不会丢。在 `config.toml` 的 `[compaction]` 里可以调整阈值或关闭自动压缩，也可以用 `--no-auto-compact` 只关这一次。

ExamPrep Agent 从 [Exam Cram Coach Skill](https://github.com/ZeKaiNie/universal-examprep-skill) 演进而来，是一个独立的开源学习 Agent。

## 它要做什么

- 只依据学生上传的资料讲解；引用由系统生成，模型只写证据编号，写不出假页码
- 只用资料里的真题出题，规则判分 + 独立的判分 Agent
- 记得错题、困惑点和掌握度，用 FSRS 安排复习
- 期末突击和长期学习两种模式
- 支持主流云端模型（OpenAI 兼容、Anthropic、Gemini）和本地模型（Ollama、LM Studio、vLLM、llama.cpp）

## 仓库结构

| 路径 | 内容 |
|---|---|
| `packages/llm` | `examprep-llm`：多厂商统一模型接口、能力表、网关（重试、限流、熔断、降级、取消） |
| `packages/runtime` | `examprep-runtime`：通用 Agent 运行时（循环、工具、事件溯源会话、lane、子 Agent、上下文组装） |
| `packages/app` | `examprep`：学习应用。目前包含资料引擎（PDF/DOCX/PPTX/文本解析、章节、题目、配图、注入检测、增量导入）和命令行 |
| `samples` | 示例课程与公开课下载脚本 |
| `docs/design` | 设计文档 |
| `.kiro/specs` | 各阶段的需求、设计、任务清单 |

## 开发

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov
```

在线测试（需要 API Key 或本地 Ollama，见 `tests/test_live.py` 开头的说明）：

```bash
uv run pytest -m live tests/test_live.py -rs
```

## 许可证

MIT
