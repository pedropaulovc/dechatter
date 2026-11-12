"""Fail unless the release tag, package.json, pyproject.toml and the package's __version__
name the same version."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(tag: str) -> int:
    versions = {
        "tag": tag.removeprefix("v"),
        "package.json": json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"],
        "pyproject.toml": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["version"],
        "__init__.py": re.search(
            r'__version__ = "([^"]+)"',
            (ROOT / "src" / "dechatter" / "__init__.py").read_text(encoding="utf-8"),
        )[1],
    }
    if len(set(versions.values())) == 1:
        print(f"version {versions['tag']}")
        return 0
    print(f"versions disagree: {versions}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
