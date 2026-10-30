# -*- coding: utf-8 -*-
"""Thirty-day long-horizon simulation: agent-guided vs fixed-order baseline.

Protocol
--------
* 23 study days (30 simulated minutes/day of *available* budget), followed by a
  7-day no-study retention interval; the delayed test is on day 30 (index 29).
* **agent arm** — the real ``Session`` planner: FSRS due cards first,
  prerequisite graph, BKT mastery gate, daily new-chapter budget.
* **baseline arm** — fixed chapter order, one quiz per question at first
  exposure, then massed restudy in a rotating cycle.  Restudy never repeats an
  item on the same simulated day (same-day re-reading has zero modelled gain),
  so the baseline is not padded with no-op actions.

Integrity and metric rules (P3 review B2/I5)
-------------------------------------------
* the hidden learner is process-isolated (see ``remote.py``); the drivers only
  receive ``RemoteLearnerAPI`` and the evaluator reads true retention through a
  separate pipe after the run;
* ``hidden_state_isolated`` is computed by :func:`isolation_report` from actual
  read/tamper attempts, never hard-coded;
* learning gain is defined as ``delayed retention - pretest retention``
  (percentage points), and both arms receive the **same available time budget**;
* efficiency metrics state their formula and denominator explicitly, including
  a counterfactual without scheduled reviews / without massed restudy and an
  equal-actual-time intervention.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .remote import HiddenSession, isolation_report, start_hidden
from .simulator import SimulationParams

START_DATE = date(2026, 1, 5)
STUDY_DAYS = 23
RETENTION_GAP_DAYS = 7
TOTAL_DAYS = STUDY_DAYS + RETENTION_GAP_DAYS  # delayed test on day 30 (index 29)


def _setup_workspace(root: Path) -> tuple[str, list[dict[str, Any]], dict[int, list[str]]]:
    from examprep_agent.service.models import SetupRequest
    from examprep_agent.service.study import StudyService

    from ..common import copy_sample_course

    course = copy_sample_course(root / "longhorizon-course")
    workspace = str(root / "longhorizon-workspace")
    service = StudyService(pointer=str(root / "pointer"))
    service.setup(SetupRequest(materials=str(course), workspace=workspace,
                               lang="zh", days=60, workers=1))
    bank = service.load_bank(workspace)
    by_chapter: dict[int, list[str]] = {}
    for question in bank:
        by_chapter.setdefault(int(question.get("chapter") or 0), []).append(str(question["id"]))
    for qids in by_chapter.values():
        qids.sort()
    return workspace, bank, by_chapter


def _complete_chapter(session: Any, chapter_n: int) -> bool:
    from examprep_agent.tools import state as st

    chapter = st.chapter(session.state, chapter_n)
    if chapter is None or chapter.get("status") != "todo":
        return False
    chapter["status"] = "verified"
    chapter["part"] = chapter["parts"]
    todo = [item for item in session.state["chapters"] if item.get("status") == "todo"]
    if todo:
        session.state["current"] = todo[0]["n"]
    return True


def _blank_counters() -> dict[str, Any]:
    return {
        "teach_minutes": 0,
        "quiz_minutes": 0,
        "review_minutes": 0,
        "restudy_minutes": 0,
        "answers": 0,
        "reviews_done": 0,
        "chapters_completed": 0,
        "study_days_used": 0,
        "daily": [],
    }


def _agent_arm(api: Any, workspace: str, bank: Sequence[Mapping[str, Any]],
               by_chapter: Mapping[int, Sequence[str]], params: SimulationParams,
               *, cap_minutes: int | None = None,
               allow_reviews: bool = True) -> dict[str, Any]:
    """Drive the real planner/FSRS session against the isolated learner.

    ``cap_minutes`` implements the equal-actual-time intervention: once the
    cumulative action minutes reach the cap, no further actions are taken.
    ``allow_reviews=False`` is the no-scheduled-review counterfactual: the same
    new-material teaching and initial quizzes, but due FSRS reviews are skipped.
    """
    from examprep_agent.core.session import Session

    session = Session.load(workspace)
    if not allow_reviews:
        session.policy = replace(session.policy, review_first=False)
    state = session.state
    counters = _blank_counters()
    taught: set[int] = set()
    completed: set[int] = set()
    question_chapter = {str(q["id"]): int(q.get("chapter") or 0) for q in bank}
    total_spent = 0

    for day_index in range(STUDY_DAYS):
        if cap_minutes is not None and total_spent >= cap_minutes:
            break
        today = START_DATE.fromordinal(START_DATE.toordinal() + day_index)
        budget = params.day_minutes
        if cap_minutes is not None:
            budget = min(budget, cap_minutes - total_spent)
        reviewed_today: set[str] = set()
        taught_today: set[int] = set()
        day_actions = 0
        # Each iteration performs one action and then re-reads the planner.
        for _ in range(200):
            if budget < min(params.quiz_minutes, params.teach_minutes):
                break
            recommendation = session.recommend(bank, today=today)
            if recommendation.kind == "due_review":
                if not allow_reviews:
                    # With review_first disabled, due_review only appears when
                    # no new material remains; the counterfactual stops there.
                    break
                due = [item for item in recommendation.due if item.qid not in reviewed_today]
                if not due:
                    break
                item = due[0]
                correct = api.quiz(item.qid, day_index)
                session.record_answer(item.qid, int(item.chapter or 0),
                                      "right" if correct else "wrong", today=today)
                budget -= params.quiz_minutes
                total_spent += params.quiz_minutes
                counters["review_minutes"] += params.quiz_minutes
                counters["reviews_done"] += 1
                counters["answers"] += 1
                reviewed_today.add(item.qid)
                day_actions += 1
                continue
            if recommendation.kind not in ("new", "continue", "prereq_blocked", "daily_limit"):
                break
            chapter_n = recommendation.chapter
            if chapter_n is None:
                break
            chapter_n = int(chapter_n)
            qids = list(by_chapter.get(chapter_n, []))
            if not qids:
                break
            # First exposure to this chapter: present the material.
            if chapter_n not in taught and budget >= params.teach_minutes:
                api.teach(qids, day_index)
                session.mark_new_chapter(chapter_n, today)
                budget -= params.teach_minutes
                total_spent += params.teach_minutes
                counters["teach_minutes"] += params.teach_minutes
                taught.add(chapter_n)
                taught_today.add(chapter_n)
                day_actions += 1
            # Retrieval practice: unseen cards first, then open mistakes/newer.
            if budget >= params.quiz_minutes:
                ordered = sorted(
                    qids,
                    key=lambda qid: (qid not in session.scheduler.cards,
                                     qid not in {m["qid"] for m in state["mistakes"]
                                                 if m.get("status") == "open"},
                                     qid),
                )
                for qid in ordered:
                    if budget < params.quiz_minutes:
                        break
                    correct = api.quiz(qid, day_index)
                    session.record_answer(qid, chapter_n,
                                          "right" if correct else "wrong", today=today)
                    budget -= params.quiz_minutes
                    total_spent += params.quiz_minutes
                    counters["quiz_minutes"] += params.quiz_minutes
                    counters["answers"] += 1
                    day_actions += 1
                    if (session.should_advance(chapter_n)
                            and all(q in session.scheduler.cards for q in qids)):
                        break
                if (session.should_advance(chapter_n)
                        and all(q in session.scheduler.cards for q in qids)):
                    if _complete_chapter(session, chapter_n):
                        completed.add(chapter_n)
                        counters["chapters_completed"] += 1
                        day_actions += 1
            # Still below the gate and time remains: re-present the material.
            if (chapter_n not in completed and budget >= params.teach_minutes
                    and chapter_n in taught and chapter_n not in taught_today):
                api.teach(qids, day_index)
                budget -= params.teach_minutes
                total_spent += params.teach_minutes
                counters["teach_minutes"] += params.teach_minutes
                taught_today.add(chapter_n)
                day_actions += 1
        session.save()
        if day_actions:
            counters["study_days_used"] += 1
        counters["daily"].append({
            "day": day_index + 1,
            "minutes": params.day_minutes - budget,
            "actions": day_actions,
            "completed": len(completed),
        })
    counters["chapters_completed"] = len(completed)
    counters["cap_minutes"] = cap_minutes
    counters["allow_reviews"] = allow_reviews
    return counters


def _baseline_arm(api: Any, by_chapter: Mapping[int, Sequence[str]],
                  params: SimulationParams, *, cap_minutes: int | None = None,
                  allow_restudy: bool = True) -> dict[str, Any]:
    """Fixed order, one quiz at first exposure, then massed restudy.

    Same-day restudy of an already-restudied item is skipped: it has zero
    modelled gain and would only inflate the baseline's time denominator.
    """
    counters = _blank_counters()
    chapters = sorted(by_chapter)
    taught: set[int] = set()
    restudy_cycle: list[str] = []
    total_spent = 0
    for day_index in range(STUDY_DAYS):
        if cap_minutes is not None and total_spent >= cap_minutes:
            break
        budget = params.day_minutes
        if cap_minutes is not None:
            budget = min(budget, cap_minutes - total_spent)
        day_actions = 0
        # First pass: teach the next chapter in fixed order and quiz once.
        for chapter_n in chapters:
            if chapter_n in taught:
                continue
            qids = list(by_chapter[chapter_n])
            if not qids or budget < params.teach_minutes:
                break
            api.teach(qids, day_index)
            budget -= params.teach_minutes
            total_spent += params.teach_minutes
            counters["teach_minutes"] += params.teach_minutes
            taught.add(chapter_n)
            restudy_cycle.extend(qids)
            day_actions += 1
            for qid in qids:
                if budget < params.quiz_minutes:
                    break
                api.quiz(qid, day_index)
                budget -= params.quiz_minutes
                total_spent += params.quiz_minutes
                counters["quiz_minutes"] += params.quiz_minutes
                counters["answers"] += 1
                day_actions += 1
            break
        # Fill the remaining budget with massed restudy in a fixed cycle.
        restudied_today: set[str] = set()
        restudy_index = day_index % max(1, len(restudy_cycle)) if restudy_cycle else 0
        while allow_restudy and restudy_cycle and budget >= params.restudy_minutes:
            picked = next((restudy_cycle[(restudy_index + offset) % len(restudy_cycle)]
                           for offset in range(len(restudy_cycle))
                           if restudy_cycle[(restudy_index + offset) % len(restudy_cycle)]
                           not in restudied_today), None)
            if picked is None:
                break
            restudy_index = (restudy_cycle.index(picked) + 1) % len(restudy_cycle)
            api.restudy([picked], day_index)
            restudied_today.add(picked)
            budget -= params.restudy_minutes
            total_spent += params.restudy_minutes
            counters["restudy_minutes"] += params.restudy_minutes
            day_actions += 1
        if day_actions:
            counters["study_days_used"] += 1
        counters["daily"].append({
            "day": day_index + 1,
            "minutes": params.day_minutes - budget,
            "actions": day_actions,
            "completed": len(taught),
        })
    counters["chapters_completed"] = len(taught)
    counters["cap_minutes"] = cap_minutes
    counters["allow_restudy"] = allow_restudy
    return counters


def _ci95(probability: float, items: int) -> list[float] | None:
    """Normal-approximation 95% interval for one sampled delayed test."""
    if items <= 0:
        return None
    error = 1.96 * math.sqrt(max(0.0, probability * (1.0 - probability)) / items)
    return [round(max(0.0, probability - error) * 100.0, 4),
            round(min(1.0, probability + error) * 100.0, 4)]


def _evaluate(evaluator: Any, counters: Mapping[str, Any],
              params: SimulationParams, arm: str, pretest: float,
              *, items: int) -> dict[str, Any]:
    """Read true retention through the evaluator channel after the run."""
    post_day = STUDY_DAYS - 1          # end of the last study day
    test_day = TOTAL_DAYS - 1          # 7 days after the last study day
    post_training = evaluator.expected_retention(post_day)
    delayed = evaluator.expected_retention(test_day)
    realized = evaluator.realized_test(test_day)
    study_minutes = int(counters["teach_minutes"] + counters["quiz_minutes"]
                        + counters["review_minutes"] + counters["restudy_minutes"])
    review_minutes = int(counters["review_minutes"])
    restudy_minutes = int(counters["restudy_minutes"])
    study_hours = study_minutes / 60.0
    budget_minutes = STUDY_DAYS * params.day_minutes
    gain_points = (delayed - pretest) * 100.0
    return {
        "arm": arm,
        "protocol_days": TOTAL_DAYS,
        "study_days": STUDY_DAYS,
        "retention_gap_days": RETENTION_GAP_DAYS,
        "pretest_retention_pct": round(pretest * 100.0, 4),
        "end_of_training_retention_pct": round(post_training * 100.0, 4),
        "delayed_retention_pct": round(delayed * 100.0, 4),
        "delayed_test_accuracy_pct": round(realized * 100.0, 4),
        "delayed_test_ci95_pct": _ci95(realized, items),
        "learning_gain_pct_points": round(gain_points, 4),
        "available_budget_minutes": budget_minutes,
        "study_minutes": study_minutes,
        "teach_minutes": int(counters["teach_minutes"]),
        "quiz_minutes": int(counters["quiz_minutes"]),
        "review_minutes": review_minutes,
        "restudy_minutes": restudy_minutes,
        "answers": int(counters["answers"]),
        "reviews_done": int(counters["reviews_done"]),
        "chapters_completed": int(counters["chapters_completed"]),
        "study_days_used": int(counters["study_days_used"]),
        "gain_per_study_hour": (round(gain_points / study_hours, 4)
                                if study_hours else None),
        "gain_per_budget_hour": round(gain_points / (budget_minutes / 60.0), 4),
        "retention_per_study_hour": (round(delayed * 100.0 / study_hours, 4)
                                     if study_hours else None),
        "cap_minutes": counters.get("cap_minutes"),
        "daily": list(counters["daily"]),
    }


def _run_one(questions: Sequence[Mapping[str, Any]], seed: int,
             params: SimulationParams, arm: str, *, workspace: str | None,
             bank: Sequence[Mapping[str, Any]] | None,
             by_chapter: Mapping[int, Sequence[str]] | None,
             pretest: float, cap_minutes: int | None = None,
             allow_reviews: bool = True,
             allow_restudy: bool = True) -> dict[str, Any]:
    """Start one isolated learner, drive one arm, evaluate, and shut it down."""
    session: HiddenSession = start_hidden(questions, seed, params)
    try:
        if arm == "agent_guided":
            counters = _agent_arm(session.api, str(workspace), bank or [],
                                  by_chapter or {}, params,
                                  cap_minutes=cap_minutes,
                                  allow_reviews=allow_reviews)
        else:
            counters = _baseline_arm(session.api, by_chapter or {}, params,
                                     cap_minutes=cap_minutes,
                                     allow_restudy=allow_restudy)
        return _evaluate(session.evaluator, counters, params, arm, pretest,
                         items=len(questions))
    finally:
        session.close()


def run(root: str | Path, seed: int = 20261030,
        params: SimulationParams | None = None) -> dict[str, Any]:
    """Run both arms, counterfactuals and the isolation audit."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    params = params or SimulationParams()
    workspace, bank, by_chapter = _setup_workspace(root / "main")
    questions = [{"id": str(q["id"]), "chapter": int(q.get("chapter") or 0)} for q in bank]

    # Pretest: a fresh simulator, before any exposure (recall is 0 by construction).
    pretest_session = start_hidden(questions, seed, params)
    try:
        pretest = pretest_session.evaluator.expected_retention(-1)
    finally:
        pretest_session.close()

    # Main arms.
    agent = _run_one(questions, seed, params, "agent_guided", workspace=workspace,
                     bank=bank, by_chapter=by_chapter, pretest=pretest)
    baseline = _run_one(questions, seed, params, "baseline_fixed_order",
                        workspace=None, bank=None, by_chapter=by_chapter,
                        pretest=pretest)

    # Counterfactuals: same policy without scheduled reviews / massed restudy.
    no_review_workspace, no_review_bank, no_review_by_chapter = _setup_workspace(
        root / "no-review")
    no_review = _run_one(questions, seed, params, "agent_guided",
                         workspace=no_review_workspace, bank=no_review_bank,
                         by_chapter=no_review_by_chapter, pretest=pretest,
                         allow_reviews=False)
    no_restudy = _run_one(questions, seed, params, "baseline_fixed_order",
                          workspace=None, bank=None, by_chapter=by_chapter,
                          pretest=pretest, allow_restudy=False)
    agent["counterfactual_no_scheduled_review"] = {
        "delayed_retention_pct": no_review["delayed_retention_pct"],
        "incremental_gain_pct_points": round(
            agent["delayed_retention_pct"] - no_review["delayed_retention_pct"], 4),
    }
    if agent["review_minutes"] > 0:
        agent["incremental_review_efficiency_pp_per_review_hour"] = round(
            (agent["delayed_retention_pct"] - no_review["delayed_retention_pct"])
            / (agent["review_minutes"] / 60.0), 4)
    else:
        agent["incremental_review_efficiency_pp_per_review_hour"] = None
    baseline["counterfactual_no_massed_restudy"] = {
        "delayed_retention_pct": no_restudy["delayed_retention_pct"],
        "incremental_gain_pct_points": round(
            baseline["delayed_retention_pct"] - no_restudy["delayed_retention_pct"], 4),
    }
    if baseline["restudy_minutes"] > 0:
        baseline["incremental_restudy_efficiency_pp_per_restudy_hour"] = round(
            (baseline["delayed_retention_pct"] - no_restudy["delayed_retention_pct"])
            / (baseline["restudy_minutes"] / 60.0), 4)
    else:
        baseline["incremental_restudy_efficiency_pp_per_restudy_hour"] = None

    # Equal-actual-time intervention: both arms stop after the same spent minutes.
    equal_cap = int(min(agent["study_minutes"], baseline["study_minutes"]))
    equal_workspace, equal_bank, equal_by_chapter = _setup_workspace(root / "equal-time")
    equal_agent = _run_one(questions, seed, params, "agent_guided",
                           workspace=equal_workspace, bank=equal_bank,
                           by_chapter=equal_by_chapter, pretest=pretest,
                           cap_minutes=equal_cap)
    equal_baseline = _run_one(questions, seed, params, "baseline_fixed_order",
                              workspace=None, bank=None, by_chapter=by_chapter,
                              pretest=pretest, cap_minutes=equal_cap)

    # Isolation audit: real read/tamper attempts on a fresh isolated learner.
    isolation = isolation_report(questions, seed, params)

    return {
        "layer": "LongHorizon Sim",
        "integrity": {
            "hidden_state_isolated": bool(isolation["all_passed"]),
            "isolation": isolation,
            "agent_visible_surface": ["teach", "quiz -> bool", "restudy"],
            "note": ("HiddenLearner is constructed only in a child process; the agent "
                     "holds a pipe-backed façade. hidden_state_isolated is computed by "
                     "isolation_report(), not hard-coded."),
        },
        "seed": seed,
        "params": params.to_dict(),
        "protocol": {
            "start_date": START_DATE.isoformat(),
            "study_days": STUDY_DAYS,
            "retention_gap_days": RETENTION_GAP_DAYS,
            "total_days": TOTAL_DAYS,
            "delayed_test_day": TOTAL_DAYS,
            "day_minutes": params.day_minutes,
            "available_budget_minutes": STUDY_DAYS * params.day_minutes,
            "same_time_budget_for_both_arms": True,
            "agent_arm": "FSRS due reviews + prerequisite graph + BKT mastery gate",
            "baseline_arm": ("fixed order; one quiz per question at first exposure; "
                             "massed restudy in a rotating cycle; no FSRS scheduling"),
            "metrics": {
                "pretest_retention": ("mean true recall probability on a fresh "
                                      "simulator before any exposure (0 by construction)"),
                "learning_gain": "delayed_retention_pct - pretest_retention_pct (percentage points)",
                "gain_per_study_hour": ("learning_gain_pct_points / (actual study minutes / 60); "
                                        "the denominator is what the arm actually spent"),
                "gain_per_budget_hour": ("learning_gain_pct_points / (available budget minutes / 60); "
                                         "the denominator is identical for both arms"),
                "incremental_review_efficiency": ("(delayed retention with scheduled reviews - "
                                                  "delayed retention of the same policy without "
                                                  "scheduled reviews) / (review minutes / 60)"),
                "incremental_restudy_efficiency": ("(delayed retention with massed restudy - "
                                                   "delayed retention without massed restudy) / "
                                                   "(restudy minutes / 60)"),
                "delayed_test_ci95": ("normal-approximation 95% interval for the one sampled "
                                      "delayed test over all items"),
            },
        },
        "arms": {"agent_guided": agent, "baseline_fixed_order": baseline},
        "counterfactual_arms": {
            "agent_no_scheduled_review": no_review,
            "baseline_no_massed_restudy": no_restudy,
        },
        "equal_time": {
            "cap_minutes": equal_cap,
            "matching": "both arms stop once cumulative action minutes reach the cap",
            "agent_guided": {
                "delayed_retention_pct": equal_agent["delayed_retention_pct"],
                "study_minutes": equal_agent["study_minutes"],
                "learning_gain_pct_points": equal_agent["learning_gain_pct_points"],
            },
            "baseline_fixed_order": {
                "delayed_retention_pct": equal_baseline["delayed_retention_pct"],
                "study_minutes": equal_baseline["study_minutes"],
                "learning_gain_pct_points": equal_baseline["learning_gain_pct_points"],
            },
        },
        "caveat": (
            "Simulation results are not real-human effects. The hidden learner is a "
            "parameterised FSRS-like model with deterministic response sampling; the "
            "numbers validate the scheduling pipeline under those assumptions, not "
            "learning outcomes for people."
        ),
    }
