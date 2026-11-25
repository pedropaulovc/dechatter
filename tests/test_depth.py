"""A session that steps the axial depth at a fixed radial width: full-width slots in the top,
or side milling on the front face."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import synth

from dechatter import audio, bayes, browser, gcode, report, session
from dechatter.app import App
from dechatter.config import ConfigError, Vary, parse_setup, read_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import (
    REFACE_EXTRA_MM,
    Face,
    Made,
    Milling,
    Order,
    PassKind,
    PlanError,
    PlannerKind,
    Reach,
    assemble,
    envelope,
    grid_tests,
    mrr_mm3_min,
)

MM = gcode.Units.MM

# pedropaulovc/harmonic-analyzer#3428: a 1/2 in 3-flute carbide end mill in 1018, slotting at
# its full 12.7 mm width, 0.25 to 0.75 mm deep.
SLOTS = """\
[machine]
name = "PM-30MV"
max_rpm = 2800
min_rpm = 500
spindle_power_hp = 2.0
weight_kg = 250

[tool]
diameter_mm = 12.7
flutes = 3
stickout_mm = 27.94
holder = "R8 collet"
material = "carbide"

[cut]
material = "steel-1018"
vary = "depth"
radial_width_mm = 12.7
depth_max_mm = 0.75
chip_load_mm = 0.071

