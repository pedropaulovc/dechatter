"""A session directory: setup.toml plus one sub-directory per round of test cuts."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import shutil
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import audio, bayes, detect, gcode
from .bayes import Observation
from .config import Setup, SetupFile, SetupSource, load_setup, setup_source
from .detect import Thresholds, Verdict
from .plan import (
    DEPTH_TOLERANCE_MM,
    FRESH,
    Envelope,
    Face,
    Leg,
    PassKind,
    Plan,
    PlanError,
    PlannerKind,
    SchemaError,
    assemble,
    auto_lanes,
    grid_tests,
    mrr_mm3_min,
    sweep_tests,
)

log = logging.getLogger(__name__)

SCHEMA = 2  # 2: dechatter 0.2.0 (the detector's own verdict)
SETUP_FILE = "setup.toml"
RECORDING_FILE = "recording.wav"  # where the widget keeps a round's recording
REPORT_FILE = "report.html"  # the last printable report, if one was written into the session
PLAN_FILE = "plan.json"
PROGRAM_FILE = "program.nc"
RESULTS_FILE = "results.json"
TRIM_FRACTION = 0.15
AIR_GUARD_S = 0.3
MIN_AIR_S = 1.0
MIN_ABORTED_S = 1.0
# A cut no louder than this over the spindle in the air just before it, and heard as
# stable, may have cut nothing: a face planned where none was (fresh stock not mounted),
# or a cut too light to hear. Real cuts measured 1-15 dB over it (tests/fixtures/v8).
QUIET_DB = 1.0
EDGE_FRACTION = 0.9  # of the spindle-speed search window: a fit this far out is suspect
TIMELINE_FILE = "timeline.json"  # the widget's record of the run, beside its recording


class SessionError(RuntimeError):
    pass


class PassStatus(StrEnum):
    ANALYSED = "analysed"
    ABORTED = "aborted"  # the cut stopped early (feed hold); analysed on what was recorded
    MISSING = "missing"  # run, but not found in the recording
    # Found by sound alone: no cut heard after an earlier one. Likeliest not run, but it may
    # have run unheard, so a verdict may still be set by hand.
    LIKELY_NOT_RUN = "likely not run"
    NOT_RUN = "not run"  # the widget's run never reached it (or never touched the stock)
    # Run, but its cut was no louder than the spindle in the air and sounded stable, or the
    # widget saw it cut where the recording holds nothing to hear: it may have cut nothing.
    # Its face still moved, so it counts as cut; a verdict may be set by hand.
    QUIET = "no cut heard"


# Passes without a cut heard in them.
UNHEARD = frozenset({PassStatus.MISSING, PassStatus.LIKELY_NOT_RUN, PassStatus.NOT_RUN})


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
    detected: Verdict | None = None  # the detector's own verdict; `verdict` may be overruled
    over_air_db: float | None = None  # the cut's level over the spindle in the air before it


class VerdictSource(StrEnum):
    DETECTOR = "detector"
    OPERATOR = "operator"  # overruled or set by hand, in the widget or in results.json


def verdict_source(p: PassResult) -> VerdictSource:
    return VerdictSource.DETECTOR if p.verdict is p.detected else VerdictSource.OPERATOR


class SpindleFit(StrEnum):
    NEAR = "near"  # the measured spindle speed is near the pass's
    EDGE = "edge"  # at the edge of the search around it: likely another pass's speed


def spindle_fit(p: PassResult) -> SpindleFit | None:
    """How well the spindle speed measured in a pass fits the speed it was planned at."""
    if p.spindle_hz is None:
        return None
    off = abs(p.spindle_hz * 60 / p.rpm - 1)
    return SpindleFit.EDGE if off >= EDGE_FRACTION * detect.RPM_TOLERANCE else SpindleFit.NEAR


@dataclass(frozen=True)
class Results:
    round: int
    recording: str
    thresholds: Thresholds
    passes: tuple[PassResult, ...]
    schema: int = SCHEMA

    def save(self, path: Path) -> None:
        # NaN and infinity are not JSON (the widget cannot read them back): refuse them here.
        text = json.dumps(asdict(self), indent=2, allow_nan=False)
        path.write_text(text + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Results:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise SchemaError(path, data.get("schema"), SCHEMA)
        passes = tuple(
            PassResult(
                **{
                    **_finite(p),
                    "kind": PassKind(p["kind"]),
                    "status": PassStatus(p["status"]),
                }
            )
            for p in data.pop("passes")
        )
        return Results(**{**data, "thresholds": Thresholds(**data["thresholds"]), "passes": passes})


def _finite(p: dict) -> dict:
    """A saved pass, its verdicts parsed, without the NaN 0.5.0 wrote for a silent cut: the
    numbers that are not numbers are dropped, and with a prominence that is not one, the
    detector's verdict too (it measured nothing; a verdict set by hand stays)."""
    verdict = Verdict(p["verdict"]) if p["verdict"] else None
    detected = Verdict(p["detected"]) if p["detected"] else None
    void = [k for k, v in p.items() if isinstance(v, float) and not math.isfinite(v)]
    if not void:
        return {**p, "verdict": verdict, "detected": detected}
    log.warning("pass %s: %s not a number: dropped", p["id"], ", ".join(void))
    if "prominence_db" not in void:
        return {**p, **dict.fromkeys(void), "verdict": verdict, "detected": detected}
    if verdict is detected:
        verdict = None
    return {**p, **dict.fromkeys(void), "verdict": verdict, "detected": None}


