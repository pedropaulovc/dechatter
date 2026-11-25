"""Write what the simulated machine of tests/synth.py sounds like cutting a session's latest round.

Usage: uv run python e2e/round_audio.py <session export .zip> <out.wav>

The widget's Export button produces the zip. Cue the WAV in the widget before pressing Start with
`await window.fakeMicCue('/dechatter/e2e/round.wav')` (see fake_mic.js): it decodes the audio up
front, starts playback on the widget's `dechatter:record` event, and re-anchors each pass at its
dwell from `<out.wav>.json` so simulator or facing timing drift cannot shift later cuts.

If the latest round in `<session export .zip>` already carries `timeline.json`, the audio is laid
out directly on the widget's recorded cut windows instead of G-code timing estimates.
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

from dechatter import audio, session  # noqa: E402
from dechatter.config import Vary  # noqa: E402
from dechatter.plan import Plan  # noqa: E402

RAPID_MM_MIN = 3000.0
WORD = re.compile(r"([GXYZFP])(-?\d+(?:\.\d+)?)")


def run(program: str, start: tuple[float, float, float]) -> tuple[list[tuple[float, float]], tuple]:
    """(start, end) seconds of every pass's cutting move (the G1 along X after its dwell), and
    where the machine ends up."""
    position = dict(zip("XYZ", start, strict=True))
    clock, feed, cuts, dwelled = 0.0, 0.0, [], -1
    for index, line in enumerate(program.splitlines()):
        code = re.sub(r"\(.*?\)", "", line).strip()
        words = dict(WORD.findall(code))
        if re.match(r"M0\b", code) and "set the spindle" in line:
            raise SystemExit("manual-spindle program: its M0 stops cannot be timed in advance")
        if words.get("G") == "4":
            clock += float(words["P"])
            dwelled = index
            continue
        if words.get("G") not in ("0", "1"):
            if set(words) & set("XYZ"):
                raise SystemExit(f"cannot time this move: {line}")
            continue
        feed = float(words.get("F", feed))
        target = {axis: float(words.get(axis, position[axis])) for axis in "XYZ"}
        seconds = math.dist(list(position.values()), list(target.values())) * 60
        seconds /= RAPID_MM_MIN if words["G"] == "0" else feed
        if words["G"] == "1" and "X" in words and dwelled == index - 1:
            cuts.append((clock, clock + seconds))
        clock += seconds
        position = target
    return cuts, tuple(position.values())


def _from_timeline(plan: Plan, timeline: list) -> tuple[list[float], list[float], dict[str, float]]:
    placed = session.placements_from_timeline(plan, timeline)
    if not placed:
        raise SystemExit("timeline.json has no placed cuts")
    air, cut_s, dwells, cursor = [], [], {}, 0.0
    for index, p in enumerate(plan.passes):
        if index not in placed:
            break
        pl = placed[index]
        start, end = pl.cut.start_s, pl.cut.end_s
        air_len = max(start - cursor, 0.0)
        dwell_start = (
            pl.air.start_s
            if pl.air is not None
            else max(start - p.lead_in_s - plan.dwell_s, cursor)
        )
        dwells[str(p.id)] = round(dwell_start, 3)
        air.append(air_len)
        cut_s.append(max(end - start, 0.0))
        cursor = end
    return air, cut_s, dwells


def main(export: Path, out: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(export.read_bytes())) as archive:
        names = set(archive.namelist())
        rounds = sorted(name[: -len("plan.json")] for name in names if name.endswith("/plan.json"))
        latest = rounds[-1]
        data = archive.read(f"{latest}plan.json")
        timeline = (
            json.loads(archive.read(f"{latest}timeline.json").decode("utf-8"))
            if f"{latest}timeline.json" in names
            else None
        )
        programs = (
            []
            if timeline is not None
            else [archive.read(f"{folder}program.nc").decode("ascii") for folder in rounds]
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plan.json"
        path.write_bytes(data)
        plan = Plan.load(path)

    machine = synth.DEPTH_MACHINE if plan.vary is Vary.DEPTH else synth.DEFAULT_MACHINE
    if timeline is not None:
        air, cut_s, dwells = _from_timeline(plan, timeline)
        subplan = plan
        if len(cut_s) < len(plan.passes):
            # Keep only passes present in the recorded timeline.
            kept = set(range(len(cut_s)))
            idx = 0
            legs = []
            for leg in plan.legs:
                lp = tuple(p for p in leg.passes if (idx := idx + 1) - 1 in kept)
                if lp:
                    legs.append(type(leg)(**{**leg.__dict__, "passes": lp}))
            subplan = Plan(**{**plan.__dict__, "legs": tuple(legs)})
        heard = synth.record(
            subplan,
            machine,
            seed=plan.round,
            air_s=air,
            cut_s=cut_s,
            facing=synth.Facing.PAUSED,
            rapid_s=0.0,
        )
    else:
        position = (0.0, 0.0, 0.0)
        for program in programs:
            cuts, position = run(program, position)
        faced = {leg.passes[0].id: leg.face_s for leg in plan.legs if leg.reface_mm > 0}
        air, dwells, heard_until = [], {}, 0.0
        for p, (start, _) in zip(plan.passes, cuts, strict=True):
            contact = start + p.lead_in_s
            dwells[str(p.id)] = round(max(start - plan.dwell_s, 0.0), 3)
            air.append(contact - heard_until - faced.get(p.id, 0.0))
            heard_until = contact + p.expected_cut_s
        heard = synth.record(
            plan, machine, seed=plan.round, air_s=air, facing=synth.Facing.HEARD, rapid_s=0.0
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    audio.save_wav(out, heard, synth.RATE)
    cues_path = out.with_name(out.name + ".json")
    cues_path.write_text(json.dumps({"dwells": dwells}, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "round": plan.round,
                "source": "timeline" if timeline is not None else "program",
                "seconds": round(len(heard) / synth.RATE, 1),
            }
        )
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
