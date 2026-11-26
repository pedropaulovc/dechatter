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

from . import audio, bayes, detect, gcode, seeding
from .bayes import Observation
from .config import (
    Direction,
    Setup,
    SetupFile,
    SetupSource,
    Vary,
    full_width,
    load_setup,
    relief_room_mm,
    setup_source,
    side_milled,
)
from .detect import Thresholds, Verdict
from .plan import (
    DEPTH_TOLERANCE_MM,
    ENGAGEMENT_TOLERANCE_MM,
    FOLLOW_UP,
    FRESH,
    Envelope,
    Face,
    LaneEnd,
    Leg,
    Made,
    Milling,
    PassKind,
    Plan,
    PlanError,
    PlannerKind,
    Reach,
    Relief,
    SchemaError,
    Start,
    Top,
    assemble,
    auto_lanes,
    check_tests,
    cut_direction,
    cut_size,
    cutting,
    fixed_mm,
    grid_tests,
    milling,
    mrr_mm3_min,
    other_direction,
    sweep_tests,
)
from .seeding import Seed, SeedPass, SeedStatus

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
    # Legacy statuses from 0.2.0 timeline-less runs, kept so older exported sessions still load.
    MISSING = "missing"
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


class SpeedSource(StrEnum):
    """Where the speed a pass is mapped at comes from."""

    HEARD = "heard"  # the spindle speed the detector found in the cut
    ASKED = "asked"  # none was found reliably (no cut heard, or "speed?"): the speed asked for


def heard_rpm(p: PassResult) -> float | None:
    """The spindle speed found in the pass's cut, when it can be trusted: none in a pass
    with no cut heard or not run, nor one found at the edge of the search around the speed
    asked."""
    if p.spindle_hz is None or p.status is PassStatus.QUIET or p.status in UNHEARD:
        return None
    if spindle_fit(p) is SpindleFit.EDGE:
        return None
    return round(p.spindle_hz * 60, 1)


def chip_load_cut_mm(p: PassResult, feed_mm_min: float, flutes: int) -> float | None:
    """The chip load a pass really cut at: its programmed feed (from the speed asked for)
    over the speed heard; None where no speed was heard."""
    heard = heard_rpm(p)
    return None if heard is None else round(feed_mm_min / (flutes * heard), 4)


class Facing(StrEnum):
    """What became of facing the top down mid-round, for a run that got to it: no sound
    tells (see facing_asked)."""

    DONE = "done"  # it finished: the new top is flat
    CUT_SHORT = "cut-short"  # it stopped part-way: the top is faced only in part
    NOT_RUN = "not-run"  # it never started


@dataclass(frozen=True)
class Results:
    round: int
    recording: str
    thresholds: Thresholds
    passes: tuple[PassResult, ...]
    # For a run that got to facing the top down mid-round (see facing_asked): what the
    # facing did, from the widget's record of the run or the operator's word. None
    # elsewhere, or until the operator says.
    facing: Facing | None = None
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
        facing = Facing(data["facing"]) if data.get("facing") else None
        return Results(
            **{
                **data,
                "thresholds": Thresholds(**data["thresholds"]),
                "passes": passes,
                "facing": facing,
            }
        )


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

    def __post_init__(self):
        # However the results were set (analysis, or "facing" edited in results.json), a
        # facing cut short or never run leaves nothing cut after it. Only on read: the file
        # keeps what was heard, should the facing be corrected to done.
        if self.results is not None:
            object.__setattr__(self, "results", after_facing(self.plan, self.results))


def after_facing(plan: Plan, results: Results) -> Results:
    """`results` with every pass from the leg after a mid-round facing on not run, when the
    facing was cut short or never ran: a pass heard there was something else (the facing's
    own rows, perhaps), and its verdict is no evidence, nor can one be set."""
    faced = plan.mid_facing
    if faced is None or results.facing not in (Facing.CUT_SHORT, Facing.NOT_RUN):
        return results
    start = plan.first_index(faced)
    dropped = tuple(
        PassResult(p.id, p.kind, p.rpm, p.width_mm, PassStatus.NOT_RUN)
        for p in results.passes[start:]
    )
    return dataclasses.replace(results, passes=results.passes[:start] + dropped)


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
        setup_path = plan_path.with_name(SETUP_FILE)
        if not setup_path.exists():
            raise SessionError(
                f"{plan_path.parent.name}/{SETUP_FILE} is missing: the copy of the setup the "
                "round was planned from. Put it back, or remove the round"
            )
        plan = _read(plan_path, Plan.load)
        if plan.direction is None:
            # Planned by dechatter 0.7.0 or older, when every round cut as its setup said.
            direction = _read(setup_path, load_setup).cut.direction
            plan = dataclasses.replace(plan, direction=direction)
        results_path = plan_path.with_name(RESULTS_FILE)
        results = _read(results_path, Results.load) if results_path.exists() else None
        rounds.append(Round(plan_path.parent, plan, results))
    return rounds


def as_planned(r: Round) -> Setup:
    """The setup a round was planned from, at the round's own level and cutting in its
    direction: what its program follows."""
    return cutting(at_level(planned_with(r), r.plan.fixed_mm), r.plan.direction)


