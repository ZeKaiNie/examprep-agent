# -*- coding: utf-8 -*-
"""Offline cost aggregation from the JSONL event logs."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from examprep_agent import cli
from examprep_agent.core.provider import FakeProvider
from examprep_agent.service import StudyService, SetupRequest
from examprep_agent.service.costs import aggregate_costs


def _write_log(workspace: Path, name: str, entries: list[dict]) -> None:
    logs = workspace / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / name).write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def test_aggregate_costs_by_session_and_command(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-2026-01-01.jsonl", [
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 200, "completion_tokens": 100}},
        {"event": "compaction", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 100, "completion_tokens": 50}},
        {"event": "model_response", "session_id": "s2", "command": "ask",
         "usage": {"prompt_tokens": 300, "completion_tokens": 0}},
        {"event": "tool_call", "session_id": "s1", "usage": {"prompt_tokens": 999}},
        {"event": "model_response", "session_id": "s3", "usage": None},
    ])
    result = aggregate_costs(str(ws), input_price=1.0, output_price=2.0)
    assert result.prompt_tokens == 1600
    assert result.completion_tokens == 650
    assert result.total_tokens == 2250
    expected = 1600 / 1_000_000 * 1.0 + 650 / 1_000_000 * 2.0
    assert result.cost == pytest.approx(expected)
    by_session = {row.session_id: row for row in result.sessions}
    assert by_session["s1"].prompt_tokens == 1300 and by_session["s1"].calls == 3
    assert by_session["s1"].command == "next"
    by_command = {row.command: row for row in result.commands}
    assert by_command["next"].total_tokens == 1950
    assert by_command["ask"].total_tokens == 300
    assert result.input_price_per_mtok == 1.0
    assert result.output_price_per_mtok == 2.0


def test_filter_by_command_and_missing_logs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-x.jsonl", [
        {"event": "model_response", "session_id": "a", "command": "next",
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"event": "model_response", "session_id": "b", "command": "ask",
         "usage": {"prompt_tokens": 20, "completion_tokens": 10}},
    ])
    only_next = aggregate_costs(str(ws), input_price=1.0, output_price=1.0, command="next")
    assert only_next.total_tokens == 15
    empty = aggregate_costs(str(tmp_path / "no-workspace"), input_price=1.0, output_price=1.0)
    assert empty.total_tokens == 0 and empty.cost == 0 and empty.log_files == []


def test_command_sessions_counts_distinct_sessions(tmp_path: Path) -> None:
    """S4: one session running two commands is one session per command."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-x.jsonl", [
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
        {"event": "model_response", "session_id": "s1", "command": "ask",
         "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
        {"event": "model_response", "session_id": "s2", "command": "next",
         "usage": {"prompt_tokens": 30, "completion_tokens": 3}},
    ])
    result = aggregate_costs(str(ws), input_price=1.0, output_price=1.0)
    by_command = {row.command: row for row in result.commands}
    assert by_command["next"].sessions == 2
    assert by_command["ask"].sessions == 1


def test_non_finite_usage_and_prices_are_handled(tmp_path: Path) -> None:
    """S4: ``1e999`` usage or ``inf`` price must not crash or emit invalid JSON."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-x.jsonl", [
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 1e999, "completion_tokens": 5}},
        {"event": "compaction", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": float("nan"), "completion_tokens": 0}},
        {"event": "model_response", "session_id": "s2", "command": "ask",
         "usage": {"prompt_tokens": 7, "completion_tokens": 1}},
    ])
    result = aggregate_costs(str(ws), input_price=1.0, output_price=1.0)
    assert result.total_tokens == 8  # the malformed blocks are skipped

    with pytest.raises(ValueError):
        aggregate_costs(str(ws), input_price=float("inf"), output_price=1.0)
    with pytest.raises(ValueError):
        aggregate_costs(str(ws), input_price=1.0, output_price=float("nan"))


def test_cli_costs_rejects_an_infinite_price(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-x.jsonl", [
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 10, "completion_tokens": 1}},
    ])
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "pointer"))
    error = io.StringIO()
    with contextlib.redirect_stderr(error):
        rc = cli.main(["costs", "--workspace", str(ws), "--input-price", "inf"])
    assert rc == 2
    assert "finite" in error.getvalue()


def test_cli_costs_prints_the_same_numbers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_log(ws, "agent-x.jsonl", [
        {"event": "model_response", "session_id": "s1", "command": "next",
         "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
    ])
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "pointer"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["costs", "--workspace", str(ws), "--input-price", "2.5",
                       "--output-price", "0"])
    assert rc == 0
    text = out.getvalue()
    assert "1000000" in text and "2.500000" in text

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.main(["costs", "--workspace", str(ws), "--json",
                       "--input-price", "2.5", "--output-price", "0"])
    payload = json.loads(out.getvalue())
    assert rc == 0 and payload["total_tokens"] == 1_000_000
    assert payload["cost"] == pytest.approx(2.5)


def test_agent_run_log_is_aggregated(tmp_path: Path, sample_course: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    materials = tmp_path / "materials"
    shutil.copytree(sample_course, materials)
    workspace = materials / "exam-cram"
    service = StudyService(pointer=str(tmp_path / "pointer"))
    service.setup(SetupRequest(materials=str(materials), workspace=str(workspace), days=5))
    # Drive the real explain loop with a provider that reports usage; the event
    # logger must stamp command/model and the aggregator must find it.
    provider = FakeProvider([
        {"content": "Ground explanation [第1章_线性表.md p.1].",
         "usage": {"prompt_tokens": 700, "completion_tokens": 300}},
    ])
    monkeypatch.setattr(cli, "_make_provider", lambda: provider)
    monkeypatch.setattr(cli, "POINTER", str(tmp_path / "pointer"))
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(workspace))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert cli.main(["next"]) == 0
    result = aggregate_costs(str(workspace), input_price=1.0, output_price=1.0)
    assert result.total_tokens == 1000
    assert any(row.command == "next" for row in result.commands)
