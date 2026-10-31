# -*- coding: utf-8 -*-
"""Real-course live comparison: MIT 6.006 (lec 1-6) + Yale PSYC 110 (lec 1-4).

Method (user-approved 2026-10-31; NOT a comparison against the skill repo):

* Answering arms read ONLY ``live_compare_work/questions_only.json``
  (id + question). They never open the gold files. The scorer in this module
  is the only code path that reads gold, and it runs after all answers exist.
* Scorer v2 (same ruler for all seven arms): normalise gold and candidate
  answers, extract content tokens (ASCII/digit tokens of length >= 2 minus
  stopwords; CJK characters as single-char tokens), and mark an answerable
  item correct when >= 80% of the gold token set is covered by the answer.
  Probes (answerable=false) are correct only on refusal.
* Known v2 edge case, kept deliberately (do NOT "fix" it silently, it would
  change the ruler mid-experiment): when the gold token set is empty
  (algo_q6 gold "3", algo_q7 gold "O(1)" -- every token is shorter than 2),
  coverage is defined as 0.0, so those two items score 0 for every arm even
  when the surface answer looks right. See LIVE_COMPARE.md.
* v1 (verbatim substring containment) is retired: it marked correct
  generative answers wrong for short golds such as "A stroke" (psyc_q4:
  "It is typically the result of some sort of stroke" does not contain the
  verbatim string "A stroke" as a substring in the required direction).

Costs: every DeepSeek arm counts API calls and interface ``usage`` tokens.
DeepSeek V4.1 Flash (``deepseek-flash``) official October 2026 rates, used
for the USD estimates in the report: input $0.15 / $0.30 per M tokens
(off-peak / peak), cache-hit input $0.003 / $0.006, output $0.60 / $1.20.
This run took place on a Saturday (off-peak all day), so off-peak rates are
applied; the actually billed total is cross-checked against the account
balance delta. dsh usage comes from its ``--json`` step_end events
(inputTokens + cacheReadTokens = prompt, outputTokens = completion).
Claude arms have no cost data (blind subagents, no API usage recorded) and
are reported with cost = null, never estimated.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- v2 scorer
STOPWORDS = frozenset(
    "a an the and or is are was were be been being of in on at by for with to "
    "from as this that these those it its his her their your our you we they "
    "he she i me my do does did can could will would should often".split()
)
_ASCII_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

REFUSAL_EXACT = {"无法从材料中找到", "不知道", "无法回答", ""}
REFUSAL_PREFIXES = (
    "无法从材料中找到",
    "不知道",
    "[sub-agent produced no safe summary]",
    "the material does not cover",
    "the materials do not cover",
    "not covered by the material",
    "no evidence in the material",
    "i don't know",
    "i do not know",
)


def content_tokens(text: str) -> set[str]:
    """v2 content tokens: ASCII/digit tokens len>=2 minus stopwords + CJK chars."""
    lowered = (text or "").lower()
    lowered = lowered.replace("−", "-").replace("–", "-").replace("—", "-")
    tokens = {tok for tok in _ASCII_RE.findall(lowered) if len(tok) >= 2 and tok not in STOPWORDS}
    tokens.update(_CJK_RE.findall(text or ""))
    return tokens


def is_refusal(answer: str) -> bool:
    stripped = (answer or "").strip()
    if stripped in REFUSAL_EXACT:
        return True
    low = stripped.lower()
    if any(low.startswith(prefix) for prefix in REFUSAL_PREFIXES):
        return True
    # A short answer that embeds the exact refusal string is a refusal too.
    if "无法从材料中找到" in stripped or stripped == "不知道":
        return True
    return False


def score_one(gold_answer: str, answer: str, answerable: bool) -> dict:
    """Score one item under v2. ``gold_answer`` is read by the scorer only."""
    refused = is_refusal(answer or "")
    gold_tokens = content_tokens(gold_answer) if answerable else set()
    if not answerable:
        return {"refused": refused, "correct": refused, "token_frac": None,
                "gold_tokens": sorted(gold_tokens)}
    if refused or not gold_tokens:
        return {"refused": refused, "correct": False, "token_frac": 0.0,
                "gold_tokens": sorted(gold_tokens)}
    answer_tokens = content_tokens(answer or "")
    covered = gold_tokens & answer_tokens
    frac = len(covered) / len(gold_tokens)
    return {"refused": False, "correct": frac >= 0.8, "token_frac": round(frac, 3),
            "gold_tokens": sorted(gold_tokens)}


# ---------------------------------------------------------------- cost counting
class CountingProvider:
    """Wrap an OpenAI-compatible provider, counting calls and usage tokens.

    ``raw`` usage is kept so cached prompt tokens can be priced separately.
    """

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "counting")
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0

    def complete(self, messages, tools=(), *, temperature=None):
        response = self.inner.complete(messages, tools, temperature=temperature)
        self.calls += 1
        if response.usage is not None:
            self.prompt_tokens += response.usage.prompt_tokens
            self.completion_tokens += response.usage.completion_tokens
        raw_usage = (response.raw or {}).get("usage") or {}
        details = raw_usage.get("prompt_tokens_details") or {}
        self.cached_tokens += int(details.get("cached_tokens") or 0)
        return response

    def totals(self) -> dict:
        return {
            "api_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_prompt_tokens": self.cached_tokens,
            "uncached_prompt_tokens": self.prompt_tokens - self.cached_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }


def deepseek_cost_usd(prompt: int, completion: int, cached: int, *, off_peak: bool = True) -> float:
    """USD estimate at official deepseek-flash rates (see module docstring)."""
    miss = 0.15 if off_peak else 0.30
    hit = 0.003 if off_peak else 0.006
    out = 0.60 if off_peak else 1.20
    return ((prompt - cached) * miss + cached * hit + completion * out) / 1_000_000


# ---------------------------------------------------------------- gold loading (scorer only)
def load_gold(gold_dir: str) -> dict:
    gold = {}
    for filename in ("items_pilot_6006.jsonl", "items_pilot_psyc110.jsonl"):
        path = os.path.join(gold_dir, filename)
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                item = json.loads(line)
                gold[item["id"]] = item
    return gold


def score_arm(answers: dict, gold: dict) -> dict:
    """Score a whole arm. ``answers`` maps id -> answer string (no gold inside)."""
    records = []
    correct = 0
    answerable_total = 0
    probe_refused = 0
    probes = 0
    for item_id in sorted(gold):
        item = gold[item_id]
        result = score_one(item.get("gold_answer", ""), answers.get(item_id, ""), bool(item["answerable"]))
        record = {"id": item_id, "answerable": bool(item["answerable"]),
                  "refused": result["refused"], "correct": result["correct"] if item["answerable"] else None,
                  "token_frac": result["token_frac"], "gold": item.get("gold_answer", ""),
                  "answer": answers.get(item_id, "")}
        records.append(record)
        if item["answerable"]:
            answerable_total += 1
            correct += 1 if result["correct"] else 0
        else:
            probes += 1
            probe_refused += 1 if result["refused"] else 0
    return {"correct": correct, "answerable": answerable_total,
            "probe_refused": probe_refused, "probes": probes, "records": records}


def main() -> int:
    print("live_compare_real: scorer + arm helpers. Run arms via run_live_compare.sh steps; "
          "use score_arm()/score_one() for scoring.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
