# -*- coding: utf-8 -*-
"""Process-isolated simulated learner.

The P3 review (B2) showed that a closure-based façade still leaked the hidden
learner through ``api.quiz.__func__.__closure__``.  Structural isolation is now
an OS boundary:

* the :class:`~benchmarks.longhorizon.simulator.HiddenLearner` is constructed
  **only inside a child process**; the parent never holds it;
* the agent driver receives :class:`RemoteLearnerAPI`, whose only public
  methods are ``teach`` / ``quiz`` / ``restudy`` and whose single private slot
  is a pipe connection;
* a second, evaluator-only pipe supports ``expected_retention`` /
  ``realized_test`` / ``snapshot``; the agent's pipe rejects those operations;
* :func:`isolation_report` actually attempts reads and tampering and returns
  booleans; the benchmark reports ``hidden_state_isolated = all(checks)``
  instead of a hard-coded literal.

The protocol is request/response on both pipes, so a run is deterministic even
though it crosses a process boundary.
"""
from __future__ import annotations

import gc
import multiprocessing
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from typing import Any, Iterable, Mapping, Sequence

from .simulator import HiddenLearner, LearnerAPI, SimulationParams

HIDDEN_ATTRIBUTES = (
    "items", "stability", "seed", "snapshot", "expected_retention",
    "realized_test", "learner", "hidden", "last_day", "practices",
    "successes", "true_stability",
)
HIDDEN_DICT_KEYS = frozenset({
    "stability", "last_day", "practices", "successes", "true_stability",
    "exposures", "expected_retention",
})
AGENT_OPS = frozenset({"teach", "quiz", "restudy", "close"})
EVAL_OPS = frozenset({"expected_retention", "realized_test", "snapshot", "close"})


class RemoteError(RuntimeError):
    """The simulator process failed or disappeared."""


def _reply(connection: Connection, payload: dict[str, Any]) -> None:
    connection.send(payload)


def _serve(agent_connection: Connection, eval_connection: Connection,
           questions: Sequence[Mapping[str, Any]], seed: int,
           params: SimulationParams) -> None:
    """Child process entry point: owns the only HiddenLearner instance."""
    hidden = HiddenLearner(questions, seed, params)
    connections = {agent_connection: "agent", eval_connection: "evaluator"}
    open_connections = {agent_connection, eval_connection}
    while open_connections:
        try:
            ready = wait(list(open_connections))
        except (EOFError, OSError):
            break
        for connection in ready:
            if connection not in open_connections:
                continue
            try:
                request = connection.recv()
            except (EOFError, OSError):
                open_connections.discard(connection)
                continue
            if not isinstance(request, dict):
                _reply(connection, {"ok": False, "error": "malformed request"})
                continue
            op = str(request.get("op", ""))
            role = connections[connection]
            allowed = AGENT_OPS if role == "agent" else EVAL_OPS
            if op not in allowed:
                _reply(connection, {"ok": False, "error": "unsupported operation: %s" % op})
                continue
            if op == "close":
                for item in list(open_connections):
                    try:
                        item.close()
                    except (OSError, ValueError):
                        pass
                return
            try:
                _reply(connection, {"ok": True, "result": _handle(hidden, op, request)})
            except Exception as exc:  # never leak a traceback or hidden state
                _reply(connection, {"ok": False,
                                    "error": "%s: %s" % (type(exc).__name__, exc)})


def _handle(hidden: HiddenLearner, op: str, request: Mapping[str, Any]) -> Any:
    """Execute one validated operation on the hidden learner."""
    if op == "teach":
        qids = _qids(request)
        return hidden.teach(qids, _day(request))
    if op == "restudy":
        qids = _qids(request)
        return hidden.restudy(qids, _day(request))
    if op == "quiz":
        qid = request.get("qid")
        if not isinstance(qid, str) or not qid:
            raise ValueError("qid must be a non-empty string")
        return bool(hidden.practice(qid, _day(request)))
    if op == "expected_retention":
        return float(hidden.expected_retention(_day(request)))
    if op == "realized_test":
        return float(hidden.realized_test(_day(request)))
    if op == "snapshot":
        return hidden.snapshot()
    raise ValueError("unsupported operation: %s" % op)


