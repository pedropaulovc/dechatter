from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from dechatter import bayes, gcode
from dechatter.bayes import Observation
from dechatter.config import ConfigError, Direction, load_setup, parse_setup, relief_wall_mm
from dechatter.detect import Verdict
from dechatter.plan import (
    REFACE_EXTRA_MM,
    SCHEMA,
    SWEEP_PASSES,
    Face,
    Fit,
    LaneEnd,
    Made,
    Milled,
    PassKind,
    Plan,
    PlanError,
    PlannerKind,
    Reach,
    Start,
    Top,
    assemble,
    auto_lanes,
    check_tests,
    cut_order,
    cutting,
    envelope,
    facing_rows,
    grid_tests,
    lane_face,
    lane_x_mm,
    mrr_mm3_min,
    relief_rows,
    sweep_tests,
)

MM = gcode.Units.MM


def round1(setup, face=0.0, remnant=0.0):
    env = envelope(setup)
    start = Face(y_mm=face, remnant_mm=remnant)
    return assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env), face=start)


def test_round1_ramps_width_and_holds_chip_load(setup):
    plan = round1(setup)
    tests = [p for p in plan.passes if p.kind is PassKind.TEST]
    widths = [p.width_mm for p in tests]
    assert widths == sorted(widths)
    assert plan.passes[0].kind is PassKind.TRUING
    for p in tests:
        assert p.feed_mm_min / (p.rpm * plan.flutes) == pytest.approx(plan.chip_load_mm, rel=1e-3)


def test_surface_speed_limit_caps_spindle_speed(write_setup):
    steel = load_setup(write_setup(m=('material = "aluminum-6061"', 'material = "stainless-304"')))
    # 60 m/min on a 9.525 mm cutter is 2005 rpm, well under the machine's 5000.
    assert envelope(steel).speed_max_rpm == 2000


def test_power_cap_excludes_heavy_cuts(write_setup):
    weak = load_setup(write_setup(p=("spindle_power_hp = 2.0", "spindle_power_hp = 0.08")))
    env = envelope(weak)
    tests = grid_tests(weak, env)
    full = load_setup(write_setup())
    assert 0 < len(tests) < len(grid_tests(full, envelope(full)))
    limit = env.mrr_limit_mm3_min
    assert all(mrr_mm3_min(weak, env, rpm, width) <= limit for rpm, width, _ in tests)


def test_plan_stops_at_the_stock_it_has(setup):
    plan = round1(setup, face=20.0)
    assert plan.legs[-1].face_y_end_mm <= setup.stock.width_mm
    assert len(plan.passes) < len(round1(setup).passes)
    with pytest.raises(PlanError, match="stock exhausted"):
        round1(setup, face=29.9)


def test_aborted_pass_is_cleared_in_light_steps_before_truing(setup):
    plan = round1(setup, face=5.0, remnant=2.5)
    cleanup = [p for p in plan.passes if p.kind is PassKind.CLEANUP]
    assert sum(p.width_mm for p in cleanup) == pytest.approx(2.5, abs=0.01)
    assert max(p.width_mm for p in cleanup) <= 0.1 * setup.tool.diameter_mm + 1e-9
    kinds = [p.kind for p in plan.passes]
    assert kinds.index(PassKind.TRUING) == len(cleanup)


def cutting_moves(program: str) -> list[tuple[float, float, float]]:
    """(y, x_end, feed) of each pass: the positioning move, then the first G1 in X after it."""
    moves, y = [], None
    for line in program.splitlines():
        if m := re.fullmatch(r"G0 X(\S+) Y(\S+)", line):
            y = float(m[2])
        if m := re.fullmatch(r"G1 X(\S+) F(\S+)", line):
            moves.append((y, float(m[1]), float(m[2])))
    return moves


def test_gcode_steps_the_cutter_into_the_face_by_each_pass_width(setup):
    plan = round1(setup, face=4.0)
    moves = cutting_moves(gcode.render(plan, setup, MM))
    assert len(moves) == len(plan.passes)
    face = plan.legs[0].face_y_start_mm
    for (y, x_end, feed), p in zip(moves, plan.passes, strict=True):
        face += p.width_mm
        assert y == pytest.approx(face - plan.diameter_mm / 2, abs=1e-3)
        assert feed == pytest.approx(p.feed_mm_min)
        assert x_end > setup.stock.length_mm  # conventional: tool in front, travelling +X


def test_climb_reverses_travel(write_setup):
    climb = load_setup(write_setup(d=('direction = "conventional"', 'direction = "climb"')))
    assert all(x_end < 0 for _, x_end, _ in cutting_moves(gcode.render(round1(climb), climb, MM)))


def test_dwell_precedes_every_cut(setup):
    program = gcode.render(round1(setup), setup, MM).splitlines()
    cuts = [i for i, line in enumerate(program) if line.startswith("G1 X")]
    assert all(program[i - 1] == "G4 P4" for i in cuts)


MANUAL = {"s": ('spindle = "program"', 'spindle = "manual"')}


def test_an_inch_machine_gets_the_same_program_in_inches(setup):
    plan = round1(setup, face=4.0)
    mm = gcode.render(plan, setup, MM).splitlines()
    inch = gcode.render(plan, setup, gcode.Units.INCH).splitlines()

    assert [line for line in mm if re.match(r"G2[01]\b", line)] == ["G21 G90 G94 G17"]
    # The program never leaves inches: its machine stays in them however the job ends.
    assert [line for line in inch if re.match(r"G2[01]\b", line)] == ["G20 G90 G94 G17"]
    assert all(len(f) <= 4 for line in inch for f in re.findall(r"\.(\d+)", line.split("(")[0]))
    # Four decimals of an inch: within 0.0013 mm, plus the mm program's own rounding.
    in_mm = [v * 25.4 for move in cutting_moves("\n".join(inch)) for v in move]
    assert in_mm == pytest.approx(
        [v for move in cutting_moves("\n".join(mm)) for v in move], abs=0.002
    )
    # Dwell seconds, spindle speeds and the mm in the comments are the same.
    for start in ("G4", "M3", "(pass", "(dechatter", "(front face"):
        assert [x for x in inch if x.startswith(start)] == [x for x in mm if x.startswith(start)]
    plunge = next(line for line in inch if line.startswith("G1 Z"))
    depth, feed = re.fullmatch(r"G1 Z(\S+) F(\S+)", plunge).groups()
    assert float(depth) * 25.4 == pytest.approx(-plan.fixed_mm, abs=0.002)
    assert float(feed) * 25.4 == pytest.approx(setup.program.plunge_feed_mm_min, abs=0.002)


