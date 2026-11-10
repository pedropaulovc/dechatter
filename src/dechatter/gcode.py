"""Render a plan as Grbl-dialect G-code (FluidNC, grblHAL, Grbl): G0/G1/G4/M3/M5 only."""

from __future__ import annotations

from .config import Coolant, Direction, Setup
from .plan import PassKind, Plan

COOLANT_ON = {Coolant.FLOOD: "M8", Coolant.MIST: "M7"}


def _n(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def render(plan: Plan, setup: Setup) -> str:
    radius = plan.diameter_mm / 2
    clearance = setup.program.clearance_mm
    near, far = -(radius + clearance), setup.stock.length_mm + radius + clearance
    x_start, x_end = (near, far) if setup.cut.direction is Direction.CONVENTIONAL else (far, near)
    minutes = plan.run_time_s / 60

    lines = [
        f"(dechatter round {plan.round}: {len(plan.passes)} passes, about {minutes:.0f} min)",
        f"(tool: D{_n(plan.diameter_mm)} mm, {plan.flutes} flutes; "
        f"depth {_n(plan.axial_depth_mm)} mm; {_n(plan.chip_load_mm)} mm per tooth)",
        "(WCS zero: X = left end of stock, Y = front face as first mounted, Z = top)",
        f"(front face starts this round at Y{_n(plan.face_y_start_mm)}; "
        f"ends at Y{_n(plan.face_y_start_mm + plan.consumed_mm)})",
        "(RECORD: start one continuous recording before cycle start; stop after M30)",
        "(STOP RULE: on violent chatter feed-hold and abort. Later passes are heavier.)",
        "G21 G90 G94 G17",
        "M5",
        f"G0 Z{_n(setup.program.safe_z_mm)}",
    ]
    if setup.program.coolant in COOLANT_ON:
        lines.append(COOLANT_ON[setup.program.coolant])

    face = plan.face_y_start_mm
    for p in plan.passes:
        face += p.width_mm
        y_cut = face - radius
        label = "test" if p.kind is PassKind.TEST else p.kind.value
        lines += [
            f"(pass {p.id} {label}: S{p.rpm} width {_n(p.width_mm)} mm F{_n(p.feed_mm_min)})",
            f"G0 X{_n(x_start)} Y{_n(y_cut)}",
            f"M3 S{p.rpm}",
            f"G0 Z{_n(clearance)}",
            f"G1 Z{_n(-plan.axial_depth_mm)} F{_n(setup.program.plunge_feed_mm_min)}",
            f"G4 P{_n(plan.dwell_s)}",
            f"G1 X{_n(x_end)} F{_n(p.feed_mm_min)}",
            f"G0 Y{_n(y_cut - clearance)}",
            f"G0 Z{_n(setup.program.safe_z_mm)}",
        ]

    lines += ["M9", "M5", "M30"]
    return "\n".join(lines) + "\n"
