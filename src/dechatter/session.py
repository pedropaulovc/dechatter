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
from .config import Setup, SetupFile, SetupSource, load_setup, setup_source
from .detect import Thresholds, Verdict
from .plan import (
    Envelope,
    PassKind,
    Plan,
    PlanError,
    PlannerKind,
    SchemaError,
    assemble,
    grid_tests,
    mrr_mm3_min,
    sweep_tests,
)

log = logging.getLogger(__name__)

SCHEMA = 2  # 2: dechatter 0.2.0 (the detector's own verdict)
SETUP_FILE = "setup.toml"
RECORDING_FILE = "recording.wav"  # where the widget keeps a round's recording
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
    detected: Verdict | None = None  # the detector's own verdict; `verdict` may be overruled


class VerdictSource(StrEnum):
    DETECTOR = "detector"
    OPERATOR = "operator"  # overruled or set by hand, in the widget or in results.json


def verdict_source(p: PassResult) -> VerdictSource:
    return VerdictSource.DETECTOR if p.verdict is p.detected else VerdictSource.OPERATOR


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
            raise SchemaError(path, data.get("schema"), SCHEMA)
        passes = tuple(
            PassResult(
                **{
                    **p,
                    "kind": PassKind(p["kind"]),
                    "status": PassStatus(p["status"]),
                    "verdict": Verdict(p["verdict"]) if p["verdict"] else None,
                    "detected": Verdict(p["detected"]) if p["detected"] else None,
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


DEPTH_TOLERANCE_MM = 1e-6


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
                detected=reading.verdict,
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
    """Lay the tests out on the face as it stands, and write the round."""
    face, remnant = face_after(rounds[-1]) if rounds else (0.0, 0.0)
    if stock is Stock.FRESH:
        face, remnant = 0.0, 0.0
    number = rounds[-1].plan.round + 1 if rounds else 1
    shallowest = _shallowest_on_block(rounds)
    if face > 0 and depth_mm > shallowest + DEPTH_TOLERANCE_MM:
        raise PlanError(
            f"round {number} would cut {depth_mm:g} mm deep, but this block's face is "
            f"only flat down to {shallowest:g} mm. Mount fresh stock and plan with new stock."
        )
    at = at_depth(source.setup, depth_mm)
    plan = assemble(at, env, number, planner, face, remnant, tests)
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
    """Delete what is left of rounds discarded earlier; one that will not go is left."""
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


def contents(session: Path) -> Contents:
    rounds = _round_dirs(session)
    return Contents(len(rounds), sum((r / RECORDING_FILE).exists() for r in rounds))


def reset(session: Path) -> None:
    """Discard every round, keeping the setup: the session starts over from round 1.

    The rounds are not read, so a damaged one goes too. They are moved aside together and
    only then deleted; if moving one fails, those already moved are put back.
    """
    sweep_discarded(session)
    aside = session / f"{DISCARDED}reset-{uuid.uuid4().hex[:8]}"
    aside.mkdir()
    moved: list[Path] = []
    try:
        for folder in _round_dirs(session):
            folder.rename(aside / folder.name)
            moved.append(folder)
    except OSError:
        for folder in reversed(moved):
            (aside / folder.name).rename(folder)
        aside.rmdir()
        raise
    log.info("reset %s: discarded %d rounds", session.name, len(moved))
    try:
        shutil.rmtree(aside)
    except OSError as exc:
        log.warning("could not delete %s yet: %s", aside.name, exc)


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
