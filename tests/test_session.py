# -*- coding: utf-8 -*-
"""Session checkpoint tests: persistence, restore, branch reservation, atomic writes."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from examprep_agent.core.session import SESSION_FILE, Session
from examprep_agent.learning.scheduler import FSRS, Rating
from examprep_agent.tools import state as st


def make_state() -> dict:
    chapters = [{"n": 1, "title": "Stacks", "sources": ["a.md"], "parts": 1, "questions": 2},
                {"n": 2, "title": "Queues", "sources": ["b.md"], "parts": 1, "questions": 1}]
    return st.new_state("course", "en", None, "/materials", chapters, 3000)


def test_session_save_and_restore(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    session = Session.create(ws, make_state(), "cram")
    info = session.record_answer("q001", 1, "right")
    session.save()

    assert os.path.exists(os.path.join(ws, "study_state.json"))
    assert os.path.exists(os.path.join(ws, SESSION_FILE))
    assert not os.path.exists(os.path.join(ws, "study_state.json.tmp"))
    assert not os.path.exists(os.path.join(ws, SESSION_FILE + ".tmp"))

    loaded = Session.restore(ws)
    assert loaded.mode == "cram"
    assert loaded.policy.name == "cram"
    assert loaded.learner.mastery("ch1") == info["mastery"]
    assert loaded.scheduler.cards["q001"].reps == 1
    assert loaded.counters["answers"] == 1

    state = st.load(ws)
    assert state is not None
    assert state["mode"] == "cram"
    assert "learner" in state and "reviews" in state
    assert state["history"][0]["qid"] == "q001"


def test_context_summary_and_branch_roundtrip(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    session = Session.create(ws, make_state(), "longterm")
    session.context.summary = "The learner confused LIFO and FIFO."
    branch = session.new_branch("what-if", parent="main")
    assert branch["id"] == "b1" and branch["parent"] == "main" and branch["label"] == "what-if"
    session.save()

    loaded = Session.load(ws)
    assert loaded.context.summary == "The learner confused LIFO and FIFO."
    assert loaded.branch["id"] == "b1"
    assert loaded.branch["parent"] == "main"

    checkpoint = json.loads((tmp_path / "ws" / SESSION_FILE).read_text(encoding="utf-8"))
    assert checkpoint["mode"] == "longterm"
    assert checkpoint["branch"]["label"] == "what-if"
    assert checkpoint["counters"]["branches"] == 1


def test_corrupt_checkpoint_does_not_destroy_the_session(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    session = Session.create(ws, make_state(), "cram")
    session.save()
    (tmp_path / "ws" / SESSION_FILE).write_text("{not json", encoding="utf-8")

    loaded = Session.load(ws)
    assert loaded.mode == "cram"          # study_state still has the mode
    assert loaded.context.summary == ""   # checkpoint defaults used


def test_should_advance_follows_the_policy_gate(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    session = Session.create(ws, make_state(), "cram")
    assert not session.should_advance(1)
    for _ in range(10):
        session.learner.update("ch1", True)
        if session.should_advance(1):
            break
    assert session.should_advance(1)


def test_missing_workspace_raises(tmp_path: Path) -> None:
    import pytest
    with pytest.raises(FileNotFoundError):
        Session.load(str(tmp_path / "nowhere"))


def test_policy_target_retention_is_applied_to_fsrs(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    cram = Session.create(ws, make_state(), "cram")
    assert cram.scheduler.fsrs.desired_retention == pytest.approx(0.8)
    assert cram.record_answer("q001", 1, "right")["interval_days"] == 9

    longterm = Session.create(str(tmp_path / "ws2"), make_state(), "longterm")
    assert longterm.scheduler.fsrs.desired_retention == pytest.approx(0.9)


def test_skip_maps_to_hard_without_a_lapse(tmp_path: Path) -> None:
    ws = str(tmp_path / "ws")
    os.makedirs(ws)
    session = Session.create(ws, make_state(), "longterm")
    info = session.record_answer("q001", 1, "skip")
    card = session.scheduler.cards["q001"]
    assert card.stability == pytest.approx(FSRS().initial_stability(Rating.HARD))
    assert card.lapses == 0 and card.reps == 1
    assert info["mastery"] == pytest.approx(0.3)  # skip does not move BKT
