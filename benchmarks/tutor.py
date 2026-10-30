# -*- coding: utf-8 -*-
"""TutorRubric: deterministic, verifiable rubric signals on fixed transcripts.

P3 review fixes (I6):

* **cites_material** is now content-verifiable: every explanation carries a
  citation, every citation resolves to a real page, at least one cited page
  contains the scenario ``evidence_keys``, and the explanation itself states
  every scenario ``claim_term``;
* **tests_first** requires a diagnostic *before* the first explanation, the
  diagnostic must contain a scenario ``quiz_keyword``, and a student response
  must occur before teaching;
* **gives_next_step** requires a syntactically executable ``examprep`` command
  whose qids/chapters exist in the workspace bank; negation markers reject the
  step;
* **no_fabrication** rejects scenario-forbidden claims, fake citations and a
  global exam-gaming phrase list (必考 / 包过 / 背下来就行 / 不用理解 …).

The fixture adds four *medium* single-signal ablation samples and three
exam-style *bypass* samples; ``bypass_full_score_count`` must stay zero.  The
offline run is deterministic.  ``--judge`` is optional and skipped without a
provider/key.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import DATA_DIR, copy_sample_course, material_pages, page_text, pct, read_json

CITATION_RE = re.compile(
    r"([^\s\[\]|，。；：（）()]+\.(?:md|markdown|txt|pdf|docx|pptx|html))\s+p\.(\d+)",
    re.I,
)
FACTUAL_ACTIONS = {"explain", "answer", "feedback"}
QUIZ_ACTIONS = {"quiz", "diagnostic", "test", "probe"}
SIGNALS = ("cites_material", "tests_first", "gives_next_step", "no_fabrication")
COMMAND_RE = re.compile(r"examprep\s+([a-z][a-z0-9_-]*)([^\n]*)")
NEGATIVE_MARKERS = ("不要执行", "不要运行", "别执行", "do not run", "don't run", "never run")
#: Exam-style shortcuts that are pedagogically unsound even when the citation
#: is real.  Applied globally, not per scenario.
EXAM_GAMING_PATTERNS = (
    "必考", "包过", "押题", "背下来就行", "不用理解", "死记硬背",
    "考试就选", "记住结论就行", "直接背答案",
)
TEACHING_VERBS = {
    "status", "next", "ask", "quiz", "check", "grade", "answer", "done",
    "note", "mistakes", "cheatsheet", "plan", "goto", "graph",
}
#: Flags accepted after a teaching verb; ``--workspace`` is allowed everywhere.
COMMON_FLAGS = {"--workspace"}
FLAG_VALUES = {
    "--workspace", "-n", "--chapter", "--days", "--date", "--type", "--out",
    "--lang", "--mode", "--profile", "--slice", "--workers", "--name",
}
BOOL_FLAGS = {
    "--all", "--review", "--answers", "--force", "--fresh", "--read-only",
    "--restart", "--rebuild",
}


def _citations(text: str) -> list[tuple[str, int]]:
    return [(match.group(1), int(match.group(2))) for match in CITATION_RE.finditer(text)]


def _normalize(text: str) -> str:
    """Whitespace-free comparison form with unified punctuation/dashes."""
    table = str.maketrans({"−": "-", "–": "-", "（": "(", "）": ")",
                           "，": ",", "：": ":", "；": ";", "“": '"', "”": '"'})
    return re.sub(r"\s+", "", str(text).translate(table)).casefold()


def _is_explanation(step: Mapping[str, Any]) -> bool:
    """A tutor explanation step; student answers are not explanations."""
    return (str(step.get("action")) in FACTUAL_ACTIONS
            and str(step.get("actor", "agent")) != "student")


def _agent_text(scenario: Mapping[str, Any]) -> str:
    """Only the tutor's messages; student errors must not count as fabrication."""
    return "\n".join(str(step.get("text", "")) for step in scenario.get("transcript") or []
                     if str(step.get("actor", "agent")) != "student")


