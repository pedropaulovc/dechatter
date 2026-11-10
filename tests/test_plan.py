from __future__ import annotations

import re

import pytest

from dechatter import gcode
from dechatter.config import ConfigError, load_setup
from dechatter.plan import (
    PassKind,
    PlanError,
    PlannerKind,
    assemble,
    envelope,
    grid_tests,
    mrr_mm3_min,
)


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
    weak = load_setup(write_setup(p=("spindle_power_w = 1500", "spindle_power_w = 60")))
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
    moves = cutting_moves(gcode.render(plan, setup))
    assert len(moves) == len(plan.passes)
    face = plan.face_y_start_mm
    for (y, x_end, feed), p in zip(moves, plan.passes, strict=True):
        face += p.width_mm
        assert y == pytest.approx(face - plan.diameter_mm / 2, abs=1e-3)
        assert feed == pytest.approx(p.feed_mm_min)
        assert x_end > setup.stock.length_mm  # conventional: tool in front, travelling +X


def test_climb_reverses_travel(write_setup):
    climb = load_setup(write_setup(d=('direction = "conventional"', 'direction = "climb"')))
    assert all(x_end < 0 for _, x_end, _ in cutting_moves(gcode.render(round1(climb), climb)))


def test_dwell_precedes_every_cut(setup):
    program = gcode.render(round1(setup), setup).splitlines()
    cuts = [i for i, line in enumerate(program) if line.startswith("G1 X")]
    assert all(program[i - 1] == "G4 P4" for i in cuts)


MANUAL = {"s": ('spindle = "program"', 'spindle = "manual"')}


def test_an_inch_machine_is_put_back_in_inches_even_by_a_program_without_m30():
    restore = gcode.RESTORE_INCH
    assert gcode.for_machine("G21\nG0 X1\nM30\n", gcode.Units.MM) == "G21\nG0 X1\nM30\n"
    assert (
        gcode.for_machine("G21\nG0 X1\nM30\n", gcode.Units.INCH) == f"G21\nG0 X1\n{restore}\nM30\n"
    )
    # Hand-edited: no M30, and a last line that would swallow anything glued onto it.
    assert gcode.for_machine("G21\n; end", gcode.Units.INCH) == f"G21\n; end\n{restore}\n"
    assert gcode.for_machine("G21\nG0 X1\n\n\n", gcode.Units.INCH) == f"G21\nG0 X1\n{restore}\n"


def test_manual_spindle_pauses_once_per_speed_and_never_commands_the_spindle(write_setup):
    manual = load_setup(write_setup(**MANUAL))
    plan = round1(manual)
    program = gcode.render(plan, manual).splitlines()

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
