from __future__ import annotations

import pytest

from kaopu_agent.context import ContextAssembler, ContextSection
from kaopu_llm import estimate_tokens


def sec(name: str, n: int, **kw: object) -> ContextSection:
    return ContextSection(name=name, content="字" * n, **kw)  # type: ignore[arg-type]


def test_within_budget_untouched() -> None:
    ctx = ContextAssembler(1000).assemble([sec("L0", 100, cacheable=True), sec("L5", 200)])
    assert ctx.total_tokens == 300
    assert not ctx.overflow
    assert [r.tokens_after for r in ctx.reports] == [100, 200]
    assert not any(r.truncated or r.dropped for r in ctx.reports)


def test_section_budget() -> None:
    ctx = ContextAssembler(10_000).assemble([sec("L5", 500, budget=100)])
    r = ctx.reports[0]
    assert r.truncated
    assert r.tokens_before == 500
    assert r.tokens_after <= 100
    assert ctx.sections[0].content.endswith("（已截断）")


def test_total_budget_cuts_low_priority_first() -> None:
    sections = [
        sec("L0", 100, priority=100, truncatable=False, cacheable=True),
        sec("L4", 50, priority=90, truncatable=False),
        sec("L5", 400, priority=50),
        sec("L6", 200, priority=10),
        sec("L7", 300, priority=10),
    ]
    ctx = ContextAssembler(600).assemble(sections)
    by = {r.name: r for r in ctx.reports}
    # 同优先级时靠后的 L7 先裁：超出 450，L7（300）整层丢弃，L6 再裁 150
    assert by["L7"].dropped
    assert by["L6"].truncated
    assert by["L5"].tokens_after == 400
    assert by["L0"].tokens_after == 100
    assert ctx.total_tokens <= 600
    assert not ctx.overflow
    assert [s.name for s in ctx.sections] == ["L0", "L4", "L5", "L6"]  # 保持输入顺序


def test_overflow_when_untruncatable_exceeds() -> None:
    ctx = ContextAssembler(100).assemble([sec("L0", 150, truncatable=False), sec("L5", 50)])
    assert ctx.overflow
    assert ctx.reports[1].dropped
    assert ctx.total_tokens == 150


def test_to_system_messages_merges_adjacent() -> None:
    ctx = ContextAssembler(1000).assemble(
        [
            ContextSection(name="L0", content="策略", cacheable=True),
            ContextSection(name="L1", content="工具", cacheable=True),
            ContextSection(name="L4", content="本轮"),
            ContextSection(name="L5", content=""),
            ContextSection(name="L6", content="摘要"),
        ]
    )
    msgs = ctx.to_system_messages()
    assert [(m.content, m.cacheable) for m in msgs] == [("策略\n\n工具", True), ("本轮\n\n摘要", False)]
    table = ctx.report_table()
    assert "L0" in table
    assert "预算 1000" in table


def test_validation() -> None:
    with pytest.raises(ValueError, match="负"):
        ContextAssembler(-1)
    with pytest.raises(ValueError, match="重复"):
        ContextAssembler(10).assemble([sec("a", 1), sec("a", 1)])


def test_custom_counter_and_tiny_remaining() -> None:
    # 截断按自定义计数器计算：len("…（已截断）") == 6
    ctx = ContextAssembler(8, counter=len).assemble([ContextSection(name="x", content="abcdefghij")])
    assert ctx.sections[0].content == "ab…（已截断）"
    assert ctx.total_tokens == 8
    # 剩余额度放不下截断标记：整层丢弃
    ctx = ContextAssembler(5, counter=len).assemble([ContextSection(name="x", content="abcdefghij")])
    assert ctx.reports[0].dropped
    assert ctx.total_tokens == 0
    assert estimate_tokens("") == 0
