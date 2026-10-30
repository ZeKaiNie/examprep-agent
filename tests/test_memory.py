# -*- coding: utf-8 -*-
"""P1 three-layer long-term memory: persistence, recovery and normalization."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examprep_agent.core.session import Session
from examprep_agent.memory import LongTermMemory, classify_error
from examprep_agent.tools import state as st


STATE = {
    "profile": "small",
    "language": "zh",
    "mode": "longterm",
    "slice_chars": 1800,
    "history": [
        {"qid": "q1", "result": "right", "ts": "2026-10-30 10:00", "chapter": 1},
    ],
}


class TestThreeLayers:
    def test_answers_land_in_all_three_layers_and_reload(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        memory = LongTermMemory(str(ws))
        memory.sync_from_state(STATE)
        label = memory.record_answer(
            qid="q2", kp="ch1", result="wrong", mastery=0.42, chapter=1,
            note="概念混淆：把栈和队列搞混了", ts="2026-10-30 10:01",
        )
        memory.record_explanation(120)
        assert label == "concept_confusion"

        files = {path.name for path in (ws / "memory").iterdir()}
        assert {"episodic.jsonl", "semantic.json", "procedural.json"} <= files

        events = memory.episodic.events()
        assert any(event["type"] == "answer" and event["qid"] == "q2" for event in events)
        assert any(event["type"] == "explanation" for event in events)

        semantic = json.loads((ws / "memory" / "semantic.json").read_text(encoding="utf-8"))
        assert semantic["mastery_history"]["ch1"][-1]["p_known"] == 0.42
        assert semantic["error_patterns"]["concept_confusion"] == 1
        assert semantic["kp_error_patterns"]["ch1"]["concept_confusion"] == 1

        procedural = json.loads((ws / "memory" / "procedural.json").read_text(encoding="utf-8"))
        assert procedural["preferences"]["profile"] == "small"
        assert procedural["preferences"]["explanation_granularity"] == "concise"
        assert procedural["strategies"]["quiz"]["uses"] == 1
        assert procedural["strategies"]["kernel_explanation"]["uses"] == 1

        reloaded = LongTermMemory(str(ws))
        assert reloaded.semantic.summary()["answers"] == 2  # imported q1 + q2
        assert reloaded.procedural.preferences()["profile"] == "small"
        assert any(event["qid"] == "q2" for event in reloaded.episodic.events())

    def test_error_clusters_cover_the_common_labels(self) -> None:
        assert classify_error("概念混淆") == "concept_confusion"
        assert classify_error("计算失误") == "calculation_error"
        assert classify_error("太粗心看错了") == "careless"
        assert classify_error("没学过，完全忘了") == "knowledge_gap"
        assert classify_error("答错了") == "unclassified"
        assert classify_error("概念混淆", result="right") == ""

    def test_corrupt_layers_degrade_without_crashing(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        memory = LongTermMemory(str(ws))
        memory.record_answer(qid="q1", kp="ch1", result="right", mastery=0.5, chapter=1)
        (ws / "memory" / "semantic.json").write_text("{bad", encoding="utf-8")
        (ws / "memory" / "procedural.json").write_text("[not-an-object]", encoding="utf-8")
        with open(ws / "memory" / "episodic.jsonl", "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")

        recovered = LongTermMemory(str(ws))
        summary = recovered.summary()
        assert summary["semantic"]["answers"] == 0
        assert summary["procedural"]["preferences"]["profile"] == "standard"
        assert recovered.episodic.events()  # the well-formed lines survive
        assert summary["episodic"]["damaged_lines"] == 1
        # semantic + procedural quarantines + the episodic bad-line sidecar
        assert len(summary["corrupt_files"]) == 3
        assert any("episodic.jsonl.corrupt-" in path for path in summary["corrupt_files"])

    def test_history_import_is_idempotent(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        memory = LongTermMemory(str(ws))
        memory.sync_from_state(STATE)
        first = len(memory.episodic.events())
        memory.sync_from_state(STATE)
        assert len(memory.episodic.events()) == first == 1

    def test_agent_logs_are_normalized_once(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        logs = ws / "logs"
        logs.mkdir(parents=True)
        (logs / "agent-2026-10-30.jsonl").write_text(
            '{"ts": "2026-10-30T10:00:00Z", "session_id": "s", "event": "session_start", "step": 0}\n'
            '{"ts": "2026-10-30T10:00:01Z", "session_id": "s", "event": "final", "step": 1, "content": "ok"}\n',
            encoding="utf-8",
        )
        memory = LongTermMemory(str(ws))
        assert memory.ingest_agent_logs(str(logs)) == 2
        assert memory.ingest_agent_logs(str(logs)) == 0
        types = [event["type"] for event in memory.episodic.events()]
        assert "agent_session_start" in types and "agent_final" in types


def _study_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    chapters = [{"n": 1, "title": "基础", "sources": ["ch1.md"], "parts": 1, "questions": 1}]
    state = st.new_state("course", "zh", None, str(tmp_path / "materials"), chapters, 3000)
    st.save(str(ws), state)
    return ws


class TestSchemaCorruption:
    """B3: valid JSON with corrupted field types must degrade, never crash."""

    @pytest.mark.parametrize("layer,payload", [
        ("semantic.json", {"answers": "abc", "error_patterns": {}, "mastery_history": {}}),
        ("semantic.json", {"kp_error_patterns": []}),
        ("semantic.json", {"mastery_history": {"ch1": "not-a-list"}}),
        ("procedural.json", {"strategies": {"quiz": {"uses": "x"}}}),
        ("procedural.json", {"preferences": ["not", "an", "object"]}),
    ])
    def test_session_load_survives_structural_damage(
            self, tmp_path: Path, layer: str, payload: dict) -> None:
        ws = _study_workspace(tmp_path)
        memory_dir = ws / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / layer).write_text(json.dumps(payload), encoding="utf-8")

        session = Session.load(str(ws))  # must not raise ValueError/AttributeError
        assert session.memory.semantic.error_profile()["overall"]
        assert session.memory.procedural.preferences()["profile"] == "standard"
        assert list((memory_dir).glob("%s.corrupt-*" % layer))

    def test_bad_preference_values_are_coerced_to_defaults(self, tmp_path: Path) -> None:
        ws = _study_workspace(tmp_path)
        memory_dir = ws / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        (memory_dir / "procedural.json").write_text(json.dumps({
            "preferences": {"profile": [], "slice_chars": "abc", "language": None},
        }), encoding="utf-8")
        session = Session.load(str(ws))
        prefs = session.memory.procedural.preferences()
        assert prefs["profile"] == "standard"
        assert prefs["slice_chars"] == 3000
        assert prefs["language"] == "zh"

    def test_semantic_and_procedural_restore_from_snapshot(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        memory = LongTermMemory(str(ws))
        memory.record_answer(qid="q1", kp="ch1", result="right", mastery=0.5, chapter=1)
        memory.record_answer(qid="q2", kp="ch1", result="wrong", mastery=0.4, chapter=1)
        # The second write snapshotted the first version.
        (ws / "memory" / "semantic.json").write_text(
            json.dumps({"answers": "abc"}), encoding="utf-8")
        (ws / "memory" / "procedural.json").write_text(
            json.dumps({"strategies": {"quiz": {"uses": "x"}}}), encoding="utf-8")

        recovered = LongTermMemory(str(ws))
        assert recovered.semantic.summary()["answers"] == 1
        assert recovered.procedural.summary()["preferences"]["profile"] == "standard"
        strategies = recovered.procedural.summary()["strategies"]
        assert strategies and strategies[0]["uses"] == 1

    def test_answer_recording_never_breaks_the_study_loop(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ws = _study_workspace(tmp_path)
        session = Session.load(str(ws))

        def boom(**kwargs):  # noqa: ANN003
            raise ValueError("simulated schema failure")

        monkeypatch.setattr(session.memory, "record_answer", boom)
        info = session.record_answer("q1", 1, "right")  # must not raise
        assert info["kp"] == "ch1"
        events = [event for event in session.audit.tail() if event["event"] == "memory_write_error"]
        assert events and "simulated schema failure" in events[-1]["error"]

    def test_cli_status_reports_an_unreadable_workspace(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from examprep_agent import cli

        import contextlib
        import io

        ws = _study_workspace(tmp_path)
        monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))

        def broken(workspace: str) -> Session:
            raise ValueError("simulated load failure")

        monkeypatch.setattr(Session, "load", staticmethod(broken))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cli.main(["status"])
        assert rc == 2
        assert "unreadable" in err.getvalue()


class TestImportDeduplication:
    def test_same_minute_answers_are_not_collapsed(self, tmp_path: Path) -> None:
        """I8: (qid, ts) alone dropped distinct same-minute results."""
        ws = tmp_path / "ws"
        state = {
            "profile": "standard", "language": "zh", "mode": "longterm",
            "slice_chars": 3000,
            "history": [
                {"qid": "q1", "result": "wrong", "ts": "2026-10-30 10:00", "chapter": 1},
                {"qid": "q1", "result": "right", "ts": "2026-10-30 10:00", "chapter": 1},
            ],
        }
        memory = LongTermMemory(str(ws))
        memory.sync_from_state(state)
        answers = [event for event in memory.episodic.events() if event["type"] == "answer"]
        assert len(answers) == 2
        assert memory.semantic.summary()["answers"] == 2
        assert memory.semantic.summary()["correct"] == 1