@dataclass(frozen=True)
class Round:
    path: Path
    plan: Plan
    results: Results | None


def round_dir(session: Path, number: int) -> Path:
    return session / f"round-{number:02d}"


def _read(path: Path, load):
    """A round's file, or a SessionError that says what to do about it."""
    try:
        return load(path)
    except SchemaError as exc:
        raise SessionError(f"{path.parent.name}/{exc}. Start a new session") from exc
    except (KeyError, TypeError, ValueError) as exc:
        problem = f"missing {exc}" if isinstance(exc, KeyError) else str(exc)
        raise SessionError(
            f"{path.parent.name}/{path.name} cannot be read ({problem}). Fix it by hand, or "
            "remove the round"
        ) from exc


def load_rounds(session: Path) -> list[Round]:
    """The session's rounds. A round this version cannot read is refused, and with it the
    session, with what to do: start a new session for an older one, else repair it."""
    rounds = []
    for plan_path in sorted(session.glob(f"round-*/{PLAN_FILE}")):
        plan = _read(plan_path, Plan.load)
        if not plan_path.with_name(SETUP_FILE).exists():
            raise SessionError(
                f"{plan_path.parent.name}/{SETUP_FILE} is missing: the copy of the setup the "
                "round was planned from. Put it back, or remove the round"
            )
        results_path = plan_path.with_name(RESULTS_FILE)
        results = _read(results_path, Results.load) if results_path.exists() else None
        rounds.append(Round(plan_path.parent, plan, results))
    return rounds


def _tests(rounds: list[Round], wanted) -> list[Observation]:
    """Classified test cuts of the rounds whose depth `wanted` accepts, as they were made.

    A chip-load sweep is left out: its cuts are not at the session's chip load.
    """
    return [
        Observation(p.rpm, p.width_mm, p.verdict, p.chatter_hz)
        for r in rounds
        if r.results is not None
        and r.plan.planner is not PlannerKind.SWEEP
        and wanted(r.plan.axial_depth_mm)
        for p in r.results.passes
        if p.kind is PassKind.TEST and p.verdict is not None
    ]


def _at(depth_mm: float):
    return lambda depth: abs(depth - depth_mm) <= DEPTH_TOLERANCE_MM


def _cuts(rounds: list[Round], env: Envelope, wanted) -> list[Observation]:
    """What the cuts tell the map searched now; its bounds may have narrowed since.

    A cut at a speed outside the search, or chatter wider than the widest width searched,
    says nothing about the cells searched. A stable cut that wide proves every narrower
    width stable, so it counts as stable at the widest width. The cuts stay in their rounds.
    """
    found = []
    for o in _tests(rounds, wanted):
        if env.contains(o.rpm, o.width_mm):
            found.append(o)
            continue
        if env.holds_speed(o.rpm) and o.verdict is Verdict.STABLE:
            found.append(Observation(o.rpm, env.width_top_mm, o.verdict, o.chatter_hz))
    return found