def _tests(rounds: list[Round], wanted) -> list[Observation]:
    """Classified test cuts of the rounds whose level `wanted` accepts, as they were made:
    at the speed heard in each cut, else at the one asked for (see heard_rpm).

    A round's level is what its test cuts share: the depth, or where the session steps the
    depth, the width. The rounds that follow convergence are left out: a chip-load sweep's
    cuts are not at the session's chip load, and a check's not in its direction.
    """
    found = []
    for r in rounds:
        if r.results is None or r.plan.planner in FOLLOW_UP:
            continue
        if not wanted(r.plan.fixed_mm):
            continue
        sizes = {p.id: r.plan.engagement_mm(p) for p in r.plan.passes}
        found += [
            _observed(p, sizes[p.id])
            for p in r.results.passes
            if p.kind is PassKind.TEST and p.verdict is not None
        ]
    return found


def _observed(p: PassResult, engagement_mm: float) -> Observation:
    heard = heard_rpm(p)
    if heard is None:
        return Observation(float(p.rpm), engagement_mm, p.verdict, p.chatter_hz)
    return Observation(heard, engagement_mm, p.verdict, p.chatter_hz, float(p.rpm))


def _at(level_mm: float):
    return lambda level: abs(level - level_mm) <= DEPTH_TOLERANCE_MM


def _cuts(rounds: list[Round], env: Envelope, wanted) -> list[Observation]:
    """What the cuts tell the map searched now; its bounds may have narrowed since.

    A cut asked for at a speed outside the search, or chatter beyond the largest engagement
    searched, says nothing about the cells searched. A stable cut that large proves every
    smaller one stable, so it counts as stable at the largest engagement. The cuts stay in
    their rounds. The speed asked for is what the search bounded; the one heard may lie a
    little beyond it.
    """
    found = []
    for o in _tests(rounds, wanted):
        if env.contains(o.asked, o.engagement_mm):
            found.append(o)
            continue
        if env.holds_speed(o.asked) and o.verdict is Verdict.STABLE:
            found.append(dataclasses.replace(o, engagement_mm=env.engagement_top_mm))
    return found


def observations(rounds: list[Round], level_mm: float, env: Envelope) -> list[Observation]:
    """The test cuts made at one level, as the map searched now sees them."""
    return _cuts(rounds, env, _at(level_mm))


def evidence(rounds: list[Round], level_mm: float, env: Envelope) -> list[Observation]:
    """What is known at one level: the cuts made there, and the stable cuts made above it.

    A width that was stable in a deeper cut is stable in a shallower one, and a depth that
    was stable in a wider cut is stable in a narrower one. Chatter in a deeper or wider cut
    says nothing here, so those cuts are left out.
    """
    above = _cuts(rounds, env, lambda level: level > level_mm + DEPTH_TOLERANCE_MM)
    carried = [
        dataclasses.replace(o, chatter_hz=None) for o in above if o.verdict is Verdict.STABLE
    ]
    return [*observations(rounds, level_mm, env), *carried]


def levels(setup: Setup) -> list[float]:
    """The levels a session works through: the setup's own depth, then the others, deepest
    first; or where it steps the depth, its width, then the others, widest first.

    Deepest first, because a shallower cut leaves the face flat only above its own depth;
    widest first, to match. Either way, what is stable at one level carries over to the next.
    """
    others = setup.cut.other_depths_mm
    if setup.cut.vary is Vary.DEPTH:
        others = setup.cut.other_widths_mm
    return [fixed_mm(setup), *sorted(set(others), reverse=True)]


def at_level(setup: Setup, level_mm: float) -> Setup:
    """The setup with its depth, or where it steps the depth its width, at `level_mm`."""
    held = "radial_width_mm" if setup.cut.vary is Vary.DEPTH else "axial_depth_mm"
    return dataclasses.replace(setup, cut=dataclasses.replace(setup.cut, **{held: level_mm}))


@dataclass(frozen=True)
class StableCut:
    """A stable test cut: at the speed heard in it (`speed` says if it was, else the speed
    asked for), with the speed asked for and the removal rate of the feed it was cut at."""

    mrr_mm3_min: float
    depth_mm: float
    rpm: float
    width_mm: float
    asked_rpm: float
    speed: SpeedSource


def level_of(setup: Setup, cut: StableCut) -> float:
    """A cut's size in the dimension the session holds per round: the depth, or its width."""
    return cut.width_mm if setup.cut.vary is Vary.DEPTH else cut.depth_mm


def engagement_of(setup: Setup, cut: StableCut) -> float:
    """A cut's size in the dimension the session steps."""
    return cut.depth_mm if setup.cut.vary is Vary.DEPTH else cut.width_mm


def best_cuts(setup: Setup, env: Envelope, rounds: list[Round]) -> list[StableCut]:
    """Every stable test cut of the session inside the search now, power cap included, at
    whatever level, highest removal rate first. Its feed is the one programmed, from the
    speed asked for."""
    made = []
    for level in sorted({r.plan.fixed_mm for r in rounds}, reverse=True):
        at = at_level(setup, level)
        for o in _tests(rounds, _at(level)):
            if o.verdict is not Verdict.STABLE or not env.contains(o.asked, o.engagement_mm):
                continue
            width, depth = cut_size(at, o.engagement_mm)
            rate = float(mrr_mm3_min(at, env, o.asked, o.engagement_mm))
            speed = SpeedSource.ASKED if o.asked_rpm is None else SpeedSource.HEARD
            made.append(StableCut(rate, depth, o.rpm, width, o.asked, speed))
    found = [cut for cut in made if cut.mrr_mm3_min <= env.mrr_limit_mm3_min * (1 + 1e-9)]
    return sorted(found, key=lambda cut: -cut.mrr_mm3_min)


