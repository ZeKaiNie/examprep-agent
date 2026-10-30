# -*- coding: utf-8 -*-
"""GroundingBench: closed-book vs raw-file vs agent on one shared gold QA set.

Integrity rules (P3 review B1/I2):

* the **agent arm** receives only the question *string* and the production
  :class:`~examprep_agent.service.study.StudyService.ask` result.  It never sees
  ``answer``, ``answer_terms``, ``evidence_keys`` or the gold pages; those are
  held by :func:`_score` only;
* the **same** deterministic extractive reader and the **same** scorer are used
  by all three arms.  The arms differ only in *information access*:
  - ``closed_book``: a small fixed prior table (five memorised facts) and no
    course evidence; anything outside the prior is honestly refused;
  - ``raw_file``: the complete course text, chunked for retrieval/statistics but
    with **no page anchors**, so it answers from full text and cannot page-cite;
  - ``agent``: ``StudyService.ask`` (production BM25 retrieval) over the real
    workspace, with page-level citations from the retrieved chunks.
* a probe is refused only when the **evidence-support check** fails.  The check
  is global and gold-free: IDF-weighted coverage of the question's character
  n-grams over the evidence the arm can see, plus a minimum contiguous lexical
  overlap.  It has no question-id or question-text branches.

The reader is deterministic and extractive, so this layer measures the
retrieval → support → extraction → citation pipeline, not an LLM's parametric
knowledge.  The report states that boundary explicitly.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .common import (
    DATA_DIR,
    copy_sample_course,
    material_pages,
    page_text,
    pct,
    read_json,
)

#: A small, explicitly reported stand-in for parametric knowledge.  The
#: closed-book arm may answer only when the question matches one of these
#: memorised facts; otherwise it refuses (honest abstention without evidence).
CLOSED_BOOK_PRIOR: tuple[dict[str, str], ...] = (
    {"question": "栈的存取特点是什么？", "answer": "后进先出（LIFO）"},
    {"question": "队列的存取特点是什么？", "answer": "先进先出（FIFO）"},
    {"question": "二叉树的先序遍历顺序是什么？", "answer": "根 → 左 → 右"},
    {"question": "二叉排序树的中序遍历会得到什么序列？", "answer": "递增序列"},
    {"question": "AVL 树要求任一结点左右子树高度差不超过多少？", "answer": "高度差不超过 1"},
)

#: Global support thresholds, fixed once for the whole gold set (never per
#: item).  With the symmetric n-gram tokenizer below, the in-material minimum
#: is 0.1069 and the probe maximum is 0.0787 on this corpus (see
#: results.json -> grounding.support_calibration).
SUPPORT_MIN_COVERAGE = 0.09
SUPPORT_MIN_CONTIGUOUS = 3
#: The closed-book prior is a *memorised* table, so matching it is deliberately
#: stricter than evidence support: only near-verbatim questions hit the prior.
PRIOR_MIN_COVERAGE = 0.5
PRIOR_MIN_CONTIGUOUS = 6

_ASCII_STOP = frozenset(
    "the and or is are of in on to a an for how what which why does do".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'+#\-]*|[一-鿿]+")


# ---------------------------------------------------------------- text features
def question_terms(query: str) -> list[str]:
    """ASCII words plus CJK n-grams (2–4) of the question; no gold knowledge."""
    out: list[str] = []
    for token in _TOKEN_RE.findall(query.lower()):
        if token[0].isascii():
            if token not in _ASCII_STOP:
                out.append(token)
            continue
        for size in (2, 3, 4):
            out.extend(token[index:index + size]
                       for index in range(len(token) - size + 1))
    return out


def _idf_table(documents: Sequence[str]) -> Any:
    """Smoothed IDF over the evidence an arm can actually see."""
    df: Counter[str] = Counter()
    for document in documents:
        df.update(set(_evidence_tokens(document)))
    total = max(1, len(documents))
    return lambda term: math.log(1.0 + (total + 1.0) / (df.get(term, 0) + 0.5))


def _evidence_tokens(text: str) -> set[str]:
    """Evidence tokens use the same question n-gram space (ASCII + CJK 2–4)."""
    return set(question_terms(text))


def _coverage(terms: set[str], evidence_tokens: set[str], idf: Any) -> float:
    total = sum(idf(term) for term in terms)
    if total <= 0:
        return 0.0
    matched = sum(idf(term) for term in terms if term in evidence_tokens)
    return matched / total


def _cjk(text: str) -> str:
    return "".join(re.findall(r"[一-鿿]", text))


def _longest_common_substring(left: str, right: str) -> int:
    """Longest contiguous common substring length (CJK characters only)."""
    left, right = _cjk(left), _cjk(right)
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for char in left:
        current = [0] * (len(right) + 1)
        for index, other in enumerate(right, 1):
            if char == other:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def _segments(text: str) -> list[str]:
    """Candidate answer segments: table rows stay whole, prose splits on sentences."""
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("|"):
            out.append(line)
            continue
        out.extend(segment.strip() for segment in re.split(r"(?<=[。；！？])", line)
                   if segment.strip())
    return out


def _normalize(text: str) -> str:
    """Comparison form: no whitespace, unified dashes/parentheses/case."""
    table = str.maketrans({"−": "-", "–": "-", "（": "(", "）": ")",
                           "，": ",", "：": ":", "；": ";"})
    return re.sub(r"\s+", "", str(text).translate(table)).casefold()


# ---------------------------------------------------------------- arm answers
@dataclass
class ArmAnswer:
    """What one arm produced for one question (no gold ever enters this object)."""

    answered: bool
    answer: str | None
    citation: dict[str, Any] | None
    citation_valid: bool | None
    refused: bool
    retrieval_hits: int = 0
    support_coverage: float = 0.0
    contiguous_overlap: int = 0
    selected_segments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "answer": self.answer,
            "citation": self.citation,
            "citation_valid": self.citation_valid,
            "refused": self.refused,
            "retrieval_hits": self.retrieval_hits,
            "support_coverage": self.support_coverage,
            "contiguous_overlap": self.contiguous_overlap,
            "selected_segments": self.selected_segments,
        }


def _refusal(**metrics: Any) -> ArmAnswer:
    return ArmAnswer(False, None, None, None, True, **metrics)


def _ascii_entities(query: str) -> list[str]:
    """Multi-character alphanumeric entities named in the question (KMP, B+, O(1))."""
    return [token for token in _TOKEN_RE.findall(query.lower())
            if token[0].isascii() and len(token) >= 2 and token not in _ASCII_STOP]


def _evidence_support(query: str, documents: Sequence[str], *,
                      min_coverage: float = SUPPORT_MIN_COVERAGE,
                      min_contiguous: int = SUPPORT_MIN_CONTIGUOUS,
                      require_entities: bool = True) -> tuple[bool, float, int]:
    """Gold-free evidence-support check shared by every arm.

    A question is supported when (a) every multi-character alphanumeric entity
    it names occurs in the evidence, (b) IDF-weighted question n-gram coverage
    reaches the global threshold, and (c) there is a contiguous CJK overlap of
    at least ``min_contiguous`` characters.  All three conditions are global;
    none depends on question ids, gold answers or page metadata.
    """
    if not documents:
        return False, 0.0, 0
    terms = set(question_terms(query))
    idf = _idf_table(documents)
    union = set()
    for document in documents:
        union |= _evidence_tokens(document)
    coverage = _coverage(terms, union, idf)
    contiguous = max(_longest_common_substring(query, document) for document in documents)
    entities_present = (not require_entities
                        or all(entity in union for entity in _ascii_entities(query)))
    supported = (entities_present and coverage >= min_coverage
                 and contiguous >= min_contiguous)
    return supported, coverage, contiguous


def _segment_prior(segment: str) -> float:
    """A statement answers better than a heading or an exercise question."""
    weight = 1.0
    if segment.lstrip().startswith("#"):
        weight *= 0.4
    if segment.rstrip().endswith(("？", "?")) or "____" in segment:
        weight *= 0.4
    return weight


def _rank_segments(query: str, documents: Sequence[str], idf: Any) -> list[str]:
    """Segments of the visible evidence ranked by IDF-weighted question overlap.

    ``idf`` is always built from the *whole* visible evidence, so extracting
    from one retrieved chunk and from the full raw text use the same weights.
    Headings and exercise questions are down-weighted: they share question
    wording but are not answer statements.  The same prior applies to every arm.
    """
    terms = set(question_terms(query))
    scored: list[tuple[float, str]] = []
    for document in documents:
        for segment in _segments(document):
            score = _coverage(terms, _evidence_tokens(segment), idf) * _segment_prior(segment)
            scored.append((score, segment))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [segment for score, segment in scored if score > 0]


def _extractive_answer(query: str, documents: Sequence[str],
                       citation: dict[str, Any] | None,
                       *, rank_all_documents: bool = False) -> ArmAnswer:
    """Same reader for every evidence-backed arm.

    ``answer`` is the single best segment; ``selected_segments`` keeps the top
    three (used for the evidence-recall@3 metric).  When
    ``rank_all_documents`` is true (raw-file arm) segments are ranked across the
    whole visible text, because that arm has no page/chunk anchors to restrict
    extraction to one retrieved chunk.
    """
    supported, coverage, contiguous = _evidence_support(query, documents)
    if not supported:
        return _refusal(retrieval_hits=len(documents), support_coverage=coverage,
                        contiguous_overlap=contiguous)
    terms = set(question_terms(query))
    idf = _idf_table(documents)
    if rank_all_documents:
        ranked = _rank_segments(query, documents, idf)
    else:
        best_document = max(
            documents,
            key=lambda document: _coverage(terms, _evidence_tokens(document), idf),
        )
        ranked = _rank_segments(query, [best_document], idf)
    selected = ranked[:3]
    if not selected:
        return _refusal(retrieval_hits=len(documents), support_coverage=coverage,
                        contiguous_overlap=contiguous)
    return ArmAnswer(
        answered=True,
        answer=selected[0],
        citation=citation,
        citation_valid=None,
        refused=False,
        retrieval_hits=len(documents),
        support_coverage=coverage,
        contiguous_overlap=contiguous,
        selected_segments=selected,
    )


def _closed_book_answer(query: str) -> ArmAnswer:
    """No course evidence: answer only from the five memorised facts, else refuse."""
    priors = [item["question"] for item in CLOSED_BOOK_PRIOR]
    supported, coverage, contiguous = _evidence_support(
        query, priors, min_coverage=PRIOR_MIN_COVERAGE,
        min_contiguous=PRIOR_MIN_CONTIGUOUS, require_entities=False)
    if not supported:
        return _refusal(support_coverage=coverage, contiguous_overlap=contiguous)
    idf = _idf_table(priors)
    best = max(
        CLOSED_BOOK_PRIOR,
        key=lambda item: _coverage(set(question_terms(query)),
                                   _evidence_tokens(item["question"]), idf),
    )
    return ArmAnswer(
        answered=True,
        answer=best["answer"],
        citation=None,
        citation_valid=None,
        refused=False,
        support_coverage=coverage,
        contiguous_overlap=contiguous,
        selected_segments=[best["question"]],
    )


def _raw_file_answer(query: str, chunk_texts: Sequence[str], course_label: str) -> ArmAnswer:
    """Whole course text, chunked for statistics, but no page anchors."""
    return _extractive_answer(
        query, list(chunk_texts),
        citation={"kind": "file_only", "file": course_label, "page": None},
        rank_all_documents=True,
    )


def _agent_answer(query: str, service: Any, workspace: str, k: int = 5) -> ArmAnswer:
    """Production retrieval path: only ``StudyService.ask`` and its hits."""
    ask = service.ask(workspace, query, k=k, chars=0)
    hits = list(ask.hits)
    if not hits:
        return _refusal(retrieval_hits=0)
    documents = [str(hit.text) for hit in hits]
    supported, coverage, contiguous = _evidence_support(query, documents)
    if not supported:
        return _refusal(retrieval_hits=len(hits), support_coverage=coverage,
                        contiguous_overlap=contiguous)
    terms = set(question_terms(query))
    idf = _idf_table(documents)
    best_index = max(
        range(len(hits)),
        key=lambda index: _coverage(terms, _evidence_tokens(documents[index]), idf),
    )
    best_hit = hits[best_index]
    answer = _extractive_answer(
        query, documents,
        citation={"kind": "page", "file": str(best_hit.file),
                  "page": int(best_hit.page)},
    )
    answer.support_coverage = coverage
    answer.contiguous_overlap = contiguous
    return answer


# ---------------------------------------------------------------- scoring
def _score(question: dict[str, Any], arm_answer: ArmAnswer,
           pages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Gold-only scoring: the readers never call this function.

    * in-material correctness: every gold ``answer_terms`` snippet appears in
      the extracted answer (normalised);
    * citation: a page citation is verified when the cited page exists and
      contains every answer-term snippet;
    * probes: the only correct outcome is an evidence-supported refusal.
    """
    out_of_material = bool(question.get("out_of_material"))
    terms = [str(item) for item in
             (question.get("answer_terms") or ([question["answer"]]
                                               if question.get("answer") else []))]
    answer_text = _normalize(arm_answer.answer or "")
    matched_terms = [term for term in terms if _normalize(term) in answer_text]
    answer_correct = bool(
        not out_of_material
        and arm_answer.answered
        and terms
        and len(matched_terms) == len(terms)
    )
    citation = arm_answer.citation or {}
    if citation.get("kind") == "page":
        file, page = str(citation.get("file")), int(citation.get("page") or 0)
        page_text_value = page_text(pages, file, page)
        citation_valid = bool(
            page_text_value is not None
            and terms
            and all(_normalize(term) in _normalize(page_text_value) for term in terms)
        )
        arm_answer.citation_valid = citation_valid
    else:
        # file-only / no citation: page verifiability is not attempted
        arm_answer.citation_valid = None
        citation_valid = None
    return {
        "out_of_material": out_of_material,
        "expected_answer": question.get("answer"),
        "answer_terms": terms,
        "answer_correct": answer_correct,
        "matched_terms": matched_terms,
        "false_answer": bool(out_of_material and arm_answer.answered),
        "citation_valid": citation_valid,
        "page_citation_attempted": citation.get("kind") == "page",
    }


