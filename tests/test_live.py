"""在线测试：真实模型跑同一段工具调用脚本。默认跳过，用 ``uv run pytest -m live tests/test_live.py`` 运行。

需要的环境变量（缺失则跳过对应用例）：
- DEEPSEEK_API_KEY      → deepseek/deepseek-chat（OpenAI 兼容协议）
- ANTHROPIC_API_KEY     → anthropic/claude-haiku-4-5
- GEMINI_API_KEY        → gemini/gemini-2.5-flash
- KAOPU_OLLAMA_MODEL    → ollama/<模型名>（需要本地 Ollama 在 11434 端口运行，模型需支持工具调用，如 qwen3:8b）
"""

from __future__ import annotations

import os

import httpx
import pytest
from pydantic import BaseModel

from kaopu_agent import Agent, Budget, ToolContext, tool
from kaopu_llm import AssistantMessage, Gateway, GatewayConfig, ToolResultMessage

pytestmark = pytest.mark.live


class CityArgs(BaseModel):
    city: str


calls: list[str] = []


@tool(description="查询城市今天的天气。回答天气问题前必须调用。")
async def get_weather(params: CityArgs, ctx: ToolContext) -> str:
    calls.append(params.city)
    return f"{params.city}：晴，气温 25 度，东南风 2 级"


def _ollama_up() -> bool:
    try:
        return httpx.get("http://localhost:11434/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _cases() -> list[object]:
    out: list[object] = []
    for provider, key_env, model in [
        ("deepseek", "DEEPSEEK_API_KEY", "deepseek-chat"),
        ("anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5"),
        ("gemini", "GEMINI_API_KEY", "gemini-2.5-flash"),
    ]:
        marks = [] if os.environ.get(key_env) else [pytest.mark.skip(reason=f"未设置 {key_env}")]
        out.append(pytest.param(provider, f"{provider}/{model}", id=provider, marks=marks))
    ollama_model = os.environ.get("KAOPU_OLLAMA_MODEL")
    reason = None if ollama_model else "未设置 KAOPU_OLLAMA_MODEL"
    if ollama_model and not _ollama_up():
        reason = "本地 Ollama 未运行"
    marks = [pytest.mark.skip(reason=reason)] if reason else []
    out.append(pytest.param("ollama", f"ollama/{ollama_model or 'none'}", id="ollama", marks=marks))
    return out


@pytest.mark.parametrize(("provider", "ref"), _cases())
async def test_live_tool_call(provider: str, ref: str) -> None:
    calls.clear()
    cfg = GatewayConfig.model_validate({"providers": {provider: {}}, "roles": {"tutor": [ref]}})
    gateway = Gateway.from_config(cfg)
    agent = Agent(
        gateway,
        role="tutor",
        system="你是天气助手。用户问天气时先调用 get_weather，再用一句中文回答。",
        tools=[get_weather],
        budget=Budget(max_turns=4),
    )
    events = [ev async for ev in agent.run("北京今天天气怎么样？")]
    await gateway.aclose()
    end = events[-1]
    assert end.type == "agent_end"
    assert end.reason == "completed", getattr(end, "error", None)
    assert calls, "模型没有调用工具"
    assert any(isinstance(m, ToolResultMessage) for m in agent.messages)
    final = agent.messages[-1]
    assert isinstance(final, AssistantMessage)
    assert "25" in final.text
