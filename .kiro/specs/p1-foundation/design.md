# P1 地基：设计

依据：`requirements.md`（R1–R10）、`docs/design/03-architecture.md`、`docs/design/04-agent-core.md`。
本文给出模块与文件清单、关键接口、关键行为和测试方案。代码注释用中文，标识符用英文。

## 0. 设计原则

1. **L1 不知道 Agent，L2 不知道学习**。`kaopu_llm` 只依赖 httpx、pydantic；`kaopu_agent` 只依赖 `kaopu_llm`、pydantic 和标准库。
2. **可确定性测试**。时间（`Clock`）、随机数（`random.Random`）、HTTP（`httpx` transport）全部可注入；重试、限流、熔断测试不真正睡眠。
3. **数据结构可序列化**。消息、事件都是 Pydantic v2 模型，带 `type`/`role` 判别字段，直接进事件存储和 SSE。
4. **取消是一等公民**。`CancelToken` 从 Agent 一路传到网关和工具；取消后 HTTP 流立即关闭。
5. **兼容 Python 3.11**。不用 3.12 专有语法（`type` 语句、类泛型新语法）。

## 1. 仓库结构

```
kaopu/                                  # 仓库根（即本工作区）
├ pyproject.toml                        # uv workspace 根（package = false）+ ruff/pyright/pytest/coverage 配置
├ uv.lock
├ .python-version                       # 3.12
├ LICENSE  README.md  .gitignore  .editorconfig
├ .github/workflows/ci.yml
├ packages/
│  ├ llm/   pyproject.toml  README.md  src/kaopu_llm/   tests/
│  └ agent/ pyproject.toml  README.md  src/kaopu_agent/ tests/
├ tests/                                # 跨包集成：三协议回放验收、在线测试
├ scripts/check_versions.py             # 校验各包版本号一致
└ docs/design/  .kiro/
```

- 构建后端：hatchling（src 布局）。两个包版本号同为 `0.1.0.dev0`，由 `tests/test_versions.py` 校验一致。
- 开发依赖放根 `[dependency-groups] dev`：pytest、pytest-asyncio、pytest-cov、ruff、`pyright[nodejs]`（自带 Node，不依赖本机 Node 16）。
- pytest 用 `--import-mode=importlib`，`asyncio_mode = "auto"`；在线测试标 `live`，默认跳过。

## 2. kaopu_llm（L1）

### 2.1 文件清单

| 文件 | 内容 |
|---|---|
| `types.py` | 内容块、消息、工具声明、`LLMRequest`、`Usage` |
| `events.py` | 流式事件 |
| `errors.py` | 统一错误类型 |
| `cancel.py` | `CancelToken`、`cancellable()` |
| `clock.py` | `Clock` 协议、`SystemClock` |
| `sse.py` | SSE 解码器（W3C 规范） |
| `accumulator.py` | `StreamAccumulator`：把增量拼成完整 `AssistantMessage`，三种协议共用 |
| `tokens.py` | token 估算（中日韩字符按 1、其他按 4 字符 1 token），截断 |
| `registry.py` | `Pricing`、`ModelInfo`、`ModelRegistry`、内置模型表、成本计算 |
| `config.py` | `ProviderConfig`、内置 provider 预设（base_url、协议、Key 环境变量） |
| `protocols/__init__.py` | `Provider` 协议、`build_provider()` |
| `protocols/_http.py` | HTTP 错误映射、retry-after 解析、SSE 请求封装 |
| `protocols/openai_compat.py` | OpenAI 兼容 Chat Completions |
| `protocols/anthropic.py` | Anthropic Messages |
| `protocols/gemini.py` | Gemini `streamGenerateContent?alt=sse` |
| `gateway/retry.py` | `RetryPolicy`、退避计算 |
| `gateway/ratelimit.py` | `TokenBucket`（RPM）、`ProviderLimiter`（令牌桶 + 并发信号量） |
| `gateway/breaker.py` | `CircuitBreaker`（closed / open / half_open） |
| `gateway/ledger.py` | `CallRecord`、`UsageLedger`（按角色、模型汇总） |
| `gateway/gateway.py` | `Gateway`、`GatewayConfig` |
| `testing.py` | `FakeProvider`、`FakeTurn`、`FakeClock`、SSE 构造工具（对下游包公开） |

