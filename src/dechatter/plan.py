"""Test plans: the ordered passes of one round, where on the stock they run, and the
envelope that bounds them."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import numpy as np

from . import materials
from .config import Direction, Planner, Setup, SpindleControl, Vary, full_width, slot_wall_mm
from .detect import Verdict

# 2: dechatter 0.2.0 (planned_on, per-pass chip loads); 3: 0.6.0 (a round in legs, across
# lanes and faced tops; what the session varies, and each pass's own depth). A schema-2
# plan still loads, as one leg (see _one_leg), stepping the width (see _stepping_width).
# 4: 0.8.0 (the direction a round cuts in, what its first leg starts on). A schema-3 plan
# still loads, cut in its setup's direction (see Plan.load and session.load_rounds).
# 5: 0.8.1 (how a pass meets the lane after its own, the reliefs milled between lanes, each
# pass's lead-out, and the stock each lane had left when the round chose where to start). A
# schema-4 plan still loads, its passes stopping in the stock as 0.8.0 cut them.
SCHEMA = 5
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
    # A session started from an earlier one: a few cuts where that one predicts the best
    # cut, and one in its chatter band as a control, instead of the grid (see seeding).
    CONFIRM = "confirm"
    # The best cut of one level, in a ramp up to it and beyond, cut in the other direction
    # than the session's; not part of the stability map.
    CHECK = "check"


# Rounds that follow convergence: their cuts stay off the stability map, and they are
# planned only where all their cuts fit (cut short, their heaviest cuts would go untried).
FOLLOW_UP = frozenset({PlannerKind.SWEEP, PlannerKind.CHECK})


class Start(StrEnum):
    """What a round's first leg is cut into."""

    FRESH = "fresh"  # a top no round cut into yet: fresh stock, a faced top
    IN_USE = "in use"  # a top earlier rounds cut into, even where this leg's lane is whole


class Milling(StrEnum):
    """How a round's passes meet the stock."""

    SIDE = "side"  # along a lane's front face, each pass stepping it back from Y0
    SLOT = "slot"  # the cutter's full width into the top, each slot in its own row along Y


class Order(StrEnum):
    """The order a round cuts its tests in."""

    RAMP = "ramp"  # lightest first: each cut a small step up from one that ran before it
    DEEPEST_FIRST = "deepest-first"  # as the stock demands where depths differ


class Reach(StrEnum):
    """How far along its lane a pass was cut."""

    END = "end"
    PART = "part"  # stopped part-way: beyond where it stopped the stock stands uncut


class LaneEnd(StrEnum):
    """How a side-milling pass in a lane before the last meets the lane after its own."""

    # dechatter 0.8.0: it stops in the stock, just short of the next lane. Its cutter then
    # sweeps the thin crescent the pass before left at its own stop: near the stop the
    # cutter engages its whole front quarter, as if cutting up to the face's depth wide.
    STOP = "stop"
    # 0.8.1: a pocket is milled where two lanes meet (see Relief). The pass ends in it and
    # the next lane's passes start in it: both ends of every pass are in the air.
    RELIEF = "relief"


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


def other_direction(direction: Direction) -> Direction:
    if direction is Direction.CLIMB:
        return Direction.CONVENTIONAL
    return Direction.CLIMB


def cut_direction(setup: Setup, planner: PlannerKind) -> Direction:
    """The direction a round cuts in: the session's, or for a check the other one."""
    if planner is PlannerKind.CHECK:
        return other_direction(setup.cut.direction)
    return setup.cut.direction


def cutting(setup: Setup, direction: Direction) -> Setup:
    """The setup, cutting in `direction`: lanes, travel and the program follow it."""
    if setup.cut.direction is direction:
        return setup
    return replace(setup, cut=replace(setup.cut, direction=direction))


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
    # A confirmation cut: what the session this one was started from predicts for it.
    expected: Verdict | None = None
    # The feed move after the cutter leaves the stock. None in a plan of dechatter 0.8.0 or
    # older, whose analysis took the lead-in off both ends of the cut.
    lead_out_s: float | None = None

    @property
    def lead_off_s(self) -> float:
        """How long the cut move goes on once the cutter has left the stock."""
        return self.lead_in_s if self.lead_out_s is None else self.lead_out_s


@dataclass(frozen=True)
class Relief:
    """A pocket where two lanes meet, at `boundary` k: between lanes k and k+1, counted as
    the lanes are. The passes of lane k end in it and those of lane k+1 start in it, so
    both ends of every pass are in the air and no pass ever crosses another lane.

    It is milled the way the top is faced (see relief_rows): a little wider than the cutter,
    from the front face back to `y_mm` from Y0 and down to `depth_mm` below the leg's top,
    in rows and levels no larger than facing's. It reaches as far back and as deep as the
    passes that use it in the round; `done_y_mm` and `done_depth_mm` are what earlier
    rounds milled of it on this top, left out."""

    boundary: int
    y_mm: float
    depth_mm: float
    done_y_mm: float = 0.0
    done_depth_mm: float = 0.0


@dataclass(frozen=True)
class LaneStock:
    """A lane as a round found it when it chose where to start: the stock left behind its
    face (the width to use, less how far back it was cut) and whether the round's first cut
    fits on it."""

    lane: int
    left_mm: float
    open: bool