def observations(rounds: list[Round], depth_mm: float, env: Envelope) -> list[Observation]:
    """The test cuts made at one depth, as the map searched now sees them."""
    return _cuts(rounds, env, _at(depth_mm))


def evidence(rounds: list[Round], depth_mm: float, env: Envelope) -> list[Observation]:
    """What is known at one depth: the cuts made there, and the stable cuts made deeper.

    A width that was stable in a deeper cut is stable in a shallower one. Chatter in a
    deeper cut says nothing here, so those cuts are left out.
    """
    deeper = _cuts(rounds, env, lambda depth: depth > depth_mm + DEPTH_TOLERANCE_MM)
    carried = [
        Observation(o.rpm, o.width_mm, o.verdict) for o in deeper if o.verdict is Verdict.STABLE
    ]
    return [*observations(rounds, depth_mm, env), *carried]


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
    """Every stable test cut of the session inside the search now, power cap included, at
    whatever depth, highest removal rate first."""
    cut_depths = sorted({r.plan.axial_depth_mm for r in rounds}, reverse=True)
    made = [
        StableCut(
            float(mrr_mm3_min(at_depth(setup, depth), env, o.rpm, o.width_mm)),
            depth,
            o.rpm,
            o.width_mm,
        )
        for depth in cut_depths
        for o in _tests(rounds, _at(depth))
        if o.verdict is Verdict.STABLE and env.contains(o.rpm, o.width_mm)
    ]
    found = [cut for cut in made if cut.mrr_mm3_min <= env.mrr_limit_mm3_min * (1 + 1e-9)]
    return sorted(found, key=lambda cut: -cut.mrr_mm3_min)


@dataclass(frozen=True)
class Stage:
    """The depth to cut at next and the test cuts wanted there; no tests: nothing is left."""

    depth_mm: float
    planner: PlannerKind
    tests: list
    expected_improvement: float


def next_stage(setup: Setup, env: Envelope, rounds: list[Round], found: list[StableCut]) -> Stage:
    """The first depth, in session order, that still has a cut worth making; `found` is
    the session's best cuts, which set the removal rate to beat."""
    base = setup.cut.axial_depth_mm
    if not rounds:
        return Stage(base, PlannerKind.GRID, grid_tests(setup, env), 0.0)
    target = found[0].mrr_mm3_min if found else 0.0
    stage = Stage(base, PlannerKind.BAYES, [], 0.0)
    for depth in depths(setup):
        proposal = bayes.propose(at_depth(setup, depth), env, evidence(rounds, depth, env), target)
        stage = Stage(depth, PlannerKind.BAYES, proposal.tests, proposal.expected_improvement)
        if stage.tests:
            return stage
    return stage


def _last_heard(r: Round) -> int | None:
    """The index of the last pass a round's run got to, as heard; None if none was."""
    attempted = [i for i, p in enumerate(r.results.passes) if p.status not in UNHEARD]
    return attempted[-1] if attempted else None


def face_after(last: Round) -> Face:
    """Where the stock stands after a round was run: on the lane of the last pass heard, its
    face and the step an aborted pass left on it, with the top that leg was cut from. With
    no pass heard, where the round started (after the facing it starts with, if any)."""
    if last.results is None:
        raise SessionError(f"{last.path.name} has no results; run `dechatter analyze` first")
    final = _last_heard(last)
    if final is None:
        start = last.plan.legs[0]
        return Face(start.lane, start.face_y_start_mm, 0.0, start.lane_cap_mm, start.top_mm)
    number, place = last.plan.leg_of(final)
    leg = last.plan.legs[number]
    widths = [p.width_mm for p in leg.passes]
    y = leg.face_y_start_mm + sum(widths[:place])
    if last.results.passes[final].status is PassStatus.ABORTED:
        return Face(leg.lane, y, widths[place], leg.lane_cap_mm, leg.top_mm)
    return Face(leg.lane, y + widths[place], 0.0, leg.lane_cap_mm, leg.top_mm)


