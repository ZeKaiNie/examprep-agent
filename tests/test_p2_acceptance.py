# -*- coding: utf-8 -*-
"""P2 acceptance: production agent-loop file tools are gated, audited and fenced."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from examprep_agent import cli
from examprep_agent.core.provider import FakeProvider
from examprep_agent.security import audit_path, read_jsonl
from examprep_agent.service import StudyService, SetupRequest


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def course(tmp_path: Path, sample_course: Path,
           monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    materials = tmp_path / "materials"
    shutil.copytree(sample_course, materials)
    workspace = materials / "exam-cram"
    StudyService(pointer=str(tmp_path / "pointer")).setup(SetupRequest(
        materials=str(materials), workspace=str(workspace), days=5))
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "pointer"))
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(workspace))
    return materials, workspace


def _decisions(workspace: Path) -> list[dict]:
    return [event for event in read_jsonl(audit_path(str(workspace)))
            if event.get("event") == "permission_decision"]


def test_agent_loop_reads_and_writes_through_the_gate_and_audits(
        course: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    materials, workspace = course
    material = materials / "第1章_线性表.md"
    target = workspace / "notes" / "summary.md"
    provider = FakeProvider([
        {"tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": {"path": str(material)}},
            {"id": "c2", "name": "write_file",
             "arguments": {"path": str(target), "content": "顺序表 O(1) 访问。"}},
        ]},
        "Notes written [第1章_线性表.md p.1].",
    ])
    monkeypatch.setattr(cli, "_make_provider", lambda: provider)
    rc, out, _ = run_cli("next")
    assert rc == 0
    assert "Notes written" in out

    assert target.read_text(encoding="utf-8") == "顺序表 O(1) 访问。"
    decisions = _decisions(workspace)
    allowed = [event for event in decisions if event.get("decision") == "allow"]
    read = next(event for event in allowed if event.get("tool") == "read_file")
    write = next(event for event in allowed if event.get("tool") == "write_file")
    assert read.get("access") == "read" and read.get("risk") == "L0"
    assert write.get("access") == "write" and write.get("risk") == "L1"
    assert str(target) in write.get("paths", [])

    log = sorted((workspace / "logs").glob("agent-*.jsonl"))[-1]
    results = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
               if json.loads(line)["event"] == "tool_result"]
    assert [result["tool"] for result in results] == ["read_file", "write_file"]
    assert all(result["ok"] is True for result in results)


def test_agent_loop_cannot_write_outside_the_workspace(
        course: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    materials, workspace = course
    outside = tmp_path / "escape.txt"
    provider = FakeProvider([
        {"tool_calls": [
            {"id": "c1", "name": "write_file",
             "arguments": {"path": str(outside), "content": "escaped"}},
            {"id": "c2", "name": "delete_path",
             "arguments": {"path": "study_state.json"}},
        ]},
        "Blocked.",
    ])
    monkeypatch.setattr(cli, "_make_provider", lambda: provider)
    rc, out, _ = run_cli("next")
    assert rc == 0
    assert not outside.exists()
    assert (workspace / "study_state.json").exists()

    denied = [event for event in _decisions(workspace) if event.get("decision") == "deny"]
    codes = {event.get("code") for event in denied}
    assert {"outside_write_needs_grant", "internal_path_read_only"} <= codes
    log = sorted((workspace / "logs").glob("agent-*.jsonl"))[-1]
    results = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
               if json.loads(line)["event"] == "tool_result"]
    assert [json.loads(result["observation"])["code"] for result in results] == [
        "outside_write_needs_grant", "internal_path_read_only",
    ]
