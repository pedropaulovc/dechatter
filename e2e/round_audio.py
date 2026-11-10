"""Write what the simulated machine of tests/synth.py sounds like cutting a session's latest round.

Usage: uv run python e2e/round_audio.py <session export .zip> <out.wav>

The widget's Export button produces the zip. Play the WAV into the widget with fake_mic.js,
or through the speakers, starting when the job starts. The cuts are timed from the round's
G-code the way fake_grbl.py runs it at its default --rapid (G0 at 3000 mm/min, G1 at its feed,
G4 for its seconds), so the sound follows the simulated machine's motion to within about a
second: the simulator's 10 ms ticks add up over a round.

Assumes every earlier round's program ran to completion on the same simulator, since that is
where this round starts from. Manual-spindle programs are refused: their M0 stops last as
long as the operator takes.
"""

from __future__ import annotations

import io
import json
import math
import re
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import synth  # noqa: E402

from dechatter import audio  # noqa: E402
from dechatter.plan import Plan  # noqa: E402

RAPID_MM_MIN = 3000.0
WORD = re.compile(r"([GXYZFP])(-?\d+(?:\.\d+)?)")


def run(program: str, start: tuple[float, float, float]) -> tuple[list[tuple[float, float]], tuple]:
    """(start, end) seconds of every cutting move (G1 along X), and where the machine ends up."""
    position = dict(zip("XYZ", start, strict=True))
    clock, feed, cuts = 0.0, 0.0, []
    for line in program.splitlines():
        code = re.sub(r"\(.*?\)", "", line).strip()
        words = dict(WORD.findall(code))
        if re.match(r"M0\b", code):
            raise SystemExit("manual-spindle program: its M0 stops cannot be timed in advance")
        if words.get("G") == "4":
            clock += float(words["P"])
            continue
        if words.get("G") not in ("0", "1"):
            if set(words) & set("XYZ"):
                raise SystemExit(f"cannot time this move: {line}")
            continue
        feed = float(words.get("F", feed))
        target = {axis: float(words.get(axis, position[axis])) for axis in "XYZ"}
        seconds = math.dist(list(position.values()), list(target.values())) * 60
        seconds /= RAPID_MM_MIN if words["G"] == "0" else feed
        if words["G"] == "1" and "X" in words:
            cuts.append((clock, clock + seconds))
        clock += seconds
        position = target
    return cuts, tuple(position.values())


def main(export: Path, out: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(export.read_bytes())) as archive:
        rounds = sorted(
            name[: -len("plan.json")] for name in archive.namelist() if name.endswith("/plan.json")
        )
        programs = [archive.read(f"{folder}program.nc").decode("ascii") for folder in rounds]
        data = archive.read(f"{rounds[-1]}plan.json")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plan.json"
        path.write_bytes(data)
        plan = Plan.load(path)

    # The machine starts each round where the previous round's program left it.
    position = (0.0, 0.0, 0.0)
    for program in programs:
        cuts, position = run(program, position)

    air, heard_until = [], 0.0
    for p, (start, _) in zip(plan.passes, cuts, strict=True):
        air.append(start + p.lead_in_s - heard_until)
        heard_until = start + p.lead_in_s + p.expected_cut_s
    heard = synth.record(plan, seed=plan.round, air_s=air)
    out.parent.mkdir(parents=True, exist_ok=True)
    audio.save_wav(out, heard, synth.RATE)
    print(json.dumps({"round": plan.round, "seconds": round(len(heard) / synth.RATE, 1)}))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