[stock]
length_mm = 150.0
width_mm = 50.8
thickness_mm = 50.8
height_mm = 20.0
"""
DIAMETER, RADIUS, WALL = 12.7, 6.35, 12.7 / 4
PITCH = DIAMETER + WALL
# On this machine a 12.7 mm slot chatters from 0.25 mm deep, 0.5 mm at its sweet spots.
MACHINE = synth.DEPTH_MACHINE


def slots(**changes: tuple[str, str]):
    text = SLOTS
    for old, new in changes.values():
        assert old in text
        text = text.replace(old, new)
    return parse_setup(text, "setup.toml")


SIDE = {"w": ("radial_width_mm = 12.7", "radial_width_mm = 3.0"), "d": ("0.75", "3.0")}
NO_HEIGHT = {"h": ("height_mm = 20.0\n", "")}
ONE_LANE = {"l": ("length_mm = 150.0", "length_mm = 150.0\nlanes = 1")}
TWO_LANES = {"l": ("length_mm = 150.0", "length_mm = 150.0\nlanes = 2")}
THIN = {"t": ("width_mm = 50.8\nthickness_mm = 50.8", "width_mm = 19.0\nthickness_mm = 19.0")}


def test_what_a_session_steps_is_a_setup_choice_with_its_own_entries():
    setup = slots()
    assert setup.cut.vary is Vary.DEPTH and setup.cut.axial_depth_mm is None
    env = envelope(setup)
    assert env.engagement_max_mm == 0.75 and env.engagement_top_mm == pytest.approx(0.75)
    # Removal rate is width x depth x feed, whichever of the two is stepped.
    feed = env.chip_load_mm * 3 * 2000
    assert mrr_mm3_min(setup, env, 2000, 0.5) == pytest.approx(12.7 * 0.5 * feed)

    refused = {
        "needs radial_width_mm": {"w": ("radial_width_mm = 12.7\n", "")},
        "needs depth_max_mm": {"d": ("depth_max_mm = 0.75\n", "")},
        "axial_depth_mm do not apply": {"w": ("vary =", "axial_depth_mm = 1.0\nvary =")},
        "width_max_mm do not apply": {"w": ("vary =", "width_max_mm = 5.0\nvary =")},
        "within \\(0, tool diameter\\]": {"w": ("radial_width_mm = 12.7", "radial_width_mm = 13")},
        "narrower than radial_width_mm": {"w": ("vary =", "other_widths_mm = [12.7]\nvary =")},
        "height_mm must stand above the deepest cut, depth_max_mm": {"h": ("20.0", "0.5")},
        "thickness_mm cannot be less": {"t": ("thickness_mm = 50.8", "thickness_mm = 19")},
        "depth_max_mm apply only": {"v": ('vary = "depth"', "axial_depth_mm = 1.0")},
        "holds no 12.7 mm slot with 3.175 mm": THIN,
        "round1_depth_fractions must list": {
            "p": ("[stock]", "[planner]\nround1_depth_fractions = [1.5]\n\n[stock]")
        },
    }
    for message, change in refused.items():
        with pytest.raises(ConfigError, match=message):
            slots(**change)
    # A thinner wall fits the slot into the thinner block.
    thin = THIN | {"h": ("height_mm = 20.0", "")}
    slots(**thin, p=("[stock]", "[program]\nslot_wall_mm = 1.0\n\n[stock]"))


def test_round_one_steps_the_depth_in_full_width_slots_deepest_first():
    setup = slots()
    env = envelope(setup)
    tests = grid_tests(setup, env)
    assert sorted({round(depth, 3) for _, depth, _ in tests}) == [0.15, 0.3, 0.45]
    plan = assemble(setup, env, 1, PlannerKind.GRID, tests)

    assert (plan.vary, plan.fixed_mm, plan.milling) == (Vary.DEPTH, 12.7, Milling.SLOT)
    # Slots need no face trued: every pass is a test, at the cutter's full width.
    assert all(p.kind is PassKind.TEST and p.width_mm == 12.7 for p in plan.passes)
    # The stock is long enough for two lanes, so the round runs deepest first.
    assert plan.lanes == 2 and plan.order is Order.DEEPEST_FIRST
    first, second, third = plan.legs[:3]
    # Three rows of slots across the 50.8 mm top, a wall before each and behind the last.
    assert (first.lane, len(first.passes), first.wall_mm) == (1, 3, WALL)
    assert first.face_y_end_mm == pytest.approx(3 * PITCH)
    assert (second.lane, len(second.passes), second.lane_cap_mm) == (2, 3, 47.625)
    # A slot in the second lane is reached through the first lane's slot in its row.
    for before, after in zip(first.passes, second.passes, strict=True):
        assert after.depth_mm <= before.depth_mm
    # The top used up, it comes down below the deepest slot, and the round goes on.
    deepest = max(p.depth_mm for p in (*first.passes, *second.passes))
    assert third.reface_mm == pytest.approx(deepest + REFACE_EXTRA_MM)
    assert (third.lane, third.face_y_start_mm, third.top_mm) == (1, 0.0, third.reface_mm)
    for start in range(0, len(plan.legs), 2):
        depths = [p.depth_mm for leg in plan.legs[start : start + 2] for p in leg.passes]
        assert depths == sorted(depths, reverse=True), "deepest first on every top"
    # The top is faced once a round: twelve slots. The three that wait are the heaviest, the
    # deepest at the fastest speeds, not the shallow ones the round would end on.
    kept = sorted(tests, key=lambda t: (t[1], t[0]))[:12]
    assert sorted((p.rpm, p.depth_mm) for p in plan.passes) == sorted(
        (round(rpm), round(depth, 3)) for rpm, depth, _ in kept
    )

    blocks = gcode.blocks(plan, setup, MM)
    of = lambda step, p: next(b for b in blocks if b.step is step and b.pass_id == p.id)  # noqa: E731
    clearance = setup.program.clearance_mm
    for row, p in enumerate(first.passes):
        rapid, plunge = of(gcode.Step.RAPID, p), of(gcode.Step.PLUNGE, p)
        # The cutter plunges in the air beyond the stock's end, centred on its row.
        assert rapid.target["x"] == pytest.approx(-(RADIUS + clearance))
        assert rapid.target["y"] == pytest.approx(WALL + RADIUS + row * PITCH, abs=1e-3)
        assert plunge.lines == (f"G0 Z{clearance:g}", f"G1 Z-{p.depth_mm:g} F150")
        # It stops in the stock, its slot's round end just short of the second lane.
        assert of(gcode.Step.CUT, p).target["x"] == pytest.approx(75.0 - RADIUS)
        # Stock on both sides: straight up, no backing off a face.
        assert of(gcode.Step.RETRACT, p).lines == ("G0 Z10",)
    for before, p in zip(first.passes, second.passes, strict=True):
        # The second lane's cutter plunges inside the first lane's slot, and runs out the end.
        assert of(gcode.Step.RAPID, p).target == of(gcode.Step.RAPID, before).target | {
            "x": pytest.approx(75.0 - DIAMETER - clearance)
        }
        assert of(gcode.Step.CUT, p).target["x"] == pytest.approx(150.0 + RADIUS + clearance)
    lowered = of(gcode.Step.PLUNGE, third.passes[0])
    assert lowered.target["z"] == pytest.approx(-(third.top_mm + third.passes[0].depth_mm))

    program = gcode.render(plan, setup, MM)
    assert "width 12.7 mm, full-width slots, the depth stepped" in program
    assert "(lane 1 of 2, X0 to X75: slots in the top from Y0 to Y47.625)" in program
    assert "(pass 0 test: S700 width 12.7 mm depth 0.45 mm F149.1)" in program
    assert "The deepest passes come first" in program
    assert "LANE CHANGE: lane 2 of 2, X75 to X150 from here, slots from Y0 again" in program


def test_slots_in_one_lane_ramp_up_and_take_any_depth_in_a_row_of_their_own():
    setup = slots(**ONE_LANE, **NO_HEIGHT)
    env = envelope(setup)
    plan = assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env))
    # Each slot is cut into stock of its own: the round keeps its ramp, lightest first.
    assert plan.order is Order.RAMP and len(plan.passes) == 3
    assert [p.depth_mm for p in plan.passes] == [0.15, 0.15, 0.15]
    assert "Later passes are heavier" in gcode.render(plan, setup, MM)

    # One row used by a shallow slot: a deeper one goes into the next row as it is.
    used = Face(y_mm=PITCH)
    made = (Made(1, PITCH, 0.15, Milling.SLOT),)
    deeper = [(2000.0, 0.6, 0.9), (2000.0, 0.7, 0.8), (2400.0, 0.5, 0.9)]
    plan = assemble(setup, env, 2, PlannerKind.BAYES, deeper, face=used, made=made)
    assert [leg.reface_mm for leg in plan.legs] == [0.0]
    assert [(p.rpm, p.depth_mm) for p in plan.passes] == [(2400, 0.5), (2000, 0.6)]
    assert plan.legs[0].face_y_start_mm == pytest.approx(PITCH)


def test_a_used_up_top_is_faced_below_its_deepest_slot_or_fresh_stock_is_asked_for():
    env = envelope(slots())
    used = Face(lane=2, y_mm=3 * PITCH, cap_mm=3 * PITCH)
    made = tuple(
        Made(lane, (row + 1) * PITCH, depth, Milling.SLOT)
        for lane in (1, 2)
        for row, depth in enumerate((0.45, 0.3, 0.15))
    )
    tests = [(2000.0, 0.6, 0.9)]
    plan = assemble(slots(), env, 2, PlannerKind.BAYES, tests, face=used, made=made)
    assert (plan.legs[0].reface_mm, plan.legs[0].top_mm) == pytest.approx((0.65, 0.65))

    # Without the height above the jaws the top is never faced.
    with pytest.raises(PlanError, match="no room left for a slot in lane 2 of 2.*height_mm"):
        assemble(slots(**NO_HEIGHT), env, 2, PlannerKind.BAYES, tests, face=used, made=made)
    # Nor once the next slot's bottom would come within the clearance of the jaws.
    low = slots(h=("height_mm = 20.0", "height_mm = 4.0"))
    with pytest.raises(PlanError, match="The top cannot come down further"):
        assemble(low, env, 2, PlannerKind.BAYES, tests, face=used, made=made)

    # A face milled into this top leaves no whole top for slots, and the reverse.
    milled = (Made(1, 6.0, 2.0, Milling.SIDE),)
    plan = assemble(slots(), env, 2, PlannerKind.BAYES, tests, face=Face(y_mm=6.0), made=milled)
    assert plan.legs[0].reface_mm == pytest.approx(2.2)
    with pytest.raises(PlanError, match="slots into the top, but this block's top holds side cuts"):
        assemble(
            slots(**NO_HEIGHT), env, 2, PlannerKind.BAYES, tests, face=Face(y_mm=6.0), made=milled
        )


def test_side_milling_steps_the_depth_deepest_first_on_each_face():
    setup = slots(**SIDE, **TWO_LANES, **NO_HEIGHT)
    env = envelope(setup)
    plan = assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env))
    assert (plan.milling, plan.order, plan.fixed_mm) == (Milling.SIDE, Order.DEEPEST_FIRST, 3.0)
    (leg,) = plan.legs
    truing, *tests = leg.passes
    depths = [p.depth_mm for p in tests]
    # A face is flat only above its shallowest cut: no cut is deeper than one before it.
    assert depths == sorted(depths, reverse=True) and set(depths) == {0.6, 1.2, 1.8}
    assert all(p.width_mm == 3.0 for p in tests)
    # The face is trued as deep as the deepest test.
    assert (truing.kind, truing.width_mm, truing.depth_mm) == (PassKind.TRUING, 0.2, 1.8)
    blocks = gcode.blocks(plan, setup, MM)
    bottoms = [b.target["z"] for b in blocks if b.step is gcode.Step.PLUNGE]
    assert bottoms == [-p.depth_mm for p in leg.passes]
    # Side milling backs off the face before it lifts.
    retract = next(b for b in blocks if b.step is gcode.Step.RETRACT)
    assert len(retract.lines) == 2 and retract.lines[0].startswith("G0 Y")
    assert "(pass 1 test: S700 width 3 mm depth 1.8 mm F149.1)" in gcode.render(plan, setup, MM)


def test_a_cut_deeper_than_its_face_allows_moves_to_a_fresh_lane():
    setup = slots(**SIDE, **TWO_LANES, **NO_HEIGHT)
    env = envelope(setup)
    first = assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env)).legs[0]
    made, y = [], 0.0
    for p in first.passes:
        y += p.width_mm
        made.append(Made(1, y, p.depth_mm))
    face = Face(y_mm=y)
    # The first lane's face ends 0.6 mm deep. It takes the shallower cut as it stands; the
    # deeper ones go to the second lane, a fresh face, each no deeper than the first lane
    # was cut that far back, which the cutter crosses to reach it.
    tests = [(2000.0, 1.0, 0.9)] * 12 + [(2000.0, 0.5, 0.9)]
    plan = assemble(setup, env, 2, PlannerKind.BAYES, tests, face=face, made=tuple(made))
    assert [(leg.lane, leg.face_y_start_mm) for leg in plan.legs] == [(1, y), (2, 0.0)]
    assert [p.depth_mm for p in plan.legs[0].passes] == [0.5, 0.5]  # truing, then the test
    fresh = plan.legs[1]
    assert fresh.lane_cap_mm == pytest.approx(y + 3.2)
    reached = 0.0
    for p in fresh.passes:
        reached += p.width_mm
        behind = min((m for m in made if m.y_mm >= reached - 1e-6), key=lambda m: m.y_mm)
        assert p.depth_mm == 1.0 <= behind.depth_mm
    # The first lane was cut 1.8 and 1.2 mm deep for 30.2 mm back: ten 3 mm cuts fit, and the
    # two beyond them would cross stock cut only 0.6 mm deep.
    assert len(fresh.passes) == 1 + 10

    # Deeper than the first lane ever went: no lane is clear for it, only a faced top.
    with pytest.raises(PlanError, match="would cut 2.4 mm deep.*only flat down to 0.6 mm"):
        assemble(
            setup, env, 2, PlannerKind.BAYES, [(2000.0, 2.4, 0.9)], face=face, made=tuple(made)
        )
    tall = slots(**SIDE, **TWO_LANES)
    plan = assemble(
        tall, env, 2, PlannerKind.BAYES, [(2000.0, 2.4, 0.9)], face=face, made=tuple(made)
    )
    assert plan.legs[0].reface_mm == pytest.approx(1.8 + REFACE_EXTRA_MM)


def far_walls(plan, lane: int, top_mm: float) -> list[float]:
    """The far wall, from Y0, of every slot a plan cuts in a lane on a top."""
    walls = []
    for leg in plan.legs:
        y = leg.face_y_start_mm
        for p in leg.passes:
            y += p.width_mm + leg.wall_mm
            if (leg.lane, leg.top_mm) == (lane, top_mm):
                walls.append(round(y, 3))
    return walls


def test_a_slot_stopped_part_way_clears_nothing_for_the_next_lane():
    setup = slots()
    env = envelope(setup)
    first = assemble(setup, env, 1, PlannerKind.GRID, [(1500.0, 0.6, None), (1500.0, 0.5, None)])
    assert [(leg.lane, len(leg.passes)) for leg in first.legs] == [(1, 2)]
    # The second slot, in the second row of lane 1, was feed-held part-way.
    heard = tuple(
        session.PassResult(p.id, p.kind, p.rpm, p.width_mm, status, verdict)
        for p, status, verdict in zip(
            first.passes,
            (session.PassStatus.ANALYSED, session.PassStatus.ABORTED),
            (Verdict.STABLE, Verdict.UNSTABLE),
            strict=True,
        )
    )
    results = session.Results(1, "recording.wav", Thresholds(), heard)
    cut = session.Round(Path("round-01"), first, results)
    face = session.face_after(cut)
    made = session._made_since_top([cut])
    # Its row is used in lane 1, and the stock beyond where it stopped stands uncut.
    assert (face.lane, face.y_mm, face.remnant_mm) == (1, pytest.approx(2 * PITCH), 0.0)
    assert [m.reach for m in made] == [Reach.END, Reach.PART]

    tests = [(1500.0, 0.6, None), (1500.0, 0.5, None), (1500.0, 0.45, None)]
    plan = assemble(setup, env, 2, PlannerKind.BAYES, tests, face=face, made=made, lanes=2)
    # Lane 2 comes in through the whole slot of row 1 only. A slot in row 2 would plunge at
    # X59.3, inside lane 1, into the stock the stopped slot never reached: the third cut
    # waits for the top to be faced instead.
    assert far_walls(plan, 1, 0.0) == [pytest.approx(3 * PITCH)]
    assert far_walls(plan, 2, 0.0) == [pytest.approx(PITCH)]
    assert [leg.reface_mm for leg in plan.legs] == pytest.approx([0, 0, 0.6 + REFACE_EXTRA_MM])
    for block in gcode.blocks(plan, setup, MM):
        inside_lane_1 = block.step is gcode.Step.RAPID and block.target["x"] > 0
        assert not inside_lane_1 or block.target["y"] == pytest.approx(WALL + RADIUS)
    # With no height to face the top there is room for two cuts, the row lane 1 has left
    # and row 1 of lane 2: the heaviest waits, and nothing goes into row 2 of lane 2.
    bare = slots(**NO_HEIGHT)
    plan = assemble(bare, env, 2, PlannerKind.BAYES, tests, face=face, made=made, lanes=2)
    assert [(leg.lane, p.depth_mm) for leg in plan.legs for p in leg.passes] == [
        (1, 0.5),
        (2, 0.45),
    ]
    assert far_walls(plan, 2, 0.0) == [pytest.approx(PITCH)]


def test_a_slot_in_a_later_lane_needs_the_slot_of_its_own_row_before_it():
    # The wall sets the rows: it is part of what a session's slots were cut with.
    assert "program.slot_wall_mm" not in session.FREE_AFTER_CUT
    thin = slots(p=("[stock]", "[program]\nslot_wall_mm = 1.0\n\n[stock]"))
    assert session._measured_changes(slots(), thin) == ["program.slot_wall_mm"]

    # Lane 1 holds three slots 3.175 mm apart. Slots 1 mm apart would lie across its walls:
    # none goes into lane 2 on this top, whatever the depth.
    env = envelope(thin)
    used = Face(lane=1, y_mm=3 * PITCH)
    made = tuple(Made(1, row * PITCH, 0.6, Milling.SLOT) for row in (1, 2, 3))
    tests = [(1500.0, 0.5, None)] * 3
    plan = assemble(thin, env, 2, PlannerKind.BAYES, tests, face=used, made=made, lanes=2)
    assert far_walls(plan, 2, 0.0) == []
    assert [(leg.lane, leg.top_mm) for leg in plan.legs] == [(1, pytest.approx(0.8))]
    thin_bare = slots(p=("[stock]", "[program]\nslot_wall_mm = 1.0\n\n[stock]"), **NO_HEIGHT)
    with pytest.raises(PlanError, match="no room left for a slot"):
        assemble(thin_bare, env, 2, PlannerKind.BAYES, tests, face=used, made=made, lanes=2)
    # On the rows lane 1 was cut in, they go in, no deeper than its slots.
    plan = assemble(slots(), env, 2, PlannerKind.BAYES, tests, face=used, made=made, lanes=2)
    assert far_walls(plan, 2, 0.0) == pytest.approx([PITCH, 2 * PITCH, 3 * PITCH])


DEEP = {"d": ("depth_max_mm = 0.75", "depth_max_mm = 3.0")}


def test_no_depth_searched_or_planned_is_deeper_than_depth_max_mm():
    coarse = "[planner]\ndepth_step_mm = 0.4\nround1_depth_fractions = [0.5, 1.0]\n\n[stock]"
    setup = slots(**DEEP, p=("[stock]", coarse), c=("chip_load_mm = 0.071", "chip_load_mm = 0.01"))
    env = envelope(setup)
    # 3.0 mm is 7.5 steps of 0.4 mm: the search stops at 7, 2.8 mm, not at 8, 3.2 mm.
    assert env.engagements(0.4)[-1] == pytest.approx(2.8)
    assert env.engagement_top_mm == pytest.approx(2.8)
    assert bayes.build_map(setup, env, []).engagements[-1] == pytest.approx(2.8)
    tests = grid_tests(setup, env)
    assert sorted({depth for _, depth, _ in tests}) == pytest.approx([1.6, 2.8])
    plan = assemble(setup, env, 1, PlannerKind.GRID, tests)
    assert max(p.depth_mm for p in plan.passes) == pytest.approx(2.8)

    # The deepest cut must stay the clearance (3 mm) above what the stock stands on.
    with pytest.raises(ConfigError, match="must leave .* below the deepest cut.*at least 6 mm"):
        slots(**DEEP, h=("height_mm = 20.0", "height_mm = 5.9"))
    slots(**DEEP, h=("height_mm = 20.0", "height_mm = 6.0"))
    with pytest.raises(ConfigError, match="depth_step_mm cannot be more than"):
        slots(p=("[stock]", "[planner]\ndepth_step_mm = 0.8\n\n[stock]"))


def test_a_cut_on_a_faced_top_stays_the_clearance_above_what_the_stock_stands_on():
    shallow = "[program]\nclearance_mm = 1.0\n\n[stock]"
    setup = slots(**DEEP, h=("height_mm = 20.0", "height_mm = 5.0"), p=("[stock]", shallow))
    env = envelope(setup)
    # Earlier rounds faced the top down 3.2 mm; the search may still ask for 3.0 mm deep,
    # whose bottom would be 6.2 mm below Z0 on a block that stands 5 mm above the jaws.
    low = Face(top_mm=3.2)
    deep, light = (1000.0, 3.0, None), (1000.0, 0.6, None)
    for lanes in (1, 2):
        with pytest.raises(PlanError, match="3 mm deep below a top already faced down 3.2 mm"):
            assemble(setup, env, 5, PlannerKind.BAYES, [deep], face=low, lanes=lanes)
        # A lighter cut still fits (3.2 + 0.6 + 1 mm): it is cut, the deep one left out,
        # in a ramp and deepest first alike.
        plan = assemble(setup, env, 5, PlannerKind.BAYES, [deep, light], face=low, lanes=lanes)
        assert [(p.depth_mm, leg.top_mm) for leg in plan.legs for p in leg.passes] == [(0.6, 3.2)]
    # Nor is the top faced down to where the next cut would not fit: 0.8 mm more, then 0.6.
    used = Face(y_mm=3 * PITCH, top_mm=3.2)
    made = tuple(Made(1, row * PITCH, 0.6, Milling.SLOT) for row in (1, 2, 3))
    with pytest.raises(PlanError, match="The top cannot come down further"):
        assemble(setup, env, 5, PlannerKind.BAYES, [light], face=used, made=made, lanes=1)


def record(work, target: session.Round, machine, seed) -> None:
    wav = target.path / session.RECORDING_FILE
    heard = synth.record(target.plan, machine, seed=seed, facing=synth.Facing.HEARD)
    audio.save_wav(wav, heard, synth.RATE)
    tl = synth.timeline(target.plan, facing=synth.Facing.HEARD)
    (target.path / session.TIMELINE_FILE).write_text(json.dumps(tl), encoding="utf-8")
    session.analyze_recording(target, wav, Thresholds())


def test_a_slotting_session_converges_on_the_depth_limit(tmp_path):
    # A block that stands 4.5 mm above the jaws: the top can come down once.
    setup_text = SLOTS.replace("height_mm = 20.0", "height_mm = 4.5")
    source = read_setup(setup_text, session.SETUP_FILE)
    setup = source.setup
    work = tmp_path / "slots"
    work.mkdir()
    (work / session.SETUP_FILE).write_text(setup_text)
    env = envelope(setup)
    fresh_blocks = 0
    for _ in range(12):
        try:
            planned = session.plan_next(work, source, env, session.Stock.SAME)
        except PlanError as exc:
            # No row left and the top cannot come down further: fresh stock is asked for.
            assert "mount fresh stock and plan with new stock" in str(exc)
            fresh_blocks += 1
            planned = session.plan_next(work, source, env, session.Stock.FRESH)
        if planned.plan is None:
            break
        rounds = session.load_rounds(work)
        record(work, rounds[-1], MACHINE, len(rounds))
    else:
        pytest.fail("did not converge in 12 rounds")

    rounds = session.load_rounds(work)
    assert all(r.plan.milling is Milling.SLOT and r.plan.fixed_mm == 12.7 for r in rounds)
    refaced = [leg.reface_mm for r in rounds for leg in r.plan.legs if leg.reface_mm > 0]
    assert refaced and fresh_blocks >= 1

    cuts = session.observations(rounds, 12.7, env)
    for obs in cuts:
        truth = MACHINE.chatters(obs.rpm, 12.7, 3, obs.engagement_mm)
        assert (obs.verdict is Verdict.UNSTABLE) == truth or MACHINE.masked(obs.rpm), obs
    assert any(obs.verdict is Verdict.UNSTABLE for obs in cuts)
    best = session.best_cuts(setup, env, rounds)[0]
    assert best.width_mm == 12.7 and not MACHINE.chatters(best.rpm, 12.7, 3, best.depth_mm)
    optimum = max(
        mrr_mm3_min(setup, env, rpm, min(MACHINE.depth_limit_mm(rpm, 3, 12.7), 0.75))
        for rpm in env.speeds(10)
    )
    # It finds a sweet spot from the chatter tone, where a slot goes half again as deep as
    # elsewhere, and brackets the limit there, a stable cut under a chattering one. It stops
    # when the depths between promise under 5%, not always at the best of the sweet spots.
    assert best.depth_mm >= 1.5 * min(MACHINE.depth_limit_mm(s, 3, 12.7) for s in env.speeds(10))
    assert best.mrr_mm3_min >= 0.7 * optimum

    state = App(work, Thresholds()).state()
    assert (state["vary"], state["level_mm"]) == ("depth", 12.7)
    assert state["summary"]["levels_mm"] == [12.7] and state["map"]["engagements"][-1] == 0.75
    assert (
        state["rounds"][0]["milling"] == "slot" and state["rounds"][0]["order"] == "deepest-first"
    )
    assert state["best"][0]["depth_mm"] == best.depth_mm
    # The depths are stepped a hundredth of a millimetre at a time: 75 levels up to 0.75 mm.
    assert {round(o["engagement_mm"] * 100, 6) % 1 for o in state["observations"]} == {0.0}

    page = report.render(work, MM)
    assert "axial depth, at each width below" in page and "<th>Widths</th>" not in page
    assert "<td>Widths</td><td>12.70 mm</td>" in page
    assert "<h3>12.70 mm wide</h3>" in page and "<th>Depth</th>" in page
    assert "full-width slots, deepest cut first" in page and ">depth (mm)</text>" in page
    assert "The session stepped the depth of cut." in page


LADDER = """\
[machine]
max_rpm = 5000
spindle_power_hp = 2.0
weight_kg = 250

