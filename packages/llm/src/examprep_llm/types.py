"""统一的消息、内容块、工具声明、请求与用量模型（R2.1、R2.3）。

所有模型都是 Pydantic v2，带 ``type`` / ``role`` 判别字段，可直接 JSON 序列化进事件存储或 SSE。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StopReason = Literal["stop", "length", "tool_use", "content_filter", "error", "aborted"]
ReasoningLevel = Literal["off", "low", "medium", "high"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 内容块


class TextBlock(_Model):
    type: Literal["text"] = "text"
    text: str
    # Gemini 3 在文本 part 上也可能带 thoughtSignature
    signature: str | None = None


class ImageBlock(_Model):
    type: Literal["image"] = "image"
    media_type: str
    data: str  # base64，不含 data: 前缀


class ThinkingBlock(_Model):
    type: Literal["thinking"] = "thinking"
    text: str
    signature: str | None = None
    # Anthropic redacted_thinking：text 存放加密数据
    redacted: bool = False


class ToolCallBlock(_Model):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict[str, Any])
    # 参数 JSON 解析失败时保留原文，Agent 据此把错误回给模型
    raw_arguments: str | None = None
    signature: str | None = None


UserContent = Annotated[TextBlock | ImageBlock, Field(discriminator="type")]
AssistantContent = Annotated[TextBlock | ThinkingBlock | ToolCallBlock, Field(discriminator="type")]


def _coerce_content(value: object) -> object:
    """允许直接传字符串，自动转成单个文本块。"""
    if isinstance(value, str):
        return [TextBlock(text=value)]
    return value


# ---------------------------------------------------------------- 用量


class Usage(_Model):
    input_tokens: int = 0  # 未命中缓存的输入
    output_tokens: int = 0  # 含推理
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0  # output 的子集，仅统计

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


# ---------------------------------------------------------------- 消息


class SystemMessage(_Model):
    role: Literal["system"] = "system"
    content: str
    # 是否适合进入 prompt cache 前缀（Anthropic 会在最后一个可缓存块上打 cache_control）
    cacheable: bool = False


class UserMessage(_Model):
    role: Literal["user"] = "user"
    content: list[UserContent]

    @field_validator("content", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        return _coerce_content(value)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


class AssistantMessage(_Model):
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list[AssistantContent])
    provider: str = ""
    model: str = ""
    protocol: str = ""
    stop_reason: StopReason = "stop"
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def thinking(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, ThinkingBlock) and not b.redacted)

    @property
    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.content if isinstance(b, ToolCallBlock)]


class ToolResultMessage(_Model):
    role: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    content: list[UserContent]
    is_error: bool = False

    @field_validator("content", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> object:
        return _coerce_content(value)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="role"),
]


# ---------------------------------------------------------------- 请求


class ToolSpec(_Model):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema（object）


class LLMRequest(_Model):
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list[ToolSpec])
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: ReasoningLevel | None = None
    # 原样合并进请求体（顶层浅合并），用于厂商特有参数
    extra: dict[str, Any] = Field(default_factory=dict[str, Any])

    @property
    def system_messages(self) -> list[SystemMessage]:
        return [m for m in self.messages if isinstance(m, SystemMessage)]

    @property
    def conversation(self) -> list[UserMessage | AssistantMessage | ToolResultMessage]:
        return [m for m in self.messages if not isinstance(m, SystemMessage)]