def test_manual_spindle_pauses_once_per_speed_and_never_commands_the_spindle(write_setup):
    manual = load_setup(write_setup(**MANUAL))
    plan = round1(manual)
    program = gcode.render(plan, manual, MM).splitlines()

    assert not [line for line in program if re.match(r"M[345]\b|S\d", line)]
    asked = [
        int(re.search(r"(\d+) rpm", line).group(1)) for line in program if line.startswith("M0")
    ]
    rpms = [p.rpm for p in plan.passes]
    assert asked == sorted(set(rpms)), "one stop per speed, speeds grouped"
    # Each stop comes with the cutter clear of the work: after the retract, before the plunge.
    for i, line in enumerate(program):
        if line.startswith("M0"):
            assert program[i - 1].startswith("G0 X") and program[i + 1].startswith("G0 Z")
    # Within a speed the cuts still get heavier, and the truing pass needs no speed of its own.
    for rpm in set(rpms):
        widths = [p.width_mm for p in plan.passes if p.rpm == rpm and p.kind is PassKind.TEST]
        assert widths == sorted(widths)
    assert plan.passes[0].kind is PassKind.TRUING and plan.passes[0].rpm == plan.passes[1].rpm


def test_manual_spindle_tests_the_same_cuts_as_program_control(setup, write_setup):
    cuts = lambda plan: sorted((p.rpm, p.width_mm) for p in plan.passes if p.kind is PassKind.TEST)  # noqa: E731
    assert cuts(round1(load_setup(write_setup(**MANUAL)))) == cuts(round1(setup))


# A dial that rests on these speeds only; 1000 rpm lies below the template's search.
STOPS = (1000, 1513, 1839, 1950, 2275, 2380, 2482, 2590, 2692, 2795, 4000)


def with_stops(write_setup, stops=STOPS, spindle="manual"):
    listed = ", ".join(str(stop) for stop in stops)
    return write_setup(
        s=('spindle = "program"', f'spindle = "{spindle}"'),
        t=("weight_kg = 250", f"weight_kg = 250\nspeed_stops_rpm = [{listed}]"),
    )


def test_speed_stops_are_the_only_speeds_planned_and_each_stop_is_named(write_setup):
    setup = load_setup(with_stops(write_setup))
    env = envelope(setup)
    assert env.speed_stops_rpm == STOPS[1:], "only the stops inside the search"
    assert set(STOPS[1:]) <= set(env.speeds(setup.planner.speed_step_rpm))

    plan = round1(setup)
    tested = sorted({p.rpm for p in plan.passes if p.kind is PassKind.TEST})
    assert {p.rpm for p in plan.passes} <= set(STOPS[1:])
    assert len(tested) == setup.planner.round1_speeds
    assert (tested[0], tested[-1]) == (1513, 4000), "spread from the slowest to the fastest"

    seen = [
        Observation(1950.0, 1.0, Verdict.STABLE),
        Observation(2380.0, 2.5, Verdict.UNSTABLE, 950.0),
        Observation(2477.0, 1.2, Verdict.STABLE, asked_rpm=2482.0),
    ]
    proposal = bayes.propose(setup, env, seen)
    assert proposal.tests
    assert {rpm for rpm, _, _ in proposal.tests} <= set(env.speed_stops_rpm)

    asks = [b for b in gcode.blocks(plan, setup, MM) if b.step is gcode.Step.SET_SPINDLE]
    assert [(b.rpm, b.stop, b.stops) for b in asks] == [
        (rpm, STOPS.index(rpm) + 1, len(STOPS)) for rpm in tested
    ]
    program = gcode.render(plan, setup, MM).splitlines()
    assert [line for line in program if line.startswith("M0")][0] == (
        "M0 (set the spindle to 1513 rpm, speed stop 2 of 11 from the slowest, then resume)"
    )

    narrowed = dataclasses.replace(setup, cut=dataclasses.replace(setup.cut, speed_min_rpm=3000))
    assert envelope(narrowed).speed_stops_rpm == (4000,)
    with pytest.raises(PlanError, match="no speed stop"):
        envelope(load_setup(with_stops(write_setup, (600, 1000))))

    # The automatic lanes leave every cut long enough at the fastest stop, not at the top
    # speed of the search, which no cut is planned at, besides the reliefs at a lane's ends.
    long = {"l": ("length_mm = 60.0", "length_mm = 600.0")}
    free = load_setup(write_setup(**MANUAL, **long))
    slow = dataclasses.replace(
        setup,
        stock=free.stock,
        machine=dataclasses.replace(setup.machine, speed_stops_rpm=(1513, 1839)),
    )
    cut = envelope(slow).chip_load_mm * slow.tool.flutes * 1839 / 60 * 6.0
    lanes = auto_lanes(slow, envelope(slow))
    assert 600.0 / lanes - 2 * relief_wall_mm(slow) >= cut
    assert 600.0 / (lanes + 1) - 2 * relief_wall_mm(slow) < cut
    assert lanes > auto_lanes(free, envelope(free))


