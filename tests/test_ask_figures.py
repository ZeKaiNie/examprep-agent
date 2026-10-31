# -*- coding: utf-8 -*-
"""Regression test for a crash in StudyService.ask with figures.

``figures_for_pages`` returns FigureRecord models, but ask() subscripted them
(``f["path"]``), so any retrieved chunk whose page carried a figure crashed
with ``TypeError: 'FigureRecord' object is not subscriptable``. Found by the
2026-10-31 real-course live comparison (MIT PDFs carry figures; the md-only
sample courses used by earlier tests never hit the branch).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from examprep_agent.service import SetupRequest, StudyService


def test_ask_with_figure_on_hit_page_does_not_crash(
        sample_course: Path, tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    shutil.copytree(sample_course, materials)
    ws = materials / "exam-cram"
    service = StudyService()
    service.setup(SetupRequest(materials=str(materials), workspace=str(ws), days=30))
    index = json.loads((ws / "index.json").read_text(encoding="utf-8"))
    chunk = index["chunks"][0]
    figures = [{"file": chunk["file"], "page": chunk["page"],
                "path": "figures/fig1.png", "kind": "figure"}]
    (ws / "figures.json").write_text(json.dumps(figures, ensure_ascii=False), encoding="utf-8")
    query = " ".join(chunk["text"].split()[:6])
    result = service.ask(str(ws), query, k=5, audit_tool="test.ask")
    assert result.found is True
    assert result.hits
    assert "figures/fig1.png" in result.hits[0].figures