@dataclass(frozen=True)
class Placement:
    """Where a pass's cut is in a recording, and the spindle-only sound just before it."""

    cut: audio.Segment
    status: PassStatus
    air: audio.Segment | None = None  # None: guess it from the time before the cut


def placements_from_timeline(plan: Plan, timeline: list) -> dict[int, Placement] | None:
    """Pass index -> where the widget recorded it running; None without such a record.

    The widget times each pass's cut move, from sending it to Idle at its end, and the
    dwell before it. The cutter meets the stock a lead-in of motion after the move starts
    and leaves it as long before it ends; the machine stands still during a hold, so a hold
    in the lead-in delays contact by as long, and a hold in the cut is left out of what is
    analysed. A pass with no cut window, or stopped before contact, never ran.
    """
    starts = [e["t"] for e in timeline if e.get("event") == "record"]
    if not starts:
        return None
    t0 = starts[0]
    cuts = {e["pass"]: e for e in timeline if e.get("event") == "cut"}
    dwells = {e["pass"]: e for e in timeline if e.get("event") == "dwell"}
    found = {}
    for index, p in enumerate(plan.passes):
        cut = cuts.get(p.id)
        if cut is None:
            continue
        aborted = bool(cut.get("aborted"))
        holds = [(a - t0, b - t0) for a, b in cut.get("holds") or []]
        start = _after_motion(cut["start"] - t0, p.lead_in_s, holds)
        end = cut["end"] - t0
        if not aborted:
            end = -_after_motion(-end, p.lead_in_s, [(-b, -a) for a, b in holds])
        stretch = _longest_free(start, end, holds)
        if stretch is None:
            continue  # stopped before the cutter met the stock: not run
        status = PassStatus.ABORTED if aborted else PassStatus.ANALYSED
        dwell = dwells.get(p.id)
        air = None if dwell is None else audio.Segment(dwell["start"] - t0, dwell["end"] - t0)
        found[index] = Placement(stretch, status, air)
    return found


def _after_motion(t: float, motion_s: float, holds: list[tuple[float, float]]) -> float:
    """When a machine moving from time t, standing still during each hold, has moved for
    motion_s seconds."""
    for hold_start, hold_end in sorted(holds):
        if hold_end <= t:
            continue
        if hold_start >= t + motion_s:
            break
        motion_s -= max(hold_start - t, 0.0)
        t = hold_end
    return t + motion_s


def _longest_free(
    start: float, end: float, holds: list[tuple[float, float]]
) -> audio.Segment | None:
    """The longest stretch of [start, end] without a hold in it, None if there is none: the
    machine stood still during a hold, and the sound of a still cutter is no cut."""
    free, at = [], start
    for hold_start, hold_end in sorted(holds):
        if hold_start > at:
            free.append(audio.Segment(at, min(hold_start, end)))
        at = max(at, hold_end)
    if at < end:
        free.append(audio.Segment(at, end))
    free = [s for s in free if s.duration_s > 0]
    return max(free, key=lambda s: s.duration_s, default=None)


def _match(plan: Plan, samples: np.ndarray, rate: int, manual: list[audio.Segment] | None):
    """Pass index -> Placement, from the sound alone (or cuts marked by hand)."""
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
    return {i: Placement(matched[i], statuses[i]) for i in matched}


