"""A session started from an earlier one on the same tool assembly: what carries over, the
confirmation round that replaces the grid, and the way back to the grid."""

from __future__ import annotations

import dataclasses
import io
import json
import math
import re
import zipfile

import pytest
import synth
from test_app import NAME, PENDING, Engine, prepared_zip

from dechatter import bayes, seeding, session
from dechatter.bayes import Observation
from dechatter.config import Direction, SpindleControl, Vary, load_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import PassKind, Plan, PlannerKind, envelope, grid_tests
from dechatter.seeding import Carry, Seed, SeedPass, SeedStatus
from dechatter.session import PassResult, PassStatus, Results

MACHINE = synth.Machine()
# A machine that chatters in every cut the planner would try.
HOPELESS = synth.Machine(floor_mm=0.05, pocket_gain=0.0)
NEW = "al-10mm-again"


def cut(engine: Engine, name: str, number: int, machine: synth.Machine = MACHINE) -> dict:
    """Write the round's results as the simulated machine would have sounded: every test
    cut heard, chattering where the machine's lobes say so. No audio is involved."""
    folder = session.round_dir(engine.root / name, number)
    plan = Plan.load(folder / session.PLAN_FILE)
    passes = []
    for p in plan.passes:
        verdict = chatter_hz = None
        if p.kind is PassKind.TEST:
            chatters = machine.chatters(p.rpm, p.width_mm, plan.flutes, p.depth_mm)
            verdict = Verdict.UNSTABLE if chatters else Verdict.STABLE
            chatter_hz = machine.natural_hz * 1.03 if chatters else None
        passes.append(
            PassResult(
                p.id,
                p.kind,
                p.rpm,
                p.width_mm,
                PassStatus.ANALYSED,
                verdict,
                chatter_hz=chatter_hz,
                detected=verdict,
            )
        )
    results = Results(number, session.RECORDING_FILE, Thresholds(), tuple(passes))
    results.save(folder / session.RESULTS_FILE)
    return engine.call("state", name)


def run(engine: Engine, name: str, machine: synth.Machine = MACHINE) -> dict:
    """Plan and cut rounds until nothing is left to plan."""
    state = engine.call("state", name)
    for _ in range(16):
        if state["next"] != "plan":
            return state
        state = engine.call("plan", name, "fresh")
        state = cut(engine, name, state["rounds"][-1]["round"], machine)
    pytest.fail("did not converge in 16 rounds")


def cuts_of(round_json: dict) -> list[dict]:
    return [p for p in round_json["passes"] if p["kind"] == "test"]


def change(engine: Engine, name: str, **entries) -> dict:
    """Save the session's setup with `section.key` entries changed (dots as double underscores)."""
    values = engine.call("setup_form", name)["values"]
    for at, value in entries.items():
        section, key = at.split("__")
        values[section][key] = value
    return engine.call("save_setup", name, json.dumps(values))


@pytest.fixture
def engine(tmp_path) -> Engine:
    """A session whose setup was saved through the wizard with the values it offered."""
    engine = Engine(tmp_path / "sessions")
    engine.call("create", NAME)
    engine.call("save_setup", NAME, json.dumps(engine.call("setup_form", NAME)["values"]))
    return engine


@pytest.fixture
def measured(engine) -> Engine:
    """The engine's session, run to convergence on the simulated machine."""
    state = run(engine, NAME)
    assert state["next"] in ("converged", "sweep")
    assert state["seed"] is None and state["seeded"] == []
    return engine


def test_a_seeded_session_confirms_the_predicted_best_cut_instead_of_cutting_the_grid(measured):
    engine = measured
    source = engine.call("state", NAME)
    best = source["best"][0]
    cuts_there = sum(len(cuts_of(r)) for r in source["rounds"])
    setup = load_setup(engine.root / NAME / session.SETUP_FILE)
    grid = len(grid_tests(setup, envelope(setup)))

    assert engine.call("create", NEW, NAME) == NEW
    state = engine.call("state", NEW)
    assert state["seed"] == {
        "source": NAME,
        "material": "aluminum-6061",
        "passes": cuts_there,
        "status": "unconfirmed",
        "transfer": "limits",
        "stickout_mm": 30.0,
        "stickout_tolerance_mm": 0.6,
    }
    # The earlier cuts are on the map before any cut here, apart from this session's own.
    assert len(state["seeded"]) == cuts_there and state["observations"] == []
    assert (state["next"], state["next_planner"]) == ("plan", "confirm")
    assert f"started from {NAME}" not in json.dumps(state["rounds"])

    state = engine.call("plan", NEW, "fresh")
    first = state["rounds"][0]
    assert first["planner"] == "confirm"
    planned = cuts_of(first)
    assert 2 <= len(planned) < grid
    # The predicted best cut is the earlier session's best, and one cut is the control.
    assert (best["rpm"], best["width_mm"]) in {(p["rpm"], p["width_mm"]) for p in planned}
    control = [p for p in planned if p["expected"] == "unstable"]
    assert len(control) == 1
    assert MACHINE.chatters(control[0]["rpm"], control[0]["width_mm"], setup.tool.flutes)
    assert all(p["expected"] == "stable" for p in planned if p not in control)

    # On the same machine the cuts agree: the seed stands, and far fewer cuts settle the map.
    state = cut(engine, NEW, 1)
    assert state["seed"]["status"] == "confirmed" and state["seeded"]
    state = run(engine, NEW)
    assert state["next"] in ("converged", "sweep")
    assert state["best"][0]["mrr_cm3_min"] >= best["mrr_cm3_min"]
    assert sum(len(cuts_of(r)) for r in state["rounds"]) < cuts_there / 2
    assert all(r["planner"] != "grid" for r in state["rounds"])


