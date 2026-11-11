"""校验 workspace 内各包版本号一致（R1.1）。"""

import tomllib
from pathlib import Path

import examprep_llm
import examprep_runtime

ROOT = Path(__file__).resolve().parents[1]


def _version(pyproject: Path) -> str:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_versions_in_sync() -> None:
    versions = {
        "root": _version(ROOT / "pyproject.toml"),
        "llm": _version(ROOT / "packages" / "llm" / "pyproject.toml"),
        "runtime": _version(ROOT / "packages" / "runtime" / "pyproject.toml"),
        "examprep_llm.__version__": examprep_llm.__version__,
        "examprep_runtime.__version__": examprep_runtime.__version__,
    }
    assert len(set(versions.values())) == 1, versions
