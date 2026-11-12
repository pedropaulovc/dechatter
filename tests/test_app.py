"""The widget's engine API: the same calls the browser worker makes, against a real folder."""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import date
from pathlib import Path

import numpy as np
import pytest
import synth
from scipy import signal

import dechatter
from dechatter import browser, cli, session
from dechatter.app import TIMELINE_FILE, App
from dechatter.config import load_setup
from dechatter.plan import Plan, envelope

MACHINE = synth.Machine()
NAME = "al-10mm"
BROWSER_RATE = 48000


class Engine:
    """Calls go through `browser.call`, so replies are decoded exactly as the widget does."""

    def __init__(self, root) -> None:
        self.root = root
        self.sessions = browser.Sessions(root)

    def call(self, method: str, *args):
        reply = browser.call(self.sessions, method, *args)
        if isinstance(reply, bytes):
            return reply
        decoded = json.loads(reply)
        if "error" in decoded:
            raise RuntimeError(decoded["error"])
        return decoded["ok"]

    def run_round(
        self,
        number: int,
        *,
        abort_in_pass: int | None = None,
        gain: float = 1.0,
        speed_change_s: float = 0.0,
        machine: synth.Machine = MACHINE,
    ):
        """Record what the simulated machine sounds like cutting the round, as the mic would."""
        plan = Plan.load(session.round_dir(self.root / NAME, number) / session.PLAN_FILE)
        heard = synth.record(
            plan,
            machine,
            seed=number,
            abort_in_pass=abort_in_pass,
            speed_change_s=speed_change_s,
        )
        heard = heard * gain
        # Browsers capture at 48 kHz, far above the simulated machine's own bandwidth.
        heard = signal.resample_poly(heard, BROWSER_RATE, synth.RATE).astype(np.float32)
        self.call("record_start", NAME, number)
        timeline = json.dumps([{"t": 1.0, "event": "workflow", "state": "running"}])
        return self.call("record_stop", NAME, memoryview(heard), float(BROWSER_RATE), timeline)


@pytest.fixture
def engine(tmp_path) -> Engine:
    """A session whose setup was saved through the wizard with the values it offered."""
    engine = Engine(tmp_path / "sessions")
    engine.call("create", NAME)
    engine.call("save_setup", NAME, json.dumps(engine.call("setup_form", NAME)["values"]))
    return engine


def converge(engine, setup, machine: synth.Machine = MACHINE, **run) -> dict:
    """Plan and run rounds until the planner stops, checking every verdict against the machine."""
    for _ in range(16):
        state = engine.call("plan", NAME, "fresh")
        number = state["rounds"][-1]["round"]
        assert state["next"] == "run"
        program = engine.call("program", NAME, number, "mm")
        assert program == (session.round_dir(engine.root / NAME, number) / "program.nc").read_text()
        assert program.rstrip().endswith("M30")
        # An inch machine gets the program in inches and stays in them.
        assert "G20 G90 G94 G17" in engine.call("program", NAME, number, "in").splitlines()
        state = engine.run_round(number, machine=machine, **run)
        latest = state["rounds"][-1]
        assert latest["analysed"]
        for p in latest["passes"]:
            if p["kind"] != "test":
                continue
            truth = machine.chatters(p["rpm"], p["width_mm"], setup.tool.flutes, latest["depth_mm"])
            assert p["verdict"] == ("unstable" if truth else "stable")
        if state["next"] in ("converged", "sweep"):
            return state
    pytest.fail("did not converge in 16 rounds")


def test_session_runs_to_convergence(engine, setup):
    state = engine.call("state", NAME)
    assert state["next"] == "plan" and state["rounds"] == []

    state = converge(engine, setup)

    assert state["best"][0]["mrr_cm3_min"] > 12
    assert len(state["observations"]) == sum(
        p["kind"] == "test" for r in state["rounds"] for p in r["passes"]
    )
    timeline = json.loads((engine.root / NAME / "round-01" / TIMELINE_FILE).read_text())
    assert timeline[0]["state"] == "running"


def test_manual_spindle_session_converges_despite_the_stops_to_change_speed(engine, setup):
    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["spindle"] = "manual"
    assert engine.call("save_setup", NAME, json.dumps(form))["spindle"] == "manual"

    state = converge(engine, setup, speed_change_s=12.0)
    assert state["best"][0]["mrr_cm3_min"] > 12


def test_a_shallower_depth_is_tried_from_the_stable_widths_and_wins_where_the_machine_allows(
    engine, setup
):
    form = engine.call("setup_form", NAME)["values"]
    form["cut"]["other_depths_mm"] = [4.0]
    engine.call("save_setup", NAME, json.dumps(form))
    deep = form["cut"]["axial_depth_mm"]
    # On this machine a shallower cut can be much wider before it chatters.
    machine = synth.Machine(reference_depth_mm=deep)

    state = converge(engine, setup, machine)

    order = [r["depth_mm"] for r in state["rounds"]]
    assert order == sorted(order, reverse=True) and set(order) == {deep, 4.0}
    tests = lambda depth: [  # noqa: E731
        p
        for r in state["rounds"]
        if r["depth_mm"] == depth
        for p in r["passes"]
        if p["kind"] == "test"
    ]
    stable_deep = {(p["rpm"], p["width_mm"]) for p in tests(deep) if p["verdict"] == "stable"}
    # Nothing already known to be stable is cut again, and the search starts where it left off.
    assert not stable_deep & {(p["rpm"], p["width_mm"]) for p in tests(4.0)}
    assert min(p["width_mm"] for p in tests(4.0)) > machine.floor_mm
    best = state["best"][0]
    assert best["depth_mm"] == 4.0
    assert best["mrr_cm3_min"] > max(
        b["mrr_cm3_min"] for b in state["best"] if b["depth_mm"] == deep
    )


