"""每轮的上下文组装（04 文档第 2 节）。

稳定的层（L0 策略、L2 课程目录）放在系统消息最前面以命中 prompt cache；学习者快照（L3）随后；
本轮任务（L4）和证据（L5）放进最后一条用户消息，不破坏前面的缓存前缀。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from examprep_llm import SystemMessage, estimate_tokens
from examprep_runtime import ContextAssembler, ContextSection

from ..agents import prompts
from ..agents.citations import CitationMap
from ..ingest import guard
from ..knowledge.evidence import EvidenceChunk

TOC_RADIUS = 3


@dataclass(slots=True)
class LearnerSnapshot:
    chapter: int
    chapter_title: str
    part: int
    parts: int
    status: str
    confusions: list[str]
    open_mistakes: int


def system_messages(
    language: str,
    course: str,
    toc: Sequence[tuple[int, str]],
    snap: LearnerSnapshot,
    *,
    budget: int,
) -> list[SystemMessage]:
    zh = language == "zh"
    nearby = [(n, t) for n, t in toc if abs(n - snap.chapter) <= TOC_RADIUS]
    lines = [f"{'课程' if zh else 'Course'}：{course}", "目录：" if zh else "Contents:"]
    lines += [f"{'→' if n == snap.chapter else ' '} {n}. {t}" for n, t in nearby]
    l3 = [
        (
            f"当前：第 {snap.chapter} 章《{snap.chapter_title}》，第 {snap.part}/{snap.parts} 段，状态 {snap.status}"
        )
        if zh
        else f"Now: chapter {snap.chapter} ({snap.chapter_title}), part {snap.part}/{snap.parts}, status {snap.status}",
        (f"未解决的错题：{snap.open_mistakes} 道" if zh else f"Open mistakes: {snap.open_mistakes}"),
    ]
    if snap.confusions:
        l3.append("本章学生没懂过的地方：" if zh else "Student found these confusing in this chapter:")
        l3 += [f"- {c}" for c in snap.confusions[-5:]]
    sections = [
        ContextSection(
            name="L0",
            content=prompts.system_policy(language),
            priority=100,
            cacheable=True,
            truncatable=False,
        ),
        ContextSection(name="L2", content="\n".join(lines), priority=80, cacheable=True, budget=800),
        ContextSection(name="L3", content="\n".join(l3), priority=60, budget=500),
    ]
    return ContextAssembler(budget).assemble(sections).to_system_messages()


def materials(chunks: Sequence[EvidenceChunk], cmap: CitationMap, *, budget: int, language: str) -> str:
    """把证据块编号并装进围栏；超出预算的块不放（保留顺序）。"""
    out: list[str] = []
    used = 0
    for c in chunks:
        block = f"[{cmap.add(c)}] {c.rel} p.{c.page}\n{guard.fence(c.text)}"
        cost = estimate_tokens(block)
        if out and used + cost > budget:
            break
        out.append(block)
        used += cost
    if not out:
        return ""
    head = "## 资料" if language == "zh" else "## Materials"
    return head + "\n\n" + "\n\n".join(out)


def user_turn(language: str, student: str, task: str, material: str, extra: str = "") -> str:
    zh = language == "zh"
    parts = [f"{'学生说' if zh else 'Student'}：{student}" if student else ""]
    parts.append(("## 本轮任务\n" if zh else "## Task\n") + task)
    if extra:
        parts.append(extra)
    if material:
        parts.append(material)
    return "\n\n".join(p for p in parts if p)
