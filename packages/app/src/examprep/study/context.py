"""每轮的上下文组装（04 文档第 2 节）。

提供方的 prompt cache 按请求开头的 token 序列匹配（DeepSeek 要求完整匹配上一次请求的输入或输出结尾），
所以 Tutor 的请求结构是“不变前缀 + 只追加的对话”：

- 系统消息只放一段对话内逐字节不变的层（L0 策略、L2 课程目录、前几章小结、压缩摘要 L6）；
- 对话按章分段，段内只追加，发出去的用户消息和模型原样输出都原样留在历史里，不改写、不滑动；
  一段太长时压缩：摘要进新段的系统消息，原文丢弃；
- 证据编号在段内共用，已经给过的资料不再重发；
- 会变的学习者状态（L3）、本轮任务（L4）和新证据（L5）只出现在最后一条用户消息里。

真实运行（deepseek-flash）测得：旧做法（L3 在系统消息里、历史只存“继续”这类短句并每轮滑动、证据每轮重发）
Tutor 的缓存命中率约 11%。
"""

from __future__ import annotations

import re
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
    *,
    chapter: int,
    budget: int,
    recap: str = "",
    summary: str = "",
) -> list[SystemMessage]:
    """L0 + L2 + 前几章小结 + 本段之前对话的摘要（L6）。

    这些内容只在开新段时变化，同一段对话内逐字节不变（当前位置写在用户消息里），以命中 prompt cache。
    """
    zh = language == "zh"
    nearby = [(n, t) for n, t in toc if abs(n - chapter) <= TOC_RADIUS]
    lines = [f"{'课程' if zh else 'Course'}：{course}", "目录：" if zh else "Contents:"]
    lines += [f"{n}. {t}" for n, t in nearby]
    sections = [
        ContextSection(
            name="L0",
            content=prompts.system_policy(language),
            priority=100,
            cacheable=True,
            truncatable=False,
        ),
        ContextSection(name="L2", content="\n".join(lines), priority=80, cacheable=True, budget=800),
    ]
    if recap:
        head = (
            "## 前面几章的小结（学生已经学过）\n"
            if zh
            else "## Summaries of earlier chapters (already learned)\n"
        )
        sections.append(
            ContextSection(name="L2b", content=head + recap, priority=60, cacheable=True, budget=1500)
        )
    if summary:
        head = (
            "## 这一章前面对话的摘要（原文已压缩，接着这里往下教）\n"
            if zh
            else "## Summary of the earlier conversation in this chapter (compacted; continue from here)\n"
        )
        sections.append(
            ContextSection(name="L6", content=head + summary, priority=70, cacheable=True, budget=1500)
        )
    return ContextAssembler(budget).assemble(sections).to_system_messages()


# 渲染后的出处“（第1章_线性表.md p.1；lec2.pdf p.3）”
_RENDERED_CITE = re.compile(r"（[^（）\n]*?\bp\.\d+）")
RECAP_CHAPTERS = 5


def recap_text(language: str, chapters: Sequence[tuple[int, str, str]]) -> str:
    """前几章的小结（章号, 标题, 小结），只取最近 RECAP_CHAPTERS 章；去掉渲染好的出处，避免模型照抄页码。"""
    zh = language == "zh"
    out: list[str] = []
    for n, title, text in list(chapters)[-RECAP_CHAPTERS:]:
        body = _RENDERED_CITE.sub("", text).strip()
        out.append((f"第 {n} 章《{title}》\n" if zh else f"Chapter {n} ({title})\n") + body)
    return "\n\n".join(out)


def learner_state(language: str, snap: LearnerSnapshot) -> str:
    """L3：每轮都可能变化，放进最后一条用户消息。"""
    zh = language == "zh"
    lines = [
        "## 学习者状态" if zh else "## Learner state",
        (
            f"当前：第 {snap.chapter} 章《{snap.chapter_title}》，第 {snap.part}/{snap.parts} 段，状态 {snap.status}"
            if zh
            else f"Now: chapter {snap.chapter} ({snap.chapter_title}), part {snap.part}/{snap.parts}, "
            f"status {snap.status}"
        ),
        f"未解决的错题：{snap.open_mistakes} 道" if zh else f"Open mistakes: {snap.open_mistakes}",
    ]
    if snap.confusions:
        lines.append("本章学生没懂过的地方：" if zh else "Student found these confusing in this chapter:")
        lines += [f"- {c}" for c in snap.confusions[-5:]]
    return "\n".join(lines)


@dataclass(slots=True)
class Materials:
    text: str  # 放进本轮用户消息的部分：只含本段对话里第一次出现的块
    labels: list[str]  # 本轮相关的全部编号（含前面已经给过的）
    new_ids: list[str]  # 本轮新编号的证据 id（按编号顺序，恢复会话时据此重建编号表）


def materials(chunks: Sequence[EvidenceChunk], cmap: CitationMap, *, budget: int, language: str) -> Materials:
    """给证据块编号并装进围栏。

    编号表在一段对话（同一章）内共用：前面已经给过的块不再重复发送，只列出编号，
    这样历史保持只追加，缓存前缀不断；新块超出预算时不放（保留顺序）。
    """
    zh = language == "zh"
    sep = "、" if zh else ", "
    blocks: list[str] = []
    labels: list[str] = []
    old: list[str] = []
    new_ids: list[str] = []
    used = 0
    for c in chunks:
        known = cmap.label_of(c.id)
        if known is not None:
            if known not in labels:
                labels.append(known)
                old.append(known)
            continue
        body = guard.fence(c.text)
        cost = estimate_tokens(body) + 16
        if blocks and used + cost > budget:
            break
        label = cmap.add(c)
        labels.append(label)
        new_ids.append(c.id)
        blocks.append(f"[{label}] {c.rel} p.{c.page}\n{body}")
        used += cost
    if not labels:
        return Materials("", [], [])
    lines = [
        f"## 资料（本轮相关：{sep.join(labels)}）"
        if zh
        else f"## Materials (relevant now: {sep.join(labels)})"
    ]
    if old:
        lines.append(f"{sep.join(old)} 在前面已经给出。" if zh else f"{sep.join(old)} were given above.")
    text = "\n".join(lines)
    if blocks:
        text += "\n\n" + "\n\n".join(blocks)
    return Materials(text, labels, new_ids)


def user_turn(
    language: str, student: str, task: str, material: str, extra: str = "", *, state: str = ""
) -> str:
    zh = language == "zh"
    parts = [state, f"{'学生说' if zh else 'Student'}：{student}" if student else ""]
    parts.append(("## 本轮任务\n" if zh else "## Task\n") + task)
    if extra:
        parts.append(extra)
    if material:
        parts.append(material)
    return "\n\n".join(p for p in parts if p)
