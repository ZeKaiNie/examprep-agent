# kaopu-agent

Kaopu 的通用 Agent 运行时（L2），只依赖 `kaopu-llm`，不含学习领域代码。

- 异步生成器 Agent 循环：事件流、并行工具、钩子、steer / follow_up、取消、预算与死循环保护
- Pydantic 定义工具参数，自动生成 JSON Schema
- 事件溯源会话存储（内存 / SQLite WAL），支持分叉
- 会话 lane：同会话串行、跨会话并行
- 子 Agent：独立上下文，通过 `submit_result` 返回 Pydantic 结构体
- 分层上下文组装与 token 预算
