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
from .config import Direction, Setup, SpindleControl, Vary, full_width, slot_wall_mm

# 2: dechatter 0.2.0 (planned_on, per-pass chip loads); 3: 0.6.0 (a round in legs, across
# lanes and faced tops; what the session varies, and each pass's own depth). A schema-2
# plan still loads, as one leg (see _one_leg), stepping the width (see _stepping_width).
SCHEMA = 3
SPINDLE_EFFICIENCY = 0.8
SPEED_TOLERANCE_RPM = 0.5
ENGAGEMENT_TOLERANCE_MM = 1e-6
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
    SWEEP = "sweep"  # one cut at rising chip loads; not part of the stability map


class Milling(StrEnum):
    """How a round's passes meet the stock."""

    SIDE = "side"  # along a lane's front face, each pass stepping it back from Y0
    SLOT = "slot"  # the cutter's full width into the top, each slot in its own row along Y


class Order(StrEnum):
    """The order a round cuts its tests in."""

    RAMP = "ramp"  # lightest first: each cut a small step up from one that ran before it
    DEEPEST_FIRST = "deepest-first"  # as the stock demands where depths differ


def order(vary: Vary, kind: Milling, lanes: int) -> Order:
    """A round that steps the depth cuts deepest first: stock is only clear down to the
    shallowest cut made in front of it, on a face and from one lane to the next, so a
    shallower cut can follow a deeper one there and never the reverse. The round's heaviest
    cut then comes first, on no more than the planner's chance of being stable, instead of
    after lighter ones. Only slots in a single lane, each in stock of its own, keep the ramp."""
    if vary is Vary.WIDTH:
        return Order.RAMP
    if kind is Milling.SLOT and lanes == 1:
        return Order.RAMP
    return Order.DEEPEST_FIRST


@dataclass(frozen=True)
class Pass:
    id: int
    kind: PassKind
    rpm: int
    width_mm: float
    depth_mm: float
    feed_mm_min: float
    expected_cut_s: float
    lead_in_s: float
    plunge_s: float  # feed move from the clearance plane down to depth, beside the stock
    p_stable: float | None = None
    chip_load_mm: float | None = None  # a chip-load sweep's own; else the plan's


@dataclass(frozen=True)
class Leg:
    """Part of a round cut on one lane. With `reface_mm`, the program first takes the top
    down that much, to `top_mm` below Z0, its feed moves taking `face_s`. Then the passes use
    the lane's Y from `face_y_start_mm` on, no further than `lane_cap_mm`: what the lane
    before it used (None: all the stock has). Side milling cuts the front face back by each
    pass's width; a slot takes its width with `wall_mm` of stock left standing before it."""

    lane: int
    face_y_start_mm: float
    passes: tuple[Pass, ...]
    lane_cap_mm: float | None = None
    top_mm: float = 0.0
    reface_mm: float = 0.0
    face_s: float = 0.0
    wall_mm: float = 0.0

    @property
    def face_y_end_mm(self) -> float:
        return self.face_y_start_mm + sum(p.width_mm + self.wall_mm for p in self.passes)