@dataclass(frozen=True)
class Leg:
    """Part of a round cut on one lane. With `reface_mm`, the program first takes the top
    down that much, to `top_mm` below Z0, its feed moves taking `face_s`; then it mills the
    `reliefs` the leg's passes start or end in, taking `relief_s`. Then the passes use the
    lane's Y from `face_y_start_mm` on, no further than `lane_cap_mm`: what the lane before
    it used (None: all the stock has; only slots are bound by it now). Side milling cuts the
    front face back by each pass's width; a slot takes its width with `wall_mm` of stock
    left standing before it."""

    lane: int
    face_y_start_mm: float
    passes: tuple[Pass, ...]
    lane_cap_mm: float | None = None
    top_mm: float = 0.0
    reface_mm: float = 0.0
    face_s: float = 0.0
    wall_mm: float = 0.0
    reliefs: tuple[Relief, ...] = ()
    relief_s: float = 0.0

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
    # the top down and start again at the first lane (1 is the one the cutter meets first,
    # cutting in `direction`).
    legs: tuple[Leg, ...]
    planned_on: str  # ISO date
    # The direction the round cuts in: the session's, or the other for a check. None only
    # in a plan of dechatter 0.7.0 or older, read alone: it cut in the direction of the setup
    # beside it, which session.load_rounds fills in.
    direction: Direction | None
    start: Start
    lanes: int = 1
    schema: int = SCHEMA
    # How passes meet the lane after their own. A plan of 0.8.0 or older, which has no such
    # field, stopped them in the stock.
    lane_end: LaneEnd = LaneEnd.STOP
    # The lanes as the round found them when it chose the one to start in: the one with the
    # most stock left that its first cut fits on. Empty where it had no choice to make.
    lane_stock: tuple[LaneStock, ...] = ()

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
        """Feed moves and dwells, facing and reliefs included. Rapids come on top and depend
        on the machine."""
        cuts = sum(
            self.dwell_s + p.plunge_s + p.lead_in_s + p.expected_cut_s + (p.lead_out_s or 0.0)
            for p in self.passes
        )
        return sum(leg.face_s + leg.relief_s for leg in self.legs) + cuts

    def save(self, path: Path) -> None:
        # NaN and infinity are not JSON (the widget cannot read them back): refuse them here.
        text = json.dumps(asdict(self), indent=2, allow_nan=False)
        path.write_text(text + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Plan:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") in (3, 4):
            data = {**data, "schema": SCHEMA}
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
                    "passes": tuple(_pass(p) for p in leg["passes"]),
                    "reliefs": tuple(Relief(**r) for r in leg.get("reliefs", ())),
                }
            )
            for leg in data.pop("legs")
        )
        direction = data.pop("direction", None)
        return Plan(
            **{
                **data,
                "planner": PlannerKind(data["planner"]),
                "vary": Vary(data["vary"]),
                "milling": Milling(data["milling"]),
                "legs": legs,
                "direction": None if direction is None else Direction(direction),
                "start": Start(data["start"]) if "start" in data else _start_of(legs[0]),
                "lane_end": LaneEnd(data.get("lane_end", LaneEnd.STOP)),
                "lane_stock": tuple(LaneStock(**s) for s in data.get("lane_stock", ())),
            }
        )


def _pass(p: dict) -> Pass:
    expected = Verdict(p["expected"]) if p.get("expected") else None
    return Pass(**{**p, "kind": PassKind(p["kind"]), "expected": expected})


def _start_of(first: Leg) -> Start:
    """What the first leg of a plan of dechatter 0.7.0 or older started on. Every round then
    cut one way, and one that started at Y0 of its first lane had nothing cut before it on
    that top: earlier rounds' cuts would have left it further on."""
    return Start.FRESH if first.lane == 1 and first.face_y_start_mm == 0 else Start.IN_USE


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
    # A manual spindle's speed stops inside the search: the only speeds a cut is planned at.
    # Empty: any speed on the planner's step.
    speed_stops_rpm: tuple[int, ...] = ()

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
        """The speed axis of the map: the planner's steps, and the speed stops among them."""
        stepped = np.arange(self.speed_min_rpm, self.speed_max_rpm + 1, step_rpm, dtype=float)
        return np.union1d(stepped, np.array(self.speed_stops_rpm, dtype=float))

    def settable(self, speeds: np.ndarray) -> np.ndarray:
        """Which of `speeds` a cut may be planned at: the speed stops, if the machine has any."""
        if not self.speed_stops_rpm:
            return np.ones(len(speeds), dtype=bool)
        return np.isin(speeds, np.array(self.speed_stops_rpm, dtype=float))

    def nearest_stop(self, rpm: float) -> float:
        """The speed stop nearest `rpm`; the machine must have some."""
        return float(min(self.speed_stops_rpm, key=lambda stop: abs(stop - rpm)))

    def engagements(self, step_mm: float) -> np.ndarray:
        count = max(1, int(round(self.engagement_top_mm / step_mm)))
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