@pytest.mark.parametrize(
    ("stops", "spindle", "message"),
    [
        ((1500, 2000), "program", 'only with spindle = "manual"'),
        ((2000, 1500), "manual", "slowest first"),
        ((1500, 1500), "manual", "each once"),
        ((1500.5,), "manual", "whole rpm"),
        ((400, 1500), "manual", r"within min_rpm and max_rpm \(500\.\.5000 rpm\)"),
        ((1500, 5100), "manual", "within min_rpm and max_rpm"),
    ],
)
def test_speed_stops_are_refused_unless_whole_sorted_in_range_and_set_by_hand(
    write_setup, stops, spindle, message
):
    with pytest.raises(ConfigError, match=message):
        load_setup(with_stops(write_setup, stops, spindle))


def test_unknown_setup_key_is_rejected(write_setup):
    with pytest.raises(ConfigError, match="flute_count"):
        load_setup(write_setup(k=("flutes = 3", "flute_count = 3")))


def test_spindle_power_is_given_in_hp_and_the_old_watts_key_is_refused(write_setup):
    half = load_setup(write_setup(p=("spindle_power_hp = 2.0", "spindle_power_hp = 1.0")))
    assert half.machine.spindle_power_w == pytest.approx(745.7)
    assert envelope(half).mrr_limit_mm3_min == pytest.approx(
        envelope(load_setup(write_setup())).mrr_limit_mm3_min / 2
    )
    with pytest.raises(ConfigError, match="spindle_power_w is now spindle_power_hp"):
        load_setup(write_setup(p=("spindle_power_hp = 2.0", "spindle_power_w = 1500")))


def test_a_chip_load_sweep_steps_lightest_first_inside_the_power_cap(setup, write_setup):
    env = envelope(setup)
    tests = sweep_tests(setup, env, 4000, 2.0)
    loads = [t[3] for t in tests]
    assert len(tests) == SWEEP_PASSES and loads == sorted(loads)
    assert loads[0] == pytest.approx(0.6 * env.chip_load_mm, abs=1e-4)
    assert loads[-1] == pytest.approx(1.7 * env.chip_load_mm, abs=1e-4)
    plan = assemble(setup, env, 2, PlannerKind.SWEEP, tests)
    swept = [p for p in plan.passes if p.kind is PassKind.TEST]
    assert [p.chip_load_mm for p in swept] == loads
    assert [p.feed_mm_min for p in swept] == [
        pytest.approx(load * setup.tool.flutes * 4000, abs=0.1) for load in loads
    ]

    weak = load_setup(write_setup(p=("spindle_power_hp = 2.0", "spindle_power_hp = 0.3")))
    capped = sweep_tests(weak, envelope(weak), 4000, 2.0)
    assert 0 < len(capped) < SWEEP_PASSES
    with pytest.raises(ConfigError, match="chip_load_sweep_low < chip_load_sweep_high"):
        load_setup(write_setup(s=("chip_load_sweep_high = 1.7", "chip_load_sweep_high = 0.5")))


def test_a_check_ramps_up_to_the_best_cut_and_a_step_beyond(setup, write_setup):
    env = envelope(setup)
    tests = check_tests(setup, env, 4000.0, 2.0)
    assert tests == [
        (4000.0, 1.0, None),
        (4000.0, 1.5, None),
        (4000.0, 2.0, None),
        (4000.0, 2.1, None),
    ]

    # The v6 record's ruling: 1.8, 2.0 and 2.2 mm, and nothing beyond the record.
    record = load_setup(
        write_setup(
            s=(
                "chip_load_sweep_high = 1.7",
                "chip_load_sweep_high = 1.7\ncheck_fractions = [0.8, 0.9, 1.0]\n"
                "check_steps_beyond = 0",
            )
        )
    )
    assert [t[1] for t in check_tests(record, envelope(record), 2400.0, 2.2)] == [1.8, 2.0, 2.2]

    # No step beyond the widest width searched, nor over the power cap.
    top = env.engagement_top_mm
    assert [t[1] for t in check_tests(setup, env, 4000.0, top)][-1] == top
    # 0.27 hp caps 4000 rpm between the 2 mm cut and the step beyond it.
    weak = load_setup(write_setup(p=("spindle_power_hp = 2.0", "spindle_power_hp = 0.27")))
    weak_env = envelope(weak)
    assert [t[1] for t in check_tests(setup, env, 4000.0, 2.0)] == [1.0, 1.5, 2.0, 2.1]
    assert [t[1] for t in check_tests(weak, weak_env, 4000.0, 2.0)] == [1.0, 1.5, 2.0]
    for bad in ("check_fractions = [0, 1]", "check_fractions = [1.2]", "check_steps_beyond = -1"):
        with pytest.raises(ConfigError, match="check_"):
            load_setup(
                write_setup(s=("chip_load_sweep_high = 1.7", f"chip_load_sweep_high = 1.7\n{bad}"))
            )


def test_a_check_cuts_the_other_way_on_the_lanes_from_the_other_end(write_setup):
    setup = load_setup(write_setup(**{"l": ("width_mm = 30.0", "width_mm = 30.0\nlanes = 3")}))
    env = envelope(setup)
    climb = cutting(setup, Direction.CLIMB)
    assert (
        climb.cut.direction is Direction.CLIMB and cutting(setup, Direction.CONVENTIONAL) is setup
    )
    tests = check_tests(setup, env, 4000.0, 2.0)
    # Conventional rounds used lane 1 (the left): climbing, it is lane 3. Lanes 1 and 2 have
    # the most stock left: the check starts in lane 1, the right-hand one.
    made = (Made(3, 6.0, setup.cut.axial_depth_mm),)
    plan = assemble(climb, env, 5, PlannerKind.CHECK, tests, made=made, lanes=3, pick=True)
    assert plan.direction is Direction.CLIMB and plan.start is Start.IN_USE
    assert [leg.lane for leg in plan.legs] == [1]
    assert [s.left_mm for s in plan.legs[0].lane_stock] == [30.0, 30.0, 24.0]
    assert lane_x_mm(climb, 1, 3) == (40.0, 60.0)
    # Its passes run in from the right end and end in the relief at X40, climbing.
    assert {x_end for _, _, x_end in sweeps(plan, climb)} == {40.0}
    assert [r.boundary for r in plan.legs[0].reliefs] == [1]
    assert "CLIMB CHECK" in gcode.render(plan, climb, MM)

    # The lanes it may use all cut: with no height to face the top down, fresh stock.
    with pytest.raises(PlanError, match="cuts climb, using the lanes from the end"):
        assemble(climb, env, 5, PlannerKind.CHECK, tests, made=made, lanes=3, lanes_open=0)
    # The check must fit whole, as the chip-load sweep must: it may span lanes, but with
    # 1.5 mm left in each, its 1.5 mm cut fits on none.
    narrow = tuple(Made(lane, setup.stock.width_mm - 1.5, 5.0) for lane in (1, 2, 3))
    with pytest.raises(PlanError, match="climb check needs room for all its 4 cuts"):
        assemble(climb, env, 5, PlannerKind.CHECK, tests, made=narrow, lanes=3, pick=True)


