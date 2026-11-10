"""Serve the widget with the simulated machine as the microphone.

    uv run python e2e/serve_simulated.py <session-dir> [port]

Pairs with `start.sh`: cncjs runs the program on the fake Grbl, and this server "hears" the
simulated machine of `tests/synth.py` cut it, so a whole session can be driven from the
browser with no mill and no sound.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from synth import SimulatedMic  # noqa: E402

from dechatter import server  # noqa: E402
from dechatter.detect import Thresholds  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
server.serve(server.App(Path(sys.argv[1]), SimulatedMic(), Thresholds()), "127.0.0.1", port)
