"""The widget's engine API: the same calls the browser worker makes, against a real folder."""

from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import date

import numpy as np
import pytest
import synth
from scipy import signal

import dechatter
from dechatter import browser, cli, session
from dechatter.app import TIMELINE_FILE, App
from dechatter.plan import Plan

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
        if state["next"] == "converged":
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


def test_a_session_whose_rounds_have_no_setup_copy_still_opens(engine):
    """Rounds without a setup copy, under a setup in today's format, open for viewing.

    (A setup still in 0.1.1's format, with spindle_power_w, does not load at all.)
    """
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    (session.round_dir(engine.root / NAME, 1) / session.SETUP_FILE).unlink()
    engine.sessions = browser.Sessions(engine.root)

    state = engine.call("state", NAME)
    assert state["rounds"][0]["analysed"] and state["rounds"][0]["program"].endswith(".nc")
    tests = [p for p in state["rounds"][0]["passes"] if p["kind"] == "test"]
    engine.call("set_verdict", NAME, 1, tests[0]["id"], "unstable")
    assert engine.call("export_zip", NAME, "mm")
    older = "planned by an older dechatter"
    with pytest.raises(RuntimeError, match=older):
        engine.call("plan", NAME, "same")
    form = engine.call("setup_form", NAME)["values"]
    form["program"]["safe_z_mm"] = 20.0
    with pytest.raises(RuntimeError, match=older):
        engine.call("save_setup", NAME, json.dumps(form))
    with pytest.raises(RuntimeError, match=older):
        engine.call("program", NAME, 1, "mm")


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
    assert statuses[9] == "aborted" and set(statuses[10:]) == {"missing"}

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
    assert "2 flutes" in state["summary"]
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


def test_session_names_cannot_escape_the_store(engine):
    for name in ("../evil", "a/b", "", ".hidden"):
        with pytest.raises(RuntimeError, match="session names"):
            engine.call("create", name)
    with pytest.raises(RuntimeError, match="already exists"):
        engine.call("create", NAME)


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
