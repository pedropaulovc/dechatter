"""Fail unless the release tag, package.json and the package's version agree.

The Python package's version is `__version__` in src/dechatter/__init__.py, which
pyproject.toml reads; package.json repeats it for npm.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "dechatter" / "__init__.py"
VERSION = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)


def package_version() -> str:
    found = VERSION.search(INIT.read_text(encoding="utf-8"))
    if found is None:
        raise SystemExit(f'{INIT.relative_to(ROOT)} has no line __version__ = "X.Y.Z"')
    return found[1]


def main(tag: str) -> int:
    versions = {
        "tag": tag.removeprefix("v"),
        "package.json": json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"],
        "__init__.py": package_version(),
    }
    if len(set(versions.values())) == 1:
        print(f"version {versions['tag']}")
        return 0
    print(f"versions disagree: {versions}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
