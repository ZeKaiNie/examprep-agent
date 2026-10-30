# -*- coding: utf-8 -*-
"""Knowledge graph of chapters / knowledge points and prerequisite edges.

Nodes are chapter-level KPs (``ch1``, ``ch2``, …) — the granularity BKT
maintains today.  Edges point from a *prerequisite* to the KP that needs it and
come from two deterministic sources:

``sequence``
    Consecutive chapters in teaching order (ch1 → ch2 → ch3).
``reference``
    A chapter that explicitly refers back to an earlier chapter (``第 2 章``,
    ``Chapter 2``, ``§2.1``).  Forward references are ignored: they are usually
    "we will see this later" pointers and would introduce cycles.

Users can revise the edges from the CLI/state (``graph --add-prereq`` /
``graph --remove-prereq``); revisions are stored as ``graph_edits`` in
``study_state.json`` and reapplied whenever the graph is rebuilt.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..security.atomicio import atomic_write_json

GRAPH_FILE = "knowledge_graph.json"
GRAPH_VERSION = 1


def kp_for_chapter(number: int) -> str:
    """Canonical knowledge-point id for a chapter number."""
    return "ch%d" % int(number)


@dataclass
class GraphNode:
    """One knowledge point (a chapter today)."""

    id: str
    title: str
    chapter: int
    sources: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON view for knowledge_graph.json."""
        return {
            "id": self.id,
            "title": self.title,
            "chapter": self.chapter,
            "sources": list(self.sources),
            "concepts": list(self.concepts),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GraphNode":
        """Rebuild a node, tolerating missing optional fields.

        ``chapter`` is read from the field when present.  Only when it is
        missing do we fall back to an ``ch<number>`` id — and a non-``chN`` id
        with no chapter is a hard error rather than a silently dropped node.

        Raises:
            ValueError: when the id is missing or the chapter cannot be read.
        """
        if "id" not in data or data.get("id") in (None, ""):
            raise ValueError("graph node has no id")
        node_id = str(data["id"]).strip()
        raw_chapter = data.get("chapter")
        if raw_chapter is None:
            match = re.fullmatch(r"ch(\d+)", node_id, re.I)
            if not match:
                raise ValueError("graph node %r has no chapter" % node_id)
            chapter = int(match.group(1))
        else:
            chapter = int(raw_chapter)

        def strings(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if not isinstance(value, (list, tuple)):
                raise ValueError("graph node %r field must be a list" % node_id)
            return [str(item) for item in value]

        return cls(
            id=node_id,
            title=str(data.get("title", "")),
            chapter=chapter,
            sources=strings(data.get("sources")),
            concepts=strings(data.get("concepts")),
        )


@dataclass(frozen=True)
class GraphEdge:
    """A prerequisite edge: ``prerequisite`` must be learned before ``target``."""

    prerequisite: str
    target: str
    reason: str = "sequence"
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "from": self.prerequisite,
            "to": self.target,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GraphEdge":
        """Rebuild an edge from stored JSON."""
        return cls(
            prerequisite=str(data.get("from", data.get("prerequisite", ""))),
            target=str(data.get("to", data.get("target", ""))),
            reason=str(data.get("reason", "sequence")),
            evidence=str(data.get("evidence", "")),
        )


# ---------------------------------------------------------------- references
_REF_EN = re.compile(r"\b(?:chapter|chap\.?|lecture|lec\.?|unit|lesson)\s*[.#]?\s*(\d{1,3})\b", re.I)
_REF_ZH = re.compile(r"第\s*(\d{1,3})\s*(?:章|讲|课|单元)")
_REF_SECTION = re.compile(r"§\s*(\d{1,3})(?:\.\d+)*")
_REF_SEE = re.compile(
    r"(?:see|refer\s+to|review|recall|见|参见|参考|回顾|复习)[^\n]{0,24}?"
    r"(?:chapter|section|§|第\s*)\s*(\d{1,3})",
    re.I,
)
_HEAD_MD = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_HEAD_NUM = re.compile(r"^\s*(?:第\s*\d+\s*节|\d+\.\d+(?:\.\d+)?)\s+([^\n]{1,70})$")


def _concepts(text: str, limit: int = 12) -> list[str]:
    """Short heading-like lines inside a chapter, used as concept labels."""
    found: list[str] = []
    for line in text.splitlines():
        match = _HEAD_MD.match(line) or _HEAD_NUM.match(line)
        if not match:
            continue
        title = " ".join(match.group(1).split())
        if 2 <= len(title) <= 70 and title not in found:
            found.append(title)
        if len(found) >= limit:
            break
    return found


def _evidence(text: str, match: re.Match[str], limit: int = 120) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    line = text[start:end if end >= 0 else len(text)]
    clean = " ".join(line.split())
    return clean[:limit] + ("…" if len(clean) > limit else "")


class KnowledgeGraph:
    """Nodes + prerequisite edges with cycle-safe user editing."""

    def __init__(
        self,
        nodes: Mapping[str, GraphNode] | Iterable[GraphNode] | None = None,
        edges: Iterable[GraphEdge] | None = None,
    ) -> None:
        if nodes is None:
            self.nodes: dict[str, GraphNode] = {}
        elif isinstance(nodes, Mapping):
            self.nodes = dict(nodes)
        else:
            self.nodes = {node.id: node for node in nodes}
        self.edges: list[GraphEdge] = []
        for edge in edges or []:
            # Cycles are never useful for prerequisites; drop offending edges
            # even when a caller constructs the graph directly.
            self.add_edge(edge.prerequisite, edge.target, edge.reason, edge.evidence, check_cycle=True)

    # -------------------------------------------------------------- mutation
    def add_node(self, node: GraphNode) -> GraphNode:
        """Add/replace a node."""
        self.nodes[node.id] = node
        return node

    def add_edge(
        self,
        prerequisite: str,
        target: str,
        reason: str = "user",
        evidence: str = "",
        *,
        check_cycle: bool = True,
    ) -> bool:
        """Add a prerequisite edge; returns False for invalid/duplicate/cyclic edges."""
        if prerequisite == target or prerequisite not in self.nodes or target not in self.nodes:
            return False
        if any(edge.prerequisite == prerequisite and edge.target == target for edge in self.edges):
            return False
        if check_cycle and self._reachable(target, prerequisite):
            return False
        self.edges.append(GraphEdge(prerequisite, target, reason, evidence))
        return True

    def remove_edge(self, prerequisite: str, target: str) -> bool:
        """Remove one edge; returns whether it existed."""
        before = len(self.edges)
        self.edges = [
            edge for edge in self.edges
            if not (edge.prerequisite == prerequisite and edge.target == target)
        ]
        return len(self.edges) != before

    def _reachable(self, start: str, goal: str, seen: set[str] | None = None) -> bool:
        """Whether ``goal`` is reachable from ``start`` by following edges forward."""
        if start == goal:
            return True
        seen = seen or set()
        if start in seen:
            return False
        seen.add(start)
        for edge in self.edges:
            if edge.prerequisite == start and self._reachable(edge.target, goal, seen):
                return True
        return False

    # -------------------------------------------------------------- queries
    def prerequisites_of(self, kp: str) -> list[str]:
        """Direct prerequisite ids, stable order."""
        return [edge.prerequisite for edge in self.edges if edge.target == kp]

    def dependents_of(self, kp: str) -> list[str]:
        """Direct dependents of a KP."""
        return [edge.target for edge in self.edges if edge.prerequisite == kp]

    def ancestors(self, kp: str, _seen: set[str] | None = None) -> list[str]:
        """All transitive prerequisites, nearest first (cycle-safe)."""
        out: list[str] = []
        seen = set() if _seen is None else _seen
        for prereq in self.prerequisites_of(kp):
            if prereq in seen:
                continue
            seen.add(prereq)
            out.append(prereq)
            for item in self.ancestors(prereq, seen):
                if item not in out:
                    out.append(item)
        return out

    def has_cycle(self) -> bool:
        """True when the prerequisite edges contain a cycle."""
        indegree = {kp: 0 for kp in self.nodes}
        for edge in self.edges:
            if edge.target in indegree:
                indegree[edge.target] += 1
        ready = [kp for kp, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            kp = ready.pop()
            visited += 1
            for target in self.dependents_of(kp):
                if target not in indegree:
                    continue
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        return visited != len(self.nodes)

    def topological_order(self) -> list[str]:
        """Stable topological order (prerequisites first).

        Raises:
            ValueError: when a cycle exists (a loaded graph is rejected before
                it reaches the planner, so this is a last-resort guard).
        """
        indegree = {kp: 0 for kp in self.nodes}
        for edge in self.edges:
            if edge.target in indegree and edge.prerequisite in indegree:
                indegree[edge.target] += 1
        ready = sorted(
            (kp for kp, degree in indegree.items() if degree == 0),
            key=lambda item: (self.nodes[item].chapter, item),
        )
        ordered: list[str] = []
        while ready:
            kp = ready.pop(0)
            ordered.append(kp)
            for target in sorted(
                self.dependents_of(kp),
                key=lambda item: (self.nodes[item].chapter if item in self.nodes else 0, item),
            ):
                if target not in indegree:
                    continue
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
            ready.sort(key=lambda item: (self.nodes[item].chapter, item))
        if len(ordered) != len(self.nodes):
            raise ValueError("knowledge graph contains a cycle")
        return ordered

    def blocking_chain(
        self,
        kp: str,
        mastery: Callable[[str], float],
        threshold: float,
    ) -> list[tuple[str, float]]:
        """Unmet prerequisites for ``kp``, deepest/oldest first.

        A prerequisite blocks when its BKT mastery is below ``threshold``.  If a
        mastered prerequisite itself has an unmet prerequisite, the deeper gap
        is still reported — knowledge is transitive.
        """
        out: list[tuple[str, float]] = []
        # The target itself can never be its own prerequisite, even if a
        # hand-edited edge list somehow contains a cycle.
        seen: set[str] = {kp}

        def visit(current: str) -> None:
            for prereq in self.prerequisites_of(current):
                if prereq in seen:
                    continue
                seen.add(prereq)
                visit(prereq)
                value = float(mastery(prereq))
                if value < threshold and (prereq, value) not in out:
                    out.append((prereq, value))

        visit(kp)
        return out

    def is_unlocked(self, kp: str, mastery: Callable[[str], float], threshold: float) -> bool:
        """No unmet prerequisite blocks ``kp``."""
        return not self.blocking_chain(kp, mastery, threshold)

    def statistics(
        self,
        mastery: Callable[[str], float],
        threshold: float,
        due_kps: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Counts for the status panel: unlocked / blocked / due review."""
        due = set(due_kps)
        unlocked: list[str] = []
        blocked: list[str] = []
        for kp in self.topological_order():
            if self.is_unlocked(kp, mastery, threshold):
                unlocked.append(kp)
            else:
                blocked.append(kp)
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "unlocked": len(unlocked),
            "blocked": len(blocked),
            "due": len(due),
            "unlocked_ids": unlocked,
            "blocked_ids": blocked,
        }

    # -------------------------------------------------------------- edits
    def apply_edits(self, edits: Mapping[str, Any] | None) -> dict[str, int]:
        """Apply ``{"add": [[prereq, target], …], "remove": [[prereq, target], …]}``."""
        stats = {"added": 0, "removed": 0, "ignored": 0}
        if not edits or not isinstance(edits, Mapping):
            return stats
        for pair in edits.get("remove", []) or []:
            try:
                prerequisite, target = str(pair[0]), str(pair[1])
            except (IndexError, TypeError, KeyError, AttributeError):
                stats["ignored"] += 1
                continue
            if self.remove_edge(prerequisite, target):
                stats["removed"] += 1
            else:
                stats["ignored"] += 1
        for pair in edits.get("add", []) or []:
            try:
                prerequisite, target = str(pair[0]), str(pair[1])
            except (IndexError, TypeError, KeyError, AttributeError):
                stats["ignored"] += 1
                continue
            if self.add_edge(prerequisite, target, reason="user"):
                stats["added"] += 1
            else:
                stats["ignored"] += 1
        return stats

    # -------------------------------------------------------------- serialization
    def to_dict(self) -> dict[str, Any]:
        """JSON view."""
        return {
            "version": GRAPH_VERSION,
            "nodes": [node.to_dict() for node in sorted(self.nodes.values(), key=lambda n: n.chapter)],
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "KnowledgeGraph":
        """Rebuild from :meth:`to_dict` output (``None`` → empty graph).

        Unlike a tolerant preview parser, a persisted graph is either used in
        full or rejected: a single malformed node/edge, an edge pointing at a
        missing node, or any cycle raises :class:`ValueError` so
        :func:`load_or_build` can rebuild from study state instead of silently
        planning on a half-graph (where a missing prerequisite looked unlocked).
        """
        if not data:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("knowledge graph must be an object")
        raw_nodes = data.get("nodes") or []
        raw_edges = data.get("edges") or []
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise ValueError("knowledge graph nodes/edges must be lists")
        nodes: dict[str, GraphNode] = {}
        for item in raw_nodes:
            if not isinstance(item, Mapping):
                raise ValueError("graph node must be an object")
            node = GraphNode.from_dict(item)
            if node.id in nodes:
                raise ValueError("duplicate graph node %r" % node.id)
            nodes[node.id] = node
        edges: list[GraphEdge] = []
        for item in raw_edges:
            if not isinstance(item, Mapping):
                raise ValueError("graph edge must be an object")
            edge = GraphEdge.from_dict(item)
            if not edge.prerequisite or not edge.target:
                raise ValueError("graph edge has an empty endpoint")
            if edge.prerequisite not in nodes or edge.target not in nodes:
                raise ValueError(
                    "graph edge %s -> %s references a missing node"
                    % (edge.prerequisite, edge.target)
                )
            edges.append(edge)
        graph = cls(nodes=nodes)
        seen: set[tuple[str, str]] = set()
        for edge in edges:
            key = (edge.prerequisite, edge.target)
            if key in seen:
                continue
            seen.add(key)
            if not graph.add_edge(edge.prerequisite, edge.target, edge.reason, edge.evidence):
                raise ValueError("graph edge %s -> %s is cyclic" % key)
        return graph

    def save(self, path: str) -> None:
        """Atomically write the graph (with a pre-write snapshot)."""
        atomic_write_json(
            path,
            self.to_dict(),
            snapshot_root=os.path.join(os.path.dirname(os.path.abspath(path)), ".snapshots"),
        )

    @classmethod
    def load(cls, path: str) -> "KnowledgeGraph | None":
        """Load a graph; a missing/corrupt file returns None (caller rebuilds)."""
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls.from_dict(data)
        except (ValueError, TypeError, AttributeError, KeyError):
            return None


# ---------------------------------------------------------------- builders
def build_graph(chapters: Iterable[Any]) -> KnowledgeGraph:
    """Build the chapter graph from ``Chapter`` objects or ``chapters.json`` dicts."""
    items: list[Any] = list(chapters)
    graph = KnowledgeGraph()
    text_by_number: dict[int, str] = {}
    for item in items:
        if isinstance(item, Mapping):
            number = int(item.get("n", item.get("number", 0)))
            title = str(item.get("title", ""))
            sources = [str(value) for value in item.get("sources", [])]
            blocks = [(str(block.get("file", "")), int(block.get("page", 0)), str(block.get("text", "")))
                      for block in item.get("blocks", [])]
        else:
            number = int(item.number)
            title = str(item.title)
            sources = [str(value) for value in item.sources]
            blocks = [(str(file), int(page), str(text)) for file, page, text in item.blocks]
        text = "\n\n".join(block[2] for block in blocks if block[2])
        text_by_number[number] = "\n\n".join(filter(None, (text_by_number.get(number, ""), text)))
        graph.add_node(GraphNode(
            id=kp_for_chapter(number),
            title=title,
            chapter=number,
            sources=sources,
            concepts=_concepts(text),
        ))

    numbers = sorted(node.chapter for node in graph.nodes.values())
    for earlier, later in zip(numbers, numbers[1:]):
        graph.add_edge(kp_for_chapter(earlier), kp_for_chapter(later), reason="sequence")

    # Backward references only (forward pointers would create cycles).
    for node in sorted(graph.nodes.values(), key=lambda item: item.chapter):
        text = text_by_number.get(node.chapter, "")
        if not text:
            continue
        for pattern in (_REF_ZH, _REF_EN, _REF_SECTION, _REF_SEE):
            for match in pattern.finditer(text):
                try:
                    referenced = int(match.group(1))
                except (ValueError, IndexError):
                    continue
                if referenced >= node.chapter or referenced not in numbers:
                    continue
                graph.add_edge(
                    kp_for_chapter(referenced),
                    node.id,
                    reason="reference",
                    evidence=_evidence(text, match),
                )
    return graph


def graph_from_state(state: Mapping[str, Any]) -> KnowledgeGraph:
    """Rebuild a minimal chapter graph from study state (fallback path)."""
    raw = state.get("chapters") or []
    chapters = [chapter for chapter in raw if isinstance(chapter, Mapping)] \
        if isinstance(raw, (list, tuple)) else []
    graph = build_graph([{
        "n": chapter.get("n", 0),
        "title": chapter.get("title", ""),
        "sources": chapter.get("sources", []),
        "blocks": [],
    } for chapter in chapters])
    graph.apply_edits(state.get("graph_edits"))
    return graph


def load_or_build(workspace: str, state: Mapping[str, Any]) -> KnowledgeGraph:
    """Load ``knowledge_graph.json`` or fall back to a graph built from state.

    A file that parses but is semantically empty (no nodes) is treated like a
    corrupt file when study state knows chapters, so a truncated graph cannot
    silently unlock every prerequisite.
    """
    graph = KnowledgeGraph.load(os.path.join(workspace, GRAPH_FILE))
    if graph is None or (not graph.nodes and state.get("chapters")):
        graph = graph_from_state(state)
    graph.apply_edits(state.get("graph_edits"))
    return graph
