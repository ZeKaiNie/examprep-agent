# -*- coding: utf-8 -*-
"""Model-provider abstraction.

The agent kernel only depends on the small :class:`ModelProvider` protocol.  Two
implementations ship in P0:

- :class:`OpenAICompatibleProvider` — talks to any OpenAI-compatible
  ``/chat/completions`` endpoint using only the standard library.  Configuration comes
  from the environment (``EXAMPREP_MODEL``, ``EXAMPREP_BASE_URL``,
  ``DEEPSEEK_API_KEY`` / ``OPENAI_API_KEY``); the default is DeepSeek's
  ``deepseek-flash``.
- :class:`FakeProvider` — replays a scripted list of responses and never touches the
  network, so the whole test suite runs offline.

No API key is ever logged or written to disk.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"


class ProviderError(RuntimeError):
    """Raised when a provider call fails (transport error, bad response, missing key)."""


@dataclass(frozen=True)
class ChatMessage:
    """One chat message in a provider-neutral shape."""

    role: str                      # system | user | assistant | tool
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """JSON-schema description of a tool exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token usage reported by (or estimated for) a completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Prompt plus completion tokens."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class ModelResponse:
    """A provider response: either final text or one or more tool calls."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    raw: dict[str, Any] | None = None


class ProviderConfig(BaseModel):
    """Validated provider configuration loaded from the environment."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(default=DEFAULT_MODEL, description="Model identifier sent to the endpoint")
    base_url: str = Field(default=DEFAULT_BASE_URL, description="OpenAI-compatible API base URL")
    api_key: str | None = Field(default=None, description="Bearer token; never persisted")
    timeout: float = Field(default=60.0, gt=0, description="HTTP timeout in seconds")
    temperature: float = Field(default=0.0, ge=0, le=2)


@runtime_checkable
class ModelProvider(Protocol):
    """The interface the agent loop depends on."""

    name: str

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
    ) -> ModelResponse:
        """Return the next assistant turn for ``messages``, optionally calling tools."""
        ...


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    """Encode a ChatMessage in OpenAI chat-completions format."""
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    if message.name:
        payload["name"] = message.name
    return payload


class OpenAICompatibleProvider:
    """Provider for any OpenAI-compatible ``/chat/completions`` endpoint (stdlib HTTP)."""

    def __init__(self, config: ProviderConfig) -> None:
        if not config.api_key:
            raise ProviderError("an API key is required")
        self.config = config
        self.name = "openai-compatible:%s" % config.model

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenAICompatibleProvider | None:
        """Build a provider from environment variables, or return None when no key is set."""
        env = os.environ if env is None else env
        key = env.get("DEEPSEEK_API_KEY") or env.get("OPENAI_API_KEY")
        if not key:
            return None
        config = ProviderConfig(
            model=env.get("EXAMPREP_MODEL") or DEFAULT_MODEL,
            base_url=env.get("EXAMPREP_BASE_URL") or DEFAULT_BASE_URL,
            api_key=key,
        )
        return cls(config)

    def _endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
    ) -> ModelResponse:
        """POST the conversation to the endpoint and normalise the response."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [_message_payload(m) for m in messages],
            "temperature": self.config.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters or {"type": "object", "properties": {}},
                    },
                }
                for tool in tools
            ]
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer %s" % self.config.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ProviderError("HTTP %s: %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - network path
            raise ProviderError("transport error: %s" % exc) from exc
        try:
            data = json.loads(body)
            choice = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:  # pragma: no cover - bad response
            raise ProviderError("malformed response: %s" % body[:300]) from exc
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(choice.get("tool_calls") or []):
            function = raw_call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {"_raw": function.get("arguments")}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            calls.append(ToolCall(
                id=raw_call.get("id") or "call_%d" % (index + 1),
                name=function.get("name") or "",
                arguments=arguments,
            ))
        raw_usage = data.get("usage") or {}
        usage = None
        if raw_usage:
            usage = Usage(
                prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                completion_tokens=int(raw_usage.get("completion_tokens") or 0),
            )
        return ModelResponse(
            content=choice.get("content") or "",
            tool_calls=tuple(calls),
            usage=usage,
            raw=data,
        )


ScriptItem = ModelResponse | str | Mapping[str, Any] | Callable[[Sequence[ChatMessage], Sequence[ToolSpec]], ModelResponse]


class FakeProvider:
    """Scripted provider for tests: replays responses in order, never uses the network.

    Each script item may be a :class:`ModelResponse`, a plain final string, a mapping
    with ``content`` and/or ``tool_calls`` (``[{"name": ..., "arguments": {...}}]``),
    or a callable receiving ``(messages, tools)``.  When the script is exhausted the
    provider returns a final message so a loop can never hang.
    """

    name = "fake"

    def __init__(self, script: Sequence[ScriptItem] = ()) -> None:
        self.script: list[ScriptItem] = list(script)
        self.calls: list[tuple[tuple[ChatMessage, ...], tuple[ToolSpec, ...]]] = []

    def _coerce(self, item: ScriptItem, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec]) -> ModelResponse:
        if isinstance(item, ModelResponse):
            return item
        if isinstance(item, str):
            return ModelResponse(content=item)
        if callable(item):
            result = item(messages, tools)
            if not isinstance(result, ModelResponse):
                raise ProviderError("FakeProvider callable must return ModelResponse")
            return result
        content = str(item.get("content") or "")
        calls: list[ToolCall] = []
        for index, raw in enumerate(item.get("tool_calls") or []):
            calls.append(ToolCall(
                id=raw.get("id") or "call_%d" % (index + 1),
                name=raw.get("name") or "",
                arguments=dict(raw.get("arguments") or {}),
            ))
        usage_raw = item.get("usage") or {}
        usage = None
        if usage_raw:
            usage = Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            )
        return ModelResponse(content=content, tool_calls=tuple(calls), usage=usage)

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float | None = None,
    ) -> ModelResponse:
        """Record the call and return the next scripted response."""
        self.calls.append((tuple(messages), tuple(tools)))
        if not self.script:
            return ModelResponse(content="[fake] script exhausted")
        item = self.script.pop(0)
        return self._coerce(item, messages, tools)


def provider_from_env(env: Mapping[str, str] | None = None) -> ModelProvider | None:
    """Return the configured real provider, or None when no API key is available."""
    return OpenAICompatibleProvider.from_env(env)