@dataclass(frozen=True)
class Plan:
    round: int
    planner: PlannerKind
    flutes: int
    diameter_mm: float
    # What the session steps, and the dimension every test cut of the round shares: the
    # axial depth when it steps the width, the radial width when it steps the depth.
    vary: Vary
    fixed_mm: float
    milling: Milling
    chip_load_mm: float
    dwell_s: float
    # The passes in the order they run, in legs of one lane each: the round fills what is
    # left of its lane, goes on into the lanes after it, and once every lane is used may face
    # the top down and start again at the first lane (1 is the one the cutter meets first).
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

    def first_index(self, leg: int) -> int:
        """The index in `passes` of the leg's first pass."""
        return sum(len(earlier.passes) for earlier in self.legs[:leg])

    @property
    def mid_facing(self) -> int | None:
        """The leg the top is faced down before partway through the round (at most one; see
        _lay_out), if any; facing before the first leg comes before the recording starts."""
        return next((n for n, leg in enumerate(self.legs) if n > 0 and leg.reface_mm > 0), None)

    def facing_before(self, index: int) -> int | None:
        """The leg whose facing of the top comes right before the pass at `index`, if any."""
        if index >= len(self.passes):
            return None
        number, place = self.leg_of(index)
        return number if place == 0 and self.legs[number].reface_mm > 0 else None

    def engagement_mm(self, p: Pass) -> float:
        """A pass's size in the dimension the session steps."""
        return p.depth_mm if self.vary is Vary.DEPTH else p.width_mm

    @property
    def order(self) -> Order:
        return order(self.vary, self.milling, self.lanes)

    @property
    def run_time_s(self) -> float:
        """Feed moves and dwells, facing included. Rapids come on top and depend on the machine."""
        cuts = sum(self.dwell_s + p.plunge_s + p.lead_in_s + p.expected_cut_s for p in self.passes)
        return sum(leg.face_s for leg in self.legs) + cuts

    def save(self, path: Path) -> None:
        # NaN and infinity are not JSON (the widget cannot read them back): refuse them here.
        text = json.dumps(asdict(self), indent=2, allow_nan=False)
        path.write_text(text + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Plan:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") == 2:
            data = _one_leg(data)
        if data.get("schema") != SCHEMA:
            raise SchemaError(path, data.get("schema"), SCHEMA)
        if "vary" not in data:
            data = _stepping_width(data)
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
        return Plan(
            **{
                **data,
                "planner": PlannerKind(data["planner"]),
                "vary": Vary(data["vary"]),
                "milling": Milling(data["milling"]),
                "legs": legs,
            }
        )


def _stepping_width(data: dict) -> dict:
    """A plan written before a session could step the depth, as one that steps the width:
    side milling at the plan's one axial depth, every pass plunging to it."""
    depth, plunge_s = data.pop("axial_depth_mm"), data.pop("plunge_s")
    legs = [
        {**leg, "passes": [{**p, "depth_mm": depth, "plunge_s": plunge_s} for p in leg["passes"]]}
        for leg in data["legs"]
    ]
    return {**data, "vary": Vary.WIDTH, "fixed_mm": depth, "milling": Milling.SIDE, "legs": legs}


# What a schema-2 plan held of its round's one lane, the leg it is now.
SCHEMA_2_LEG = ("face_y_start_mm", "lane_cap_mm", "top_mm", "reface_mm", "face_s")


def _one_leg(data: dict) -> dict:
    """A plan of dechatter 0.2.0 to 0.5.0, a round on one lane, as a single leg. Fields
    0.2.0 to 0.4.0 lacked take their defaults, as they did then."""
    leg = {key: data.pop(key) for key in SCHEMA_2_LEG if key in data}
    leg = {"lane": data.pop("lane", 1), **leg, "passes": data.pop("passes")}
    return {**data, "legs": [leg], "schema": SCHEMA}


@dataclass(frozen=True)
class Envelope:
    """What machine, tool and material allow. Bounds the search; predicts nothing.

    The engagement is the dimension the session steps: the radial width, or with
    cut.vary = "depth" the axial depth.
    """

    chip_load_mm: float
    speed_min_rpm: int
    speed_max_rpm: int
    engagement_max_mm: float
    mrr_limit_mm3_min: float
    # The speed bounds before they are snapped to the planner's speed step, and the largest
    # engagement on the grid, which rounds to its step and can lie above engagement_max_mm.
    # A cut made inside them was inside the search, whatever the steps were then.
    speed_low_rpm: float
    speed_high_rpm: float
    engagement_top_mm: float

    def holds_speed(self, rpm: float) -> bool:
        return (
            self.speed_low_rpm - SPEED_TOLERANCE_RPM
            <= rpm
            <= self.speed_high_rpm + SPEED_TOLERANCE_RPM
        )

    def contains(self, rpm: float, engagement_mm: float) -> bool:
        """Whether a cut lies in the region searched now; bounds may change after cutting."""
        return (
            self.holds_speed(rpm)
            and engagement_mm <= self.engagement_top_mm + ENGAGEMENT_TOLERANCE_MM
        )

    def speeds(self, step_rpm: int) -> np.ndarray:
        return np.arange(self.speed_min_rpm, self.speed_max_rpm + 1, step_rpm, dtype=float)

    def engagements(self, step_mm: float) -> np.ndarray:
        count = max(1, int(round(self.engagement_max_mm / step_mm)))
        return step_mm * np.arange(1, count + 1, dtype=float)


def fixed_mm(setup: Setup) -> float:
    """The dimension every test cut shares: the depth, or with vary = "depth" the width."""
    if setup.cut.vary is Vary.DEPTH:
        return setup.cut.radial_width_mm
    return setup.cut.axial_depth_mm


def cut_size(setup: Setup, engagement_mm: float) -> tuple[float, float]:
    """(width, depth) of a test cut that steps the varied dimension to `engagement_mm`."""
    if setup.cut.vary is Vary.DEPTH:
        return setup.cut.radial_width_mm, engagement_mm
    return engagement_mm, setup.cut.axial_depth_mm


# The planner only steps up where the next level is likely stable given the one below, and
# its prior falls to 5% at the largest cut: with too few levels the top ones are each too big
# a step to ever take.
DEPTH_LEVELS = 50


def engagement_step_mm(setup: Setup) -> float:
    if setup.cut.vary is Vary.WIDTH:
        return setup.planner.width_step_mm
    if setup.planner.depth_step_mm is not None:
        return setup.planner.depth_step_mm
    # The largest of 1, 2, 5 x 10^n that gives at least DEPTH_LEVELS depths.
    finest = setup.cut.depth_max_mm / DEPTH_LEVELS
    decade = 10.0 ** math.floor(math.log10(finest))
    return decade * max(m for m in (1, 2, 5) if m * decade <= finest * (1 + 1e-9))


def milling(setup: Setup) -> Milling:
    return Milling.SLOT if full_width(setup) else Milling.SIDE


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
    if setup.cut.vary is Vary.DEPTH:
        engagement_max = setup.cut.depth_max_mm
    else:
        engagement_max = setup.cut.width_max_mm or diameter / 2
    engagement_step = engagement_step_mm(setup)
    return Envelope(
        chip_load_mm=round(chip_load, 4),
        speed_min_rpm=speed_min,
        speed_max_rpm=speed_max,
        engagement_max_mm=engagement_max,
        mrr_limit_mm3_min=usable_w / material.specific_energy_j_mm3 * 60,
        speed_low_rpm=speed_low,
        speed_high_rpm=speed_high,
        engagement_top_mm=engagement_step * max(1, int(round(engagement_max / engagement_step))),
    )


def feed_mm_min(setup: Setup, env: Envelope, rpm: float) -> float:
    return env.chip_load_mm * setup.tool.flutes * rpm


def mrr_mm3_min(setup: Setup, env: Envelope, rpm: float, engagement_mm: float) -> float:
    """Width x depth x feed: the dimension the setup holds times the one it steps."""
    return fixed_mm(setup) * engagement_mm * feed_mm_min(setup, env, rpm)


def _reach(diameter_mm: float, width_mm: float) -> float:
    """How far along the feed a cutter `width_mm` into a face touches it, from its centre:
    its radius once the cut is at least that wide."""
    if width_mm >= diameter_mm / 2:
        return diameter_mm / 2
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
    """The feed move of a pass `width_mm` wide whose far side along Y is `face_mm` from Y0:
    the face a side-milling pass leaves, or a slot's far wall.

    A lane starts where the one before it was cut, so the cutter starts in the air: in front
    of the stock in the first lane, and a diameter clear of what the lane before left at its
    far end in the others. A pass in the last lane runs out past the stock's end. One in an
    earlier lane stops in the stock, its cutter just short of the next lane, still uncut at
    Y0: nowhere does it reach past that lane's start.
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
    depth_mm: float,
    path: Travel,
    p_stable: float | None = None,
    chip_load_mm: float | None = None,
) -> Pass:
    feed = (chip_load_mm or env.chip_load_mm) * setup.tool.flutes * rpm
    plunge_mm = setup.program.clearance_mm + depth_mm
    return Pass(
        id=pass_id,
        kind=kind,
        rpm=int(round(rpm)),
        width_mm=round(width_mm, 3),
        depth_mm=round(depth_mm, 3),
        feed_mm_min=round(feed, 1),
        expected_cut_s=round((path.leave - path.contact) / feed * 60, 2),
        lead_in_s=round((path.contact - path.start) / feed * 60, 2),
        plunge_s=round(plunge_mm / setup.program.plunge_feed_mm_min * 60, 2),
        p_stable=None if p_stable is None else round(p_stable, 3),
        chip_load_mm=chip_load_mm,
    )


def preparation_passes(setup: Setup, env: Envelope, remnant_mm: float) -> list[tuple[float, float]]:
    """(rpm, width) passes that leave a flat face: clear an aborted pass's step, then true up.
    Slots have no face to prepare."""
    if milling(setup) is Milling.SLOT:
        return []
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


def wall_mm(setup: Setup) -> float:
    """The stock left standing before each pass along Y: a slot's wall; none in side milling."""
    return slot_wall_mm(setup) if milling(setup) is Milling.SLOT else 0.0


def lane_budget_mm(setup: Setup, lane_cap_mm: float | None) -> float:
    """How far from Y0 a lane's passes may go. Side milling: the stock width to use. Slots:
    the stock's thickness, less the wall behind the last one. In a lane after the first, no
    further than the lane before it was cut (the cutter crosses it)."""
    room = setup.stock.width_mm
    if milling(setup) is Milling.SLOT:
        room = (setup.stock.thickness_mm or setup.stock.width_mm) - slot_wall_mm(setup)
    return min(room, math.inf if lane_cap_mm is None else lane_cap_mm)


class Top(StrEnum):
    """What the stock's top is like at `top_mm`, the lanes' cuts aside."""

    FLAT = "flat"
    # A facing stopped part-way: some of it is faced lower. Only facing it down again from
    # `top_mm` makes it flat; a cut planned on it could meet less stock than planned.
    PART_FACED = "part faced"


@dataclass(frozen=True)
class Face:
    """Where cutting can go on: in a lane, `y_mm` back from Y0 (side milling: its front
    face; slots: the rows used), the step an aborted pass left on the face, how far back the
    lane may go (None: all the stock has), how far below Z0 the top stands, and whether it
    is flat there."""

    lane: int = 1
    y_mm: float = 0.0
    remnant_mm: float = 0.0
    cap_mm: float | None = None
    top_mm: float = 0.0
    top: Top = Top.FLAT


FRESH = Face()  # fresh stock: the first lane's face at Y0, the top at Z0


@dataclass(frozen=True)
class Made:
    """A pass cut into the top as it stands: its lane, how far from Y0 it reached (the face
    it left, or a slot's far wall), how deep it went, and how it met the stock."""

    lane: int
    y_mm: float
    depth_mm: float
    milling: Milling = Milling.SIDE


def cut_order(
    setup: Setup, tests: list[tuple], lanes: int = 1, floor_mm: float = math.inf
) -> list[tuple]:
    """The tests in the order a round cuts them (see `order`).

    A ramp runs lightest first; with a manual spindle each speed's together, lightest first,
    since every speed change is a stop at the machine. Deepest first, the cuts the face takes
    as it stands (no deeper than `floor_mm`) come before those that need a fresh one, and a
    manual spindle is stopped wherever the speed changes. Equal cuts keep their order: a
    chip-load sweep's, lightest first.
    """
    if order(setup.cut.vary, milling(setup), lanes) is Order.DEEPEST_FIRST:
        return sorted(tests, key=lambda t: (t[1] > floor_mm + DEPTH_TOLERANCE_MM, -t[1], t[0]))
    if setup.machine.spindle is SpindleControl.MANUAL:
        return sorted(tests, key=lambda t: (t[0], t[1]))
    return sorted(tests, key=lambda t: (t[1], t[0]))


def _face_floor(setup: Setup, lane: int, made: list[Made]) -> float:
    """How deep a cut on a lane's front face may go: a face is flat only above the shallowest
    cut made on it. A slot is cut into stock of its own, at any depth."""
    if milling(setup) is Milling.SLOT:
        return math.inf
    return min((m.depth_mm for m in made if m.lane == lane), default=math.inf)


def _lane_floor(lane: int, y_mm: float, made: list[Made]) -> float:
    """How deep a cut reaching `y_mm` from Y0 may go in a lane after the first: the cutter
    comes to it through the lane before, which is clear only as deep as it was cut there.
    With no pass of that lane on record, only the face's cap bounds the cut."""
    before = [m for m in made if m.lane == lane - 1]
    if not before:
        return math.inf
    reached = [m for m in before if m.y_mm >= y_mm - 1e-6]
    if not reached:
        return -math.inf
    return min(reached, key=lambda m: m.y_mm).depth_mm


def _fill(
    setup: Setup, env: Envelope, face: Face, tests: list[tuple], made: list[Made]
) -> tuple[list[tuple[float, float]], list[tuple]]:
    """(face preparation, the tests that fit after it): `tests` in order, up to the first
    that does not fit, or that cuts deeper than the stock in front of it is clear (`made`:
    the passes cut into the top as it stands)."""
    prep = preparation_passes(setup, env, face.remnant_mm)
    y = face.y_mm + sum(w for _, w in prep)
    budget = lane_budget_mm(setup, face.cap_mm)
    wall = wall_mm(setup)
    floor = _face_floor(setup, face.lane, made)
    kept = []
    for test in tests:
        width, depth = cut_size(setup, test[1])
        y += width + wall
        clear = min(floor, _lane_floor(face.lane, y, made))
        if depth > clear + DEPTH_TOLERANCE_MM or y > budget + 1e-9:
            break
        if milling(setup) is Milling.SIDE:
            floor = min(floor, depth)
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
    # The face is prepared as deep as the leg's deepest test: flat for every one of them.
    deepest = max(cut_size(setup, test[1])[1] for test in tests)
    kinds = [*[PassKind.CLEANUP] * (len(prep) - 1), *[PassKind.TRUING] * min(len(prep), 1)]
    planned = [
        *[(kind, rpm, width, deepest) for kind, (rpm, width) in zip(kinds, prep, strict=True)],
        *[(PassKind.TEST, rpm, *cut_size(setup, size), *rest) for rpm, size, *rest in tests],
    ]
    wall = wall_mm(setup)
    passes = []
    y = face.y_mm
    for kind, rpm, width, depth, *rest in planned:
        y += width + wall
        path = travel(setup, face.lane, lanes, y, width)
        pass_id = first_id + len(passes)
        passes.append(make_pass(setup, env, pass_id, kind, rpm, width, depth, path, *rest))
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
        wall_mm=round(wall, 3),
    )


