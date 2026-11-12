"""A session directory: setup.toml plus one sub-directory per round of test cuts."""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import audio, bayes, detect, gcode
from .bayes import Observation
from .config import Setup, SetupSource, load_setup, parse_setup, setup_source
from .detect import Thresholds, Verdict
from .plan import (
    Envelope,
    PassKind,
    Plan,
    PlanError,
    PlannerKind,
    assemble,
    envelope,
    grid_tests,
    mrr_mm3_min,
)

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


DEPTH_TOLERANCE_MM = 1e-6


def _cuts(rounds: list[Round], wanted) -> list[Observation]:
    """Classified test cuts of the rounds whose depth `wanted` accepts."""
    return [
        Observation(p.rpm, p.width_mm, p.verdict, p.chatter_hz)
        for r in rounds
        if r.results is not None and wanted(r.plan.axial_depth_mm)
        for p in r.results.passes
        if p.kind is PassKind.TEST and p.verdict is not None
    ]


def observations(rounds: list[Round], depth_mm: float) -> list[Observation]:
    """The test cuts made at one depth."""
    return _cuts(rounds, lambda depth: abs(depth - depth_mm) <= DEPTH_TOLERANCE_MM)


def evidence(rounds: list[Round], depth_mm: float) -> list[Observation]:
    """What is known at one depth: the cuts made there, and the stable cuts made deeper.

    A width that was stable in a deeper cut is stable in a shallower one. Chatter in a
    deeper cut says nothing here, so those cuts are left out.
    """
    deeper = _cuts(rounds, lambda depth: depth > depth_mm + DEPTH_TOLERANCE_MM)
    carried = [
        Observation(o.rpm, o.width_mm, o.verdict) for o in deeper if o.verdict is Verdict.STABLE
    ]
    return [*observations(rounds, depth_mm), *carried]


def depths(setup: Setup) -> list[float]:
    """The depths a session works through: the setup's own, then the others, deepest first.

    Deepest first, because a shallower cut leaves the face flat only above its own depth.
    """
    return [setup.cut.axial_depth_mm, *sorted(set(setup.cut.other_depths_mm), reverse=True)]


def at_depth(setup: Setup, depth_mm: float) -> Setup:
    return dataclasses.replace(setup, cut=dataclasses.replace(setup.cut, axial_depth_mm=depth_mm))


@dataclass(frozen=True)
class StableCut:
    mrr_mm3_min: float
    depth_mm: float
    rpm: float
    width_mm: float


def best_cuts(setup: Setup, env: Envelope, rounds: list[Round]) -> list[StableCut]:
    """Every stable test cut of the session, at whatever depth, highest removal rate first."""
    cut_depths = sorted({r.plan.axial_depth_mm for r in rounds}, reverse=True)
    found = [
        StableCut(
            float(mrr_mm3_min(at_depth(setup, depth), env, o.rpm, o.width_mm)),
            depth,
            o.rpm,
            o.width_mm,
        )
        for depth in cut_depths
        for o in observations(rounds, depth)
        if o.verdict is Verdict.STABLE
    ]
    return sorted(found, key=lambda cut: -cut.mrr_mm3_min)


@dataclass(frozen=True)
class Stage:
    """The depth to cut at next and the test cuts wanted there; no tests: nothing is left."""

    depth_mm: float
    planner: PlannerKind
    tests: list
    expected_improvement: float


def next_stage(setup: Setup, env: Envelope, rounds: list[Round]) -> Stage:
    """The first depth, in session order, that still has a cut worth making."""
    base = setup.cut.axial_depth_mm
    if not rounds:
        return Stage(base, PlannerKind.GRID, grid_tests(setup, env), 0.0)
    found = best_cuts(setup, env, rounds)
    target = found[0].mrr_mm3_min if found else 0.0
    stage = Stage(base, PlannerKind.BAYES, [], 0.0)
    for depth in depths(setup):
        proposal = bayes.propose(at_depth(setup, depth), env, evidence(rounds, depth), target)
        stage = Stage(depth, PlannerKind.BAYES, proposal.tests, proposal.expected_improvement)
        if stage.tests:
            return stage
    return stage


def _shallowest_on_block(rounds: list[Round]) -> float:
    """The shallowest depth cut since the block now mounted was fresh."""
    shallowest = float("inf")
    for r in reversed(rounds):
        shallowest = min(shallowest, r.plan.axial_depth_mm)
        if r.plan.face_y_start_mm == 0:
            break
    return shallowest


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


class Stock(StrEnum):
    SAME = "same"  # the block from the previous round is still mounted
    FRESH = "fresh"  # a new block: the front face is back at Y0


@dataclass(frozen=True)
class Planned:
    plan: Plan | None  # None: converged, nothing left worth cutting
    expected_improvement: float


def plan_next(session: Path, setup_text: str, stock: Stock) -> Planned:
    """Plan the next round from setup file text and write its directory, or report
    convergence. The round keeps that text: the setup its plan and program came from."""
    if setup_source(setup_text) is SetupSource.TEMPLATE:
        raise PlanError(
            f"{SETUP_FILE} is still the example template, not your machine: fill in the "
            "machine, tool, cut and stock (the widget's setup wizard, or edit the file) "
            "before planning"
        )
    setup = parse_setup(setup_text, SETUP_FILE)
    env = envelope(setup)
    rounds = load_rounds(session)
    check_measured(rounds, setup)
    face, remnant = face_after(rounds[-1]) if rounds else (0.0, 0.0)
    if stock is Stock.FRESH:
        face, remnant = 0.0, 0.0
    stage = next_stage(setup, env, rounds)
    if not stage.tests:
        return Planned(None, stage.expected_improvement)
    number = rounds[-1].plan.round + 1 if rounds else 1
    shallowest = _shallowest_on_block(rounds)
    if face > 0 and stage.depth_mm > shallowest + DEPTH_TOLERANCE_MM:
        raise PlanError(
            f"round {number} would cut {stage.depth_mm:g} mm deep, but this block's face is "
            f"only flat down to {shallowest:g} mm. Mount fresh stock and plan with new stock."
        )
    at = at_depth(setup, stage.depth_mm)
    plan = assemble(at, env, number, stage.planner, face, remnant, stage.tests)
    _write_round(session, setup_text, at, plan)
    return Planned(plan, stage.expected_improvement)


