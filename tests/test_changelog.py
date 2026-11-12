"""The changelog the release notes come from, and the script that takes them out."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("changelog", ROOT / ".github" / "changelog.py")
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

SAMPLE = """# Changelog

## [Unreleased]

### Added

- Something new.

## [1.1.0] - 2026-12-01

### Changed

- **Breaking:** a renamed key.

## [1.0.0] - 2026-11-01

### Added

- The first release.

[Unreleased]: https://example.invalid/compare/v1.1.0...HEAD
[1.1.0]: https://example.invalid/compare/v1.0.0...v1.1.0
[1.0.0]: https://example.invalid/releases/tag/v1.0.0
"""


def test_a_release_takes_its_own_section_as_notes():
    assert changelog.section(SAMPLE, "1.1.0") == "### Changed\n\n- **Breaking:** a renamed key.\n"
    # The last section stops before the link references.
    assert changelog.section(SAMPLE, "1.0.0") == "### Added\n\n- The first release.\n"


def test_a_release_without_a_dated_section_is_refused():
    with pytest.raises(changelog.ChangelogError, match="no section for 1.2.0"):
        changelog.section(SAMPLE, "1.2.0")
    with pytest.raises(changelog.ChangelogError, match="no YYYY-MM-DD date"):
        changelog.section(SAMPLE, "Unreleased")
    undated = SAMPLE.replace("## [1.1.0] - 2026-12-01", "## [1.1.0] - soon")
    with pytest.raises(changelog.ChangelogError, match="no YYYY-MM-DD date"):
        changelog.section(undated, "1.1.0")
    empty = SAMPLE.replace("### Changed\n\n- **Breaking:** a renamed key.\n", "")
    with pytest.raises(changelog.ChangelogError, match="is empty"):
        changelog.section(empty, "1.1.0")


def test_the_command_prints_the_notes_or_fails(capsys):
    assert changelog.main("v0.1.1") == 0
    assert capsys.readouterr().out.startswith("### Changed\n\n- No changes")
    assert changelog.main("v9.9.9") == 1
    assert "no section for 9.9.9" in capsys.readouterr().err


def test_the_changelog_starts_with_unreleased_and_links_every_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = [h["version"] for h in changelog.HEADING.finditer(text)]
    assert versions[0] == "Unreleased"
    assert "0.1.0" in versions and "0.1.1" in versions
    for version in versions:
        assert re.search(rf"^\[{re.escape(version)}\]: https://\S+$", text, re.MULTILINE)
    for version in versions[1:]:
        changelog.section(text, version)
