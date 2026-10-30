# -*- coding: utf-8 -*-
"""Shared pytest fixtures: offline env, sample course and workspace."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE = FIXTURES / "zh-data-structures"

_ENV_KEYS = (
    "EXAMPREP_MODEL",
    "EXAMPREP_BASE_URL",
    "EXAMPREP_WORKSPACE",
    "EXAMPREP_OFFLINE",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "EXAM_CRAM_WORKSPACE",
)


@pytest.fixture(autouse=True)
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee every test is offline: no API key, no ambient workspace."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EXAMPREP_OFFLINE", "1")


@pytest.fixture
def sample_course(tmp_path: Path) -> Path:
    """A writable copy of the bundled zh-data-structures sample course."""
    dest = tmp_path / "course"
    shutil.copytree(SAMPLE, dest)
    return dest


@pytest.fixture
def workspace(sample_course: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sample course plus an EXAM_CRAM-compatible workspace env var."""
    ws = sample_course / "exam-cram"
    monkeypatch.setenv("EXAMPREP_WORKSPACE", str(ws))
    return ws