def _split_command_tokens(rest: str) -> list[str]:
    """Tokenise the tail of an ``examprep`` command without shell expansion."""
    rest = rest.strip().rstrip("。.!！?？)")
    return rest.split() if rest else []


def _validate_command(verb: str, rest: str, valid_qids: set[str],
                      valid_chapters: set[int]) -> bool:
    """Whether one ``examprep <verb> ...`` occurrence is executable as written."""
    if verb not in TEACHING_VERBS:
        return False
    tokens = _split_command_tokens(rest)
    index = 0

    def take_value() -> str | None:
        nonlocal index
        if index + 1 >= len(tokens):
            return None
        value = tokens[index + 1]
        index += 2
        return value

    if verb in ("next", "status", "done", "cheatsheet", "graph", "mistakes", "plan"):
        while index < len(tokens):
            token = tokens[index]
            if token in BOOL_FLAGS or token in COMMON_FLAGS:
                if token == "--workspace":
                    if take_value() is None:
                        return False
                else:
                    index += 1
                continue
            if token in FLAG_VALUES and token in ("--days", "--date", "--out"):
                if take_value() is None:
                    return False
                continue
            return False
        return True
    if verb == "quiz":
        while index < len(tokens):
            token = tokens[index]
            if token in ("--all", "--review"):
                index += 1
                continue
            if token in ("-n", "--chapter", "--workspace"):
                value = take_value()
                if value is None:
                    return False
                if token == "-n" and (not value.isdigit() or int(value) < 1):
                    return False
                if token == "--chapter" and not value.isdigit():
                    return False
                continue
            return False
        return True
    if verb == "check":
        qid = None
        while index < len(tokens):
            token = tokens[index]
            if token == "--workspace":
                if take_value() is None:
                    return False
                continue
            if qid is None and not token.startswith("-"):
                qid = token
                index += 1
                continue
            return False
        return bool(qid and qid in valid_qids)
    if verb in ("answer", "grade"):
        qid, result = None, None
        while index < len(tokens):
            token = tokens[index]
            if token == "--workspace":
                if take_value() is None:
                    return False
                continue
            if qid is None and not token.startswith("-"):
                qid = token
                index += 1
                continue
            if result is None:
                result = token
                index += 1
                continue
            result += " " + token  # a free-text grade reply
            index += 1
        if verb == "answer":
            return bool(qid in valid_qids and result in ("right", "wrong", "skip"))
        return bool(qid in valid_qids and result)
    if verb == "goto":
        chapter = None
        while index < len(tokens):
            token = tokens[index]
            if token == "--workspace":
                if take_value() is None:
                    return False
                continue
            if token in ("--force", "--restart"):
                index += 1
                continue
            if chapter is None and token.isdigit():
                chapter = int(token)
                index += 1
                continue
            return False
        return chapter in valid_chapters
    if verb == "note":
        note_type = "note"
        text_tokens: list[str] = []
        while index < len(tokens):
            token = tokens[index]
            if token == "--workspace":
                if take_value() is None:
                    return False
                continue
            if token == "--type":
                value = take_value()
                if value not in ("summary", "confusion", "note"):
                    return False
                note_type = value
                continue
            text_tokens.append(token)
            index += 1
        return note_type in ("summary", "confusion", "note") and bool(text_tokens)
    if verb == "ask":
        query: list[str] = []
        while index < len(tokens):
            token = tokens[index]
            if token == "--workspace":
                if take_value() is None:
                    return False
                continue
            query.append(token)
            index += 1
        return bool(query)
    return False


def _next_step(text: str, valid_qids: set[str],
               valid_chapters: set[int]) -> tuple[bool, list[str]]:
    """Validate the final tutor message's concrete next command."""
    if any(marker in text.lower() for marker in NEGATIVE_MARKERS):
        return False, []
    accepted: list[str] = []
    for match in COMMAND_RE.finditer(text):
        verb, rest = match.group(1), match.group(2)
        if _validate_command(verb, rest, valid_qids, valid_chapters):
            accepted.append(("examprep %s%s" % (verb, rest)).strip())
    return bool(accepted), accepted


