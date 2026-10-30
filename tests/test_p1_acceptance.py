# -*- coding: utf-8 -*-
"""P1 acceptance smoke on the bundled sample course.

Two scenarios from ``P1_BRIEF.md``:

1. setup → answer → move one card's due date into the past → ``plan`` shows that
   review card as its first item;
2. zero a prerequisite KP's mastery → ``next`` first recommends filling the
   prerequisite and explains why (and ``goto`` warns the same way).
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from examprep_agent import cli
from examprep_agent.core.session import Session
from examprep_agent.security import RiskLevel


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def state_path(course: Path) -> Path:
    return course / "exam-cram" / "study_state.json"


def read_state(course: Path) -> dict:
    return json.loads(state_path(course).read_text(encoding="utf-8"))


def write_state(course: Path, state: dict) -> None:
    state_path(course).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def line_index(lines: list[str], needle: str) -> int:
    return next(index for index, line in enumerate(lines) if needle in line)


@pytest.fixture
def workspace_env(sample_course: Path, tmp_path: Path,
                  monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at the copied sample course (offline is enforced globally)."""
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(sample_course / "exam-cram"))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    assert run_cli("setup", str(sample_course), "--days", "30")[0] == 0
    return sample_course


def test_plan_lists_a_manually_due_card_first(workspace_env: Path) -> None:
    course = workspace_env
    assert run_cli("answer", "q006", "right")[0] == 0

    state = read_state(course)
    state["reviews"]["cards"]["q006"]["due"] = "2020-01-01"
    write_state(course, state)

    rc, out, _ = run_cli("plan")
    assert rc == 0
    lines = [line for line in out.splitlines() if line.strip()]
    due_line = line_index(lines, "今日到期复习（第一优先）")
    assert "q006" in lines[due_line]
    assert "到期 2020-01-01" in lines[due_line]
    # It is genuinely the first plan item, ahead of day 1's chapters.
    assert due_line < line_index(lines, "第 1 天")


def test_next_redirects_to_the_unmet_prerequisite_with_a_reason(workspace_env: Path) -> None:
    course = workspace_env
    state = read_state(course)
    state["learner"] = {
        "params": {"p_init": 0.3, "p_learn": 0.15, "p_guess": 0.2, "p_slip": 0.1},
        "kps": {"ch1": {"p_known": 0.0, "attempts": 0, "correct": 0}},
    }
    state["reviews"] = {"cards": {}}   # isolate the prerequisite path from review debt
    state["current"] = 2
    write_state(course, state)

    rc, out, _ = run_cli("next")
    assert rc == 0
    assert "先修阻塞" in out
    assert "ch1" in out and "掌握度 0%" in out
    assert "examprep goto 1" in out

    # Start from ch1 so a refused goto would be observable.
    rc, out, _ = run_cli("goto", "1")
    assert rc == 0 and read_state(course)["current"] == 1

    # Without --force the blocking is a state decision: current stays put.
    rc, out, _ = run_cli("goto", "2")
    assert rc == 0
    assert "先修未掌握" in out
    assert "examprep goto 1" in out
    assert read_state(course)["current"] == 1

    # Force is the only path that actually switches.
    rc, out, _ = run_cli("goto", "2", "--force")
    assert rc == 0
    assert "强制跳过" in out
    assert read_state(course)["current"] == 2


def test_cli_survives_structurally_corrupt_memory(workspace_env: Path) -> None:
    """B3 repro: valid JSON with wrong field types must not break the CLI."""
    ws = workspace_env / "exam-cram"
    memory = ws / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "semantic.json").write_text(
        json.dumps({"answers": "abc", "error_patterns": {}, "mastery_history": {}}),
        encoding="utf-8")
    (memory / "procedural.json").write_text(
        json.dumps({"strategies": {"quiz": {"uses": "x"}}}), encoding="utf-8")
    (memory / "semantic.json").write_text(
        json.dumps({"kp_error_patterns": []}), encoding="utf-8")

    rc, out, err = run_cli("status")
    assert rc == 0, err
    assert "进度" in out
    rc, out, err = run_cli("answer", "q006", "right")
    assert rc == 0, err
    assert "已记录" in out


def test_status_exposes_graph_review_and_retention_views(workspace_env: Path) -> None:
    course = workspace_env
    assert run_cli("answer", "q006", "right")[0] == 0
    state = read_state(course)
    state["learner"]["kps"]["ch1"]["p_known"] = 0.0
    write_state(course, state)

    rc, out, _ = run_cli("status")
    assert rc == 0
    assert "图谱: 已解锁" in out and "阻塞" in out and "待复习" in out
    assert "复习: 今日到期" in out and "预测留存" in out
    assert "今日新学" in out


def test_permissions_cli_grant_list_revoke(workspace_env: Path, tmp_path: Path) -> None:
    ws = workspace_env / "exam-cram"
    outside = tmp_path / "granted-media"
    outside.mkdir()

    rc, out, _ = run_cli("permissions", "grant", str(outside), "--note", "course media")
    assert rc == 0
    grant_id = next(token for token in out.split() if token.startswith("g-"))
    rc, out, _ = run_cli("permissions", "list")
    assert rc == 0 and grant_id in out and str(outside) in out

    session = Session.load(str(ws))
    decision = session.gate.check("write_file", RiskLevel.L1,
                                  {"path": str(outside / "a.txt")}, access="write")
    assert decision.allowed is True

    rc, out, _ = run_cli("permissions", "revoke", grant_id)
    assert rc == 0
    rc, out, _ = run_cli("permissions", "list")
    assert grant_id not in out
    decision = Session.load(str(ws)).gate.check(
        "write_file", RiskLevel.L1, {"path": str(outside / "b.txt")}, access="write")
    assert decision.allowed is False and decision.code == "outside_write_needs_grant"


def test_permissions_grant_refuses_over_broad_targets(workspace_env: Path) -> None:
    import os

    for target in (os.path.abspath(os.sep), os.path.expanduser("~")):
        rc, out, _ = run_cli("permissions", "grant", target)
        assert rc == 2
        assert "refusing grant" in out
    rc, out, _ = run_cli("permissions", "list")
    assert rc == 0 and "没有授权" in out


def test_graph_cli_revisions_are_stored_and_reload(workspace_env: Path) -> None:
    ws = workspace_env / "exam-cram"
    rc, out, _ = run_cli("graph", "--add-prereq", "ch1:ch3")
    assert rc == 0 and "ch1 -> ch3" in out
    rc, out, _ = run_cli("graph", "--remove-prereq", "ch2:ch3")
    assert rc == 0

    state = read_state(workspace_env)
    assert ["ch1", "ch3"] in state["graph_edits"]["add"]
    assert ["ch2", "ch3"] in state["graph_edits"]["remove"]

    saved = json.loads((ws / "knowledge_graph.json").read_text(encoding="utf-8"))
    edges = {(edge["from"], edge["to"]): edge["reason"] for edge in saved["edges"]}
    assert edges[("ch1", "ch3")] == "user"
    assert ("ch2", "ch3") not in edges

    reloaded = Session.load(str(ws)).graph
    assert any(edge.prerequisite == "ch1" and edge.target == "ch3"
               for edge in reloaded.edges)
    assert not any(edge.prerequisite == "ch2" and edge.target == "ch3"
                   for edge in reloaded.edges)
