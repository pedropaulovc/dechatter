"""Test plans: the ordered passes of one round, where on the stock they run, and the
envelope that bounds them."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import numpy as np

from . import materials
from .config import Direction, Setup, SpindleControl

# 2: dechatter 0.2.0 (planned_on, per-pass chip loads); 3: 0.6.0 (a round in legs, across
# lanes and faced tops)
SCHEMA = 3
SPINDLE_EFFICIENCY = 0.8
SPEED_TOLERANCE_RPM = 0.5
WIDTH_TOLERANCE_MM = 1e-6
DEPTH_TOLERANCE_MM = 1e-6


class PlanError(RuntimeError):
    pass


class SchemaError(PlanError):
    """A session file in another format than this version writes."""

    def __init__(self, path: Path, found: object, wanted: int):
        age = "an older" if isinstance(found, int) and found < wanted else "another"
        super().__init__(f"{path.name} was written by {age} dechatter (schema {found})")


class PassKind(StrEnum):
    TEST = "test"
    TRUING = "truing"
    CLEANUP = "cleanup"


class PlannerKind(StrEnum):
    GRID = "grid"
    BAYES = "bayes"
    SWEEP = "sweep"  # one cut at rising chip loads; not part of the speed x width map


@dataclass(frozen=True)
class Pass:
    id: int
    kind: PassKind
    rpm: int
    width_mm: float
    feed_mm_min: float
    expected_cut_s: float
    lead_in_s: float
    p_stable: float | None = None
    chip_load_mm: float | None = None  # a chip-load sweep's own; else the plan's


@dataclass(frozen=True)
class Leg:
    """Part of a round cut on one lane's face. With `reface_mm`, the program first takes the
    top down that much, to `top_mm` below Z0, its feed moves taking `face_s`. Then the passes
    cut the face back from `face_y_start_mm`, no further than `lane_cap_mm`: the face the lane
    before it was left with (None: the stock's width)."""

    lane: int
    face_y_start_mm: float
    passes: tuple[Pass, ...]
    lane_cap_mm: float | None = None
    top_mm: float = 0.0
    reface_mm: float = 0.0
    face_s: float = 0.0

    @property
    def face_y_end_mm(self) -> float:
        return self.face_y_start_mm + sum(p.width_mm for p in self.passes)


@dataclass(frozen=True)
class Plan:
    round: int
    planner: PlannerKind
    flutes: int
    diameter_mm: float
    axial_depth_mm: float
    chip_load_mm: float
    dwell_s: float
    plunge_s: float  # feed move from the clearance plane down to depth, beside the stock
    # The passes in the order they run, in legs of one lane's face each: the round fills what
    # is left of its lane, goes on into the lanes after it, and once every lane is used may
    # face the top down and start again at the first lane (1 is the one the cutter meets first).
    legs: tuple[Leg, ...]
    planned_on: str  # ISO date
    lanes: int = 1
    schema: int = SCHEMA

    @cached_property
    def passes(self) -> tuple[Pass, ...]:
        return tuple(p for leg in self.legs for p in leg.passes)

    def leg_of(self, index: int) -> tuple[int, int]:
        """(leg, place in it) of the pass at `index` in `passes`."""
        for number, leg in enumerate(self.legs):
            if index < len(leg.passes):
                return number, index
            index -= len(leg.passes)
        raise IndexError(index)

    @property
    def run_time_s(self) -> float:
        """Feed moves and dwells, facing included. Rapids come on top and depend on the machine."""
        per_pass = self.dwell_s + self.plunge_s
        cuts = sum(per_pass + p.lead_in_s + p.expected_cut_s for p in self.passes)
        return sum(leg.face_s for leg in self.legs) + cuts

    def save(self, path: Path) -> None:
        # NaN and infinity are not JSON (the widget cannot read them back): refuse them here.
        text = json.dumps(asdict(self), indent=2, allow_nan=False)
        path.write_text(text + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Plan:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise SchemaError(path, data.get("schema"), SCHEMA)
        legs = tuple(
            Leg(
                **{
                    **leg,
                    "passes": tuple(
                        Pass(**{**p, "kind": PassKind(p["kind"])}) for p in leg["passes"]
                    ),
                }
            )
            for leg in data.pop("legs")
        )
        return Plan(**{**data, "planner": PlannerKind(data["planner"]), "legs": legs})


@dataclass(frozen=True)
class Envelope:
    """What machine, tool and material allow. Bounds the search; predicts nothing."""

    chip_load_mm: float
    speed_min_rpm: int
    speed_max_rpm: int
    width_max_mm: float
    mrr_limit_mm3_min: float
    # The speed bounds before they are snapped to the planner's speed step, and the widest
    # width on the grid, which rounds to the width step and can lie above width_max_mm. A
    # cut made inside them was inside the search, whatever the steps were then.
    speed_low_rpm: float
    speed_high_rpm: float
    width_top_mm: float

    def holds_speed(self, rpm: float) -> bool:
        return (
            self.speed_low_rpm - SPEED_TOLERANCE_RPM
            <= rpm
            <= self.speed_high_rpm + SPEED_TOLERANCE_RPM
        )

    def contains(self, rpm: float, width_mm: float) -> bool:
        """Whether a cut lies in the region searched now; bounds may change after cutting."""
        return self.holds_speed(rpm) and width_mm <= self.width_top_mm + WIDTH_TOLERANCE_MM

    def speeds(self, step_rpm: int) -> np.ndarray:
        return np.arange(self.speed_min_rpm, self.speed_max_rpm + 1, step_rpm, dtype=float)

    def widths(self, step_mm: float) -> np.ndarray:
        count = max(1, int(round(self.width_max_mm / step_mm)))
        return step_mm * np.arange(1, count + 1, dtype=float)


def envelope(setup: Setup) -> Envelope:
    material = materials.lookup(setup.cut.material)
    step = setup.planner.speed_step_rpm
    diameter = setup.tool.diameter_mm

    surface_limit = (
        materials.surface_speed_m_min(material, setup.tool.material) * 1000 / (math.pi * diameter)
    )
    speed_high = min(setup.machine.max_rpm, surface_limit, setup.cut.speed_max_rpm or math.inf)
    speed_max = int(speed_high // step * step)
    speed_low = max(setup.machine.min_rpm, setup.cut.speed_min_rpm or speed_max / 4)
    speed_min = int(math.ceil(speed_low / step) * step)
    if speed_min >= speed_max:
        raise PlanError(f"empty speed range: {speed_min}..{speed_max} rpm")

    chip_load = setup.cut.chip_load_mm or (
        material.chip_load_per_diameter
        * diameter
        * materials.weight_derate(setup.machine.weight_kg)
    )
    usable_w = setup.machine.spindle_power_w * setup.planner.power_fraction * SPINDLE_EFFICIENCY
    width_max = setup.cut.width_max_mm or diameter / 2
    width_step = setup.planner.width_step_mm
    return Envelope(
        chip_load_mm=round(chip_load, 4),
        speed_min_rpm=speed_min,
        speed_max_rpm=speed_max,
        width_max_mm=width_max,
        mrr_limit_mm3_min=usable_w / material.specific_energy_j_mm3 * 60,
        speed_low_rpm=speed_low,
        speed_high_rpm=speed_high,
        width_top_mm=width_step * max(1, int(round(width_max / width_step))),
    )


def feed_mm_min(setup: Setup, env: Envelope, rpm: float) -> float:
    return env.chip_load_mm * setup.tool.flutes * rpm


def mrr_mm3_min(setup: Setup, env: Envelope, rpm: float, width_mm: float) -> float:
    return setup.cut.axial_depth_mm * width_mm * feed_mm_min(setup, env, rpm)


def _reach(diameter_mm: float, width_mm: float) -> float:
    """How far along the feed a cutter `width_mm` into a face touches it, from its centre."""
    return math.sqrt(max(width_mm * (diameter_mm - width_mm), 0.0))


@dataclass(frozen=True)
class Travel:
    """One pass's cutting move along the feed, in mm from the end of the stock the cutter
    meets first (the left end in conventional milling, the right end in climb)."""

    start: float  # where the feed move starts, in the air
    contact: float  # where the cutter meets the lane's stock
    leave: float  # where it stops cutting
    end: float  # where the feed move ends


def travel(setup: Setup, lane: int, lanes: int, face_mm: float, width_mm: float) -> Travel:
    """The feed move of a pass that leaves the face at `face_mm` from Y0, `width_mm` wide.

    A lane starts where the one before it was cut back, so the cutter starts in the air: in
    front of the stock in the first lane, and a diameter clear of what the lane before left
    at its far end in the others. A pass in the last lane runs out past the stock's end. One
    in an earlier lane stops in the stock, its cutter just short of the next lane, still
    uncut at Y0: nowhere does it reach past that lane's start.
    """
    diameter, clearance = setup.tool.diameter_mm, setup.program.clearance_mm
    radius = diameter / 2
    length = setup.stock.length_mm / lanes
    first, last = (lane - 1) * length, lane * length
    start = first - radius - clearance - (radius if lane > 1 else 0.0)
    contact = first - _reach(diameter, width_mm)
    if lane == lanes:
        leave = setup.stock.length_mm + _reach(diameter, width_mm)
        return Travel(start, contact, leave, setup.stock.length_mm + radius + clearance)
    stop = last - _reach(diameter, min(face_mm, radius))
    return Travel(start, contact, stop, stop)


def auto_lanes(setup: Setup, env: Envelope) -> int:
    """The setup's lanes, or when it leaves them empty, as many as the stock length holds
    with every cut at least `planner.min_cut_s` long at the fastest test speed (where cuts
    are shortest) and every lane at least twice the cutter diameter long."""
    if setup.stock.lanes is not None:
        return setup.stock.lanes
    feed_mm_s = env.chip_load_mm * setup.tool.flutes * env.speed_max_rpm / 60
    shortest = max(2 * setup.tool.diameter_mm, feed_mm_s * setup.planner.min_cut_s)
    return max(1, int(setup.stock.length_mm / shortest + 1e-9))


def lane_x_mm(setup: Setup, lane: int, lanes: int) -> tuple[float, float]:
    """Where a lane lies along X, left end first; climb milling uses the lanes from the right."""
    length = setup.stock.length_mm / lanes
    first, last = (lane - 1) * length, lane * length
    if setup.cut.direction is Direction.CLIMB:
        first, last = setup.stock.length_mm - last, setup.stock.length_mm - first
    return round(first, 3), round(last, 3)


def make_pass(
    setup: Setup,
    env: Envelope,
    pass_id: int,
    kind: PassKind,
    rpm: float,
    width_mm: float,
    p_stable: float | None = None,
    chip_load_mm: float | None = None,
    *,
    path: Travel | None = None,
) -> Pass:
    feed = (chip_load_mm or env.chip_load_mm) * setup.tool.flutes * rpm
    path = path or travel(setup, 1, 1, width_mm, width_mm)
    return Pass(
        id=pass_id,
        kind=kind,
        rpm=int(round(rpm)),
        width_mm=round(width_mm, 3),
        feed_mm_min=round(feed, 1),
        expected_cut_s=round((path.leave - path.contact) / feed * 60, 2),
        lead_in_s=round((path.contact - path.start) / feed * 60, 2),
        p_stable=None if p_stable is None else round(p_stable, 3),
        chip_load_mm=chip_load_mm,
    )


def preparation_passes(setup: Setup, env: Envelope, remnant_mm: float) -> list[tuple[float, float]]:
    """(rpm, width) passes that leave a flat face: clear an aborted pass's step, then true up."""
    rpm = (env.speed_min_rpm + env.speed_max_rpm) / 2
    step = max(setup.program.truing_width_mm, 0.1 * setup.tool.diameter_mm)
    count = math.ceil(remnant_mm / step - 1e-9)
    cleanup = [(rpm, remnant_mm / max(count, 1))] * count
    return [*cleanup, (rpm, setup.program.truing_width_mm)]


REFACE_EXTRA_MM = 0.2  # faced off below the deepest cut, so the new top is whole


@dataclass(frozen=True)
class FacingRow:
    z_mm: float  # the level, from Z0
    y_mm: float  # the cutter's centre
    start: float  # along the feed, as Travel
    end: float


def facing_rows(setup: Setup, top_mm: float, reface_mm: float) -> list[FacingRow]:
    """The passes that take the top from `top_mm - reface_mm` down to `top_mm` below Z0:
    levels no deeper than program.face_depth_mm, each in rows along the feed that step back
    from the front face by program.face_width_mm, over the whole length and from Y0 to the
    stock's thickness. Each row starts and ends a cutter radius and the clearance beyond the
    stock's ends, in the air, and cuts on its +Y side only, as the test passes do."""
    if reface_mm <= 0:
        return []
    radius, clearance = setup.tool.diameter_mm / 2, setup.program.clearance_mm
    step = setup.program.face_width_mm or setup.tool.diameter_mm / 4
    depth = setup.stock.thickness_mm or setup.stock.width_mm
    rows = max(1, math.ceil(depth / step - 1e-9))
    levels = max(1, math.ceil(reface_mm / setup.program.face_depth_mm - 1e-9))
    start, end = -radius - clearance, setup.stock.length_mm + radius + clearance
    return [
        FacingRow(
            round(top_mm - reface_mm * (levels - 1 - level) / levels, 3),
            round(depth * (row + 1) / rows - radius, 3),
            start,
            end,
        )
        for level in range(levels)
        for row in range(rows)
    ]


def facing_s(setup: Setup, rows: list[FacingRow], feed_mm_min: float) -> float:
    """The feed moves of facing: each row's plunge in the air and its cut."""
    plunge = setup.program.clearance_mm / setup.program.plunge_feed_mm_min * 60
    return sum(plunge + (r.end - r.start) / feed_mm_min * 60 for r in rows)


def lane_budget_mm(setup: Setup, lane_cap_mm: float | None) -> float:
    """How far back from Y0 the lane's face may go: the stock width to use, and in a lane
    after the first, no further than the lane before it was cut back (the cutter crosses it)."""
    return min(setup.stock.width_mm, math.inf if lane_cap_mm is None else lane_cap_mm)


@dataclass(frozen=True)
class Face:
    """Where cutting can go on: a lane's front face `y_mm` back from Y0, the step an aborted
    pass left on it, how far back the lane may go (None: the stock's width), and how far below
    Z0 the top stands."""

    lane: int = 1
    y_mm: float = 0.0
    remnant_mm: float = 0.0
    cap_mm: float | None = None
    top_mm: float = 0.0


FRESH = Face()  # fresh stock: the first lane's face at Y0, the top at Z0


def cut_order(setup: Setup, tests: list[tuple]) -> list[tuple]:
    """The tests in the order a round cuts them: narrowest first, a gradual ramp; with a manual
    spindle each speed's together, lightest first, since every speed change is a stop at the
    machine. Equal cuts keep their order: a chip-load sweep's, lightest first."""
    if setup.machine.spindle is SpindleControl.MANUAL:
        return sorted(tests, key=lambda t: (t[0], t[1]))
    return sorted(tests, key=lambda t: (t[1], t[0]))


def _fill(
    setup: Setup, env: Envelope, face: Face, tests: list[tuple]
) -> tuple[list[tuple[float, float]], list[tuple]]:
    """(face preparation, the tests that fit after it): `tests` in order, up to the first that
    does not fit."""
    prep = preparation_passes(setup, env, face.remnant_mm)
    budget = lane_budget_mm(setup, face.cap_mm) - face.y_mm - sum(w for _, w in prep)
    kept = []
    for test in tests:
        if test[1] > budget + 1e-9:
            break
        budget -= test[1]
        kept.append(test)
    return prep, kept


def _leg(
    setup: Setup,
    env: Envelope,
    lanes: int,
    face: Face,
    reface_mm: float,
    prep: list[tuple[float, float]],
    tests: list[tuple],
    first_id: int,
) -> Leg:
    """The leg that prepares `face`, after taking the top down `reface_mm` to it, and cuts
    `tests` on it; its passes are numbered from `first_id`."""
    if setup.machine.spindle is SpindleControl.MANUAL:
        # Every speed change is a stop at the machine: prepare the face at the leg's first speed.
        prep = [(tests[0][0], width) for _, width in prep]
    planned = [
        *[(PassKind.CLEANUP, rpm, width) for rpm, width in prep[:-1]],
        (PassKind.TRUING, *prep[-1]),
        *[(PassKind.TEST, *test) for test in tests],
    ]
    passes = []
    y = face.y_mm
    for kind, rpm, width, *rest in planned:
        y += width
        path = travel(setup, face.lane, lanes, y, width)
        pass_id = first_id + len(passes)
        passes.append(make_pass(setup, env, pass_id, kind, rpm, width, *rest, path=path))
    # The top comes down at the leg's first speed: no extra spindle stop.
    feed = env.chip_load_mm * setup.tool.flutes * passes[0].rpm
    return Leg(
        lane=face.lane,
        face_y_start_mm=round(face.y_mm, 3),
        passes=tuple(passes),
        lane_cap_mm=None if face.cap_mm is None else round(face.cap_mm, 3),
        top_mm=round(face.top_mm, 3),
        reface_mm=round(reface_mm, 3),
        face_s=round(facing_s(setup, facing_rows(setup, face.top_mm, reface_mm), feed), 2),
    )


def _lay_out(
    setup: Setup,
    env: Envelope,
    tests: list[tuple],
    face: Face,
    cut_mm: tuple[float, ...],
    lanes: int,
) -> tuple[list[Leg], list[tuple]]:
    """The legs the tests fill in cut order from `face` on, and the tests left over.

    A leg takes the tests that fit on its face, up to the first that does not. The next leg
    is the next lane: a fresh face at Y0, cut back no further than this one was. Once the
    last lane is used, or the face is flat only above the round's depth (`cut_mm`: the depths
    cut into the top as it stands), the top comes down below the deepest of those cuts, given
    stock.height_mm and while the next cut's bottom stays the clearance above it, and the
    round goes on at the first lane. A round faces the top at most once: facing is slow, so
    the tests the lanes of the new top cannot hold are left over (assemble drops them).
    """
    depth = setup.cut.axial_depth_mm
    left = cut_order(setup, tests)
    legs: list[Leg] = []
    here, cut, reface = face, list(cut_mm), 0.0
    while left:
        flat = not cut or depth <= min(cut) + DEPTH_TOLERANCE_MM
        prep, kept = _fill(setup, env, here, left) if flat else ([], [])
        if kept:
            first_id = sum(len(leg.passes) for leg in legs)
            legs.append(_leg(setup, env, lanes, here, reface, prep, kept, first_id))
            left = left[len(kept) :]
            cut.append(depth)
            reface = 0.0
            here = Face(here.lane, legs[-1].face_y_end_mm, 0.0, here.cap_mm, here.top_mm)
        if not left:
            break
        if flat and here.y_mm > 0 and here.lane < lanes:
            here = Face(here.lane + 1, 0.0, 0.0, here.y_mm, here.top_mm)
            continue
        # Every lane of this top is used, or none would hold the next test (a lane after this
        # one may go no further back): only a fresh top can, once a round.
        height = setup.stock.height_mm
        if height is None or not cut or here.top_mm > face.top_mm:
            break
        down = max(cut) + REFACE_EXTRA_MM
        lower = Face(top_mm=here.top_mm + down)
        if lower.top_mm + depth + setup.program.clearance_mm > height:
            break
        if not _fill(setup, env, lower, left)[1]:
            break
        here, cut, reface = lower, [], down
    return legs, left


def _no_room(
    setup: Setup,
    round_no: int,
    tests: list[tuple],
    face: Face,
    cut_mm: tuple[float, ...],
    lanes: int,
    legs: list[Leg],
) -> str:
    if setup.stock.height_mm is None:
        hint = (
            "Mount fresh stock and plan with new stock, or give the stock's height above "
            "the vise jaws ([stock] height_mm) so dechatter can face the top down itself"
        )
    else:
        hint = "The top cannot come down further: mount fresh stock and plan with new stock"
    if legs:
        return (
            f"the chip-load sweep needs room for all its {len(tests)} cuts: no face left has "
            f"room for them. {hint}."
        )
    depth = setup.cut.axial_depth_mm
    if cut_mm and depth > min(cut_mm) + DEPTH_TOLERANCE_MM:
        return (
            f"round {round_no} would cut {depth:g} mm deep, but this block's face is only flat "
            f"down to {min(cut_mm):g} mm. {hint}."
        )
    room = lane_budget_mm(setup, face.cap_mm)
    where = f" in lane {face.lane} of {lanes}" if lanes > 1 else ""
    return (
        f"stock exhausted: {max(room - face.y_mm, 0.0):.1f} mm left of {room:.1f} mm{where}, "
        f"and no face after it has room. {hint}."
    )


def assemble(
    setup: Setup,
    env: Envelope,
    round_no: int,
    planner: PlannerKind,
    tests: list[tuple],
    *,
    face: Face = FRESH,
    cut_mm: tuple[float, ...] = (),
    lanes: int | None = None,
) -> Plan:
    """Lay the tests out on the stock from `face` on, in legs that each prepare their face
    first (see _lay_out), on the setup's lanes unless `lanes` says otherwise.

    A test is (rpm, width, P(stable)), or in a chip-load sweep (rpm, width, None, chip load),
    already lightest first. `cut_mm` are the depths cut into the top as it stands (none: it
    is untouched). Tests left over once the stock runs out are dropped; but a chip-load sweep
    must fit whole: cut short, its heaviest chip loads would go untried and the lightest read
    as a limit.
    """
    lanes = lanes or auto_lanes(setup, env)
    legs, left = _lay_out(setup, env, tests, face, cut_mm, lanes)
    if not legs or (left and planner is PlannerKind.SWEEP):
        raise PlanError(_no_room(setup, round_no, tests, face, cut_mm, lanes, legs))
    return Plan(
        round=round_no,
        planner=planner,
        flutes=setup.tool.flutes,
        diameter_mm=setup.tool.diameter_mm,
        axial_depth_mm=setup.cut.axial_depth_mm,
        chip_load_mm=env.chip_load_mm,
        dwell_s=setup.program.dwell_s,
        plunge_s=round(
            (setup.program.clearance_mm + setup.cut.axial_depth_mm)
            / setup.program.plunge_feed_mm_min
            * 60,
            2,
        ),
        legs=tuple(legs),
        planned_on=date.today().isoformat(),
        lanes=lanes,
    )


SWEEP_PASSES = 5


def sweep_tests(setup: Setup, env: Envelope, rpm: float, width_mm: float) -> list[tuple]:
    """(rpm, width, None, chip load) for a chip-load sweep of one cut, lightest first.

    Chip loads step evenly between the planner's bounds, as multiples of the session's chip
    load. The power cap trims the heaviest.
    """
    cfg = setup.planner
    tests = []
    for factor in np.linspace(cfg.chip_load_sweep_low, cfg.chip_load_sweep_high, SWEEP_PASSES):
        chip_load = round(float(factor) * env.chip_load_mm, 4)
        mrr = setup.cut.axial_depth_mm * width_mm * chip_load * setup.tool.flutes * rpm
        if mrr <= env.mrr_limit_mm3_min:
            tests.append((rpm, width_mm, None, chip_load))
    return tests


def grid_tests(setup: Setup, env: Envelope) -> list[tuple[float, float, None]]:
    """Round 1: coarse speed x width grid inside the power cap."""
    cfg = setup.planner
    speeds = np.linspace(env.speed_min_rpm, env.speed_max_rpm, cfg.round1_speeds)
    speeds = np.unique(np.round(speeds / cfg.speed_step_rpm) * cfg.speed_step_rpm)
    widths = sorted(
        {
            max(
                cfg.width_step_mm,
                round(f * env.width_max_mm / cfg.width_step_mm) * cfg.width_step_mm,
            )
            for f in cfg.round1_width_fractions
        }
    )
    tests = [
        (float(rpm), float(width), None)
        for width in widths
        for rpm in speeds
        if mrr_mm3_min(setup, env, rpm, width) <= env.mrr_limit_mm3_min
    ]
    if not tests:
        raise PlanError("power cap excludes every round-1 cut; reduce axial_depth_mm or chip load")
    return tests
