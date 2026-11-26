"""Render a plan as Grbl-dialect G-code (FluidNC, grblHAL, Grbl): G0/G1/G4/M3/M5, and M0."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from .config import Coolant, Direction, Setup, SpindleControl, Vary
from .plan import (
    Milling,
    Order,
    Pass,
    PassKind,
    Plan,
    PlannerKind,
    Relief,
    facing_rows,
    lane_x_mm,
    other_direction,
    relief_rows,
    relief_wall_mm,
    travel,
)


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
    RELIEF = "relief"  # one row of a relief between lanes; not analysed, like facing
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
    leg: int | None = None  # the plan's leg; facing and reliefs belong to the leg they serve
    rows_after: int | None = None  # FACE, RELIEF: the rows still to come after this one
    boundary: int | None = None  # RELIEF: the boundary between lanes it is milled at
    # SET_SPINDLE on a machine with speed stops: which stop `rpm` is, 1 the slowest, of how many.
    stop: int | None = None
    stops: int | None = None


def _set_spindle(setup: Setup, rpm: int, pass_id: int | None, leg: int) -> Block:
    """A manual spindle's stop to set the speed, naming the speed stop where `rpm` is one."""
    stops = setup.machine.speed_stops_rpm
    if rpm not in stops:
        return Block(Step.SET_SPINDLE, (), pass_id, rpm, leg=leg)
    return Block(
        Step.SET_SPINDLE, (), pass_id, rpm, leg=leg, stop=stops.index(rpm) + 1, stops=len(stops)
    )


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
    for number, leg in enumerate(plan.legs):
        rows = facing_rows(setup, leg.top_mm, leg.reface_mm)
        reliefs = [(r, relief_rows(setup, plan.lanes, leg.top_mm, r)) for r in leg.reliefs]
        first = leg.passes[0]
        feed = plan.chip_load_mm * plan.flutes * first.rpm
        if rows or any(milled for _, milled in reliefs):
            # The top comes down, and the reliefs are milled, at the leg's first speed: no
            # extra spindle stop.
            if manual and first.rpm != spindle_rpm:
                found.append(_set_spindle(setup, first.rpm, None, number))
            if not manual:
                spindle = (f"M3 S{first.rpm}",)
                found.append(Block(Step.SPINDLE, spindle, None, first.rpm, leg=number))
            spindle_rpm = first.rpm
        old_top = leg.top_mm - leg.reface_mm
        for index, row in enumerate(rows):
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
                    leg=number,
                    rows_after=len(rows) - 1 - index,
                )
            )
        for relief, relief_found in reliefs:
            for index, row in enumerate(relief_found):
                x_start, x_end = x_at(row.start), x_at(row.end)
                # Down through what is milled above, to the clearance over it.
                over = f"G0 Z{n(clearance - row.above_mm)}"
                plunge = f"G1 Z{n(-row.z_mm)} F{n(setup.program.plunge_feed_mm_min)}"
                found.append(
                    Block(
                        Step.RELIEF,
                        (
                            # The row's far end first, where the rows before it left their
                            # end caps' crescents: plunged, then fed into, it cuts its step.
                            f"G0 X{n(x_end)} Y{n(row.y_mm)}",
                            over,
                            plunge,
                            over,
                            f"G0 X{n(x_start)}",
                            plunge,
                            f"G1 X{n(x_end)} F{n(feed)}",
                            f"G0 Z{n(safe_z)}",
                        ),
                        None,
                        first.rpm,
                        target=move(x=x_end, y=row.y_mm, z=safe_z),
                        leg=number,
                        rows_after=len(relief_found) - 1 - index,
                        boundary=relief.boundary,
                    )
                )

        slot = plan.milling is Milling.SLOT
        face = leg.face_y_start_mm
        for p in leg.passes:
            # Side milling: the cutter's far side on the face the pass leaves. A slot: on its
            # far wall, the leg's wall of stock left standing before it.
            face += p.width_mm + leg.wall_mm
            y_cut = face - radius
            bottom = -(leg.top_mm + p.depth_mm)
            path = travel(setup, leg.lane, plan.lanes, face, p.width_mm, plan.lane_end)
            # Off the face it cut, then up; a slot has stock on both sides: straight up.
            y_clear = y_cut if slot else y_cut - clearance
            back_off = () if slot else (f"G0 Y{n(y_clear)}",)
            x_start, x_end = x_at(path.start), x_at(path.end)
            found.append(
                Block(
                    Step.RAPID,
                    (f"G0 X{n(x_start)} Y{n(y_cut)}",),
                    p.id,
                    p.rpm,
                    target=move(x=x_start, y=y_cut),
                    leg=number,
                )
            )
            if not manual:
                found.append(Block(Step.SPINDLE, (f"M3 S{p.rpm}",), p.id, p.rpm, leg=number))
            if manual and p.rpm != spindle_rpm:
                found.append(_set_spindle(setup, p.rpm, p.id, number))
            spindle_rpm = p.rpm
            found += [
                Block(
                    Step.PLUNGE,
                    (
                        f"G0 Z{n(clearance - leg.top_mm)}",
                        f"G1 Z{n(bottom)} F{n(setup.program.plunge_feed_mm_min)}",
                    ),
                    p.id,
                    p.rpm,
                    target=move(z=bottom),
                    leg=number,
                ),
                Block(Step.DWELL, (), p.id, p.rpm, seconds=plan.dwell_s, leg=number),
                Block(
                    Step.CUT,
                    (f"G1 X{n(x_end)} F{n(p.feed_mm_min)}",),
                    p.id,
                    p.rpm,
                    target=move(x=x_end),
                    leg=number,
                ),
                Block(
                    Step.RETRACT,
                    (*back_off, f"G0 Z{n(safe_z)}"),
                    p.id,
                    p.rpm,
                    target=move(y=y_clear, z=safe_z),
                    leg=number,
                ),
            ]
    found.append(Block(Step.END, ("M9",) if manual else ("M9", "M5")))
    return found


