# -*- coding: utf-8 -*-
"""Deterministic tool layer.

This package is a near-faithful port of the deterministic core of the
`universal-examprep-skill` coach package, reorganised as library modules under
`examprep_agent.tools`.  Derived from universal-examprep-skill (MIT, (c) ZeKaiNie):

- :mod:`examprep_agent.tools.extract`   — course files → numbered pages of text
- :mod:`examprep_agent.tools.chapters`  — sources → ordered chapters
- :mod:`examprep_agent.tools.questions` — homework/exams → quiz bank + deterministic grading
- :mod:`examprep_agent.tools.figures`   — figures and question crops
- :mod:`examprep_agent.tools.guard`     — material fencing and prompt-injection heuristics
- :mod:`examprep_agent.tools.index`     — dependency-free BM25 retrieval
- :mod:`examprep_agent.tools.text`      — tokenization / text cleanup helpers
- :mod:`examprep_agent.tools.state`     — atomic study_state.json persistence

The original CLI lives elsewhere; this package contains no presentation logic.
"""

from . import chapters, extract, figures, guard, index, questions, state, text

__all__ = ["chapters", "extract", "figures", "guard", "index", "questions", "state", "text"]
