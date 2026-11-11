"""把流式增量拼成完整的 ``AssistantMessage``，三种协议共用。

协议适配器只负责“线上格式 → 增量”，调用本类的方法，并把返回的事件 yield 出去。
"""

from __future__ import annotations

import json
from typing import Any, cast

from .events import (
    DoneEvent,
    StartEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
)
from .types import (
    AssistantContent,
    AssistantMessage,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    Usage,
)


class _ToolDraft:
    __slots__ = ("args", "id", "index", "name", "signature")

    def __init__(self, index: int, id: str, name: str) -> None:
        self.index = index
        self.id = id
        self.name = name
        self.args: list[str] = []
        self.signature: str | None = None


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """解析工具参数 JSON。返回 (参数, 失败时的原文)。空串视为空对象。"""
    if raw.strip() == "":
        return {}, None
    try:
        value: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}, raw
    if not isinstance(value, dict):
        return {}, raw
    return cast(dict[str, Any], value), None


class StreamAccumulator:
    def __init__(self, *, provider: str, model: str, protocol: str) -> None:
        self.provider = provider
        self.model = model
        self.protocol = protocol
        self._blocks: list[AssistantContent | _ToolDraft] = []
        self._tools: dict[object, _ToolDraft] = {}
        self._open_kind: str | None = None  # 当前可追加的文本/推理块类型
        self.usage = Usage()

    # ------------------------------------------------------------ 事件生成

    def start(self) -> StartEvent:
        return StartEvent(provider=self.provider, model=self.model)

    def text(self, delta: str) -> TextDelta | None:
        if delta == "":
            return None
        if self._open_kind != "text":
            self._blocks.append(TextBlock(text=""))
            self._open_kind = "text"
        idx = len(self._blocks) - 1
        block = cast(TextBlock, self._blocks[idx])
        block.text += delta
        return TextDelta(index=idx, text=delta)

    def thinking(self, delta: str, *, signature: str | None = None) -> ThinkingDelta | None:
        if delta == "" and signature is None:
            return None
        if self._open_kind != "thinking":
            self._blocks.append(ThinkingBlock(text=""))
            self._open_kind = "thinking"
        idx = len(self._blocks) - 1
        block = cast(ThinkingBlock, self._blocks[idx])
        block.text += delta
        if signature is not None:
            block.signature = (block.signature or "") + signature
        if delta == "":
            return None
        return ThinkingDelta(index=idx, text=delta)

    def redacted_thinking(self, data: str) -> None:
        self._blocks.append(ThinkingBlock(text=data, redacted=True))
        self._open_kind = None

    def set_signature(self, signature: str) -> None:
        """给最后一个文本/推理块追加签名（Anthropic signature_delta、Gemini 文本 thoughtSignature）。"""
        if not self._blocks:
            return
        last = self._blocks[-1]
        if isinstance(last, ThinkingBlock | TextBlock):
            last.signature = (last.signature or "") + signature

    def close_block(self) -> None:
        """结束当前文本/推理块，后续增量另起新块（Anthropic content_block_stop）。"""
        self._open_kind = None

    def tool_start(self, key: object, id: str, name: str, *, signature: str | None = None) -> ToolCallStart:
        draft = _ToolDraft(len(self._blocks), id, name)
        draft.signature = signature
        self._blocks.append(draft)
        self._tools[key] = draft
        self._open_kind = None
        return ToolCallStart(index=draft.index, id=id, name=name)

    def has_tool(self, key: object) -> bool:
        return key in self._tools

    def tool_args(self, key: object, delta: str) -> ToolCallDelta | None:
        draft = self._tools.get(key)
        if draft is None or delta == "":
            return None
        draft.args.append(delta)
        return ToolCallDelta(index=draft.index, arguments_delta=delta)

    def tool_end(self, key: object) -> ToolCallEnd | None:
        draft = self._tools.pop(key, None)
        if draft is None:
            return None
        block = self._finalize_tool(draft)
        self._blocks[draft.index] = block
        return ToolCallEnd(index=draft.index, call=block)

    def end_all_tools(self) -> list[ToolCallEnd]:
        out: list[ToolCallEnd] = []
        for key in list(self._tools):
            ev = self.tool_end(key)
            if ev is not None:
                out.append(ev)
        return out

    def finish(self, stop_reason: StopReason) -> DoneEvent:
        self.end_all_tools()
        msg = self.snapshot(stop_reason=stop_reason)
        if stop_reason == "stop" and msg.tool_calls:
            msg.stop_reason = "tool_use"
        return DoneEvent(message=msg)

    # ------------------------------------------------------------ 快照

    def snapshot(
        self, *, stop_reason: StopReason = "stop", include_partial_tools: bool = True
    ) -> AssistantMessage:
        content: list[AssistantContent] = []
        for b in self._blocks:
            if isinstance(b, _ToolDraft):
                if include_partial_tools:
                    content.append(self._finalize_tool(b))
            else:
                content.append(b.model_copy())
        return AssistantMessage(
            content=content,
            provider=self.provider,
            model=self.model,
            protocol=self.protocol,
            stop_reason=stop_reason,
            usage=self.usage.model_copy(),
        )

    @staticmethod
    def _finalize_tool(draft: _ToolDraft) -> ToolCallBlock:
        args, raw = parse_tool_arguments("".join(draft.args))
        return ToolCallBlock(
            id=draft.id, name=draft.name, arguments=args, raw_arguments=raw, signature=draft.signature
        )

    def set_tool_arguments(self, key: object, arguments: dict[str, Any]) -> ToolCallDelta | None:
        """整块到达的参数（Gemini）。"""
        return self.tool_args(key, json.dumps(arguments, ensure_ascii=False))
