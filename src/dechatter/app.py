"""One session as the cncjs widget sees it: its state as JSON-ready data, and its actions.

The widget records in the browser and hands the samples over; everything else (planning,
analysis, files) is the same code the command line uses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import audio, bayes, session
from .config import load_setup
from .detect import Thresholds, Verdict
from .plan import PassKind, envelope, feed_mm_min
from .session import PassStatus, SessionError, Stock

log = logging.getLogger(__name__)

RECORDING_FILE = "recording.wav"
TIMELINE_FILE = "timeline.json"
MAP_SPEEDS = 48
MAP_WIDTHS = 24
TOP_RESULTS = 5


class Next(StrEnum):
    PLAN = "plan"  # plan the first or the next round
    RUN = "run"  # a round is planned: load it, cut it, record it
    CONVERGED = "converged"


class App:
    def __init__(self, session_dir: Path, thresholds: Thresholds):
        self.session_dir = session_dir
        self.thresholds = thresholds
        self.setup = load_setup(session_dir / session.SETUP_FILE)
        self.env = envelope(self.setup)
        self.recording_round: int | None = None

    def program_name(self, number: int) -> str:
        return f"dechatter-{self.session_dir.resolve().name}-round-{number:02d}.nc"

    def _round(self, number: int) -> session.Round:
        for candidate in session.load_rounds(self.session_dir):
            if candidate.plan.round == number:
                return candidate
        raise SessionError(f"round {number} does not exist")

    def state(self) -> dict:
        rounds = session.load_rounds(self.session_dir)
        observations = session.observations(rounds)
        stability = bayes.build_map(self.setup, self.env, observations)
        speeds = np.unique(
            np.linspace(0, len(stability.speeds) - 1, MAP_SPEEDS).round().astype(int)
        )
        widths = np.unique(
            np.linspace(0, len(stability.widths) - 1, MAP_WIDTHS).round().astype(int)
        )

        upcoming = Next.PLAN
        if rounds and rounds[-1].results is None:
            upcoming = Next.RUN
        if rounds and rounds[-1].results is not None:
            proposal = bayes.propose(self.setup, self.env, observations)
            upcoming = Next.PLAN if proposal.tests else Next.CONVERGED

        tool, cut = self.setup.tool, self.setup.cut
        return {
            "session": self.session_dir.resolve().name,
            "summary": (
                f"D{tool.diameter_mm:g} mm, {tool.flutes} flutes, {cut.material}, "
                f"depth {cut.axial_depth_mm:g} mm, {self.env.chip_load_mm:g} mm/tooth"
            ),
            "next": upcoming,
            "recording_round": self.recording_round,
            "rounds": [self._round_json(r) for r in rounds],
            "observations": [
                {"rpm": o.rpm, "width_mm": o.width_mm, "verdict": o.verdict} for o in observations
            ],
            "best": [
                {
                    "rpm": o.rpm,
                    "width_mm": o.width_mm,
                    "feed_mm_min": round(feed_mm_min(self.setup, self.env, o.rpm)),
                    "mrr_cm3_min": round(mrr / 1000, 2),
                }
                for mrr, o in bayes.best_stable(self.setup, self.env, observations)[:TOP_RESULTS]
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
            "planner": r.plan.planner,
            "program": self.program_name(r.plan.round),
            "run_time_s": round(r.plan.run_time_s),
            "face_y_end_mm": round(r.plan.face_y_start_mm + r.plan.consumed_mm, 2),
            "analysed": r.results is not None,
            "has_recording": (r.path / RECORDING_FILE).exists(),
            "passes": passes,
        }

    def plan(self, stock: Stock) -> None:
        session.plan_next(self.session_dir, self.setup, self.env, stock)

    def program(self, number: int) -> str:
        return (self._round(number).path / session.PROGRAM_FILE).read_text(encoding="ascii")

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
