# -*- coding: utf-8 -*-
"""Service-layer tests: structured outputs, CLI parity and workspace isolation."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from examprep_agent import cli
from examprep_agent.service import (
    StudyService,
    SetupRequest,
    AnswerRequest,
    QuizRequest,
    GradeRequest,
    CheatsheetRequest,
    DoneRequest,
    GotoRequest,
    GraphRequest,
    NoteRequest,
    PermissionRequest,
)


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def copy_course(sample_course: Path, tmp_path: Path, name: str = "materials") -> tuple[Path, Path]:
    materials = tmp_path / name
    shutil.copytree(sample_course, materials)
    return materials, materials / "exam-cram"


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pointer = tmp_path / "home" / "last_workspace"
    monkeypatch.setattr(cli, "POINTER", str(pointer))
    return pointer


def test_setup_result_is_structured(cli_env: Path, sample_course: Path, tmp_path: Path) -> None:
    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    result = service.setup(SetupRequest(
        materials=str(materials), workspace=str(ws), days=2, workers=2))
    assert result.exit_code == 0 and result.workspace == str(ws)
    assert result.language == "zh" and result.mode == "cram"
    assert len(result.chapters) == 3
    assert [c.n for c in result.chapters] == [1, 2, 3]
    assert result.questions == 12 and result.answered == 10
    assert result.files.get("lecture") == 3
    assert (ws / "study_state.json").exists()
    assert (ws / "logs").is_dir()  # ingestion progress JSONL


def test_cli_is_a_thin_wrapper_over_the_service(
        cli_env: Path, sample_course: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    materials, ws = copy_course(sample_course, tmp_path)
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))
    rc, out, _ = run_cli("setup", str(materials), "--days", "30")
    assert rc == 0
    service = StudyService(pointer=str(cli_env))

    status = service.status(str(ws))
    rc, out, _ = run_cli("status")
    assert rc == 0
    assert "%d/%d" % (status.done, status.total) in out
    assert status.mode in out
    assert status.course in out

    quiz = service.quiz(QuizRequest(workspace=str(ws), n=1))
    rc, out, _ = run_cli("quiz", "-n", "1")
    assert rc == 0
    assert quiz.found and quiz.questions
    for question in quiz.questions:
        assert "[%s]" % question.id in out

    plan = service.plan(str(ws))
    rc, out, _ = run_cli("plan")
    assert rc == 0
    for day in plan.days:
        assert "第 %d 天" % day.day in out
    assert plan.done == status.done and plan.total == status.total

    qid = quiz.questions[0].id
    grade = service.grade(GradeRequest(workspace=str(ws), qid=qid, reply="B"))
    rc, out, _ = run_cli("grade", qid, "B")
    assert rc == 0
    assert grade.verdict in ("right", "wrong", "unsure")
    state = json.loads((ws / "study_state.json").read_text(encoding="utf-8"))
    assert state["history"], "the service and the CLI must record into the same state"


def test_next_result_matches_cli_rendering(
        cli_env: Path, sample_course: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    result = service.setup(SetupRequest(materials=str(materials), workspace=str(ws), days=5))
    step = service.next(str(ws))
    assert step.kind == "material"
    assert step.chapter == 1 and step.part == 1 and step.parts == 1
    assert "线性表" in step.chapter_title
    assert step.part_text and step.next_command == "examprep quiz"

    # The CLI on an identically prepared copy renders the same first slice.
    materials2 = tmp_path / "course2"
    shutil.copytree(sample_course, materials2)
    ws2 = materials2 / "exam-cram"
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws2))
    run_cli("setup", str(materials2), "--days", "5")
    rc, out, _ = run_cli("next")
    assert rc == 0
    assert "(段 %d/%d)" % (step.part, step.parts) in out
    assert step.anchors[0].split(" p.")[0] in out


def test_workspace_isolation_between_service_instances(
        cli_env: Path, sample_course: Path, tmp_path: Path) -> None:
    service = StudyService(pointer=str(cli_env))
    m1, ws1 = copy_course(sample_course, tmp_path, "one")
    m2, ws2 = copy_course(sample_course, tmp_path, "two")
    service.setup(SetupRequest(materials=str(m1), workspace=str(ws1), days=2, name="one"))
    service.setup(SetupRequest(materials=str(m2), workspace=str(ws2), days=30, name="two"))
    first = service.status(str(ws1))
    second = service.status(str(ws2))
    assert first.course == "one" and second.course == "two"
    assert first.mode == "cram" and second.mode == "longterm"
    # Answering in one workspace must never show up in the other.
    service.answer(AnswerRequest(workspace=str(ws1), qid="q006", result="right"))
    assert service.status(str(ws1)).chapters[0].right == 1  # q006 belongs to ch1
    state1 = json.loads((ws1 / "study_state.json").read_text(encoding="utf-8"))
    state2 = json.loads((ws2 / "study_state.json").read_text(encoding="utf-8"))
    assert state1["history"] and not state2["history"]


def test_concurrent_answers_do_not_lose_an_update(
        cli_env: Path, sample_course: Path, tmp_path: Path) -> None:
    """I5: FastAPI runs sync endpoints in threads; the workspace must serialise."""
    import threading

    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    service.setup(SetupRequest(materials=str(materials), workspace=str(ws), days=30))
    bank = json.loads((ws / "quiz_bank.json").read_text(encoding="utf-8"))
    qids = [bank[0]["id"], bank[1]["id"]]

    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def record(qid: str) -> None:
        try:
            barrier.wait(timeout=10)
            service.answer(AnswerRequest(workspace=str(ws), qid=qid, result="right"))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(qid,)) for qid in qids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    state = json.loads((ws / "study_state.json").read_text(encoding="utf-8"))
    recorded = {entry["qid"] for entry in state["history"]}
    assert set(qids) <= recorded  # both read-modify-write cycles survived
    assert len(state["history"]) == 2


def test_every_service_entry_point_accepts_an_audit_tool() -> None:
    """I2: CLI/HTTP/MCP can name every call; no path silently skips the gate."""
    import inspect

    expected = {
        "setup", "status", "plan", "next", "ask", "quiz", "check", "grade",
        "answer", "done", "note", "mistakes", "cheatsheet", "goto", "graph",
        "permissions",
    }
    for name in expected:
        parameters = inspect.signature(getattr(StudyService, name)).parameters
        assert "audit_tool" in parameters, name


def test_service_write_paths_are_all_audited(
        cli_env: Path, sample_course: Path, tmp_path: Path) -> None:
    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    service.setup(SetupRequest(materials=str(materials), workspace=str(ws), days=30),
                  audit_tool="test.setup")
    service.plan(str(ws), audit_tool="test.plan")
    service.next(str(ws), audit_tool="test.next")
    service.answer(AnswerRequest(workspace=str(ws), qid="q006", result="right"),
                   audit_tool="test.answer")
    service.grade(GradeRequest(workspace=str(ws), qid="q006", reply="B"),
                  audit_tool="test.grade")
    service.done(DoneRequest(workspace=str(ws), chapter=1, force=True),
                 audit_tool="test.done")
    service.note(NoteRequest(workspace=str(ws), text="note", type="note"),
                 audit_tool="test.note")
    service.cheatsheet(CheatsheetRequest(workspace=str(ws)), audit_tool="test.cheatsheet")
    service.goto(GotoRequest(workspace=str(ws), chapter=2, force=True),
                 audit_tool="test.goto")
    service.graph(GraphRequest(workspace=str(ws), remove_prereq=["ch1:ch2"]),
                  audit_tool="test.graph")
    service.permissions(PermissionRequest(workspace=str(ws), action="list"),
                        audit_tool="test.permissions")

    from examprep_agent.security import audit_path, read_jsonl

    decisions = [event for event in read_jsonl(audit_path(str(ws)))
                 if event.get("event") == "permission_decision"]
    tools = {event.get("tool") for event in decisions}
    expected = {"test." + name for name in (
        "setup", "plan", "next", "answer", "grade", "done", "note",
        "cheatsheet", "goto", "graph", "permissions",
    )}
    assert expected <= tools
    access = {event["tool"]: event.get("access") for event in decisions}
    assert access["test.setup"] == "write" and access["test.answer"] == "write"
    assert access["test.permissions"] == "read"


def test_graph_edits_are_transactional_on_partial_failure(
        cli_env: Path, sample_course: Path, tmp_path: Path) -> None:
    """I6: one invalid edge must not persist the valid half."""
    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    service.setup(SetupRequest(materials=str(materials), workspace=str(ws), days=30))
    graph_before = json.loads((ws / "knowledge_graph.json").read_text(encoding="utf-8"))
    edits_before = json.loads((ws / "study_state.json").read_text(encoding="utf-8"))["graph_edits"]

    result = service.graph(GraphRequest(
        workspace=str(ws), add_prereq=["ch1:ch3", "ch2:ch99"]))
    assert result.exit_code == 2
    assert result.errors and result.changed is False
    assert result.edits == []  # no success can be reported for a rolled-back change

    assert json.loads((ws / "knowledge_graph.json").read_text(encoding="utf-8")) == graph_before
    assert json.loads((ws / "study_state.json").read_text(encoding="utf-8"))["graph_edits"] == edits_before


def test_unknown_questions_are_structured_errors(
        cli_env: Path, sample_course: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    from examprep_agent.service import ServiceError

    materials, ws = copy_course(sample_course, tmp_path)
    service = StudyService(pointer=str(cli_env))
    service.setup(SetupRequest(materials=str(materials), workspace=str(ws)))
    with pytest.raises(ServiceError) as info:
        service.answer(AnswerRequest(workspace=str(ws), qid="nope", result="right"))
    assert info.value.code == "unknown_question" and info.value.exit_code == 2
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))
    rc, out, err = run_cli("answer", "nope", "right")
    assert rc == 2
    assert "找不到题目" in out
