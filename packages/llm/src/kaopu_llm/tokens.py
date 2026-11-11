"""token 估算与截断。

P1 不依赖 tiktoken（离线环境下载编码表会失败），用启发式估算：中日韩字符每个按 1 token，其他字符每 4 个按 1 token。
对中文偏保守（实际常见分词器约 0.6–1 token/字），用于预算控制足够；精确计数与用量校正在 P4 做。
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from .types import (
    AssistantMessage,
    ImageBlock,
    Message,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultMessage,
    UserMessage,
)

_CJK_RANGES = (
    (0x3000, 0x303F),  # 中文标点
    (0x3040, 0x30FF),  # 日文假名
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),  # 韩文
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),  # 全角
    (0x20000, 0x2FA1F),
)

MESSAGE_OVERHEAD = 4
IMAGE_TOKENS = 1000
TRUNCATION_MARK = "…（已截断）"


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + math.ceil(other / 4)


def estimate_message_tokens(message: Message) -> int:
    n = MESSAGE_OVERHEAD
    if isinstance(message, SystemMessage):
        return n + estimate_tokens(message.content)
    if isinstance(message, UserMessage | ToolResultMessage):
        for b in message.content:
            n += IMAGE_TOKENS if isinstance(b, ImageBlock) else estimate_tokens(b.text)
        return n
    assert isinstance(message, AssistantMessage)
    for b in message.content:
        if isinstance(b, TextBlock | ThinkingBlock):
            n += estimate_tokens(b.text)
        else:
            n += estimate_tokens(b.name) + estimate_tokens(str(b.arguments))
    return n


def estimate_messages_tokens(messages: Iterable[Message]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def truncate_to_tokens(text: str, max_tokens: int, *, mark: str = TRUNCATION_MARK) -> str:
    """截掉尾部使估算 token 数不超过 ``max_tokens``（含截断标记）。不需要截断时原样返回。"""
    if estimate_tokens(text) <= max_tokens:
        return text
    budget = max_tokens - estimate_tokens(mark)
    if budget <= 0:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:  # 二分找最长前缀
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + mark
