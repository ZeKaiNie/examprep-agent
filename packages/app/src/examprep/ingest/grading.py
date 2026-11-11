"""短答案的规则判分。移植自 Skill v5.3 questions.py 的 answer_key / grade_answer。

只判能机械比较的答案：选项字母、对错、数值、短填空。其余交给讲解者或判分 Agent，它们能看到参考答案。
文字填空只会判“对”或“不确定”，绝不判错。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

Verdict = Literal["right", "wrong", "unsure"]
KeyKind = Literal["choice", "truth", "number", "text"]
AnswerKey = (
    tuple[Literal["choice"], frozenset[str]]
    | tuple[Literal["truth"], bool]
    | tuple[Literal["number"], tuple[float, int]]
    | tuple[Literal["text"], str]
)

_KEY_PREFIX = re.compile(
    r"^\s*(?:正确答案|参考答案|答案|答|解|answer|ans|key|solution)\s*(?:是|为|is)?\s*[:：.．]?\s*", re.I
)
_LETTERS = re.compile(r"^[（(]?\s*([A-H](?:\s*[,，、/和与]?\s*[A-H])*)\s*[)）]?(?![A-Za-z])")
_LETTER_ANYWHERE = re.compile(
    r"(?:答案|选|answer\s+is|correct\s+(?:answer|option)\s+is)\s*[:：是为]?\s*[（(]?\s*([A-H])(?![A-Za-z])",
    re.I,
)
_TRUE = {"正确", "对", "是", "√", "✓", "✔", "t", "true", "yes", "y", "correct", "right"}
_FALSE = {"错误", "错", "否", "不对", "×", "✗", "✘", "x", "f", "false", "no", "n", "incorrect", "wrong"}
_STRONG_TRUTH = {"正确", "错误", "√", "×", "✓", "✗", "✔", "✘", "true", "false"}  # 非判断题里也无歧义
_NEG = re.compile(r"错|不|否|×|✗|✘|false|incorrect|wrong|\bno\b", re.I)
_POS = re.compile(r"对|正确|是|√|✓|✔|true|correct|right|\byes\b", re.I)
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:\s*/\s*\d+(?:\.\d+)?)?\s*%?")
_PUNCT = re.compile(r"[\s,，.。;；:：、!！?？'\"“”‘’()（）\[\]【】<>《》]+")


def _norm(text: str | None) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def first_line(answer: str | None) -> str:
    for line in (answer or "").split("\n"):
        if line.strip():
            return _KEY_PREFIX.sub("", _norm(line)).strip()
    return ""


def _number(text: str, strict: bool = True) -> float | None:
    """``text`` 中唯一的数（允许分数和百分数），否则 None。

    strict：除了这个数不能有别的内容（参考答案）；否则允许单位或词语（学生作答），只要恰好一个数。
    """
    found = _NUMBER.findall(text)
    if len(found) != 1 or (strict and _NUMBER.sub("", text).strip(" =≈约是为:：")):
        return None
    tok = found[0].replace(" ", "")
    pct = tok.endswith("%")
    tok = tok.rstrip("%")
    try:
        if "/" in tok:
            a, b = tok.split("/")
            val = float(a) / float(b)
        else:
            val = float(tok)
    except (ValueError, ZeroDivisionError):
        return None
    return val / 100 if pct else val


def _decimals(text: str) -> int:
    m = re.search(r"\.(\d+)", text)
    return len(m.group(1)) if m else 0


def _truth(text: str, loose: bool = False) -> bool | None:
    t = _PUNCT.sub("", _norm(text)).lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    if loose and len(t) <= 12:  # 学生作答：“对的”“不正确”“it's false”
        if _NEG.search(t):
            return False
        if _POS.search(t):
            return True
    return None


def answer_key(answer: str | None, qtype: str, options: Sequence[str] = ()) -> AnswerKey | None:
    """能机械比较的参考答案返回 (类型, 值)，否则 None。"""
    norm = _norm(answer)
    if not norm:
        return None
    head = first_line(norm)
    if options:
        m = _LETTERS.match(head) or _LETTER_ANYWHERE.search(norm)
        if m:
            return "choice", frozenset(re.findall(r"[A-H]", m.group(1)))
        return None
    lead_m = re.match(r"^(\S+?)(?=$|[\s.,;:!，。；：！])", head)  # "False. The heap property ..." → "False"
    lead = lead_m.group(1) if lead_m else head
    if qtype == "true_false" or _PUNCT.sub("", lead).lower() in _STRONG_TRUTH:
        truth = _truth(lead) if lead != head or qtype == "true_false" else _truth(head)
        if truth is not None:
            return "truth", truth
    if len(head) > 40:
        return None
    num = _number(head)
    if num is not None:
        return "number", (num, _decimals(head))
    if qtype == "fill_blank" and len(head) <= 30:
        return "text", _PUNCT.sub("", head).lower()
    return None


def grade_answer(
    answer: str | None, qtype: str, options: Sequence[str], given: str
) -> tuple[Verdict, str | None]:
    """判学生作答 ``given``：返回 ("right" | "wrong" | "unsure", 展示给学生的参考答案)。"""
    key = answer_key(answer, qtype, options)
    given = _norm(given)
    if key is None or not given:
        return "unsure", None
    if key[0] == "choice":
        value = key[1]
        shown = "".join(sorted(value))
        # 句子里只认大写字母（避免把冠词 a 读成选项 A）；很短的作答允许小写（"b"、"b,d"）
        pattern = (
            r"(?<![A-Za-z])([A-Ha-h])(?![A-Za-z])" if len(given) <= 5 else r"(?<![A-Za-z])([A-H])(?![A-Za-z])"
        )
        letters = re.findall(pattern, given)
        if not letters:  # 学生可能直接写了选项内容
            g = _PUNCT.sub("", given).lower()
            letters = [o[0] for o in options if _PUNCT.sub("", o[2:]).lower() == g]
        if not letters:
            return "unsure", shown
        return ("right" if frozenset(ch.upper() for ch in letters) == value else "wrong"), shown
    if key[0] == "truth":
        t = _truth(given, loose=True)
        shown = "√" if key[1] else "×"
        return ("unsure" if t is None else "right" if t == key[1] else "wrong"), shown
    if key[0] == "number":
        num, dec = key[1]
        g = _number(given, strict=False)
        if g is None:
            return "unsure", first_line(answer)
        tol = max(0.5 * 10**-dec, 0.01 * abs(num), 1e-9)
        return ("right" if abs(g - num) <= tol else "wrong"), first_line(answer)
    same = _PUNCT.sub("", given).lower() == key[1]
    return ("right" if same else "unsure"), first_line(answer)
