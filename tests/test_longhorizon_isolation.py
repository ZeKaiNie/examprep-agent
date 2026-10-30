# -*- coding: utf-8 -*-
"""Integrity tests for the LongHorizon simulated learner (P3 review B2).

The hidden learner now lives in a separate process.  These tests do not trust
that design statement: they walk the object graph reachable from the agent
façade, inspect closures, and actually send read/tamper requests through the
agent pipe, asserting that every attempt is rejected and the true state is
unchanged.  ``hidden_state_isolated`` is recomputed by the same
``isolation_report`` the benchmark uses.
"""
from __future__ import annotations

import gc
import inspect
import json
from pathlib import Path

from benchmarks.common import tolerance_equal
from benchmarks.longhorizon import runner
from benchmarks.longhorizon.remote import (
    HIDDEN_ATTRIBUTES,
    isolation_report,
    start_hidden,
)
from benchmarks.longhorizon.simulator import HiddenLearner, SimulationParams

QUESTIONS = [
    {"id": "q1", "chapter": 1},
    {"id": "q2", "chapter": 1},
    {"id": "q3", "chapter": 2},
]


def test_pretest_is_zero_for_a_fresh_simulator() -> None:
    hidden = HiddenLearner(QUESTIONS, seed=7)
    assert hidden.expected_retention(-1) == 0.0
    assert hidden.realized_test(-1) == 0.0


def test_isolation_report_runs_real_attempts_and_passes() -> None:
    report = isolation_report(QUESTIONS, seed=7, params=SimulationParams())
    assert report["all_passed"] is True, report["checks"]
    assert report["checks"], "the report must contain computed checks"
    assert all(report["checks"].values()), report["checks"]
    rejected = [item for item in report["attempts"]
                if item["attempt"] != "legitimate teach+quiz"]
    assert rejected, "the audit must actually attempt reads/tampering"
    assert all(item["response"].get("ok") is False for item in rejected)
    attempted_ops = {item["attempt"].get("op") for item in rejected}
    assert {"snapshot", "expected_retention", "realized_test",
            "set_stability", "teach"} <= attempted_ops


def test_agent_facade_has_no_hidden_object_or_closure() -> None:
    session = start_hidden(QUESTIONS, seed=11)
    try:
        api = session.api
        assert not hasattr(api, "__dict__")
        assert all(not hasattr(api, name) for name in HIDDEN_ATTRIBUTES)
        for name in ("teach", "quiz", "restudy"):
            function = getattr(api, name)
            assert getattr(function, "__closure__", None) is None
            assert not any(isinstance(cell.cell_contents, HiddenLearner)
                           for cell in (getattr(function, "__closure__", None) or ()))
        reachable = [item for item in gc.get_referents(api)
                     if isinstance(item, HiddenLearner)]
        assert not reachable
        assert "stability" not in repr(api)
        assert "last_day" not in repr(api)
        try:
            api.__setattr__("items", {})
        except AttributeError:
            pass
        else:  # pragma: no cover - a slots regression would fail here
            raise AssertionError("slots must reject attribute injection")
    finally:
        session.close()


def test_tamper_through_the_agent_pipe_is_rejected_and_state_is_unchanged() -> None:
    session = start_hidden(QUESTIONS, seed=13)
    try:
        api, evaluator = session.api, session.evaluator
        api.teach(["q1"], 0)
        assert type(api.quiz("q1", 0)) is bool
        before = evaluator.snapshot()
        channel = api._channel
        for payload in (
            {"op": "snapshot"},
            {"op": "expected_retention", "day": 29},
            {"op": "realized_test", "day": 29},
            {"op": "set_stability", "qid": "q1", "value": 60.0},
            {"op": "teach", "qids": ["__class__"], "day": 0},
        ):
            channel.send(payload)
            response = channel.recv()
            assert response.get("ok") is False, (payload, response)
            assert set(response) <= {"ok", "error"}, response
            assert "last_day" not in json.dumps(response)
            assert "practices" not in json.dumps(response)
            assert "60.0" not in json.dumps(response)
        after = evaluator.snapshot()
        assert after == before
    finally:
        session.close()


def test_agent_driver_source_has_no_hidden_evaluation_calls() -> None:
    source = inspect.getsource(runner._agent_arm)
    for forbidden in ("expected_retention", "realized_test", "snapshot",
                      ".items", "true_stability", "hidden.", "make_learner_api"):
        assert forbidden not in source, forbidden


def test_agent_driver_only_uses_the_allowed_api(tmp_path: Path) -> None:
    class SpyAPI:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def teach(self, qids, day) -> None:  # noqa: ANN001
            self.calls.append("teach")

        def quiz(self, qid, day) -> bool:  # noqa: ANN001
            self.calls.append("quiz")
            return True

        def restudy(self, qids, day) -> None:  # noqa: ANN001
            self.calls.append("restudy")

    workspace, bank, by_chapter = runner._setup_workspace(tmp_path / "lh")
    spy = SpyAPI()
    counters = runner._agent_arm(spy, workspace, bank, by_chapter, SimulationParams())
    assert set(spy.calls) <= {"teach", "quiz", "restudy"}
    assert spy.calls, "the agent must exercise the learner API"
    assert counters["answers"] > 0


def test_hidden_state_never_reaches_results_or_study_state(tmp_path: Path) -> None:
    root = tmp_path / "results"
    results = runner.run(root, seed=42)
    payload = json.dumps(results, ensure_ascii=False)
    # Rejected operation *names* appear in the isolation audit; hidden field
    # names and values must not.
    for hidden_key in ('"last_day"', '"practices"', '"successes"',
                       '"true_stability"', '"exposures"'):
        assert hidden_key not in payload, hidden_key
    assert results["integrity"]["hidden_state_isolated"] is True
    assert results["integrity"]["hidden_state_isolated"] == \
        results["integrity"]["isolation"]["all_passed"]
    assert all(results["integrity"]["isolation"]["checks"].values())

    state_path = root / "main" / "longhorizon-workspace" / "study_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_text = json.dumps(state, ensure_ascii=False)
    for hidden_key in ('"last_day"', '"practices"', '"successes"',
                       '"true_stability"', '"exposures"'):
        assert hidden_key not in state_text, hidden_key


def test_same_seed_repeats_and_a_different_seed_changes_the_sample(tmp_path: Path) -> None:
    first = runner.run(tmp_path / "a", seed=99)
    second = runner.run(tmp_path / "b", seed=99)
    assert tolerance_equal(first["arms"], second["arms"])
    third = runner.run(tmp_path / "c", seed=100)
    assert not tolerance_equal(first["arms"], third["arms"])


def test_examprep_package_does_not_import_the_benchmark_package() -> None:
    package_root = Path(__file__).resolve().parent.parent / "examprep_agent"
    offenders = []
    for path in package_root.rglob("*.py"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import benchmarks", "from benchmarks")):
                offenders.append("%s: %s" % (path, stripped))
    assert not offenders, offenders