@dataclass(frozen=True)
class Stage:
    """The level to cut at next and the test cuts wanted there; no tests: nothing is left."""

    level_mm: float
    planner: PlannerKind
    tests: list
    expected_improvement: float


def seed_passes(rounds: list[Round]) -> tuple[SeedPass, ...]:
    """What another session may start from: every test cut with a verdict, the detector's
    or one set by hand, as it was made, with the speed heard in it. A chip-load sweep and a
    check of the other direction are left out, as from the map: a seed carries the session's
    own direction."""
    found = []
    for r in rounds:
        if r.results is None or r.plan.planner in FOLLOW_UP:
            continue
        planned = {p.id: p for p in r.plan.passes}
        found += [
            SeedPass(
                r.plan.round,
                p.id,
                p.rpm,
                planned[p.id].width_mm,
                planned[p.id].depth_mm,
                p.verdict,
                p.chatter_hz,
                heard_rpm(p),
            )
            for p in r.results.passes
            if p.kind is PassKind.TEST and p.verdict is not None
        ]
    return tuple(found)


def seed_status(seed: Seed, setup: Setup, env: Envelope, rounds: list[Round]) -> SeedStatus:
    """Where a seeded session stands with its seed: what the confirmation cuts that got a
    verdict made of it (see seeding.judged); until one has, still to confirm, unless there
    is nothing to confirm (no stable pass of the seed inside the search) or the session went
    on without confirming. The seed then stays on the map untested, or where none of it
    lies inside the search, is not used."""
    cuts = []
    for r in rounds:
        if r.results is None or r.plan.planner is not PlannerKind.CONFIRM:
            continue
        expected = {p.id: p.expected for p in r.plan.passes}
        cuts += [
            (expected[p.id], p.verdict)
            for p in r.results.passes
            if p.kind is PassKind.TEST and p.verdict is not None
        ]
    told = seeding.judged(cuts)
    if told is not None:
        return told
    inside = seeding.prior(seed, setup, env)
    unconfirmed = SeedStatus.UNTESTED if inside else SeedStatus.UNUSED
    if any(r.plan.planner is not PlannerKind.CONFIRM for r in rounds):
        return unconfirmed
    if not seeding.confirmation(seed, setup, env):
        return unconfirmed
    return SeedStatus.UNCONFIRMED


def carried(
    seed: Seed | None, status: SeedStatus | None, setup: Setup, env: Envelope, level_mm: float
) -> list[Observation]:
    """What the session it was started from tells a session's map at one level, each pass at
    its weight; nothing once its confirmation cuts rejected it, or when it is not used.
    `status` is where the session stands with it (from seed_status; None without a seed)."""
    if seed is None or status in (SeedStatus.REJECTED, SeedStatus.UNUSED):
        return []
    return seeding.prior(seed, at_level(setup, level_mm), env)


def grid_due(status: SeedStatus | None, rounds: list[Round]) -> bool:
    """Whether the round-1 grid is the next thing to plan: a session's first round, unless
    it has a seed to confirm (`status`: where it stands with it, None without one). And
    once more for a seeded session that has cut no grid, when its confirmation cuts rejected
    the seed, or when none of them got a verdict and nothing is left to confirm."""
    if status is SeedStatus.UNCONFIRMED:
        return False
    if not rounds:
        return True
    if status is None or any(r.plan.planner is PlannerKind.GRID for r in rounds):
        return False
    if status is SeedStatus.REJECTED:
        return True
    return status is not SeedStatus.CONFIRMED and not _tests(rounds, lambda level: True)


def next_stage(
    setup: Setup,
    env: Envelope,
    rounds: list[Round],
    found: list[StableCut],
    seed: Seed | None = None,
) -> Stage:
    """The first level, in session order, that still has a cut worth making; `found` is
    the session's best cuts, which set the removal rate to beat.

    A session started from another (`seed`) confirms that one's prediction first, instead
    of the grid, and cuts the grid after all where `grid_due` says so."""
    base = fixed_mm(setup)
    status = None if seed is None else seed_status(seed, setup, env, rounds)
    if status is SeedStatus.UNCONFIRMED:
        tests = seeding.confirmation(seed, setup, env)
        return Stage(base, PlannerKind.CONFIRM, tests, 0.0)
    if grid_due(status, rounds):
        return Stage(base, PlannerKind.GRID, grid_tests(setup, env), 0.0)
    target = found[0].mrr_mm3_min if found else 0.0
    stage = Stage(base, PlannerKind.BAYES, [], 0.0)
    for level in levels(setup):
        known = [*evidence(rounds, level, env), *carried(seed, status, setup, env, level)]
        proposal = bayes.propose(at_level(setup, level), env, known, target)
        stage = Stage(level, PlannerKind.BAYES, proposal.tests, proposal.expected_improvement)
        if stage.tests:
            return stage
    return stage


def _final(passes: tuple[PassResult, ...]) -> int | None:
    """The index of the last pass a run got to, as heard; None if none was."""
    attempted = [i for i, p in enumerate(passes) if p.status not in UNHEARD]
    return attempted[-1] if attempted else None