### 2.2 消息模型（R2.1）

```python
class TextBlock(BaseModel):      type: Literal["text"]; text: str; signature: str | None = None
class ImageBlock(BaseModel):     type: Literal["image"]; media_type: str; data: str   # base64
class ThinkingBlock(BaseModel):  type: Literal["thinking"]; text: str; signature: str | None; redacted: bool = False
class ToolCallBlock(BaseModel):  type: Literal["tool_call"]; id: str; name: str
                                 arguments: dict[str, Any]; raw_arguments: str | None = None  # 解析失败时保留原文
                                 signature: str | None = None                                 # Gemini thoughtSignature

class SystemMessage(BaseModel):     role: Literal["system"]; content: str; cacheable: bool = False
class UserMessage(BaseModel):       role: Literal["user"]; content: list[TextBlock | ImageBlock]   # 接受 str 自动转换
class AssistantMessage(BaseModel):  role: Literal["assistant"]; content: list[TextBlock | ThinkingBlock | ToolCallBlock]
                                    provider: str; model: str; protocol: str
                                    stop_reason: StopReason; usage: Usage; error: str | None = None
class ToolResultMessage(BaseModel): role: Literal["tool_result"]; tool_call_id: str; tool_name: str
                                    content: list[TextBlock | ImageBlock]; is_error: bool = False

Message = Annotated[SystemMessage | UserMessage | AssistantMessage | ToolResultMessage, Field(discriminator="role")]
StopReason = Literal["stop", "length", "tool_use", "content_filter", "error", "aborted"]
```

约定：
- 所有 `SystemMessage` 被协议层提到最前（Anthropic `system`、Gemini `systemInstruction`、OpenAI 首部 system 消息）；`cacheable=True` 的最后一块打 Anthropic `cache_control`。
- 推理块和签名只在**同一协议**的消息回传时保留（`AssistantMessage.protocol` 判断），跨协议降级时丢弃，避免签名校验失败。
- 工具结果里的图片：Anthropic 原样传；OpenAI、Gemini 替换为文字占位。

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class LLMRequest(BaseModel):
    messages: list[Message]
    tools: list[ToolSpec] = []
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: Literal["off", "low", "medium", "high"] | None = None
    extra: dict[str, Any] = {}  # 原样合并进请求体，处理厂商特有参数
```

### 2.3 用量与成本（R2.3）

```python
class Usage(BaseModel):
    input_tokens: int = 0         # 未命中缓存的输入
    output_tokens: int = 0        # 含推理
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0     # output 的子集，仅统计
    def __add__(...)

class Pricing(BaseModel):  input: float; output: float; cache_read: float | None; cache_write: float | None; currency: str = "USD"
                           # 单位：每百万 token