def _lay_out(
    setup: Setup,
    env: Envelope,
    tests: list[tuple],
    face: Face,
    made: tuple[Made, ...],
    lanes: int,
) -> tuple[list[Leg], list[tuple]]:
    """The legs the tests fill in cut order from `face` on, and the tests left over.

    A leg takes the tests that fit on its lane, up to the first that does not, or that cuts
    deeper than the stock in front of it is clear (see _face_floor and _lane_floor; `made`:
    the passes cut into the top as it stands). The next leg is the next lane: fresh at Y0,
    cut back no further than this one was. Once no lane of this top takes the next test, or
    the top was cut the other way (slots under a face to mill, or the reverse), or the top
    is faced only in part (a facing stopped part-way), the top comes down below the deepest
    of those cuts, given stock.height_mm and while the deepest cut left would stay the
    clearance above it, and the round goes on at the first lane. A round faces the top at
    most once: facing is slow, so the tests the lanes of the new top cannot hold are left
    over (assemble drops them).
    """
    kind = milling(setup)
    cut = list(made)
    # Nothing is cut on a top that holds the other kind of cut, or is faced only in part.
    unfit = any(m.milling is not kind for m in cut) or face.top is not Top.FLAT
    left = cut_order(setup, tests, lanes, _face_floor(setup, face.lane, cut))
    legs: list[Leg] = []
    here, reface = face, 0.0
    while left:
        prep, kept = ([], []) if unfit else _fill(setup, env, here, left, cut)
        if kept:
            first_id = sum(len(leg.passes) for leg in legs)
            legs.append(_leg(setup, env, lanes, here, reface, prep, kept, first_id))
            left = left[len(kept) :]
            y = here.y_mm
            for p in legs[-1].passes:
                y += p.width_mm + legs[-1].wall_mm
                cut.append(Made(here.lane, y, p.depth_mm, kind))
            reface = 0.0
            here = Face(here.lane, legs[-1].face_y_end_mm, 0.0, here.cap_mm, here.top_mm)
        if not left:
            break
        beyond = Face(here.lane + 1, 0.0, 0.0, here.y_mm, here.top_mm)
        further = here.y_mm > 0 and here.lane < lanes and not unfit
        if further and _fill(setup, env, beyond, left, cut)[1]:
            here = beyond
            continue
        # Every lane of this top is used, or none would hold the next test (a lane after this
        # one may go no further back, nor deeper): only a fresh top can, once a round.
        height = setup.stock.height_mm
        if height is None or not cut or here.top_mm > face.top_mm:
            break
        down = max(m.depth_mm for m in cut) + REFACE_EXTRA_MM
        lower = Face(top_mm=here.top_mm + down)
        deepest = max(cut_size(setup, test[1])[1] for test in left)
        if lower.top_mm + deepest + setup.program.clearance_mm > height:
            break
        if not _fill(setup, env, lower, left, [])[1]:
            break
        here, cut, unfit, reface = lower, [], False, down
    return legs, left