def score_scenario(scenario: Mapping[str, Any], pages: Sequence[dict[str, Any]],
                   valid_qids: set[str],
                   valid_chapters: set[int]) -> dict[str, Any]:
    """Apply the four verifiable signals to one scenario transcript."""
    transcript = list(scenario.get("transcript") or [])
    explain_steps = [step for step in transcript if _is_explanation(step)]
    tutor_text = _agent_text(scenario)
    citations: list[tuple[str, int]] = []
    for step in explain_steps:
        citations.extend(_citations(str(step.get("text", ""))))
    valid = [pair for pair in citations if page_text(pages, pair[0], pair[1]) is not None]
    evidence_keys = [str(key) for key in (scenario.get("evidence_keys") or [])]
    key_support = bool(evidence_keys) and any(
        all(_normalize(key) in _normalize(page_text(pages, file, page) or "")
            for key in evidence_keys)
        for file, page in valid
    )
    claim_terms = [str(term) for term in (scenario.get("claim_terms") or [])]
    explanation_text = "\n".join(str(step.get("text", "")) for step in explain_steps)
    missing_claims = [term for term in claim_terms
                      if _normalize(term) not in _normalize(explanation_text)]
    claim_support = bool(claim_terms) and not missing_claims
    every_explanation_cited = bool(explain_steps) and all(
        bool(_citations(str(step.get("text", "")))) for step in explain_steps)
    all_citations_valid = bool(citations) and len(valid) == len(citations)
    cites_material = (every_explanation_cited and all_citations_valid
                      and key_support and claim_support)

    first_explain = next((index for index, step in enumerate(transcript)
                          if _is_explanation(step)), None)
    before_explain = transcript[:first_explain if first_explain is not None else len(transcript)]
    quiz_steps = [step for step in before_explain
                  if str(step.get("action")) in QUIZ_ACTIONS]
    quiz_keywords = [str(key) for key in (scenario.get("quiz_keywords") or [])]
    quiz_relevant = any(
        _normalize(keyword) in _normalize(str(step.get("text", "")))
        for step in quiz_steps for keyword in quiz_keywords
    )
    quiz_index = next((index for index, step in enumerate(transcript)
                       if str(step.get("action")) in QUIZ_ACTIONS), None)
    student_answered = bool(
        quiz_index is not None
        and any(str(step.get("actor")) == "student"
                and str(step.get("action")) in ("answer", "response")
                for step in transcript[quiz_index + 1:first_explain
                                       if first_explain is not None else len(transcript)])
    )
    tests_first = bool(quiz_relevant and student_answered)

    last_agent = next((str(step.get("text", "")) for step in reversed(transcript)
                       if str(step.get("actor", "agent")) == "agent"), "")
    gives_next_step, accepted_commands = _next_step(
        last_agent, valid_qids, valid_chapters)

    forbidden = [str(item) for item in (scenario.get("forbidden") or [])]
    forbidden_hits = [item for item in forbidden if item in tutor_text]
    gaming_hits = [pattern for pattern in EXAM_GAMING_PATTERNS if pattern in tutor_text]
    citation_sane = len(valid) == len(citations)
    no_fabrication = not forbidden_hits and not gaming_hits and citation_sane

    signals = {
        "cites_material": bool(cites_material),
        "tests_first": bool(tests_first),
        "gives_next_step": bool(gives_next_step),
        "no_fabrication": bool(no_fabrication),
    }
    support_citations = [pair for pair in citations if evidence_keys]
    support_ok = [pair for pair in support_citations
                  if page_text(pages, pair[0], pair[1]) is not None
                  and all(_normalize(key) in _normalize(
                      page_text(pages, pair[0], pair[1]) or "") for key in evidence_keys)]
    return {
        "id": scenario["id"],
        "category": scenario["category"],
        "quality": scenario.get("quality", "unknown"),
        "bypass": bool(scenario.get("bypass")),
        "title": scenario.get("title", ""),
        "signals": signals,
        "score": round(sum(signals.values()) / len(signals), 6),
        "evidence": {
            "explanation_steps": len(explain_steps),
            "citations": [list(pair) for pair in citations],
            "valid_citations": [list(pair) for pair in valid],
            "key_support": key_support,
            "support_citations": len(support_citations),
            "support_citations_valid": len(support_ok),
            "claim_terms": claim_terms,
            "missing_claim_terms": missing_claims,
            "quiz_keywords": quiz_keywords,
            "quiz_relevant": quiz_relevant,
            "student_answered": student_answered,
            "accepted_next_commands": accepted_commands,
            "forbidden_hits": forbidden_hits,
            "gaming_hits": gaming_hits,
            "last_agent_message": last_agent,
        },
        "citation_validity_pct": pct(len(valid), len(citations)),
        "citation_support_pct": pct(len(support_ok), len(support_citations)),
    }


