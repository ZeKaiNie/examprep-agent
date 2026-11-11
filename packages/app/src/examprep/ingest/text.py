"""文本工具：分词（英文单词 + 中文二元组）、清洗、分段打包、中文数字。移植自 Skill v5.3 text.py。"""

from __future__ import annotations

import re
import unicodedata

# 日文假名、中日韩统一表意文字扩展 A、基本区
CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[a-z0-9][a-z0-9'+#\-]*|[{CJK}]+")
_CJK_RE = re.compile(f"[{CJK}]")
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "shall",
        "should",
        "can",
        "could",
        "may",
        "might",
        "must",
        "of",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "about",
        "between",
        "into",
        "through",
        "to",
        "from",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "and",
        "or",
        "but",
        "not",
        "no",
        "so",
        "than",
        "too",
        "very",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "you",
        "they",
        "he",
        "she",
        "i",
        "me",
        "my",
        "our",
        "your",
        "their",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "if",
        "then",
        "else",
    ]
)

# PDF 抽取常见的连字与排版符号
_REPLACE = {
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb00": "ff",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\u2019": "'",
    "\u2018": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": " ",
    "\u2212": "-",
    "\u2013": "-",
    "\u2014": "-",
}


def clean(text: str) -> str:
    """规范空白与排版符号，保留段落分隔。"""
    if not text:
        return ""
    for k, v in _REPLACE.items():
        text = text.replace(k, v)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_mostly_cjk(text: str, sample: int = 4000) -> bool:
    """中日韩字符在字母类内容中占多数时为 True（一个汉字约等于两个拉丁字母的信息量）。"""
    if not text:
        return False
    sample_text = text[:sample]
    cjk = len(_CJK_RE.findall(sample_text))
    latin = sum(1 for ch in sample_text if ch.isascii() and ch.isalpha())
    return cjk * 2 > latin


def tokenize(text: str) -> list[str]:
    """英文单词转小写（去停用词与 's），中日韩字符切成二元组（单字保留为一元）。"""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if _CJK_RE.match(tok):
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i : i + 2] for i in range(len(tok) - 1))
        else:
            if tok in _STOP:
                continue
            if tok.endswith("'s"):
                tok = tok[:-2]
            out.append(tok)
    return out


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def pack(text: str, target: int, hard_max: int | None = None) -> list[str]:
    """按段落、再按行/句切成约 ``target`` 字的片段。"""
    limit = hard_max or int(target * 1.4)
    pieces: list[str] = []
    cur = ""
    for para in split_paragraphs(text):
        if len(para) > limit:
            # 超长段落：按句子或行切
            for line in re.split(r"(?<=[.!?。！？])\s+|\n", para):
                if not line:
                    continue
                if cur and len(cur) + len(line) + 1 > target:
                    pieces.append(cur)
                    cur = ""
                cur = (cur + "\n" + line) if cur else line
            continue
        if cur and len(cur) + len(para) + 2 > target:
            pieces.append(cur)
            cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        pieces.append(cur)
    return pieces


def shorten(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


_ZH_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def chinese_numeral(s: str) -> int | None:
    """一二三…十/百 转为整数；阿拉伯数字直接转换；不是数字返回 None。"""
    if s.isdigit():
        return int(s)
    if not s or any(ch not in _ZH_DIGITS and ch not in "十百" for ch in s):
        return None
    total, num = 0, 0
    for ch in s:
        if ch == "十":
            total += (num or 1) * 10
            num = 0
        elif ch == "百":
            total += (num or 1) * 100
            num = 0
        else:
            num = _ZH_DIGITS[ch]
    return total + num


def display_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