def _qids(request: Mapping[str, Any]) -> list[str]:
    qids = request.get("qids")
    if not isinstance(qids, (list, tuple)) or not all(isinstance(q, str) for q in qids):
        raise ValueError("qids must be a list of strings")
    return list(qids)


def _day(request: Mapping[str, Any]) -> int:
    day = request.get("day")
    if not isinstance(day, int) or isinstance(day, bool):
        raise ValueError("day must be an integer")
    return int(day)


class RemoteLearnerAPI(LearnerAPI):
    """The only learner-facing object the agent driver receives.

    ``__slots__`` prevents instance dictionaries; methods are ordinary class
    functions (no closures), and the sole private slot is a pipe.  The hidden
    learner lives in the child process and is never reachable from this object.
    """

    __slots__ = ("_channel",)

    def __init__(self, channel: Connection) -> None:
        self._channel = channel

    def public_surface(self) -> tuple[str, ...]:
        return ("teach", "quiz", "restudy", "public_surface")

    def _request(self, payload: dict[str, Any]) -> Any:
        try:
            self._channel.send(payload)
            response = self._channel.recv()
        except (EOFError, OSError) as exc:
            raise RemoteError("simulator process is not available") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else "malformed response"
            raise RemoteError(str(error))
        return response.get("result")

    def teach(self, qids: Iterable[str], day: int) -> None:
        self._request({"op": "teach", "qids": list(qids), "day": int(day)})

    def quiz(self, qid: str, day: int) -> bool:
        return bool(self._request({"op": "quiz", "qid": str(qid), "day": int(day)}))

    def restudy(self, qids: Iterable[str], day: int) -> None:
        self._request({"op": "restudy", "qids": list(qids), "day": int(day)})


class RemoteEvaluator:
    """Evaluator-only channel: true retention is read here, after the run."""

    __slots__ = ("_channel", "_process")

    def __init__(self, channel: Connection, process: multiprocessing.Process) -> None:
        self._channel = channel
        self._process = process

    def _request(self, payload: dict[str, Any]) -> Any:
        try:
            self._channel.send(payload)
            response = self._channel.recv()
        except (EOFError, OSError) as exc:
            raise RemoteError("simulator process is not available") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else "malformed response"
            raise RemoteError(str(error))
        return response.get("result")

    def expected_retention(self, day: int) -> float:
        return float(self._request({"op": "expected_retention", "day": int(day)}))

    def realized_test(self, day: int) -> float:
        return float(self._request({"op": "realized_test", "day": int(day)}))

    def snapshot(self) -> dict[str, Any]:
        return dict(self._request({"op": "snapshot"}))

    def close(self) -> None:
        try:
            if self._process.is_alive():
                self._channel.send({"op": "close"})
        except (EOFError, OSError, ValueError):
            pass
        try:
            self._process.join(timeout=5)
        except (OSError, ValueError):
            pass
        try:
            self._channel.close()
        except (OSError, ValueError):
            pass
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)

    def __enter__(self) -> "RemoteEvaluator":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


@dataclass
class HiddenSession:
    """Bundled agent API + evaluator channel for one simulated learner."""

    api: RemoteLearnerAPI
    evaluator: RemoteEvaluator

    def close(self) -> None:
        self.evaluator.close()


def _context() -> Any:
    # fork preserves the already-imported package and is available on the
    # Linux target; fall back to spawn where fork is unavailable.
    try:
        return multiprocessing.get_context("fork")
    except ValueError:  # pragma: no cover - non-POSIX
        return multiprocessing.get_context("spawn")


def start_hidden(questions: Sequence[Mapping[str, Any]], seed: int,
                 params: SimulationParams | None = None) -> HiddenSession:
    """Start a child process that owns one HiddenLearner and return its façades."""
    context = _context()
    agent_parent, agent_child = context.Pipe()
    eval_parent, eval_child = context.Pipe()
    process = context.Process(
        target=_serve,
        args=(agent_child, eval_child, [dict(q) for q in questions], int(seed),
              params or SimulationParams()),
        daemon=True,
    )
    process.start()
    # The child owns these ends; close the parent's copies so EOF is meaningful.
    agent_child.close()
    eval_child.close()
    return HiddenSession(RemoteLearnerAPI(agent_parent),
                         RemoteEvaluator(eval_parent, process))


