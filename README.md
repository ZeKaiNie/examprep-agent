# Kaopu（靠谱）

> 基于你自己的课件、作业和真题学习的开源学习 Agent。每句讲解都能点回原文页码。

**状态：P1 地基开发中，尚不可用。**

## 它要做什么

- 只依据学生上传的资料讲解；引用由系统生成，模型只写证据编号，写不出假页码
- 只用资料里的真题出题，规则判分 + 独立的判分 Agent
- 记得错题、困惑点和掌握度，用 FSRS 安排复习
- 期末突击和长期学习两种模式
- 支持主流云端模型（OpenAI 兼容、Anthropic、Gemini）和本地模型（Ollama、LM Studio、vLLM、llama.cpp）

## 仓库结构

| 路径 | 内容 |
|---|---|
| `packages/llm` | `kaopu-llm`：多厂商统一模型接口、能力表、网关 |
| `packages/agent` | `kaopu-agent`：通用 Agent 运行时 |
| `docs/design` | 设计文档 |

## 开发

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov
```

在线测试（需要 API Key 或本地 Ollama）：

```bash
uv run pytest -m live tests/test_live.py
```

## 许可证

MIT