def analyze(
    plan: Plan,
    samples: np.ndarray,
    rate: int,
    recording: str,
    thresholds: Thresholds,
    manual: list[audio.Segment] | None = None,
    timeline: list | None = None,
) -> Results:
    """Classify each pass. Where the widget recorded the run (`timeline`), its windows say
    where every cut is; otherwise the cuts are found by their sound."""
    placed = (
        None if manual is not None or timeline is None else placements_from_timeline(plan, timeline)
    )
    windowed = placed is not None
    if windowed and not placed:
        raise audio.AudioError(
            "the run ended before its first cut, so there is nothing to analyse; the round is "
            "still planned: run it again"
        )
    if placed is None:
        placed = _match(plan, samples, rate, manual)
    last = max(placed, default=-1)
    results = []
    previous_end = 0.0
    for index, p in enumerate(plan.passes):
        unplaced = PassStatus.MISSING
        if index > last:
            unplaced = PassStatus.NOT_RUN if windowed else PassStatus.LIKELY_NOT_RUN
        if windowed and index < last:
            unplaced = PassStatus.NOT_RUN  # the run passed it by without cutting: never cut
        base = PassResult(p.id, p.kind, p.rpm, p.width_mm, unplaced)
        if index not in placed:
            results.append(base)
            continue
        place = placed[index]
        segment = place.cut
        trim = TRIM_FRACTION * segment.duration_s
        cut = samples[int((segment.start_s + trim) * rate) : int((segment.end_s - trim) * rate)]
        if place.air is not None:
            air_start, air_end = place.air.start_s, place.air.end_s
        else:
            air_start = max(
                segment.start_s - p.lead_in_s - plan.dwell_s / 2, previous_end + 0.5, 0.0
            )
            air_end = segment.start_s - AIR_GUARD_S
        air = samples[int(max(air_start, 0.0) * rate) : int(max(air_end, 0.0) * rate)]
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
        except (detect.TooShort, detect.Silent) as exc:
            log.warning("pass %d: %s", p.id, exc)
            # Too short to hear, or silent (the recording stopped before it). A cut stopped
            # part-way still left its step on the face, heard or not. So did one the widget
            # saw the machine make: "no cut heard". Found by sound, it is not in the recording.
            status = PassStatus.QUIET if windowed else PassStatus.MISSING
            if place.status is PassStatus.ABORTED:
                status = PassStatus.ABORTED
            results.append(dataclasses.replace(base, status=status))
            continue
        over_air = None
        if air_end - air_start >= MIN_AIR_S:
            over_air = audio.level_db(cut, rate) - audio.level_db(air, rate)
        quiet = (
            over_air is not None
            and over_air < QUIET_DB
            and reading.verdict is Verdict.STABLE
            and place.status is PassStatus.ANALYSED
        )
        if quiet:
            log.warning("pass %d: no louder than the spindle in the air: no cut heard", p.id)
            reading = dataclasses.replace(reading, verdict=None)
        results.append(
            PassResult(
                p.id,
                p.kind,
                p.rpm,
                p.width_mm,
                PassStatus.QUIET if quiet else place.status,
                verdict=reading.verdict,
                chatter_hz=round(reading.chatter_hz, 1),
                spindle_hz=round(reading.spindle_hz, 3),
                prominence_db=round(reading.prominence_db, 1),
                excess_db=None if reading.excess_db is None else round(reading.excess_db, 1),
                start_s=round(segment.start_s, 2),
                end_s=round(segment.end_s, 2),
                detected=reading.verdict,
                over_air_db=None if over_air is None else round(over_air, 1),
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


def _rounds_to_plan_after(session: Path, source: SetupFile) -> list[Round]:
    """The session's rounds, once the setup is one a round may be planned from."""
    if setup_source(source.text) is SetupSource.TEMPLATE:
        raise PlanError(
            f"{SETUP_FILE} is still the example template, not your machine: fill in the "
            "machine, tool, cut and stock (the widget's setup wizard, or edit the file) "
            "before planning"
        )
    rounds = load_rounds(session)
    check_measured(rounds, source.setup)
    return rounds


def _reached(r: Round) -> tuple[Leg, ...]:
    """The legs of a round its run got to: up to that of its last pass heard (the first, if
    none was), or every leg of a round never analysed."""
    if r.results is None:
        return r.plan.legs
    final = _last_heard(r)
    return r.plan.legs[: 1 if final is None else r.plan.leg_of(final)[0] + 1]


def _cut_since_top(rounds: list[Round]) -> tuple[float, ...]:
    """The depths cut into the top as it stands: since the block was mounted or last faced,
    the depth of every leg a run got to."""
    cut = []
    for r in reversed(rounds):
        for leg in reversed(_reached(r)):
            cut.append(r.plan.axial_depth_mm)
            if leg.face_y_start_mm == 0 and leg.lane == 1:
                return tuple(cut)
    return tuple(cut)


def _place(
    session: Path,
    source: SetupFile,
    env: Envelope,
    rounds: list[Round],
    stock: Stock,
    depth_mm: float,
    planner: PlannerKind,
    tests: list,
) -> Plan:
    """Lay the tests out on the stock from where the last round left it, and write the round.

    The round fills what is left of the lane it is on, then the lanes after it, and with
    stock.height_mm given, faces the top down and starts again at the first lane, for as long
    as it has tests left and the top can come down (see plan.assemble).
    """
    setup = source.setup
    number = rounds[-1].plan.round + 1 if rounds else 1
    # The lanes are settled by the session's first round: an automatic count must not move
    # with a speed limit changed since.
    lanes = rounds[-1].plan.lanes if rounds else auto_lanes(setup, env)
    face, cut = FRESH, ()
    if rounds and stock is Stock.SAME:
        face = face_after(rounds[-1])
        # A top whose first lane is still whole at Y0 was not cut into.
        if face.y_mm > 0 or face.lane > 1:
            cut = _cut_since_top(rounds)
    at = at_depth(setup, depth_mm)
    plan = assemble(at, env, number, planner, tests, face=face, cut_mm=cut, lanes=lanes)
    _write_round(session, source.text, at, plan)
    return plan


def plan_next(session: Path, source: SetupFile, env: Envelope, stock: Stock) -> Planned:
    """Plan the next round and write its directory, or report convergence.

    The round keeps the setup's text: the setup its plan and program came from.
    """
    rounds = _rounds_to_plan_after(session, source)
    if rounds:
        face_after(rounds[-1])  # the last round must have been analysed
    stage = next_stage(source.setup, env, rounds, best_cuts(source.setup, env, rounds))
    if not stage.tests:
        return Planned(None, stage.expected_improvement)
    plan = _place(session, source, env, rounds, stock, stage.depth_mm, stage.planner, stage.tests)
    return Planned(plan, stage.expected_improvement)


def plan_sweep(session: Path, source: SetupFile, env: Envelope, stock: Stock) -> Plan:
    """Once the map has converged: the best stable cut at rising chip loads, lightest first.

    Its cuts are kept apart from the speed x width map, which stays at the session's chip
    load. The chip load is not a setup change here: the sweep is the one place it varies.
    """
    rounds = _rounds_to_plan_after(session, source)
    if rounds:
        face_after(rounds[-1])
    found = best_cuts(source.setup, env, rounds)
    if next_stage(source.setup, env, rounds, found).tests:
        raise PlanError("the stability map has not converged yet: plan the next round first")
    if not found:
        raise PlanError("no stable cut has been measured to sweep")
    best = found[0]
    tests = _sweep_of(source.setup, env, best)
    if not tests:
        raise PlanError("the power cap leaves no chip load to sweep at the best cut")
    return _place(session, source, env, rounds, stock, best.depth_mm, PlannerKind.SWEEP, tests)


def _sweep_of(setup: Setup, env: Envelope, best: StableCut) -> list:
    return sweep_tests(at_depth(setup, best.depth_mm), env, best.rpm, best.width_mm)


def to_sweep(
    setup: Setup, env: Envelope, found: list[StableCut], sweep: Sweep | None
) -> StableCut | None:
    """The best cut, when a chip-load sweep of it is still to run (`sweep`, from `swept`,
    is None) and the power cap leaves it a chip load to try."""
    if not found or sweep is not None:
        return None
    return found[0] if _sweep_of(setup, env, found[0]) else None


@dataclass(frozen=True)
class Sweep:
    """What a chip-load sweep found at one cut."""

    round: int
    rpm: float
    width_mm: float
    depth_mm: float
    session_chip_load_mm: float
    # The highest chip load that stayed stable, its pass's feed and removal rate; all None
    # when not even the lightest pass did.
    stable_chip_load_mm: float | None
    feed_mm_min: float | None
    mrr_mm3_min: float | None
    # Every chip load tried stayed stable: the limit lies above them, not at the last one.
    untopped: bool = False


def sweeps(rounds: list[Round], env: Envelope) -> list[Sweep]:
    """The analysed chip-load sweeps, oldest first.

    Passes run lightest first, so the result is the last chip load stable before the first
    pass that chattered, aborted unheard or was not found, and within the power cap now.
    A sweep stable only above that cap has no result.
    """
    found = []
    for r in rounds:
        if r.plan.planner is not PlannerKind.SWEEP or r.results is None:
            continue
        verdicts = {p.id: p.verdict for p in r.results.passes}
        tests = [p for p in r.plan.passes if p.kind is PassKind.TEST]
        depth = r.plan.axial_depth_mm
        stable, capped = None, False
        for p in tests:
            if verdicts.get(p.id) is not Verdict.STABLE:
                break
            if depth * p.width_mm * p.feed_mm_min > env.mrr_limit_mm3_min:
                capped = True
                break
            stable = p
        if stable is None and capped:
            continue
        first = tests[0]
        found.append(
            Sweep(
                r.plan.round,
                first.rpm,
                first.width_mm,
                depth,
                r.plan.chip_load_mm,
                None if stable is None else stable.chip_load_mm,
                None if stable is None else stable.feed_mm_min,
                None if stable is None else depth * stable.width_mm * stable.feed_mm_min,
                untopped=stable is not None and stable is tests[-1],
            )
        )
    return found


def swept(found: list[StableCut], done: list[Sweep]) -> Sweep | None:
    """The sweep of the best cut as it stands (`found`, from best_cuts), if `done` (from
    sweeps) holds one."""
    if not found:
        return None
    best = found[0]
    for sweep in reversed(done):
        same = (
            round(sweep.rpm) == round(best.rpm)
            and abs(sweep.width_mm - best.width_mm) <= 1e-6
            and abs(sweep.depth_mm - best.depth_mm) <= DEPTH_TOLERANCE_MM
        )
        if same:
            return sweep
    return None


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
        "machine.spindle_power_hp",
        "cut.width_max_mm",
        "cut.speed_min_rpm",
        "cut.speed_max_rpm",
        "cut.other_depths_mm",
        "program.safe_z_mm",
        "program.clearance_mm",
        "program.plunge_feed_mm_min",
        "program.truing_width_mm",
        "stock.width_mm",
        "stock.height_mm",
        "stock.thickness_mm",
        "program.face_depth_mm",
        "program.face_width_mm",
    }
)
# Fields that are in neither a plan nor its program: an unrun round survives their change.
OUTSIDE_PROGRAM = frozenset({"machine.name"})


