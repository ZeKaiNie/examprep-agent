"""证据块与检索（含 Skill tests/test_index_state.py 的检索部分）。"""

from __future__ import annotations

from examprep.ingest.chapters import Chapter
from examprep.knowledge.evidence import EvidenceIndex, build_evidence, locate


def chapter(n: int, text: str, rel: str | None = None) -> Chapter:
    return Chapter(n, f"T{n}", blocks=[(rel or f"f{n}.md", 1, text)])


TEXTS = {
    1: "Counting sort runs in linear time when keys are small integers.",
    2: "循环队列用数组实现，判满条件是 (rear + 1) % MaxSize == front。",
}


def make_index() -> EvidenceIndex:
    chs = [chapter(n, t) for n, t in TEXTS.items()]
    pages = {(f"f{n}.md", 1): t for n, t in TEXTS.items()}
    return EvidenceIndex(build_evidence(chs, pages))


def test_hit_and_miss() -> None:
    idx = make_index()
    hits = idx.search("counting sort linear time")
    assert hits[0].chunk.chapter == 1
    assert hits[0].score > 0
    assert idx.search("quantum entanglement") == []


def test_cjk_query_and_chapter_filter() -> None:
    idx = make_index()
    assert idx.search("循环队列 判满")[0].chunk.chapter == 2
    assert idx.search("循环队列 判满", chapter=1) == []
    assert idx.search("循环队列", chapter=9) == []


def test_chunks_have_spans_and_stable_ids() -> None:
    page_text = "\n\n".join(f"第 {i} 段：" + "栈是后进先出的线性表。" * 12 for i in range(6))
    chs = [chapter(1, page_text, "lec1.md")]
    pages = {("lec1.md", 1): page_text}
    a = build_evidence(chs, pages)
    b = build_evidence(chs, pages)
    assert len(a) > 1
    assert [c.id for c in a] == [c.id for c in b]
    assert len({c.id for c in a}) == len(a)
    assert all(c.id.startswith("E") and len(c.id) == 11 for c in a)
    for c in a:
        assert c.char_start is not None
        assert c.char_end is not None
        assert page_text[c.char_start : c.char_end] == c.text
        assert c.tokens > 0
    assert [c.ordinal for c in a] == list(range(len(a)))


def test_locate_fallbacks() -> None:
    assert locate("abc", "") == (None, None)
    assert locate("zzz", "abc") == (None, None)
    page = "First sentence here.\nSecond sentence there."
    start, end = locate("First sentence here.\nSecond sentence there.", page)
    assert (start, end) == (0, len(page))
    s, e = locate("First sentence here. Second sentence there.", page)  # 断行被改写
    assert s == 0
    assert e == len(page)