def test_a_setup_from_dechatter_0_1_is_refused_with_what_to_do():
    old = (Path(__file__).parent / "fixtures" / "setup-0.1.1.toml").read_text()
    with pytest.raises(ConfigError, match=r"from dechatter 0\.1\.x.*start a new session"):
        parse_setup(old, "setup.toml")


def test_the_widget_runs_the_program_as_blocks_with_targets_in_mm(setup, write_setup):
    plan = round1(setup, face=4.0)
    blocks = {}
    for units in gcode.Units:
        blocks[units] = gcode.blocks(plan, setup, units)
        program = gcode.render(plan, setup, units).splitlines()
        # The blocks send exactly the program's own lines, but for its comments, its dwells
        # (the widget times them) and the end of program.
        own = [line for line in program if not line.startswith(("(", "G4", "M0")) and line != "M30"]
        assert [line for b in blocks[units] for line in b.lines] == own
    steps = [b.step for b in blocks[MM]]
    assert steps.count(gcode.Step.CUT) == steps.count(gcode.Step.DWELL) == len(plan.passes)
    assert {b.seconds for b in blocks[MM] if b.step is gcode.Step.DWELL} == {plan.dwell_s}
    # Positions to wait for are in mm whatever the program's units.
    for mm, inch in zip(blocks[MM], blocks[gcode.Units.INCH], strict=True):
        assert (mm.target is None) == (inch.target is None)
        for axis, value in (mm.target or {}).items():
            assert inch.target[axis] == pytest.approx(value, abs=0.003)
    cut = next(b for b in blocks[MM] if b.step is gcode.Step.CUT)
    assert cut.target["x"] > setup.stock.length_mm and cut.target["z"] == -plan.fixed_mm

    manual = load_setup(write_setup(**MANUAL))
    manual_blocks = gcode.blocks(round1(manual), manual, MM)
    asks = [b for b in manual_blocks if b.step is gcode.Step.SET_SPINDLE]
    assert [b.rpm for b in asks] == sorted({p.rpm for p in round1(manual).passes})
    assert all(b.lines == () and b.target is None for b in asks)
    assert not [line for b in manual_blocks for line in b.lines if line.startswith(("M0", "M3"))]


LANES = {"l": ("width_mm = 30.0", "width_mm = 30.0\nlanes = 2")}


def lane_setup(write_setup, **more):
    return load_setup(write_setup(**LANES, **more))


def sweeps(plan, setup):
    """(y of the cutter centre, x where the plunge is, x where the feed move ends) per pass."""
    found = []
    for block in gcode.blocks(plan, setup, MM):
        if block.step is gcode.Step.RAPID:
            x0, y = block.target["x"], block.target["y"]
        if block.step is gcode.Step.CUT:
            found.append((y, x0, block.target["x"]))
    return found


def removed(stock, sweeps, radius):
    """Carve the passes out of `stock`, a set of (x, y) points of material; return, per pass,
    the points it cut and whether its plunge point was in the air."""
    cuts = []
    for y_c, x_from, x_to in sweeps:
        lo, hi = sorted((x_from, x_to))
        plunge_clear = not any((x - x_from) ** 2 + (y - y_c) ** 2 < radius**2 for x, y in stock)
        cut = {
            (x, y)
            for x, y in stock
            if abs(y - y_c) < radius
            and lo - (radius**2 - (y - y_c) ** 2) ** 0.5
            < x
            < hi + (radius**2 - (y - y_c) ** 2) ** 0.5
        }
        stock -= cut
        cuts.append((cut, plunge_clear))
    return cuts


def grid(length, width, step=0.25, y_step=0.1):
    """Points of material, at the middle of each cell so a narrow pass still finds some."""
    return {
        ((i + 0.5) * step, (j + 0.5) * y_step)
        for i in range(int(length / step))
        for j in range(int(width / y_step))
    }


def relief_sweeps(plan, setup):
    """(y of the cutter centre, x where the row's feed starts, x where it ends) per relief
    row; each row plunges at its end, then at its start, then feeds from one to the other,
    and goes up to the safe height in a block of its own."""
    found = []
    for block in gcode.blocks(plan, setup, MM):
        if block.step is gcode.Step.RELIEF and block.lines != (f"G0 Z{setup.program.safe_z_mm:g}",):
            ends = [line for line in block.lines if line.startswith("G0 X")]
            assert len(ends) == 2 and ends[0].endswith(f"Y{block.target['y']:g}")
            start = float(ends[1].split()[1][1:])
            found.append((block.target["y"], start, block.target["x"]))
    return found


