from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dechatter.config import TEMPLATE, Setup, load_setup  # noqa: E402


@pytest.fixture
def write_setup(tmp_path: Path):
    """Write the template setup with `old -> new` text substitutions; return its path."""

    def write(**replacements: str) -> Path:
        text = TEMPLATE
        for old, new in replacements.values():
            assert old in text
            text = text.replace(old, new)
        path = tmp_path / "setup.toml"
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def setup(write_setup) -> Setup:
    return load_setup(write_setup())