class Cost(BaseModel):     input; output; cache_read; cache_write; total; currency
def compute_cost(usage, pricing) -> Cost | None                  # 无价格返回 None
```

各家归一化：
| 协议 | input | cache_read | cache_write | output |
|---|---|---|---|---|
| OpenAI 兼容 | `prompt_tokens − cached_tokens`；DeepSeek 用 `prompt_cache_miss_tokens` | `prompt_tokens_details.cached_tokens` 或 `prompt_cache_hit_tokens` | 0 | `completion_tokens` |
| Anthropic | `input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` | `output_tokens`（以 `message_delta` 的累计值为准） |
| Gemini | `promptTokenCount − cachedContentTokenCount` | `cachedContentTokenCount` | 0 | `candidatesTokenCount + thoughtsTokenCount` |

### 2.4 流式事件（R2.2）

```python
StartEvent(type="start", provider, model)
TextDelta(type="text_delta", index, text)               # index = 最终消息 content 中的块下标
ThinkingDelta(type="thinking_delta", index, text)
ToolCallStart(type="tool_call_start", index, id, name)
ToolCallDelta(type="tool_call_delta", index, arguments_delta)
ToolCallEnd(type="tool_call_end", index, call: ToolCallBlock)
DoneEvent(type="done", message: AssistantMessage)       # 带 usage、stop_reason
StreamEvent = Annotated[Union[...], Field(discriminator="type")]
```

错误不作为事件，而是抛出 `LLMError`。`StreamAccumulator` 提供 `start() / text() / thinking() / tool_start(key, id, name) / tool_args(key, delta) / tool_end(key) / set_signature() / finish(stop_reason, usage)`，每个方法返回要 yield 的事件；`snapshot()` 返回当前部分消息（取消时用）。

### 2.5 错误（R3.4）

```python
class LLMError(Exception):        retryable: ClassVar[bool] = False; status: int | None; provider: str | None; model: str | None
class AuthenticationError(LLMError)                         # 401/403、Gemini "API key not valid"
class RateLimitError(LLMError):   retryable = True; retry_after: float | None   # 429，解析 retry-after / retry-after-ms / Gemini RetryInfo
class ContextOverflowError(LLMError)                        # 各家“上下文超长”文本与错误码
class BadRequestError(LLMError)                             # 其余 4xx
class ServerError(LLMError):      retryable = True          # 5xx、Anthropic 529 overloaded、流中 error 事件
class LLMTimeoutError(LLMError):  retryable = True          # httpx.TimeoutException
class NetworkError(LLMError):     retryable = True          # httpx.TransportError
class AbortedError(LLMError)                                # 调用方取消
class NoModelAvailableError(LLMError)                       # 降级链上全部熔断
```

### 2.6 取消（R5.5）

```python
class CancelToken:
    def __init__(self, parent: CancelToken | None = None)   # 父取消则子取消
    def cancel(self, reason: str = "cancelled") -> None
    cancelled: bool; reason: str | None
    def on_cancel(self, cb: Callable[[], None]) -> Callable[[], None]   # 返回注销函数
    async def wait(self) -> None
    def raise_if_cancelled(self) -> None                    # 抛 AbortedError
    def child(self) -> CancelToken

async def cancellable(source: AsyncIterator[T], token: CancelToken | None) -> AsyncIterator[T]
```

`cancellable` 在独立任务里消费 `source`（provider 的异步生成器连同 `async with client.stream()` 始终在同一任务里，避免 anyio 跨任务退出 cancel scope），事件经队列转交；令牌取消时直接 `task.cancel()`，HTTP 响应随生成器退出关闭，调用方收到 `AbortedError`。

### 2.7 SSE 解码（R3）

`SSEDecoder.decode(line) -> SSEEvent | None` 按 W3C 规则处理 `data`（多行拼接）、`event`、`id`、`retry`、注释行、BOM、空行分发；`aiter_sse(response)` 基于 `response.aiter_lines()`。OpenAI 的 `data: [DONE]` 由协议层识别。

### 2.8 协议适配

```python
class Provider(Protocol):
    name: str; config: ProviderConfig
    def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]
    async def aclose(self) -> None