@pytest.mark.parametrize("direction", ["conventional", "climb"])
def test_a_lane_is_cut_without_touching_the_lanes_on_either_side(write_setup, direction):
    turn = {"d": ('direction = "conventional"', f'direction = "{direction}"')}
    setup = lane_setup(write_setup, **turn)
    env = envelope(setup)
    radius = setup.tool.diameter_mm / 2
    wall = relief_wall_mm(setup)
    length = setup.stock.length_mm
    half = length / 2
    stock = grid(length, setup.stock.width_mm)

    def first_lane(x):  # the lane the cutter meets first: left in conventional milling
        return x < half if direction == "conventional" else x > half

    def relief(x):  # the pocket milled where the lanes meet
        return abs(x - half) <= wall

    one = round1(setup)
    assert [leg.lane for leg in one.legs] == [1] and one.lanes == 2
    # The relief first, within its pocket; then the passes, which end in it.
    (pocket,) = one.legs[0].reliefs
    assert (pocket.boundary, pocket.y_mm) == (1, one.legs[0].face_y_end_mm)
    for cut, _ in removed(stock, relief_sweeps(one, setup), radius):
        assert all(relief(x) for x, _ in cut)
    for cut, plunge_clear in removed(stock, sweeps(one, setup), radius):
        assert plunge_clear
        assert cut and all(first_lane(x) for x, _ in cut)
    # The second lane may go further back than the first: its passes start in the relief,
    # which is milled further back first, and never cross the first lane.
    made = tuple(Made(1, y, p.depth_mm) for y, p in faces(one))
    tests = grid_tests(setup, env)
    two = assemble(
        setup,
        env,
        2,
        PlannerKind.GRID,
        tests + tests[-4:],
        face=Face(lane=2),
        made=made,
        reliefs=(pocket,),
    )
    assert two.legs[-1].face_y_end_mm > pocket.y_mm
    (extended,) = two.legs[0].reliefs
    assert (extended.done, extended.y_mm) == (
        (Milled(pocket.y_mm, pocket.depth_mm),),
        two.legs[-1].face_y_end_mm,
    )
    for cut, _ in removed(stock, relief_sweeps(two, setup), radius):
        assert all(relief(x) for x, _ in cut)
    for cut, plunge_clear in removed(stock, sweeps(two, setup), radius):
        assert plunge_clear
        assert cut and all(not first_lane(x) for x, _ in cut)
    # The last lane runs out past the end of the stock.
    end = sweeps(two, setup)[-1][2]
    assert (end > length) if direction == "conventional" else (end < 0)
    assert "lane 2 of 2" in gcode.render(two, setup, MM)


def faces(plan):
    """(the face each pass leaves, the pass)."""
    found, y = [], plan.legs[0].face_y_start_mm
    for p in plan.passes:
        y += p.width_mm
        found.append((y, p))
    return found


def test_lanes_count_the_cut_and_lead_in_of_the_lane_alone(write_setup, setup):
    lanes = lane_setup(write_setup)
    whole, first = round1(setup).passes[0], round1(lanes).passes[0]
    assert first.expected_cut_s < whole.expected_cut_s / 2 + 1.0
    assert first.lead_in_s == whole.lead_in_s


def test_a_side_milling_round_starts_in_the_lane_with_the_most_stock_left(write_setup):
    setup = lane_setup(write_setup)
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    # Lane 1 cut back 3 mm, lane 2 untouched: lane 2, which may go further back than lane 1.
    plan = assemble(setup, env, 2, PlannerKind.GRID, tests, made=(Made(1, 3.0, depth),), pick=True)
    assert plan.legs[0].lane == 2 and plan.legs[0].face_y_start_mm == 0.0
    assert plan.legs[0].face_y_end_mm > 3.0 and plan.legs[0].lane_cap_mm is None
    assert [(s.lane, s.left_mm, s.fit) for s in plan.legs[0].lane_stock] == [
        (1, 27.0, Fit.FITS),
        (2, 30.0, Fit.FITS),
    ]
    # Lane 2 cut back further than lane 1, and a pass stopped part-way there: lane 1, and
    # where lane 2 is the choice, the step the stopped pass left is cleared first.
    made = (Made(1, 3.0, depth), Made(2, 5.0, depth), Made(2, 6.0, depth, reach=Reach.PART))
    plan = assemble(setup, env, 3, PlannerKind.GRID, tests, made=made, pick=True)
    assert plan.legs[0].lane == 1 and plan.legs[0].face_y_start_mm == 3.0
    assert lane_face(2, list(made), Face()) == Face(2, 5.0, 1.0)
    # No lane with room for the first cut: every lane's stock in the reason.
    full = (Made(1, 29.9, depth), Made(2, 29.95, depth))
    with pytest.raises(PlanError, match=r"no lane has room .*lane 1: 0\.1 mm; lane 2: 0\.1 mm"):
        assemble(setup, env, 3, PlannerKind.GRID, tests, made=full, pick=True)


def test_a_round_that_fills_its_lane_goes_on_in_the_lane_with_the_most_stock_left(write_setup):
    """Started in lane 2 of 2, with more tests than it holds: the round goes on in lane 1,
    which still has room, before it would face the top down."""
    setup = load_setup(
        write_setup(l=("width_mm = 30.0", "width_mm = 30.0\nlanes = 2\nheight_mm = 40.0"))
    )
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    made = (Made(1, 20.0, depth), Made(2, 18.0, depth))
    plan = assemble(setup, env, 2, PlannerKind.GRID, tests, made=made, pick=True)
    assert [(leg.lane, leg.reface_mm) for leg in plan.legs] == [(2, 0.0), (1, 0.0)]
    assert [leg.face_y_start_mm for leg in plan.legs] == [18.0, 20.0]
    assert [(s.lane, s.left_mm, s.fit) for s in plan.legs[1].lane_stock] == [
        (1, 10.0, Fit.FITS),
        (2, pytest.approx(30.0 - plan.legs[0].face_y_end_mm), Fit.USED),
    ]
    program = gcode.render(plan, setup, MM)
    assert "(STARTS IN lane 2 of 2, X30 to X60: of the lanes not cut yet this round" in program
    assert "(lane 1: of the lanes not cut yet this round that its first cut fits on, the one " in (
        program
    )


SPILL = {"l": ("width_mm = 30.0", "width_mm = 5.0\nlanes = 3\nheight_mm = 40.0")}


