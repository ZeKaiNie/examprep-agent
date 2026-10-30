# -*- coding: utf-8 -*-
"""Offline provider tests: config, payload encoding and HTTP response parsing.

``urllib.request.urlopen`` is monkeypatched everywhere, so the suite never
touches the network and needs no API key.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from pydantic import ValidationError

from examprep_agent.core.provider import (
    ChatMessage,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ToolCall,
    ToolSpec,
    _message_payload,
    provider_from_env,
)


class FakeHTTPResponse:
    """Minimal context-manager stand-in for urlopen's response."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def install_urlopen(monkeypatch: pytest.MonkeyPatch, body: dict | str | bytes, captured: dict) -> None:
    if isinstance(body, dict):
        body = json.dumps(body).encode("utf-8")
    elif isinstance(body, str):
        body = body.encode("utf-8")

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ANN202
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeHTTPResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def make_provider(**overrides) -> OpenAICompatibleProvider:
    config = ProviderConfig(api_key="test-key", **overrides)
    return OpenAICompatibleProvider(config)


class TestProviderConfig:
    def test_defaults(self) -> None:
        config = ProviderConfig()
        assert config.model == "deepseek-flash"
        assert config.base_url == "https://api.deepseek.com"
        assert config.api_key is None
        assert config.timeout > 0

    def test_validation(self) -> None:
        with pytest.raises(ValidationError):
            ProviderConfig(timeout=0)
        with pytest.raises(ValidationError):
            ProviderConfig(temperature=3)

    def test_missing_key_is_a_provider_error(self) -> None:
        with pytest.raises(ProviderError):
            OpenAICompatibleProvider(ProviderConfig())


class TestFromEnv:
    def test_no_key_returns_none(self) -> None:
        assert OpenAICompatibleProvider.from_env({}) is None
        assert provider_from_env({}) is None

    def test_defaults_and_overrides(self) -> None:
        provider = OpenAICompatibleProvider.from_env({"DEEPSEEK_API_KEY": "sk-1"})
        assert provider is not None
        assert provider.name == "openai-compatible:deepseek-flash"
        assert provider.config.base_url == "https://api.deepseek.com"
        assert provider.config.api_key == "sk-1"

        custom = OpenAICompatibleProvider.from_env({
            "OPENAI_API_KEY": "sk-2",
            "EXAMPREP_MODEL": "local-model",
            "EXAMPREP_BASE_URL": "https://example.test/v1",
        })
        assert custom is not None
        assert custom.config.model == "local-model"
        assert custom.config.base_url == "https://example.test/v1"
        assert custom.name == "openai-compatible:local-model"


class TestMessagePayload:
    def test_plain_and_tool_messages(self) -> None:
        assert _message_payload(ChatMessage(role="user", content="hi")) == {
            "role": "user", "content": "hi",
        }
        tool = _message_payload(ChatMessage(role="tool", content="{}", tool_call_id="c1", name="search"))
        assert tool["tool_call_id"] == "c1" and tool["name"] == "search"

    def test_assistant_tool_calls_are_json_encoded(self) -> None:
        message = ChatMessage(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="c1", name="search", arguments={"query": "栈"}),),
        )
        payload = _message_payload(message)
        call = payload["tool_calls"][0]
        assert call["id"] == "c1" and call["type"] == "function"
        assert call["function"]["name"] == "search"
        assert json.loads(call["function"]["arguments"]) == {"query": "栈"}


class TestComplete:
    def test_request_and_response_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        install_urlopen(monkeypatch, {
            "choices": [{
                "message": {
                    "content": "A stack is LIFO.",
                    "tool_calls": [{
                        "id": "call_9",
                        "function": {"name": "search_materials", "arguments": "{\"query\": \"stack\"}"},
                    }],
                },
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        }, captured)
        provider = make_provider()
        tools = [ToolSpec(name="search_materials", description="Search",
                          parameters={"type": "object", "properties": {}})]
        response = provider.complete(
            [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="teach")],
            tools,
            temperature=0.2,
        )

        assert captured["url"] == "https://api.deepseek.com/chat/completions"
        assert captured["authorization"] == "Bearer test-key"
        assert captured["timeout"] == provider.config.timeout
        payload = captured["payload"]
        assert payload["model"] == "deepseek-flash"
        assert payload["temperature"] == 0.2
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert payload["tools"][0]["function"]["name"] == "search_materials"

        assert response.content == "A stack is LIFO."
        assert response.tool_calls == (ToolCall(id="call_9", name="search_materials",
                                                arguments={"query": "stack"}),)
        assert response.usage is not None and response.usage.total_tokens == 15

    def test_missing_usage_is_reported_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_urlopen(monkeypatch, {"choices": [{"message": {"content": "ok"}}]}, {})
        response = make_provider().complete([ChatMessage(role="user", content="x")])
        assert response.usage is None  # the loop must estimate instead of trusting 0

    def test_malformed_arguments_fall_back_to_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_urlopen(monkeypatch, {
            "choices": [{"message": {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "t", "arguments": "{not json"}},
            ]}}],
        }, {})
        response = make_provider().complete([ChatMessage(role="user", content="x")])
        assert response.tool_calls[0].arguments == {"_raw": "{not json"}

    def test_malformed_response_is_a_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_urlopen(monkeypatch, "not json", {})
        with pytest.raises(ProviderError, match="malformed response"):
            make_provider().complete([ChatMessage(role="user", content="x")])

    def test_http_error_is_a_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_http(request, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.HTTPError("https://example.test", 500, "boom", None,
                                         io.BytesIO(b'{"error": "exploded"}'))

        monkeypatch.setattr(urllib.request, "urlopen", raise_http)
        with pytest.raises(ProviderError, match="HTTP 500"):
            make_provider().complete([ChatMessage(role="user", content="x")])

    def test_transport_error_is_a_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raise_transport(request, timeout=None):  # noqa: ANN001, ARG001
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", raise_transport)
        with pytest.raises(ProviderError, match="transport error"):
            make_provider().complete([ChatMessage(role="user", content="x")])
