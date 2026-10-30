# -*- coding: utf-8 -*-
"""MCP server smoke tests (skipped when the ``mcp`` extra is absent)."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from examprep_agent.core.provider import FakeProvider
from examprep_agent.interfaces import mcp_available
from examprep_agent.interfaces import mcp_server
from examprep_agent.service import StudyService, SetupRequest
from examprep_agent.service.errors import ExtrasMissing

pytestmark = pytest.mark.skipif(not mcp_available(), reason="mcp extra not installed")

TOOL_NAMES = {
    "search_materials", "get_status", "get_plan", "get_next",
    "ask", "start_quiz", "submit_answer",
}


@pytest.fixture
def workspace(tmp_path: Path, sample_course: Path) -> Path:
    materials = tmp_path / "materials"
    shutil.copytree(sample_course, materials)
    ws = materials / "exam-cram"
    StudyService(pointer=str(tmp_path / "pointer")).setup(SetupRequest(
        materials=str(materials), workspace=str(ws), days=30))
    return ws


def _call(server, name: str, arguments: dict) -> dict:
    """Call one tool through the real MCP client handshake (in-memory transport)."""
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def run() -> dict:
        low = mcp_server.lowlevel(server)
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            async with ClientSession(*client_streams) as session:
                task = asyncio.create_task(
                    low.run(*server_streams, low.create_initialization_options()))
                try:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert names == TOOL_NAMES  # exact surface, no accidental extras
                    response = await session.call_tool(name, arguments)
                    text = response.content[0].text
                    return json.loads(text)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    return asyncio.run(run())


def test_tools_list_and_one_read_only_call(workspace: Path, tmp_path: Path) -> None:
    service = StudyService(pointer=str(tmp_path / "pointer"))
    provider = FakeProvider(["A stack is LIFO [第2章_栈和队列.md p.1]."])
    server = mcp_server.build_server(service=service, provider=provider)

    status = _call(server, "get_status", {"workspace": str(workspace)})
    assert status["ok"] is True and status["total"] == 3
    assert status["exit_code"] == 0

    ask = _call(server, "ask", {"workspace": str(workspace), "query": "栈 LIFO"})
    assert ask["ok"] is True and ask["found"] is True
    assert ask["summary"] and "LIFO" in ask["summary"]
    # Both material and sub-agent summaries leave the server fenced, never raw.
    assert ask["summary"].startswith("<<<MATERIAL ")
    assert ask["hits"] and ask["hits"][0]["text"].startswith("<<<MATERIAL ")
    assert provider.calls, "the FakeProvider must drive the read-only ask summary"


def test_write_tool_goes_through_the_service_and_audit(workspace: Path, tmp_path: Path) -> None:
    service = StudyService(pointer=str(tmp_path / "pointer"))
    server = mcp_server.build_server(service=service)
    quiz = _call(server, "start_quiz", {"workspace": str(workspace), "n": 1})
    assert quiz["ok"] is True and quiz["questions"]
    qid = quiz["questions"][0]["id"]

    submitted = _call(server, "submit_answer", {
        "workspace": str(workspace), "qid": qid, "result": "right",
    })
    assert submitted["ok"] is True and submitted["qid"] == qid
    assert submitted["exit_code"] == 0

    state = json.loads((workspace / "study_state.json").read_text(encoding="utf-8"))
    assert any(entry["qid"] == qid for entry in state["history"])
    from examprep_agent.security import audit_path, read_jsonl

    events = read_jsonl(audit_path(str(workspace)))
    assert any(event.get("event") == "permission_decision"
               and event.get("tool") == "mcp.submit_answer" for event in events)


def test_out_of_range_numbers_return_structured_errors(workspace: Path, tmp_path: Path) -> None:
    """S3: MCP never turns a negative n/k into Python-slice semantics."""
    service = StudyService(pointer=str(tmp_path / "pointer"))
    server = mcp_server.build_server(service=service)

    negative_n = _call(server, "start_quiz", {"workspace": str(workspace), "n": -1})
    assert negative_n["ok"] is False
    assert negative_n["code"] == "validation_error" and negative_n["exit_code"] == 2

    negative_k = _call(server, "search_materials", {
        "workspace": str(workspace), "query": "栈", "k": -1})
    assert negative_k["ok"] is True and negative_k["found"] is True
    assert len(negative_k["hits"]) == 1  # clamped to 1, not "everything but the last"


def test_python_m_entrypoint_starts_a_real_stdio_process(tmp_path: Path) -> None:
    """I8: ``python -m examprep_agent.interfaces.mcp_server`` must handshake."""
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    repo_root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "examprep_agent.interfaces.mcp_server"],
        env={**os.environ, "EXAMPREP_CONFIG_DIR": str(tmp_path / "config"),
             "EXAMPREP_OFFLINE": "1"},
        cwd=str(repo_root),
    )

    async def run() -> set[str]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)
                tools = await asyncio.wait_for(session.list_tools(), timeout=30)
                return {tool.name for tool in tools.tools}

    assert asyncio.run(run()) == TOOL_NAMES


def test_import_without_mcp_gives_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "MCP_AVAILABLE", False)
    with pytest.raises(ExtrasMissing) as info:
        mcp_server.build_server()
    assert "examprep-agent[mcp]" in str(info.value)