def test_a_deeper_cut_is_refused_on_a_block_whose_face_is_only_flat_higher_up(engine):
    form = engine.call("setup_form", NAME)["values"]
    form["cut"]["other_depths_mm"] = [4.0]
    engine.call("save_setup", NAME, json.dumps(form))
    machine = synth.Machine(reference_depth_mm=5.0)
    state = engine.call("state", NAME)
    while state["next_depth_mm"] == 5.0:
        state = engine.call("plan", NAME, "fresh")
        state = engine.run_round(state["rounds"][-1]["round"], machine=machine)
    state = engine.call("plan", NAME, "fresh")
    state = engine.run_round(state["rounds"][-1]["round"], machine=machine)
    assert state["rounds"][-1]["depth_mm"] == 4.0

    # The operator hears that a 5 mm cut was stable after all: there is more to try at 5 mm,
    # but this block was cut flat only down to 4 mm.
    flips = [
        (r["round"], p["id"])
        for r in state["rounds"]
        if r["depth_mm"] == 5.0
        for p in sorted(r["passes"], key=lambda p: -p["width_mm"])
        if p["verdict"] == "unstable"
    ]
    for number, pass_id in flips:
        state = engine.call("set_verdict", NAME, number, pass_id, "stable")
        if state["next_depth_mm"] == 5.0:
            break
    assert state["next"] == "plan" and state["next_depth_mm"] == 5.0
    with pytest.raises(RuntimeError, match="only flat down to 4 mm"):
        engine.call("plan", NAME, "same")
    assert engine.call("plan", NAME, "fresh")["rounds"][-1]["depth_mm"] == 5.0


def test_planning_waits_until_the_template_setup_is_filled_in(engine):
    engine.call("create", "untouched")
    state = engine.call("state", "untouched")
    assert state["next"] == "setup"
    with pytest.raises(RuntimeError, match="still the example template"):
        engine.call("plan", "untouched", "same")
    assert not list((engine.root / "untouched").glob("round-*"))

    # Saving the wizard, even on the values it offered, describes the machine.
    form = engine.call("setup_form", "untouched")["values"]
    form["tool"]["flutes"] = 2
    assert engine.call("save_setup", "untouched", json.dumps(form))["next"] == "plan"
    state = engine.call("plan", "untouched", "same")
    assert state["next"] == "run"
    plan = Plan.load(session.round_dir(engine.root / "untouched", 1) / session.PLAN_FILE)
    assert plan.flutes == 2


