"""规则意图分类（R3.1）。分类结果决定状态迁移，Tutor 只负责生成内容。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..ingest.text import chinese_numeral

IntentKind = Literal[
    "continue",  # 继续讲
    "quiz",  # 考我
    "answer",  # 作答（等待作答时）
    "check_answer",  # 回答检查问题
    "skip",  # 跳过当前题
    "hint",
    "reveal",  # 看答案
    "goto",  # 跳到某章
    "progress",
    "confused",  # 没懂
    "done",  # 本章结束
    "ask",  # 自由提问
    "compact",  # 压缩上下文
]
Awaiting = Literal["answer", "check"] | None


@dataclass(frozen=True, slots=True)
class Intent:
    kind: IntentKind
    text: str
    chapter: int | None = None


_P = r"[\s。.!！?？~～]*"


def _rx(words: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*(?:{words}){_P}$", re.I)


_GOTO = [
    re.compile(
        r"^\s*(?:跳到|去|转到|回到|开始|学|讲)?\s*第\s*([0-9一二三四五六七八九十百]+)\s*章\s*(?:吧)?[\s。.!！]*$"
    ),
    re.compile(r"^\s*(?:go\s*to|goto|jump\s+to|chapter|ch)\s*(?:chapter\s*)?(\d{1,3})[\s.!]*$", re.I),
]
_PROGRESS = _rx(r"进度|看进度|看看进度|学到哪了|学到哪里了|status|progress")
_HINT = _rx(r"(?:再)?(?:给(?:我)?(?:个|一个|点|一点)?)?提示|hint|give me a hint|another hint")
_REVEAL = _rx(
    r"看答案|答案|公布答案|告诉我答案|我不会|不会|放弃|show (?:me )?the answer|answer|give up|reveal"
)
_SKIP = _rx(r"跳过|下一题|skip|next question")
_CONFUSED = _rx(
    r"没懂|没听懂|不懂|看不懂|听不懂|还是不懂|这里没懂|没太懂|再讲一遍|换个讲法|换种讲法|太难了|confused|"
    r"i don'?t (?:get|understand) (?:it|this)|explain again"
)
_DONE = _rx(r"这章好了|本章结束|这章结束|下一章|学完了|next chapter|done with (?:this|the) chapter|done")
_QUIZ = _rx(r"考考我|考我|出题|出道题|做题|练习|练一练|quiz(?: me)?|test me")
# 明确的说法任何状态下都识别；单说“压缩”可能是在答题（例如“哈夫曼编码用来做什么”），只在不等作答时识别
_COMPACT = _rx(r"压缩上下文|压缩对话|压缩一下上下文|/compact|compact(?: the)? (?:context|conversation)")
_COMPACT_SHORT = _rx(r"压缩|压缩一下|compact")
_CONTINUE = _rx(
    r"继续|好|好的|行|可以|ok|okay|嗯|嗯嗯|下一段|接着讲|继续讲|往下讲|然后呢|next|go on|continue|go"
)
# 明确指令，按优先级排列；任何状态下都先识别
_COMMANDS: tuple[tuple[IntentKind, re.Pattern[str]], ...] = (
    ("progress", _PROGRESS),
    ("compact", _COMPACT),
    ("hint", _HINT),
    ("confused", _CONFUSED),
    ("done", _DONE),
    ("quiz", _QUIZ),
)
_QUESTION_MARK = re.compile(r"[?？]\s*$")
_QUESTION_WORDS = re.compile(
    r"什么|为什么|为何|怎么|如何|哪|吗|呢|多少|是否|区别|能不能|可不可以|^(?:what|why|how|when|where|which|who|is|are|can|does|do)\b",
    re.I,
)


def classify(text: str, *, awaiting: Awaiting = None) -> Intent:
    s = text.strip()
    if not s:
        return Intent("continue", s)
    for rx in _GOTO:
        m = rx.match(s)
        if m:
            n = chinese_numeral(m.group(1))
            if n is not None:
                return Intent("goto", s, n)
    for kind, rx in _COMMANDS:
        if rx.match(s):
            return Intent(kind, s)
    if awaiting == "answer":
        if _REVEAL.match(s):
            return Intent("reveal", s)
        if _SKIP.match(s):
            return Intent("skip", s)
        return Intent("answer", s)
    if awaiting == "check":
        if _CONTINUE.match(s):
            return Intent("continue", s)  # 不答检查题直接继续
        return Intent("check_answer", s)
    if _COMPACT_SHORT.match(s):
        return Intent("compact", s)
    if _CONTINUE.match(s):
        return Intent("continue", s)
    if _QUESTION_MARK.search(s) or _QUESTION_WORDS.search(s):
        return Intent("ask", s)
    return Intent("continue" if len(s) <= 4 else "ask", s)
