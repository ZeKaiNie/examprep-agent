# -*- coding: utf-8 -*-
"""Parallel ingestion: equivalence with the serial path, failure isolation, JSONL progress."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from examprep_agent.service import StudyService, SetupRequest
from examprep_agent.service import ingest
from examprep_agent.tools import chapters as chmod
from examprep_agent.tools import extract, index as idx


def _source_dicts(sources) -> list[dict]:
    return [source.to_dict() for source in sources]


def _chapter_dicts(chapters) -> list[dict]:
    return [chapter.to_dict() for chapter in chapters]


def test_parallel_extraction_chapters_and_index_match_serial(sample_course: Path,
                                                             tmp_path: Path) -> None:
    serial_sources = ingest.extract_sources(str(sample_course), workers=1)
    parallel_sources = ingest.extract_sources(str(sample_course), workers=4)
    assert _source_dicts(parallel_sources) == _source_dicts(serial_sources)

    serial_chapters = ingest.build_chapters_parallel(serial_sources, workers=1)
    parallel_chapters = ingest.build_chapters_parallel(parallel_sources, workers=4)
    assert _chapter_dicts(parallel_chapters) == _chapter_dicts(serial_chapters)

    serial_chunks = ingest.build_chunks_parallel(serial_chapters, workers=1)
    parallel_chunks = ingest.build_chunks_parallel(parallel_chapters, workers=4)
    assert parallel_chunks == serial_chunks
    assert serial_chunks  # the sample course really indexes something


def test_a_bad_file_does_not_abort_the_batch(tmp_path: Path) -> None:
    materials = tmp_path / "materials"
    materials.mkdir()
    (materials / "lec1.md").write_text("# Lecture 1: Intro\n\nGood content one.\n", encoding="utf-8")
    (materials / "lec2.md").write_text("# Lecture 2: More\n\nGood content two.\n", encoding="utf-8")
    # A .docx whose zip payload is invalid: extraction must record an error and
    # the rest of the batch must still be ingested.
    (materials / "broken.docx").write_bytes(b"this is not a zip archive")

    progress = ingest.IngestProgress(str(tmp_path / "progress.jsonl"))
    sources = ingest.extract_sources(str(materials), workers=4, progress=progress)
    by_name = {source.rel: source for source in sources}
    assert by_name["broken.docx"].error
    assert not by_name["lec1.md"].error and by_name["lec1.md"].text
    assert not by_name["lec2.md"].error and by_name["lec2.md"].text

    events = [json.loads(line) for line in
              (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "ingest_start"
    files = [event for event in events if event["event"] == "file_extracted"]
    assert {event["file"] for event in files} == {"lec1.md", "lec2.md", "broken.docx"}
    assert any(event["file"] == "broken.docx" and event["ok"] is False for event in files)
    assert events[-1]["event"] == "ingest_done" and events[-1]["errors"] == 1


def test_a_chapter_plan_failure_does_not_abort_the_batch(
        sample_course: Path, tmp_path: Path, monkeypatch) -> None:
    """I3: planning one source must fail in isolation, not fail setup()."""
    sources = ingest.extract_sources(str(sample_course), workers=2)
    real_plan = chmod.source_plan

    def flaky(source):
        if "第2章" in source.rel:
            raise RuntimeError("plan boom")
        return real_plan(source)

    monkeypatch.setattr(chmod, "source_plan", flaky)
    progress = ingest.IngestProgress(str(tmp_path / "progress.jsonl"))
    chapters = ingest.build_chapters_parallel(sources, workers=4, progress=progress)
    assert {chapter.number for chapter in chapters} == {1, 3}
    failed = [source for source in sources if source.rel.endswith("第2章_栈和队列.md")]
    assert failed and any("chapter_plan_failed" in w for w in failed[0].warnings)
    events = [json.loads(line) for line in
              (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(event["event"] == "chapter_plan_failed" for event in events)


def test_an_index_chunk_failure_does_not_abort_the_batch(
        sample_course: Path, tmp_path: Path, monkeypatch) -> None:
    """I3: one bad chapter contributes no chunks; the rest of the index builds."""
    sources = ingest.extract_sources(str(sample_course), workers=2)
    chapters = ingest.build_chapters_parallel(sources, workers=2)
    real_chunks = idx.chunks_for_chapter

    def flaky(chapter):
        if chapter.number == 2:
            raise RuntimeError("chunk boom")
        return real_chunks(chapter)

    monkeypatch.setattr(idx, "chunks_for_chapter", flaky)
    progress = ingest.IngestProgress(str(tmp_path / "progress.jsonl"))
    chunks = ingest.build_chunks_parallel(chapters, workers=4, progress=progress)
    assert chunks and {chunk["chapter"] for chunk in chunks} == {1, 3}
    events = [json.loads(line) for line in
              (tmp_path / "progress.jsonl").read_text(encoding="utf-8").splitlines()]
    failed = [event for event in events if event["event"] == "chunks_failed"]
    assert failed and failed[0]["chapter"] == 2


def test_same_basename_sources_do_not_collide_in_figure_names(
        tmp_path: Path) -> None:
    """I4: ``a.docx`` and ``sub/a.docx`` must not write the same figure path."""
    import zipfile

    materials = tmp_path / "materials"
    (materials / "sub").mkdir(parents=True)

    def make_docx(path: Path, payload: bytes) -> None:
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                'package/2006/content-types"/>')
            zf.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Note A</w:t></w:r></w:p>'
                '<!-- r:embed="rId1" --></w:body></w:document>')
            zf.writestr(
                "word/_rels/document.xml.rels",
                '<?xml version="1.0"?><Relationships>'
                '<Relationship Id="rId1" Target="media/image1.png"/></Relationships>')
            zf.writestr("word/media/image1.png", payload)

    make_docx(materials / "a.docx", b"first-image")
    make_docx(materials / "sub" / "a.docx", b"second-image")

    outputs = {}
    for workers in (1, 4):
        ws = tmp_path / ("ws-%d" % workers)
        sources = ingest.extract_sources(str(materials), workers=workers)
        records, _scans, _infos = ingest.extract_figures_parallel(
            str(materials), sources, str(ws), workers=workers)
        outputs[workers] = [
            (record["file"], record["path"], (ws / "figures" / Path(record["path"]).name).read_bytes())
            for record in records
        ]
    assert outputs[1] == outputs[4]  # serial/parallel equivalence
    paths = {path for _file, path, _data in outputs[1]}
    assert len(paths) == 2  # two distinct figure files, not one overwritten path
    files = {file for file, _path, _data in outputs[1]}
    assert files == {"a.docx", "sub/a.docx"}
    contents = {data for _file, _path, data in outputs[1]}
    assert contents == {b"first-image", b"second-image"}


def test_parallel_setup_produces_identical_artifacts(sample_course: Path, tmp_path: Path) -> None:
    service = StudyService(pointer=str(tmp_path / "pointer"))
    artifacts = {}
    for workers in (1, 4):
        materials = tmp_path / ("course-%d" % workers)
        shutil.copytree(sample_course, materials)
        workspace = materials / "exam-cram"
        result = service.setup(SetupRequest(
            materials=str(materials), workspace=str(workspace),
            lang="zh", days=5, workers=workers,
        ))
        assert result.exit_code == 0
        artifacts[workers] = {
            name: (workspace / name).read_text(encoding="utf-8")
            for name in ("chapters.json", "quiz_bank.json", "index.json", "figures.json")
        }
        artifacts[workers]["chapter_files"] = sorted(
            (path.name, path.read_text(encoding="utf-8"))
            for path in (workspace / "chapters").glob("ch*.md")
        )
    assert artifacts[1]["chapters.json"] == artifacts[4]["chapters.json"]
    assert artifacts[1]["quiz_bank.json"] == artifacts[4]["quiz_bank.json"]
    assert artifacts[1]["index.json"] == artifacts[4]["index.json"]
    assert artifacts[1]["figures.json"] == artifacts[4]["figures.json"]
    assert artifacts[1]["chapter_files"] == artifacts[4]["chapter_files"]
