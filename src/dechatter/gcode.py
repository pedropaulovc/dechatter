"""Render a plan as Grbl-dialect G-code (FluidNC, grblHAL, Grbl): G0/G1/G4/M3/M5, and M0."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .config import Coolant, Direction, Setup, SpindleControl
from .plan import PassKind, Plan


class Units(enum.StrEnum):
    """The length mode a machine works in, G21 or G20; a program is written in it."""

    MM = "mm"
    INCH = "in"


MODE = {Units.MM: "G21", Units.INCH: "G20"}
MM_PER_INCH = 25.4

COOLANT_ON = {Coolant.FLOOD: "M8", Coolant.MIST: "M7"}
MANUAL_NOTE = "(MANUAL SPINDLE: the program pauses before each speed. Set it, then resume.)"


def _n(value: float, places: int = 3) -> str:
    return f"{value:.{places}f}".rstrip("0").rstrip(".")


def _length(units: Units):
    """Formats a length or feed given in mm (or mm/min) as a word in `units`."""
    if units is Units.MM:
        return _n
    return lambda mm: _n(mm / MM_PER_INCH, 4)


class Step(enum.StrEnum):
    """What a block of the program does; the widget runs a round one block at a time."""

    SETUP = "setup"  # modal state, spindle off, safe height, coolant
    RAPID = "rapid"  # to the pass's start, beside the stock
    SPINDLE = "spindle"  # the program sets the speed (M3 S); moves nothing
    SET_SPINDLE = "set-spindle"  # manual spindle: the operator sets it (an M0 in the file)
    PLUNGE = "plunge"  # down to depth, in the air beside the stock
    DWELL = "dwell"  # spindle at speed in the air: the reference sound before the cut
    CUT = "cut"
    RETRACT = "retract"  # back off the face, up to the safe height
    END = "end"  # coolant and spindle off


@dataclass(frozen=True)
class Block:
    step: Step
    lines: tuple[str, ...]  # as the program file has them; none for SET_SPINDLE and DWELL
    pass_id: int | None = None
    rpm: int | None = None
    seconds: float | None = None  # DWELL
    # Where the machine stands once the block's moves are done, in mm whatever the program's
    # units (controllers report positions in mm unless set otherwise); only the axes known by
    # then. None for a block that moves nothing.
    target: dict[str, float] | None = None


def blocks(plan: Plan, setup: Setup, units: Units) -> list[Block]:
    """The plan's program as blocks, in the machine's own units."""
    n = _length(units)
    radius = plan.diameter_mm / 2
    clearance = setup.program.clearance_mm
    near, far = -(radius + clearance), setup.stock.length_mm + radius + clearance
    x_start, x_end = (near, far) if setup.cut.direction is Direction.CONVENTIONAL else (far, near)
    manual = setup.machine.spindle is SpindleControl.MANUAL
    at: dict[str, float] = {}

    scale = MM_PER_INCH if units is Units.INCH else 1.0

    def move(**axes: float) -> dict[str, float]:
        # Where the words written send it: rounded as the program has them.
        at.update({axis: round(float(n(value)) * scale, 4) for axis, value in axes.items()})
        return dict(at)

    setup_lines = [f"{MODE[units]} G90 G94 G17", *([] if manual else ["M5"])]
    setup_lines.append(f"G0 Z{n(setup.program.safe_z_mm)}")
    target = move(z=setup.program.safe_z_mm)
    if setup.program.coolant in COOLANT_ON:
        setup_lines.append(COOLANT_ON[setup.program.coolant])
    found = [Block(Step.SETUP, tuple(setup_lines), target=target)]

    face = plan.face_y_start_mm
    spindle_rpm = None
    for p in plan.passes:
        face += p.width_mm
        y_cut = face - radius
        found.append(
            Block(
                Step.RAPID,
                (f"G0 X{n(x_start)} Y{n(y_cut)}",),
                p.id,
                p.rpm,
                target=move(x=x_start, y=y_cut),
            )
        )
        if not manual:
            found.append(Block(Step.SPINDLE, (f"M3 S{p.rpm}",), p.id, p.rpm))
        if manual and p.rpm != spindle_rpm:
            found.append(Block(Step.SET_SPINDLE, (), p.id, p.rpm))
        spindle_rpm = p.rpm
        found += [
            Block(
                Step.PLUNGE,
                (
                    f"G0 Z{n(clearance)}",
                    f"G1 Z{n(-plan.axial_depth_mm)} F{n(setup.program.plunge_feed_mm_min)}",
                ),
                p.id,
                p.rpm,
                target=move(z=-plan.axial_depth_mm),
            ),
            Block(Step.DWELL, (), p.id, p.rpm, seconds=plan.dwell_s),
            Block(
                Step.CUT,
                (f"G1 X{n(x_end)} F{n(p.feed_mm_min)}",),
                p.id,
                p.rpm,
                target=move(x=x_end),
            ),
            Block(
                Step.RETRACT,
                (f"G0 Y{n(y_cut - clearance)}", f"G0 Z{n(setup.program.safe_z_mm)}"),
                p.id,
                p.rpm,
                target=move(y=y_cut - clearance, z=setup.program.safe_z_mm),
            ),
        ]
    found.append(Block(Step.END, ("M9",) if manual else ("M9", "M5")))
    return found


def _file_lines(block: Block, plan: Plan) -> list[str]:
    if block.step is Step.SET_SPINDLE:
        return [f"M0 (set the spindle to {block.rpm} rpm, then resume)"]
    if block.step is Step.DWELL:
        return [f"G4 P{_n(plan.dwell_s)}"]
    return list(block.lines)


def render(plan: Plan, setup: Setup, units: Units) -> str:
    """The plan's program in the machine's own units, so running it never changes them.

    Comments give millimetres either way, as the plan and the results do.
    """
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
        *([] if units is Units.MM else ["(lengths in inches; the comments give mm)"]),
    ]
    passes = {p.id: p for p in plan.passes}
    for block in blocks(plan, setup, units):
        if block.step is Step.RAPID:
            p = passes[block.pass_id]
            label = "test" if p.kind is PassKind.TEST else p.kind.value
            lines.append(
                f"(pass {p.id} {label}: S{p.rpm} width {_n(p.width_mm)} mm F{_n(p.feed_mm_min)})"
            )
        lines += _file_lines(block, plan)
    lines.append("M30")
    return "\n".join(lines) + "\n"
