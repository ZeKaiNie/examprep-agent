# ExamPrep Agent

> 靠谱：只讲你资料里有的，每句话都能翻到原文那一页。
>
> The study agent that cites every page, quizzes you with real exam questions, and remembers what you got wrong.

[![CI](https://github.com/ZeKaiNie/examprep-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ZeKaiNie/examprep-agent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**状态：P1 地基已完成（模型层与 Agent 运行时），学习功能尚在开发，暂不可用。**

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