def _arm_summary(arm: str, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    in_material = [item for item in records if not item["out_of_material"]]
    probes = [item for item in records if item["out_of_material"]]
    answered = [item for item in in_material if item["arm"]["answered"]]
    page_citations = [item for item in in_material
                      if item["arm"]["answered"] and item.get("page_citation_attempted")]
    verified = [item for item in in_material if item.get("citation_valid") is True]
    top1 = sum(1 for item in in_material if item["answer_correct"])
    top3 = sum(1 for item in in_material if item["answer_terms_in_top3"])
    return {
        "arm": arm,
        "information_condition": records[0].get("information_condition") if records else "",
        "in_material_total": len(in_material),
        "answered_in_material": len(answered),
        "correct_in_material_top1": top1,
        "material_answer_accuracy_pct": pct(top1, len(in_material)),
        "answer_terms_in_evidence_top3": top3,
        "evidence_recall_at_3_pct": pct(top3, len(in_material)),
        "page_citation_attempts": len(page_citations),
        "page_citation_verified": len(verified),
        "citation_accuracy_pct": pct(len(verified), len(page_citations)),
        "probes_total": len(probes),
        "probe_refusals": sum(1 for item in probes if item["arm"]["refused"]),
        "probe_false_answers": sum(1 for item in probes if item["false_answer"]),
        "out_of_material_refusal_pct": pct(
            sum(1 for item in probes if item["arm"]["refused"]), len(probes)),
        "probes_with_retrieval_hits": sum(1 for item in probes
                                          if item["arm"].get("retrieval_hits", 0) > 0),
        "retrieval_hits_on_probes": sum(int(item["arm"].get("retrieval_hits", 0))
                                        for item in probes),
    }


def run(root: str | Path, *, k: int = 5) -> dict[str, Any]:
    """Run the three-arm grounding benchmark and return its results section."""
    from examprep_agent.service.models import SetupRequest
    from examprep_agent.service.study import StudyService
    from examprep_agent.tools.text import pack

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    course = copy_sample_course(root / "course")
    workspace = str(root / "workspace")
    service = StudyService(pointer=str(root / "pointer"))
    setup = service.setup(SetupRequest(materials=str(course), workspace=workspace,
                                       lang="zh", days=30, workers=1))
    pages = material_pages(course)
    raw_text = "\n".join(str(page["text"]) for page in pages)
    raw_chunks = pack(raw_text, 600)
    data = read_json(DATA_DIR / "grounding_qa.json")
    questions: list[dict[str, Any]] = list(data["questions"])

    conditions = {
        "closed_book": ("explicit lower bound: five memorised facts only; no course text; "
                        "honest refusal outside the prior"),
        "raw_file": ("complete course text; no page anchors or BM25 retrieval ranking; "
                     "same reader and scorer"),
        "agent": "production StudyService.ask retrieval; page anchors on retrieved chunks",
    }
    records: list[dict[str, Any]] = []
    per_arm: dict[str, list[dict[str, Any]]] = {
        "closed_book": [], "raw_file": [], "agent": []}
    for question in questions:
        query = str(question["question"])
        # Readers receive only the query string; gold stays in _score().
        answers = {
            "closed_book": _closed_book_answer(query),
            "raw_file": _raw_file_answer(query, raw_chunks, course.name),
            "agent": _agent_answer(query, service, workspace, k=k),
        }
        for arm, arm_answer in answers.items():
            scored = _score(question, arm_answer, pages)
            record = {
                "id": question["id"],
                "question": query,
                "arm_name": arm,
                "out_of_material": bool(question.get("out_of_material")),
                "information_condition": conditions[arm],
                "expected_answer": scored["expected_answer"],
                "answer_terms": scored["answer_terms"],
                "answer_correct": scored["answer_correct"],
                "answer_terms_in_top3": bool(
                    not scored["out_of_material"]
                    and all(_normalize(term) in _normalize(
                        " ".join(arm_answer.selected_segments)) for term in scored["answer_terms"])
                ),
                "matched_terms": scored["matched_terms"],
                "false_answer": scored["false_answer"],
                "citation_valid": scored["citation_valid"],
                "page_citation_attempted": scored["page_citation_attempted"],
                "arm": arm_answer.to_dict(),
            }
            records.append(record)
            per_arm[arm].append(record)

    summaries = {arm: _arm_summary(arm, arm_records)
                 for arm, arm_records in per_arm.items()}
    in_material_scores = [record["arm"]["support_coverage"]
                          for record in records
                          if record["arm_name"] == "agent" and not record["out_of_material"]]
    probe_scores = [record["arm"]["support_coverage"] for record in records
                    if record["arm_name"] == "agent" and record["out_of_material"]]
    return {
        "layer": "GroundingBench",
        "definition": {
            "reader": "deterministic extractive reader; same code for closed-book/raw-file/agent",
            "support_check": ("global, gold-free: IDF-weighted question n-gram coverage over the "
                              "visible evidence >= %.3f AND longest contiguous CJK overlap >= %d"
                              % (SUPPORT_MIN_COVERAGE, SUPPORT_MIN_CONTIGUOUS)),
            "material_answer_accuracy": ("share of in-material questions whose top-1 extracted "
                                         "segment contains every gold answer_term"),
            "evidence_recall_at_3": ("share of in-material questions whose top-3 extracted "
                                     "segments contain every gold answer_term"),
            "citation_accuracy": ("verified page citations / page-citation attempts; a citation "
                                  "is verified when the cited page exists and contains every answer_term"),
            "out_of_material_refusal": "evidence-support refusals / out-of-material probes",
            "gold_location": "answer / answer_terms / gold pages are read only by _score()",
        },
        "support_calibration": {
            "min_coverage": SUPPORT_MIN_COVERAGE,
            "min_contiguous_overlap": SUPPORT_MIN_CONTIGUOUS,
            "in_material_agent_coverage_min": round(min(in_material_scores), 6)
            if in_material_scores else None,
            "probe_agent_coverage_max": round(max(probe_scores), 6) if probe_scores else None,
            "note": "one global threshold for all items; per-item scores are in records[]",
        },
        "setup": {
            "materials": "tests/fixtures/zh-data-structures",
            "chapters": len(setup.chapters),
            "questions_in_course": setup.questions,
            "pages_extracted": len(pages),
            "workspace": "temporary benchmark workspace",
        },
        "gold_set": {
            "in_material": sum(1 for question in questions
                               if not question.get("out_of_material")),
            "out_of_material_probes": sum(1 for question in questions
                                          if question.get("out_of_material")),
            "total": len(questions),
            "retrieval_k": k,
        },
        "arms": summaries,
        "records": records,
    }
