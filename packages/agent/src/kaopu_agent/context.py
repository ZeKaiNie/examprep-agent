"""分层上下文组装与 token 预算（R10）。P1 只做组装与预算；压缩在 P4。

各层按输入顺序输出（稳定层放前面以命中 prompt cache）。超出总预算时，按优先级从低到高裁剪可裁层：
先截尾到剩余额度，额度不足则整层丢弃。每层给出裁剪前后的 token 数，供“检查器”面板展示。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from kaopu_llm import SystemMessage, estimate_tokens, truncate_to_tokens

TokenCounter = Callable[[str], int]


class ContextSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    content: str
    priority: int = 0  # 越大越重要，越晚被裁
    cacheable: bool = False
    budget: int | None = Field(default=None, ge=0)  # 本层上限（token）
    truncatable: bool = True  # False：不裁，超出时报告 overflow


class SectionReport(BaseModel):
    name: str
    tokens_before: int
    tokens_after: int
    truncated: bool = False
    dropped: bool = False
    cacheable: bool = False


class AssembledContext(BaseModel):
    sections: list[ContextSection]
    reports: list[SectionReport]
    total_tokens: int
    budget: int
    overflow: bool

    def to_system_messages(self, *, separator: str = "\n\n") -> list[SystemMessage]:
        """相邻且缓存属性相同的层合并成一条 SystemMessage。"""
        out: list[SystemMessage] = []
        for s in self.sections:
            if not s.content:
                continue
            if out and out[-1].cacheable == s.cacheable:
                out[-1] = SystemMessage(
                    content=out[-1].content + separator + s.content, cacheable=s.cacheable
                )
            else:
                out.append(SystemMessage(content=s.content, cacheable=s.cacheable))
        return out

    def report_table(self) -> str:
        """简单的文本表格，便于日志与调试。"""
        lines = [f"{'层':<16}{'前':>8}{'后':>8}  状态"]
        for r in self.reports:
            state = "丢弃" if r.dropped else ("截断" if r.truncated else "")
            lines.append(f"{r.name:<16}{r.tokens_before:>8}{r.tokens_after:>8}  {state}")
        lines.append(
            f"{'合计':<16}{'':>8}{self.total_tokens:>8}  预算 {self.budget}{' 超出' if self.overflow else ''}"
        )
        return "\n".join(lines)


class _Work:
    __slots__ = ("before", "content", "dropped", "section", "tokens", "truncated")

    def __init__(self, section: ContextSection, counter: TokenCounter) -> None:
        self.section = section
        self.content = section.content
        self.before = counter(section.content)
        self.tokens = self.before
        self.truncated = False
        self.dropped = False


class ContextAssembler:
    def __init__(self, budget: int, *, counter: TokenCounter = estimate_tokens) -> None:
        if budget < 0:
            raise ValueError("budget 不能为负")
        self.budget = budget
        self.counter = counter

    def _cut(self, w: _Work, limit: int) -> None:
        if limit <= 0:
            w.content, w.tokens, w.dropped = "", 0, True
            return
        cut = truncate_to_tokens(w.content, limit, counter=self.counter)
        if cut == "":
            w.content, w.tokens, w.dropped = "", 0, True
            return
        w.content = cut
        w.tokens = self.counter(cut)
        w.truncated = True

    def assemble(self, sections: Sequence[ContextSection]) -> AssembledContext:
        names = [s.name for s in sections]
        if len(set(names)) != len(names):
            raise ValueError("层名称重复")
        work = [_Work(s, self.counter) for s in sections]

        # 1. 每层自身预算
        for w in work:
            b = w.section.budget
            if b is not None and w.tokens > b and w.section.truncatable:
                self._cut(w, b)

        # 2. 总预算：优先级低的先裁；同优先级时靠后的先裁
        total = sum(w.tokens for w in work)
        order = sorted(
            (i for i, w in enumerate(work) if w.section.truncatable),
            key=lambda i: (work[i].section.priority, -i),
        )
        for i in order:
            excess = total - self.budget
            if excess <= 0:
                break
            w = work[i]
            if w.tokens == 0:
                continue
            old = w.tokens
            self._cut(w, old - excess)
            total += w.tokens - old

        reports = [
            SectionReport(
                name=w.section.name,
                tokens_before=w.before,
                tokens_after=w.tokens,
                truncated=w.truncated and not w.dropped,
                dropped=w.dropped,
                cacheable=w.section.cacheable,
            )
            for w in work
        ]
        kept = [w.section.model_copy(update={"content": w.content}) for w in work if not w.dropped]
        return AssembledContext(
            sections=kept,
            reports=reports,
            total_tokens=total,
            budget=self.budget,
            overflow=total > self.budget,
        )