def _judge_prompt(scenario: Mapping[str, Any]) -> str:
    transcript = "\n".join(
        "%s/%s: %s" % (step.get("actor", "agent"), step.get("action", ""), step.get("text", ""))
        for step in scenario.get("transcript") or []
    )
    return (
        "Score this tutor transcript on four binary signals: cites_material "
        "(page-verifiable citations that actually state the claim), tests_first "
        "(a relevant quiz/diagnostic before the first explanation, with a student "
        "response), gives_next_step (a concrete executable examprep command), "
        "no_fabrication (no unsupported claim / fake citation / exam-gaming shortcut).\n"
        "Return ONLY a JSON object with those four keys set to 0 or 1.\n\n"
        "Scenario: %s\n%s\n" % (scenario.get("title", ""), transcript)
    )


def _parse_judge_json(text: str) -> dict[str, int] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {signal: 1 if payload.get(signal) in (1, True, "1", "true") else 0
            for signal in SIGNALS}


def _judge(requested: bool, scenarios: Sequence[Mapping[str, Any]],
           provider: Any | None = None) -> dict[str, Any]:
    """Optional LLM judge; skipped offline or without a provider."""
    if not requested:
        return {
            "enabled": False,
            "reason": "flag_not_set",
            "note": "deterministic rubric only; pass --judge (with an API key) to enable LLM scoring",
        }
    if provider is None:
        from examprep_agent.core.provider import provider_from_env

        provider = provider_from_env()
    if provider is None:
        return {
            "enabled": False,
            "reason": "no_api_key",
            "note": ("no API key is visible to the judge; run_all passes the caller's "
                     "provider before entering the offline sandbox"),
        }
    from examprep_agent.core.provider import ChatMessage

    scores: list[dict[str, Any]] = []
    for scenario in scenarios:
        try:
            response = provider.complete([
                ChatMessage(role="system",
                            content="You are a strict, deterministic tutor evaluator. "
                                    "Return JSON only; do not add prose."),
                ChatMessage(role="user", content=_judge_prompt(scenario)),
            ])
            parsed = _parse_judge_json(str(getattr(response, "content", "")))
            if parsed is None:
                raise ValueError("judge returned no parseable JSON")
            scores.append({"id": scenario["id"], "signals": parsed,
                           "score": round(sum(parsed.values()) / len(parsed), 6)})
        except Exception as exc:  # one bad judge call must not abort the run
            scores.append({"id": scenario["id"], "error": "%s: %s" % (type(exc).__name__, exc)})
    valid = [item for item in scores if "score" in item]
    return {
        "enabled": True,
        "provider": getattr(provider, "name", "unknown"),
        "scores": scores,
        "mean_score": round(sum(item["score"] for item in valid) / max(1, len(valid)), 6),
        "note": "LLM judge requested; deterministic scores remain the offline reference",
    }