def planned_with(r: Round) -> Setup:
    """The setup a round was planned from, as its directory keeps it."""
    return load_setup(r.path / SETUP_FILE)


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
    """Delete what is left of rounds discarded earlier, and of resets that did not finish
    (see RESETTING); one that will not go is left for the next sweep."""
    _finish_resets(session)
    for leftover in session.glob(f"{DISCARDED}*"):
        try:
            shutil.rmtree(leftover)
        except OSError as exc:
            log.warning("could not delete %s: %s", leftover.name, exc)


def restore(aside: Path, r: Round) -> None:
    aside.rename(r.path)


def _round_dirs(session: Path) -> list[Path]:
    return sorted(p for p in session.glob("round-*") if p.is_dir())


@dataclass(frozen=True)
class Contents:
    """What a session holds, counted from its folders alone: it may not load."""

    rounds: int
    recordings: int


# A reset moves the rounds into `.reset-<hex>`. Once every round is in it, a sibling file
# `.reset-<hex>.done` marks it as meant to go, and it is renamed `.discarded-<hex>` and
# deleted. Whatever of that fails, the sweep finishes: it deletes a marked `.reset-*` and an
# empty one, and keeps an unmarked one with rounds in it. That one holds rounds a failed
# reset could not put back: the error named it, no sweep deletes it, and Export keeps it.
RESETTING = ".reset-"
DONE = ".done"