def _no_room(
    setup: Setup,
    round_no: int,
    tests: list[tuple],
    face: Face,
    made: tuple[Made, ...],
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
    if face.top is Top.PART_FACED and not legs:
        return (
            f"the last round stopped part-way through facing the top down, so round {round_no} "
            f"must face it again first, and it has no room to. {hint}."
        )
    if legs:
        return (
            f"the chip-load sweep needs room for all its {len(tests)} cuts: no face left has "
            f"room for them. {hint}."
        )
    slots = milling(setup) is Milling.SLOT
    if any(m.milling is not milling(setup) for m in made):
        return (
            f"round {round_no} cuts {'slots into the top' if slots else 'the front face'}, "
            f"but this block's top holds {'side cuts' if slots else 'slots'} from earlier "
            f"rounds. {hint}."
        )
    floor = _face_floor(setup, face.lane, list(made))
    first = cut_order(setup, tests, lanes, floor)[0]
    depth = cut_size(setup, first[1])[1]
    if depth > floor + DEPTH_TOLERANCE_MM:
        return (
            f"round {round_no} would cut {depth:g} mm deep, but this block's face is only flat "
            f"down to {floor:g} mm. {hint}."
        )
    room = lane_budget_mm(setup, face.cap_mm)
    where = f" in lane {face.lane} of {lanes}" if lanes > 1 else ""
    if slots:
        return (
            f"stock exhausted: no room left for a slot{where} ({max(room - face.y_mm, 0.0):.1f} "
            f"mm of {room:.1f} mm), and none after it has room. {hint}."
        )
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
    made: tuple[Made, ...] = (),
    lanes: int | None = None,
) -> Plan:
    """Lay the tests out on the stock from `face` on, in legs that each prepare their face
    first (see _lay_out), on the setup's lanes unless `lanes` says otherwise.

    A test is (rpm, engagement, P(stable)), or in a chip-load sweep (rpm, engagement, None,
    chip load), already lightest first; the engagement is the width, or the depth when the
    setup steps that. `made` are the passes cut into the top as it stands (none: it is
    untouched). Tests left over once the stock runs out are dropped, the heaviest ones: a
    ramp leaves them for last, and a round that runs deepest first is laid out again without
    them, since the lighter cuts are what the heavier ones were chosen on. But a chip-load
    sweep must fit whole: cut short, its heaviest chip loads would go untried and the
    lightest read as a limit.
    """
    lanes = lanes or auto_lanes(setup, env)
    legs, left = _lay_out(setup, env, tests, face, made, lanes)
    if not legs or (left and planner is PlannerKind.SWEEP):
        raise PlanError(_no_room(setup, round_no, tests, face, made, lanes, legs))
    deepest_first = order(setup.cut.vary, milling(setup), lanes) is Order.DEEPEST_FIRST
    while left and deepest_first:
        lighter = sorted(tests, key=lambda t: (t[1], t[0]))[: len(tests) - len(left)]
        again, rest = _lay_out(setup, env, lighter, face, made, lanes)
        if not again:
            break
        tests, legs, left = lighter, again, rest
    return Plan(
        round=round_no,
        planner=planner,
        flutes=setup.tool.flutes,
        diameter_mm=setup.tool.diameter_mm,
        vary=setup.cut.vary,
        fixed_mm=fixed_mm(setup),
        milling=milling(setup),
        chip_load_mm=env.chip_load_mm,
        dwell_s=setup.program.dwell_s,
        legs=tuple(legs),
        planned_on=date.today().isoformat(),
        lanes=lanes,
    )


