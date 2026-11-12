"""Print a release's section of CHANGELOG.md, for its release notes; fail if there is none.

    uv run python .github/changelog.py v1.2.3 > notes.md

The section must be headed `## [1.2.3] - YYYY-MM-DD`: a release is dated, Unreleased is not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>.*))?$", re.MULTILINE)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
LINK = re.compile(r"^\[[^\]]+\]: \S+$", re.MULTILINE)


class ChangelogError(ValueError):
    pass


def section(text: str, version: str) -> str:
    """The body of `version`'s section, without its heading or the link references."""
    headings = list(HEADING.finditer(text))
    found = [h for h in headings if h["version"] == version]
    if not found:
        raise ChangelogError(f"CHANGELOG.md has no section for {version}")
    heading = found[0]
    if heading["date"] is None or not DATE.fullmatch(heading["date"].strip()):
        raise ChangelogError(f"the {version} section of CHANGELOG.md has no YYYY-MM-DD date")
    later = [h.start() for h in headings if h.start() > heading.start()]
    end = later[0] if later else len(text)
    body = LINK.sub("", text[heading.end() : end]).strip()
    if not body:
        raise ChangelogError(f"the {version} section of CHANGELOG.md is empty")
    return body + "\n"


def main(tag: str) -> int:
    try:
        notes = section(CHANGELOG.read_text(encoding="utf-8"), tag.removeprefix("v"))
    except ChangelogError as exc:
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
