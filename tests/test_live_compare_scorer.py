# -*- coding: utf-8 -*-
"""Tests for the live real-course comparison scorer (v2 token-set ruler).

The scorer is the only code path allowed to read gold answers; these tests
use synthetic gold strings, never the pilot gold files.
"""
from __future__ import annotations

from benchmarks.live_compare_real import content_tokens, is_refusal, score_one


def test_content_tokens_drop_stopwords_and_short_tokens():
    tokens = content_tokens("O(n log n) and the quick brown fox")
    assert tokens == {"log", "quick", "brown", "fox"}


def test_v2_marks_paraphrase_correct_above_threshold():
    result = score_one(
        "Neuroscience, developmental, cognitive, social, and clinical",
        "Neuroscience, developmental, cognitive, social, and clinical",
        True,
    )
    assert result["correct"] is True and result["token_frac"] == 1.0


def test_v2_marks_short_gold_paraphrase_correct_where_v1_failed():
    # v1 verbatim containment marked this wrong; v2 token coverage marks it right.
    result = score_one("A stroke", "It is typically the result of some sort of stroke", True)
    assert result["correct"] is True


def test_v2_threshold_is_80_percent():
    result = score_one("alpha beta gamma delta epsilon", "alpha beta gamma delta", True)
    assert result["token_frac"] == 0.8 and result["correct"] is True
    result = score_one("alpha beta gamma delta epsilon", "alpha beta gamma", True)
    assert result["correct"] is False


def test_empty_gold_token_set_scores_zero_by_construction():
    # Gold "3" and "O(1)" yield no len>=2 content tokens; the ruler scores 0.
    # This is a documented limitation, not a model failure (see LIVE_COMPARE.md).
    assert score_one("3", "3", True)["correct"] is False
    assert score_one("O(1)", "Expected O(1) time", True)["correct"] is False


def test_probes_are_correct_only_on_refusal():
    assert score_one("", "无法从材料中找到", False)["correct"] is True
    assert score_one("", "O(n log n)", False)["correct"] is False


def test_refusal_detection():
    assert is_refusal("无法从材料中找到") is True
    assert is_refusal("不知道") is True
    assert is_refusal("O(n log n)") is False
    assert is_refusal("The material does not cover this question.") is True


def test_refused_answerable_item_is_incorrect():
    result = score_one("O(n log n)", "无法从材料中找到", True)
    assert result["refused"] is True and result["correct"] is False