# The setup fields that may change once a round was cut. They move the program around the
# cuts, bound how much stock is left, or bound where the planner searches (speeds, widths,
# depths and the power cap: the chip load and feed of a cut do not depend on them), but do
# not change what a cut was or sounded like. Every other field, [planner] aside, is part of
# what was measured; the machine's weight among them, since it derates the chip load.
FREE_AFTER_CUT = frozenset(
    {
        "machine.name",
        "machine.min_rpm",
        "machine.max_rpm",
        "machine.spindle_power_w",
        "cut.width_max_mm",
        "cut.speed_min_rpm",
        "cut.speed_max_rpm",
        "cut.other_depths_mm",
        "program.safe_z_mm",
        "program.clearance_mm",
        "program.plunge_feed_mm_min",
        "program.truing_width_mm",
        "stock.width_mm",
    }
)
# Fields that are in neither a plan nor its program: an unrun round survives their change.
OUTSIDE_PROGRAM = frozenset({"machine.name"})


def planned_with(r: Round) -> Setup:
    """The setup a round was planned from, as its directory keeps it."""
    path = r.path / SETUP_FILE
    if not path.exists():
        raise SessionError(
            f"{r.path.name} has no copy of the setup it was planned from: it was planned by an "
            "older dechatter. Start a new session"
        )
    return load_setup(path)


def _changes(old: Setup, new: Setup) -> list[str]:
    """`section.field` of every setup value that differs."""
    return [
        f"{section.name}.{entry.name}"
        for section in dataclasses.fields(Setup)
        for entry in dataclasses.fields(getattr(old, section.name))
        if getattr(getattr(old, section.name), entry.name)
        != getattr(getattr(new, section.name), entry.name)
    ]


def _measured_changes(old: Setup, new: Setup) -> list[str]:
    return [
        change
        for change in _changes(old, new)
        if not change.startswith("planner.") and change not in FREE_AFTER_CUT
    ]


def _unrun(r: Round) -> bool:
    """Planned and nothing more: no recording, timeline or results beside the plan."""
    planned = {PLAN_FILE, PROGRAM_FILE, SETUP_FILE}
    return r.results is None and {p.name for p in r.path.iterdir()} <= planned


def check_measured(rounds: list[Round], setup: Setup) -> None:
    """Refuse a setup that differs from a cut round's in what was measured."""
    for r in reversed(rounds):
        if _unrun(r):
            continue
        changed = _measured_changes(planned_with(r), setup)
        if not changed:
            continue
        raise SessionError(
            f"round {r.plan.round} was cut with another {', '.join(changed)}, and the "
            "stability map belongs to the setup it was measured with. Put the setup back, or "
            "start a new session for this one"
        )


def stale_round(session: Path, setup: Setup) -> Round | None:
    """The unrun round that `setup` would leave stale, to be planned again from it.

    A setup that changes what a cut round measured is refused.
    """
    rounds = load_rounds(session)
    check_measured(rounds, setup)
    if not rounds or not _unrun(rounds[-1]):
        return None
    if set(_changes(planned_with(rounds[-1]), setup)) <= OUTSIDE_PROGRAM:
        return None
    return rounds[-1]


DISCARDED = ".discarded-"


def set_aside(r: Round) -> Path:
    """Move a round out of the session; `restore` brings it back, deleting it is final.

    Each gets its own name, so one whose deletion failed never blocks the next.
    """
    sweep_discarded(r.path.parent)
    aside = r.path.with_name(f"{DISCARDED}{r.path.name}-{uuid.uuid4().hex[:8]}")
    r.path.rename(aside)
    return aside


def sweep_discarded(session: Path) -> None:
    """Delete what is left of rounds discarded earlier; one that will not go is left."""
    for leftover in session.glob(f"{DISCARDED}*"):
        try:
            shutil.rmtree(leftover)
        except OSError as exc:
            log.warning("could not delete %s: %s", leftover.name, exc)


def restore(aside: Path, r: Round) -> None:
    aside.rename(r.path)


def _write_round(session: Path, setup_text: str, setup: Setup, plan: Plan) -> None:
    target = round_dir(session, plan.round)
    target.mkdir()
    (target / SETUP_FILE).write_text(setup_text, encoding="utf-8")
    plan.save(target / PLAN_FILE)
    (target / PROGRAM_FILE).write_text(gcode.render(plan, setup, gcode.Units.MM), encoding="ascii")
    log.info("planned round %d: %d passes", plan.round, len(plan.passes))


def analyze_recording(
    target: Round,
    recording: Path,
    thresholds: Thresholds,
    manual: list[audio.Segment] | None = None,
) -> Results:
    """Classify a round's passes from its recording and save the results beside the plan."""
    samples, rate = audio.load(recording)
    results = analyze(target.plan, samples, rate, recording.name, thresholds, manual)
    results.save(target.path / RESULTS_FILE)
    log.info(
        "analysed round %d from %s: %.0f s at %d Hz",
        target.plan.round,
        recording.name,
        len(samples) / rate,
        rate,
    )
    return results
