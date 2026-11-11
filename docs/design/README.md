# ExamPrep Agent 设计文档（v1.0，已确认）

| 文档 | 内容 |
|---|---|
| [01-skill-analysis.md](01-skill-analysis.md) | Skill 全部模块拆解、踩过的坑、要复活和要丢掉的设计、复用清单 |
| [02-product.md](02-product.md) | 定位、用户场景、竞品对比、产品形态、命名、传播 |
| [03-architecture.md](03-architecture.md) | 三层架构、仓库结构、技术选型、数据模型、关键流程 |
| [04-agent-core.md](04-agent-core.md) | 循环、上下文引擎、压缩、记忆、多 Agent、并发、持久化、反幻觉、安全、模型网关、可观测性 |
| [05-pedagogy.md](05-pedagogy.md) | 教学状态机、3×4 策略矩阵、BKT、FSRS、提示阶梯、计划器 |
| [06-roadmap.md](06-roadmap.md) | P0–P8 阶段与完成标准 |

## 已确定

| 日期 | 决策 |
|---|---|
| 2026-11-11 | 自研，不基于 pi；参考 Claude Code、Codex、OpenClaw/pi、Hermes、Letta、Mem0、opencode 的公开设计 |
| 2026-11-11 | Python 为主；核心循环、上下文引擎、模型网关自己写，不依赖 LangChain/LangGraph/LiteLLM |
| 2026-11-11 | 面向用户使用；benchmark 放在最后（P7）；暂无目标比赛 |
| 2026-11-11 | 支持主流云端模型和本地模型 |
| 2026-11-11 | 原 Skill 仓库不改动 |
| 2026-11-11 | 产品形态：本地优先 Web App + CLI 先行，MCP Server 和 IM 机器人第二批，桌面版第三批 |
| 2026-11-11 | 许可证 MIT（与 Skill 一致） |
| 2026-11-11 | 前端 React（Vite + TypeScript），构建产物打包进 Python 包，本地运行不需要 Node |
| 2026-11-11 | 项目名 **ExamPrep Agent**（中文口号“靠谱”），仓库 `ZeKaiNie/examprep-agent`；包名 `examprep-llm` / `examprep-runtime` / `examprep`。设计初稿中的名字 Kaopu 已全部替换，见 02 文档第 5 节 |