def _levels(setup: Setup, engagement_max_mm: float, step_mm: float) -> int:
    """How many steps the search takes up to its largest cut. A width rounds to its step, a
    hair over at most. A depth never goes over: depth_max_mm is as deep as the setup lets a
    cut go, and what stock.height_mm was checked against."""
    if setup.cut.vary is Vary.DEPTH:
        return max(1, math.floor(engagement_max_mm / step_mm + 1e-9))
    return max(1, int(round(engagement_max_mm / step_mm)))


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
    speed_range = Envelope(
        chip_load_mm=round(chip_load, 4),
        speed_min_rpm=speed_min,
        speed_max_rpm=speed_max,
        engagement_max_mm=engagement_max,
        mrr_limit_mm3_min=usable_w / material.specific_energy_j_mm3 * 60,
        speed_low_rpm=speed_low,
        speed_high_rpm=speed_high,
        engagement_top_mm=engagement_step * _levels(setup, engagement_max, engagement_step),
    )
    stops = setup.machine.speed_stops_rpm
    if not stops:
        return speed_range
    inside = tuple(stop for stop in stops if speed_range.holds_speed(stop))
    if not inside:
        raise PlanError(
            f"no speed stop ([machine] speed_stops_rpm) lies within the speeds searched, "
            f"{speed_low:.0f}..{speed_high:.0f} rpm"
        )
    return replace(speed_range, speed_stops_rpm=inside)


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


def relief_step_mm(setup: Setup) -> float:
    """How far apart the rows of facing, and of a relief, are: program.face_width_mm, else a
    quarter of the cutter."""
    return setup.program.face_width_mm or setup.tool.diameter_mm / 4


def relief_wall_mm(setup: Setup) -> float:
    """How far a relief's walls stand along the feed from the boundary it is milled at: a
    cutter radius and a row step, so the pocket is a cutter diameter and two steps wide."""
    return setup.tool.diameter_mm / 2 + relief_step_mm(setup)


def travel(
    setup: Setup,
    lane: int,
    lanes: int,
    face_mm: float,
    width_mm: float,
    lane_end: LaneEnd = LaneEnd.RELIEF,
) -> Travel:
    """The feed move of a pass `width_mm` wide whose far side along Y is `face_mm` from Y0:
    the face a side-milling pass leaves, or a slot's far wall.

    The first lane's passes start in the air in front of the stock's end, and the last
    lane's run out past its other end. Where lanes meet, with `lane_end` RELIEF, a pocket is
    milled at the boundary first (see Relief): a pass ends with its cutter centred on the
    boundary, inside the pocket, and the next lane's passes start there, so both ends of
    every pass are in the air and no pass crosses another lane. The cutter stops cutting at
    the pocket's near wall and starts at its far one.

    With STOP (dechatter 0.8.0, and slots, each in a row of its own), a lane starts where the
    one before it was cut: the cutter starts a diameter clear of what that lane left at its
    far end, and a pass in a lane before the last stops in the stock, its cutter just short
    of the next lane, still uncut at Y0.
    """
    diameter, clearance = setup.tool.diameter_mm, setup.program.clearance_mm
    radius = diameter / 2
    reach = _reach(diameter, width_mm)
    length = setup.stock.length_mm / lanes
    first, last = (lane - 1) * length, lane * length
    if lane_end is LaneEnd.STOP:
        start = first - radius - clearance - (radius if lane > 1 else 0.0)
        contact = first - reach
        if lane == lanes:
            leave = setup.stock.length_mm + reach
            return Travel(start, contact, leave, setup.stock.length_mm + radius + clearance)
        stop = last - _reach(diameter, min(face_mm, radius))
        return Travel(start, contact, stop, stop)
    wall = relief_wall_mm(setup)
    start, contact = -radius - clearance, -reach
    if lane > 1:
        start, contact = first, first + wall - reach
    leave, end = setup.stock.length_mm + reach, setup.stock.length_mm + radius + clearance
    if lane < lanes:
        leave, end = last - wall + reach, last
    return Travel(start, contact, leave, end)


def auto_lanes(setup: Setup, env: Envelope) -> int:
    """The setup's lanes, or when it leaves them empty, as many as the stock length holds
    with every lane at least twice the cutter diameter long and every cut at least
    `planner.min_cut_s` long at the fastest test speed (where cuts are shortest; with speed
    stops, the fastest stop), less what the reliefs take at a side-milling lane's inner
    ends (see Relief)."""
    if setup.stock.lanes is not None:
        return setup.stock.lanes
    fastest = env.speed_stops_rpm[-1] if env.speed_stops_rpm else env.speed_max_rpm
    feed_mm_s = env.chip_load_mm * setup.tool.flutes * fastest / 60
    cut = feed_mm_s * setup.planner.min_cut_s
    shortest = 2 * setup.tool.diameter_mm
    wall = relief_wall_mm(setup) if milling(setup) is Milling.SIDE else 0.0
    lanes = max(1, int(setup.stock.length_mm / max(shortest, cut) + 1e-9))
    while lanes > 1:
        length = setup.stock.length_mm / lanes
        inner = 1 if lanes == 2 else 2  # the ends a relief takes, in the most hemmed-in lane
        if length >= shortest - 1e-9 and length - inner * wall >= cut - 1e-9:
            break
        lanes -= 1
    return lanes


