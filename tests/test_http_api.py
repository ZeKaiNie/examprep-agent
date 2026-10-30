# -*- coding: utf-8 -*-
"""FastAPI transport smoke tests (skipped when the ``serve`` extra is absent)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from examprep_agent.interfaces import http_available
from examprep_agent.interfaces import http as http_module
from examprep_agent.service import StudyService
from examprep_agent.service.errors import ExtrasMissing

pytestmark = pytest.mark.skipif(not http_available(), reason="serve extra not installed")


@pytest.fixture
def client(tmp_path: Path, sample_course: Path):
    from fastapi.testclient import TestClient

    materials = tmp_path / "materials"
    shutil.copytree(sample_course, materials)
    workspace = materials / "exam-cram"
    service = StudyService(pointer=str(tmp_path / "pointer"))
    app = http_module.create_app(service=service)
    return TestClient(app), materials, workspace


def test_health_and_setup_then_core_commands(client) -> None:
    client, materials, workspace = client
    health = client.get("/health")
    assert health.status_code == 200 and health.json()["ok"] is True

    response = client.post("/setup", json={
        "materials": str(materials), "workspace": str(workspace), "days": 30, "workers": 2,
    })
    assert response.status_code == 200, response.text
    setup = response.json()
    assert setup["exit_code"] == 0 and len(setup["chapters"]) == 3

    status = client.get("/status", params={"workspace": str(workspace)})
    assert status.status_code == 200
    assert status.json()["total"] == 3
    assert status.json()["mode"] == "longterm"

    plan = client.get("/plan", params={"workspace": str(workspace)})
    assert plan.status_code == 200 and plan.json()["days"]

    nxt = client.get("/next", params={"workspace": str(workspace)})
    assert nxt.status_code == 200
    assert nxt.json()["kind"] == "material" and nxt.json()["chapter"] == 1

    ask = client.get("/ask", params={"workspace": str(workspace), "q": "线性表"})
    assert ask.status_code == 200 and ask.json()["found"] is True
    assert ask.json()["hits"]

    quiz = client.post("/quiz", json={"workspace": str(workspace), "n": 1})
    assert quiz.status_code == 200
    qid = quiz.json()["questions"][0]["id"]

    answer = client.post("/answer", json={
        "workspace": str(workspace), "qid": qid, "result": "right",
    })
    assert answer.status_code == 200
    assert answer.json()["exit_code"] == 0 and answer.json()["qid"] == qid

    check = client.get("/check/%s" % qid, params={"workspace": str(workspace)})
    assert check.status_code == 200 and check.json()["question"]["id"] == qid

    costs = client.get("/costs", params={"workspace": str(workspace)})
    assert costs.status_code == 200 and costs.json()["total_tokens"] >= 0


def test_http_status_codes_match_cli_exit_codes(client) -> None:
    client, materials, workspace = client
    assert client.post("/setup", json={
        "materials": str(materials), "workspace": str(workspace), "days": 5,
    }).status_code == 200

    # A fresh app whose pointer cannot rescue an explicit nonexistent workspace.
    from fastapi.testclient import TestClient

    isolated = TestClient(http_module.create_app(
        service=StudyService(pointer=str(materials.parent / "no-pointer"))))
    missing = isolated.get("/status", params={"workspace": str(workspace / "nope")})
    assert missing.status_code == 400
    assert missing.json()["exit_code"] == 2
    assert missing.json()["code"] == "workspace_not_found"

    unknown = client.post("/answer", json={
        "workspace": str(workspace), "qid": "does-not-exist", "result": "right",
    })
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "unknown_question"

    no_match = client.get("/ask", params={"workspace": str(workspace), "q": "量子纠缠"})
    assert no_match.status_code == 404
    assert no_match.json()["exit_code"] == 4


def test_every_http_write_path_is_audited(client) -> None:
    """I2: HTTP does not get to skip the gate/audit that CLI and MCP use."""
    client, materials, workspace = client
    assert client.post("/setup", json={
        "materials": str(materials), "workspace": str(workspace), "days": 30,
    }).status_code == 200
    assert client.post("/note", json={
        "workspace": str(workspace), "text": "note", "type": "note",
    }).status_code == 200
    assert client.post("/done", json={
        "workspace": str(workspace), "chapter": 1, "force": True,
    }).status_code == 200
    assert client.post("/cheatsheet", json={"workspace": str(workspace)}).status_code == 200
    assert client.post("/graph", json={
        "workspace": str(workspace), "remove_prereq": ["ch1:ch2"],
    }).status_code == 200
    assert client.get("/permissions", params={"workspace": str(workspace)}).status_code == 200
    assert client.get("/mistakes", params={"workspace": str(workspace)}).status_code == 200

    from examprep_agent.security import audit_path, read_jsonl

    decisions = [event for event in read_jsonl(audit_path(str(workspace)))
                 if event.get("event") == "permission_decision"]
    tools = {event.get("tool") for event in decisions}
    assert {"http.setup", "http.note", "http.done", "http.cheatsheet",
            "http.graph", "http.permissions", "http.mistakes"} <= tools


def test_ask_chars_is_applied_by_the_service(client) -> None:
    """S1: HTTP ``chars`` must actually truncate, not just be accepted."""
    client, materials, workspace = client
    client.post("/setup", json={
        "materials": str(materials), "workspace": str(workspace), "days": 30,
    })
    short = client.get("/ask", params={
        "workspace": str(workspace), "q": "线性表", "chars": 10,
    })
    assert short.status_code == 200
    assert short.json()["hits"]
    assert all(len(hit["text"]) <= 10 for hit in short.json()["hits"])
    full = client.get("/ask", params={"workspace": str(workspace), "q": "线性表"})
    assert any(len(hit["text"]) > 10 for hit in full.json()["hits"])


def test_validation_errors_use_the_shared_error_model(client) -> None:
    """S2: pydantic failures are 400/exit 2 with ``code``, not FastAPI's 422."""
    client, materials, workspace = client
    client.post("/setup", json={
        "materials": str(materials), "workspace": str(workspace), "days": 30,
    })
    response = client.post("/answer", json={
        "workspace": str(workspace), "qid": "q006", "result": "bogus",
    })
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["code"] == "validation_error" and payload["exit_code"] == 2

    negative = client.post("/quiz", json={"workspace": str(workspace), "n": -1})
    assert negative.status_code == 400
    assert negative.json()["code"] == "validation_error"


