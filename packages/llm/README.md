# kaopu-llm

Kaopu 的模型层（L1），不依赖 Agent 和学习领域代码，可以单独使用。

- 统一的消息、内容块和流式事件模型
- 三种协议：OpenAI 兼容 Chat Completions、Anthropic Messages、Gemini `streamGenerateContent`
- 模型能力表：上下文长度、最大输出、工具调用、视觉、推理、prompt cache、价格
- 网关：按角色路由与降级链、重试（指数退避 + 抖动 + retry-after）、令牌桶限流、并发上限、熔断、取消、用量与成本记账
- `kaopu_llm.testing.FakeProvider`：脚本化的假模型，用于确定性测试

内置价格只是参考，可能过时，请以自己的配置为准。