def lane_x_mm(setup: Setup, lane: int, lanes: int) -> tuple[float, float]:
    """Where a lane lies along X, left end first; climb milling uses the lanes from the right."""
    length = setup.stock.length_mm / lanes
    first, last = (lane - 1) * length, lane * length
    if setup.cut.direction is Direction.CLIMB:
        first, last = setup.stock.length_mm - last, setup.stock.length_mm - first
    return round(first, 3), round(last, 3)


def boundary_x_mm(setup: Setup, boundary: int, lanes: int) -> float:
    """Where the boundary between lanes `boundary` and `boundary` + 1 lies along X, the
    lanes counted in the setup's direction."""
    at = boundary * setup.stock.length_mm / lanes
    if setup.cut.direction is Direction.CLIMB:
        at = setup.stock.length_mm - at
    return round(at, 3)


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
    expected: Verdict | None = None,
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
        expected=expected,
        lead_out_s=round((path.end - path.leave) / feed * 60, 2),
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


@dataclass(frozen=True)
class ReliefRow:
    z_mm: float  # the level, from Z0
    above_mm: float  # the floor already milled above it here, from Z0 (the top for the first)
    y_mm: float  # the cutter's centre
    start: float  # along the feed, as Travel
    end: float


def relief_rows(setup: Setup, lanes: int, top_mm: float, relief: Relief) -> list[ReliefRow]:
    """The rows that mill `relief` into a top `top_mm` below Z0: levels no deeper than
    program.face_depth_mm down to `relief.depth_mm`, each in rows along the feed that step
    back from the front face by at most a row step (relief_step_mm) up to `relief.y_mm`,
    from a step before the boundary to a step after it, as facing does: each row cuts on its
    +Y side only, at most a step wide and a level deep. The pocket's ends are in the stock,
    where a row fed into its end would cut away the crescents the end caps of the rows
    before it left, as wide as the pocket is deep so far, as a pass stopped in the stock did
    (see Relief). So a row first plunges, at the plunge feed, at its end and then at its
    start, into what the rows before left of its level there (at most a step wide at the
    middle, and those crescents), and is then fed from one to the other: never a full-width
    slot. Rows wholly inside what earlier rounds milled (`done_y_mm`, `done_depth_mm`) are
    left out."""
    radius, step = setup.tool.diameter_mm / 2, relief_step_mm(setup)
    rows = max(1, math.ceil(relief.y_mm / step - 1e-9))
    levels = max(1, math.ceil(relief.depth_mm / setup.program.face_depth_mm - 1e-9))
    at = relief.boundary * setup.stock.length_mm / lanes
    found = []
    for level in range(levels):
        down = relief.depth_mm * (level + 1) / levels
        above = relief.depth_mm * level / levels
        for row in range(rows):
            back = relief.y_mm * (row + 1) / rows
            done = back <= relief.done_y_mm + 1e-6 and down <= relief.done_depth_mm + 1e-6
            if done:
                continue
            found.append(
                ReliefRow(
                    round(top_mm + down, 3),
                    round(top_mm + above, 3),
                    round(back - radius, 3),
                    round(at - step, 3),
                    round(at + step, 3),
                )
            )
    return found


def relief_s(setup: Setup, rows: list[ReliefRow], feed_mm_min: float) -> float:
    """The feed moves of milling reliefs: each row's two plunges and its cut."""
    clearance, plunge = setup.program.clearance_mm, setup.program.plunge_feed_mm_min
    return sum(
        2 * (clearance + r.z_mm - r.above_mm) / plunge * 60 + (r.end - r.start) / feed_mm_min * 60
        for r in rows
    )


def wall_mm(setup: Setup) -> float:
    """The stock left standing before each pass along Y: a slot's wall; none in side milling."""
    return slot_wall_mm(setup) if milling(setup) is Milling.SLOT else 0.0


def lane_budget_mm(setup: Setup, lane_cap_mm: float | None) -> float:
    """How far from Y0 a lane's passes may go. Side milling: the stock width to use. Slots:
    the stock's thickness, less the wall behind the last one, and in a lane after the first
    no further than the lane before it was cut (the cutter comes in through its slots)."""
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
    it left, or a slot's far wall), how deep it went, how it met the stock, and whether it
    ran the lane's length."""

    lane: int
    y_mm: float
    depth_mm: float
    milling: Milling = Milling.SIDE
    reach: Reach = Reach.END


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


# Slots of two lanes are in one row when their far walls are this close: what rounding a
# leg's wall and start to the plan's 0.001 mm adds up to over a stock's rows.
ROW_TOLERANCE_MM = 0.01


def _lane_floor(lane: int, y_mm: float, made: list[Made]) -> float:
    """How deep a slot reaching `y_mm` from Y0 may go in a lane after the first: it comes in
    through the slot of its own row in the lane before, and no other (beside it stand the
    walls), which is clear only as deep as it was cut. That slot must have run the lane's
    length, since the cutter plunges inside its far end: one stopped part-way clears nothing
    for the lane after it. With no pass of that lane on record, only the face's cap bounds
    the cut. (Side-milling lanes do not bound each other: see Relief.)
    """
    before = [m for m in made if m.lane == lane - 1]
    if not before:
        return math.inf
    row = [
        m for m in before if m.milling is Milling.SLOT and abs(m.y_mm - y_mm) <= ROW_TOLERANCE_MM
    ]
    if not row or any(m.reach is Reach.PART for m in row):
        return -math.inf
    return min(m.depth_mm for m in row)


