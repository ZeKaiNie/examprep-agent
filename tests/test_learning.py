# -*- coding: utf-8 -*-
"""Learning-layer tests: mode selection, BKT direction and FSRS interval monotonicity."""
from __future__ import annotations

from datetime import date

import pytest

from examprep_agent.learning.learner import BKTModel, BKTParams
from examprep_agent.learning.modes import (
    LONGTERM_THRESHOLD_DAYS,
    CramPolicy,
    LongTermPolicy,
    select_policy,
)
from examprep_agent.learning.scheduler import Card, FSRS, FSRSScheduler, Rating


class TestModes:
    def test_auto_selection_boundary(self) -> None:
        assert LONGTERM_THRESHOLD_DAYS == 21
        assert isinstance(select_policy(21), LongTermPolicy)
        assert isinstance(select_policy(90), LongTermPolicy)
        assert isinstance(select_policy(20), CramPolicy)
        assert isinstance(select_policy(0), CramPolicy)
        assert isinstance(select_policy(None), LongTermPolicy)

    def test_overrides(self) -> None:
        assert select_policy(2, "longterm").name == "longterm"
        assert select_policy(90, "cram").name == "cram"
        assert select_policy(5, "auto").name == "cram"
        with pytest.raises(ValueError):
            select_policy(5, "nonsense")

    def test_policies_differ_on_teaching_knobs(self) -> None:
        long_term, cram = LongTermPolicy(), CramPolicy()
        assert long_term.quiz_intensity < cram.quiz_intensity
        assert long_term.advance_mastery > cram.advance_mastery
        assert long_term.target_retention > cram.target_retention
        assert long_term.daily_new_limit < cram.daily_new_limit
        assert long_term.review_share > cram.review_share
        assert long_term.can_advance(0.85) and not cram.can_advance(0.5)


class TestBKT:
    def test_correct_raises_and_wrong_lowers_mastery(self) -> None:
        model = BKTModel()
        prior = model.mastery("ch1")
        after_right = model.observe("ch1", True)
        assert after_right > prior
        after_wrong = model.observe("ch1", False)
        assert after_wrong < after_right
        assert 0.0 < after_wrong < 1.0

    def test_repeated_evidence_converges(self) -> None:
        model = BKTModel()
        for _ in range(12):
            model.observe("ch1", True)
        assert model.mastery("ch1") > 0.9
        other = BKTModel()
        for _ in range(12):
            other.observe("ch2", False)
        assert other.mastery("ch2") < 0.2

    def test_counts_and_roundtrip(self) -> None:
        model = BKTModel(BKTParams(p_init=0.4))
        model.observe("ch1", True)
        model.observe("ch1", False)
        state = model.get("ch1")
        assert state.attempts == 2 and state.correct == 1
        rebuilt = BKTModel.from_dict(model.to_dict())
        assert rebuilt.mastery("ch1") == pytest.approx(model.mastery("ch1"))
        assert rebuilt.params.p_init == 0.4

    def test_invalid_parameters_rejected(self) -> None:
        with pytest.raises(ValueError):
            BKTParams(p_guess=1.0)
        with pytest.raises(ValueError):
            BKTParams(p_slip=-0.1)

    def test_reference_posterior_values(self) -> None:
        # Defaults p_init=.3, p_learn=.15, p_guess=.2, p_slip=.1:
        # correct → .3·.9/(.3·.9 + .7·.2) then + (1-p)·.15
        assert BKTModel().observe("ch1", True) == pytest.approx(0.7097560975609756, rel=1e-12)
        # wrong → .3·.1/(.3·.1 + .7·.8) then + (1-p)·.15
        assert BKTModel().observe("ch1", False) == pytest.approx(0.19322033898305085, rel=1e-12)


