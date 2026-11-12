"""The widget's engine API: the same calls the browser worker makes, against a real folder."""

from __future__ import annotations

import io
import json
import re
import zipfile

import numpy as np
import pytest
import synth
from scipy import signal

from dechatter import browser, session
from dechatter.app import TIMELINE_FILE
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
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    # Only a setup file edited by hand can ask for it; the wizard refuses such a change.
    path = engine.root / NAME / session.SETUP_FILE
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("axial_depth_mm = 5.0", "axial_depth_mm = 8.0"), encoding="utf-8")
    engine.sessions = browser.Sessions(engine.root)
    with pytest.raises(RuntimeError, match="only flat down to 5 mm"):
        engine.call("plan", NAME, "same")
    assert engine.call("plan", NAME, "fresh")["rounds"][-1]["depth_mm"] == 8.0


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


def test_a_setup_change_discards_the_round_planned_from_the_old_one(engine):
    engine.call("plan", NAME, "same")
    form = engine.call("setup_form", NAME)["values"]
    assert engine.call("save_setup", NAME, json.dumps(form))["rounds"], "no change, round kept"

    form["machine"]["spindle"] = "manual"
    form["machine"]["max_rpm"] = 2800
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["rounds"] == [] and state["discarded_round"] == 1 and state["next"] == "plan"
    assert not session.round_dir(engine.root / NAME, 1).exists()

    state = engine.call("plan", NAME, "same")
    assert state["discarded_round"] is None
    assert max(p["rpm"] for p in state["rounds"][0]["passes"]) <= 2800
    program = engine.call("program", NAME, 1, "mm").splitlines()
    assert not [line for line in program if re.match(r"M3\b|S\d", line)]
    assert [line for line in program if line.startswith("M0")]


def test_a_setup_change_after_a_round_was_cut_is_refused_but_planner_settings_may_change(engine):
    engine.call("plan", NAME, "same")
    engine.run_round(1)
    folder = engine.root / NAME
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    form = engine.call("setup_form", NAME)["values"]

    changed = json.loads(json.dumps(form))
    changed["tool"]["flutes"] = 2
    changed["tool"]["diameter_mm"] = 12.7
    with pytest.raises(RuntimeError, match=r"start a new session"):
        engine.call("save_setup", NAME, json.dumps(changed))
    assert {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before
    assert engine.call("setup_form", NAME)["values"] == form

    form["planner"]["batch_size"] = 6
    state = engine.call("save_setup", NAME, json.dumps(form))
    assert state["rounds"][0]["analysed"] and state["discarded_round"] is None


def test_a_round_that_was_recorded_but_not_analysed_counts_as_cut(engine):
    engine.call("plan", NAME, "same")
    with pytest.raises(RuntimeError, match="no cut"):
        engine.run_round(1, gain=0.0)
    form = engine.call("setup_form", NAME)["values"]
    form["stock"]["width_mm"] = 40.0
    with pytest.raises(RuntimeError, match=r"round 1 was cut.*\[stock\]"):
        engine.call("save_setup", NAME, json.dumps(form))
    assert engine.call("state", NAME)["rounds"][0]["has_recording"]


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


def test_export_round_trips_into_another_browser(engine, tmp_path):
    engine.call("plan", NAME, "same")
    before = engine.run_round(1)
    archive = engine.call("export_zip", NAME)

    other = Engine(tmp_path / "elsewhere")
    assert other.call("import_zip", memoryview(archive)) == NAME
    assert other.call("names") == [NAME]
    assert other.call("state", NAME) == before
    with pytest.raises(RuntimeError, match="already exists"):
        other.call("import_zip", memoryview(archive))


def test_import_refuses_archives_that_are_not_a_session(engine):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside/setup.toml", "x")
    with pytest.raises(RuntimeError, match="not a dechatter session export"):
        engine.call("import_zip", memoryview(buffer.getvalue()))
    assert not (engine.root.parent / "outside").exists()