def run(root: str | Path, *, judge: bool = False, provider: Any | None = None) -> dict[str, Any]:
    """Run TutorRubric and return its results section."""
    from examprep_agent.service.models import SetupRequest
    from examprep_agent.service.study import StudyService

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    course = copy_sample_course(root / "tutor-course")
    pages = material_pages(course)
    workspace = str(root / "tutor-workspace")
    service = StudyService(pointer=str(root / "pointer"))
    service.setup(SetupRequest(materials=str(course), workspace=workspace,
                               lang="zh", days=30, workers=1))
    bank = service.load_bank(workspace)
    valid_qids = {str(question["id"]) for question in bank}
    valid_chapters = {int(question.get("chapter") or 0) for question in bank}
    data = read_json(DATA_DIR / "tutor_scenarios.json")
    scenarios = list(data["scenarios"])
    scored = [score_scenario(scenario, pages, valid_qids, valid_chapters)
              for scenario in scenarios]

    by_category: dict[str, list[dict[str, Any]]] = {}
    for item in scored:
        by_category.setdefault(item["category"], []).append(item)
    category_summary = {
        category: {
            "count": len(items),
            "mean_score": round(sum(item["score"] for item in items) / len(items), 6),
            "signal_pass_pct": {
                signal: pct(sum(1 for item in items if item["signals"][signal]), len(items))
                for signal in SIGNALS
            },
        }
        for category, items in sorted(by_category.items())
    }

    def mean_for(quality: str) -> float | None:
        values = [item["score"] for item in scored if item["quality"] == quality]
        return round(sum(values) / len(values), 6) if values else None

    good = mean_for("good")
    medium = mean_for("medium")
    flawed = mean_for("flawed")
    bypass = mean_for("bypass")
    bypass_full = [item["id"] for item in scored
                   if item.get("bypass") and item["score"] >= 1.0]
    overall = (round(sum(item["score"] for item in scored) / len(scored), 6)
               if scored else None)
    return {
        "layer": "TutorRubric",
        "definition": {
            "cites_material": ("every explanation has a citation; every citation resolves to a "
                               "real page; a cited page contains the scenario evidence_keys; the "
                               "explanation states every scenario claim_term"),
            "tests_first": ("a diagnostic/quiz occurs before the first explanation, contains a "
                            "scenario quiz_keyword, and is followed by a student response"),
            "gives_next_step": ("the last tutor message contains a syntactically executable "
                                "examprep command with existing qids/chapters, and no negation"),
            "no_fabrication": ("no scenario-forbidden claim, no fake citation, and no global "
                               "exam-gaming phrase (必考/包过/背下来就行/不用理解/...)"),
            "score": "mean of the four 0/1 signals",
            "quality": "good / medium (single-signal ablation) / flawed / bypass (exam-style shortcut)",
        },
        "scenario_count": len(scored),
        "quality_counts": {
            quality: sum(1 for item in scored if item["quality"] == quality)
            for quality in ("good", "medium", "flawed", "bypass")
        },
        "categories": category_summary,
        "overall_mean_score": overall,
        "good_mean_score": good,
        "medium_mean_score": medium,
        "flawed_mean_score": flawed,
        "bypass_mean_score": bypass,
        "discrimination": round(good - flawed, 6) if good is not None and flawed is not None else None,
        "discrimination_good_vs_medium": (
            round(good - medium, 6) if good is not None and medium is not None else None),
        "discrimination_medium_vs_flawed": (
            round(medium - flawed, 6) if medium is not None and flawed is not None else None),
        "bypass_full_score_count": len(bypass_full),
        "bypass_full_score_ids": bypass_full,
        "citation_validity_pct": pct(
            sum(len(item["evidence"]["valid_citations"]) for item in scored),
            sum(len(item["evidence"]["citations"]) for item in scored),
        ),
        "citation_support_pct": pct(
            sum(item["evidence"]["support_citations_valid"] for item in scored),
            sum(item["evidence"]["support_citations"] for item in scored),
        ),
        "scenarios": scored,
        "judge": _judge(judge, scenarios, provider),
    }
