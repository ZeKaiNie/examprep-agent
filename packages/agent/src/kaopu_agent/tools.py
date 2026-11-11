"""工具定义（R6.2）。

参数用 Pydantic 模型声明，自动生成 JSON Schema（内联 ``$ref``，兼容只认扁平 schema 的本地模型服务）。
"""

from __future__ import annotations

import inspect
import typing
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from kaopu_llm import CancelToken, ImageBlock, TextBlock, ToolCallBlock, ToolSpec

P = TypeVar("P", bound=BaseModel)

ToolContent = str | list[TextBlock | ImageBlock]


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ToolContent
    is_error: bool = False
    # 本批工具全部 terminate 时，Agent 在本批之后结束（不再请求模型）
    terminate: bool = False
    # 只给 UI / trace，不发给模型
    details: Any = None

    def blocks(self) -> list[TextBlock | ImageBlock]:
        if isinstance(self.content, str):
            return [TextBlock(text=self.content)]
        return list(self.content)

    @classmethod
    def error(cls, message: str, *, details: Any = None) -> ToolOutput:
        return cls(content=message, is_error=True, details=details)


class ToolContext:
    """传给工具的运行上下文。"""

    def __init__(
        self,
        call: ToolCallBlock,
        cancel: CancelToken,
        emit: Callable[[Any], None],
        agent: Any = None,
    ) -> None:
        self.call = call
        self.cancel = cancel
        self.agent = agent
        self._emit = emit

    @property
    def call_id(self) -> str:
        return self.call.id

    @property
    def tool_name(self) -> str:
        return self.call.name

    def update(self, data: Any) -> None:
        """报告进度，产生 ``ToolUpdate`` 事件。"""
        self._emit(data)


class Tool(ABC, Generic[P]):
    name: str
    description: str
    params: type[P]
    sequential: bool = False  # True：不与其他工具并行
    timeout: float | None = None

    @abstractmethod
    async def run(self, params: P, ctx: ToolContext) -> ToolOutput | str: ...

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=model_schema(self.params))

    def validate(self, arguments: dict[str, Any]) -> P:
        return self.params.model_validate(arguments)


class FunctionTool(Tool[P]):
    def __init__(
        self,
        fn: Callable[[P, ToolContext], Awaitable[ToolOutput | str]],
        params: type[P],
        *,
        name: str,
        description: str,
        sequential: bool = False,
        timeout: float | None = None,
    ) -> None:
        self.fn = fn
        self.params = params
        self.name = name
        self.description = description
        self.sequential = sequential
        self.timeout = timeout

    async def run(self, params: P, ctx: ToolContext) -> ToolOutput | str:
        return await self.fn(params, ctx)


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    sequential: bool = False,
    timeout: float | None = None,
) -> Callable[[Callable[[P, ToolContext], Awaitable[ToolOutput | str]]], FunctionTool[P]]:
    """把 ``async def f(params: Model, ctx: ToolContext)`` 变成工具。描述默认取函数 docstring。"""

    def wrap(fn: Callable[[P, ToolContext], Awaitable[ToolOutput | str]]) -> FunctionTool[P]:
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)
        first = next(iter(sig.parameters), None)
        params_type: Any = hints.get(first) if first is not None else None
        if not (isinstance(params_type, type) and issubclass(params_type, BaseModel)):
            raise TypeError(f"{fn.__name__} 的第一个参数必须标注为 Pydantic 模型")
        desc = description or inspect.getdoc(fn) or ""
        return FunctionTool(
            fn,
            cast(type[P], params_type),
            name=name or fn.__name__,
            description=desc,
            sequential=sequential,
            timeout=timeout,
        )

    return wrap


class NoParams(BaseModel):
    """无参数工具使用的参数模型。"""

    model_config = ConfigDict(extra="forbid")


def model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """生成内联 ``$ref`` 的 JSON Schema，去掉顶层 title 和 description（模型 docstring 不发给模型，工具描述另有字段）。"""
    schema = model.model_json_schema()
    defs = cast(dict[str, Any], schema.pop("$defs", {}))
    out = cast(dict[str, Any], _inline(schema, defs, ()))
    out.pop("title", None)
    out.pop("description", None)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline(x, defs, stack) for x in cast(list[Any], node)]
    if not isinstance(node, dict):
        return node
    d = cast(dict[str, Any], node)
    ref = d.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        key = ref.removeprefix("#/$defs/")
        if key in stack or key not in defs:  # 递归模型：停止展开
            return {"type": "object"}
        target = cast(dict[str, Any], _inline(defs[key], defs, (*stack, key)))
        extra = {k: v for k, v in d.items() if k != "$ref"}
        return {**target, **{k: _inline(v, defs, stack) for k, v in extra.items()}}
    return {k: _inline(v, defs, stack) for k, v in d.items()}


def format_validation_error(err: ValidationError) -> str:
    lines: list[str] = []
    for e in err.errors(include_url=False):
        loc = ".".join(str(x) for x in e["loc"]) or "(参数)"
        lines.append(f"- {loc}: {e['msg']}")
    return "参数校验失败：\n" + "\n".join(lines)


__all__ = [
    "FunctionTool",
    "NoParams",
    "Tool",
    "ToolContext",
    "ToolOutput",
    "format_validation_error",
    "model_schema",
    "tool",
]