def build_provider(config: ProviderConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> Provider
```

| 协议 | 端点 | 鉴权 | 要点 |
|---|---|---|---|
| `openai` | `POST {base}/chat/completions` | `Authorization: Bearer` | `stream_options.include_usage`；`delta.reasoning_content`（DeepSeek/Qwen/vLLM）与 `delta.reasoning`（OpenRouter/Ollama）都识别为推理；`tool_calls[].index` 聚合；流中 `{"error": ...}` 映射为错误；`reasoning` → `reasoning_effort` |
| `anthropic` | `POST {base}/v1/messages` | `x-api-key` + `anthropic-version: 2023-06-01` | 连续的 user / tool_result 合并成一个 user 消息；`thinking` + `signature_delta`；`redacted_thinking`；`cache_control`；`reasoning` → `thinking.budget_tokens`（low 2048 / medium 8192 / high 16384） |
| `gemini` | `POST {base}/v1beta/models/{m}:streamGenerateContent?alt=sse` | `x-goog-api-key` | `role: model`；函数调用整块到达，无 id 时生成 `call_<n>`；`functionResponse` 需要工具名（来自 `ToolResultMessage.tool_name`）；`thought: true` 为推理；`thoughtSignature` 回传；JSON Schema 去掉 `$defs/title/additionalProperties/default` 等不支持字段 |

停止原因映射：OpenAI `stop/length/tool_calls/content_filter`；Anthropic `end_turn/stop_sequence/max_tokens/tool_use/refusal`；Gemini `STOP/MAX_TOKENS/SAFETY/RECITATION/...`，有函数调用时为 `tool_use`。

### 2.9 能力表与 provider 配置（R4）

```python
class ModelInfo(BaseModel):
    id: str; provider: str; protocol: Literal["openai", "anthropic", "gemini", "fake"]
    context_window: int; max_output: int
    supports_tools: bool; supports_vision: bool; supports_reasoning: bool; supports_prompt_cache: bool
    pricing: Pricing | None

class ModelRegistry:
    def __init__(self, overrides: Iterable[ModelInfo] = (), *, include_builtin: bool = True)
    def get(self, provider: str, model_id: str, protocol: str) -> ModelInfo   # 覆盖 > 内置 > 保守默认
    def register(self, info: ModelInfo) -> None
```

- 模型引用写成 `"provider/model"`，只按第一个 `/` 切分（OpenRouter 模型名本身含 `/`）。
- 保守默认值：上下文 8192、最大输出 2048、支持工具调用、其余能力关闭、无价格。本地 provider（ollama、lmstudio、vllm、llamacpp）价格为 0。
- 内置价格标注 `PRICING_AS_OF`，README 说明“以用户配置为准”。

```python
class ProviderConfig(BaseModel):
    name: str
    protocol: Literal["openai", "anthropic", "gemini", "fake"]
    base_url: str
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    headers: dict[str, str] = {}
    timeout: float = 120.0
    connect_timeout: float = 10.0
    rpm: int | None = None
    max_concurrency: int | None = None


PROVIDER_PRESETS: dict[
    str, ProviderConfig
]  # openai deepseek qwen glm kimi siliconflow openrouter ollama lmstudio vllm llamacpp anthropic gemini
```

### 2.10 网关（R5）

```python
class RetryPolicy(BaseModel):    max_attempts: int = 3; base_delay: float = 0.5; max_delay: float = 20.0; max_retry_after: float = 60.0
class BreakerPolicy(BaseModel):  failure_threshold: int = 5; cooldown: float = 30.0

class GatewayConfig(BaseModel):
    providers: dict[str, ProviderConfig]          # 可只写名字和 key，缺省字段从预设补齐
    models: list[ModelInfo] = []                  # 能力表覆盖
    roles: dict[str, list[str]]                   # 角色 -> 降级链，如 {"tutor": ["deepseek/deepseek-chat", "ollama/qwen3:8b"]}
    retry: RetryPolicy; breaker: BreakerPolicy

class Gateway:
    def __init__(self, providers: Mapping[str, Provider], roles: Mapping[str, Sequence[str]], *,
                 registry: ModelRegistry | None = None, retry: RetryPolicy = ..., breaker: BreakerPolicy = ...,
                 clock: Clock | None = None, rng: random.Random | None = None)
    @classmethod
    def from_config(cls, config: GatewayConfig, *, transport=None) -> Gateway
    def stream(self, request: LLMRequest, *, role: str | None = None, model: str | None = None,
               cancel: CancelToken | None = None) -> AsyncIterator[StreamEvent]
    async def complete(self, request, **kw) -> AssistantMessage
    ledger: UsageLedger
    def on_call(self, cb: Callable[[CallRecord], None]) -> None
    async def aclose(self) -> None
```

`stream` 的流程：

```
chain = roles[role] 或 [model]
for ref in chain:
    if breaker[ref] 拒绝: 记录跳过, 下一个
    for attempt in 1..max_attempts:
        async with limiter[provider]:                     # 令牌桶取 1 个令牌 + 并发信号量（持有到流结束）
            try: 逐个转发 provider 事件（经 cancellable 包装）; emitted = True
            except AbortedError: 记录 aborted; 抛出
            except LLMError as e:
                breaker 记失败（仅 retryable 错误与鉴权错误）
                if emitted: 抛出                           # 已向调用方输出过内容，不重试不降级
                if not e.retryable: 鉴权错误 → 降级；请求错误、上下文超长 → 直接抛出
                计算延迟 = max(retry_after, 指数退避 × 抖动[0.5,1])；retry_after > max_retry_after → 直接降级
                clock.sleep(延迟)（可被取消）
        成功: breaker 记成功；记账；return
全部失败: 抛最后一个错误；若全部因熔断跳过，抛 NoModelAvailableError
```

- 熔断器按模型引用独立：连续失败达阈值 → open；`cooldown` 后 half_open，只放行一个试探请求，成功 → closed，失败 → 重新 open。
- `CallRecord`：role、model_ref、attempts、fallback_from、usage、cost、ttft、latency、outcome（ok / error / aborted）、error_type。`UsageLedger.by_role()`、`by_model()`、`total()`。

### 2.11 FakeProvider（测试用，对外公开）

```python
@dataclass
class FakeTurn:
    text: str = ""; thinking: str = ""; tool_calls: list[tuple[str, dict[str, Any]]] = []
    usage: Usage = Usage(input_tokens=10, output_tokens=5); stop_reason: StopReason | None = None
    chunk_size: int = 0                  # >0 时把文本按长度切成多个增量
    gate: asyncio.Event | None = None    # 输出第一个增量后等待该事件（用于取消测试）
    error: LLMError | None = None        # 在开始前抛出
    error_after_output: LLMError | None = None

class FakeProvider:                      # 实现 Provider 协议，protocol = "fake"
    def __init__(self, script: Sequence[FakeTurn | LLMError | Callable[[LLMRequest], FakeTurn]], name="fake")
    requests: list[LLMRequest]           # 记录收到的请求
```

`FakeClock`：`now()` 返回虚拟时间；`sleep(d)` 推进虚拟时间并 `await asyncio.sleep(0)`。

## 3. kaopu_agent（L2）

### 3.1 文件清单

| 文件 | 内容 |
|---|---|
| `events.py` | Agent 事件 |
| `tools.py` | `Tool`、`@tool` 装饰器、`ToolContext`、`ToolOutput`、JSON Schema 生成（内联 `$ref`） |
| `hooks.py` | `Hooks` 基类 |
| `budget.py` | `Budget`、重复调用检测 |
| `loop.py` | `Agent`：循环、工具执行、steer / follow_up、取消 |
| `session/store.py` | `EventStore` 协议、`StoredEvent`、`SessionInfo` |
| `session/memory.py` | `InMemoryEventStore` |
| `session/sqlite.py` | `SQLiteEventStore`（WAL、只追加触发器） |
| `session/projection.py` | `project_messages(events)` |
| `session/recorder.py` | `SessionRecorder`：把 Agent 事件写入存储 |
| `lane.py` | `LaneManager`：会话串行、会话间并行、steer / follow_up / collect / queue |
| `subagent.py` | `run_subagent()`、`SubAgentResult` |
| `context.py` | `ContextSection`、`ContextAssembler`、`AssembledContext` |

### 3.2 LLM 客户端协议

Agent 只依赖下面的协议，`Gateway` 天然满足：

```python
class LLMClient(Protocol):
    def stream(
        self,
        request: LLMRequest,
        *,
        role: str | None = None,
        model: str | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
```

### 3.3 事件（R6.1）

```python
AgentStart(type="agent_start", run_id)
AgentEnd(type="agent_end", run_id, reason: EndReason, error: str | None, new_messages: list[Message])
TurnStart(type="turn_start", turn)
TurnEnd(type="turn_end", turn, message: AssistantMessage | None, tool_results: list[ToolResultMessage])
MessageStart(type="message_start", role, message: Message | None)     # assistant 流式开始时 message 为 None
MessageUpdate(type="message_update", event: StreamEvent)
MessageEnd(type="message_end", message: Message)                      # user / assistant / tool_result 都发
ToolStart(type="tool_start", call: ToolCallBlock)
ToolUpdate(type="tool_update", call_id, tool_name, data: Any)
ToolEnd(type="tool_end", call_id, tool_name, result: ToolResultMessage, details: Any)
EndReason = Literal["completed", "aborted", "error", "max_turns", "max_tool_calls", "loop_detected"]
```

### 3.4 工具（R6.2、R6.3）

```python
class ToolOutput(BaseModel):  content: str | list[TextBlock | ImageBlock]; is_error: bool = False
                              terminate: bool = False; details: Any = None      # details 只给 UI，不发给模型
class ToolContext:            call_id; tool_name; cancel: CancelToken; agent: Agent
                              def update(self, data: Any) -> None               # 产生 ToolUpdate 事件
class Tool(ABC, Generic[P]):
    name: str; description: str; params: type[P]; sequential: bool = False; timeout: float | None = None
    @abstractmethod
    async def run(self, params: P, ctx: ToolContext) -> ToolOutput | str
    def spec(self) -> ToolSpec                                  # model_json_schema() 内联 $ref、去掉顶层 title

def tool(*, name=None, description=None, sequential=False, timeout=None)   # 装饰 async def f(params: Model, ctx) 
```

执行一批工具调用：
1. **预检（按模型给出的顺序串行）**：未知工具 → 错误结果；参数 JSON 解析失败 → 错误结果；Pydantic 校验失败 → 把校验错误回给模型；预算与重复检测；`before_tool` 钩子返回 `ToolBlocked(reason)` 则拦截。
2. **执行**：通过预检的调用按顺序分组，连续的非 sequential 工具组成一组并发执行（`asyncio.TaskGroup` 语义，单个失败不影响其他），sequential 工具单独成组；组与组之间串行。工具异常 → `is_error`；超时 → `is_error`。
3. **写回**：`after_tool` 钩子可改写结果；`ToolEnd` 事件按完成顺序发出，`ToolResultMessage` 按模型给出的顺序追加到对话。
4. 本批全部结果 `terminate=True` 时，本批后结束循环。

### 3.5 钩子（R6.4）

```python
class Hooks:
    async def prepare_request(self, ctx: TurnContext, request: LLMRequest) -> LLMRequest:
        return request

    async def before_tool(self, ctx: ToolCallContext) -> ToolBlocked | None:
        return None

    async def after_tool(self, ctx: ToolCallContext, output: ToolOutput) -> ToolOutput:
        return output

    async def finish_turn(self, ctx: TurnContext) -> Literal["end", "continue"] | None:
        return None
```

### 3.6 Agent 循环（R6）

```python
class Agent:
    def __init__(self, llm: LLMClient, *, system: str | list[SystemMessage] | None = None, tools: Sequence[Tool] = (),
                 hooks: Hooks | None = None, budget: Budget | None = None, role: str | None = None, model: str | None = None,
                 messages: Sequence[Message] = (), request_defaults: RequestDefaults | None = None)
    messages: list[Message]                     # 对话记录（不含 system）
    running: bool
    def run(self, input: str | Message | Sequence[Message] | None = None, *, cancel: CancelToken | None = None) -> AsyncIterator[AgentEvent]
    def steer(self, message: str | UserMessage) -> None
    def follow_up(self, message: str | UserMessage) -> None
    def abort(self, reason: str = "user") -> None
    def drain_queues(self) -> list[UserMessage]  # lane 在运行结束后取走残留消息
```

主循环与 `04-agent-core.md` 第 1 节一致：

```
AgentStart
pending = 输入
loop:
    若 turn >= max_turns：reason = max_turns，结束
    TurnStart；把 pending 追加到对话（MessageStart/End）
    request = 默认请求（system + messages + tools + 默认参数）→ hooks.prepare_request
    流式调用 llm（MessageStart → MessageUpdate* → MessageEnd）
        AbortedError → 部分消息（去掉工具调用）stop_reason=aborted，结束
        LLMError     → 错误消息 stop_reason=error，结束
    有工具调用 → 执行工具批次（3.4）；取消 / 超预算 / 重复调用 → 结束
    decision = hooks.finish_turn；TurnEnd
    全部 terminate 且 decision != continue → 结束
    有 steer → pending = 全部 steer；继续
    decision == end → 结束
    有工具调用或 decision == continue → pending = []；继续
    有 follow_up → pending = 全部 follow_up；继续
    结束
AgentEnd(reason)
```

- 一次只允许一个 `run`；重入抛 `RuntimeError`。
- 取消：`run` 内部创建子令牌；`abort()` 或外部令牌取消时，模型流立即中断；执行中的工具任务被取消并得到 `is_error` 的“已取消”结果，保证每个工具调用都有结果，对话可继续。
- 工具结果在模型请求前不会缺失：被跳过的调用（预算、重复）也写入错误结果。

### 3.7 预算（R6.7）

```python
class Budget(BaseModel):
    max_turns: int = 20
    max_tool_calls: int = 50
    max_repeat_calls: int = 3
```

- 每次 `run` 重新计数。
- 重复检测：签名 = 工具名 + 参数 JSON（`sort_keys`）。连续出现的相同签名达到 `max_repeat_calls` 时，该调用不执行，写错误结果，结束原因 `loop_detected`。
- 工具调用计数超过 `max_tool_calls`：超出部分不执行，写错误结果，结束原因 `max_tool_calls`。

### 3.8 会话存储（R7）

```python
@dataclass(frozen=True)
class StoredEvent:  session_id: str; seq: int; type: str; payload: dict[str, Any]; created_at: datetime
@dataclass(frozen=True)
class SessionInfo:  id: str; parent_id: str | None; fork_at_seq: int | None; created_at: datetime; metadata: dict[str, Any]

class EventStore(Protocol):
    async def create_session(self, *, metadata: dict[str, Any] | None = None, session_id: str | None = None) -> SessionInfo
    async def get_session(self, session_id: str) -> SessionInfo | None
    async def append(self, session_id: str, type: str, payload: dict[str, Any]) -> StoredEvent
    async def events(self, session_id: str, *, upto_seq: int | None = None) -> list[StoredEvent]   # 含祖先
    async def last_seq(self, session_id: str) -> int
    async def fork(self, session_id: str, at_seq: int, *, metadata=None) -> SessionInfo
    async def close(self) -> None
```

- seq 沿谱系连续：子会话的第一个事件 seq = `fork_at_seq + 1`。`events(child)` = 祖先 seq ≤ fork_at_seq 的事件 + 自身事件。
- SQLite：`journal_mode=WAL`、`synchronous=NORMAL`、外键开启；单连接 + 线程锁，经 `asyncio.to_thread` 调用；追加在 `BEGIN IMMEDIATE` 事务里算 seq；`events` 表有禁止 UPDATE/DELETE 的触发器。
- 事件类型：`message`（payload = 消息 JSON）、`run_end`（reason、error）。`project_messages` 只投影 `message`，后续阶段再加 `compaction` 等。
- `SessionRecorder(store, session_id).record(event)`：`MessageEnd` → `message`；`AgentEnd` → `run_end`。

### 3.9 Lane（R8）

```python
SubmitPolicy = Literal["queue", "steer", "follow_up", "collect"]
class LaneManager:
    def __init__(self, agent_factory: Callable[[str], Awaitable[Agent]], *,
                 on_event: Callable[[str, AgentEvent], Awaitable[None]] | None = None,
                 collect_window: float = 0.8, clock: Clock | None = None)
    async def submit(self, session_id: str, message: str | UserMessage, policy: SubmitPolicy = "queue") -> Literal["started", "queued", "steered", "follow_up", "collected"]
    def abort(self, session_id: str) -> bool
    async def wait_idle(self, session_id: str | None = None) -> None
    def is_running(self, session_id: str) -> bool
    async def close(self) -> None
```

- 每个会话一个 lane：`asyncio.Queue` + 单个消费任务，任务按顺序 `agent.run()`，事件转发给 `on_event`；队列空时消费任务退出、lane 回收（Agent 由工厂按需重建）。
- 空闲时任何策略都会排队成为新一次运行；`collect` 空闲时等待 `collect_window` 收集更多消息再合并运行。
- 运行中：`steer` → `agent.steer`；`follow_up` → `agent.follow_up`；`queue` → 当前运行结束后单独运行；`collect` → 缓冲，当前运行结束后合并为一条消息运行。
- 运行结束后调用 `agent.drain_queues()`，把来不及注入的 steer / follow_up 作为下一次运行，消息不丢。
- 消费任务里单次运行抛出的意外异常被记录日志并转为 `AgentEnd(reason="error")` 事件，不影响后续运行。

### 3.10 子 Agent（R9）

```python
@dataclass
class SubAgentResult(Generic[T]):  ok: bool; value: T | None; error: str | None; usage: Usage; turns: int; messages: list[Message]

async def run_subagent(llm: LLMClient, result_type: type[T], *, instructions: str, input: str | Sequence[Message],
                       tools: Sequence[Tool] = (), role: str | None = None, model: str | None = None,
                       budget: Budget | None = None, timeout: float | None = 60.0, max_nudges: int = 1,
                       cancel: CancelToken | None = None, system_prefix: Sequence[SystemMessage] = ()) -> SubAgentResult[T]
```

- 自动添加 `submit_result` 工具，参数模型就是 `result_type`，调用成功即 `terminate`。
- 模型自然结束却没提交：追加提醒消息再跑，最多 `max_nudges` 次，仍没有则失败。
- 超时：`call_later` 取消子令牌，结果为 `error="timeout"`。父令牌取消同样中止子 Agent。
- 所有 `LLMError` 和工具层问题都转为 `ok=False`，不向父 Agent 抛出；只有 `asyncio.CancelledError` 原样传播。
- `system_prefix` 用于复用父 Agent 的 L0 前缀以命中缓存。

### 3.11 上下文组装（R10）

```python
class ContextSection(BaseModel):
    name: str; content: str; priority: int = 0          # 数字越大越重要
    cacheable: bool = False; budget: int | None = None   # 本层上限（token）
    truncatable: bool = True                             # False：不裁，超了报告 overflow

class SectionReport(BaseModel):  name; tokens_before; tokens_after; truncated: bool; dropped: bool; cacheable
class AssembledContext(BaseModel):
    sections: list[ContextSection]; reports: list[SectionReport]; total_tokens: int; budget: int; overflow: bool
    def to_system_messages(self) -> list[SystemMessage]   # 相邻同缓存属性的层合并

class ContextAssembler:
    def __init__(self, budget: int, *, counter: Callable[[str], int] = estimate_tokens)
    def assemble(self, sections: Sequence[ContextSection]) -> AssembledContext
```

算法：先把每层裁到自身 `budget`；总量仍超出时，按优先级从低到高裁剪可裁层（先截尾到剩余额度，额度为 0 则整层丢弃）；输出保持输入顺序（稳定层在前以命中缓存）；每层给出前后 token 数。截断按 token 估算二分，截断处加“…（已截断）”标记。

## 4. 测试方案

| 范围 | 方法 |
|---|---|
| SSE | 规范用例：多行 data、CRLF、注释、BOM、不以空行结尾、字节随机切块 |
| 三种协议 | `tests/fixtures/*.sse` 存各家真实格式的流（文本、推理、工具调用、usage、错误事件）；httpx `MockTransport` 回放，断言事件序列、最终消息、用量；断言请求体格式（消息转换、工具声明、tool_result 合并、签名回传） |
| 错误映射 | 参数化：401/403/429(+retry-after)/400 超长/400/413/500/529/504、超时、连接失败、流中错误 |
| 网关 | FakeProvider + FakeClock：重试次数与延迟、已输出后不重试、retry-after、降级链、熔断 open/half_open/closed、限流令牌桶速率、并发上限、取消立即中断 HTTP（自定义 `AsyncByteStream` 断言已关闭）、记账 |
| Agent 循环 | FakeProvider 驱动：事件顺序、并行工具写回顺序、sequential 串行、参数校验错误、工具异常、超时、钩子四种、steer/follow_up、取消（流中与工具中）、预算三种、terminate |
| 会话存储 | 两种实现跑同一组用例（参数化 fixture）：seq 递增、并发追加不重号、分叉、谱系投影、只追加触发器 |
| Lane | 同会话并发提交断言串行（无交叠）、不同会话并行、四种策略、drain、异常隔离 |
| 子 Agent | 正常提交、校验失败后重试、提醒、超时、LLM 错误、父取消 |
| 上下文 | 预算截断、优先级、不可裁层溢出、报告 |
| 验收 | `tests/test_acceptance_replay.py`：同一“调用工具 → 根据结果回答”脚本在三种协议回放中通过 |
| 在线 | `tests/test_live.py`（`-m live`）：DeepSeek / Anthropic / Gemini 需 Key，Ollama 需本地服务；无则跳过 |

覆盖率目标：`kaopu_llm`、`kaopu_agent` 合计 ≥ 85%，CI 中 `--cov-fail-under=85`。

## 5. 暂不做（留给后续阶段）

- tiktoken 精确计数、厂商 usage 校正（P4 与压缩一起做）；P1 用估算。
- TPM 限流、对冲请求、OpenTelemetry（P6）。
- 不支持工具调用的模型改用 JSON 模式（P3 有需要时做）。
- 压缩四级（P4）。
