"""One session as the cncjs widget sees it: its state as JSON-ready data, and its actions.

The widget records in the browser and hands the samples over; everything else (planning,
analysis, files) is the same code the command line uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import audio, bayes, gcode, session
from .config import Setup, SetupSource, read_setup, setup_source
from .detect import Thresholds, Verdict
from .plan import PassKind, envelope, feed_mm_min
from .session import PassStatus, SessionError, Stock

log = logging.getLogger(__name__)

RECORDING_FILE = session.RECORDING_FILE
TIMELINE_FILE = "timeline.json"
TOP_RESULTS = 5
PROGRAM_ID_LENGTH = 6


class Next(StrEnum):
    SETUP = "setup"  # the setup is still the template: fill it in before planning
    PLAN = "plan"  # plan the first or the next round
    RUN = "run"  # a round is planned: load it, cut it, record it
    SWEEP = "sweep"  # converged; a chip-load sweep of the best cut is on offer
    CONVERGED = "converged"  # converged, and the best cut's chip load swept


class App:
    def __init__(self, session_dir: Path, thresholds: Thresholds):
        self.session_dir = session_dir
        self.thresholds = thresholds
        # Kept as read: a round planned now stores exactly the setup it was planned from.
        self.source = read_setup(
            (session_dir / session.SETUP_FILE).read_text(encoding="utf-8"), session.SETUP_FILE
        )
        self.setup = self.source.setup
        self.env = envelope(self.setup)
        self.recording_round: int | None = None
        self.discarded_round: int | None = None  # unrun round a setup change threw away

    def program_name(self, r: session.Round) -> str:
        """Names one planned program: a round planned again after a setup change gets a new
        name, so a program still loaded in cncjs never passes for it."""
        digest = hashlib.sha256()
        for name in (session.SETUP_FILE, session.PLAN_FILE):
            digest.update((r.path / name).read_bytes())
        return (
            f"dechatter-{self.session_dir.resolve().name}-round-{r.plan.round:02d}-"
            f"{digest.hexdigest()[:PROGRAM_ID_LENGTH]}.nc"
        )

    def _round(self, number: int) -> session.Round:
        for candidate in session.load_rounds(self.session_dir):
            if candidate.plan.round == number:
                return candidate
        raise SessionError(f"round {number} does not exist")

    def state(self) -> dict:
        rounds = session.load_rounds(self.session_dir)
        # What is shown belongs to the depth of the latest round: the one planned or just cut.
        depth = rounds[-1].plan.axial_depth_mm if rounds else self.setup.cut.axial_depth_mm
        observations = session.observations(rounds, depth, self.env)
        stability = bayes.build_map(
            session.at_depth(self.setup, depth), self.env, session.evidence(rounds, depth, self.env)
        )
        shown = bayes.coarse(stability)

        found = session.best_cuts(self.setup, self.env, rounds)
        sweep = session.swept(found, session.sweeps(rounds, self.env))
        upcoming, next_depth = Next.PLAN, self.setup.cut.axial_depth_mm
        if rounds and rounds[-1].results is None:
            upcoming, next_depth = Next.RUN, depth
        if rounds and rounds[-1].results is not None:
            stage = session.next_stage(self.setup, self.env, rounds, found)
            upcoming = Next.PLAN if stage.tests else Next.CONVERGED
            next_depth = stage.depth_mm
        if upcoming is Next.CONVERGED and session.to_sweep(self.setup, self.env, found, sweep):
            upcoming = Next.SWEEP
        template = setup_source(self.source.text) is SetupSource.TEMPLATE
        if upcoming is Next.PLAN and template:
            upcoming = Next.SETUP

        tool = self.setup.tool
        return {
            "session": self.session_dir.resolve().name,
            # In mm, like every length here: the widget shows them in the machine's units.
            "summary": {
                "diameter_mm": tool.diameter_mm,
                "flutes": tool.flutes,
                "material": self.setup.cut.material,
                "depths_mm": session.depths(self.setup),
                "chip_load_mm": self.env.chip_load_mm,
            },
            "spindle": self.setup.machine.spindle,
            "next": upcoming,
            "next_depth_mm": next_depth,
            "depth_mm": depth,
            "recording_round": self.recording_round,
            "discarded_round": self.discarded_round,
            "rounds": [self._round_json(r) for r in rounds],
            "observations": [
                {"rpm": o.rpm, "width_mm": o.width_mm, "verdict": o.verdict} for o in observations
            ],
            "best": [
                {
                    "rpm": best.rpm,
                    "width_mm": best.width_mm,
                    "depth_mm": best.depth_mm,
                    "feed_mm_min": round(feed_mm_min(self.setup, self.env, best.rpm)),
                    "mrr_cm3_min": round(best.mrr_mm3_min / 1000, 2),
                }
                for best in found[:TOP_RESULTS]
            ],
            "map": {
                "speeds": shown.speeds.tolist(),
                "widths": shown.widths.round(2).tolist(),
                "p": shown.p.round(3).tolist(),
            },
            "sweep": None if sweep is None else self._sweep_json(sweep),
        }

    def _round_json(self, r: session.Round) -> dict:
        results = {p.id: p for p in r.results.passes} if r.results else {}
        passes = []
        for p in r.plan.passes:
            result = results.get(p.id)
            passes.append(
                {
                    "id": p.id,
                    "kind": p.kind,
                    "rpm": p.rpm,
                    "width_mm": p.width_mm,
                    "feed_mm_min": p.feed_mm_min,
                    "p_stable": p.p_stable,
                    "chip_load_mm": p.chip_load_mm or r.plan.chip_load_mm,
                    "status": result.status if result else None,
                    "verdict": result.verdict if result else None,
                    "chatter_hz": result.chatter_hz if result else None,
                    "prominence_db": result.prominence_db if result else None,
                }
            )
        return {
            "round": r.plan.round,
            "depth_mm": r.plan.axial_depth_mm,
            "planner": r.plan.planner,
            "program": self.program_name(r),
            "run_time_s": round(r.plan.run_time_s),
            "face_y_end_mm": round(r.plan.face_y_start_mm + r.plan.consumed_mm, 2),
            "analysed": r.results is not None,
            "has_recording": (r.path / RECORDING_FILE).exists(),
            "passes": passes,
        }

    def _sweep_json(self, sweep: session.Sweep) -> dict:
        return {
            "round": sweep.round,
            "rpm": sweep.rpm,
            "width_mm": sweep.width_mm,
            "depth_mm": sweep.depth_mm,
            "session_chip_load_mm": sweep.session_chip_load_mm,
            "chip_load_mm": sweep.stable_chip_load_mm,
            "feed_mm_min": None if sweep.feed_mm_min is None else round(sweep.feed_mm_min),
            "mrr_cm3_min": None
            if sweep.mrr_mm3_min is None
            else round(sweep.mrr_mm3_min / 1000, 2),
        }

    def plan(self, stock: Stock) -> None:
        session.plan_next(self.session_dir, self.source, self.env, stock)
        self.discarded_round = None

    def plan_sweep(self, stock: Stock) -> None:
        session.plan_sweep(self.session_dir, self.source, self.env, stock)
        self.discarded_round = None

    def _planned(self, number: int) -> tuple[session.Round, Setup]:
        target = self._round(number)
        return target, session.at_depth(session.planned_with(target), target.plan.axial_depth_mm)

    def program(self, number: int, units: gcode.Units) -> str:
        """The round's program in the machine's units. The file beside the plan is in mm."""
        target, planned = self._planned(number)
        return gcode.render(target.plan, planned, units)

    def steps(self, number: int, units: gcode.Units) -> list[dict]:
        """The round's program as the blocks the widget runs one at a time."""
        target, planned = self._planned(number)
        return [asdict(block) for block in gcode.blocks(target.plan, planned, units)]

    def record_start(self, number: int) -> None:
        if self.recording_round is not None:
            raise SessionError(f"already recording round {self.recording_round}")
        self._round(number)
        self.recording_round = number

    def record_stop(self, samples: np.ndarray, rate: int, timeline: list) -> None:
        """Keep the recording and the job timeline cncjs reported, then analyse."""
        if self.recording_round is None:
            raise SessionError("not recording")
        target = self._round(self.recording_round)
        self.recording_round = None
        if len(samples) == 0:
            raise SessionError(
                "the microphone delivered no audio during the run; check that it shows a level, "
                "then run the round again"
            )
        recording = target.path / RECORDING_FILE
        audio.save_wav(recording, samples, rate)
        (target.path / TIMELINE_FILE).write_text(json.dumps(timeline), encoding="utf-8")
        log.info("round %d: recorded %.1f s at %d Hz", target.plan.round, len(samples) / rate, rate)
        session.analyze_recording(target, recording, self.thresholds)

    def analyze(self, number: int) -> None:
        target = self._round(number)
        recording = target.path / RECORDING_FILE
        if not recording.exists():
            raise SessionError(f"round {number} has no recording")
        session.analyze_recording(target, recording, self.thresholds)

    def set_verdict(self, number: int, pass_id: int, verdict: Verdict) -> None:
        """The operator's ears overrule the detector."""
        target = self._round(number)
        if target.results is None:
            raise SessionError(f"round {number} is not analysed")
        passes = []
        for p in target.results.passes:
            if p.id == pass_id:
                status = PassStatus.ANALYSED if p.status is PassStatus.MISSING else p.status
                p = session.PassResult(**{**asdict(p), "verdict": verdict, "status": status})
            passes.append(p)
        if all(p.id != pass_id or p.kind is not PassKind.TEST for p in passes):
            raise SessionError(f"round {number} has no test pass {pass_id}")
        updated = session.Results(
            target.results.round,
            target.results.recording,
            target.results.thresholds,
            tuple(passes),
        )
        updated.save(target.path / session.RESULTS_FILE)
        log.info("round %d pass %d set to %s by the operator", number, pass_id, verdict)
