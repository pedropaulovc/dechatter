"""Write what the simulated machine of tests/synth.py sounds like cutting a session's latest round.

Usage: uv run python e2e/round_audio.py <session export .zip> <out.wav>

The widget's Export button produces the zip. Play the WAV into the widget with fake_mic.js.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import synth  # noqa: E402

from dechatter import audio  # noqa: E402
from dechatter.plan import Plan  # noqa: E402


def main(export: Path, out: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(export.read_bytes())) as archive:
        plans = sorted(name for name in archive.namelist() if name.endswith("/plan.json"))
        data = archive.read(plans[-1])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plan.json"
        path.write_bytes(data)
        plan = Plan.load(path)
    heard = synth.record(plan, seed=plan.round)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio.save_wav(out, heard, synth.RATE)
    print(json.dumps({"round": plan.round, "seconds": round(len(heard) / synth.RATE, 1)}))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
