"""引用渲染：编号映射、合并、删除不存在的编号、流式与一次性结果一致。"""

from __future__ import annotations

import random

import pytest

from examprep.agents.citations import (
    CitationMap,
    CitationStream,
    has_markers,
    labels_in,
    render,
    strip_markers,
)
from examprep.knowledge.evidence import EvidenceChunk


def chunk(eid: str, rel: str, page: int) -> EvidenceChunk:
    return EvidenceChunk(eid, rel, page, 1, 0, "text", 0, 4, 1)


@pytest.fixture
def cmap() -> CitationMap:
    m = CitationMap([chunk("Eaaa", "slides/lec1.pdf", 3), chunk("Ebbb", "第2章_栈和队列.md", 1)])
    assert m.add(chunk("Eaaa", "slides/lec1.pdf", 3)) == "E1"  # 同一证据不重复编号
    assert m.add(chunk("Eccc", "slides/lec1.pdf", 3)) == "E3"  # 同一页的另一个块
    return m


def test_render_replaces_and_merges(cmap: CitationMap) -> None:
    r = render("栈是后进先出的 [[E2]]。插入要移动元素[[E1]][[E3]]，见上[[E1, E2]]。", cmap)
    assert (
        r.text
        == "栈是后进先出的（第2章_栈和队列.md p.1）。插入要移动元素（lec1.pdf p.3），见上（lec1.pdf p.3；第2章_栈和队列.md p.1）。"
    )
    assert [c.label for c in r.citations] == ["E2", "E1", "E3"]
    assert r.citations[1].evidence_id == "Eaaa"
    assert r.unknown == []


def test_unknown_labels_are_dropped(cmap: CitationMap) -> None:
    r = render("编造的页码 [[E9]]。部分编造 [[E2, E7]]。", cmap)
    assert r.text == "编造的页码。部分编造（第2章_栈和队列.md p.1）。"
    assert r.unknown == ["E9", "E7"]


def test_no_markers(cmap: CitationMap) -> None:
    assert render("普通文本 [E1] [ [E2] ]", cmap).text == "普通文本 [E1] [ [E2] ]"
    assert not has_markers("普通文本")
    assert has_markers("x [[E1]]")


def test_strip_markers() -> None:
    assert strip_markers("栈后进先出 [[E2]]。队列[[E1]][[E3]]先进先出。") == "栈后进先出。队列先进先出。"


def test_fence_ids_written_as_markers_are_dropped(cmap: CitationMap) -> None:
    # 真实模型（deepseek-flash）会把参考答案围栏的 id 当编号写出来，不能原样显示给学生
    r = render("与参考答案一致 [[c8406b]]。见资料[[E2, df7e70]]。", cmap)
    assert r.text == "与参考答案一致。见资料（第2章_栈和队列.md p.1）。"
    assert r.unknown == ["c8406b", "df7e70"]
    assert labels_in("一致 [[c8406b]]。见[[E2, df7e70]]。") == ["E2"]  # 围栏 id 不交给 Verifier 核对
    assert strip_markers("一致 [[c8406b]]。") == "一致。"


SAMPLE = (
    "顺序表插入第 i 个位置时，后面的 n - i + 1 个元素都要后移 [[E1]]。"
    "链表只需改指针[[E2]] [[E3]]，不需要移动元素。\n\n"
    "注意 [[E8]] 这是编造的。数组下标 a[0] 和 [[E1, E2]] 结尾 [["
)


@pytest.mark.parametrize("seed", range(20))
def test_stream_equals_one_shot(cmap: CitationMap, seed: int) -> None:
    rng = random.Random(seed)
    expected = render(SAMPLE, cmap)
    stream = CitationStream(cmap)
    out: list[str] = []
    i = 0
    while i < len(SAMPLE):
        n = rng.randint(1, 6)
        out.append(stream.feed(SAMPLE[i : i + n]))
        i += n
    out.append(stream.flush())
    text = "".join(out)
    assert text == expected.text
    result = stream.result(text)
    assert result.citations == expected.citations
    assert result.unknown == expected.unknown


def test_stream_emits_promptly(cmap: CitationMap) -> None:
    s = CitationStream(cmap)
    assert s.feed("第一句。") == "第一句。"
    assert s.feed("第二句 [[E") == "第二句"
    assert s.feed("1]]") == ""
    assert s.feed("。") == "（lec1.pdf p.3）。"
