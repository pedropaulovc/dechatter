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
from .config import Setup, SpindleControl

SCHEMA = 1
SPINDLE_EFFICIENCY = 0.8
SPEED_TOLERANCE_RPM = 0.5
WIDTH_TOLERANCE_MM = 1e-6


class PlanError(RuntimeError):
    pass


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
            raise PlanError(f"{path}: unsupported plan schema {data.get('schema')}")
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


def make_pass(
    setup: Setup,
    env: Envelope,
    pass_id: int,
    kind: PassKind,
    rpm: float,
    width_mm: float,
    p_stable: float | None = None,
    chip_load_mm: float | None = None,
) -> Pass:
    feed = (chip_load_mm or env.chip_load_mm) * setup.tool.flutes * rpm
    # The cutter touches the stock when its centre is this far from the stock end.
    reach = math.sqrt(max(width_mm * (setup.tool.diameter_mm - width_mm), 0.0))
    lead_in = setup.program.clearance_mm + setup.tool.diameter_mm / 2 - reach
    return Pass(
        id=pass_id,
        kind=kind,
        rpm=int(round(rpm)),
        width_mm=round(width_mm, 3),
        feed_mm_min=round(feed, 1),
        expected_cut_s=round((setup.stock.length_mm + 2 * reach) / feed * 60, 2),
        lead_in_s=round(lead_in / feed * 60, 2),
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


def assemble(
    setup: Setup,
    env: Envelope,
    round_no: int,
    planner: PlannerKind,
    face_y_mm: float,
    remnant_mm: float,
    tests: list[tuple],
) -> Plan:
    """Order tests by increasing width (gradual ramp), prepend face preparation, fit the stock.

    A test is (rpm, width, P(stable)), or in a chip-load sweep (rpm, width, None, chip load),
    already lightest first: equal widths keep their order.
    """
    prep = preparation_passes(setup, env, remnant_mm)
    budget = setup.stock.width_mm - face_y_mm - sum(w for _, w in prep)
    ordered = sorted(tests, key=lambda t: (t[1], t[0]))
    kept = []
    for test in ordered:
        if test[1] > budget + 1e-9:
            break
        budget -= test[1]
        kept.append(test)
    if not kept:
        raise PlanError(
            f"stock exhausted: {setup.stock.width_mm - face_y_mm:.1f} mm left of "
            f"{setup.stock.width_mm:.1f} mm. Mount fresh stock and rerun with --new-stock."
        )

    if setup.machine.spindle is SpindleControl.MANUAL:
        # Every speed change is a stop at the machine: run each speed's cuts together,
        # lightest first, and prepare the face at the first of those speeds.
        kept.sort(key=lambda t: (t[0], t[1]))
        prep = [(kept[0][0], width) for _, width in prep]

    passes = []
    for rpm, width in prep[:-1]:
        passes.append(make_pass(setup, env, len(passes), PassKind.CLEANUP, rpm, width))
    passes.append(make_pass(setup, env, len(passes), PassKind.TRUING, *prep[-1]))
    for rpm, width, p_stable, *chip_load in kept:
        passes.append(
            make_pass(setup, env, len(passes), PassKind.TEST, rpm, width, p_stable, *chip_load)
        )
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