class TestFSRS:
    def test_initial_stability_orders_ratings(self) -> None:
        fsrs = FSRS()
        stabilities = [fsrs.initial_stability(r) for r in (1, 2, 3, 4)]
        assert stabilities == sorted(stabilities)
        assert len(set(stabilities)) == 4
        difficulties = [fsrs.initial_difficulty(r) for r in (1, 2, 3, 4)]
        assert difficulties == sorted(difficulties, reverse=True)

    def test_interval_is_monotonic_in_stability(self) -> None:
        fsrs = FSRS()
        intervals = [fsrs.next_interval(s) for s in (0.5, 1, 3, 10, 30, 100)]
        assert intervals == sorted(intervals)
        assert intervals[-1] > intervals[0]

    def test_higher_target_retention_shortens_intervals(self) -> None:
        fsrs = FSRS()
        calm = fsrs.next_interval(30, desired_retention=0.8)
        strict = fsrs.next_interval(30, desired_retention=0.95)
        assert strict < calm

    def test_retrievability_decays_with_time(self) -> None:
        fsrs = FSRS()
        values = [fsrs.retrievability(days, 10) for days in (0, 1, 5, 30, 365)]
        assert values[0] == pytest.approx(1.0)
        assert values == sorted(values, reverse=True)

    def test_repeated_good_reviews_grow_the_interval(self) -> None:
        fsrs = FSRS()
        card = Card(key="q1", due="2026-01-01", stability=0.0, difficulty=0.0)
        today = date(2026, 1, 1)
        intervals = []
        for _ in range(4):
            outcome = fsrs.review(card, Rating.GOOD, today)
            intervals.append(outcome.interval_days)
            card = outcome.card
            today = date.fromisoformat(card.due)
        assert intervals == sorted(intervals)
        assert intervals[-1] > intervals[0]

    def test_again_resets_interval_and_counts_a_lapse(self) -> None:
        fsrs = FSRS()
        card = Card(key="q1", due="2026-01-01", stability=0.0, difficulty=0.0)
        learned = fsrs.review(card, Rating.GOOD, date(2026, 1, 1)).card
        lapsed = fsrs.review(learned, Rating.AGAIN, date(2026, 1, 5))
        assert lapsed.interval_days == 1
        assert lapsed.card.lapses == 1
        assert lapsed.card.state == "relearning"
        assert lapsed.card.stability < learned.stability

    def test_easy_interval_beats_good_for_a_new_card(self) -> None:
        fsrs = FSRS()
        base = Card(key="q1", due="2026-01-01", stability=0.0, difficulty=0.0)
        good = fsrs.review(base, Rating.GOOD, date(2026, 1, 1)).interval_days
        easy = fsrs.review(base, Rating.EASY, date(2026, 1, 1)).interval_days
        hard = fsrs.review(base, Rating.HARD, date(2026, 1, 1)).interval_days
        assert hard <= good < easy

    def test_scheduler_store_due_and_roundtrip(self) -> None:
        scheduler = FSRSScheduler()
        today = date(2026, 1, 1)
        outcome = scheduler.review("q1", Rating.GOOD, today)
        assert scheduler.due(today) == []
        assert [c.key for c in scheduler.due(date.fromisoformat(outcome.card.due))] == ["q1"]
        rebuilt = FSRSScheduler.from_dict(scheduler.to_dict())
        assert rebuilt.cards["q1"].due == outcome.card.due
        assert rebuilt.cards["q1"].stability == pytest.approx(outcome.card.stability)

    def test_invalid_rating_rejected(self) -> None:
        fsrs = FSRS()
        card = Card(key="q", due="2026-01-01", stability=0.0, difficulty=0.0)
        with pytest.raises(ValueError):
            fsrs.review(card, 5, date(2026, 1, 1))


class TestFSRSReferenceVectors:
    """FSRS-4.5 public reference values (hard-coded from the published formula).

    These pin the actual coefficients: a monotonic-but-wrong implementation used
    to pass the earlier tests.  S/D are compared to 1e-9 relative, intervals
    exactly.
    """

    def test_first_review_matches_fsrs45_defaults(self) -> None:
        fsrs = FSRS()
        today = date(2026, 1, 1)
        cases = (
            (Rating.AGAIN, 0.4872, 7.6214, 1),
            (Rating.HARD, 1.4003, 6.3916, 1),
            (Rating.GOOD, 3.7145, 5.1618, 4),
            (Rating.EASY, 13.8206, 3.932, 14),
        )
        for rating, stability, difficulty, interval in cases:
            outcome = fsrs.review(Card(key="q", due="2026-01-01", stability=0.0, difficulty=0.0),
                                  rating, today)
            assert outcome.card.stability == pytest.approx(stability, rel=1e-9), rating
            assert outcome.card.difficulty == pytest.approx(difficulty, rel=1e-9), rating
            assert outcome.interval_days == interval, rating

    def test_second_review_matches_fsrs45_defaults(self) -> None:
        fsrs = FSRS()
        first = fsrs.review(Card(key="q", due="2026-01-01", stability=0.0, difficulty=0.0),
                            Rating.GOOD, date(2026, 1, 1)).card
        again = fsrs.review(first, Rating.AGAIN, date(2026, 1, 5))
        assert again.retrievability == pytest.approx(0.8934995006528037, rel=1e-12)
        assert again.card.stability == pytest.approx(1.4332344897795595, rel=1e-9)
        assert again.card.difficulty == pytest.approx(6.8630312, rel=1e-9)
        assert again.interval_days == 1 and again.card.lapses == 1

        good = fsrs.review(first, Rating.GOOD, date(2026, 1, 5))
        assert good.card.stability == pytest.approx(14.808100506496405, rel=1e-9)
        assert good.card.difficulty == pytest.approx(5.1236762, rel=1e-9)
        assert good.interval_days == 15
        # The interval is re-solved at the requested retention: at 0.8 it is longer.
        assert fsrs.next_interval(good.card.stability, desired_retention=0.8) == 36

    def test_custom_weights_survive_serialization(self) -> None:
        custom = tuple(value * 1.5 for value in FSRS().w)
        scheduler = FSRSScheduler(FSRS(weights=custom, desired_retention=0.8))
        scheduler.review("q1", Rating.GOOD, date(2026, 1, 1))
        data = scheduler.to_dict()
        assert data["weights"] == list(custom)
        rebuilt = FSRSScheduler.from_dict(data)
        assert rebuilt.fsrs.w == custom
        assert rebuilt.fsrs.desired_retention == pytest.approx(0.8)

    def test_short_stored_weight_list_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            FSRSScheduler.from_dict({"weights": [1.0, 2.0, 3.0], "cards": {}})
