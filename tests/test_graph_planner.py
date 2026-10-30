# -*- coding: utf-8 -*-
"""P1 knowledge graph and planner: prerequisites, due priority, cram ordering."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from examprep_agent.core.session import Session
from examprep_agent.learning.graph import (
    GraphEdge,
    GraphNode,
    KnowledgeGraph,
    build_graph,
    kp_for_chapter,
    load_or_build,
)
from examprep_agent.learning.modes import CramPolicy, LongTermPolicy
from examprep_agent.learning.planner import (
    due_reviews,
    order_chapters,
    recommend,
    unmet_prerequisites,
)
from examprep_agent.learning.scheduler import Rating
from examprep_agent.tools import state as st


def chapter(n: int, title: str = "", text: str = "") -> dict:
    return {
        "n": n,
        "title": title or ("Chapter %d" % n),
        "sources": ["ch%d.md" % n],
        "parts": 1,
        "questions": 0,
        "blocks": [{"file": "ch%d.md" % n, "page": 1, "text": text or ("# %s\ncontent" % title)}],
    }


def make_session(tmp_path: Path, *, current: int = 1, mode: str = "longterm",
                 chapters: list[dict] | None = None) -> Session:
    ws = tmp_path / "ws"
    ws.mkdir()
    data = chapters or [chapter(1, "基础"), chapter(2, "进阶"), chapter(3, "高级")]
    state = st.new_state("course", "zh", None, str(tmp_path / "materials"), data, 3000)
    state["current"] = current
    st.save(str(ws), state)
    session = Session.load(str(ws))
    session.mode = mode
    session.policy = CramPolicy() if mode == "cram" else LongTermPolicy()
    return session


class TestKnowledgeGraph:
    def test_builds_sequence_and_backward_reference_edges(self) -> None:
        graph = build_graph([
            chapter(1, "基础", "# 基础\n定义与符号。"),
            chapter(2, "进阶", "# 进阶\n参见第1章的定义。"),
            chapter(3, "高级", "# 高级\n见 Chapter 2，并回顾 §1。"),
        ])
        assert set(graph.nodes) == {"ch1", "ch2", "ch3"}
        reasons = {(edge.prerequisite, edge.target): edge.reason for edge in graph.edges}
        assert reasons[("ch1", "ch2")] == "sequence"
        assert reasons[("ch2", "ch3")] == "sequence"
        # ch3's "§1" adds a direct reference edge from ch1.
        assert reasons[("ch1", "ch3")] == "reference"
        assert graph.nodes["ch1"].concepts == ["基础"]

    def test_forward_references_are_ignored_to_avoid_cycles(self) -> None:
        graph = build_graph([
            chapter(1, "基础", "# 基础\n这里先提一下第 3 章。"),
            chapter(2, "进阶", "# 进阶\ntext"),
            chapter(3, "高级", "# 高级\ntext"),
        ])
        assert not any(edge.prerequisite == "ch3" for edge in graph.edges)

    def test_user_edits_apply_and_cycles_are_rejected(self) -> None:
        graph = build_graph([chapter(1), chapter(2), chapter(3)])
        stats = graph.apply_edits({"remove": [["ch2", "ch3"]], "add": [["ch1", "ch3"]]})
        assert stats["removed"] == 1 and stats["added"] == 1
        assert not any(edge.prerequisite == "ch2" and edge.target == "ch3" for edge in graph.edges)
        # ch1 -> ch2 -> ch3 + ch3 -> ch1 would be a cycle; ch3 -> ch2 is also
        # cyclic because ch2 already depends on ch1 which depends on ch3? No:
        # only the reverse of an existing path is rejected.
        assert graph.add_edge("ch3", "ch1") is False
        assert graph.remove_edge("ch1", "ch3") is True

    def test_blocking_chain_is_deepest_first_and_transitive(self) -> None:
        graph = KnowledgeGraph(
            nodes=[GraphNode("ch1", "a", 1), GraphNode("ch2", "b", 2), GraphNode("ch3", "c", 3)],
            edges=[GraphEdge("ch1", "ch2"), GraphEdge("ch2", "ch3")],
        )
        chain = graph.blocking_chain("ch3", lambda kp: 0.0, 0.8)
        assert [kp for kp, _ in chain] == ["ch1", "ch2"]
        assert graph.is_unlocked("ch1", lambda kp: 0.0, 0.8) is True
        assert graph.is_unlocked("ch2", lambda kp: 0.0, 0.8) is False
        # A mastered direct prerequisite does not hide a deeper gap.
        assert [kp for kp, _ in graph.blocking_chain("ch3", lambda kp: 1.0 if kp == "ch2" else 0.0, 0.8)] == ["ch1"]

    def test_corrupt_graph_file_falls_back_to_state(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        data = [chapter(1), chapter(2)]
        state = st.new_state("c", "zh", None, str(tmp_path / "materials"), data, 3000)
        (ws / "knowledge_graph.json").write_text("{not json", encoding="utf-8")
        graph = load_or_build(str(ws), state)
        assert set(graph.nodes) == {"ch1", "ch2"}
        assert any(edge.prerequisite == "ch1" and edge.target == "ch2" for edge in graph.edges)

    def test_cyclic_graph_file_is_rejected_and_state_is_rebuilt(self, tmp_path: Path) -> None:
        """I3: a hand-edited cycle must not survive loading."""
        ws = tmp_path / "ws"
        ws.mkdir()
        data = [chapter(1), chapter(2)]
        state = st.new_state("c", "zh", None, str(tmp_path / "materials"), data, 3000)
        payload = {
            "nodes": [
                {"id": "ch1", "title": "a", "chapter": 1},
                {"id": "ch2", "title": "b", "chapter": 2},
            ],
            "edges": [
                {"from": "ch1", "to": "ch2"},
                {"from": "ch2", "to": "ch1"},
            ],
        }
        with pytest.raises(ValueError):
            KnowledgeGraph.from_dict(payload)
        (ws / "knowledge_graph.json").write_text(json.dumps(payload), encoding="utf-8")

        graph = load_or_build(str(ws), state)
        assert not graph.has_cycle()
        assert set(graph.nodes) == {"ch1", "ch2"}
        # The target is never reported as its own prerequisite, even if a
        # caller force-appends a cycle after construction.
        graph.edges.append(GraphEdge("ch2", "ch1"))
        assert "ch1" not in [kp for kp, _ in graph.blocking_chain("ch1", lambda kp: 0.0, 0.8)]
        with pytest.raises(ValueError):
            graph.topological_order()

    def test_non_chapter_node_ids_are_not_silently_dropped(self, tmp_path: Path) -> None:
        """I4: eager default evaluation used to drop any non-``chN`` node."""
        node = GraphNode.from_dict({"id": "node1", "title": "x", "chapter": 1})
        assert node.chapter == 1
        with pytest.raises(ValueError):
            GraphNode.from_dict({"id": "node1", "title": "x"})

        ws = tmp_path / "ws"
        ws.mkdir()
        state = st.new_state("c", "zh", None, str(tmp_path / "materials"),
                             [chapter(1), chapter(2)], 3000)
        payload = {
            "nodes": [{"id": "node1", "title": "x", "chapter": 1}],
            "edges": [{"from": "node1", "to": "ch1"}],
        }
        (ws / "knowledge_graph.json").write_text(json.dumps(payload), encoding="utf-8")
        graph = load_or_build(str(ws), state)
        # Rejected file → state fallback, which has the real chapter ids.
        assert set(graph.nodes) == {"ch1", "ch2"}

    def test_missing_target_is_rebuilt_from_state_not_treated_as_unlocked(
            self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=2)
        session.graph = KnowledgeGraph()  # simulate a truncated rebuild
        session.learner.states["ch1"].p_known = 0.0
        gaps = unmet_prerequisites(session, 2)
        assert [gap.kp for gap in gaps] == ["ch1"]
        assert "ch2" in session.graph.nodes

    def test_apply_edits_ignores_malformed_entries(self) -> None:
        graph = build_graph([chapter(1), chapter(2)])
        stats = graph.apply_edits({"add": [{"x": 1}, "ab"], "remove": [42]})
        assert stats["ignored"] >= 2
        assert isinstance(graph.apply_edits(["not", "a", "mapping"]), dict)


class TestPlanner:
    def test_due_reviews_outrank_prerequisites_and_new_material(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=2)
        session.graph = KnowledgeGraph(
            nodes=[GraphNode("ch1", "a", 1), GraphNode("ch2", "b", 2)],
            edges=[GraphEdge("ch1", "ch2")],
        )
        session.learner.states["ch1"].p_known = 0.0
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        session.scheduler.review("q1", Rating.GOOD, today=date(2026, 1, 1), kp="ch1")
        session.scheduler.cards["q1"].due = yesterday
        bank = [{"id": "q1", "chapter": 1, "question": "q?", "source": {"kind": "exam"}}]
        rec = recommend(session, bank)
        assert rec.kind == "due_review"
        assert rec.qid == "q1"
        assert rec.due[0].qid == "q1"

    def test_unmet_prerequisite_redirects_to_the_prerequisite(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=2)
        session.graph = KnowledgeGraph(
            nodes=[GraphNode("ch1", "基础", 1), GraphNode("ch2", "进阶", 2)],
            edges=[GraphEdge("ch1", "ch2", "sequence")],
        )
        session.learner.states["ch1"].p_known = 0.0
        rec = recommend(session, [])
        assert rec.kind == "prereq_blocked"
        assert rec.chapter == 1
        assert rec.params["target"] == 2
        gaps = unmet_prerequisites(session, 2)
        assert gaps and gaps[0].kp == "ch1"

    def test_daily_new_limit_stops_new_chapters(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=1)
        session.policy = replace(LongTermPolicy(), daily_new_limit=0)
        rec = recommend(session, [])
        assert rec.kind == "daily_limit"
        # Already-started material may continue even at the limit.
        session.state["chapters"][0]["part"] = 1
        assert recommend(session, []).kind == "continue"

    def test_cram_orders_by_urgency_frequency_and_unmastered(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=1, mode="cram")
        session.policy = CramPolicy()
        session.learner.states["ch2"].p_known = 0.9
        session.learner.states["ch3"].p_known = 0.1
        bank = (
            [{"id": "q%d" % i, "chapter": 2, "source": {"kind": "exam"}} for i in range(5)]
            + [{"id": "q9", "chapter": 3, "source": {"kind": "exam"}}]
        )
        todo = session.state["chapters"]
        ordered = order_chapters(session, todo, bank, days_left=5)
        assert ordered[0]["n"] == 3
        assert ordered[-1]["n"] == 1

    def test_cram_order_changes_with_the_exam_runway(self, tmp_path: Path) -> None:
        """I5: the exam date must affect chapter *order*, not just a factor."""
        session = make_session(tmp_path, current=1, mode="cram",
                               chapters=[chapter(1), chapter(2)])
        session.policy = CramPolicy()
        session.learner.states["ch1"].p_known = 0.5
        session.learner.states["ch2"].p_known = 0.1
        bank = (
            [{"id": "a%d" % i, "chapter": 1, "source": {"kind": "other"}} for i in range(40)]
            + [{"id": "e%d" % i, "chapter": 2, "source": {"kind": "exam"}} for i in range(4)]
        )
        todo = session.state["chapters"]
        # Long runway: ch2's exam frequency wins.  Short runway: ch1's larger
        # practice debt becomes urgent and overtakes it.
        assert order_chapters(session, todo, bank, days_left=30)[0]["n"] == 2
        assert order_chapters(session, todo, bank, days_left=2)[0]["n"] == 1

    def test_review_first_policy_controls_the_next_action(self, tmp_path: Path) -> None:
        """I5: ``review_first`` is a real policy switch, not dead configuration."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        bank = [{"id": "q1", "chapter": 1, "question": "q?", "source": {"kind": "exam"}}]

        (tmp_path / "lt").mkdir()
        longterm = make_session(tmp_path / "lt", current=1, mode="longterm",
                                chapters=[chapter(1), chapter(2)])
        longterm.scheduler.review("q1", Rating.GOOD, today=date(2026, 1, 1), kp="ch1")
        longterm.scheduler.cards["q1"].due = yesterday
        assert recommend(longterm, bank).kind == "due_review"

        (tmp_path / "cram").mkdir()
        cram = make_session(tmp_path / "cram", current=1, mode="cram",
                            chapters=[chapter(1), chapter(2)])
        cram.scheduler.review("q1", Rating.GOOD, today=date(2026, 1, 1), kp="ch1")
        cram.scheduler.cards["q1"].due = yesterday
        assert recommend(cram, bank).kind == "new"

    def test_due_review_retrievability_is_reported(self, tmp_path: Path) -> None:
        session = make_session(tmp_path, current=1)
        session.scheduler.review("q1", Rating.GOOD, today=date(2026, 1, 1), kp="ch1")
        session.scheduler.cards["q1"].due = "2026-01-02"
        reviews = due_reviews(session, [{"id": "q1", "chapter": 1, "source": {"kind": "exam"}}],
                              today=date(2026, 1, 2))
        assert reviews and reviews[0].kp == "ch1"
        assert 0.0 < reviews[0].retrievability <= 1.0
