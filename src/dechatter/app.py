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
from .config import SetupSource, SpindleControl, parse_setup, setup_source
from .detect import Thresholds, Verdict
from .plan import PassKind, envelope, feed_mm_min
from .session import PassStatus, SessionError, Stock

log = logging.getLogger(__name__)

RECORDING_FILE = "recording.wav"
TIMELINE_FILE = "timeline.json"
MAP_SPEEDS = 48
MAP_WIDTHS = 24
TOP_RESULTS = 5
PROGRAM_ID_LENGTH = 6


class Next(StrEnum):
    SETUP = "setup"  # the setup is still the template: fill it in before planning
    PLAN = "plan"  # plan the first or the next round
    RUN = "run"  # a round is planned: load it, cut it, record it
    CONVERGED = "converged"


class App:
    def __init__(self, session_dir: Path, thresholds: Thresholds):
        self.session_dir = session_dir
        self.thresholds = thresholds
        # Kept as read: a round planned now stores exactly the setup it was planned from.
        self.setup_text = (session_dir / session.SETUP_FILE).read_text(encoding="utf-8")
        self.setup = parse_setup(self.setup_text, session.SETUP_FILE)
        self.env = envelope(self.setup)
        self.recording_round: int | None = None
        self.discarded_round: int | None = None  # unrun round a setup change threw away

    def program_name(self, r: session.Round) -> str:
        """Names one planned program: a round planned again after a setup change gets a new
        name, so a program still loaded in cncjs never passes for it."""
        digest = hashlib.sha256()
        # A round planned by an older dechatter has no setup copy; its plan alone names it.
        for path in (r.path / session.SETUP_FILE, r.path / session.PLAN_FILE):
            if path.exists():
                digest.update(path.read_bytes())
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
        observations = session.observations(rounds, depth)
        stability = bayes.build_map(
            session.at_depth(self.setup, depth), self.env, session.evidence(rounds, depth)
        )
        speeds = np.unique(
            np.linspace(0, len(stability.speeds) - 1, MAP_SPEEDS).round().astype(int)
        )
        widths = np.unique(
            np.linspace(0, len(stability.widths) - 1, MAP_WIDTHS).round().astype(int)
        )

        upcoming, next_depth = Next.PLAN, self.setup.cut.axial_depth_mm
        if rounds and rounds[-1].results is None:
            upcoming, next_depth = Next.RUN, depth
        if rounds and rounds[-1].results is not None:
            stage = session.next_stage(self.setup, self.env, rounds)
            upcoming = Next.PLAN if stage.tests else Next.CONVERGED
            next_depth = stage.depth_mm
        template = setup_source(self.setup_text) is SetupSource.TEMPLATE
        if upcoming is Next.PLAN and template:
            upcoming = Next.SETUP

        tool, cut = self.setup.tool, self.setup.cut
        manual = self.setup.machine.spindle is SpindleControl.MANUAL
        all_depths = ", ".join(f"{d:g}" for d in session.depths(self.setup))
        return {
            "session": self.session_dir.resolve().name,
            "summary": (
                f"D{tool.diameter_mm:g} mm, {tool.flutes} flutes, {cut.material}, "
                f"depth {all_depths} mm, {self.env.chip_load_mm:g} mm/tooth"
                f"{', manual spindle' if manual else ''}"
            ),
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
                for best in session.best_cuts(self.setup, self.env, rounds)[:TOP_RESULTS]
            ],
            "map": {
                "speeds": stability.speeds[speeds].tolist(),
                "widths": stability.widths[widths].round(2).tolist(),
                "p": stability.p_stable()[np.ix_(speeds, widths)].round(3).tolist(),
            },
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

    def plan(self, stock: Stock) -> None:
        session.plan_next(self.session_dir, self.setup_text, stock)
        self.discarded_round = None

    def program(self, number: int, units: gcode.Units) -> str:
        """The round's program in the machine's units. The file beside the plan is in mm."""
        target = self._round(number)
        planned = session.at_depth(session.planned_with(target), target.plan.axial_depth_mm)
        return gcode.render(target.plan, planned, units)

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