def _done(aside: Path) -> Path:
    return aside.with_name(aside.name + DONE)


def _discarded_for(aside: Path) -> Path:
    return aside.with_name(DISCARDED + aside.name[len(RESETTING) :])


def finished_reset(path: Path) -> bool:
    """A reset's folder, or its marker, that is only waiting to be deleted."""
    if path.name.endswith(DONE):
        return path.name.startswith(RESETTING)
    return path.name.startswith(RESETTING) and _done(path).exists()


def _finish_resets(session: Path) -> None:
    for aside in session.glob(f"{RESETTING}*"):
        if not aside.is_dir():
            orphan = aside.name.endswith(DONE)
            if orphan and not aside.with_name(aside.name[: -len(DONE)]).exists():
                try:
                    aside.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not remove %s: %s", aside.name, exc)
            continue
        try:
            if not any(aside.iterdir()):
                aside.rmdir()
            elif _done(aside).exists():
                aside.rename(_discarded_for(aside))
            else:
                continue
            _done(aside).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("could not finish %s: %s", aside.name, exc)


def _put_back(session: Path, aside: Path, moved: list[Path], exc: OSError) -> None:
    """Return the rounds a failed reset moved; any that will not go back stay in `aside`."""
    stuck = []
    for folder in reversed(moved):
        try:
            (aside / folder.name).rename(folder)
        except OSError:
            stuck.append(folder.name)
    if stuck:
        raise SessionError(
            f"could not reset {session.name}: {exc}; {', '.join(stuck)} could not be put "
            f"back and are in {aside}"
        ) from exc
    try:
        aside.rmdir()
    except OSError as cleanup:
        log.warning("reset %s: could not remove the empty %s: %s", session.name, aside, cleanup)