def lane_end_of(setup: Setup) -> LaneEnd:
    """How the setup's passes meet the lane after their own: side milling ends them in a
    relief; a slot, in a row of its own, stops in the stock."""
    return LaneEnd.RELIEF if milling(setup) is Milling.SIDE else LaneEnd.STOP


def lane_face(lane: int, made: list[Made], top: Face) -> Face:
    """Where a side-milling lane stands on the top `top` describes: its face as far back as
    the passes that ran its length cut it, whichever way they cut, and the step beyond that
    a pass stopped part-way left, to clear first. What 0.8.0 left where it stopped passes in
    the stock, at the lane's ends, a relief clears. Lanes do not bound each other: no pass
    crosses another lane (see Relief)."""
    here = [m for m in made if m.lane == lane]
    whole = max((m.y_mm for m in here if m.reach is Reach.END), default=0.0)
    reached = max((m.y_mm for m in here), default=0.0)
    return Face(lane, whole, max(reached - whole, 0.0), None, top.top_mm, top.top)


def _start(
    setup: Setup, env: Envelope, tests: list[tuple], made: list[Made], lanes: int, top: Face
) -> tuple[Face, tuple[LaneStock, ...]]:
    """Where a side-milling round starts: in the lane with the most stock left behind its
    face (the thickest wall behind the cut) that its first cut fits on, the first such lane
    of equals; with none, the one with the most stock left, for the reason it has no room.
    With each lane's stock as the round found it."""
    found = []
    for lane in range(1, lanes + 1):
        face = lane_face(lane, made, top)
        first = cut_order(setup, tests, lanes, _face_floor(setup, lane, made))
        found.append((face, bool(_fill(setup, env, face, first, made)[1])))
    width = setup.stock.width_mm
    stock = tuple(
        LaneStock(face.lane, round(max(width - face.y_mm - face.remnant_mm, 0.0), 3), fits)
        for face, fits in found
    )
    pool = [face for face, fits in found if fits] or [face for face, _ in found]
    return min(pool, key=lambda f: (f.y_mm + f.remnant_mm, f.lane)), stock


def _reliefs(
    lane: int, lanes: int, y_mm: float, depth_mm: float, relieved: dict[int, tuple[float, float]]
) -> tuple[Relief, ...]:
    """The reliefs a leg on `lane` mills before its passes, which reach `y_mm` from Y0 and
    `depth_mm` deep: at the boundary its passes start at and the one they end at, wherever
    what is milled there on this top (`relieved`: boundary -> how far back, how deep; updated)
    does not reach that far."""
    found = []
    for boundary in (lane - 1, lane):
        if not 0 < boundary < lanes:
            continue
        y0, d0 = relieved.get(boundary, (0.0, 0.0))
        if y_mm <= y0 + 1e-6 and depth_mm <= d0 + DEPTH_TOLERANCE_MM:
            continue
        relief = Relief(
            boundary,
            round(max(y_mm, y0), 3),
            round(max(depth_mm, d0), 3),
            round(y0, 3),
            round(d0, 3),
        )
        relieved[boundary] = (relief.y_mm, relief.depth_mm)
        found.append(relief)
    return tuple(found)


def _too_low(setup: Setup, top_mm: float, depth_mm: float) -> bool:
    """Whether a cut `depth_mm` deep, on a top faced down to `top_mm` below Z0, would come
    within the clearance of what stock.height_mm keeps the cutter above. On the top as it
    was mounted the setup itself says how deep a cut may go (see config)."""
    height = setup.stock.height_mm
    if height is None or top_mm <= 0:
        return False
    return top_mm + depth_mm + setup.program.clearance_mm > height + DEPTH_TOLERANCE_MM


def _fill(
    setup: Setup, env: Envelope, face: Face, tests: list[tuple], made: list[Made]
) -> tuple[list[tuple[float, float]], list[tuple]]:
    """(face preparation, the tests that fit after it): `tests` in order, up to the first
    that does not fit, or that cuts deeper than the stock in front of it is clear (`made`:
    the passes cut into the top as it stands), or too near what the stock stands on."""
    prep = preparation_passes(setup, env, face.remnant_mm)
    y = face.y_mm + sum(w for _, w in prep)
    budget = lane_budget_mm(setup, face.cap_mm)
    wall = wall_mm(setup)
    floor = _face_floor(setup, face.lane, made)
    slots = milling(setup) is Milling.SLOT
    kept = []
    for test in tests:
        width, depth = cut_size(setup, test[1])
        y += width + wall
        clear = min(floor, _lane_floor(face.lane, y, made)) if slots else floor
        if depth > clear + DEPTH_TOLERANCE_MM or y > budget + 1e-9:
            break
        if _too_low(setup, face.top_mm, depth):
            break
        if not slots:
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
    relieved: dict[int, tuple[float, float]],
) -> Leg:
    """The leg that prepares `face`, after taking the top down `reface_mm` to it and milling
    the reliefs its passes need (see _reliefs; `relieved` is updated), and cuts `tests` on
    it; its passes are numbered from `first_id`."""
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
    ends = lane_end_of(setup)
    passes = []
    y = face.y_mm
    for kind, rpm, width, depth, *rest in planned:
        y += width + wall
        path = travel(setup, face.lane, lanes, y, width, ends)
        pass_id = first_id + len(passes)
        passes.append(make_pass(setup, env, pass_id, kind, rpm, width, depth, path, *rest))
    reliefs = ()
    if ends is LaneEnd.RELIEF:
        reliefs = _reliefs(face.lane, lanes, y, max(p.depth_mm for p in passes), relieved)
    rows = [row for r in reliefs for row in relief_rows(setup, lanes, face.top_mm, r)]
    # The top comes down, and the reliefs are milled, at the leg's first speed: no extra
    # spindle stop.
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
        reliefs=reliefs,
        relief_s=round(relief_s(setup, rows, feed), 2),
    )


