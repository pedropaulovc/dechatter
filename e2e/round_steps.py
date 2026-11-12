"""Print a round's blocks as the widget runs them, as JSON.

uv run python e2e/round_steps.py <session folder> <round> [mm|in] > blocks.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dechatter.app import App
from dechatter.detect import Thresholds
from dechatter.gcode import Units


def main(folder: str, number: str, units: str = "mm") -> None:
    app = App(Path(folder), Thresholds())
    print(json.dumps(app.steps(int(number), Units(units))))


if __name__ == "__main__":
    main(*sys.argv[1:])