[tool]
diameter_mm = 9.525
flutes = 3
stickout_mm = 30.0

[cut]
material = "aluminum-6061"
vary = "depth"
radial_width_mm = 4.0
other_widths_mm = [2.0]
depth_max_mm = 6.0
speed_min_rpm = 2500

[stock]
length_mm = 60.0
width_mm = 80.0
"""


def test_a_narrower_width_starts_from_the_depths_that_were_stable_at_the_wider_one(tmp_path):
    source = read_setup(LADDER, session.SETUP_FILE)
    setup = source.setup
    work = tmp_path / "ladder"
    work.mkdir()
    (work / session.SETUP_FILE).write_text(LADDER)
    env = envelope(setup)
    assert session.levels(setup) == [4.0, 2.0]
    # On this machine a narrower cut stays stable much deeper.
    machine = synth.Machine(reference_depth_mm=2.5, depth_power=0.8)
    for _ in range(16):
        planned = session.plan_next(work, source, env, session.Stock.FRESH)
        if planned.plan is None:
            break
        rounds = session.load_rounds(work)
        record(work, rounds[-1], machine, len(rounds))
    else:
        pytest.fail("did not converge in 16 rounds")

    rounds = session.load_rounds(work)
    order = [r.plan.fixed_mm for r in rounds]
    assert order == sorted(order, reverse=True) and set(order) == {4.0, 2.0}
    wide = session.observations(rounds, 4.0, env)
    narrow = session.observations(rounds, 2.0, env)
    stable_wide = {(o.rpm, o.engagement_mm) for o in wide if o.verdict is Verdict.STABLE}
    # What was stable 4 mm wide is known to be stable 2 mm wide: it is not cut again, it
    # counts as evidence there, and the search goes on above it.
    assert not stable_wide & {(o.rpm, o.engagement_mm) for o in narrow}
    carried = session.evidence(rounds, 2.0, env)
    assert len(carried) == len(narrow) + len(stable_wide)
    # Below this depth a 4 mm cut is stable at any speed on this machine.
    floor = machine.reference_depth_mm * (machine.floor_mm / 4.0) ** (1 / machine.depth_power)
    assert min(o.engagement_mm for o in narrow) > floor
    # Chatter 4 mm wide says nothing 2 mm wide.
    assert all(o.verdict is Verdict.STABLE for o in carried if o not in narrow)
    # The best cuts are ranked across widths, each by its own width x depth x feed.
    best = session.best_cuts(setup, env, rounds)
    assert {cut.width_mm for cut in best} == {4.0, 2.0}
    assert [cut.mrr_mm3_min for cut in best] == sorted((c.mrr_mm3_min for c in best), reverse=True)
    narrowest = next(cut for cut in best if cut.width_mm == 2.0)
    assert narrowest.mrr_mm3_min == pytest.approx(
        mrr_mm3_min(session.at_level(setup, 2.0), env, narrowest.rpm, narrowest.depth_mm)
    )
    for r in rounds:
        for p in r.results.passes:
            if p.kind is PassKind.TEST:
                depth = next(q.depth_mm for q in r.plan.passes if q.id == p.id)
                truth = machine.chatters(p.rpm, r.plan.fixed_mm, 3, depth)
                heard = (p.verdict is Verdict.UNSTABLE) == truth
                assert heard or machine.masked(p.rpm), (p, depth)


def test_the_wizard_takes_and_keeps_what_the_session_steps(tmp_path):
    sessions = browser.Sessions(tmp_path / "sessions")

    def call(method: str, *args):
        reply = json.loads(browser.call(sessions, method, *args))
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["ok"]

    call("create", "slots")
    form = call("setup_form", "slots")
    assert form["values"]["cut"]["vary"] == "width" and form["defaults"]["cut"]["vary"] == "width"
    values = form["values"]
    values["cut"] |= {
        "vary": "depth",
        "axial_depth_mm": None,
        "radial_width_mm": 9.525,
        "depth_max_mm": 3.0,
    }
    values["stock"]["thickness_mm"] = 30.0
    preview = call("setup_preview", json.dumps(values))
    assert "width_max_mm" not in preview["cut"]
    assert preview["program"]["slot_wall_mm"] == pytest.approx(2.381)
    state = call("save_setup", "slots", json.dumps(values))
    assert state["vary"] == "depth" and state["next_level_mm"] == 9.525
    state = call("plan", "slots", "same")
    assert state["rounds"][0]["milling"] == "slot"
    assert all(
        p["width_mm"] == 9.525 and p["depth_mm"] <= 3.0 for p in state["rounds"][0]["passes"]
    )

    # What a session steps is part of what it measured: it cannot change once a round is cut.
    (tmp_path / "sessions" / "slots" / "round-01" / "recording.wav").write_bytes(b"")
    back = call("setup_form", "slots")["values"]
    back["cut"] |= {"vary": "width", "axial_depth_mm": 3.0, "radial_width_mm": None}
    back["cut"]["depth_max_mm"] = None
    with pytest.raises(RuntimeError, match="was cut with another .*cut.vary"):
        call("save_setup", "slots", json.dumps(back))