def spilled(write_setup):
    """A round planned 1 mm into lane 1 of 3 of a narrow block already cut at its depth:
    more tests than the lanes have room for."""
    setup = load_setup(write_setup(**SPILL))
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    start = Face(y_mm=1.0)
    made = (Made(1, 1.0, depth),)
    plan = assemble(setup, env, 2, PlannerKind.GRID, grid_tests(setup, env), face=start, made=made)
    return setup, plan


def test_a_round_uses_what_every_lane_has_left_before_it_faces_the_top_down(write_setup):
    setup, plan = spilled(write_setup)
    depth = setup.cut.axial_depth_mm
    legs = plan.legs
    # The rest of lane 1, then lanes 2 and 3 as they are; only then a fresh top, from lane 1.
    assert [leg.lane for leg in legs] == [1, 2, 3, 1, 2]
    faced = depth + REFACE_EXTRA_MM
    assert [leg.reface_mm for leg in legs] == pytest.approx([0, 0, 0, faced, 0])
    assert [leg.top_mm for leg in legs] == pytest.approx([0, 0, 0, faced, faced])
    assert legs[0].face_y_start_mm == 1.0
    # A later lane starts at Y0, as far back as its stock allows: no lane caps another, as
    # each lane's passes start and end in the reliefs where it meets the lanes beside it.
    # A leg mills each relief its passes need, as deep and as far back as they go, unless
    # one milled since the top was last faced already reaches there; facing starts afresh.
    assert all(leg.lane_cap_mm is None for leg in legs)
    assert all(leg.face_y_start_mm == 0.0 for leg in legs[1:])
    assert [[(r.boundary, r.y_mm) for r in leg.reliefs] for leg in legs] == [
        [(1, legs[0].face_y_end_mm)],
        [(2, legs[1].face_y_end_mm)],
        [],
        [(1, legs[3].face_y_end_mm)],
        [(2, legs[4].face_y_end_mm)],
    ]
    assert all(r.depth_mm == depth for leg in legs for r in leg.reliefs)
    assert all(leg.face_y_end_mm <= setup.stock.width_mm + 1e-9 for leg in legs)
    # Every test, narrowest first across the legs, each leg truing its face first.
    widths = [p.width_mm for p in plan.passes if p.kind is PassKind.TEST]
    assert widths == sorted(widths)
    assert len(widths) == len(grid_tests(setup, envelope(setup)))
    assert all(leg.passes[0].kind is PassKind.TRUING for leg in legs)
    assert [p.id for p in plan.passes] == list(range(len(plan.passes)))

    found = gcode.blocks(plan, setup, MM)
    for block in found:
        if block.step is gcode.Step.PLUNGE:
            assert block.target["z"] == pytest.approx(-(legs[block.leg].top_mm + depth))
        if block.step is not gcode.Step.CUT:
            continue
        # Each pass cuts its leg's lane: it ends on the boundary, in the relief there; the
        # last lane's run out past the end.
        lane = legs[block.leg].lane
        last = lane_x_mm(setup, lane, 3)[1]
        assert block.target["x"] == pytest.approx(last) if lane < 3 else block.target["x"] > last
    reliefs = [b for b in found if b.step is gcode.Step.RELIEF]
    assert [b.leg for b in reliefs] == [
        number
        for number, leg in enumerate(legs)
        for relief in leg.reliefs
        for _ in relief_rows(setup, 3, leg.top_mm, relief)
        for _block in ("row", "up")
    ]
    rows = facing_rows(setup, legs[3].top_mm, legs[3].reface_mm)
    faces = [i for i, b in enumerate(found) if b.step is gcode.Step.FACE]
    assert len(faces) == len(rows) and {found[i].leg for i in faces} == {3}
    cut = max(i for i, b in enumerate(found) if b.step is gcode.Step.CUT and b.leg == 2)
    rapid = min(i for i, b in enumerate(found) if b.step is gcode.Step.RAPID and b.leg == 3)
    assert cut < faces[0] and faces[-1] < rapid

    # The program says where the lane changes and where the top is faced, with no M0 stops.
    program = gcode.render(plan, setup, MM).splitlines()

    def at(prefix: str) -> int:
        return next(i for i, line in enumerate(program) if line.startswith(prefix))

    assert at(f"(pass {legs[0].passes[-1].id} ") < at("(LANE CHANGE: lane 2 of 3")
    assert at("(LANE CHANGE: lane 2 of 3") < at(f"(pass {legs[1].passes[0].id} ")
    facing_comment = at("(FACING THE TOP DOWN")
    after_facing = at(f"(pass {legs[3].passes[0].id} ")
    relief = next(
        i for i in range(facing_comment, after_facing) if program[i].startswith("(RELIEF")
    )
    assert at(f"(pass {legs[2].passes[-1].id} ") < facing_comment < relief < after_facing
    assert sum(line.startswith("G1 X") for line in program[facing_comment:relief]) == len(rows)
    assert sum(line.startswith("G1 X") for line in program[relief:after_facing]) == len(
        relief_rows(setup, 3, legs[3].top_mm, legs[3].reliefs[0])
    )
    assert not [line for line in program if line.startswith("M0")]

    # The facing's and the reliefs' feed moves count in the run time.
    cuts = sum(
        plan.dwell_s + p.plunge_s + p.lead_in_s + p.expected_cut_s + (p.lead_out_s or 0)
        for p in plan.passes
    )
    relief_s = sum(leg.relief_s for leg in legs)
    assert legs[3].face_s > 0 and relief_s > 0
    assert plan.run_time_s == pytest.approx(cuts + legs[3].face_s + relief_s)