def _last_heard(r: Round) -> int | None:
    return _final(r.results.passes)


def _reached_facing(plan: Plan, passes: tuple[PassResult, ...]) -> int | None:
    """The leg whose mid-round facing of the top (Plan.mid_facing) the run got to: one of that
    leg's passes or a later one ran, or the last pass before the facing ran to its end. None
    when the round has no mid-round facing or stopped short of it."""
    faced = plan.mid_facing
    final = _final(passes)
    if faced is None or final is None:
        return None
    start = plan.first_index(faced)
    if final >= start:
        return faced
    if final == start - 1 and passes[final].status is not PassStatus.ABORTED:
        return faced
    return None


def face_after(last: Round) -> Face:
    """Where the stock stands after a round was run: on the lane of the last pass heard, how
    far from Y0 it was used and the step an aborted pass left on its face, with the top that
    leg was cut from. With no pass heard, where the round started (after the facing it starts
    with, if any). A slot stopped part-way leaves no step to clear: its row is used. Where
    the run got to facing the top down mid-round, `results.facing` says: done, the new top;
    cut short, the end of the leg before it on the old top, faced in part, to face again
    before any cut; not run, the last pass heard before it."""
    if last.results is None:
        raise SessionError(f"{last.path.name} has no results")
    plan, passes, facing = last.plan, last.results.passes, last.results.facing
    faced = plan.mid_facing
    if facing is None and _reached_facing(plan, passes) is not None:
        raise SessionError(f"{last.path.name} has no record of what its mid-round facing did")
    final = _last_heard(last)
    done = faced is not None and facing is Facing.DONE
    if done and (final is None or final < plan.first_index(faced)):
        # Faced, and stopped before the first cut on the new top.
        new = plan.legs[faced]
        return Face(new.lane, new.face_y_start_mm, 0.0, new.lane_cap_mm, new.top_mm)
    if faced is not None and facing is Facing.CUT_SHORT:
        end = plan.legs[faced - 1]
        return Face(end.lane, end.face_y_end_mm, 0.0, end.lane_cap_mm, end.top_mm, Top.PART_FACED)
    if final is None:
        start = plan.legs[0]
        return Face(start.lane, start.face_y_start_mm, 0.0, start.lane_cap_mm, start.top_mm)
    number, place = plan.leg_of(final)
    leg = plan.legs[number]
    steps = [p.width_mm + leg.wall_mm for p in leg.passes]
    y = leg.face_y_start_mm + sum(steps[:place])
    if passes[final].status is PassStatus.ABORTED and plan.milling is Milling.SIDE:
        return Face(leg.lane, y, steps[place], leg.lane_cap_mm, leg.top_mm)
    return Face(leg.lane, y + steps[place], 0.0, leg.lane_cap_mm, leg.top_mm)


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
            end = -_after_motion(-end, p.lead_off_s, [(-b, -a) for a, b in holds])
        stretch = _longest_free(start, end, holds)
        if stretch is None:
            if aborted:
                continue  # stopped before the cutter met the stock: not run
            # The move finished at its target, faster than its planned lead-ins (a feed override,
            # or a stepped clock): the face was cut, though no stretch is left to classify.
            mid = max((cut["start"] + cut["end"]) / 2 - t0, 0.0)
            stretch = audio.Segment(mid, mid)
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


def analyze(
    plan: Plan,
    samples: np.ndarray,
    rate: int,
    recording: str,
    thresholds: Thresholds,
    timeline: list,
) -> Results:
    """Classify each pass from the cut and dwell windows the widget recorded in `timeline`."""
    placed = placements_from_timeline(plan, timeline)
    if placed is None:
        raise audio.AudioError("the recording has no run timeline; run the round again")
    if not placed:
        raise audio.AudioError(
            "the run ended before its first cut, so there is nothing to analyse; the round is "
            "still planned: run it again"
        )
    if not samples.any():
        raise audio.AudioError(
            "no sound was recorded (the recording is silent); check the microphone, then run "
            "the round again"
        )
    results = []
    previous_end = 0.0
    for index, p in enumerate(plan.passes):
        base = PassResult(p.id, p.kind, p.rpm, p.width_mm, PassStatus.NOT_RUN)
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
            # saw the machine make: "no cut heard".
            status = PassStatus.ABORTED if place.status is PassStatus.ABORTED else PassStatus.QUIET
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
    passes = tuple(results)
    faced = _reached_facing(plan, passes)
    if faced is None:
        return Results(plan.round, recording, thresholds, passes)
    went_on = _final(passes) >= plan.first_index(faced)
    recorded = Facing.DONE if went_on else facing_in_timeline(timeline)
    return Results(plan.round, recording, thresholds, passes, recorded)


