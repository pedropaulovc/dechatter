"""Test plans: the ordered passes of one round, and the envelope that bounds them."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import materials
from .config import Direction, Setup, SpindleControl

SCHEMA = 2  # 2: dechatter 0.2.0 (planned_on, per-pass chip loads)
SPINDLE_EFFICIENCY = 0.8
SPEED_TOLERANCE_RPM = 0.5
WIDTH_TOLERANCE_MM = 1e-6


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
class Plan:
    round: int
    planner: PlannerKind
    flutes: int
    diameter_mm: float
    axial_depth_mm: float
    chip_load_mm: float
    face_y_start_mm: float
    dwell_s: float
    plunge_s: float  # feed move from the clearance plane down to depth, beside the stock
    passes: tuple[Pass, ...]
    planned_on: str  # ISO date
    # The lane of the stock the round cuts (1 is the first the cutter meets), of how many,
    # and how far back its face may go: the face the lane before it was left with.
    lane: int = 1
    lanes: int = 1
    lane_cap_mm: float | None = None
    schema: int = SCHEMA

    @property
    def consumed_mm(self) -> float:
        return sum(p.width_mm for p in self.passes)

    @property
    def run_time_s(self) -> float:
        """Feed moves and dwells. Rapids come on top and depend on the machine."""
        per_pass = self.dwell_s + self.plunge_s
        return sum(per_pass + p.lead_in_s + p.expected_cut_s for p in self.passes)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def load(path: Path) -> Plan:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise SchemaError(path, data.get("schema"), SCHEMA)
        passes = tuple(Pass(**{**p, "kind": PassKind(p["kind"])}) for p in data.pop("passes"))
        return Plan(**{**data, "planner": PlannerKind(data["planner"]), "passes": passes})


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


def lane_budget_mm(setup: Setup, lane_cap_mm: float | None) -> float:
    """How far back from Y0 the lane's face may go: the stock width to use, and in a lane
    after the first, no further than the lane before it was cut back (the cutter crosses it)."""
    return min(setup.stock.width_mm, math.inf if lane_cap_mm is None else lane_cap_mm)


def _fit(
    setup: Setup,
    env: Envelope,
    face_y_mm: float,
    remnant_mm: float,
    lane_cap_mm: float | None,
    tests: list[tuple],
) -> tuple[list[tuple[float, float]], list[tuple]]:
    """(face preparation, the tests that fit after it), narrowest tests first."""
    prep = preparation_passes(setup, env, remnant_mm)
    budget = lane_budget_mm(setup, lane_cap_mm) - face_y_mm - sum(w for _, w in prep)
    kept = []
    for test in sorted(tests, key=lambda t: (t[1], t[0])):
        if test[1] > budget + 1e-9:
            break
        budget -= test[1]
        kept.append(test)
    return prep, kept


def fitting(
    setup: Setup,
    env: Envelope,
    face_y_mm: float,
    remnant_mm: float,
    tests: list[tuple],
    lane_cap_mm: float | None = None,
) -> int:
    """How many of the tests a lane's face, as it stands, still has room for."""
    return len(_fit(setup, env, face_y_mm, remnant_mm, lane_cap_mm, tests)[1])


def assemble(
    setup: Setup,
    env: Envelope,
    round_no: int,
    planner: PlannerKind,
    face_y_mm: float,
    remnant_mm: float,
    tests: list[tuple],
    *,
    lane: int = 1,
    lane_cap_mm: float | None = None,
) -> Plan:
    """Order tests by increasing width (gradual ramp), prepend face preparation, fit the stock.

    A test is (rpm, width, P(stable)), or in a chip-load sweep (rpm, width, None, chip load),
    already lightest first: equal widths keep their order. The round cuts lane `lane` of the
    setup's lanes, whose face stands at `face_y_mm`.
    """
    lanes = setup.stock.lanes
    prep, kept = _fit(setup, env, face_y_mm, remnant_mm, lane_cap_mm, tests)
    if not kept:
        room = lane_budget_mm(setup, lane_cap_mm)
        where = f" in lane {lane} of {lanes}" if lanes > 1 else ""
        raise PlanError(
            f"stock exhausted: {max(room - face_y_mm, 0.0):.1f} mm left of "
            f"{room:.1f} mm{where}. Mount fresh stock and rerun with --new-stock."
        )

    if setup.machine.spindle is SpindleControl.MANUAL:
        # Every speed change is a stop at the machine: run each speed's cuts together,
        # lightest first, and prepare the face at the first of those speeds.
        kept.sort(key=lambda t: (t[0], t[1]))
        prep = [(kept[0][0], width) for _, width in prep]

    planned = [
        *[(PassKind.CLEANUP, rpm, width) for rpm, width in prep[:-1]],
        (PassKind.TRUING, *prep[-1]),
        *[(PassKind.TEST, *test) for test in kept],
    ]
    passes = []
    face = face_y_mm
    for kind, rpm, width, *rest in planned:
        face += width
        path = travel(setup, lane, lanes, face, width)
        passes.append(make_pass(setup, env, len(passes), kind, rpm, width, *rest, path=path))
    return Plan(
        round=round_no,
        planner=planner,
        flutes=setup.tool.flutes,
        diameter_mm=setup.tool.diameter_mm,
        axial_depth_mm=setup.cut.axial_depth_mm,
        chip_load_mm=env.chip_load_mm,
        face_y_start_mm=round(face_y_mm, 3),
        dwell_s=setup.program.dwell_s,
        plunge_s=round(
            (setup.program.clearance_mm + setup.cut.axial_depth_mm)
            / setup.program.plunge_feed_mm_min
            * 60,
            2,
        ),
        passes=tuple(passes),
        planned_on=date.today().isoformat(),
        lane=lane,
        lanes=lanes,
        lane_cap_mm=None if lane_cap_mm is None else round(lane_cap_mm, 3),
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
