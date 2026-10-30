# -*- coding: utf-8 -*-
"""P3 acceptance tests for the four-layer offline benchmark suite (post-review)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from benchmarks import behavior, grounding, tutor
from benchmarks.common import (
    DATA_DIR,
    REPO_ROOT,
    RESULTS_DIR,
    copy_sample_course,
    material_pages,
    read_json,
    tolerance_equal,
)
from benchmarks.report import (
    P3_END,
    P3_START,
    README_EN_END,
    README_EN_START,
    README_ZH_END,
    README_ZH_START,
    p3_snapshot,
    render_report,
    summary_table,
    sync_docs,
)
from benchmarks.run_all import run_all


# ---------------------------------------------------------------- grounding
class _Hit:
    def __init__(self, text: str, file: str = "course.md", page: int = 1) -> None:
        self.text = text
        self.file = file
        self.page = page


class _Ask:
    def __init__(self, hits: list[_Hit]) -> None:
        self.hits = hits


class _FakeService:
    def __init__(self, hits: list[_Hit]) -> None:
        self.hits = hits

    def ask(self, workspace: str, query: str, k: int = 5, chars: int = 700) -> _Ask:
        return _Ask(self.hits)


def test_agent_reader_receives_only_the_query_string() -> None:
    signature = inspect.signature(grounding._agent_answer)
    assert "question" not in signature.parameters
    assert list(signature.parameters)[0] == "query"
    source = inspect.getsource(grounding._agent_answer)
    for forbidden in ("answer_terms", "evidence_keys", "gold", "question["):
        assert forbidden not in source, forbidden
    for reader in (grounding._closed_book_answer, grounding._raw_file_answer):
        assert "question" not in inspect.signature(reader).parameters


def test_probe_refusal_is_evidence_driven_not_retrieval_empty() -> None:
    query = "Dijkstra 算法的时间复杂度是多少？"
    supporting = grounding._agent_answer(
        query, _FakeService([_Hit("Dijkstra 算法的时间复杂度是 O(n log n)。")]), "ws")
    assert supporting.answered is True
    assert supporting.refused is False

    unsupported = grounding._agent_answer(
        query, _FakeService([_Hit("顺序表按位序访问第 i 个元素：O(1)。")]), "ws")
    assert unsupported.retrieval_hits == 1, "the hit list is not empty"
    assert unsupported.refused is True, "refusal must come from the support check"

    material = grounding._agent_answer(
        "栈的特点是什么？",
        _FakeService([_Hit("栈的特点是后进先出（LIFO）。")]), "ws")
    assert material.answered is True


def test_grounding_support_threshold_separates_material_from_probes(tmp_path: Path) -> None:
    section = grounding.run(tmp_path / "g")
    calibration = section["support_calibration"]
    assert calibration["in_material_agent_coverage_min"] > calibration["min_coverage"]
    assert calibration["probe_agent_coverage_max"] < calibration["min_coverage"]
    assert calibration["min_contiguous_overlap"] >= 1
    assert section["arms"]["agent"]["probe_refusals"] == section["arms"]["agent"]["probes_total"]
    assert section["arms"]["agent"]["probes_with_retrieval_hits"] > 0
    assert section["arms"]["agent"]["page_citation_verified"] == \
        section["arms"]["agent"]["page_citation_attempts"]
    assert section["arms"]["closed_book"]["citation_accuracy_pct"] is None
    assert section["arms"]["raw_file"]["page_citation_attempts"] == 0
    assert section["arms"]["agent"]["material_answer_accuracy_pct"] > \
        section["arms"]["closed_book"]["material_answer_accuracy_pct"]


def test_gold_qa_answers_are_material_verifiable(tmp_path: Path) -> None:
    data = read_json(DATA_DIR / "grounding_qa.json")
    course = copy_sample_course(tmp_path / "course")
    text = "\n".join(str(page["text"]) for page in material_pages(course))

    def norm(value: str) -> str:
        return re.sub(r"\s+", "", value.replace("−", "-").replace("–", "-"))

    for question in data["questions"]:
        if question["out_of_material"]:
            assert question["answer_terms"] == []
            continue
        assert question["answer_terms"], question["id"]
        assert question["evidence_keys"], question["id"]
        for term in question["answer_terms"]:
            assert norm(term) in norm(text), (question["id"], term)


def test_grounding_three_arms_share_the_same_scorer() -> None:
    source = inspect.getsource(grounding.run)
    assert source.count("_score(question, arm_answer, pages)") == 1
    source_reader = inspect.getsource(grounding)
    assert "if question[" not in source_reader
    assert not re.search(r'["\'](?:g|p)\d{2}["\']', source_reader)


# ---------------------------------------------------------------- behavior
def test_induced_attacks_travel_through_the_real_loop_with_material(tmp_path: Path) -> None:
    section = behavior.run(tmp_path / "b")
    summary = section["summary"]
    induced = [case for case in section["attacks"] if case["channel"] == "induced"]
    assert summary["induced_attack_total"] >= 8
    assert summary["induced_attack_blocked"] == summary["induced_attack_total"]
    assert summary["material_in_context_all_induced"] is True
    assert summary["action_from_material_all_induced"] is True
    for case in induced:
        assert case["metrics"]["material_in_context"] is True
        assert case["metrics"]["action_from_material"] is True
        assert case["metrics"]["tool"]
        assert case["metrics"]["code"] == case["metrics"]["expected_code"]
    assert summary["supplementary_total"] >= 3
    assert summary["normal_total"] >= 10
    assert summary["negative_control_total"] == 3
    assert summary["normal_passed"] == summary["normal_total"]
    assert summary["negative_control_passed"] == summary["negative_control_total"]
    assert summary["invariant_passed"] == summary["invariant_total"]
    footer = next(case for case in section["invariants"]
                  if case["id"] == "inv_footer_all_teaching_commands")
    assert footer["passed"] is True
    assert "normal_done_gate" in footer["metrics"]["commands"]
    assert "normal_unknown_question" not in footer["metrics"]["commands"]


# ---------------------------------------------------------------- tutor
def test_tutor_medium_and_bypass_samples_are_not_full_score(tmp_path: Path) -> None:
    section = tutor.run(tmp_path / "t")
    assert section["good_mean_score"] == 1.0
    assert section["medium_mean_score"] == 0.75
    assert section["bypass_full_score_count"] == 0
    assert section["bypass_full_score_ids"] == []
    assert section["flawed_mean_score"] < section["medium_mean_score"] < section["good_mean_score"]
    assert section["discrimination"] > 0.5
    medium = [item for item in section["scenarios"] if item["quality"] == "medium"]
    assert medium and all(sum(not value for value in item["signals"].values()) == 1
                          for item in medium), [(item["id"], item["signals"]) for item in medium]
    bypass = [item for item in section["scenarios"] if item["quality"] == "bypass"]
    assert bypass and all(item["score"] < 1.0 for item in bypass)
    assert all(item["evidence"]["accepted_next_commands"] == [] or
               item["score"] < 1.0 for item in bypass)
    assert section["citation_support_pct"] is not None
    assert section["citation_validity_pct"] is not None


# ---------------------------------------------------------------- runner
def test_run_all_writes_consistent_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    results = run_all(out_dir=out, seed=20261030, performance=True)

    for name in ("results.json", "BENCH_REPORT.md", "summary.md"):
        assert (out / name).exists(), name
    loaded = read_json(out / "results.json")
    assert loaded["schema_version"] == 1
    assert loaded["meta"]["seed"] == 20261030
    assert set(loaded["layers"]) == {"grounding", "behavior", "longhorizon", "tutor", "performance"}

    grounding_section = loaded["layers"]["grounding"]
    assert grounding_section["gold_set"]["in_material"] >= 30
    assert grounding_section["gold_set"]["out_of_material_probes"] >= 8
    for arm in ("closed_book", "raw_file", "agent"):
        assert arm in grounding_section["arms"]

    behavior_section = loaded["layers"]["behavior"]["summary"]
    assert behavior_section["induced_attack_blocked"] == behavior_section["induced_attack_total"]
    assert behavior_section["normal_passed"] == behavior_section["normal_total"]
    assert behavior_section["negative_control_passed"] == behavior_section["negative_control_total"]

    longhorizon = loaded["layers"]["longhorizon"]
    assert longhorizon["seed"] == 20261030
    assert longhorizon["integrity"]["hidden_state_isolated"] is True
    assert longhorizon["integrity"]["hidden_state_isolated"] == \
        longhorizon["integrity"]["isolation"]["all_passed"]
    assert longhorizon["protocol"]["same_time_budget_for_both_arms"] is True
    for arm in longhorizon["arms"].values():
        assert arm["available_budget_minutes"] == longhorizon["protocol"]["available_budget_minutes"]
        assert arm["pretest_retention_pct"] == 0.0
        for key in ("delayed_retention_pct", "learning_gain_pct_points",
                    "gain_per_study_hour", "gain_per_budget_hour"):
            assert key in arm
    assert "not real-human effects" in longhorizon["caveat"]

    tutor_section = loaded["layers"]["tutor"]
    assert tutor_section["scenario_count"] >= 12
    assert set(tutor_section["categories"]) == {"concept", "error_correction", "cram_sprint"}
    assert all(item["count"] >= 3 for item in tutor_section["categories"].values())
    assert tutor_section["judge"]["enabled"] is False
    assert tutor_section["bypass_full_score_count"] == 0

    performance = loaded["layers"]["performance"]
    assert performance["deterministic"] is False
    assert performance["ingest"]["serial"]["median_seconds"] > 0

    assert (out / "BENCH_REPORT.md").read_text(encoding="utf-8") == render_report(loaded)
    assert (out / "summary.md").read_text(encoding="utf-8") == summary_table(loaded)
    report_text = (out / "BENCH_REPORT.md").read_text(encoding="utf-8")
    assert "模拟结论 ≠ 真人效果" in report_text
    assert results["meta"]["generated_at"] == loaded["meta"]["generated_at"]


def test_same_seed_repeats_the_deterministic_layers(tmp_path: Path) -> None:
    first = run_all(out_dir=tmp_path / "a", seed=20261030, performance=False)
    second = run_all(out_dir=tmp_path / "b", seed=20261030, performance=False)
    for layer in ("grounding", "behavior", "longhorizon", "tutor"):
        assert tolerance_equal(first["layers"][layer], second["layers"][layer]), layer
    assert first["layers"]["performance"]["skipped"] is True


def test_judge_is_skipped_without_an_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    section = tutor.run(tmp_path / "tutor", judge=True)
    assert section["judge"]["enabled"] is False
    assert section["judge"]["reason"] in ("no_api_key",)


def test_judge_interface_scores_with_an_injected_offline_provider(tmp_path: Path) -> None:
    from examprep_agent.core.provider import FakeProvider

    payload = ('{"cites_material": 1, "tests_first": 1, '
               '"gives_next_step": 1, "no_fabrication": 1}')
    section = tutor.run(tmp_path / "tutor", judge=True,
                        provider=FakeProvider([payload] * 16))
    assert section["judge"]["enabled"] is True
    assert len(section["judge"]["scores"]) == section["scenario_count"]
    assert section["judge"]["mean_score"] == 1.0


def test_judge_provider_is_resolved_before_the_sandbox(monkeypatch) -> None:
    from benchmarks import run_all as run_all_module

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real")
    provider = run_all_module._judge_provider(True, None)
    assert provider is not None
    assert provider.config.api_key == "sk-test-not-real"
    assert run_all_module._judge_provider(False, None) is None


def test_run_all_judge_uses_an_injected_provider(tmp_path: Path) -> None:
    from examprep_agent.core.provider import FakeProvider

    payload = ('{"cites_material": 1, "tests_first": 1, '
               '"gives_next_step": 1, "no_fabrication": 1}')
    results = run_all(out_dir=tmp_path / "out", seed=20261030, performance=False,
                      judge=True, judge_provider=FakeProvider([payload] * 16))
    judge = results["layers"]["tutor"]["judge"]
    assert judge["enabled"] is True
    assert len(judge["scores"]) == results["layers"]["tutor"]["scenario_count"]
    assert judge["mean_score"] == 1.0


# ---------------------------------------------------------------- docs
def _extract_between(text: str, start: str, end: str) -> str:
    left = text.index(start)
    right = text.index(end)
    return text[left + len(start):right].strip()


def test_committed_docs_match_committed_results() -> None:
    results = read_json(RESULTS_DIR / "results.json")
    assert (RESULTS_DIR / "BENCH_REPORT.md").read_text(encoding="utf-8") == \
        render_report(results)
    assert (RESULTS_DIR / "summary.md").read_text(encoding="utf-8") == \
        summary_table(results)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert _extract_between(readme, README_EN_START, README_EN_END) == \
        summary_table(results, "en").strip()
    assert _extract_between(readme, README_ZH_START, README_ZH_END) == \
        summary_table(results, "zh").strip()

    p3 = (REPO_ROOT / "P3_REPORT.md").read_text(encoding="utf-8")
    assert _extract_between(p3, P3_START, P3_END) == p3_snapshot(results).strip()


def test_sync_docs_roundtrip(tmp_path: Path) -> None:
    results = read_json(RESULTS_DIR / "results.json")
    readme = tmp_path / "README.md"
    p3 = tmp_path / "P3_REPORT.md"
    readme.write_text("%s\n%s\n%s\n%s\n" % (README_EN_START, README_EN_END,
                                            README_ZH_START, README_ZH_END),
                      encoding="utf-8")
    p3.write_text("%s\n%s\n" % (P3_START, P3_END), encoding="utf-8")
    sync_docs(results, readme_path=readme, p3_path=p3)
    readme_text = readme.read_text(encoding="utf-8")
    assert _extract_between(readme_text, README_EN_START, README_EN_END) == \
        summary_table(results, "en").strip()
    assert _extract_between(readme_text, README_ZH_START, README_ZH_END) == \
        summary_table(results, "zh").strip()
    p3_text = p3.read_text(encoding="utf-8")
    assert _extract_between(p3_text, P3_START, P3_END) == p3_snapshot(results).strip()