@dataclass(frozen=True)
class Layout:
    legs: list[Leg]
    left: list[tuple]  # the tests left over
    start: Face  # where the round started, or would have
    stock: tuple[LaneStock, ...]  # the lanes it chose that from; empty without a choice


def _lay_out(
    setup: Setup,
    env: Envelope,
    tests: list[tuple],
    face: Face,
    made: tuple[Made, ...],
    lanes: int,
    lanes_open: int | None = None,
    reliefs: tuple[Relief, ...] = (),
    pick: bool = False,
) -> Layout:
    """The legs the tests fill in cut order from `face` on, and the tests left over.

    With `pick` (side milling in lanes), the round starts in the lane with the most stock
    left that its first cut fits on (see _start), `face` giving only the top; `made` are
    the passes cut into the top as it stands, lanes counted in this round's direction.

    A leg takes the tests that fit on its lane, up to the first that does not, or that cuts
    deeper than the stock in front of it is clear (see _face_floor; for slots _lane_floor).
    The next leg is the next lane, where it stands (side milling, see lane_face; `reliefs`
    are those milled on this top, see Relief) or fresh at Y0 and cut back no further than
    this one was (slots), and on this top no further than lane `lanes_open` (None: the
    last; for slots, the lanes after it hold cuts made in the other direction). Once no
    lane of this top takes the next test, or the top was cut the other way (slots under a
    face to mill, or the reverse), or the top is faced only in part (a facing stopped
    part-way), the top comes down below the deepest of those cuts, given stock.height_mm and
    while the deepest cut left would stay the clearance above it, and the round goes on at
    the first lane. A round faces the top at most once: facing is slow, so the tests the
    lanes of the new top cannot hold are left over (assemble drops them).
    """
    kind = milling(setup)
    side = kind is Milling.SIDE
    cut = list(made)
    # Each relief extends the one before it at its boundary on this top (see _reliefs), so
    # together they reach as far back and as deep as the furthest and deepest.
    relieved: dict[int, tuple[float, float]] = {}
    for r in reliefs:
        y0, d0 = relieved.get(r.boundary, (0.0, 0.0))
        relieved[r.boundary] = (max(y0, r.y_mm), max(d0, r.depth_mm))
    # Nothing is cut on a top that holds the other kind of cut, or is faced only in part.
    unfit = any(m.milling is not kind for m in cut) or face.top is not Top.FLAT
    open_here = lanes if lanes_open is None else lanes_open
    stock: tuple[LaneStock, ...] = ()
    if pick and side and lanes > 1 and not unfit:
        face, stock = _start(setup, env, tests, cut, lanes, face)
    start = face
    # Cut order follows the face the round starts on, if it can cut there at all.
    usable = not unfit and face.lane <= open_here
    left = cut_order(
        setup, tests, lanes, _face_floor(setup, face.lane, cut) if usable else math.inf
    )
    legs: list[Leg] = []
    here, reface = face, 0.0
    while left:
        usable = not unfit and here.lane <= open_here
        prep, kept = _fill(setup, env, here, left, cut) if usable else ([], [])
        if kept:
            first_id = sum(len(leg.passes) for leg in legs)
            legs.append(_leg(setup, env, lanes, here, reface, prep, kept, first_id, relieved))
            left = left[len(kept) :]
            y = here.y_mm
            for p in legs[-1].passes:
                y += p.width_mm + legs[-1].wall_mm
                cut.append(Made(here.lane, y, p.depth_mm, kind))
            reface = 0.0
            here = Face(here.lane, legs[-1].face_y_end_mm, 0.0, here.cap_mm, here.top_mm)
        if not left:
            break
        if side:
            beyond = lane_face(here.lane + 1, cut, here)
            further = here.lane < open_here and not unfit
            floor = _face_floor(setup, beyond.lane, cut)
        else:
            beyond = Face(here.lane + 1, 0.0, 0.0, here.y_mm, here.top_mm)
            further = here.y_mm > 0 and here.lane < open_here and not unfit
            # A fresh lane is clear as deep as the lane before was cut at its start: the
            # slots no deeper than that first, deepest first, whatever this lane allowed.
            floor = _lane_floor(beyond.lane, 0.0, cut)
        ahead = cut_order(setup, left, lanes, floor)
        if further and _fill(setup, env, beyond, ahead, cut)[1]:
            here, left = beyond, ahead
            continue
        # Every lane of this top is used, or none would hold the next test (deeper than its
        # face is flat; for slots, a lane after this one may go no further back, nor deeper):
        # only a fresh top can, once a round.
        height = setup.stock.height_mm
        if height is None or not cut or here.top_mm > face.top_mm:
            break
        down = max(m.depth_mm for m in cut) + REFACE_EXTRA_MM
        lower = Face(top_mm=here.top_mm + down)
        deepest = max(cut_size(setup, test[1])[1] for test in left)
        if lower.top_mm + deepest + setup.program.clearance_mm > height:
            break
        # A fresh top: deepest first again, whatever the face before allowed.
        fresh = cut_order(setup, left, lanes)
        if not _fill(setup, env, lower, fresh, [])[1]:
            break
        here, cut, unfit, reface, open_here, left = lower, [], False, down, lanes, fresh
        relieved = {}  # facing takes the reliefs off with the top
    return Layout(legs, left, start, stock)