def files(folder) -> dict:
    return {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_a_setup_change_discards_the_round_planned_from_the_old_one(engine):
    state = engine.call("plan", NAME, "same")
    stale_program = state["rounds"][0]["program"]
    form = engine.call("setup_form", NAME)["values"]
    assert engine.call("save_setup", NAME, json.dumps(form))["rounds"], "no change, round kept"
    form["machine"]["name"] = "renamed mill"
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["rounds"] and state["discarded_round"] is None, "not in the program: kept"

    form["machine"]["spindle"] = "manual"
    form["machine"]["max_rpm"] = 2800
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["rounds"] == [] and state["discarded_round"] == 1 and state["next"] == "plan"
    assert [p.name for p in (engine.root / NAME).iterdir()] == [session.SETUP_FILE]

    state = engine.call("plan", NAME, "same")
    assert state["discarded_round"] is None
    assert max(p["rpm"] for p in state["rounds"][0]["passes"]) <= 2800
    # cncjs may still hold the old round 1: the new one must not pass for it.
    assert state["rounds"][0]["program"] != stale_program
    program = engine.call("program", NAME, 1, "mm").splitlines()
    assert not [line for line in program if re.match(r"M3\b|S\d", line)]
    assert [line for line in program if line.startswith("M0")]


def test_a_failed_save_puts_back_the_round_it_would_have_discarded(engine, monkeypatch):
    engine.call("plan", NAME, "same")
    before = files(engine.root / NAME)
    form = engine.call("setup_form", NAME)["values"]
    form["program"]["safe_z_mm"] = 20.0

    def broken(self):
        raise RuntimeError("state failed")

    monkeypatch.setattr(App, "state", broken)
    with pytest.raises(RuntimeError, match="state failed"):
        engine.call("save_setup", NAME, json.dumps(form))
    monkeypatch.undo()
    assert files(engine.root / NAME) == before
    assert engine.call("state", NAME)["rounds"][0]["round"] == 1


def test_a_setup_change_after_a_round_was_cut_is_refused_where_it_was_measured(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    folder = engine.root / NAME
    before = files(folder)
    form = engine.call("setup_form", NAME)["values"]

    for section, key, value in [
        ("tool", "flutes", 2),
        ("tool", "holder", "ER20"),
        ("machine", "weight_kg", 100.0),
        ("machine", "spindle", "manual"),
        ("cut", "material", "brass-360"),
        ("cut", "chip_load_mm", 0.03),
        ("stock", "length_mm", 80.0),
        ("program", "dwell_s", 2.0),
        ("program", "coolant", "mist"),
    ]:
        changed = json.loads(json.dumps(form))
        changed[section][key] = value
        with pytest.raises(RuntimeError, match=rf"round 1 .*{section}\.{key}.*new session"):
            engine.call("save_setup", NAME, json.dumps(changed))
        assert files(folder) == before
        assert engine.call("setup_form", NAME)["values"] == form

    # Moves around the cuts, the name, the stock left, where to search and the planner are
    # free.
    form["machine"]["name"] = "renamed mill"
    form["machine"]["min_rpm"] = 600
    form["machine"]["spindle_power_hp"] = 1.5
    form["cut"]["other_depths_mm"] = [2.5]
    form["cut"]["speed_min_rpm"] = 1500
    form["program"]["safe_z_mm"] = 20.0
    form["program"]["clearance_mm"] = 5.0
    form["program"]["plunge_feed_mm_min"] = 100.0
    form["program"]["truing_width_mm"] = 0.3
    form["stock"]["width_mm"] = 50.0
    form["planner"]["batch_size"] = 6
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["rounds"][0]["analysed"] and state["discarded_round"] is None
    state = engine.call("plan", NAME, "same")
    assert [r["round"] for r in state["rounds"]] == [1, 2]


def test_the_search_can_be_widened_or_narrowed_after_a_round_was_cut(engine):
    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["max_rpm"] = 3000
    engine.call("save_setup", NAME, json.dumps(form))
    engine.call("plan", NAME, "same")
    state = engine.run_round(1)
    cuts = len(state["observations"])
    tests = [p for p in state["rounds"][0]["passes"] if p["kind"] == "test"]

    # Narrower than cuts already made. Those stay in their round. A stable one proves the
    # narrower widths stable, so it counts at the widest width searched; chatter that wide
    # says nothing about them. Neither is recommended.
    narrowed = json.loads(json.dumps(form))
    narrowed["cut"]["width_max_mm"] = 1.0
    state = engine.call("save_setup", NAME, json.dumps(narrowed))
    top = state["map"]["widths"][-1]
    assert top == 1.0
    wide = [p for p in tests if p["width_mm"] > top]
    assert wide and any(p["verdict"] == "stable" for p in wide)
    chatter = sum(p["verdict"] == "unstable" for p in wide)
    assert len(state["observations"]) == cuts - chatter
    clamped = [o for o in state["observations"] if o["width_mm"] == top]
    assert len(clamped) >= sum(p["verdict"] == "stable" for p in wide)
    assert all(o["verdict"] == "stable" for o in clamped if o["width_mm"] == top)
    assert all(b["width_mm"] <= top for b in state["best"])
    assert len(state["rounds"][0]["passes"]) > cuts

    form["machine"]["max_rpm"] = 5000
    form["cut"]["speed_max_rpm"] = 4500
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert 3000 < state["map"]["speeds"][-1] <= 4500
    state = engine.call("plan", NAME, "fresh")
    planned = [p["rpm"] for p in state["rounds"][1]["passes"] if p["kind"] == "test"]
    assert max(planned) > 3000 and max(planned) <= 4500


def test_cuts_at_the_grid_edges_count_whatever_the_steps(engine):
    form = engine.call("setup_form", NAME)["values"]
    # The widest fraction lands on 4.8 mm: the width grid rounds the 4.7625 mm limit up.
    form["planner"]["round1_width_fractions"] = [0.2, 0.5, 1.0]
    engine.call("save_setup", NAME, json.dumps(form))
    engine.call("plan", NAME, "same")
    state = engine.run_round(1)
    tests = [p for p in state["rounds"][0]["passes"] if p["kind"] == "test"]
    assert max(p["width_mm"] for p in tests) == 4.8
    assert len(state["observations"]) == len(tests)
    edge = {(p["rpm"], p["verdict"]) for p in tests if p["width_mm"] == 4.8}
    assert edge == {(o["rpm"], o["verdict"]) for o in state["observations"] if o["width_mm"] == 4.8}
    measured = {(p["rpm"], p["width_mm"]) for p in tests}
    state = engine.call("plan", NAME, "same")
    planned = {
        (p["rpm"], p["width_mm"]) for p in state["rounds"][1]["passes"] if p["kind"] == "test"
    }
    assert not planned & measured, "a cell already cut is not proposed again"

    # A coarser speed step moves the snapped bounds inside the speeds already cut (1250 rpm
    # becomes 1300); those cuts still count.
    assert min(p["rpm"] for p in tests) == 1250
    form["planner"]["speed_step_rpm"] = 100
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["discarded_round"] == 2
    assert len(state["observations"]) == len(tests)
    assert min(o["rpm"] for o in state["observations"]) == 1250


def test_export_works_even_when_the_report_cannot_be_made(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    results = session.round_dir(engine.root / NAME, 1) / session.RESULTS_FILE
    results.write_text("{ not json", encoding="utf-8")
    archive = zipfile.ZipFile(io.BytesIO(engine.call("export_zip", NAME, "mm")))
    names = archive.namelist()
    assert f"{NAME}/report.html" not in names
    assert f"{NAME}/round-01/results.json" in names and f"{NAME}/round-01/recording.wav" in names
    assert archive.read(f"{NAME}/round-01/results.json") == b"{ not json"


def test_narrowing_the_speeds_after_cutting_recommends_and_plans_only_inside_them(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    engine.call("plan", NAME, "same")
    state = engine.run_round(2)
    assert state["best"][0]["rpm"] > 2000

    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["max_rpm"] = 2000
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["best"] and all(b["rpm"] <= 2000 for b in state["best"])
    assert all(o["rpm"] <= 2000 for o in state["observations"])
    assert state["next"] == "plan"
    state = engine.call("plan", NAME, "fresh")
    planned = [p for p in state["rounds"][-1]["passes"] if p["kind"] == "test"]
    assert planned and all(p["rpm"] <= 2000 for p in planned)


def test_a_session_from_an_older_dechatter_is_refused_cleanly(engine, tmp_path, capsys):
    # A 0.1.x session stops at its setup, with what to do.
    old = tmp_path / "old-session"
    old.mkdir()
    setup_011 = (Path(__file__).parent / "fixtures" / "setup-0.1.1.toml").read_text()
    (old / session.SETUP_FILE).write_text(setup_011.replace("PM-30MV", "my mill"))
    for command in (["plan"], ["report"]):
        assert cli.main([command[0], str(old)]) == 1
        err = capsys.readouterr().err
        assert "from dechatter 0.1.x" in err and "start a new session" in err

    # Rounds in the older format: their schema says so.
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    folder = session.round_dir(engine.root / NAME, 1)
    plan_path, results_path = folder / session.PLAN_FILE, folder / session.RESULTS_FILE
    plan, results = json.loads(plan_path.read_text()), json.loads(results_path.read_text())
    older = dict(plan, schema=1)
    del older["planned_on"]
    plan_path.write_text(json.dumps(older))
    message = r"round-01/plan.json was written by an older dechatter \(schema 1\)\. Start a new"
    for method, args in [("state", ()), ("plan", ("same",)), ("program", (1, "mm"))]:
        with pytest.raises(RuntimeError, match=message):
            Engine(engine.root).call(method, NAME, *args)
    for command in (["plan"], ["report"], ["analyze", str(folder / "recording.wav")]):
        assert cli.main([command[0], str(engine.root / NAME), *command[1:]]) == 1
        assert re.search(message, capsys.readouterr().err)
    plan_path.write_text(json.dumps(plan))
    results_path.write_text(json.dumps(dict(results, schema=1)))
    with pytest.raises(RuntimeError, match=r"results.json was written by an older dechatter"):
        Engine(engine.root).call("state", NAME)

    # Anything else is damage to repair by hand, named with its file.
    broken = dict(
        results, passes=[{k: v for k, v in p.items() if k != "detected"} for p in results["passes"]]
    )
    results_path.write_text(json.dumps(broken))
    with pytest.raises(
        RuntimeError,
        match=r"round-01/results.json cannot be read \(missing 'detected'\)\. Fix it by hand",
    ):
        Engine(engine.root).call("state", NAME)
    results_path.write_text("{ not json")
    with pytest.raises(RuntimeError, match=r"round-01/results.json cannot be read \(Expecting"):
        Engine(engine.root).call("state", NAME)
    results_path.write_text(json.dumps(results))
    (folder / session.SETUP_FILE).unlink()
    with pytest.raises(RuntimeError, match=r"round-01/setup.toml is missing"):
        Engine(engine.root).call("state", NAME)


def test_a_round_keeps_the_setup_it_was_planned_with_even_if_the_file_changed_since(engine):
    path = engine.root / NAME / session.SETUP_FILE
    loaded = path.read_text(encoding="utf-8")
    engine.call("state", NAME)
    # Another tab saves a setup between this one loading the session and planning.
    path.write_text(loaded.replace("safe_z_mm = 10.0", "safe_z_mm = 30.0"), encoding="utf-8")
    engine.call("plan", NAME, "same")
    copy = session.round_dir(engine.root / NAME, 1) / session.SETUP_FILE
    assert copy.read_text(encoding="utf-8") == loaded
    assert "G0 Z10" in engine.call("program", NAME, 1, "mm").splitlines()


def test_a_discarded_round_that_will_not_delete_blocks_nothing(engine, monkeypatch):
    folder = engine.root / NAME
    form = engine.call("setup_form", NAME)["values"]
    real = shutil.rmtree
    failures = []

    def fail_once(path, *args, **kwargs):
        if not failures:
            failures.append(path)
            raise OSError("storage refused")
        real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", fail_once)
    engine.call("plan", NAME, "same")
    form["program"]["safe_z_mm"] = 20.0
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["discarded_round"] == 1 and failures
    assert list(folder.glob(".discarded-*"))
    names = zipfile.ZipFile(io.BytesIO(engine.call("export_zip", NAME, "mm"))).namelist()
    assert not [n for n in names if ".discarded-" in n]

    engine.call("plan", NAME, "same")
    form["program"]["safe_z_mm"] = 25.0
    assert engine.call("save_setup", NAME, json.dumps(form))["discarded_round"] == 1
    assert not list(folder.glob(".discarded-*"))
    assert [p.name for p in folder.iterdir()] == [session.SETUP_FILE]


def test_a_round_that_was_recorded_but_not_analysed_counts_as_cut(engine):
    engine.call("plan", NAME, "same")
    with pytest.raises(RuntimeError, match="no cut"):
        engine.run_round(1, gain=0.0)
    form = engine.call("setup_form", NAME)["values"]
    form["stock"]["length_mm"] = 80.0
    with pytest.raises(RuntimeError, match=r"round 1 was cut.*stock\.length_mm"):
        engine.call("save_setup", NAME, json.dumps(form))
    assert engine.call("state", NAME)["rounds"][0]["has_recording"]


def test_a_round_keeps_the_setup_it_was_planned_from(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    path = engine.root / NAME / session.SETUP_FILE
    planned = engine.call("program", NAME, 1, "mm")
    # Edited by hand, as on the command line: the program is still the one that was cut.
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("safe_z_mm = 10.0", "safe_z_mm = 25.0"), encoding="utf-8")
    engine.sessions = browser.Sessions(engine.root)
    assert engine.call("program", NAME, 1, "mm") == planned
    engine.call("plan", NAME, "same")
    assert "G0 Z25" in engine.call("program", NAME, 2, "mm").splitlines()

    path.write_text(text.replace("flutes = 3", "flutes = 2"), encoding="utf-8")
    engine.sessions = browser.Sessions(engine.root)
    engine.run_round(2)
    with pytest.raises(RuntimeError, match=r"round 2 was cut with another tool\.flutes"):
        engine.call("plan", NAME, "same")


def test_operator_verdict_overrules_the_detector_and_feeds_the_planner(engine):
    engine.call("plan", NAME, "same")
    state = engine.run_round(1)
    tests = [p for p in state["rounds"][0]["passes"] if p["kind"] == "test"]
    stable = next(p for p in tests if p["verdict"] == "stable")
    before = sum(o["verdict"] == "unstable" for o in state["observations"])

    state = engine.call("set_verdict", NAME, 1, stable["id"], "unstable")
    changed = next(p for p in state["rounds"][0]["passes"] if p["id"] == stable["id"])
    assert changed["verdict"] == "unstable"
    assert sum(o["verdict"] == "unstable" for o in state["observations"]) == before + 1

    with pytest.raises(RuntimeError, match="no test pass 0"):
        engine.call("set_verdict", NAME, 1, 0, "unstable")


def test_unusable_recording_is_reported_and_the_round_can_be_run_again(engine):
    engine.call("plan", NAME, "same")
    with pytest.raises(RuntimeError, match="no cut"):
        engine.run_round(1, gain=0.0)

    state = engine.call("state", NAME)
    assert state["next"] == "run" and state["recording_round"] is None
    assert state["rounds"][0]["has_recording"] and not state["rounds"][0]["analysed"]
    assert engine.run_round(1)["rounds"][0]["analysed"]


def test_aborted_job_still_yields_results_and_a_cleanup_next_round(engine):
    engine.call("plan", NAME, "same")
    state = engine.run_round(1, abort_in_pass=9)
    statuses = [p["status"] for p in state["rounds"][0]["passes"]]
    assert statuses[9] == "aborted" and set(statuses[10:]) == {"not run"}

    state = engine.call("plan", NAME, "same")
    assert "cleanup" in [p["kind"] for p in state["rounds"][1]["passes"]]


def test_requests_out_of_order_are_refused(engine):
    with pytest.raises(RuntimeError, match="not recording"):
        engine.call("record_stop", NAME, memoryview(np.zeros(8, np.float32)), 48000.0, "[]")
    with pytest.raises(RuntimeError, match="round 3 does not exist"):
        engine.call("record_start", NAME, 3)
    engine.call("plan", NAME, "same")
    with pytest.raises(RuntimeError, match="has no results"):
        engine.call("plan", NAME, "same")
    engine.call("record_start", NAME, 1)
    with pytest.raises(RuntimeError, match="already recording"):
        engine.call("record_start", NAME, 1)
    with pytest.raises(RuntimeError, match="delivered no audio"):
        engine.call("record_stop", NAME, memoryview(np.zeros(0, np.float32)), 48000.0, "[]")
    assert engine.call("state", NAME)["recording_round"] is None


def test_setup_form_round_trips_and_an_invalid_one_keeps_the_old_setup(engine):
    form = engine.call("setup_form", NAME)
    assert "aluminum-6061" in form["materials"]
    good = form["values"]
    assert good["cut"]["width_max_mm"] is None, "unset limits reach the form as empty"

    bad = json.loads(json.dumps(good))
    bad["cut"]["material"] = "unobtainium"
    with pytest.raises(RuntimeError, match="unknown material"):
        engine.call("save_setup", NAME, json.dumps(bad))
    assert engine.call("setup_form", NAME)["values"] == good

    edited = json.loads(json.dumps(good))
    edited["tool"]["flutes"] = 2
    edited["tool"]["holder"] = 'ER32 "short" 🔧'
    edited["cut"]["width_max_mm"] = 3.5
    state = engine.call("save_setup", NAME, json.dumps(edited))
    assert state["summary"]["flutes"] == 2
    assert engine.call("setup_form", NAME)["values"] == edited

    with pytest.raises(RuntimeError, match="unknown keys: rpm"):
        engine.call("save_setup", NAME, json.dumps({**good, "machine": {"rpm": 1}}))


def test_setup_form_gives_the_defaults_and_what_empty_entries_become(engine):
    form = engine.call("setup_form", NAME)
    defaults = form["defaults"]
    assert defaults["program"]["dwell_s"] == 4.0 and defaults["machine"]["name"] == ""
    assert defaults["cut"]["chip_load_mm"] is None
    assert defaults["planner"]["round1_width_fractions"] == [0.1, 0.2, 0.35]
    assert "diameter_mm" not in defaults["tool"], "no default: always asked"

    values = form["values"]
    computed = engine.call("setup_preview", json.dumps(values))["cut"]
    assert computed["width_max_mm"] == round(values["tool"]["diameter_mm"] / 2, 3)
    assert computed["speed_max_rpm"] <= values["machine"]["max_rpm"]
    assert computed["chip_load_mm"] > 0
    # What the wizard holds so far, not what is saved: a wider cutter, a wider limit.
    values["tool"]["diameter_mm"] = 12.7
    assert engine.call("setup_preview", json.dumps(values))["cut"]["width_max_mm"] == 6.35
    values["cut"]["material"] = "unobtainium"
    with pytest.raises(RuntimeError, match="unknown material"):
        engine.call("setup_preview", json.dumps(values))


def test_saved_setup_pins_shown_values_and_survives_unstorable_text(engine):
    path = engine.root / NAME / session.SETUP_FILE
    with path.open("a", encoding="utf-8") as setup_file:
        setup_file.write("power_fraction = 0.6   # pinned by hand, at today's default\n")
    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["name"] = "mill \x7f"
    engine.call("save_setup", NAME, json.dumps(form))
    written = path.read_text(encoding="utf-8")
    # Shown values are pinned even at their defaults. Of the planner internals no form
    # shows, only the one the file set by hand is kept.
    assert "safe_z_mm = 10.0" in written and "power_fraction = 0.6" in written
    assert "speed_sigma_fraction" not in written
    assert engine.call("setup_form", NAME)["values"] == form

    for unusable in ([], [True], [0, 0.2], 0.2):
        bad = json.loads(json.dumps(form))
        bad["planner"]["round1_width_fractions"] = unusable
        with pytest.raises(RuntimeError, match="must list numbers"):
            engine.call("save_setup", NAME, json.dumps(bad))
        assert path.read_text(encoding="utf-8") == written

    # Half an emoji, as pasted text cut mid-character arrives from the browser.
    broken = json.loads(json.dumps(form))
    broken["tool"]["holder"] = "ER32 \ud83d"
    with pytest.raises(RuntimeError, match="broken character"):
        engine.call("save_setup", NAME, json.dumps(broken))
    assert path.read_text(encoding="utf-8") == written


def test_a_session_can_be_deleted(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    engine.call("plan", NAME, "same")
    engine.call("create", "other")
    assert engine.call("contents", NAME) == {"rounds": 2, "recordings": 1}
    assert engine.call("delete", NAME) == ["other"]
    assert not (engine.root / NAME).exists()
    with pytest.raises(RuntimeError, match="no session named"):
        engine.call("state", NAME)
    # The name is free again, for a fresh session.
    engine.call("create", NAME)
    assert engine.call("state", NAME)["rounds"] == []
    for bad in ("../sessions", "a/b", ""):
        with pytest.raises(RuntimeError, match="session names"):
            engine.call("delete", bad)
    with pytest.raises(RuntimeError, match="no session named 'missing'"):
        engine.call("delete", "missing")


def test_a_session_that_cannot_load_can_still_be_deleted(engine):
    """A 0.1.x session: its setup and rounds are never read."""
    old = engine.root / "old-011"
    for number in (1, 2):
        folder = old / f"round-{number:02d}"
        folder.mkdir(parents=True)
        (folder / session.PLAN_FILE).write_text('{"schema": 1}')
    (old / "round-01" / "recording.wav").write_bytes(b"RIFF")
    setup_011 = (Path(__file__).parent / "fixtures" / "setup-0.1.1.toml").read_text()
    (old / session.SETUP_FILE).write_text(setup_011)
    with pytest.raises(RuntimeError, match="0.1.x"):
        engine.call("state", "old-011")
    assert engine.call("contents", "old-011") == {"rounds": 2, "recordings": 1}
    assert engine.call("delete", "old-011") == [NAME]
    assert not old.exists()


def test_a_session_is_not_deleted_while_it_records(engine):
    engine.call("plan", NAME, "same")
    engine.call("record_start", NAME, 1)
    with pytest.raises(RuntimeError, match="round 1 of 'al-10mm' is being recorded"):
        engine.call("delete", NAME)
    assert (engine.root / NAME / "round-01").exists()
    with pytest.raises(RuntimeError, match="delivered no audio"):
        engine.call("record_stop", NAME, memoryview(np.zeros(0, np.float32)), 48000.0, "[]")
    assert engine.call("delete", NAME) == []


def test_a_reset_discards_every_round_and_frees_the_setup(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    engine.call("plan", NAME, "same")
    form = engine.call("setup_form", NAME)["values"]
    form["tool"]["flutes"] = 2
    with pytest.raises(RuntimeError, match="start a new session"):
        engine.call("save_setup", NAME, json.dumps(form))
    setup_before = (engine.root / NAME / session.SETUP_FILE).read_bytes()

    assert engine.call("contents", NAME) == {"rounds": 2, "recordings": 1}
    state = engine.call("reset", NAME)
    assert state["rounds"] == [] and state["next"] == "plan" and state["best"] == []
    assert sorted(p.name for p in (engine.root / NAME).iterdir()) == [session.SETUP_FILE]
    assert (engine.root / NAME / session.SETUP_FILE).read_bytes() == setup_before
    # Nothing has been cut any more: the setup may change, and planning starts at round 1.
    assert engine.call("save_setup", NAME, json.dumps(form))["summary"]["flutes"] == 2
    assert engine.call("plan", NAME, "same")["rounds"][0]["round"] == 1


def test_a_reset_is_refused_while_recording_and_undone_if_it_fails(engine, monkeypatch):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    engine.call("plan", NAME, "same")
    engine.call("record_start", NAME, 2)
    with pytest.raises(RuntimeError, match="round 2 of 'al-10mm' is being recorded; reset"):
        engine.call("reset", NAME)
    with pytest.raises(RuntimeError, match="delivered no audio"):
        engine.call("record_stop", NAME, memoryview(np.zeros(0, np.float32)), 48000.0, "[]")

    before = files(engine.root / NAME)
    real = Path.rename

    def fail_on_second(self, target):
        if self.name == "round-02":
            raise OSError("storage refused")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", fail_on_second)
    with pytest.raises(RuntimeError, match="could not reset al-10mm: storage refused"):
        engine.call("reset", NAME)
    monkeypatch.undo()
    assert files(engine.root / NAME) == before
    assert not list((engine.root / NAME).glob(".discarded-*"))
    assert [r["round"] for r in engine.call("state", NAME)["rounds"]] == [1, 2]

    # A round that cannot be put back either stays in .reset-*, named in the error.
    def fail_back(self, target):
        if self.name == "round-02" or (self.name == "round-01" and ".reset-" in str(self)):
            raise OSError("storage refused")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", fail_back)
    with pytest.raises(RuntimeError, match=r"round-01 could not be put back and are in .*\.reset-"):
        engine.call("reset", NAME)
    monkeypatch.undo()
    folder = engine.root / NAME
    kept = list(folder.glob(".reset-*"))
    assert len(kept) == 1 and (kept[0] / "round-01" / "recording.wav").exists()
    original = (kept[0] / "round-01" / session.PLAN_FILE).read_bytes()

    # Reset again and plan a new round 1: the name is reused, yet no sweep takes the
    # original, and Export carries it.
    assert engine.call("reset", NAME)["rounds"] == []
    engine.call("plan", NAME, "same")
    session.sweep_discarded(folder)
    Engine(engine.root).call("setup_form", NAME)  # opening the session sweeps too
    assert (kept[0] / "round-01" / session.PLAN_FILE).read_bytes() == original
    names = zipfile.ZipFile(io.BytesIO(engine.call("export_zip", NAME, "mm"))).namelist()
    assert f"{NAME}/{kept[0].name}/round-01/recording.wav" in names
    assert not list(folder.glob(".discarded-*"))


def test_a_finished_reset_that_could_not_delete_its_rounds_is_swept_later(engine, monkeypatch):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    folder = engine.root / NAME
    real = Path.rename

    def no_discard(self, target):
        if self.name.startswith(".reset-"):
            raise OSError("storage refused")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", no_discard)
    assert engine.call("reset", NAME)["rounds"] == []
    left = [p for p in folder.glob(".reset-*") if p.is_dir()]
    assert len(left) == 1 and (left[0] / "round-01" / "recording.wav").exists()
    assert left[0].with_name(left[0].name + ".done").exists()
    # Meanwhile Export leaves the discarded rounds out.
    names = zipfile.ZipFile(io.BytesIO(engine.call("export_zip", NAME, "mm"))).namelist()
    assert not [n for n in names if ".reset-" in n or ".discarded-" in n]

    monkeypatch.undo()
    Engine(engine.root).call("setup_form", NAME)  # opening the session sweeps
    assert sorted(p.name for p in folder.iterdir()) == [session.SETUP_FILE]

    # An empty one, left when everything was put back but its removal failed, goes too.
    (folder / ".reset-0badc0de").mkdir()
    session.sweep_discarded(folder)
    assert sorted(p.name for p in folder.iterdir()) == [session.SETUP_FILE]


def test_a_reset_that_cannot_mark_its_folder_discards_it_or_says_where(engine, monkeypatch):
    folder = engine.root / NAME
    real_touch, real_rename = Path.touch, Path.rename

    def no_marker(self, *args, **kwargs):
        if self.name.endswith(".done"):
            raise OSError("storage refused")
        return real_touch(self, *args, **kwargs)

    # Unmarked but discarded right away: a clean reset.
    engine.call("plan", NAME, "same")
    monkeypatch.setattr(Path, "touch", no_marker)
    assert engine.call("reset", NAME)["rounds"] == []
    assert sorted(p.name for p in folder.iterdir()) == [session.SETUP_FILE]

    # Nor can it be renamed: the reset says where the rounds are.
    def no_discard(self, target):
        if self.name.startswith(".reset-"):
            raise OSError("storage refused")
        return real_rename(self, target)

    engine.call("plan", NAME, "same")
    monkeypatch.setattr(Path, "rename", no_discard)
    with pytest.raises(
        RuntimeError, match=r"discarded rounds are still in .*\.reset-.*delete that"
    ):
        engine.call("reset", NAME)
    monkeypatch.undo()
    left = [p for p in folder.glob(".reset-*") if p.is_dir()]
    assert len(left) == 1 and (left[0] / "round-01").exists()


def test_a_marker_that_will_not_go_does_not_stop_the_session_opening(engine, monkeypatch):
    folder = engine.root / NAME
    (folder / ".reset-0ddba11.done").touch()
    real = Path.unlink

    def refuse(self, *args, **kwargs):
        if self.name.endswith(".done"):
            raise OSError("storage refused")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert Engine(engine.root).call("state", NAME)["rounds"] == []
    monkeypatch.undo()
    session.sweep_discarded(folder)
    assert sorted(p.name for p in folder.iterdir()) == [session.SETUP_FILE]


def test_a_reset_goes_ahead_when_the_report_cannot_be_removed(engine, monkeypatch):
    engine.call("plan", NAME, "same")
    report = engine.root / NAME / "report.html"
    report.write_text("<!doctype html>")
    real = Path.unlink

    def refuse(self, *args, **kwargs):
        if self.name == "report.html":
            raise OSError("busy")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert engine.call("reset", NAME)["rounds"] == []
    monkeypatch.undo()
    assert report.exists() and not list((engine.root / NAME).glob("round-*"))
    assert not list((engine.root / NAME).glob(".discarded-*"))


def test_reset_and_export_go_by_whether_the_setup_loads(engine):
    assert engine.call("setup_status", NAME) == {"status": "loads", "reason": ""}
    # Rounds that do not load leave the setup loading: the session can be exported and reset.
    engine.call("plan", NAME, "same")
    (session.round_dir(engine.root / NAME, 1) / session.SETUP_FILE).unlink()
    fresh = Engine(engine.root)
    with pytest.raises(RuntimeError, match="setup.toml is missing"):
        fresh.call("state", NAME)
    assert fresh.call("setup_status", NAME)["status"] == "loads"
    assert (
        f"{NAME}/setup.toml"
        in zipfile.ZipFile(io.BytesIO(fresh.call("export_zip", NAME, "mm"))).namelist()
    )
    assert fresh.call("reset", NAME)["rounds"] == []


def test_a_session_whose_setup_does_not_load_is_not_reset(engine):
    old = engine.root / "old-011"
    (old / "round-01").mkdir(parents=True)
    (old / "round-01" / session.PLAN_FILE).write_text('{"schema": 1}')
    setup_011 = (Path(__file__).parent / "fixtures" / "setup-0.1.1.toml").read_text()
    (old / session.SETUP_FILE).write_text(setup_011)
    before = files(old)
    status = engine.call("setup_status", "old-011")
    assert status["status"] == "refused" and "0.1.x" in status["reason"]
    with pytest.raises(RuntimeError, match="0.1.x"):
        engine.call("reset", "old-011")
    assert cli.main(["reset", str(old), "--yes"]) == 1
    assert files(old) == before


def test_the_command_line_resets_a_session_after_asking(engine, monkeypatch, capsys):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    folder = engine.root / NAME
    monkeypatch.setattr("builtins.input", lambda prompt: print(prompt) or "n")
    assert cli.main(["reset", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "Discard all 1 rounds and 1 recordings" in out and "nothing changed" in out
    assert (folder / "round-01").exists()

    def no_answer(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_answer)
    assert cli.main(["reset", str(folder)]) == 1
    assert "nothing changed" in capsys.readouterr().out
    assert (folder / "round-01").exists()

    def refuse(self, target):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "rename", refuse)
    assert cli.main(["reset", str(folder), "--yes"]) == 1
    assert "could not reset al-10mm: read-only file system" in capsys.readouterr().err
    monkeypatch.undo()
    assert (folder / "round-01").exists()
    (folder / "report.html").write_text("<!doctype html>")
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert cli.main(["reset", str(folder)]) == 0
    assert not (folder / "round-01").exists() and not (folder / "report.html").exists()
    engine.call("plan", NAME, "same")
    assert cli.main(["reset", str(folder), "--yes"]) == 0
    assert "discarded 1 rounds" in capsys.readouterr().out
    assert sorted(p.name for p in folder.iterdir()) == [session.SETUP_FILE]


def test_session_names_cannot_escape_the_store(engine):
    for name in ("../evil", "a/b", "", ".hidden"):
        with pytest.raises(RuntimeError, match="session names"):
            engine.call("create", name)
    with pytest.raises(RuntimeError, match="already exists"):
        engine.call("create", NAME)


def test_a_chip_load_sweep_follows_convergence_and_stays_off_the_map(engine, setup):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    with pytest.raises(RuntimeError, match="has not converged"):
        engine.call("plan_sweep", NAME, "same")

    state = converge(engine, setup)
    assert state["next"] == "sweep" and state["sweep"] is None
    best, cuts = state["best"][0], len(state["observations"])
    state = engine.call("plan_sweep", NAME, "fresh")
    sweep = state["rounds"][-1]
    assert sweep["planner"] == "sweep" and state["next"] == "run"
    tests = [p for p in sweep["passes"] if p["kind"] == "test"]
    assert 4 <= len(tests) <= 5
    assert {(p["rpm"], p["width_mm"]) for p in tests} == {(best["rpm"], best["width_mm"])}
    assert sweep["depth_mm"] == best["depth_mm"]
    loads = [p["chip_load_mm"] for p in tests]
    session_load = sweep["passes"][0]["chip_load_mm"]  # the truing pass runs at it
    assert loads == sorted(loads) and loads[0] == pytest.approx(0.6 * session_load, abs=1e-4)
    assert loads[-1] <= 1.7 * session_load + 1e-4
    for p in tests:
        assert p["feed_mm_min"] == pytest.approx(
            p["chip_load_mm"] * setup.tool.flutes * p["rpm"], abs=0.1
        )
    program = engine.call("program", NAME, sweep["round"], "mm")
    assert {f"F{p['feed_mm_min']:g}" for p in tests} <= set(program.split())

    state = engine.run_round(sweep["round"])
    assert state["next"] == "converged"
    assert len(state["observations"]) == cuts and state["best"][0] == best
    found = state["sweep"]
    assert found["chip_load_mm"] == loads[-1], "the simulated machine ignores chip load"
    assert found["feed_mm_min"] == round(loads[-1] * setup.tool.flutes * best["rpm"])

    # Chatter at the third chip load: the result is the one before it.
    third = tests[2]["id"]
    state = engine.call("set_verdict", NAME, sweep["round"], third, "unstable")
    assert state["sweep"]["chip_load_mm"] == loads[1]
    page = engine.call("report_html", NAME, "mm")
    assert "Chip-load sweep, round" in page and "chip-load sweep, planned" in page


def cap_power(engine, mrr_cm3_min: float, **planner) -> dict:
    """Set the spindle power so that the power cap is this removal rate."""
    path = engine.root / NAME / session.SETUP_FILE
    limit = envelope(load_setup(path)).mrr_limit_mm3_min / 1000
    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["spindle_power_hp"] *= mrr_cm3_min / limit
    form["planner"].update(planner)
    return engine.call("save_setup", NAME, json.dumps(form))


def test_a_lowered_power_cap_leaves_heavier_cuts_unrecommended(engine, setup):
    best = converge(engine, setup)["best"][0]
    state = cap_power(engine, 0.8 * best["mrr_cm3_min"])
    assert state["best"] and best not in state["best"]
    assert all(b["mrr_cm3_min"] <= 0.8 * best["mrr_cm3_min"] + 0.01 for b in state["best"])
    page = engine.call("report_html", NAME, "mm")
    shares = [int(share) for share in re.findall(r"hp \((\d+)%\)", page)]
    assert shares and max(shares) <= 60, "under the cap: at most the power fraction"
    assert state["next"] == "sweep"
    assert engine.call("plan_sweep", NAME, "fresh")["rounds"][-1]["planner"] == "sweep"


def test_no_sweep_is_offered_when_the_power_cap_leaves_it_no_room(engine, setup, capsys):
    best = converge(engine, setup)["best"][0]
    # The sweep starts above the session's chip load, and the cap sits at the best cut.
    state = cap_power(engine, best["mrr_cm3_min"] * 1.001, chip_load_sweep_low=1.2)
    assert state["best"][0] == best
    assert state["next"] == "converged" and state["sweep"] is None
    with pytest.raises(RuntimeError, match="leaves no chip load to sweep"):
        engine.call("plan_sweep", NAME, "fresh")
    assert cli.main(["plan", str(engine.root / NAME)]) == 0
    out = capsys.readouterr().out
    assert "converged" in out and "--sweep" not in out


def test_the_sweep_result_stays_within_a_lowered_power_cap(engine, setup, capsys):
    best = converge(engine, setup)["best"][0]
    sweep = engine.call("plan_sweep", NAME, "fresh")["rounds"][-1]
    engine.run_round(sweep["round"])
    loads = [p["chip_load_mm"] for p in sweep["passes"] if p["kind"] == "test"]
    # Passes at 0.6, 0.875, 1.15, 1.425 and 1.7 times the chip load, all stable: under a cap
    # at 1.1 times the best cut, the second is the result.
    state = cap_power(engine, 1.1 * best["mrr_cm3_min"])
    assert state["best"][0] == best and state["next"] == "converged"
    assert state["sweep"]["chip_load_mm"] == loads[1]
    assert state["sweep"]["mrr_cm3_min"] <= 1.1 * best["mrr_cm3_min"]
    assert cli.main(["report", str(engine.root / NAME)]) == 0
    assert f"stable up to {loads[1]:g} mm/tooth" in capsys.readouterr().out
    recommended = engine.call("report_html", NAME, "mm").split("<h2>Stability map")[0]
    assert f"{loads[1]:.3f} mm" in recommended and f"{loads[2]:.3f} mm" not in recommended

    # Under a cap below the lightest pass, the sweep has no result.
    state = cap_power(engine, 0.5 * best["mrr_cm3_min"])
    assert state["sweep"] is None and "Chip-load sweep, round" not in engine.call(
        "report_html", NAME, "mm"
    )


def converged_report(engine, setup, units: str) -> str:
    form = engine.call("setup_form", NAME)["values"]
    form["machine"]["spindle"] = "manual"
    engine.call("save_setup", NAME, json.dumps(form))
    state = converge(engine, setup, speed_change_s=12.0)
    first = next(p for p in state["rounds"][0]["passes"] if p["kind"] == "test")
    flipped = "stable" if first["verdict"] == "unstable" else "unstable"
    engine.call("set_verdict", NAME, 1, first["id"], flipped)
    return engine.call("report_html", NAME, units)


def test_the_report_holds_the_results_on_one_offline_page(engine, setup):
    page = converged_report(engine, setup, "in")
    state = engine.call("state", NAME)
    best = state["best"][0]

    # One file: no scripts, stylesheets, images or fonts from anywhere else.
    assert not re.search(r"<script|<link|<img|src=|url\(|@import|https?:", page)
    assert "@page" in page and "@media print" in page
    sections = ["Recommended cuts", "Stability map", "Round log", "Setup"]
    positions = [page.index(f"<h2>{title}</h2>") for title in sections]
    assert positions == sorted(positions) and page.index(NAME) < positions[0]
    # Inches first, mm beside; the manual spindle's speed to set.
    width_in = best["width_mm"] / 25.4
    assert f"{width_in:.4f} in ({best['width_mm']:.2f} mm)" in page
    assert "Set the spindle to (rpm)" in page and f"<td>{best['rpm']}</td>" in page
    assert page.count("<svg") == len({r["depth_mm"] for r in state["rounds"]})
    assert page.count("<td>by hand</td>") == 1
    rounds = len(state["rounds"])
    assert page.count("<h3>Round ") == rounds
    assert f"planned {date.today().isoformat()}" in page
    assert "spindle_power_hp" in page and f"dechatter {dechatter.__version__}" in page

    millimetres = engine.call("report_html", NAME, "mm")
    assert f"{best['width_mm']:.2f} mm" in millimetres and " in (" not in millimetres


def test_the_command_line_writes_the_report(engine, tmp_path, capsys):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    out = tmp_path / "report.html"
    assert cli.main(["report", str(engine.root / NAME), "--html", str(out), "--units", "in"]) == 0
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert " in (" in out.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        cli.main(["report", str(engine.root / NAME), "--units", "in"])
    assert refused.value.code == 2
    assert "--units applies to the HTML report only" in capsys.readouterr().err


def test_the_widget_gets_the_round_as_blocks_in_the_machine_units(engine):
    engine.call("plan", NAME, "same")
    blocks = engine.call("steps", NAME, 1, "in")
    assert blocks[0]["step"] == "setup" and blocks[0]["lines"][0].startswith("G20")
    cut = next(b for b in blocks if b["step"] == "cut")
    assert cut["lines"][0].startswith("G1 X") and cut["target"]["z"] == pytest.approx(
        -5.0, abs=0.003
    )
    assert blocks[-1]["step"] == "end"


def test_export_round_trips_into_another_browser(engine, tmp_path):
    engine.call("plan", NAME, "same")
    before = engine.run_round(1)
    archive = engine.call("export_zip", NAME, "mm")
    names = zipfile.ZipFile(io.BytesIO(archive)).namelist()
    assert f"{NAME}/report.html" in names

    other = Engine(tmp_path / "elsewhere")
    assert other.call("import_zip", memoryview(archive)) == NAME
    assert other.call("names") == [NAME]
    assert other.call("state", NAME) == before
    with pytest.raises(RuntimeError, match="already exists"):
        other.call("import_zip", memoryview(archive))
    # The report shipped in an export is made afresh, not carried twice.
    again = zipfile.ZipFile(io.BytesIO(other.call("export_zip", NAME, "in"))).namelist()
    assert again.count(f"{NAME}/report.html") == 1


def test_import_refuses_archives_that_are_not_a_session(engine):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside/setup.toml", "x")
    with pytest.raises(RuntimeError, match="not a dechatter session export"):
        engine.call("import_zip", memoryview(buffer.getvalue()))
    assert not (engine.root.parent / "outside").exists()