def test_a_round_faces_the_top_down_once_then_drops_what_its_lanes_cannot_hold(write_setup):
    setup = load_setup(write_setup(**SPILL))
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    step = depth + REFACE_EXTRA_MM
    # Every lane used, the top already 20 mm down: the tests need more than one new top.
    used = Face(lane=3, y_mm=5.0, cap_mm=5.0, top_mm=20.0)
    made = (Made(3, 5.0, depth),)
    plan = assemble(setup, env, 3, PlannerKind.GRID, tests, face=used, made=made)
    # One facing, then every lane of the new top; the tests left over are dropped.
    assert [leg.reface_mm for leg in plan.legs] == pytest.approx([step, 0, 0])
    assert [leg.lane for leg in plan.legs] == [1, 2, 3]
    assert all(leg.top_mm == pytest.approx(20.0 + step) for leg in plan.legs)
    tests_cut = len(plan.passes) - len(plan.legs)  # one truing pass a leg
    assert 0 < tests_cut < len(tests)
    widths = [p.width_mm for p in plan.passes if p.kind is PassKind.TEST]
    assert widths == pytest.approx([t[1] for t in cut_order(setup, tests, 3)[:tests_cut]])
    # A chip-load sweep the new top's lanes cannot hold whole is not planned at all.
    sweep = sweep_tests(setup, env, tests[0][0], 4.0)
    with pytest.raises(PlanError, match="needs room for all its"):
        assemble(setup, env, 3, PlannerKind.SWEEP, sweep, face=used, made=made)


def test_a_chip_load_sweep_may_span_lanes_but_is_never_cut_short(write_setup):
    setup = load_setup(write_setup(l=("width_mm = 30.0", "width_mm = 7.0\nlanes = 2")))
    env = envelope(setup)
    tests = sweep_tests(setup, env, 4000, 2.0)
    plan = assemble(setup, env, 5, PlannerKind.SWEEP, tests)
    assert [leg.lane for leg in plan.legs] == [1, 2]
    swept = [p.chip_load_mm for p in plan.passes if p.kind is PassKind.TEST]
    assert swept == [t[3] for t in tests]
    # From the second lane on, three of the five fit: no sweep at all.
    rest = Face(lane=2, cap_mm=6.2)
    with pytest.raises(PlanError, match="needs room for all its 5 cuts"):
        assemble(setup, env, 5, PlannerKind.SWEEP, tests, face=rest, made=(Made(1, 6.2, 5.0),))


@pytest.mark.parametrize(
    ("lanes", "message"), [("0", "whole number"), ("1.5", "whole number"), ("4", "twice the tool")]
)
def test_lanes_must_be_whole_and_long_enough(write_setup, lanes, message):
    with pytest.raises(ConfigError, match=message):
        load_setup(write_setup(l=("width_mm = 30.0", f"width_mm = 30.0\nlanes = {lanes}")))


def test_automatic_lanes_keep_every_cut_long_enough_to_hear(write_setup):
    from dechatter.plan import auto_lanes

    plate = load_setup(
        write_setup(
            m=('material = "aluminum-6061"', 'material = "steel-1018"'),
            d=("diameter_mm = 9.525", "diameter_mm = 12.7"),
            c=("# chip_load_mm = 0.05", "chip_load_mm = 0.071"),
            s=("# speed_max_rpm = 5000", "speed_max_rpm = 2800"),
            l=("length_mm = 60.0", "length_mm = 150.0"),
        )
    )
    env = envelope(plate)
    # 0.071 mm x 3 flutes x 2800 rpm is about 10 mm/s: 6 s of cut is about 60 mm a lane,
    # besides the relief at the lane's inner end, 9.5 mm.
    assert auto_lanes(plate, env) == 2
    shorter_plate = dataclasses.replace(
        plate, stock=dataclasses.replace(plate.stock, length_mm=126.0)
    )
    assert auto_lanes(shorter_plate, env) == 1, "63 mm lanes leave 53.5 mm cuts"
    plan = round1(plate)
    assert plan.lanes == 2
    fastest = min(p.expected_cut_s for p in plan.passes)
    assert fastest >= plate.planner.min_cut_s - 0.5
    shorter = load_setup(write_setup(l=("length_mm = 60.0", "length_mm = 30.0")))
    assert auto_lanes(shorter, envelope(shorter)) == 1
    pinned = load_setup(write_setup(**LANES))
    assert auto_lanes(pinned, envelope(pinned)) == 2


def test_facing_takes_the_top_down_in_levels_and_rows_over_the_whole_stock(write_setup):
    setup = load_setup(
        write_setup(l=("width_mm = 30.0", "width_mm = 30.0\nheight_mm = 40.0\nthickness_mm = 35.0"))
    )
    radius = setup.tool.diameter_mm / 2
    step = setup.tool.diameter_mm / 4
    rows = facing_rows(setup, top_mm=7.4, reface_mm=5.2)
    levels = sorted({r.z_mm for r in rows})
    # 5.2 mm in levels of at most 1 mm, ending on the new top.
    assert len(levels) == 6 and levels[-1] == pytest.approx(7.4)
    assert levels[0] == pytest.approx(7.4 - 5.2 + 5.2 / 6, abs=1e-3)
    for level in levels:
        edges = sorted(r.y_mm + radius for r in rows if r.z_mm == level)
        # Each row takes at most a step off the front, and the last reaches the back.
        assert edges[0] <= step + 1e-9
        assert all(b - a <= step + 1e-9 for a, b in zip(edges, edges[1:], strict=False))
        assert edges[-1] == pytest.approx(35.0, abs=1e-3)
    # Every row starts and ends beside the stock, in the air.
    assert all(r.start < -radius and r.end > setup.stock.length_mm + radius for r in rows)
    assert facing_rows(setup, top_mm=0.0, reface_mm=0.0) == []

    env = envelope(setup)
    # The face has no room left: the top, cut 5 mm deep below Z2.2, comes down to Z7.4 first.
    used = Face(y_mm=30.0, top_mm=2.2)
    tests = grid_tests(setup, env)
    made = (Made(1, 30.0, 5.0),)
    plan = assemble(setup, env, 3, PlannerKind.GRID, tests, face=used, made=made, lanes=1)
    assert (plan.legs[0].top_mm, plan.legs[0].reface_mm) == (7.4, 5.2)
    found = gcode.blocks(plan, setup, MM)
    steps = [b.step for b in found]
    assert steps.count(gcode.Step.FACE) == len(rows)
    assert steps.index(gcode.Step.FACE) < steps.index(gcode.Step.RAPID)
    assert plan.legs[0].face_s > 0 and plan.run_time_s > round1(setup).run_time_s
    plunge = next(b for b in found if b.step is gcode.Step.PLUNGE)
    assert plunge.target["z"] == pytest.approx(-(7.4 + setup.cut.axial_depth_mm))
    # Each row says how many are still to come: the widget knows from it whether the
    # facing finished.
    after = [b.rows_after for b in found if b.step is gcode.Step.FACE]
    assert after == list(range(len(rows) - 1, -1, -1))


