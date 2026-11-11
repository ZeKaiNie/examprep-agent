# P1 地基：需求

范围：`kaopu-llm`（L1 模型层）与 `kaopu-agent`（L2 运行时）两个包，以及仓库工程化。
不含任何学习领域代码。设计依据：`docs/design/03-architecture.md`、`docs/design/04-agent-core.md`。

## R1 仓库与工程化
1. uv workspace，包含 `packages/llm`、`packages/agent`，Python ≥ 3.11，版本号同步。
2. ruff（lint + format）、pyright（strict，作用于两个包的 src）、pytest + pytest-asyncio。
3. GitHub Actions：Windows / Linux / macOS × Python 3.11 / 3.12 运行 lint、类型检查、测试。
4. MIT 许可证、`.gitignore`、`.editorconfig`、README 初版。

## R2 统一消息与事件模型
1. 消息：system / user / assistant / tool_result；内容块：文本、图片、工具调用、推理内容。
2. 流式事件：开始、文本增量、推理增量、工具调用开始/参数增量/结束、完成（带完整消息、用量、停止原因）。
3. 用量：输入、输出、缓存读、缓存写 token；按模型价格计算成本。

## R3 三种协议
1. OpenAI 兼容 Chat Completions（流式），覆盖 OpenAI、DeepSeek、通义、GLM、Kimi、硅基流动、OpenRouter、Ollama、LM Studio、vLLM、llama.cpp；支持 `reasoning_content` 推理字段与流式 usage。
2. Anthropic Messages（流式），支持工具调用、thinking、prompt cache 用量。
3. Gemini `streamGenerateContent`（SSE），支持函数调用与 usageMetadata。
4. 三者都把 HTTP 错误映射为统一错误类型：鉴权、限流（含 retry-after）、上下文超长、请求错误、服务端错误、超时、网络。
5. 用 httpx MockTransport 回放各家真实格式的 SSE 做测试；另有需要 API Key 的在线测试，默认跳过。

## R4 模型能力表
1. 每个模型记录：provider、协议、上下文长度、最大输出、工具调用、视觉、推理、prompt cache、价格。
2. 内置常用模型条目；用户配置可覆盖和新增；未知模型给保守默认值。

## R5 模型网关
1. 按角色（tutor / grader / verifier / extractor / summarizer）配置模型与降级链。
2. 重试：仅可重试错误，且仅在尚未向调用方输出任何内容时；指数退避加抖动，尊重 retry-after。
3. 限流：每 provider 的令牌桶（RPM）与并发上限。
4. 熔断：每个模型连续失败达到阈值即打开，冷却后半开试探。
5. 取消：调用方取消时立即中断 HTTP 流。
6. 记录每次调用的用量与成本。

## R6 Agent 循环
1. 异步生成器输出事件：agent/turn/message/tool 的 start/update/end。
2. 工具：Pydantic 参数模型自动生成 JSON Schema；参数校验失败或工具异常以错误结果回给模型；可声明串行执行。
3. 默认并行执行工具，结果按模型给出的顺序写回。
4. 钩子：prepare_request、before_tool（可拦截）、after_tool（可改写）、finish_turn（结束/继续）。
5. steer（工具批次后注入）与 follow_up（自然结束后注入）队列。
6. 取消：可在任意时刻中断，产生 aborted 结果。
7. 预算：最大轮数、最大工具调用数；相同工具与参数连续调用 3 次即停止。

## R7 会话存储（事件溯源）
1. 只追加的事件表，按会话递增 seq。
2. 由事件投影出消息列表；支持从任意 seq 分叉新会话。
3. SQLite 实现（WAL），接口可替换。

## R8 会话 lane
1. 同一会话的请求串行执行；不同会话并行。
2. 运行中到达的新消息按 steer / follow_up / collect 策略处理。

## R9 子 Agent
1. 以独立上下文运行一个子 Agent，通过 `submit_result` 工具返回 Pydantic 结构体。
2. 独立超时与预算；失败时返回错误而不影响父 Agent。

## R10 上下文组装（P1 只做组装与预算，压缩在 P4）
1. 分层 section：名称、内容、优先级、是否可缓存、预算。
2. 按预算截断低优先级层；输出每层 token 统计。

## 验收
- 同一段“调用工具 → 根据结果回答”的脚本，在 OpenAI 兼容、Anthropic、Gemini 三种协议的回放测试中通过。
- 若环境中有 API Key 或本地 Ollama，在线测试通过（无则如实标注未验证）。
- 并发、取消、重试、熔断、lane 串行均有确定性测试。
- 两个核心包测试覆盖率 ≥ 85%；ruff 与 pyright 零错误。
