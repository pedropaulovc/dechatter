"""A session directory: setup.toml plus one sub-directory per round of test cuts."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import audio, detect
from .bayes import Observation
from .detect import Thresholds, Verdict
from .plan import PassKind, Plan

log = logging.getLogger(__name__)

SCHEMA = 1
SETUP_FILE = "setup.toml"
PLAN_FILE = "plan.json"
PROGRAM_FILE = "program.nc"
RESULTS_FILE = "results.json"
TRIM_FRACTION = 0.15
AIR_GUARD_S = 0.3
MIN_AIR_S = 1.0
MIN_ABORTED_S = 1.0


class SessionError(RuntimeError):
    pass


class PassStatus(StrEnum):
    ANALYSED = "analysed"
    ABORTED = "aborted"  # the cut stopped early (feed hold); analysed on what was recorded
    MISSING = "missing"  # not found in the recording


@dataclass(frozen=True)
class PassResult:
    id: int
    kind: PassKind
    rpm: int
    width_mm: float
    status: PassStatus
    verdict: Verdict | None = None
    chatter_hz: float | None = None
    spindle_hz: float | None = None
    prominence_db: float | None = None
    excess_db: float | None = None
    start_s: float | None = None
    end_s: float | None = None


@dataclass(frozen=True)
class Results:
    round: int
    recording: str
    thresholds: Thresholds
    passes: tuple[PassResult, ...]
    schema: int = SCHEMA

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Results:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise SessionError(f"{path}: unsupported results schema {data.get('schema')}")
        passes = tuple(
            PassResult(
                **{
                    **p,
                    "kind": PassKind(p["kind"]),
                    "status": PassStatus(p["status"]),
                    "verdict": Verdict(p["verdict"]) if p["verdict"] else None,
                }
            )
            for p in data.pop("passes")
        )
        return Results(**{**data, "thresholds": Thresholds(**data["thresholds"]), "passes": passes})


@dataclass(frozen=True)
class Round:
    path: Path
    plan: Plan
    results: Results | None


def round_dir(session: Path, number: int) -> Path:
    return session / f"round-{number:02d}"


def load_rounds(session: Path) -> list[Round]:
    rounds = []
    for plan_path in sorted(session.glob(f"round-*/{PLAN_FILE}")):
        results_path = plan_path.with_name(RESULTS_FILE)
        results = Results.load(results_path) if results_path.exists() else None
        rounds.append(Round(plan_path.parent, Plan.load(plan_path), results))
    return rounds


def observations(rounds: list[Round]) -> list[Observation]:
    return [
        Observation(p.rpm, p.width_mm, p.verdict, p.chatter_hz)
        for r in rounds
        if r.results is not None
        for p in r.results.passes
        if p.kind is PassKind.TEST and p.verdict is not None
    ]


def face_after(last: Round) -> tuple[float, float]:
    """(front face Y, width of the step an aborted pass left on it) after a round was run."""
    if last.results is None:
        raise SessionError(f"{last.path.name} has no results; run `dechatter analyze` first")
    widths = [p.width_mm for p in last.plan.passes]
    attempted = [i for i, p in enumerate(last.results.passes) if p.status is not PassStatus.MISSING]
    start = last.plan.face_y_start_mm
    if not attempted:
        return start, 0.0
    final = attempted[-1]
    if last.results.passes[final].status is PassStatus.ABORTED:
        return start + sum(widths[:final]), widths[final]
    return start + sum(widths[: final + 1]), 0.0


def _match(plan: Plan, samples: np.ndarray, rate: int, manual: list[audio.Segment] | None):
    """Pass index -> (segment, status)."""
    if manual is not None:
        if len(manual) > len(plan.passes):
            raise SessionError(f"{len(manual)} segments given for {len(plan.passes)} passes")
        matched = dict(enumerate(manual))
        leftovers: list[audio.Segment] = []
    else:
        alignment = audio.find_cuts(samples, rate, plan)
        matched = dict(alignment.matches)
        leftovers = alignment.unmatched

    statuses = {i: PassStatus.ANALYSED for i in matched}
    final = max(matched)
    tail = [s for s in leftovers if s.start_s > matched[final].end_s]
    cut_short = plan.passes[final].expected_cut_s / audio.DURATION_TOLERANCE
    if manual is not None and matched[final].duration_s < cut_short:
        statuses[final] = PassStatus.ABORTED
    if tail and final + 1 < len(plan.passes) and tail[0].duration_s >= MIN_ABORTED_S:
        matched[final + 1] = tail[0]
        statuses[final + 1] = PassStatus.ABORTED
    return {i: (matched[i], statuses[i]) for i in matched}


def analyze(
    plan: Plan,
    samples: np.ndarray,
    rate: int,
    recording: str,
    thresholds: Thresholds,
    manual: list[audio.Segment] | None = None,
) -> Results:
    matched = _match(plan, samples, rate, manual)
    results = []
    previous_end = 0.0
    for index, p in enumerate(plan.passes):
        base = PassResult(p.id, p.kind, p.rpm, p.width_mm, PassStatus.MISSING)
        if index not in matched:
            results.append(base)
            continue
        segment, status = matched[index]
        trim = TRIM_FRACTION * segment.duration_s
        cut = samples[int((segment.start_s + trim) * rate) : int((segment.end_s - trim) * rate)]
        air_start = max(segment.start_s - p.lead_in_s - plan.dwell_s / 2, previous_end + 0.5, 0.0)
        air_end = segment.start_s - AIR_GUARD_S
        air = samples[int(air_start * rate) : int(air_end * rate)]
        previous_end = segment.end_s
        try:
            reading = detect.classify(
                cut,
                air if air_end - air_start >= MIN_AIR_S else None,
                rate,
                p.rpm,
                plan.flutes,
                thresholds,
            )
        except detect.TooShort as exc:
            log.warning("pass %d: %s", p.id, exc)
            results.append(base)
            continue
        results.append(
            PassResult(
                p.id,
                p.kind,
                p.rpm,
                p.width_mm,
                status,
                verdict=reading.verdict,
                chatter_hz=round(reading.chatter_hz, 1),
                spindle_hz=round(reading.spindle_hz, 3),
                prominence_db=round(reading.prominence_db, 1),
                excess_db=None if reading.excess_db is None else round(reading.excess_db, 1),
                start_s=round(segment.start_s, 2),
                end_s=round(segment.end_s, 2),
            )
        )
    return Results(plan.round, recording, thresholds, tuple(results))