def test_a_top_faced_only_in_part_is_faced_again_before_any_cut(write_setup):
    setup = load_setup(write_setup(**SPILL))
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    # A facing stopped part-way: lane 3 still has room, but its top may be gone in places.
    stopped = Face(lane=3, y_mm=1.0, cap_mm=4.2, top=Top.PART_FACED)
    made = (Made(3, 1.0, depth),)
    plan = assemble(setup, env, 3, PlannerKind.GRID, tests, face=stopped, made=made)
    first = plan.legs[0]
    step = depth + REFACE_EXTRA_MM
    # Faced down again from the old top, as the facing cut short would have, then lane 1.
    assert (first.lane, first.face_y_start_mm, first.reface_mm) == (1, 0.0, pytest.approx(step))
    assert first.top_mm == pytest.approx(step)
    program = gcode.render(plan, setup, MM).splitlines()
    plunges = [line for line in program if line.startswith("G1 Z")]
    assert float(plunges[0].split()[1][1:]) > -1.0  # the first level, just under the old top
    # With no height to face it, nothing is cut on it.
    flat_only = load_setup(write_setup(l=("width_mm = 30.0", "width_mm = 5.0\nlanes = 3")))
    with pytest.raises(PlanError, match="stopped part-way through facing the top down"):
        assemble(flat_only, env, 3, PlannerKind.GRID, tests, face=stopped, made=made)


def test_a_faced_top_too_low_for_the_cut_is_refused(write_setup):
    setup = load_setup(write_setup(**SPILL))
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    # The clearance may be raised, or the height corrected, after the top was faced.
    fits = 40.0 - depth - setup.program.clearance_mm
    assert assemble(setup, env, 3, PlannerKind.GRID, tests, face=Face(top_mm=fits)).legs
    with pytest.raises(PlanError, match="below a top already faced down .*height_mm"):
        assemble(setup, env, 3, PlannerKind.GRID, tests, face=Face(top_mm=fits + 0.5))


FIXTURES = Path(__file__).parent / "fixtures" / "v8"


def test_a_plan_of_0_5_0_or_before_loads_as_one_leg(write_setup, tmp_path):
    # A real 0.2.0 session's round: one lane, its face 20.7 mm in.
    old = Plan.load(FIXTURES / "round-02" / "plan.json")
    assert old.schema == SCHEMA and old.lanes == 1 and len(old.legs) == 1
    leg = old.legs[0]
    assert (leg.lane, leg.face_y_start_mm, leg.lane_cap_mm, leg.top_mm) == (1, 20.7, None, 0.0)
    assert len(old.passes) == 6 and [p.id for p in old.passes] == list(range(6))

    # A 0.5.0 round on lane 2 of 3, faced down first: every field kept.
    setup = load_setup(write_setup(**SPILL))
    env = envelope(setup)
    used = Face(lane=2, y_mm=1.0, cap_mm=4.2, top_mm=5.2)
    tests = grid_tests(setup, env)[:2]
    made = (Made(2, 1.0, 5.0),)
    plan = assemble(setup, env, 4, PlannerKind.GRID, tests, face=used, made=made, lanes=3)
    assert len(plan.legs) == 1
    plan.save(tmp_path / "plan.json")
    data = json.loads((tmp_path / "plan.json").read_text())
    (single,) = data.pop("legs")
    # 0.5.0 knew one depth and one plunge a plan, side milling that steps the width.
    assert (data.pop("vary"), data.pop("milling"), single.pop("wall_mm")) == ("width", "side", 0)
    plunges = {(p.pop("depth_mm"), p.pop("plunge_s")) for p in single["passes"]}
    ((depth, plunge_s),) = plunges
    assert data.pop("fixed_mm") == depth == setup.cut.axial_depth_mm
    # 0.7.0 and older recorded neither the direction a round cut in nor what it started on.
    assert (data.pop("direction"), data.pop("start")) == ("conventional", "in use")
    # 0.8.0 and older stopped every pass short of the next lane, with no reliefs.
    assert data.pop("lane_end") == "relief" and single.pop("lane_stock") == []
    assert [r["boundary"] for r in single.pop("reliefs")] == [1, 2] and single.pop("relief_s")
    assert all(p.pop("lead_out_s") is not None for p in single["passes"])
    schema_2 = {**data, **single, "axial_depth_mm": depth, "plunge_s": plunge_s, "schema": 2}
    (tmp_path / "old.json").write_text(json.dumps(schema_2))
    loaded = Plan.load(tmp_path / "old.json")
    assert loaded.direction is None, "session.load_rounds fills it in from the setup"
    assert loaded.start is Start.IN_USE, "lane 2 of 3: earlier rounds cut lane 1"
    stopped = dataclasses.replace(
        plan.legs[0],
        passes=tuple(dataclasses.replace(p, lead_out_s=None) for p in plan.passes),
        reliefs=(),
        relief_s=0.0,
    )
    assert loaded.lane_end is LaneEnd.STOP
    assert dataclasses.replace(loaded, direction=Direction.CONVENTIONAL) == dataclasses.replace(
        plan, lane_end=LaneEnd.STOP, legs=(stopped,)
    )
    assert old.start is Start.IN_USE, "20.7 mm in: cut into before"