SWEEP_PASSES = 5


def sweep_tests(setup: Setup, env: Envelope, rpm: float, engagement_mm: float) -> list[tuple]:
    """(rpm, engagement, None, chip load) for a chip-load sweep of one cut, lightest first.

    Chip loads step evenly between the planner's bounds, as multiples of the session's chip
    load. The power cap trims the heaviest.
    """
    cfg = setup.planner
    tests = []
    for factor in np.linspace(cfg.chip_load_sweep_low, cfg.chip_load_sweep_high, SWEEP_PASSES):
        chip_load = round(float(factor) * env.chip_load_mm, 4)
        mrr = fixed_mm(setup) * engagement_mm * chip_load * setup.tool.flutes * rpm
        if mrr <= env.mrr_limit_mm3_min:
            tests.append((rpm, engagement_mm, None, chip_load))
    return tests


def grid_tests(setup: Setup, env: Envelope) -> list[tuple[float, float, None]]:
    """Round 1: coarse grid of speeds and engagements inside the power cap."""
    cfg = setup.planner
    speeds = np.linspace(env.speed_min_rpm, env.speed_max_rpm, cfg.round1_speeds)
    speeds = np.unique(np.round(speeds / cfg.speed_step_rpm) * cfg.speed_step_rpm)
    deep = setup.cut.vary is Vary.DEPTH
    step = engagement_step_mm(setup)
    fractions = cfg.round1_depth_fractions if deep else cfg.round1_width_fractions
    engagements = sorted(
        {max(step, round(f * env.engagement_max_mm / step) * step) for f in fractions}
    )
    tests = [
        (float(rpm), float(engagement), None)
        for engagement in engagements
        for rpm in speeds
        if mrr_mm3_min(setup, env, rpm, engagement) <= env.mrr_limit_mm3_min
    ]
    if not tests:
        held = "radial_width_mm" if deep else "axial_depth_mm"
        raise PlanError(f"power cap excludes every round-1 cut; reduce {held} or chip load")
    return tests