def _no_room(
    setup: Setup,
    round_no: int,
    planner: PlannerKind,
    tests: list[tuple],
    face: Face,
    made: tuple[Made, ...],
    lanes: int,
    lanes_open: int | None,
    laid: Layout,
) -> str:
    if setup.stock.height_mm is None:
        hint = (
            "Mount fresh stock and plan with new stock, or give the stock's height above "
            "the vise jaws ([stock] height_mm) so dechatter can face the top down itself"
        )
    else:
        hint = "The top cannot come down further: mount fresh stock and plan with new stock"
    if face.top is Top.PART_FACED and not laid.legs:
        return (
            f"the last round stopped part-way through facing the top down, so round {round_no} "
            f"must face it again first, and it has no room to. {hint}."
        )
    if laid.legs:
        name = "chip-load sweep"
        if planner is PlannerKind.CHECK:
            name = f"{setup.cut.direction} check"
        return (
            f"the {name} needs room for all its {len(tests)} cuts: no face left has "
            f"room for them. {hint}."
        )
    if lanes_open is not None and face.lane > lanes_open:
        return (
            f"round {round_no} cuts {setup.cut.direction}, using the lanes from the end of the "
            "stock that direction meets first, and every lane from there up to those cut the "
            f"other way is used. {hint}."
        )
    slots = milling(setup) is Milling.SLOT
    if any(m.milling is not milling(setup) for m in made):
        return (
            f"round {round_no} cuts {'slots into the top' if slots else 'the front face'}, "
            f"but this block's top holds {'side cuts' if slots else 'slots'} from earlier "
            f"rounds. {hint}."
        )
    start = laid.start
    floor = _face_floor(setup, start.lane, list(made))
    first = cut_order(setup, tests, lanes, floor)[0]
    depth = cut_size(setup, first[1])[1]
    if _too_low(setup, start.top_mm, depth):
        return (
            f"round {round_no} would cut {depth:g} mm deep below a top already faced down "
            f"{start.top_mm:g} mm, less than the clearance ({setup.program.clearance_mm:g} mm) "
            f"above [stock] height_mm ({setup.stock.height_mm:g} mm). {hint}."
        )
    where = f" in lane {start.lane} of {lanes}" if lanes > 1 else ""
    if depth > floor + DEPTH_TOLERANCE_MM:
        return (
            f"round {round_no} would cut {depth:g} mm deep, but this block's face{where} is "
            f"only flat down to {floor:g} mm. {hint}."
        )
    room = lane_budget_mm(setup, start.cap_mm)
    if slots:
        return (
            f"stock exhausted: no room left for a slot{where} "
            f"({max(room - start.y_mm, 0.0):.1f} mm of {room:.1f} mm), and none after it has "
            f"room. {hint}."
        )
    if laid.stock:
        each = "; ".join(f"lane {s.lane}: {s.left_mm:.1f} mm" for s in laid.stock)
        return (
            f"stock exhausted: no lane has room for the first cut, of {room:.1f} mm each "
            f"({each} left). {hint}."
        )
    used = start.y_mm + start.remnant_mm
    return (
        f"stock exhausted: {max(room - used, 0.0):.1f} mm left of {room:.1f} mm{where}, "
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
    lanes_open: int | None = None,
    reliefs: tuple[Relief, ...] = (),
    pick: bool = False,
) -> Plan:
    """Lay the tests out on the stock from `face` on, in legs that each prepare their face
    first (see _lay_out), on the setup's lanes unless `lanes` says otherwise, cutting in the
    setup's direction.

    A test is (rpm, engagement, P(stable)), in a chip-load sweep (rpm, engagement, None,
    chip load), already lightest first, or in a confirmation round (rpm, engagement,
    P(stable), None, the verdict expected); the engagement is the width, or the depth when the
    setup steps that. `made` are the passes cut into the top as it stands (none: it is
    untouched) and `reliefs` the reliefs milled into it, lanes and boundaries counted in the
    setup's direction; of its lanes only the first `lanes_open` may be cut (None: all; for
    slots, the rest hold cuts made in the other direction). With `pick`, side milling starts
    in the lane with the most stock left (see _start). Tests left over once the stock runs
    out are dropped, the heaviest ones: a ramp leaves them for last, and a round that runs
    deepest first is laid out again without them, since the lighter cuts are what the
    heavier ones were chosen on; so too when its deepest cut fits nowhere (deeper than any
    face left is clear, or too near what the stock stands on) and lighter ones do. But a
    round that follows convergence (a chip-load sweep, a check of the other direction) must
    fit whole: cut short, its heaviest cuts would go untried and the lightest read as a
    limit.
    """
    lanes = lanes or auto_lanes(setup, env)
    asked = tests
    place = (face, made, lanes, lanes_open, reliefs, pick)
    laid = _lay_out(setup, env, tests, *place)
    if laid.left and planner in FOLLOW_UP:
        raise PlanError(
            _no_room(setup, round_no, planner, asked, face, made, lanes, lanes_open, laid)
        )
    deepest_first = order(setup.cut.vary, milling(setup), lanes) is Order.DEEPEST_FIRST
    while laid.left and deepest_first and len(tests) > 1:
        # With nothing laid out the heaviest test is in the way: try without it.
        keep = len(tests) - len(laid.left) if laid.legs else len(tests) - 1
        lighter = sorted(tests, key=lambda t: (t[1], t[0]))[:keep]
        again = _lay_out(setup, env, lighter, *place)
        if laid.legs and not again.legs:
            break
        tests, laid = lighter, again
    if not laid.legs:
        raise PlanError(
            _no_room(setup, round_no, planner, asked, face, made, lanes, lanes_open, laid)
        )
    whole = not made and face.lane == 1 and face.y_mm == 0 and face.remnant_mm == 0
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
        legs=tuple(laid.legs),
        planned_on=date.today().isoformat(),
        direction=setup.cut.direction,
        start=Start.FRESH if whole else Start.IN_USE,
        lanes=lanes,
        lane_end=lane_end_of(setup),
        lane_stock=laid.stock if made else (),
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