def test_serve_subcommand_starts_a_real_http_process(tmp_path: Path) -> None:
    """I8: ``examprep serve`` must boot uvicorn and answer ``/health``."""
    pytest.importorskip("uvicorn")
    import os
    import socket
    import subprocess
    import sys
    import time

    import httpx

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "EXAMPREP_CONFIG_DIR": str(tmp_path / "config"),
           "EXAMPREP_OFFLINE": "1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "examprep_agent", "serve",
         "--host", "127.0.0.1", "--port", str(port),
         "--pointer", str(tmp_path / "pointer")],
        cwd=str(repo_root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    payload = None
    error_tail = ""
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                # trust_env=False: the sandbox NO_PROXY list contains bracketed
                # IPv6 entries that httpx rejects as an invalid URL.
                response = httpx.get("http://127.0.0.1:%d/health" % port,
                                     timeout=1.0, trust_env=False)
                if response.status_code == 200:
                    payload = response.json()
                    break
            except Exception:  # server not up yet
                pass
            time.sleep(0.15)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate(timeout=10)
        error_tail = stderr.decode("utf-8", "replace")[-2000:]
    assert payload is not None, error_tail
    assert payload["ok"] is True and payload["service"] == "examprep-agent"


def test_import_without_fastapi_gives_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_module, "FASTAPI_AVAILABLE", False)
    with pytest.raises(ExtrasMissing) as info:
        http_module.create_app()
    assert "examprep-agent[serve]" in str(info.value)
