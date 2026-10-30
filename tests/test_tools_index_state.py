# -*- coding: utf-8 -*-
"""BM25 index and study-state tests ported from universal-examprep-skill (MIT)."""
from __future__ import annotations

import unittest

from examprep_agent.tools import state as st
from examprep_agent.tools.chapters import Chapter
from examprep_agent.tools.index import build_chunks, search


def chapter(n: int, text: str) -> Chapter:
    c = Chapter(n, "T%d" % n)
    c.blocks = [("f%d.md" % n, 1, text)]
    return c


class IndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.chunks = build_chunks([
            chapter(1, "Counting sort runs in linear time when keys are small integers."),
            chapter(2, "循环队列用数组实现，判满条件是 (rear + 1) % MaxSize == front。"),
        ])

    def test_hit_and_miss(self) -> None:
        hits = search(self.chunks, "counting sort linear time")
        self.assertEqual(hits[0][0]["chapter"], 1)
        self.assertEqual(search(self.chunks, "quantum entanglement"), [])

    def test_cjk_query_and_chapter_filter(self) -> None:
        self.assertEqual(search(self.chunks, "循环队列 判满")[0][0]["chapter"], 2)
        self.assertEqual(search(self.chunks, "循环队列 判满", chapter=1), [])

    def test_empty_pool(self) -> None:
        self.assertEqual(search([], "anything"), [])
        self.assertEqual(search(self.chunks, "anything", chapter=99), [])


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        chapters = [{"n": 1, "title": "A", "sources": ["a.md"], "parts": 2, "questions": 1},
                    {"n": 2, "title": "B", "sources": ["b.md"], "parts": 1, "questions": 0}]
        self.s = st.new_state("course", "en", 3, "/m", chapters, 5000)

    def test_record_results_and_mistakes(self) -> None:
        st.record_result(self.s, "q001", 1, "wrong", "note")
        self.assertEqual(len(st.open_mistakes(self.s)), 1)
        st.record_result(self.s, "q001", 1, "skip")
        self.assertEqual(self.s["mistakes"][0]["count"], 2)
        st.record_result(self.s, "q001", 1, "right")
        self.assertEqual(st.open_mistakes(self.s), [])
        self.assertEqual(st.chapter_results(self.s, 1), (1, 3))

    def test_render_progress_zh_and_en(self) -> None:
        self.s["notes"].append({"chapter": 1, "type": "confusion", "text": "why", "ts": "t"})
        en = st.render_progress(self.s)
        self.assertIn("| → 1 | A | todo | 0/0 |", en)
        self.assertIn("- ch1: why", en)
        self.s["language"] = "zh"
        self.assertIn("# 复习进度", st.render_progress(self.s))

    def test_days_left_and_exam_date(self) -> None:
        import datetime
        s = {"exam_date": "2026-03-05"}
        self.assertEqual(st.days_left(s, datetime.date(2026, 3, 3)), 2)
        self.assertEqual(st.days_left(s, datetime.date(2026, 3, 9)), 0)
        self.assertIsNone(st.days_left({}))
        self.assertEqual(st.exam_date_in(4, datetime.date(2026, 3, 1)), "2026-03-05")

    def test_atomic_save_and_load(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as ws:
            st.save(ws, self.s)
            loaded = st.load(ws)
            assert loaded is not None
            self.assertEqual(loaded["course"], "course")
            self.assertFalse(__import__("os").path.exists(st.path(ws) + ".tmp"))
