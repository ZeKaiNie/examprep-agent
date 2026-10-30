# -*- coding: utf-8 -*-
"""Course files are data, never instructions.

Everything the tool prints from a student's files goes between two fence lines that carry
an id derived from the fenced text, so the text itself cannot forge the closing line.
Lines that look like instructions to an AI ("ignore previous instructions", "如果你是 AI…",
"send the student's notes to https://…") are flagged.

The detector is a heuristic: it catches common phrasings in English and Chinese, can miss
a reworded attack, and occasionally flags ordinary text (for example a psychology study
that quotes such an instruction). The fence and the rule in the system prompt are the real
boundary; the flag only draws attention.

Derived from universal-examprep-skill (MIT, (c) ZeKaiNie):
https://github.com/ZeKaiNie/universal-examprep-skill
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

OPEN = "<<<MATERIAL"
CLOSE = "MATERIAL>>>"

_ZERO_WIDTH = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")

_AI = (r"(?:ai|a\.i\.|ai\s+(?:assistant|model|tutor|agent)|"
       r"assistant(?!\s+(?:professor|prof\b|manager|director|lecturer|coach|editor|teacher|instructor|dean|principal))|"
       r"chatbot|chat\s*gpt|gpt-?\d\w*|claude|gemini|copilot|llm|(?:large\s+)?language\s+model)")
_EN = [
    # "ignore all previous instructions", "disregard your guidelines", "ignore everything above"
    r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|all|any|your|these|those)\b"
    r"[^.\n]{0,30}\b(?:instructions?|prompts?|directives?|guidelines)\b",
    r"\b(?:ignore|disregard|forget)\s+(?:everything|anything|all)\s+(?:above|before|so\s+far|previously)\b",
    r"\b(?:ignore|disregard|forget)\s+(?:the\s+|all\s+(?:the\s+)?)?(?:previous|prior|above|earlier|preceding)\s+(?:rules|context|messages?|text|content)\b",
    r"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai\s+(?:assistant|model|tutor|agent)|chat\s*gpt|gpt-?\d\w*|claude|gemini|copilot|llm|(?:large\s+)?language\s+model)\b",
    r"\bif\s+you\s+are\s+(?:an?\s+)?" + _AI + r"\b",
    r"\bas\s+an?\s+(?:ai|ai\s+(?:assistant|model)|language\s+model|llm)\s*,\s*you\b",
    r"\b(?:note|message|instructions?|reminder|attention)\s*(?:to|for)?\s*(?:the\s+|any\s+)?" + _AI + r"s?\s*[:：]",
    r"\b(?:hey|hi|dear|attention|note\s+to)\s*,?\s*" + _AI + r"\b",
    r"^\s*(?:dear\s+)?" + _AI + r"\s*[,，:：]\s*(?:please|you|do|don't|ignore|tell|mark|skip|say|give|output)\b",
    r"^\s*(?:new|updated|real|actual|hidden|secret)\s+instructions?\s*[:：]",
    r"\b(?:reveal|print|show|output|repeat)\s+(?:your|the)\s+(?:system|developer)\s+prompt\b",
    r"\b(?:do\s+not|don't|never)\s+(?:reveal|disclose|mention)\s+(?:this|these|that|it)\b[^.\n]{0,20}\b(?:user|student)s?\b",
    r"\b(?:do\s+not|don't|never)\s+(?:tell|let|inform)\s+(?:the\s+)?(?:user|student)s?\s+(?:know|about\s+(?:this|these|that|it))\b",
    r"\b(?:send|upload|post|forward|exfiltrate|email)\b[^.\n]{0,40}\b(?:(?:the\s+)?(?:student|user)'?s?|their)\s+(?:notes?|files?|data|answers?|messages?|conversation|history|keys?|passwords?|work)\b[^.\n]{0,30}\bto\s+https?://",
    # Credential exfiltration (P0.5 adversarial set: "induce key leak").
    r"\b(?:api[_-]?key|secret|token|password|passwd|credentials?)\b[^.\n]{0,50}"
    r"\b(?:send|upload|post|forward|email|leak|exfiltrate)\b[^.\n]{0,50}https?://",
    r"\b(?:send|upload|post|forward|email|leak|exfiltrate)\b[^.\n]{0,50}"
    r"\b(?:api[_-]?key|secret|token|password|passwd|credentials?)\b",
    r"\b(?:print|show|reveal|output|repeat|cat|type)\b[^.\n]{0,30}"
    r"\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\b",
    r"<\|im_start\|>\s*system",
]
_ZH = [
    r"(?:忽略|无视|忘记|忘掉|不要理会)(?:你)?(?:之前|以上|前面|上面|上述|先前|原有|原来)的?(?:所有|全部)?的?(?:指令|指示|提示词?|规则|设定|要求|内容)",
    r"(?:忽略|无视|忘记|忘掉)(?:你的)?(?:所有|全部)(?:的)?(?:指令|指示|提示词?|规则|设定)",
    r"你(?:现在)?(?:是|扮演|作为)(?:一个|一名)?[^，。,.\n]{0,10}?(?:AI|人工智能|ChatGPT|GPT|Claude|Gemini|智能助手|大模型|语言模型)",
    r"(?:如果|假如|若)你是(?:一个|一名)?\s*(?:AI|人工智能|大模型|语言模型|助手|智能体)",
    r"作为(?:一个|一名)?\s*(?:AI|人工智能|大模型|语言模型|AI\s*助手)\s*[，,]?\s*你",
    r"(?:给|对|致)\s*(?:AI|人工智能|大模型|助手|模型|智能体)的?\s*(?:指令|说明|提示|话)",
    r"^\s*(?:致|给)\s*(?:AI|人工智能|大模型|智能体|AI\s*助手)\s*[:：]",
    r"(?:AI|人工智能|大模型|语言模型|智能体|AI\s*助手)\s*(?:请注意|请你|请直接|请跳过|请忽略|请务必|注意)",
    r"(?:输出|泄露|透露|显示|打印|复述)(?:你的)?系统提示词",
    r"不要(?:向|跟|对)(?:学生|用户)(?:透露|说|提起|提到|讲)(?:这|此|本)",
    r"不要(?:告诉|让|提醒)(?:学生|用户)(?:这|此|本|知道)",
    r"(?:学生|用户)的?[^。\n]{0,15}(?:发送|上传|转发)(?:到|给)\s*https?://",
    r"(?:发送|上传|转发)(?:学生|用户)的?[^。\n]{0,15}(?:到|给)\s*https?://",
    # Credential exfiltration (P0.5 adversarial set: "induce key leak").
    r"(?:密钥|令牌|口令|密码|凭证|api\s*key|access\s*token)[^。\n]{0,40}"
    r"(?:发送|上传|转发|发给|泄露|告诉|提交)[^。\n]{0,40}https?://",
    r"(?:发送|上传|转发|发给|泄露|提交)[^。\n]{0,40}"
    r"(?:密钥|令牌|口令|密码|凭证|api\s*key|access\s*token)",
    r"(?:输出|泄露|透露|显示|打印|复述)[^。\n]{0,30}"
    r"(?:密钥|令牌|口令|密码|凭证|api\s*key)",
]
_PATTERNS = [re.compile(p, re.I | re.M) for p in _EN + _ZH]
_MARKER = re.compile(r"<\s*<\s*<\s*m\W{0,3}a\W{0,3}t\W{0,3}e\W{0,3}r\W{0,3}i\W{0,3}a\W{0,3}l"
                     r"|m\W{0,3}a\W{0,3}t\W{0,3}e\W{0,3}r\W{0,3}i\W{0,3}a\W{0,3}l\s*>\s*>\s*>", re.I)


def _norm(text: str) -> str:
    """Detection copy: NFKC (fullwidth → ASCII), no zero-width or bidi controls."""
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def is_suspicious(line: str) -> bool:
    """True when a line looks like an instruction addressed to an AI assistant."""
    s = _norm(line)
    return any(p.search(s) for p in _PATTERNS)


def flagged_lines(text: str, limit: int = 5) -> list[int]:
    """Indexes of lines that address an AI; a sentence split over two lines is caught too."""
    lines = text.split("\n")
    out: list[int] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        hit = is_suspicious(s)
        if not hit and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            # PDF extraction breaks sentences: Latin text rejoins with a space, CJK text without
            hit = not is_suspicious(nxt) and (is_suspicious(s + " " + nxt) or is_suspicious(s + nxt))
        if hit:
            out.append(i)
            if len(out) >= limit:
                break
    return out


def neutralize(text: str) -> str:
    """Break anything that looks like a fence marker, whatever its case, spacing or width."""
    chars: list[str] = []
    index: list[int] = []
    for pos, ch in enumerate(text):
        if _ZERO_WIDTH.match(ch):
            continue
        for c in unicodedata.normalize("NFKC", ch):
            chars.append(c)
            index.append(pos)
    norm = "".join(chars)
    spans = [(index[m.start()], index[m.end() - 1] + 1) for m in _MARKER.finditer(norm)]
    if not spans:
        return _ZERO_WIDTH.sub("", text) if _ZERO_WIDTH.search(text) else text
    out: list[str] = []
    last = 0
    for a, b in spans:
        out.append(text[last:a])
        out.append("[fence-like text removed]")
        last = b
    out.append(text[last:])
    return _ZERO_WIDTH.sub("", "".join(out))


def fence_id(text: str) -> str:
    """Short content-derived id that makes forging the closing fence line impractical."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:6]


def fence(text: str) -> str:
    """Wrap untrusted course text in a material fence with a content-derived id."""
    body = neutralize(text).rstrip()
    fid = fence_id(body)
    return "%s %s\n%s\n%s %s" % (OPEN, fid, body, CLOSE, fid)