def facing_in_timeline(timeline: list) -> Facing:
    """What the widget's record of the run says its facing of the top partway did, by its
    rows: the last one finished, one stopped or none run."""
    rows = [e for e in timeline if e.get("event") == "face"]
    if any(e.get("rows_after") == 0 and not e.get("aborted") for e in rows):
        return Facing.DONE
    return Facing.CUT_SHORT if rows else Facing.NOT_RUN


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
    if source.pending:
        listed = "; ".join(f"{at} ({note})" for at, note in source.pending)
        raise PlanError(
            f"{SETUP_FILE} has values to confirm on the machine before planning: {listed}. "
            "Check each in the setup wizard and save it."
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


def _faced_at_stop(r: Round) -> bool:
    """Whether the round's run ended by facing the top all the way down, after its last cut."""
    faced = r.plan.mid_facing
    if r.results is None or r.results.facing is not Facing.DONE or faced is None:
        return False
    final = _last_heard(r)
    return final is None or final < r.plan.first_index(faced)


def _cut_since_top(rounds: list[Round]) -> list[tuple[Round, Made]]:
    """The passes cut into the top as it stands: since the block was mounted or last faced,
    every pass a run got to (every pass of a leg none was heard in, or of a round never
    analysed, as stopped part-way: it may or may not have cut), latest first, with how far
    from Y0 it reached, how deep it went, and whether it was stopped part-way; each with its
    round, its lane counted from the end the round's direction meets first."""
    made = []
    for r in reversed(rounds):
        if _faced_at_stop(r):
            return made
        final = None if r.results is None else _last_heard(r)
        results = () if r.results is None else r.results.passes
        stopped = {p.id for p in results if p.status is PassStatus.ABORTED}
        reached = _reached(r)
        for number, leg in reversed(list(enumerate(reached))):
            passes = leg.passes
            if final is not None and number == len(reached) - 1:
                passes = passes[: r.plan.leg_of(final)[1] + 1]
            y = leg.face_y_start_mm
            for p in passes:
                y += p.width_mm + leg.wall_mm
                reach = Reach.PART if final is None or p.id in stopped else Reach.END
                made.append((r, Made(leg.lane, y, p.depth_mm, r.plan.milling, reach)))
            # The top began here: faced down, or nothing was cut into it before this round.
            if leg.reface_mm > 0 or (number == 0 and r.plan.start is Start.FRESH):
                return made
    return made


def _reliefs_since_top(rounds: list[Round]) -> list[tuple[Round, Relief]]:
    """The reliefs milled into the top as it stands, latest first, each with its round, its
    boundary counted from the end the round's direction meets first. A leg's reliefs count
    once one of its passes was heard: the program mills them first."""
    found = []
    for r in reversed(rounds):
        if _faced_at_stop(r):
            return found
        final = None if r.results is None else _last_heard(r)
        reached = () if final is None else r.plan.legs[: r.plan.leg_of(final)[0] + 1]
        for leg in reversed(reached):
            found += [(r, relief) for relief in leg.reliefs]
            if leg.reface_mm > 0:
                return found
        if r.plan.legs[0].reface_mm > 0 or r.plan.start is Start.FRESH:
            return found
    return found


@dataclass(frozen=True)
class StockLeft:
    """Where a round goes on, on the stock the last round left (see _stock_left)."""

    face: Face
    made: tuple[Made, ...]
    lanes_open: int | None
    reliefs: tuple[Relief, ...] = ()


def _stock_left(rounds: list[Round], direction: Direction, kind: Milling) -> StockLeft:
    """Where a round cutting in `direction` and milling `kind` goes on, on the stock the last
    round left: the face, the passes cut into the top as it stands and the reliefs milled
    into it (lanes and boundaries counted from the end `direction` meets first), and how
    many lanes from that end it may use (None: all).

    Side milling in lanes: every lane, whichever way it was cut; the face gives only the
    top, and each leg goes to the lane with the most stock left (see plan._choose). Its
    passes start and end in reliefs, so none crosses another lane.

    Slots in lanes: each direction uses the lanes from the end it meets first, so the two
    work towards each other: a round goes on from where the last round in its direction on
    this top left the stock (the first lane at Y0 if none did), up to the lane before the
    first that holds a cut made the other way. With no lane left, it faces the top down or
    needs fresh stock.

    A single lane: every pass runs the stock's whole length, from the air beyond one end to
    the air beyond the other, so either direction goes on along its face.
    """
    last = rounds[-1]
    face = face_after(last)
    lanes = last.plan.lanes
    cuts = _cut_since_top(rounds)
    made = tuple(
        m if r.plan.direction is direction else dataclasses.replace(m, lane=lanes + 1 - m.lane)
        for r, m in cuts
    )
    if lanes == 1:
        whole = face.y_mm == 0 and face.remnant_mm == 0
        return StockLeft(face, () if whole else made, None)
    if kind is Milling.SIDE:
        reliefs = tuple(
            relief
            if r.plan.direction is direction
            else dataclasses.replace(relief, boundary=lanes - relief.boundary)
            for r, relief in _reliefs_since_top(rounds)
        )
        return StockLeft(Face(top_mm=face.top_mm, top=face.top), made, None, reliefs)
    others = [
        m.lane for (r, _), m in zip(cuts, made, strict=True) if r.plan.direction is not direction
    ]
    lanes_open = min(others) - 1 if others else None
    if last.plan.direction is not direction:
        mine = next((r for r, _ in cuts if r.plan.direction is direction), None)
        resume = mine is not None and face.top is Top.FLAT
        face = face_after(mine) if resume else Face(top_mm=face.top_mm, top=face.top)
    if lanes_open is None and face.y_mm == 0 and face.lane == 1 and face.remnant_mm == 0:
        # A top whose first lane is still whole at Y0 was not cut into.
        made = ()
    return StockLeft(face, made, lanes_open)


def _place(
    session: Path,
    source: SetupFile,
    env: Envelope,
    rounds: list[Round],
    stock: Stock,
    level_mm: float,
    planner: PlannerKind,
    tests: list,
) -> Plan:
    """Lay the tests out on the stock from where the last round left it, and write the round.

    Side milling in lanes starts in the lane with the most stock left that its first cut
    fits on, whichever way earlier rounds cut it; slots and a single lane go on where the
    last round in the round's direction left the stock (see _stock_left). The round fills
    that lane, then the lanes after it, and with stock.height_mm given, faces the top down
    and starts again at the first lane, for as long as it has tests left and the top can
    come down (see plan.assemble). A round in the other direction than the last counts its
    lanes from the other end.
    """
    setup = source.setup
    number = rounds[-1].plan.round + 1 if rounds else 1
    # The lanes are settled by the session's first round: an automatic count must not move
    # with a speed limit changed since.
    lanes = rounds[-1].plan.lanes if rounds else auto_lanes(setup, env)
    direction = cut_direction(setup, planner)
    face, made, lanes_open, reliefs = FRESH, (), None, ()
    at = cutting(at_level(setup, level_mm), direction)
    if rounds and stock is Stock.SAME:
        left = _stock_left(rounds, direction, milling(at))
        face, made, lanes_open, reliefs = left.face, left.made, left.lanes_open, left.reliefs
    plan = assemble(
        at,
        env,
        number,
        planner,
        tests,
        face=face,
        made=made,
        lanes=lanes,
        lanes_open=lanes_open,
        reliefs=reliefs,
        pick=milling(at) is Milling.SIDE and lanes > 1,
    )
    _write_round(session, source.text, at, plan)
    return plan


def plan_next(session: Path, source: SetupFile, env: Envelope, stock: Stock) -> Planned:
    """Plan the next round and write its directory, or report convergence.

    The round keeps the setup's text: the setup its plan and program came from.
    """
    rounds = _rounds_to_plan_after(session, source)
    if rounds:
        face_after(rounds[-1])  # the last round must have been analysed
    setup = source.setup
    seed = seeding.load(session)
    stage = next_stage(setup, env, rounds, best_cuts(setup, env, rounds), seed)
    if not stage.tests:
        return Planned(None, stage.expected_improvement)
    plan = _place(session, source, env, rounds, stock, stage.level_mm, stage.planner, stage.tests)
    return Planned(plan, stage.expected_improvement)


def plan_sweep(session: Path, source: SetupFile, env: Envelope, stock: Stock) -> Plan:
    """Once the map has converged: the best stable cut at rising chip loads, lightest first.

    Its cuts are kept apart from the stability map, which stays at the session's chip load.
    The chip load is not a setup change here: the sweep is the one place it varies.
    """
    rounds = _rounds_to_plan_after(session, source)
    if rounds:
        face_after(rounds[-1])
    found = best_cuts(source.setup, env, rounds)
    if next_stage(source.setup, env, rounds, found, seeding.load(session)).tests:
        raise PlanError("the stability map has not converged yet: plan the next round first")
    if not found:
        raise PlanError("no stable cut has been measured to sweep")
    best = found[0]
    tests = _sweep_of(source.setup, env, best)
    if not tests:
        raise PlanError("the power cap leaves no chip load to sweep at the best cut")
    level = level_of(source.setup, best)
    return _place(session, source, env, rounds, stock, level, PlannerKind.SWEEP, tests)


def ask_rpm(env: Envelope, cut: StableCut) -> float:
    """The speed to ask for to make a stable cut again: the one it was asked for, which ran
    as heard. On a machine with speed stops that has dropped that speed from its stops
    since, the stop nearest the speed heard."""
    if not env.speed_stops_rpm or round(cut.asked_rpm) in env.speed_stops_rpm:
        return cut.asked_rpm
    return env.nearest_stop(cut.rpm)


def _sweep_of(setup: Setup, env: Envelope, best: StableCut) -> list:
    at = at_level(setup, level_of(setup, best))
    return sweep_tests(at, env, ask_rpm(env, best), engagement_of(setup, best))


def to_sweep(
    setup: Setup, env: Envelope, found: list[StableCut], sweep: Sweep | None
) -> StableCut | None:
    """The best cut, when a chip-load sweep of it is still to run (`sweep`, from `swept`,
    is None) and the power cap leaves it a chip load to try."""
    if not found or sweep is not None:
        return None
    return found[0] if _sweep_of(setup, env, found[0]) else None


def plan_check(
    session: Path, source: SetupFile, env: Envelope, stock: Stock, level_mm: float | None = None
) -> Plan:
    """The best cut of one level, in a ramp up to it and beyond (see plan.check_tests), cut
    in the other direction than the session's: the level `level_mm`, or the first, in
    session order, still to check.

    Offered once the session has a stable cut at a level, converged or not: a session
    measured by an older dechatter whose planner would now search on still gets its check.
    Its cuts are kept apart from the stability map, which belongs to the session's direction.
    """
    rounds = _rounds_to_plan_after(session, source)
    if not rounds:
        raise PlanError("no round has been cut yet: there is no best cut to check")
    face_after(rounds[-1])  # the last round must have been analysed
    setup = source.setup
    offers = to_check(setup, env, best_cuts(setup, env, rounds), checks(rounds))
    chosen = [
        best
        for best in offers
        if level_mm is None or abs(level_of(setup, best) - level_mm) <= DEPTH_TOLERANCE_MM
    ]
    if not chosen:
        raise PlanError(_no_check(setup, level_mm))
    best = chosen[0]
    tests = check_of(setup, env, best)
    level = level_of(setup, best)
    return _place(session, source, env, rounds, stock, level, PlannerKind.CHECK, tests)


def _no_check(setup: Setup, level_mm: float | None) -> str:
    other = other_direction(setup.cut.direction)
    if level_mm is not None and full_width(at_level(setup, level_mm)):
        return (
            "a full-width slot cuts climb on one side and conventional on the other at once: "
            "there is no other direction to check"
        )
    where = "" if level_mm is None else f" at {level_mm:g} mm"
    return (
        f"no cut{where} is left to check {other}: every level's best stable cut has been "
        "checked, or none was measured there"
    )


def check_of(setup: Setup, env: Envelope, best: StableCut) -> list:
    """The cuts a check of `best` makes, at the speed to ask for to cut it again (ask_rpm),
    not the one heard in it: the dial goes back where it was. None where the power cap at
    that speed trims the best cut itself (the speed stops changed since): such a check could
    never answer for it."""
    at = at_level(setup, level_of(setup, best))
    size = engagement_of(setup, best)
    tests = check_tests(at, env, ask_rpm(env, best), size)
    if not any(abs(test[1] - size) <= ENGAGEMENT_TOLERANCE_MM for test in tests):
        return []
    return tests


@dataclass(frozen=True)
class Sweep:
    """What a chip-load sweep found at one cut, asked for at `rpm`."""

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
    # The spindle speed heard in that pass, else in the first; None where neither was heard.
    heard_rpm: float | None = None


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
        results = {p.id: p for p in r.results.passes}
        verdicts = {p.id: p.verdict for p in r.results.passes}
        tests = [p for p in r.plan.passes if p.kind is PassKind.TEST]
        stable, capped = None, False
        for p in tests:
            if verdicts.get(p.id) is not Verdict.STABLE:
                break
            if p.depth_mm * p.width_mm * p.feed_mm_min > env.mrr_limit_mm3_min:
                capped = True
                break
            stable = p
        if stable is None and capped:
            continue
        first = tests[0]
        heard = [
            heard_rpm(results[p.id]) for p in (stable, first) if p is not None and p.id in results
        ]
        heard = [rpm for rpm in heard if rpm is not None]
        found.append(
            Sweep(
                r.plan.round,
                first.rpm,
                first.width_mm,
                first.depth_mm,
                r.plan.chip_load_mm,
                None if stable is None else stable.chip_load_mm,
                None if stable is None else stable.feed_mm_min,
                None if stable is None else stable.depth_mm * stable.width_mm * stable.feed_mm_min,
                untopped=stable is not None and stable is tests[-1],
                heard_rpm=heard[0] if heard else None,
            )
        )
    return found


def swept(env: Envelope, found: list[StableCut], done: list[Sweep]) -> Sweep | None:
    """The sweep of the best cut as it stands (`found`, from best_cuts), if `done` (from
    sweeps) holds one: at the speed the cut was asked for, or would be asked for now."""
    if not found:
        return None
    best = found[0]
    for sweep in reversed(done):
        same = (
            round(sweep.rpm) in (round(best.asked_rpm), round(ask_rpm(env, best)))
            and abs(sweep.width_mm - best.width_mm) <= 1e-6
            and abs(sweep.depth_mm - best.depth_mm) <= DEPTH_TOLERANCE_MM
        )
        if same:
            return sweep
    return None


@dataclass(frozen=True)
class CheckCut:
    """One cut of a check of the other direction, and what was heard."""

    rpm: int
    width_mm: float
    depth_mm: float
    feed_mm_min: float
    status: PassStatus
    verdict: Verdict | None


@dataclass(frozen=True)
class Check:
    """What a check of the other direction found at one level, its cuts lightest first."""

    round: int
    direction: Direction
    vary: Vary
    level_mm: float
    cuts: tuple[CheckCut, ...]

    def engagement_mm(self, cut: CheckCut) -> float:
        return cut.depth_mm if self.vary is Vary.DEPTH else cut.width_mm

    @property
    def held(self) -> CheckCut | None:
        """The heaviest cut that held, with every lighter one heard; None when none did. A
        cut the run never got to says nothing: a round stopped once a cut chattered leaves
        the rest unrun, the lighter ones too where it ran deepest first."""
        stable = None
        for cut in self.cuts:
            if cut.verdict is None and cut.status in UNHEARD:
                continue
            if cut.verdict is not Verdict.STABLE:
                break
            stable = cut
        return stable


def checks(rounds: list[Round]) -> list[Check]:
    """The analysed checks of the other direction, oldest first."""
    found = []
    for r in rounds:
        if r.plan.planner is not PlannerKind.CHECK or r.results is None:
            continue
        heard = {p.id: p for p in r.results.passes}
        tests = sorted(
            (p for p in r.plan.passes if p.kind is PassKind.TEST), key=r.plan.engagement_mm
        )
        cuts = []
        for p in tests:
            result = heard.get(p.id)
            status = PassStatus.NOT_RUN if result is None else result.status
            verdict = None if result is None else result.verdict
            cuts.append(CheckCut(p.rpm, p.width_mm, p.depth_mm, p.feed_mm_min, status, verdict))
        found.append(
            Check(r.plan.round, r.plan.direction, r.plan.vary, r.plan.fixed_mm, tuple(cuts))
        )
    return found


def to_check(
    setup: Setup, env: Envelope, found: list[StableCut], done: list[Check]
) -> list[StableCut]:
    """The best cut of each level, in session order, that no check of the other direction
    has cut and heard yet (`found`: from best_cuts; `done`: from checks). A level of
    full-width slots has none to check: a slot cuts climb on one side and conventional on
    the other at once."""
    offers = []
    for level in levels(setup):
        if full_width(at_level(setup, level)):
            continue
        at = [c for c in found if abs(level_of(setup, c) - level) <= DEPTH_TOLERANCE_MM]
        if not at or any(_cut_by(setup, env, check, at[0]) for check in done):
            continue
        if check_of(setup, env, at[0]):
            offers.append(at[0])
    return offers


def _cut_by(setup: Setup, env: Envelope, check: Check, best: StableCut) -> bool:
    """Whether a check answered for `best` in the other direction: it heard that cut, or a
    lighter one (or the cut itself) chatter at its speed, which settles it without cutting
    it. A check stopped before telling either way is offered again."""
    if abs(check.level_mm - level_of(setup, best)) > DEPTH_TOLERANCE_MM:
        return False
    # The level matched: only the stepped size tells cuts apart. A pass's held size is
    # rounded to the micron (make_pass), the level's (4.7625 mm for 3/16 in) is not.
    size = engagement_of(setup, best)
    # A check asks for the speed the best cut was asked at (see check_of), as a sweep does.
    speeds = (round(best.asked_rpm), round(ask_rpm(env, best)))
    for cut in check.cuts:
        if cut.rpm not in speeds:
            continue
        same = abs(check.engagement_mm(cut) - size) <= ENGAGEMENT_TOLERANCE_MM
        if same and cut.verdict is not None:
            return True
        lighter = check.engagement_mm(cut) <= size + ENGAGEMENT_TOLERANCE_MM
        if lighter and cut.verdict is Verdict.UNSTABLE:
            return True
    return False


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
        "machine.speed_stops_rpm",
        "cut.width_max_mm",
        "cut.depth_max_mm",
        "cut.speed_min_rpm",
        "cut.speed_max_rpm",
        "cut.other_depths_mm",
        "cut.other_widths_mm",
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
    """Refuse a setup that differs from a cut round's in what was measured, or that would
    leave the lanes the rounds settled too short to side-mill between their reliefs."""
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
    lanes = rounds[-1].plan.lanes if rounds else 1
    if lanes > 1 and side_milled(setup):
        between = relief_room_mm(setup, lanes)
        if between < setup.tool.diameter_mm / 2 - 1e-9:
            raise SessionError(
                f"each of the session's {lanes} lanes would keep {between:.1f} mm of stock "
                "between the reliefs milled where it meets the lanes beside it, less than a "
                "cutter radius: lower [program] face_width_mm, or start a new session"
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


def outdated_round(session: Path) -> Round | None:
    """The unrun round dechatter 0.8.0 planned with side-milling passes that stop in the
    stock where lanes meet (see plan.LaneEnd), to be planned again: run, every pass of it
    would end engaging up to its face's depth, and it never chose the lane with the most
    stock left."""
    rounds = load_rounds(session)
    if not rounds or not _unrun(rounds[-1]):
        return None
    plan = rounds[-1].plan
    stopping = plan.milling is Milling.SIDE and plan.lanes > 1 and plan.lane_end is LaneEnd.STOP
    return rounds[-1] if stopping else None


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
            orphan = (
                aside.name.endswith(DONE) and not aside.with_name(aside.name[: -len(DONE)]).exists()
            )
            if orphan:
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
    unmarked = None
    try:
        _done(aside).touch()
    except OSError as exc:
        unmarked = exc
        log.warning("reset %s: could not mark %s as done: %s", session.name, aside, exc)
    try:
        aside.rename(discarded)
    except OSError as exc:
        if unmarked is not None:
            raise SessionError(
                f"reset {session.name}, but its discarded rounds are still in {aside} "
                f"({unmarked}; {exc}): export the session, delete it and import it back"
            ) from exc
        log.warning("reset %s: the next sweep deletes %s: %s", session.name, aside, exc)
        return
    if unmarked is None:
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
) -> Results:
    """Classify a round's passes from its recording and timeline, and save the results beside
    the plan; returned as they are read (see after_facing)."""
    timeline_path = target.path / TIMELINE_FILE
    if not timeline_path.exists():
        raise audio.AudioError(
            f"{target.path.name} has no {TIMELINE_FILE}; run the round again in the widget"
        )
    samples, rate = audio.load(recording)
    timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    results = analyze(target.plan, samples, rate, recording.name, thresholds, timeline)
    results.save(target.path / RESULTS_FILE)
    log.info(
        "analysed round %d from %s: %.0f s at %d Hz",
        target.plan.round,
        recording.name,
        len(samples) / rate,
        rate,
    )
    return after_facing(target.plan, results)