def _file_lines(block: Block, plan: Plan) -> list[str]:
    if block.step is Step.SET_SPINDLE and block.stop is not None:
        return [
            f"M0 (set the spindle to {block.rpm} rpm, speed stop {block.stop} of {block.stops} "
            "from the slowest, then resume)"
        ]
    if block.step is Step.SET_SPINDLE:
        return [f"M0 (set the spindle to {block.rpm} rpm, then resume)"]
    if block.step is Step.DWELL:
        return [f"G4 P{_n(plan.dwell_s)}"]
    return list(block.lines)


def _lane(plan: Plan, setup: Setup, lane: int) -> str:
    first, last = lane_x_mm(setup, lane, plan.lanes)
    return f"lane {lane} of {plan.lanes}, X{_n(first)} to X{_n(last)}"


def _relief(plan: Plan, setup: Setup, relief: Relief) -> str:
    """Where a relief is and how far it goes, for the program's comments."""
    at = relief.boundary * setup.stock.length_mm / plan.lanes
    wall = relief_wall_mm(setup)
    xs = [at - wall, at, at + wall]
    if setup.cut.direction is Direction.CLIMB:
        xs = [setup.stock.length_mm - x for x in reversed(xs)]
    lanes = f"lanes {relief.boundary} and {relief.boundary + 1}"
    more = (
        f", extending what was milled to Y{_n(relief.done_y_mm)} and "
        f"{_n(relief.done_depth_mm)} mm deep"
        if relief.done_y_mm > 0 or relief.done_depth_mm > 0
        else ""
    )
    return (
        f"a pocket between {lanes} at X{_n(xs[1])}, X{_n(xs[0])} to X{_n(xs[2])}, back to "
        f"Y{_n(relief.y_mm)} and {_n(relief.depth_mm)} mm deep{more}"
    )


def _choice_lines(plan: Plan, setup: Setup) -> list[str]:
    """What the header says of where the round starts, when it chose the lane."""
    if not plan.lane_stock:
        return []
    lane = plan.legs[0].lane
    chosen = next(s for s in plan.lane_stock if s.lane == lane)
    others = "; ".join(
        f"lane {s.lane}: {_n(s.left_mm, 1)} mm{'' if s.open else ', no room for the first cut'}"
        for s in plan.lane_stock
        if s.lane != lane
    )
    return [
        f"(STARTS IN {_lane(plan, setup, lane)}: of the lanes its first cut fits on, the one "
        f"with the most stock left behind its face, {_n(chosen.left_mm, 1)} mm; {others})"
    ]


def _leg_lines(plan: Plan, setup: Setup) -> list[str]:
    """The header's account of where the round cuts, leg by leg."""
    lines = []
    for number, leg in enumerate(plan.legs):
        then = "then " if number else ""
        if leg.reface_mm > 0 and number == 0:
            lines.append(
                f"(FACING FIRST: the top comes down {_n(leg.reface_mm)} mm, to "
                f"Z{_n(-leg.top_mm)}; keep the zero)"
            )
        if leg.reface_mm > 0 and number > 0:
            lines.append(
                f"(then FACING: the top comes down {_n(leg.reface_mm)} mm, to "
                f"Z{_n(-leg.top_mm)}, and the round starts again at the first lane; keep the "
                "zero)"
            )
        if leg.reface_mm == 0 and number == 0 and leg.top_mm > 0:
            lines.append(f"(the top stands at Z{_n(-leg.top_mm)}, faced down by earlier rounds)")
        for relief in leg.reliefs:
            lines.append(
                f"({then}RELIEF: {_relief(plan, setup, relief)}, milled in rows like facing; "
                "the passes end and start in it, in the air; not analysed)"
            )
        faces = f"front face from Y{_n(leg.face_y_start_mm)} to Y{_n(leg.face_y_end_mm)}"
        if plan.milling is Milling.SLOT:
            faces = f"slots in the top from Y{_n(leg.face_y_start_mm)} to Y{_n(leg.face_y_end_mm)}"
        if plan.lanes == 1:
            lines.append(f"({then}{faces})")
            continue
        lines.append(f"({then}{_lane(plan, setup, leg.lane)}: {faces})")
    return lines


