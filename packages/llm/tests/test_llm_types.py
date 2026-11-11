from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from examprep_llm import (
    AssistantMessage,
    DoneEvent,
    ImageBlock,
    LLMRequest,
    Message,
    StreamEvent,
    SystemMessage,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from examprep_llm.errors import AbortedError, LLMError, RateLimitError, ServerError

MESSAGES = TypeAdapter(list[Message])
EVENT = TypeAdapter(StreamEvent)


def test_user_message_accepts_string() -> None:
    m = UserMessage(content="你好")  # type: ignore[arg-type]
    assert m.content == [TextBlock(text="你好")]
    assert m.text == "你好"


def test_message_roundtrip_with_discriminator() -> None:
    msgs: list[Message] = [
        SystemMessage(content="sys", cacheable=True),
        UserMessage(content=[TextBlock(text="看图"), ImageBlock(media_type="image/png", data="AAAA")]),
        AssistantMessage(
            content=[
                ThinkingBlock(text="想", signature="s"),
                TextBlock(text="好"),
                ToolCallBlock(id="c1", name="f", arguments={"x": 1}),
            ],
            provider="p",
            model="m",
            protocol="openai",
            stop_reason="tool_use",
            usage=Usage(input_tokens=1, output_tokens=2),
        ),
        ToolResultMessage(tool_call_id="c1", tool_name="f", content="ok"),  # type: ignore[arg-type]
    ]
    data = MESSAGES.dump_python(msgs, mode="json")
    assert [d["role"] for d in data] == ["system", "user", "assistant", "tool_result"]
    back = MESSAGES.validate_python(data)
    assert back == msgs
    assistant = back[2]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.text == "好"
    assert assistant.thinking == "想"
    assert [c.id for c in assistant.tool_calls] == ["c1"]


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        TextBlock.model_validate({"type": "text", "text": "a", "bogus": 1})


def test_usage_add_and_totals() -> None:
    u = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2, reasoning_tokens=1)
    s = u + u
    assert s.input_tokens == 20
    assert s.reasoning_tokens == 2
    assert u.total_input_tokens == 15
    assert u.total_tokens == 20


def test_request_splits_system_and_conversation() -> None:
    req = LLMRequest(messages=[SystemMessage(content="a"), UserMessage(content="b")])  # type: ignore[arg-type]
    assert [m.content for m in req.system_messages] == ["a"]
    assert len(req.conversation) == 1


def test_event_discriminator() -> None:
    ev = EVENT.validate_python({"type": "text_delta", "index": 0, "text": "x"})
    assert isinstance(ev, TextDelta)
    done = EVENT.validate_python({"type": "done", "message": AssistantMessage().model_dump()})
    assert isinstance(done, DoneEvent)


def test_error_str_and_flags() -> None:
    e = RateLimitError("slow down", retry_after=2.0, status=429, provider="p", model="m")
    assert e.retryable
    assert e.retry_after == 2.0
    assert str(e) == "[rate_limit 429 p/m] slow down"
    assert ServerError("x").retryable
    assert not AbortedError("x").retryable
    assert str(LLMError("plain")) == "[error] plain"