# ---------------------------------------------------------------- isolation audit
def _find_hidden_referents(root: Any, depth: int = 3) -> list[str]:
    """Walk the reachable object graph for a HiddenLearner or hidden-state dict."""
    found: list[str] = []
    seen: set[int] = set()

    def walk(value: Any, remaining: int) -> None:
        if remaining < 0 or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, HiddenLearner):
            found.append("HiddenLearner instance")
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) in HIDDEN_DICT_KEYS:
                    found.append("hidden key: %s" % key)
                walk(item, remaining - 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(item, remaining - 1)
            return
        for referent in gc.get_referents(value):
            walk(referent, remaining - 1)

    walk(root, depth)
    return found


def _closure_has_hidden(function: Any) -> bool:
    closure = getattr(function, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                if isinstance(cell.cell_contents, HiddenLearner):
                    return True
            except ValueError:  # empty cell
                continue
    return False


def isolation_report(questions: Sequence[Mapping[str, Any]], seed: int,
                     params: SimulationParams | None = None) -> dict[str, Any]:
    """Run real read/tamper attempts against a fresh isolated learner.

    Returns a dict of booleans computed by the attempts; the caller must use
    ``all_passed`` rather than assuming isolation from the design alone.
    """
    session = start_hidden(questions, seed, params)
    attempts: list[dict[str, Any]] = []
    checks: dict[str, bool] = {}
    try:
        api = session.api
        evaluator = session.evaluator
        checks["no_instance_dictionary"] = not hasattr(api, "__dict__")
        public = {name for name in dir(api) if not name.startswith("_")}
        checks["narrow_public_surface"] = public == {
            "public_surface", "quiz", "restudy", "teach"}
        checks["no_hidden_attributes"] = all(
            not hasattr(api, name) for name in HIDDEN_ATTRIBUTES)
        checks["no_hidden_closure_state"] = all(
            not _closure_has_hidden(getattr(api, name))
            for name in ("teach", "quiz", "restudy"))
        checks["no_hidden_objects_reachable"] = not _find_hidden_referents(api)
        checks["repr_has_no_hidden_keys"] = not any(
            key in repr(api) for key in ("stability", "last_day", "practices", "items"))
        try:
            api.__setattr__("items", {})
            checks["slots_reject_tamper"] = False
        except AttributeError:
            checks["slots_reject_tamper"] = True

        # A legitimate teach + quiz establishes that the façade works.
        probe_qid = str(questions[0]["id"]) if questions else "q1"
        api.teach([probe_qid], 0)
        first = api.quiz(probe_qid, 0)
        attempts.append({"attempt": "legitimate teach+quiz", "result": bool(first)})
        baseline = evaluator.snapshot()

        channel = api._channel  # deliberately attacking our own façade
        for payload in (
            {"op": "snapshot"},
            {"op": "expected_retention", "day": 29},
            {"op": "realized_test", "day": 29},
            {"op": "set_stability", "qid": "q1", "value": 60.0},
            {"op": "teach", "qids": ["__class__"], "day": 0},
            {"op": "quiz", "qid": "does-not-exist", "day": 0},
            {"op": "evaluate_all"},
        ):
            channel.send(payload)
            response = channel.recv()
            attempts.append({"attempt": payload, "response": response})
        # Read the true state again: none of the rejected operations may have
        # changed it.  The legitimate teach+quiz happened before `baseline`.
        after = evaluator.snapshot()
        checks["tamper_attempts_rejected"] = all(
            isinstance(item.get("response"), dict) and item["response"].get("ok") is False
            for item in attempts if item["attempt"] != "legitimate teach+quiz")
        checks["hidden_state_unchanged_by_tamper"] = after == baseline
        checks["true_state_not_returned_to_agent"] = all(
            not (isinstance(item.get("response"), dict) and item["response"].get("ok"))
            for item in attempts if item["attempt"] != "legitimate teach+quiz")
    finally:
        session.close()
    return {
        "checks": checks,
        "all_passed": all(checks.values()),
        "attempts": attempts,
        "note": ("checks are computed by running the attempts; hidden_state_isolated "
                 "in results.json is all_passed"),
    }
