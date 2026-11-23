"""Render a plan as Grbl-dialect G-code (FluidNC, grblHAL, Grbl): G0/G1/G4/M3/M5, and M0."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .config import Coolant, Direction, Setup, SpindleControl
from .plan import PassKind, Plan, facing_rows, lane_x_mm, travel


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
    FACE = "face"  # one row of facing the top down, beside the stock to beside it again
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
    length = setup.stock.length_mm
    climb = setup.cut.direction is Direction.CLIMB

    def x_at(along: float) -> float:
        # Distance along the feed from the end the cutter meets first, as X: climb milling
        # feeds in -X, from the right end.
        return length - along if climb else along

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

    safe_z = setup.program.safe_z_mm
    spindle_rpm = None
    rows = facing_rows(setup, plan.top_mm, plan.reface_mm)
    if rows:
        # The top comes down first, at the round's first speed: no extra spindle stop.
        first = plan.passes[0]
        if manual:
            found.append(Block(Step.SET_SPINDLE, (), None, first.rpm))
        else:
            found.append(Block(Step.SPINDLE, (f"M3 S{first.rpm}",), None, first.rpm))
        spindle_rpm = first.rpm
        feed = plan.chip_load_mm * plan.flutes * first.rpm
        old_top = plan.top_mm - plan.reface_mm
        for row in rows:
            x_start, x_end = x_at(row.start), x_at(row.end)
            found.append(
                Block(
                    Step.FACE,
                    (
                        f"G0 X{n(x_start)} Y{n(row.y_mm)}",
                        f"G0 Z{n(clearance - old_top)}",
                        f"G1 Z{n(-row.z_mm)} F{n(setup.program.plunge_feed_mm_min)}",
                        f"G1 X{n(x_end)} F{n(feed)}",
                        f"G0 Z{n(safe_z)}",
                    ),
                    None,
                    first.rpm,
                    target=move(x=x_end, y=row.y_mm, z=safe_z),
                )
            )

    face = plan.face_y_start_mm
    bottom = -(plan.top_mm + plan.axial_depth_mm)
    for p in plan.passes:
        face += p.width_mm
        y_cut = face - radius
        path = travel(setup, plan.lane, plan.lanes, face, p.width_mm)
        x_start, x_end = x_at(path.start), x_at(path.end)
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
                    f"G0 Z{n(clearance - plan.top_mm)}",
                    f"G1 Z{n(bottom)} F{n(setup.program.plunge_feed_mm_min)}",
                ),
                p.id,
                p.rpm,
                target=move(z=bottom),
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


def _top_lines(plan: Plan) -> list[str]:
    if plan.reface_mm > 0:
        return [
            f"(FACING FIRST: the top comes down {_n(plan.reface_mm)} mm, to Z{_n(-plan.top_mm)}; "
            "keep the zero. The program stops when it is done: start recording then)"
        ]
    if plan.top_mm > 0:
        return [f"(the top stands at Z{_n(-plan.top_mm)}, faced down by earlier rounds)"]
    return []


def _lane_lines(plan: Plan, setup: Setup) -> list[str]:
    if plan.lanes == 1:
        return []
    first, last = lane_x_mm(setup, plan.lane, plan.lanes)
    return [
        f"(lane {plan.lane} of {plan.lanes}: X{_n(first)} to X{_n(last)}; "
        "the lanes before it are cut back, the ones after it untouched)"
    ]


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
        *_lane_lines(plan, setup),
        *_top_lines(plan),
        f"(front face starts this round at Y{_n(plan.face_y_start_mm)}; "
        f"ends at Y{_n(plan.face_y_start_mm + plan.consumed_mm)})",
        "(RECORD: start one continuous recording before cycle start; stop after M30)",
        "(STOP RULE: on violent chatter feed-hold and abort. Later passes are heavier.)",
        MANUAL_NOTE if manual else "(the program starts, sets and stops the spindle)",
        *([] if units is Units.MM else ["(lengths in inches; the comments give mm)"]),
    ]
    passes = {p.id: p for p in plan.passes}
    found = blocks(plan, setup, units)
    last_face = max((i for i, b in enumerate(found) if b.step is Step.FACE), default=None)
    for index, block in enumerate(found):
        if block.step is Step.RAPID:
            p = passes[block.pass_id]
            label = "test" if p.kind is PassKind.TEST else p.kind.value
            lines.append(
                f"(pass {p.id} {label}: S{p.rpm} width {_n(p.width_mm)} mm F{_n(p.feed_mm_min)})"
            )
        lines += _file_lines(block, plan)
        if index == last_face:
            # A recording searched for its cuts by sound must not hold the facing's.
            lines.append("M0 (top faced: start the recording now, then resume)")
    lines.append("M30")
    return "\n".join(lines) + "\n"
