from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from kaopu_agent import NoParams, Tool, ToolContext, ToolOutput, model_schema
from kaopu_agent.budget import Budget, CallTracker, call_signature
from kaopu_llm import CancelToken, ImageBlock, TextBlock, ToolCallBlock


class Page(BaseModel):
    file: str
    page: int = Field(ge=1, description="页码，从 1 开始")


class Node(BaseModel):
    name: str
    children: list[Node] = []


class SearchArgs(BaseModel):
    """检索参数"""

    query: str
    pages: list[Page] = []
    mode: Literal["bm25", "hybrid"] = "bm25"
    tree: Node | None = None


def test_model_schema_inlines_refs() -> None:
    schema = model_schema(SearchArgs)
    assert "$defs" not in schema
    assert "title" not in schema
    assert schema["type"] == "object"
    assert schema["required"] == ["query"]
    page = schema["properties"]["pages"]["items"]
    assert page["properties"]["page"]["minimum"] == 1
    assert page["properties"]["page"]["description"] == "页码，从 1 开始"
    assert schema["properties"]["mode"]["enum"] == ["bm25", "hybrid"]
    # 递归模型展开一层后停止
    tree = schema["properties"]["tree"]["anyOf"][0]
    assert tree["properties"]["children"]["items"] == {"type": "object"}
    assert model_schema(NoParams) == {"additionalProperties": False, "properties": {}, "type": "object"}


class Echo(Tool[Page]):
    name = "echo"
    description = "回显"
    params = Page

    async def run(self, params: Page, ctx: ToolContext) -> ToolOutput:
        ctx.update("working")
        return ToolOutput(
            content=[
                TextBlock(text=f"{params.file}:{params.page}"),
                ImageBlock(media_type="image/png", data="x"),
            ]
        )


async def test_class_based_tool_and_context() -> None:
    t = Echo()
    spec = t.spec()
    assert spec.name == "echo"
    assert spec.parameters["required"] == ["file", "page"]
    updates: list[object] = []
    call = ToolCallBlock(id="c", name="echo", arguments={"file": "a.pdf", "page": 2})
    ctx = ToolContext(call, CancelToken(), updates.append)
    out = await t.run(t.validate(call.arguments), ctx)
    assert (ctx.call_id, ctx.tool_name) == ("c", "echo")
    assert updates == ["working"]
    assert [b.type for b in out.blocks()] == ["text", "image"]
    assert ToolOutput.error("x", details=1).is_error


def test_call_tracker() -> None:
    tracker = CallTracker(Budget(max_tool_calls=3, max_repeat_calls=2))
    a = ToolCallBlock(id="1", name="f", arguments={"x": 1, "y": 2})
    a2 = ToolCallBlock(id="2", name="f", arguments={"y": 2, "x": 1})  # 参数顺序不同视为相同
    b = ToolCallBlock(id="3", name="f", arguments={}, raw_arguments="{bad")
    assert call_signature(a) == call_signature(a2)
    assert tracker.check(a) == "ok"
    assert tracker.check(a2) == "loop_detected"
    assert tracker.check(b) == "ok"
    assert tracker.check(a) == "ok"
    assert tracker.check(ToolCallBlock(id="4", name="g", arguments={})) == "max_tool_calls"
