from __future__ import annotations

import re
from pathlib import Path

import pytest

from dechatter import gcode
from dechatter.config import ConfigError, load_setup, parse_setup
from dechatter.plan import (
    SWEEP_PASSES,
    PassKind,
    PlanError,
    PlannerKind,
    assemble,
    envelope,
    grid_tests,
    mrr_mm3_min,
    sweep_tests,
)

MM = gcode.Units.MM


def round1(setup, face=0.0, remnant=0.0):
    env = envelope(setup)
    return assemble(setup, env, 1, PlannerKind.GRID, face, remnant, grid_tests(setup, env))


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
    assert plan.face_y_start_mm + plan.consumed_mm <= setup.stock.width_mm
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
    face = plan.face_y_start_mm
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
    assert float(depth) * 25.4 == pytest.approx(-plan.axial_depth_mm, abs=0.002)
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
    plan = assemble(setup, env, 2, PlannerKind.SWEEP, 0.0, 0.0, tests)
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
    assert cut.target["x"] > setup.stock.length_mm and cut.target["z"] == -plan.axial_depth_mm

    manual = load_setup(write_setup(**MANUAL))
    manual_blocks = gcode.blocks(round1(manual), manual, MM)
    asks = [b for b in manual_blocks if b.step is gcode.Step.SET_SPINDLE]
    assert [b.rpm for b in asks] == sorted({p.rpm for p in round1(manual).passes})
    assert all(b.lines == () and b.target is None for b in asks)
    assert not [line for b in manual_blocks for line in b.lines if line.startswith(("M0", "M3"))]