def contents(session: Path) -> Contents:
    rounds = _round_dirs(session)
    return Contents(len(rounds), sum((r / RECORDING_FILE).exists() for r in rounds))


def reset(session: Path) -> None:
    """Discard every round and the report, keeping the setup: the session starts over.

    The rounds are not read, so a damaged one goes too. See RESETTING for how they are
    kept safe until the reset has succeeded.
    """
    sweep_discarded(session)
    marker = uuid.uuid4().hex[:8]
    aside = session / f"{RESETTING}{marker}"
    try:
        aside.mkdir()
        moved: list[Path] = []
        try:
            for folder in _round_dirs(session):
                folder.rename(aside / folder.name)
                moved.append(folder)
        except OSError as exc:
            _put_back(session, aside, moved, exc)
            raise
    except OSError as exc:
        raise SessionError(f"could not reset {session.name}: {exc}") from exc
    log.info("reset %s: discarded %d rounds", session.name, len(moved))
    try:
        (session / REPORT_FILE).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("reset %s: could not remove %s: %s", session.name, REPORT_FILE, exc)
    discarded = _discarded_for(aside)
    try:
        _done(aside).touch()
    except OSError as unmarked:
        # Unmarked, no sweep would delete it: discard it now, or say where the rounds are.
        try:
            aside.rename(discarded)
        except OSError as exc:
            raise SessionError(
                f"reset {session.name}, but its discarded rounds are still in {aside} "
                f"({unmarked}; {exc}): delete that folder"
            ) from exc
    else:
        try:
            aside.rename(discarded)
        except OSError as exc:
            log.warning("reset %s: the next sweep deletes %s: %s", session.name, aside, exc)
            return
        try:
            _done(aside).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("reset %s: the next sweep removes %s: %s", session.name, _done(aside), exc)
    try:
        shutil.rmtree(discarded)
    except OSError as exc:
        log.warning("reset %s: the next sweep deletes %s: %s", session.name, discarded, exc)


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
    timeline_path = target.path / TIMELINE_FILE
    timeline = (
        json.loads(timeline_path.read_text(encoding="utf-8")) if timeline_path.exists() else None
    )
    results = analyze(target.plan, samples, rate, recording.name, thresholds, manual, timeline)
    results.save(target.path / RESULTS_FILE)
    log.info(
        "analysed round %d from %s: %.0f s at %d Hz",
        target.plan.round,
        recording.name,
        len(samples) / rate,
        rate,
    )
    return results