def _leg_start(plan: Plan, setup: Setup, number: int) -> list[str]:
    """What the program says where a leg after the first begins."""
    leg = plan.legs[number]
    slot = plan.milling is Milling.SLOT
    where = (
        ("the top" if slot else "the front face")
        if plan.lanes == 1
        else _lane(plan, setup, leg.lane)
    )
    if leg.reface_mm > 0:
        return [
            f"(FACING THE TOP DOWN from here: {_n(leg.reface_mm)} mm, to Z{_n(-leg.top_mm)}; "
            f"then {where} from Y0 again, below the new top; keep the zero)"
        ]
    fresh = "slots from Y0 again" if slot else "a fresh face at Y0"
    if not slot and leg.face_y_start_mm > 0:
        fresh = f"its face at Y{_n(leg.face_y_start_mm)}"
    return [f"(LANE CHANGE: {where} from here, {fresh}; keep the zero)"]


def _held(plan: Plan) -> str:
    """The dimension every test cut of the round shares, for the header."""
    if plan.vary is Vary.WIDTH:
        return f"depth {_n(plan.fixed_mm)} mm"
    slots = ", full-width slots" if plan.milling is Milling.SLOT else ""
    return f"width {_n(plan.fixed_mm)} mm{slots}, the depth stepped"


def _size(plan: Plan, p: Pass) -> str:
    if plan.vary is Vary.WIDTH:
        return f"width {_n(p.width_mm)} mm"
    return f"width {_n(p.width_mm)} mm depth {_n(p.depth_mm)} mm"


def _stop_rule(plan: Plan) -> str:
    if plan.order is Order.DEEPEST_FIRST:
        return (
            "(STOP RULE: on violent chatter feed-hold and abort. The deepest passes come "
            "first; later ones are lighter.)"
        )
    return "(STOP RULE: on violent chatter feed-hold and abort. Later passes are heavier.)"


def _direction_lines(plan: Plan, setup: Setup) -> list[str]:
    """What the header says of the direction the passes feed in (the setup's, as `blocks`
    follows it): a check's, and climb milling's demand."""
    lines = []
    direction = setup.cut.direction
    feed = "-X from the right end" if direction is Direction.CLIMB else "+X from the left end"
    if plan.planner is PlannerKind.CHECK:
        lines.append(
            f"({direction.upper()} CHECK: every pass cuts {direction}, feeding {feed}, the "
            f"other way to the session's {other_direction(direction)} rounds; its verdicts "
            "stay off the stability map)"
        )
    if direction is Direction.CLIMB:
        lines.append("(CLIMB MILLING: needs a machine without backlash, such as ball screws)")
    return lines


def render(plan: Plan, setup: Setup, units: Units) -> str:
    """The plan's program in the machine's own units, so running it never changes them.

    Comments give millimetres either way, as the plan and the results do.
    """
    minutes = plan.run_time_s / 60
    manual = setup.machine.spindle is SpindleControl.MANUAL
    lines = [
        f"(dechatter round {plan.round}: {len(plan.passes)} passes, about {minutes:.0f} min)",
        f"(tool: D{_n(plan.diameter_mm)} mm, {plan.flutes} flutes; {_held(plan)}; "
        f"{_n(plan.chip_load_mm)} mm per tooth)",
        "(WCS zero: X = left end of stock, Y = front face as first mounted, Z = top)",
        *_direction_lines(plan, setup),
        *_choice_lines(plan, setup),
        *_leg_lines(plan, setup),
        _stop_rule(plan),
        MANUAL_NOTE if manual else "(the program starts, sets and stops the spindle)",
        *([] if units is Units.MM else ["(lengths in inches; the comments give mm)"]),
    ]
    passes = {p.id: p for p in plan.passes}
    found = blocks(plan, setup, units)
    leg, relief = 0, None
    for block in found:
        if block.leg is not None and block.leg != leg:
            leg, relief = block.leg, None
            lines += _leg_start(plan, setup, leg)
        if block.step is Step.RELIEF and block.boundary != relief:
            relief = block.boundary
            milled = next(r for r in plan.legs[leg].reliefs if r.boundary == relief)
            lines.append(f"(RELIEF: {_relief(plan, setup, milled)}; not analysed)")
        if block.step is Step.RAPID:
            p = passes[block.pass_id]
            label = "test" if p.kind is PassKind.TEST else p.kind.value
            lines.append(f"(pass {p.id} {label}: S{p.rpm} {_size(plan, p)} F{_n(p.feed_mm_min)})")
        lines += _file_lines(block, plan)
    lines.append("M30")
    return "\n".join(lines) + "\n"
