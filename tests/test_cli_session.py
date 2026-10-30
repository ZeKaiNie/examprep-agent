# -*- coding: utf-8 -*-
"""End-to-end CLI session on the bundled zh-data-structures sample course."""
from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest

from examprep_agent import cli
from examprep_agent.core.provider import FakeProvider


@pytest.fixture
def course(sample_course: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ws = sample_course / "exam-cram"
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    return sample_course


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def read_state(course: Path) -> dict:
    return json.loads((course / "exam-cram" / "study_state.json").read_text(encoding="utf-8"))


def test_full_session(course: Path) -> None:
    rc, out, _ = run_cli("setup", str(course), "--days", "2")
    assert rc == 0
    assert "章节 (3)" in out
    assert "题目: 12 (10 有参考答案)" in out
    assert "模式: 突击模式" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")
    ws = course / "exam-cram"
    for name in ("study_state.json", "quiz_bank.json", "index.json", "chapters.json",
                 "progress.md", "notebook.md", "session.json"):
        assert (ws / name).exists(), name
    assert len(list((ws / "chapters").glob("ch*.md"))) == 3
    state = read_state(course)
    assert state["language"] == "zh"
    assert state["mode"] == "cram"

    rc, out, _ = run_cli("status")
    assert rc == 0
    assert "进度: [░░░░░░░░░░] 0/3" in out
    assert "模式: cram" in out

    rc, out, _ = run_cli("next")
    assert rc == 0
    assert "第 1 章：线性表 (段 1/1)" in out
    assert "[第1章_线性表.md p.1]" in out
    assert "本章相关题目 (2)" in out
    assert "回退为上面的材料切片" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")
    assert read_state(course)["chapters"][0]["part"] == 1

    rc, out, _ = run_cli("next")  # chapter text exhausted → examples + hints, no advance
    assert "本章正文已讲完" in out

    rc, out, _ = run_cli("quiz", "-n", "1")
    assert rc == 0
    assert "[q006]" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("check", "q006")
    assert rc == 0
    assert "参考答案" in out
    assert "\nB" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("answer", "q006", "wrong", "--note", "混淆 n-i 与 n-i+1")
    assert rc == 0
    state = read_state(course)
    assert len(state["mistakes"]) == 1
    assert state["learner"]["kps"]["ch1"]["p_known"] < 0.3
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("quiz", "-n", "1")
    assert "[q006]" in out  # open mistake comes first
    rc, out, _ = run_cli("answer", "q006", "right")
    assert rc == 0
    assert read_state(course)["mistakes"][0]["status"] == "fixed"

    rc, out, _ = run_cli("note", "--type", "summary", "顺序表 O(1) 访问，链表 O(1) 插入")
    assert rc == 0
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("done")
    assert rc == 0
    assert "已验证" in out
    assert read_state(course)["current"] == 2

    rc, out, _ = run_cli("done", "--force")  # no right answers in ch2; gate overridden
    assert rc == 0
    assert "已讲完但未验证" in out
    assert "--force" in out

    rc, out, _ = run_cli("ask", "循环队列 判满")
    assert rc == 0
    assert "🟢 [ch2" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("ask", "量子纠缠")
    assert rc == 4
    assert "材料中没有找到" in out

    rc, out, _ = run_cli("mistakes")
    assert "没有待复习的错题" in out

    rc, out, _ = run_cli("cheatsheet")
    assert rc == 0
    sheet = (course / "exam-cram" / "cheatsheet.md").read_text(encoding="utf-8")
    assert "顺序表 O(1) 访问" in sheet
    assert "[q006]" in sheet

    rc, out, _ = run_cli("goto", "3")
    assert rc == 0
    assert read_state(course)["current"] == 3
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("plan")
    assert rc == 0
    assert "第 1 天" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")

    # re-running setup keeps progress (chapters AND BKT/FSRS); --fresh drops it
    before = read_state(course)
    assert before["learner"]["kps"]["ch1"]["p_known"] > 0.5
    assert "q006" in before["reviews"]["cards"]
    rc, out, _ = run_cli("setup", str(course))
    after = read_state(course)
    assert after["chapters"][0]["status"] == "verified"
    assert after["learner"] == before["learner"]  # BKT not silently reset (B2)
    assert after["reviews"]["cards"] == before["reviews"]["cards"]  # FSRS not silently reset
    assert "q006" in after["reviews"]["cards"]
    import datetime
    assert after["exam_date"] == (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
    rc, out, _ = run_cli("setup", str(course), "--fresh")
    fresh = read_state(course)
    assert fresh["chapters"][0]["status"] == "todo"
    assert fresh["learner"]["kps"] == {} and fresh["reviews"]["cards"] == {}


def test_cli_commands_are_audited_with_cli_tool_names(course: Path) -> None:
    """I2: the CLI names its calls just like HTTP/MCP do."""
    run_cli("setup", str(course), "--days", "5")
    run_cli("answer", "q006", "right")
    from examprep_agent.security import audit_path, read_jsonl

    decisions = [event for event in read_jsonl(audit_path(str(course / "exam-cram")))
                 if event.get("event") == "permission_decision"]
    by_tool = {event.get("tool"): event for event in decisions}
    assert by_tool["cli.setup"]["decision"] == "allow"
    assert by_tool["cli.answer"]["decision"] == "allow"
    assert by_tool["cli.answer"]["access"] == "write"


def test_setup_persists_mode_reason_like_p1(course: Path) -> None:
    """S5: ``study_state.json`` keeps the P1 ``mode_reason`` field."""
    run_cli("setup", str(course), "--days", "5")
    state = read_state(course)
    assert state["mode_reason"] == "距考试 5 天 < 21，突击模式"
    run_cli("plan", "--days", "30")
    assert read_state(course)["mode_reason"] == "距考试 30 天 ≥ 21，长期模式"


def test_quiz_rejects_a_negative_count_instead_of_slicing(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5")
    rc, out, err = run_cli("quiz", "-n", "-1")
    assert rc == 2
    assert not out.strip()  # no "all but the last question" surprise


def test_permissions_revoke_without_a_target_has_no_dangling_colon(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5")
    rc, out, _ = run_cli("permissions", "revoke")
    assert rc == 2
    assert out.strip() == "找不到该授权"


def test_next_keeps_the_blank_line_before_examples(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5")
    rc, out, _ = run_cli("next", "--no-explain")
    assert rc == 0
    assert "\n\n--- 本章相关题目 (2) ---" in out


def test_no_workspace_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(tmp_path / "nowhere"))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    monkeypatch.chdir(tmp_path)
    rc, out, err = run_cli("status")
    assert rc == 2
    assert "setup" in err


def test_slice_option_creates_more_parts(course: Path) -> None:
    run_cli("setup", str(course), "--slice", "600")
    assert read_state(course)["chapters"][0]["parts"] > 1
    rc, out, _ = run_cli("next")
    assert "(段 1/" in out
    assert "继续讲：examprep next" in out


def test_mode_selection_and_override(course: Path) -> None:
    run_cli("setup", str(course), "--days", "30")
    assert read_state(course)["mode"] == "longterm"
    run_cli("setup", str(course), "--days", "5")
    assert read_state(course)["mode"] == "cram"
    run_cli("setup", str(course), "--days", "5", "--mode", "longterm")
    assert read_state(course)["mode"] == "longterm"
    run_cli("setup", str(course), "--mode", "cram")  # no exam date, explicit override
    assert read_state(course)["mode"] == "cram"
    run_cli("setup", str(course))  # auto without a date → longterm, but the date is kept
    assert read_state(course)["mode"] == "cram"  # 5 days left from the kept date


def test_next_uses_the_kernel_loop_when_a_provider_is_configured(course: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_cli("setup", str(course), "--days", "5")
    provider = FakeProvider(["Ground explanation citing [第1章_线性表.md p.1]."])
    monkeypatch.setattr(cli, "_make_provider", lambda: provider)
    rc, out, _ = run_cli("next")
    assert rc == 0
    assert "内核讲解" in out
    assert "Ground explanation citing" in out
    logs = list((course / "exam-cram" / "logs").glob("agent-*.jsonl"))
    assert len(logs) == 1
    events = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "session_start"
    assert any(e["event"] == "model_response" for e in events)
    assert events[-1]["event"] == "final"


def test_next_without_a_provider_says_it_falls_back(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5")
    rc, out, _ = run_cli("next")
    assert "回退为上面的材料切片" in out
    # P2's parallel ingestion writes progress events under logs/, but an
    # offline ``next`` must not create an agent run log.
    assert not list((course / "exam-cram" / "logs").glob("agent-*.jsonl"))


def test_plan_date_reselects_and_persists_the_mode(course: Path) -> None:
    import datetime
    run_cli("setup", str(course))
    assert read_state(course)["mode"] == "longterm"
    exam = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
    rc, out, _ = run_cli("plan", "--date", exam)
    assert rc == 0
    assert read_state(course)["exam_date"] == exam
    assert read_state(course)["mode"] == "cram"
    rc, out, _ = run_cli("status")
    assert "模式: cram" in out
    # Re-selecting a mode through plan re-wires the FSRS retention target too.
    assert read_state(course)["reviews"]["desired_retention"] == pytest.approx(0.8)


def test_plan_without_exam_date(course: Path) -> None:
    run_cli("setup", str(course))
    rc, out, _ = run_cli("plan")
    assert rc == 0
    assert "没有设置考试日期" in out


def test_setup_imports_a_prebuilt_quiz_bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    course = tmp_path / "course"
    (course / "references" / "wiki").mkdir(parents=True)
    (course / "references" / "wiki" / "ch1.md").write_text("# 第 1 章：栈\n\n栈是后进先出（LIFO）。\n", encoding="utf-8")
    bank = [{
        "id": "mc_q1", "chapter": 1, "type": "choice", "question": "栈的存取顺序？",
        "options": ["A. LIFO", "B. FIFO"], "answer": "A", "source": "teacher",
    }]
    (course / "references" / "quiz_bank.json").write_text(json.dumps(bank, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(course / "exam-cram"))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    rc, out, _ = run_cli("setup", str(course), "--lang", "zh")
    assert rc == 0
    assert "题目: 1 (1 有参考答案)" in out
    rc, out, _ = run_cli("quiz", "-n", "1")
    assert rc == 0
    assert "[mc_q1]" in out


def test_doctor_runs_and_reports_environment() -> None:
    rc, out, _ = run_cli("doctor")
    assert rc == 0
    assert "examprep-agent" in out
    assert "python" in out
    assert "pydantic" in out
    assert "workspace:" in out
    assert "provider:" in out


def test_every_study_command_ends_with_a_pointer(course: Path) -> None:
    run_cli("setup", str(course), "--days", "3")
    for argv in (("next",), ("quiz", "-n", "1"), ("check", "q006"), ("answer", "q006", "wrong"),
                 ("note", "--type", "summary", "x"), ("done",), ("status",), ("ask", "循环队列"),
                 ("plan",), ("mistakes",), ("cheatsheet",), ("goto", "2")):
        rc, out, _ = run_cli(*argv)
        assert rc == 0, "%s exited %s" % (argv, rc)
        assert out.strip(), "%s printed nothing" % (argv,)
        last = out.rstrip().splitlines()[-1]
        assert last.startswith("📍"), "%s → %r" % (argv, last)
        assert "→ examprep " in last, argv


def test_workspace_inside_materials_is_not_reingested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     sample_course: Path) -> None:
    """A custom --workspace under the materials folder must not become course data."""
    ws = sample_course / "custom_ws"
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    for attempt in range(3):
        rc, out, _ = run_cli("setup", str(sample_course), "--lang", "zh", "--workspace", str(ws))
        assert rc == 0, out
        state = json.loads((ws / "study_state.json").read_text(encoding="utf-8"))
        bank = json.loads((ws / "quiz_bank.json").read_text(encoding="utf-8"))
        assert len(state["chapters"]) == 3, (attempt, [c["title"] for c in state["chapters"]])
        assert [c["questions"] for c in state["chapters"]] == [2, 6, 4], attempt
        assert len(bank) == 12, attempt


def test_quiz_without_questions_still_prints_a_pointer(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    course = tmp_path / "course"
    course.mkdir()
    (course / "lec1.md").write_text("# Lecture 1: Intro\n\n" + "course text " * 100, encoding="utf-8")
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(course / "exam-cram"))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "home" / "last_workspace"))
    rc, out, _ = run_cli("setup", str(course), "--lang", "en")
    assert rc == 0
    rc, out, _ = run_cli("quiz")
    assert rc == 3
    lines = out.rstrip().splitlines()
    assert lines[-1].startswith("📍"), lines[-1]
    assert "→ examprep next" in lines[-1]


def test_profile_small_keeps_the_skill_quiz_size(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5", "--profile", "small")  # cram policy
    rc, out, _ = run_cli("quiz")
    assert rc == 0
    assert len(re.findall(r"^\[q[0-9]+\]", out, re.M)) == 1  # small = 1, not cram's 5
    run_cli("setup", str(course), "--days", "30", "--profile", "large")  # longterm caps at 3
    rc, out, _ = run_cli("quiz", "--all")
    assert rc == 0
    assert len(re.findall(r"^\[q[0-9]+\]", out, re.M)) == 3


def test_cram_mode_wires_target_retention_into_fsrs(course: Path) -> None:
    import datetime

    run_cli("setup", str(course), "--days", "5")
    state = read_state(course)
    assert state["policy"]["target_retention"] == 0.8
    rc, out, _ = run_cli("answer", "q006", "right")
    assert rc == 0
    state = read_state(course)
    assert state["reviews"]["desired_retention"] == pytest.approx(0.8)
    # First Good review: interval is re-solved at 0.8, not the 0.9 default.
    due = datetime.date.fromisoformat(state["reviews"]["cards"]["q006"]["due"])
    assert due == datetime.date.today() + datetime.timedelta(days=9)


def test_done_mastery_gate_blocks_until_forced(course: Path) -> None:
    run_cli("setup", str(course), "--days", "5")
    rc, out, _ = run_cli("done")
    assert rc == 0
    state = read_state(course)
    assert state["chapters"][0]["status"] == "todo"  # not advanced below the gate
    assert "done --force" in out
    assert out.rstrip().splitlines()[-1].startswith("📍")

    rc, out, _ = run_cli("done", "--force")
    assert rc == 0
    state = read_state(course)
    assert state["chapters"][0]["status"] == "done"
    assert state["current"] == 2
    assert "--force" in out