def test_confirmation_cuts_that_disagree_drop_the_seed_and_bring_back_the_grid(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    setup = load_setup(engine.root / NEW / session.SETUP_FILE)
    grid = grid_tests(setup, envelope(setup))
    engine.call("plan", NEW, "fresh")

    # Another machine entirely: every cut chatters, so every cut predicted stable is wrong.
    state = cut(engine, NEW, 1, HOPELESS)
    assert state["seed"]["status"] == "rejected"
    assert state["seeded"] == []
    assert (state["next"], state["next_planner"]) == ("plan", "grid")

    state = engine.call("plan", NEW, "fresh")
    second = state["rounds"][1]
    assert second["planner"] == "grid"
    assert {(p["rpm"], p["width_mm"]) for p in cuts_of(second)} == {
        (rpm, round(width, 3)) for rpm, width, _ in grid
    }
    # The confirmation cuts were real cuts: they stay on the map as this session's own.
    assert len(state["observations"]) == len(cuts_of(state["rounds"][0]))
    # After the grid the session goes on from its own cuts alone.
    state = cut(engine, NEW, 2, HOPELESS)
    assert state["seed"]["status"] == "rejected" and state["next_planner"] != "grid"

    # One surprise among the predicted cuts leaves the seed standing: the control was stable.
    engine.call("create", "one-off", NAME)
    planned = engine.call("plan", "one-off", "fresh")["rounds"][0]
    state = cut(engine, "one-off", 1)
    control = next(p for p in cuts_of(planned) if p["expected"] == "unstable")
    state = engine.call("set_verdict", "one-off", 1, control["id"], "stable")
    assert state["seed"]["status"] == "confirmed"


def test_a_session_on_another_tool_assembly_is_refused_with_the_reason(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    setup_file = engine.root / NEW / session.SETUP_FILE
    before = setup_file.read_bytes()
    stickout = load_setup(setup_file).tool.stickout_mm

    refused = {
        "tool__stickout_mm": (stickout + 1.0, "stickout was"),
        "tool__diameter_mm": (12.0, "cutter diameter was 9.525 mm, not 12 mm"),
        "tool__flutes": (2, "cutter had 3 flutes, not 2"),
        "tool__material": ("hss", "cutter was carbide, not hss"),
        "tool__holder": ("shrink fit", "holder was"),
        "machine__weight_kg": (900.0, "machine weighed"),
        "machine__name": ("another mill", "machine was"),
    }
    for at, (value, reason) in refused.items():
        with pytest.raises(RuntimeError, match=reason) as refusal:
            change(engine, NEW, **{at: value})
        assert f"starts from {NAME}" in str(refusal.value)
        assert setup_file.read_bytes() == before
    # The session still opens on the setup it had.
    assert engine.call("state", NEW)["seed"]["source"] == NAME

    # A cutter put back within the stickout tolerance is the same assembly, and what the cut
    # differs in is never refused: material, depth, direction, what is stepped.
    state = change(
        engine,
        NEW,
        tool__stickout_mm=stickout + 0.5,
        cut__material="brass-360",
        cut__axial_depth_mm=3.0,
        cut__direction="climb",
    )
    assert state["seed"]["source"] == NAME

    # A seed whose setup was changed by hand to another assembly stops the session opening.
    seed_file = engine.root / NEW / seeding.SEED_FILE
    data = json.loads(seed_file.read_text(encoding="utf-8"))
    data["setup"] = data["setup"].replace("flutes = 3", "flutes = 4")
    seed_file.write_text(json.dumps(data), encoding="utf-8")
    fresh = Engine(engine.root)
    assert fresh.call("setup_status", NEW)["status"] == "seed_refused"
    assert "4 flutes" in fresh.call("setup_status", NEW)["reason"]


def test_a_session_with_no_verdict_cannot_be_started_from(engine):
    with pytest.raises(RuntimeError, match="no analysed cut with a verdict to start from"):
        engine.call("create", NEW, NAME)
    with pytest.raises(RuntimeError, match="no session named 'missing'"):
        engine.call("create", NEW, "missing")
    assert NEW not in engine.call("names")
    # Verdicts set by hand count like the detector's; a pass left without one is left out.
    engine.call("plan", NAME, "fresh")
    state = cut(engine, NAME, 1)
    first, second = cuts_of(state["rounds"][0])[:2]
    path = session.round_dir(engine.root / NAME, 1) / session.RESULTS_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    for p in data["passes"]:
        if p["id"] == second["id"]:
            p["verdict"] = p["detected"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    flipped = "unstable" if first["verdict"] == "stable" else "stable"
    engine.call("set_verdict", NAME, 1, first["id"], flipped)
    engine.call("create", NEW, NAME)
    seed = seeding.load(engine.root / NEW)
    verdicts = {p.id: p.verdict for p in seed.passes}
    assert verdicts[first["id"]] == flipped and second["id"] not in verdicts
    assert len(seed.passes) == len(cuts_of(state["rounds"][0])) - 1


def test_export_and_import_keep_the_seed_with_the_session(measured, tmp_path):
    engine = measured
    engine.call("create", NEW, NAME)
    engine.call("plan", NEW, "fresh")
    before = cut(engine, NEW, 1)
    archive = engine.call("export_zip", NEW, "mm")

    # Another browser, which never held the earlier session.
    other = Engine(tmp_path / "elsewhere")
    assert other.call("import_zip", archive) == NEW
    assert other.call("names") == [NEW]
    after = other.call("state", NEW)
    assert after["seed"] == before["seed"] and after["seed"]["source"] == NAME
    assert after["seeded"] == before["seeded"] and after["seeded"]
    assert after["map"] == before["map"]
    kept = seeding.load(other.root / NEW)
    assert kept == seeding.load(engine.root / NEW)
    assert kept.setup == load_setup(engine.root / NAME / session.SETUP_FILE)
    assert len(kept.passes) == before["seed"]["passes"]

    # The earlier session may go: the new one holds what it took from it. A reset keeps it.
    engine.call("delete", NAME)
    assert Engine(engine.root).call("state", NEW)["seed"] == before["seed"]
    assert engine.call("reset", NEW)["seed"]["status"] == "unconfirmed"

    report = other.call("report_html", NEW, "mm")
    assert f"Seeded from {NAME}" in report and "Started from" in report
    assert "confirmation of the session it was started from" in report
    assert "Small squares (stable) and plus signs (chatter) are seeded from" in report
    # A session started on its own says nothing of a seed.
    plain = Engine(tmp_path / "plain")
    plain.call("create", "solo")
    assert "eeded from" not in plain.call("report_html", "solo", "mm")


def test_a_damaged_seed_is_refused_with_what_to_do(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    engine.call("create", "planned", NAME)
    engine.call("plan", "planned", "fresh")
    for name in (NEW, "planned"):
        (engine.root / name / seeding.SEED_FILE).write_text('{"schema": 99}', encoding="utf-8")
    status = Engine(engine.root).call("setup_status", NEW)
    assert status["status"] == "seed_refused"
    assert "seed.json cannot be read (schema 99)" in status["reason"]
    # Before round 1 it can be removed without being read, and the session loads again.
    fresh = Engine(engine.root)
    assert fresh.call("unseed", NEW)["seed"] is None
    assert fresh.call("setup_status", NEW) == {"status": "loads", "reason": ""}
    # After it, only Delete applies.
    assert fresh.call("setup_status", "planned")["status"] == "refused"


def test_a_seed_is_kept_when_the_session_does_not_load_without_it(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    seed_file = engine.root / NEW / seeding.SEED_FILE
    kept = seed_file.read_bytes()
    (engine.root / NEW / session.SETUP_FILE).write_text("[machine\n", encoding="utf-8")
    fresh = Engine(engine.root)
    # The setup is what refuses: the seed is no way out, and goes back.
    assert fresh.call("setup_status", NEW)["status"] == "refused"
    with pytest.raises(RuntimeError, match="setup.toml"):
        fresh.call("unseed", NEW)
    assert seed_file.read_bytes() == kept


def test_a_seed_that_only_chattered_stays_on_the_map_and_round_1_is_the_grid(engine):
    engine.call("plan", NAME, "fresh")
    cut(engine, NAME, 1, HOPELESS)
    engine.call("create", NEW, NAME)
    state = engine.call("state", NEW)
    # No stable pass: no best cut to confirm. Its chatter is still what it knows of the map.
    assert state["seed"]["status"] == "untested"
    assert state["seeded"] and {o["verdict"] for o in state["seeded"]} == {"unstable"}
    assert (state["next"], state["next_planner"]) == ("plan", "grid")
    state = engine.call("plan", NEW, "fresh")
    assert state["rounds"][0]["planner"] == "grid"
    state = cut(engine, NEW, 1)
    assert state["seed"]["status"] == "untested" and state["seeded"]
    report = engine.call("report_html", NEW, "mm")
    assert "on the map untested" in report and "This session relies on" not in report

    # Nothing of it inside the search at all: not used.
    engine.call("create", "slow", NAME)
    state = change(engine, "slow", cut__speed_min_rpm=500, cut__speed_max_rpm=1000)
    assert state["seed"]["status"] == "unused" and state["seeded"] == []


def test_a_session_whose_seed_cannot_be_written_is_not_left_half_made(measured, monkeypatch):
    engine = measured

    def full(self, path):
        raise OSError("disk full")

    monkeypatch.setattr(Seed, "save", full)
    with pytest.raises(RuntimeError, match="disk full"):
        engine.call("create", NEW, NAME)
    assert NEW not in engine.call("names") and not (engine.root / NEW).exists()


def test_the_other_direction_carries_over_the_speeds_and_hardly_the_limits(measured):
    engine = measured
    source = engine.call("state", NAME)
    best = source["best"][0]
    engine.call("create", NEW, NAME)
    state = change(engine, NEW, cut__direction="climb")
    assert state["seed"]["transfer"] == "speeds" and state["seed"]["status"] == "unconfirmed"
    setup = load_setup(engine.root / NEW / session.SETUP_FILE)
    assert setup.cut.direction is Direction.CLIMB
    env = envelope(setup)
    seed = seeding.load(engine.root / NEW)
    carried = seeding.carried(seed, setup, env)
    assert {c.carry for c in carried} == {Carry.SPEEDS}
    assert {c.observation.weight for c in carried if c.observation} == {0.15}

    grid = grid_tests(setup, env)
    state = engine.call("plan", NEW, "fresh")
    planned = cuts_of(state["rounds"][0])
    assert state["rounds"][0]["planner"] == "confirm" and len(planned) < len(grid)
    control = [p for p in planned if p["expected"] == "unstable"]
    pockets = [p for p in planned if p not in control]
    # The grid's own sizes at the earlier session's best speeds: nothing is predicted of them.
    assert all(p["expected"] is None and p["p_stable"] is None for p in pockets)
    assert {p["width_mm"] for p in pockets} == {round(width, 3) for _, width, _ in grid}
    speeds = sorted({p["rpm"] for p in pockets})
    assert best["rpm"] in speeds and len(speeds) == 2
    assert speeds[1] - speeds[0] > 0.05 * speeds[0]
    # The control sits in a band the earlier session heard chatter in, at the heaviest size.
    assert len(control) == 1 and control[0]["width_mm"] == max(p["width_mm"] for p in pockets)
    heard = [p.rpm for p in seed.passes if p.verdict is Verdict.UNSTABLE]
    assert control[0]["rpm"] in heard

    # Its chatter band chattering confirms the speeds; had it not, the grid would follow.
    state = cut(engine, NEW, 1)
    assert state["seed"]["status"] == "confirmed"
    state = engine.call("set_verdict", NEW, 1, control[0]["id"], "stable")
    assert state["seed"]["status"] == "rejected" and state["next_planner"] == "grid"


def passes(*cuts) -> tuple[SeedPass, ...]:
    return tuple(
        SeedPass(1, number, rpm, width, depth, verdict)
        for number, (rpm, width, depth, verdict) in enumerate(cuts)
    )


def test_limits_scale_with_the_specific_energy_and_the_arc_of_engagement(setup, write_setup):
    text = write_setup().read_text(encoding="utf-8")
    diameter = setup.tool.diameter_mm
    seed = Seed(
        "earlier",
        text,
        passes((4000, 2.0, 5.0, Verdict.STABLE), (4000, 3.0, 5.0, Verdict.UNSTABLE)),
    )
    stable, chatter = seed.passes

    def at(**cut):
        return dataclasses.replace(setup, cut=dataclasses.replace(setup.cut, **cut))

    def arc(width):
        return math.acos(1 - 2 * width / diameter)

    # The same cut in the same material is itself.
    assert seeding.carry(seed, stable, setup) is Carry.SAME
    assert seeding.equivalent_mm(seed, stable, setup) == pytest.approx(2.0)

    # Stepping the depth at the same width, another material scales the depth by the Ks ratio.
    deep = at(vary=Vary.DEPTH, radial_width_mm=2.0, depth_max_mm=8.0, material="steel-1018")
    assert seeding.carry(seed, stable, deep) is Carry.SAME
    assert seeding.equivalent_mm(seed, stable, deep) == pytest.approx(5.0 * 0.70 / 2.20)

    # At another width the depth trades against the arc a tooth stays in the cut.
    narrow = at(vary=Vary.DEPTH, radial_width_mm=0.5, depth_max_mm=20.0)
    assert seeding.carry(seed, stable, narrow) is Carry.CONVERTED
    assert seeding.equivalent_mm(seed, stable, narrow) == pytest.approx(5.0 * arc(2.0) / arc(0.5))

    # Stepping the width in a harder material, it is the arc that shrinks, so the width
    # shrinks faster than the Ks ratio.
    steel = at(material="steel-1018")
    width = seeding.equivalent_mm(seed, stable, steel)
    assert arc(width) == pytest.approx(arc(2.0) * 0.70 / 2.20)
    assert width < 2.0 * 0.70 / 2.20

    # A full-width slot against a side cut carries over the speed only.
    slot = at(vary=Vary.DEPTH, radial_width_mm=diameter, depth_max_mm=5.0)
    assert seeding.carry(seed, stable, slot) is Carry.SPEEDS

    # Fitted to the map, a stable pass claims no more than it measured and chatter no less:
    # stable rounds down to a step, chatter up; what falls outside the search says nothing.
    env = envelope(steel)
    seen = {c.source.id: c.observation for c in seeding.carried(seed, steel, env)}
    assert seen[stable.id] is None or seen[stable.id].engagement_mm <= width
    assert seen[chatter.id].engagement_mm >= seeding.equivalent_mm(seed, chatter, steel)
    slow = dataclasses.replace(seed, passes=passes((100, 2.0, 5.0, Verdict.STABLE)))
    assert seeding.prior(slow, setup, envelope(setup)) == []
    assert seeding.confirmation(slow, setup, envelope(setup)) == []

    # A load no width reaches here (a shallow cut in a soft material) is beyond a full slot:
    # a stable pass is stable at the widest cut searched, and chatter says nothing, even
    # where the search goes up to the cutter diameter.
    soft = at(material="acetal", axial_depth_mm=1.0, width_max_mm=diameter)
    assert seeding.equivalent_mm(seed, stable, soft) == math.inf
    env = envelope(soft)
    seen = {c.source.id: c.observation for c in seeding.carried(seed, soft, env)}
    assert seen[stable.id].engagement_mm == pytest.approx(env.engagement_top_mm)
    assert seen[chatter.id] is None

    # Each cut's arc is on its own cutter: a slot of the earlier cutter stays a slot when
    # this one is a hair larger, within the tolerance.
    larger = dataclasses.replace(setup, tool=dataclasses.replace(setup.tool, diameter_mm=9.56))
    assert seeding.mismatches(seed.setup, larger) == []
    # Exactly at the tolerances passes, though the difference is not exact in binary.
    stickout = seed.setup.tool.stickout_mm
    for change in ({"stickout_mm": stickout + 0.6}, {"stickout_mm": stickout - 0.6}):
        edge = dataclasses.replace(setup, tool=dataclasses.replace(setup.tool, **change))
        assert seeding.mismatches(seed.setup, edge) == []
    slotted = dataclasses.replace(seed, passes=passes((4000, diameter, 1.0, Verdict.STABLE)))
    assert seeding.carry(slotted, slotted.passes[0], larger) is Carry.SPEEDS
    slots = dataclasses.replace(
        larger,
        cut=dataclasses.replace(
            setup.cut, vary=Vary.DEPTH, radial_width_mm=9.56, depth_max_mm=3.0, axial_depth_mm=None
        ),
    )
    assert seeding.carry(slotted, slotted.passes[0], slots) is Carry.SAME
    assert seeding.equivalent_mm(slotted, slotted.passes[0], slots) == pytest.approx(1.0)


def test_a_seed_pass_carries_over_at_the_speed_heard_in_it(measured):
    engine = measured
    folder = session.round_dir(engine.root / NAME, 1)
    results = Results.load(folder / session.RESULTS_FILE)
    # The fastest, widest cut: heard a little slower, it stays inside the search.
    tests = [p for p in results.passes if p.kind is PassKind.TEST]
    first = max(tests, key=lambda p: (p.rpm, p.width_mm))
    heard = round(first.rpm * 0.98, 1)
    passes = tuple(
        dataclasses.replace(p, spindle_hz=heard / 60) if p is first else p for p in results.passes
    )
    dataclasses.replace(results, passes=passes).save(folder / session.RESULTS_FILE)

    Engine(engine.root).call("create", NEW, NAME)
    seed = seeding.load(engine.root / NEW)
    made = next(p for p in seed.passes if (p.round, p.id) == (1, first.id))
    assert (made.rpm, made.heard_rpm, made.speed) == (first.rpm, heard, heard)
    assert all(p.heard_rpm is None and p.speed == p.rpm for p in seed.passes if p != made)

    setup = load_setup(engine.root / NEW / session.SETUP_FILE)
    placed = [c for c in seeding.carried(seed, setup, envelope(setup)) if c.source == made]
    assert placed[0].observation.rpm == heard


def test_a_confirmation_round_is_planned_on_the_speed_stops(setup, write_setup):
    text = write_setup().read_text(encoding="utf-8")
    seed = Seed(
        "earlier",
        text,
        passes((4000, 2.0, 5.0, Verdict.STABLE), (4000, 3.0, 5.0, Verdict.UNSTABLE)),
    )
    assert seeding.confirmation(seed, setup, envelope(setup))[0][0] == 4000
    by_hand = dataclasses.replace(
        setup,
        machine=dataclasses.replace(
            setup.machine, spindle=SpindleControl.MANUAL, speed_stops_rpm=(2500, 3800, 4500)
        ),
    )
    env = envelope(by_hand)
    tests = seeding.confirmation(seed, by_hand, env)
    assert tests[0][0] == 3800, "the predicted best cut at the stop nearest its speed"
    assert {t[0] for t in tests} <= set(env.speed_stops_rpm)
    # So are the grid's sizes at the best speeds, where only the speeds carry over.
    slots = dataclasses.replace(
        by_hand,
        cut=dataclasses.replace(
            by_hand.cut,
            vary=Vary.DEPTH,
            radial_width_mm=by_hand.tool.diameter_mm,
            depth_max_mm=5.0,
            axial_depth_mm=None,
        ),
    )
    assert seeding.transfer(seed, slots, envelope(slots)) is seeding.Transfer.SPEEDS
    speeds = {t[0] for t in seeding.confirmation(seed, slots, envelope(slots))}
    assert speeds and speeds <= set(envelope(slots).speed_stops_rpm)


def test_a_rejected_seed_shows_even_where_the_power_cap_leaves_no_grid(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    state = engine.call("plan", NEW, "fresh")
    setup = load_setup(engine.root / NEW / session.SETUP_FILE)
    env = envelope(setup)
    # Every confirmation cut is within the power cap at the speed it is cut at.
    for p in cuts_of(state["rounds"][0]):
        assert p["width_mm"] * p["depth_mm"] * p["feed_mm_min"] <= env.mrr_limit_mm3_min
    cut(engine, NEW, 1, HOPELESS)
    # The power may change after a cut. With none left for the grid the session still
    # opens and says what comes next; planning it says why it cannot.
    state = change(engine, NEW, machine__spindle_power_hp=0.001)
    assert state["seed"]["status"] == "rejected"
    assert (state["next"], state["next_planner"]) == ("plan", "grid")
    with pytest.raises(RuntimeError, match="power cap excludes every round-1 cut"):
        engine.call("plan", NEW, "fresh")


def test_a_confirmation_round_that_checked_no_prediction_leaves_the_seed_untested(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    change(engine, NEW, cut__direction="climb")
    planned = engine.call("plan", NEW, "fresh")["rounds"][0]
    control = next(p for p in cuts_of(planned) if p["expected"] == "unstable")
    cut(engine, NEW, 1, HOPELESS)
    # The control was not heard: every other cut chattered, but nothing was predicted of them.
    path = session.round_dir(engine.root / NEW, 1) / session.RESULTS_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    for p in data["passes"]:
        if p["id"] == control["id"]:
            p["verdict"] = p["detected"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    state = engine.call("state", NEW)
    assert state["seed"]["status"] == "untested" and state["seeded"]
    assert state["next_planner"] == "bayes"


def test_a_session_changed_since_it_was_cut_cannot_be_started_from(measured):
    engine = measured
    setup_file = engine.root / NAME / session.SETUP_FILE
    text = setup_file.read_text(encoding="utf-8")
    assert "flutes = 3" in text
    setup_file.write_text(text.replace("flutes = 3", "flutes = 2"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="was cut with another tool.flutes"):
        Engine(engine.root).call("create", NEW, NAME)
    assert not (engine.root / NEW).exists()


def test_a_seed_with_a_size_that_is_no_number_is_refused(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    seed_file = engine.root / NEW / seeding.SEED_FILE
    data = json.loads(seed_file.read_text(encoding="utf-8"))
    data["passes"][0]["width_mm"] = 0
    seed_file.write_text(json.dumps(data), encoding="utf-8")
    status = Engine(engine.root).call("setup_status", NEW)
    assert status["status"] == "seed_refused" and "no positive number" in status["reason"]


def test_a_carried_over_cut_counts_for_less_than_one_made_here(setup):
    env = envelope(setup)
    cell = (4000.0, 2.0)

    def p_stable(*known: Observation) -> float:
        stability = bayes.build_map(setup, env, list(known))
        s, e = stability.speed_index(cell[0]), stability.engagement_index(cell[1])
        return float(stability.p_stable()[s, e])

    made = Observation(*cell, Verdict.STABLE)
    same = Observation(*cell, Verdict.STABLE, weight=seeding.WEIGHT[Carry.SAME])
    converted = Observation(*cell, Verdict.STABLE, weight=seeding.WEIGHT[Carry.CONVERTED])
    speeds = Observation(*cell, Verdict.STABLE, weight=seeding.WEIGHT[Carry.SPEEDS])
    assert p_stable() < p_stable(speeds) < p_stable(converted) < p_stable(same) < p_stable(made)
    # A cut made here outweighs a carried-over one that says the opposite.
    assert p_stable(same, Observation(*cell, Verdict.UNSTABLE)) < p_stable()

    # A carried-over cut is neither the best cut to beat nor a cut already made: the planner
    # may cut it here.
    assert bayes.best_stable(setup, env, [same]) == []
    assert bayes.made([same, made]) == [made]
    assert any(rpm == cell[0] for rpm, _, _ in bayes.propose(setup, env, [same]).tests)
    assert all(rpm != cell[0] for rpm, _, _ in bayes.propose(setup, env, [made]).tests)


def test_what_the_confirmation_cuts_make_of_the_seed():
    stable, chatter = Verdict.STABLE, Verdict.UNSTABLE
    assert seeding.judged([]) is None
    assert seeding.judged([(stable, stable), (stable, stable), (chatter, chatter)]) is (
        SeedStatus.CONFIRMED
    )
    # One surprise in four stands, two do not.
    agreed = [(stable, stable), (stable, stable), (chatter, chatter)]
    assert seeding.judged([*agreed, (stable, chatter)]) is SeedStatus.CONFIRMED
    assert seeding.judged([*agreed[1:], (stable, chatter), (stable, chatter)]) is (
        SeedStatus.REJECTED
    )
    # Cuts nothing was predicted of check nothing; the control alone does.
    assert seeding.judged([(None, chatter), (None, stable)]) is SeedStatus.UNTESTED
    assert seeding.judged([(None, chatter), (chatter, stable)]) is SeedStatus.REJECTED
    assert seeding.judged([(None, chatter), (chatter, chatter)]) is SeedStatus.CONFIRMED


def test_a_plan_keeps_what_was_expected_of_each_confirmation_cut(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    engine.call("plan", NEW, "fresh")
    plan = Plan.load(session.round_dir(engine.root / NEW, 1) / session.PLAN_FILE)
    assert plan.planner is PlannerKind.CONFIRM
    expected = [p.expected for p in plan.passes if p.kind is PassKind.TEST]
    assert Verdict.STABLE in expected and expected.count(Verdict.UNSTABLE) == 1
    assert all(p.expected is None for p in plan.passes if p.kind is not PassKind.TEST)
    # A plan of the earlier session, written without the field, loads as before.
    old = Plan.load(session.round_dir(engine.root / NAME, 1) / session.PLAN_FILE)
    assert all(p.expected is None for p in old.passes)


def test_a_slot_of_an_inch_size_cutter_is_a_slot_though_plans_keep_three_decimals(write_setup):
    text = write_setup().read_text(encoding="utf-8")
    assert "diameter_mm = 9.525" in text
    seed = Seed(
        "earlier",
        text.replace("diameter_mm = 9.525", "diameter_mm = 4.7625"),
        passes((4000, 4.762, 1.0, Verdict.STABLE)),
    )
    cut = dataclasses.replace(
        seed.setup.cut, vary=Vary.DEPTH, radial_width_mm=4.7625, depth_max_mm=3.0
    )
    slots = dataclasses.replace(seed.setup, cut=dataclasses.replace(cut, axial_depth_mm=None))
    assert seeding.carry(seed, seed.passes[0], slots) is Carry.SAME
    assert seeding.carry(seed, seed.passes[0], seed.setup) is Carry.SPEEDS


def test_limits_that_predict_no_best_cut_plan_no_confirmation(setup, write_setup):
    text = write_setup().read_text(encoding="utf-8")
    env = envelope(setup)
    # Its one stable pass is contradicted by chatter in a lighter cut at the same speed.
    seed = Seed(
        "earlier",
        text,
        passes((4000, 2.0, 5.0, Verdict.STABLE), (4000, 1.0, 5.0, Verdict.UNSTABLE)),
    )
    assert seeding.transfer(seed, setup, env) is seeding.Transfer.LIMITS
    assert seeding.prior(seed, setup, env)
    # Not the grid's sizes at that speed instead: the limits carried over say no to them.
    assert seeding.confirmation(seed, setup, env) == []
    assert session.seed_status(seed, setup, env, []) is SeedStatus.UNTESTED
    assert session.next_stage(setup, env, [], [], seed).planner is PlannerKind.GRID


def test_the_control_is_the_deepest_chatter_band_not_a_pocket_probed_to_its_ceiling(
    setup, write_setup
):
    text = write_setup().read_text(encoding="utf-8")
    climb = dataclasses.replace(
        setup, cut=dataclasses.replace(setup.cut, direction=Direction.CLIMB)
    )
    env = envelope(climb)
    seed = Seed(
        "earlier",
        text,
        passes(
            (4600, 3.0, 5.0, Verdict.STABLE),
            (3700, 2.5, 5.0, Verdict.STABLE),
            # A third pocket, probed again and again at its ceiling: heavy cuts only.
            (3100, 2.0, 5.0, Verdict.STABLE),
            (3100, 2.4, 5.0, Verdict.UNSTABLE),
            (3100, 2.5, 5.0, Verdict.UNSTABLE),
            (3100, 2.6, 5.0, Verdict.UNSTABLE),
            # The trough: chatter in a light cut, heard once.
            (2200, 1.0, 5.0, Verdict.UNSTABLE),
            # Chatter a stable pass as heavy at that speed contradicts is no band at all.
            (2700, 0.5, 5.0, Verdict.UNSTABLE),
            (2700, 0.8, 5.0, Verdict.STABLE),
        ),
    )
    tests = seeding.confirmation(seed, climb, env)
    control = [(rpm, expected) for rpm, _, _, _, expected in tests if expected is not None]
    assert control == [(2200.0, Verdict.UNSTABLE)]
    assert {rpm for rpm, *_ in tests} == {4600.0, 3700.0, 2200.0}


PREPARED = "prepared-1018"


def prepare(engine: Engine, name: str = PREPARED, pending: str = PENDING, **tool) -> bytes:
    """Import a prepared session as harmonic-analyzer hands it over: the measured session's
    setup with `[pending]`, its `tool` entries replaced (a placeholder stickout, say), in
    1018. Answer the setup file as written."""
    text = (engine.root / NAME / session.SETUP_FILE).read_text(encoding="utf-8")
    text = text.replace('material = "aluminum-6061"', 'material = "steel-1018"')
    for key, value in tool.items():
        text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
    assert engine.call("import_zip", prepared_zip(name, text + pending)) == name
    return (engine.root / name / session.SETUP_FILE).read_bytes()


def test_a_prepared_session_starts_from_an_earlier_one_and_confirms_once_its_values_are(measured):
    engine = measured
    # The stickout is a placeholder far from the earlier session's 30 mm: it is to be
    # measured on the machine, so it does not stop the seed.
    written = prepare(engine, stickout_mm=45.0)
    source = engine.call("state", NAME)
    passes = sum(len(cuts_of(r)) for r in source["rounds"])

    state = engine.call("start_from", PREPARED, NAME)
    assert state["seed"]["source"] == NAME and state["seed"]["passes"] == passes
    assert state["seed"]["status"] == "unconfirmed" and state["seed"]["stickout_mm"] == 30.0
    assert state["seeded"] and state["rounds"] == []
    # The session keeps its own setup, [pending] and all; the seed keeps the earlier one's.
    assert (engine.root / PREPARED / session.SETUP_FILE).read_bytes() == written
    seed = seeding.load(engine.root / PREPARED)
    assert seed.setup_text == (engine.root / NAME / session.SETUP_FILE).read_text("utf-8")
    assert engine.call("setup_form", PREPARED)["seed"] == NAME
    assert engine.call("state", PREPARED)["summary"]["material"] == "steel-1018"

    # Seeding confirms nothing: planning waits for the wizard.
    assert state["next"] == "confirm" and set(state["pending"]) == {
        "stock.height_mm",
        "tool.stickout_mm",
    }
    with pytest.raises(RuntimeError, match="values to confirm on the machine"):
        engine.call("plan", PREPARED, "fresh")
    # The report claims no stickout it has not compared.
    report = engine.call("report_html", PREPARED, "mm")
    assert "machine, holder and cutter (this session's stickout is still to confirm" in report
    assert "holder, cutter and stickout" not in report
    # Saving the wizard confirms the stickout, which is then checked: the placeholder is
    # another assembly, and the session stays as it was.
    values = engine.call("setup_form", PREPARED)["values"]
    with pytest.raises(RuntimeError, match="stickout was 30 mm, not 45 mm") as refused:
        engine.call("save_setup", PREPARED, json.dumps(values))
    assert f"don't start from {NAME}" in str(refused.value)
    assert (engine.root / PREPARED / session.SETUP_FILE).read_bytes() == written
    assert engine.call("state", PREPARED)["next"] == "confirm"

    # The stickout measured within 0.6 mm: round 1 is the confirmation round.
    values["tool"]["stickout_mm"] = 30.4
    state = engine.call("save_setup", PREPARED, json.dumps(values))
    assert state["pending"] == {} and state["seed"]["source"] == NAME
    assert (state["next"], state["next_planner"]) == ("plan", "confirm")
    state = engine.call("plan", PREPARED, "fresh")
    assert state["rounds"][0]["planner"] == "confirm"
    assert "holder, cutter and stickout in" in engine.call("report_html", PREPARED, "mm")

    # Once a round is planned, what the session started from is fixed.
    with pytest.raises(RuntimeError, match="can only be seeded before its first round"):
        engine.call("start_from", PREPARED, NAME)
    with pytest.raises(RuntimeError, match="a seed can only be removed before the first round"):
        engine.call("unseed", PREPARED)
    assert seeding.load(engine.root / PREPARED) == seed


def test_start_from_is_refused_with_the_reason_and_writes_nothing(measured):
    engine = measured
    # A session with a round.
    engine.call("create", "planned")
    engine.call("save_setup", "planned", json.dumps(engine.call("setup_form", NAME)["values"]))
    engine.call("plan", "planned", "fresh")
    with pytest.raises(RuntimeError, match="map can only be seeded before its first round"):
        engine.call("start_from", "planned", NAME)
    # One that already starts from another.
    engine.call("create", NEW, NAME)
    with pytest.raises(RuntimeError, match=f"already starts from {NAME}: choose Don't start"):
        engine.call("start_from", NEW, NAME)
    with pytest.raises(RuntimeError, match="cannot start from itself"):
        engine.call("start_from", NAME, NAME)
    with pytest.raises(RuntimeError, match="no session named 'missing'"):
        engine.call("start_from", "missing", NAME)
    # One still on the example setup: fill it in, or use New, which takes the earlier setup.
    engine.call("create", "blank")
    with pytest.raises(RuntimeError, match="'blank' still holds the example setup"):
        engine.call("start_from", "blank", NAME)

    # Another tool assembly. Only the stickout waits for the wizard: a flute count still to
    # confirm is compared as it stands.
    flutes = '\n[pending]\n"tool.flutes" = "count them"\n'
    written = prepare(engine, pending=flutes, flutes=4)
    with pytest.raises(RuntimeError, match="cutter had 3 flutes, not 4") as refused:
        engine.call("start_from", PREPARED, NAME)
    assert f"{PREPARED!r} cannot start from {NAME}" in str(refused.value)
    # A source with no analysed cut, or that is missing.
    with pytest.raises(RuntimeError, match="'planned' has no analysed cut"):
        engine.call("start_from", PREPARED, "planned")
    with pytest.raises(RuntimeError, match="no session named 'gone'"):
        engine.call("start_from", PREPARED, "gone")

    for name in ("planned", PREPARED, "blank"):
        assert not (engine.root / name / seeding.SEED_FILE).exists()
        assert engine.call("state", name)["seed"] is None
    assert (engine.root / PREPARED / session.SETUP_FILE).read_bytes() == written


def test_a_seed_that_cannot_be_written_whole_is_not_left_behind(measured, monkeypatch):
    engine = measured
    prepare(engine)

    def half(self, path):
        path.write_text('{"schema": 1, "sou', encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(Seed, "save", half)
    with pytest.raises(RuntimeError, match="disk full"):
        engine.call("start_from", PREPARED, NAME)
    assert not (engine.root / PREPARED / seeding.SEED_FILE).exists()
    assert engine.call("state", PREPARED)["seed"] is None


def test_a_seed_can_be_removed_and_chosen_again_before_the_first_round(measured):
    engine = measured
    engine.call("create", NEW, NAME)
    setup_file = engine.root / NEW / session.SETUP_FILE
    before = setup_file.read_bytes()

    state = engine.call("unseed", NEW)
    assert state["seed"] is None and state["seeded"] == []
    assert (state["next"], state["next_planner"]) == ("plan", "grid")
    assert not (engine.root / NEW / seeding.SEED_FILE).exists()
    assert setup_file.read_bytes() == before
    with pytest.raises(RuntimeError, match="does not start from another session"):
        engine.call("unseed", NEW)

    state = engine.call("start_from", NEW, NAME)
    assert state["seed"]["source"] == NAME and state["next_planner"] == "confirm"
    assert setup_file.read_bytes() == before


def test_export_and_import_keep_a_prepared_session_and_its_seed(measured, tmp_path):
    engine = measured
    prepare(engine, stickout_mm=45.0)
    before = engine.call("start_from", PREPARED, NAME)
    archive = engine.call("export_zip", PREPARED, "mm")
    held = zipfile.ZipFile(io.BytesIO(archive))
    assert "[pending]" in held.read(f"{PREPARED}/{session.SETUP_FILE}").decode("utf-8")
    assert f"{PREPARED}/{seeding.SEED_FILE}" in held.namelist()

    # Another browser, which never held the earlier session.
    other = Engine(tmp_path / "elsewhere")
    assert other.call("import_zip", archive) == PREPARED
    after = other.call("state", PREPARED)
    for key in ("seed", "seeded", "pending", "next", "map"):
        assert after[key] == before[key]
    assert seeding.load(other.root / PREPARED) == seeding.load(engine.root / PREPARED)