def grid_engagements(setup: Setup, env: Envelope) -> list[float]:
    """The engagements round 1 cuts each of its speeds at, lightest first."""
    cfg = setup.planner
    deep = setup.cut.vary is Vary.DEPTH
    step = engagement_step_mm(setup)
    fractions = cfg.round1_depth_fractions if deep else cfg.round1_width_fractions
    return sorted(
        {
            min(env.engagement_top_mm, max(step, round(f * env.engagement_max_mm / step) * step))
            for f in fractions
        }
    )


def _round1_speeds(cfg: Planner, env: Envelope) -> np.ndarray:
    """Round 1's speeds, evenly over the search: on the planner's step, or with speed stops,
    the stops spread from the slowest to the fastest."""
    stops = env.speed_stops_rpm
    if stops:
        picks = np.linspace(0, len(stops) - 1, min(cfg.round1_speeds, len(stops)))
        return np.array([stops[i] for i in np.unique(picks.round().astype(int))], dtype=float)
    speeds = np.linspace(env.speed_min_rpm, env.speed_max_rpm, cfg.round1_speeds)
    return np.unique(np.round(speeds / cfg.speed_step_rpm) * cfg.speed_step_rpm)


def check_tests(setup: Setup, env: Envelope, rpm: float, engagement_mm: float) -> list[tuple]:
    """(rpm, engagement, None) for a check of one cut in the other direction, lightest
    first: the planner's fractions of it (check_fractions, on the engagement step), the cut
    itself, then check_steps_beyond steps heavier while they stay inside the search. The
    power cap trims the heaviest."""
    cfg = setup.planner
    step = engagement_step_mm(setup)
    sizes = {engagement_mm}
    for fraction in cfg.check_fractions:
        lighter = round(max(step, round(fraction * engagement_mm / step) * step), 6)
        if lighter < engagement_mm - ENGAGEMENT_TOLERANCE_MM:
            sizes.add(lighter)
    for steps in range(1, cfg.check_steps_beyond + 1):
        heavier = round(engagement_mm + steps * step, 6)
        if heavier > env.engagement_top_mm + ENGAGEMENT_TOLERANCE_MM:
            break
        sizes.add(heavier)
    limit = env.mrr_limit_mm3_min * (1 + 1e-9)
    return [
        (rpm, size, None) for size in sorted(sizes) if mrr_mm3_min(setup, env, rpm, size) <= limit
    ]


def grid_tests(setup: Setup, env: Envelope) -> list[tuple[float, float, None]]:
    """Round 1: coarse grid of speeds and engagements inside the power cap."""
    cfg = setup.planner
    speeds = _round1_speeds(cfg, env)
    deep = setup.cut.vary is Vary.DEPTH
    tests = [
        (float(rpm), float(engagement), None)
        for engagement in grid_engagements(setup, env)
        for rpm in speeds
        if mrr_mm3_min(setup, env, rpm, engagement) <= env.mrr_limit_mm3_min
    ]
    if not tests:
        held = "radial_width_mm" if deep else "axial_depth_mm"
        raise PlanError(f"power cap excludes every round-1 cut; reduce {held} or chip load")
    return tests
