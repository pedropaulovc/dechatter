"""Render a plan as Grbl-dialect G-code (FluidNC, grblHAL, Grbl): G0/G1/G4/M3/M5, and M0."""

from __future__ import annotations

import enum

from .config import Coolant, Direction, Setup, SpindleControl
from .plan import PassKind, Plan


class Units(enum.StrEnum):
    """The length mode a machine was in before a program: G21 or G20."""

    MM = "mm"
    INCH = "in"


PROGRAM_END = "M30"
RESTORE_INCH = "G20 (back to inches, as the machine was before this program)"

COOLANT_ON = {Coolant.FLOOD: "M8", Coolant.MIST: "M7"}
MANUAL_NOTE = "(MANUAL SPINDLE: the program pauses before each speed. Set it, then resume.)"


def _n(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def render(plan: Plan, setup: Setup) -> str:
    radius = plan.diameter_mm / 2
    clearance = setup.program.clearance_mm
    near, far = -(radius + clearance), setup.stock.length_mm + radius + clearance
    x_start, x_end = (near, far) if setup.cut.direction is Direction.CONVENTIONAL else (far, near)
    minutes = plan.run_time_s / 60
    manual = setup.machine.spindle is SpindleControl.MANUAL

    lines = [
        f"(dechatter round {plan.round}: {len(plan.passes)} passes, about {minutes:.0f} min)",
        f"(tool: D{_n(plan.diameter_mm)} mm, {plan.flutes} flutes; "
        f"depth {_n(plan.axial_depth_mm)} mm; {_n(plan.chip_load_mm)} mm per tooth)",
        "(WCS zero: X = left end of stock, Y = front face as first mounted, Z = top)",
        f"(front face starts this round at Y{_n(plan.face_y_start_mm)}; "
        f"ends at Y{_n(plan.face_y_start_mm + plan.consumed_mm)})",
        "(RECORD: start one continuous recording before cycle start; stop after M30)",
        "(STOP RULE: on violent chatter feed-hold and abort. Later passes are heavier.)",
        MANUAL_NOTE if manual else "(the program starts, sets and stops the spindle)",
        "G21 G90 G94 G17",
        *([] if manual else ["M5"]),
        f"G0 Z{_n(setup.program.safe_z_mm)}",
    ]
    if setup.program.coolant in COOLANT_ON:
        lines.append(COOLANT_ON[setup.program.coolant])

    face = plan.face_y_start_mm
    spindle_rpm = None
    for p in plan.passes:
        face += p.width_mm
        y_cut = face - radius
        label = "test" if p.kind is PassKind.TEST else p.kind.value
        lines += [
            f"(pass {p.id} {label}: S{p.rpm} width {_n(p.width_mm)} mm F{_n(p.feed_mm_min)})",
            f"G0 X{_n(x_start)} Y{_n(y_cut)}",
        ]
        if not manual:
            lines.append(f"M3 S{p.rpm}")
        if manual and p.rpm != spindle_rpm:
            lines.append(f"M0 (set the spindle to {p.rpm} rpm, then resume)")
        spindle_rpm = p.rpm
        lines += [
            f"G0 Z{_n(clearance)}",
            f"G1 Z{_n(-plan.axial_depth_mm)} F{_n(setup.program.plunge_feed_mm_min)}",
            f"G4 P{_n(plan.dwell_s)}",
            f"G1 X{_n(x_end)} F{_n(p.feed_mm_min)}",
            f"G0 Y{_n(y_cut - clearance)}",
            f"G0 Z{_n(setup.program.safe_z_mm)}",
        ]

    lines += ["M9", PROGRAM_END] if manual else ["M9", "M5", PROGRAM_END]
    return "\n".join(lines) + "\n"


def for_machine(program: str, units: Units) -> str:
    """A rendered program as a machine working in `units` should run it.

    Programs are written in millimetres and say so (G21), and the controller keeps that
    mode after the program ends. A machine that was working in inches is switched back
    just before the end, so its operator's jogs and offsets still mean inches.
    """
    if units is Units.MM:
        return program
    body, end, tail = program.rpartition(PROGRAM_END)
    if not end:
        return f"{program}{RESTORE_INCH}\n"
    return f"{body}{RESTORE_INCH}\n{end}{tail}"
