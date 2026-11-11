"""P1 验收：同一段“调用工具 → 根据结果回答”的脚本在三种协议的回放中通过。

完整链路：GatewayConfig → Gateway（真实协议适配器，httpx MockTransport 回放各家真实格式的 SSE）
→ Agent 循环（工具执行）→ SessionRecorder（SQLite 事件存储）→ 投影还原对话。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from kaopu_agent import Agent, ToolContext, tool
from kaopu_agent.session import SessionRecorder, SQLiteEventStore, load_messages
from kaopu_llm import AssistantMessage, Gateway, GatewayConfig, ToolResultMessage
from kaopu_llm.testing import sse_response

FIXTURES = Path(__file__).resolve().parents[1] / "packages" / "llm" / "tests" / "fixtures"


class CityArgs(BaseModel):
    city: str


@tool(description="查询城市今天的天气")
async def get_weather(params: CityArgs, ctx: ToolContext) -> str:
    return f"{params.city}：晴，25 度"


CASES = {
    "openai": ("deepseek", "deepseek/deepseek-chat"),
    "anthropic": ("anthropic", "anthropic/claude-sonnet-4-5"),
    "gemini": ("gemini", "gemini/gemini-2.5-flash"),
}


def _tool_result_in_body(protocol: str, body: dict[str, Any]) -> Any:
    """取出第二次请求里回传的工具结果，验证各协议的格式。"""
    if protocol == "openai":
        msgs = body["messages"]
        assert msgs[-2]["tool_calls"][0]["id"] == "call_0_weather"
        assert json.loads(msgs[-2]["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}
        return msgs[-1]
    if protocol == "anthropic":
        msgs = body["messages"]
        assistant = msgs[-2]["content"]
        # thinking 块连同签名原样回传
        assert assistant[0] == {"type": "thinking", "thinking": "需要先查天气。", "signature": "EqQBCgIYAhIM"}
        assert assistant[-1]["type"] == "tool_use"
        return msgs[-1]["content"][0]
    contents = body["contents"]
    call_part = contents[-2]["parts"][-1]
    assert call_part["thoughtSignature"] == "CiQBVKhc7g=="
    return contents[-1]["parts"][0]


@pytest.mark.parametrize("protocol", list(CASES))
async def test_tool_call_script_on_three_protocols(protocol: str, tmp_path: Path) -> None:
    provider, ref = CASES[protocol]
    responses = [
        sse_response((FIXTURES / f"{protocol}_tool_call.sse").read_text(encoding="utf-8"), chunk_size=7),
        sse_response((FIXTURES / f"{protocol}_text.sse").read_text(encoding="utf-8"), chunk_size=7),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    cfg = GatewayConfig.model_validate(
        {"providers": {provider: {"api_key": "test-key"}}, "roles": {"tutor": [ref]}}
    )
    gateway = Gateway.from_config(cfg, transport=httpx.MockTransport(handler))
    store = SQLiteEventStore(tmp_path / "kaopu.db")
    session = await store.create_session(metadata={"protocol": protocol})
    recorder = SessionRecorder(store, session.id)
    agent = Agent(gateway, role="tutor", system="你是天气助手", tools=[get_weather])

    async for ev in agent.run("北京天气？"):
        await recorder.record(ev)

    # 对话结构：用户 → 助手（工具调用）→ 工具结果 → 助手（回答）
    assert [m.role for m in agent.messages] == ["user", "assistant", "tool_result", "assistant"]
    call_msg = agent.messages[1]
    assert isinstance(call_msg, AssistantMessage)
    assert call_msg.tool_calls[0].name == "get_weather"
    assert call_msg.tool_calls[0].arguments == {"city": "北京"}
    result = agent.messages[2]
    assert isinstance(result, ToolResultMessage)
    assert result.text == "北京：晴，25 度"
    final = agent.messages[3]
    assert isinstance(final, AssistantMessage)
    assert final.text == "北京今天晴，25 度。"
    assert final.stop_reason == "stop"
    assert final.protocol == protocol

    # 第二次请求按各协议格式回传了工具调用与结果
    assert len(requests) == 2
    second = json.loads(requests[1].content)
    returned = _tool_result_in_body(protocol, second)
    assert "北京：晴，25 度" in json.dumps(returned, ensure_ascii=False)

    # 用量记账覆盖两次调用；事件存储可还原完整对话
    assert gateway.ledger.calls == 2
    assert gateway.ledger.total().output_tokens > 0
    assert await load_messages(store, session.id) == agent.messages
    await store.close()
    await gateway.aclose()
