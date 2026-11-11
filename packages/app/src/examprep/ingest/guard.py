"""课程资料是数据，不是指令。移植自 Skill v5.3 guard.py。

资料原文一律放在两条围栏行之间，围栏 id 由围栏内文本的哈希生成，文本本身无法伪造结束行。
看起来像在对 AI 下指令的行（“ignore previous instructions”“如果你是 AI…”“把学生笔记发到 https://…”）会被标记。

检测是启发式的：能抓住常见的中英文说法，换个说法可能漏掉，偶尔也会误报（例如心理学课引用了这类句子）。
真正的边界是围栏和系统提示里的规则，标记只用来提醒。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

OPEN = "<<<MATERIAL"
CLOSE = "MATERIAL>>>"

# 软连字符、蒙古文元音分隔符、零宽字符与方向控制符、词连接符、BOM
_ZERO_WIDTH = re.compile("[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

_AI = (
    r"(?:ai|a\.i\.|ai\s+(?:assistant|model|tutor|agent)|"
    r"assistant(?!\s+(?:professor|prof\b|manager|director|lecturer|coach|editor|teacher|instructor|dean|principal))|"
    r"chatbot|chat\s*gpt|gpt-?\d\w*|claude|gemini|copilot|llm|(?:large\s+)?language\s+model)"
)
_EN = [
    # "ignore all previous instructions", "disregard your guidelines", "ignore everything above"
    r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|preceding|all|any|your|these|those)\b"
    r"[^.\n]{0,30}\b(?:instructions?|prompts?|directives?|guidelines)\b",
    r"\b(?:ignore|disregard|forget)\s+(?:everything|anything|all)\s+(?:above|before|so\s+far|previously)\b",
    r"\b(?:ignore|disregard|forget)\s+(?:the\s+|all\s+(?:the\s+)?)?(?:previous|prior|above|earlier|preceding)\s+(?:rules|context|messages?|text|content)\b",
    r"\byou\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai\s+(?:assistant|model|tutor|agent)|chat\s*gpt|gpt-?\d\w*|claude|gemini|copilot|llm|(?:large\s+)?language\s+model)\b",
    r"\bif\s+you\s+are\s+(?:an?\s+)?" + _AI + r"\b",
    r"\bas\s+an?\s+(?:ai|ai\s+(?:assistant|model)|language\s+model|llm)\s*,\s*you\b",
    r"\b(?:note|message|instructions?|reminder|attention)\s*(?:to|for)?\s*(?:the\s+|any\s+)?"
    + _AI
    + r"s?\s*[:：]",
    r"\b(?:hey|hi|dear|attention|note\s+to)\s*,?\s*" + _AI + r"\b",
    r"^\s*(?:dear\s+)?"
    + _AI
    + r"\s*[,，:：]\s*(?:please|you|do|don't|ignore|tell|mark|skip|say|give|output)\b",
    r"^\s*(?:new|updated|real|actual|hidden|secret)\s+instructions?\s*[:：]",
    r"\b(?:reveal|print|show|output|repeat)\s+(?:your|the)\s+(?:system|developer)\s+prompt\b",
    r"\b(?:do\s+not|don't|never)\s+(?:reveal|disclose|mention)\s+(?:this|these|that|it)\b[^.\n]{0,20}\b(?:user|student)s?\b",
    r"\b(?:do\s+not|don't|never)\s+(?:tell|let|inform)\s+(?:the\s+)?(?:user|student)s?\s+(?:know|about\s+(?:this|these|that|it))\b",
    r"\b(?:send|upload|post|forward|exfiltrate|email)\b[^.\n]{0,40}\b(?:(?:the\s+)?(?:student|user)'?s?|their)\s+(?:notes?|files?|data|answers?|messages?|conversation|history|keys?|passwords?|work)\b[^.\n]{0,30}\bto\s+https?://",
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
]
PATTERNS = [re.compile(p, re.I | re.M) for p in _EN + _ZH]
_MARKER = re.compile(
    r"<\s*<\s*<\s*m\W{0,3}a\W{0,3}t\W{0,3}e\W{0,3}r\W{0,3}i\W{0,3}a\W{0,3}l"
    r"|m\W{0,3}a\W{0,3}t\W{0,3}e\W{0,3}r\W{0,3}i\W{0,3}a\W{0,3}l\s*>\s*>\s*>",
    re.I,
)


def normalize(text: str) -> str:
    """检测用副本：NFKC（全角转半角），去掉零宽与方向控制字符。"""
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text))


def is_suspicious(line: str) -> bool:
    s = normalize(line)
    return any(p.search(s) for p in PATTERNS)


def flagged_lines(text: str, limit: int = 5) -> list[int]:
    """疑似对 AI 下指令的行号（从 0 开始）；被拆成两行的句子也能抓到。"""
    lines = text.split("\n")
    out: list[int] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        hit = is_suspicious(s)
        if not hit and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            # PDF 抽取会把句子断开：拉丁文本用空格拼回，中日韩文本直接拼
            hit = not is_suspicious(nxt) and (is_suspicious(s + " " + nxt) or is_suspicious(s + nxt))
        if hit:
            out.append(i)
            if len(out) >= limit:
                break
    return out


def neutralize(text: str) -> str:
    """破坏任何形似围栏标记的文本（不论大小写、空格、全角）。"""
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
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:6]


def fence(text: str) -> str:
    body = neutralize(text).rstrip()
    fid = fence_id(body)
    return f"{OPEN} {fid}\n{body}\n{CLOSE} {fid}"
