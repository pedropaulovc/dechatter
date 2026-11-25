"""Placing cuts from the widget's run timeline, and classifying real 1018 steel shop cuts."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import synth

from dechatter import audio, detect, session
from dechatter.app import App
from dechatter.config import load_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import (
    Face,
    Made,
    PassKind,
    Plan,
    PlannerKind,
    Top,
    assemble,
    envelope,
    facing_rows,
    feed_mm_min,
    grid_tests,
)
from dechatter.session import PassResult, PassStatus, SpindleFit

FIXTURES = Path(__file__).parent / "fixtures" / "v8"
STEEL_FIXTURES = Path(__file__).parent / "fixtures" / "steel_1018"


def _mulaw_decode(u8: np.ndarray, peak: float, mu: float) -> np.ndarray:
    comp = u8.astype(np.float32) / 127.5 - 1.0
    norm = np.sign(comp) * (np.expm1(np.abs(comp) * np.log1p(mu)) / mu)
    return (norm * peak).astype(np.float32)


def test_real_1018_steel_widget_passes_are_classified_and_spindle_speeds_anchored():
    meta = json.loads((STEEL_FIXTURES / "cases.json").read_text(encoding="utf-8"))
    rate, mu = int(meta["rate"]), float(meta["mu"])
    with np.load(STEEL_FIXTURES / "clips.npz") as archive:
        for case in meta["cases"]:
            name = case["name"]
            cut = _mulaw_decode(archive[f"{name}_cut"], case["cut_peak"], mu)
            air = _mulaw_decode(archive[f"{name}_air"], case["air_peak"], mu)
            reading = detect.classify(cut, air, rate, case["rpm"], case["flutes"])
            expected = Verdict(case["verdict"])
            assert reading.verdict is expected, f"{name}: got {reading}"
            result = PassResult(
                0,
                PassKind.TEST,
                case["rpm"],
                case["width_mm"],
                PassStatus.ANALYSED,
                spindle_hz=reading.spindle_hz,
            )
            assert session.spindle_fit(result) is SpindleFit.NEAR, f"{name}: {reading.spindle_hz}"


def schedule(
    plan: Plan, rapid_s: float = 2.0, facing: synth.Facing = synth.Facing.PAUSED
) -> tuple[list, list[tuple[float, float]]]:
    """A timeline as the widget writes it for synth.record(plan, facing=facing), and the true
    cut times."""
    t, timeline, contact = 3.0, [{"t": 1000.0, "event": "record"}], []
    faced = {leg.passes[0].id: leg.face_s for leg in plan.legs if leg.reface_mm > 0}
    for p in plan.passes:
        if facing is synth.Facing.HEARD and p.id in faced:
            t += rapid_s + faced[p.id]
        dwell = (t + rapid_s, t + rapid_s + plan.dwell_s)
        start = t + rapid_s + plan.dwell_s + p.lead_in_s
        contact.append((start, start + p.expected_cut_s))
        timeline.append(
            {
                "t": 0,
                "event": "dwell",
                "pass": p.id,
                "start": 1000 + dwell[0],
                "end": 1000 + dwell[1],
            }
        )
        move = (start - p.lead_in_s, start + p.expected_cut_s + p.lead_in_s)
        timeline.append(
            {
                "t": 0,
                "event": "cut",
                "pass": p.id,
                "start": 1000 + move[0],
                "end": 1000 + move[1],
                "holds": [],
            }
        )
        t = start + p.expected_cut_s
    return timeline, contact  # fmt: skip


def test_windows_the_widget_recorded_say_where_each_cut_is(setup):
    from test_plan import round1

    plan = round1(setup)
    timeline, contact = schedule(plan)
    placed = session.placements_from_timeline(plan, timeline)
    for index, (start, end) in enumerate(contact):
        assert placed[index].cut.start_s == pytest.approx(start)
        assert placed[index].cut.end_s == pytest.approx(end)
        assert placed[index].air.duration_s == pytest.approx(plan.dwell_s)

    recording = synth.record(plan, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    for p, result in zip(plan.passes, results.passes, strict=True):
        assert result.status is PassStatus.ANALYSED
        truth = synth.DEFAULT_MACHINE.chatters(p.rpm, p.width_mm, plan.flutes, p.depth_mm)
        assert (result.verdict is Verdict.UNSTABLE) == truth, p.id

    # The run stopped in pass 5: a cut cut short, a held cut analysed over its longest
    # stretch without the hold, and passes never reached are "not run" without any guessing
    # from the sound.
    stopped = [e for e in timeline if e.get("pass", 0) <= 5]
    cut5 = next(e for e in stopped if e["event"] == "cut" and e["pass"] == 5)
    cut5.update(aborted=True, end=cut5["start"] + plan.passes[5].lead_in_s + 2.0)
    cut3 = next(e for e in stopped if e["event"] == "cut" and e["pass"] == 3)
    held_at = cut3["start"] + plan.passes[3].lead_in_s + 1.5
    cut3["holds"] = [[held_at, held_at + 30]]
    cut3["end"] += 30  # resumed: the move ends that much later
    placed = session.placements_from_timeline(plan, stopped)
    longer = max(1.5, plan.passes[3].expected_cut_s - 1.5)
    assert placed[3].cut.duration_s == pytest.approx(longer)
    assert placed[3].status is PassStatus.ANALYSED
    assert placed[5].cut.duration_s == pytest.approx(2.0) and placed[5].status is PassStatus.ABORTED
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), stopped)
    assert results.passes[5].status is PassStatus.ABORTED
    assert {r.status for r in results.passes[6:]} == {PassStatus.NOT_RUN}


def test_a_spindle_speed_found_at_the_edge_of_its_search_is_flagged():
    def result(measured_rpm: float) -> PassResult:
        return PassResult(1, "test", 2250, 3.0, PassStatus.ANALYSED, spindle_hz=measured_rpm / 60)

    assert session.spindle_fit(result(2260)) is SpindleFit.NEAR
    # 2180 rpm heard in a 2250 rpm pass sits 3 % off: inside the search, not at its edge.
    assert session.spindle_fit(result(2180)) is SpindleFit.NEAR
    assert session.spindle_fit(result(2250 * 0.951)) is SpindleFit.EDGE
    assert session.spindle_fit(result(2250 * 1.05)) is SpindleFit.EDGE
    missing = PassResult(1, "test", 2250, 3.0, PassStatus.NOT_RUN)
    assert session.spindle_fit(missing) is None


def held(timeline: list, pass_id: int, offsets: tuple[float, float]) -> None:
    cut = next(e for e in timeline if e["event"] == "cut" and e["pass"] == pass_id)
    cut["holds"] = [[cut["start"] + offsets[0], cut["start"] + offsets[1]]]


def test_a_hold_is_cut_out_of_the_window_wherever_it_falls(setup):
    from test_plan import round1

    plan = round1(setup)
    timeline, contact = schedule(plan)
    lead = plan.passes[2].lead_in_s
    # A hold during the lead-in delays contact by as long: the cut after it is whole.
    held(timeline, 2, (0.2 * lead, 0.6 * lead))
    delay = 0.4 * lead
    cut2 = next(e for e in timeline if e["event"] == "cut" and e["pass"] == 2)
    cut2["end"] += delay
    # A hold mid-cut: the longer of the two stretches around it.
    lead4 = plan.passes[4].lead_in_s
    held(timeline, 4, (lead4 + 1.0, lead4 + 2.0))
    placed = session.placements_from_timeline(plan, timeline)
    assert placed[2].cut.start_s == pytest.approx(contact[2][0] + delay)
    assert placed[2].cut.end_s == pytest.approx(contact[2][1] + delay)
    cut4 = placed[4].cut
    assert cut4.start_s == pytest.approx(contact[4][0] + 2.0) and cut4.end_s == pytest.approx(
        contact[4][1]
    )

    recording = synth.record(plan, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    assert results.passes[2].status is PassStatus.ANALYSED and results.passes[2].verdict is not None


def test_a_cut_stopped_just_after_contact_keeps_its_step(setup):
    from test_plan import round1

    plan = round1(setup)
    timeline, _ = schedule(plan)
    stopped = [e for e in timeline if e.get("pass", 0) <= 3]
    cut3 = next(e for e in stopped if e["event"] == "cut" and e["pass"] == 3)
    cut3.update(aborted=True, end=cut3["start"] + plan.passes[3].lead_in_s + 0.2)
    recording = synth.record(plan, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), stopped)
    # Too short to hear, but the cutter reached the face: aborted, no verdict.
    assert results.passes[3].status is PassStatus.ABORTED and results.passes[3].verdict is None
    face = session.face_after(session.Round(None, plan, results))
    assert face.remnant_mm == plan.passes[3].width_mm

    # Stopped during the lead-in: the cutter never met the stock.
    cut3["end"] = cut3["start"] + plan.passes[3].lead_in_s / 2
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), stopped)
    assert results.passes[3].status is PassStatus.NOT_RUN


def stopped_in_lead_in(plan: Plan, timeline: list, index: int) -> list:
    """The run held during pass `index`'s lead-in, cutter still in the air, then stopped:
    the window outlasts the lead-in in wall time, but the cutter never reached the stock."""
    p = plan.passes[index]
    stopped = [e for e in timeline if e.get("pass", 0) <= p.id]
    cut = next(e for e in stopped if e["event"] == "cut" and e["pass"] == p.id)
    moved = 0.5 * p.lead_in_s
    cut.update(aborted=True, end=cut["start"] + moved + 10.0)
    cut["holds"] = [[cut["start"] + moved, cut["end"]]]
    return stopped


def test_a_hold_before_contact_then_stop_cuts_nothing():
    plan = Plan.load(FIXTURES / "round-02" / "plan.json")
    timeline, _ = schedule(plan)
    recording = synth.record(plan, seed=3)
    # The first cut: nothing was cut, so the round stays to be run again.
    with pytest.raises(audio.AudioError, match="ended before its first cut"):
        session.analyze(
            plan, recording, synth.RATE, "r.wav", Thresholds(),
            stopped_in_lead_in(plan, timeline, 0),
        )  # fmt: skip
    # A later cut: not run, and the face is where the pass before it left it, with no step.
    stopped = stopped_in_lead_in(plan, timeline, 2)
    assert 2 not in session.placements_from_timeline(plan, stopped)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), stopped)
    assert results.passes[2].status is PassStatus.NOT_RUN
    face = session.face_after(session.Round(None, plan, results))
    widths = [p.width_mm for p in plan.passes]
    start = plan.legs[0].face_y_start_mm
    assert (face.y_mm, face.remnant_mm) == pytest.approx((start + sum(widths[:2]), 0.0))


@pytest.mark.parametrize(
    "ended",
    [
        "stopped by the operator",
        "error:22 (Undefined feed rate)",
        "the connection to cncjs or the machine was lost",
    ],
)
def test_a_run_that_ended_before_its_first_cut_leaves_the_round_to_run_again(ended, setup):
    from test_plan import round1

    plan = round1(setup)
    timeline = [
        {"t": 1000.0, "event": "record"},
        {"t": 1002.0, "event": "run", "outcome": "stopped", "reason": ended},
    ]
    recording = synth.record(plan, seed=3)[: synth.RATE * 3]
    with pytest.raises(audio.AudioError, match="ended before its first cut"):
        session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)


def test_a_cut_no_louder_than_the_spindle_in_the_air_is_not_called_stable(setup):
    """A face planned where there was none (fresh stock picked, none mounted) cuts air,
    and the air sounds stable. Those passes say no cut was heard instead."""
    from test_plan import round1

    plan = round1(setup)
    timeline, _ = schedule(plan)
    lost = frozenset({3, 4})
    recording = synth.record(plan, in_air=lost)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    for p in results.passes:
        if p.id in lost:
            assert p.status is PassStatus.QUIET and p.verdict is None, p
            assert p.over_air_db < session.QUIET_DB
        else:
            assert p.status is PassStatus.ANALYSED and p.verdict is not None, p
            assert p.over_air_db > session.QUIET_DB
    # The face still moved through them: the next round starts behind them.
    face = session.face_after(session.Round(Path("r"), plan, results))
    assert face.y_mm == pytest.approx(plan.legs[-1].face_y_end_mm)


@pytest.mark.parametrize(
    "ending",
    [
        "went silent",
        "stopped",
    ],
)
def test_a_recording_that_ends_before_the_last_pass_still_moves_the_face(ending, setup, tmp_path):
    """Found in a widget run: the microphone stopped before the last passes. The widget saw
    the machine cut them, so their face moved, though there is nothing of them to hear."""
    from test_plan import round1

    plan = round1(setup)
    timeline, contact = schedule(plan)
    recording = synth.record(plan, seed=3)
    end = int((contact[-2][1] + 0.5) * synth.RATE)  # just after the next-to-last cut
    if ending == "went silent":
        recording[end:] = 0.0
    else:
        recording = recording[:end]
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    last = results.passes[-1]
    assert last.status is PassStatus.QUIET and last.status not in session.UNHEARD
    assert (last.verdict, last.prominence_db, last.excess_db) == (None, None, None)
    assert {p.status for p in results.passes[:-1]} == {PassStatus.ANALYSED}
    face = session.face_after(session.Round(Path("r"), plan, results))
    assert face.y_mm == pytest.approx(plan.legs[-1].face_y_end_mm)
    path = tmp_path / session.RESULTS_FILE
    results.save(path)
    assert session.Results.load(path) == results


def assert_heard_right(plan: Plan, results, contact: list[tuple[float, float]] | None = None):
    for index, (p, result) in enumerate(zip(plan.passes, results.passes, strict=True)):
        assert result.status is PassStatus.ANALYSED, p.id
        truth = synth.DEFAULT_MACHINE.chatters(p.rpm, p.width_mm, plan.flutes, p.depth_mm)
        assert (result.verdict is Verdict.UNSTABLE) == truth, p.id
        if contact is not None:
            assert result.start_s == pytest.approx(contact[index][0], abs=0.01), p.id


def test_cuts_are_put_on_their_passes_when_the_top_is_faced_midway(write_setup):
    from test_plan import spilled

    _, plan = spilled(write_setup)
    faced = [n for n, leg in enumerate(plan.legs) if leg.reface_mm > 0]
    assert faced and faced[0] > 0 and plan.legs[faced[0]].face_s > 60

    # The widget records on through the facing, a long loud chatter; its windows place
    # every cut, and the facing, in none of them, is never analysed.
    timeline, contact = schedule(plan, facing=synth.Facing.HEARD)
    heard = synth.record(plan, seed=3, facing=synth.Facing.HEARD)
    results = session.analyze(plan, heard, synth.RATE, "r.wav", Thresholds(), timeline)
    assert_heard_right(plan, results, contact)


def stopped_before_facing(write_setup):
    """A spilled plan (see test_plan.spilled), the leg its facing comes before, and the plan
    up to that facing."""
    from test_plan import spilled

    _, plan = spilled(write_setup)
    faced = next(n for n, leg in enumerate(plan.legs) if n > 0 and leg.reface_mm > 0)
    return plan, faced, dataclasses.replace(plan, legs=plan.legs[:faced])


def rows_setup(write_setup, lanes: int):
    stock = f"width_mm = 6.0\nlanes = {lanes}\nheight_mm = 40.0"
    setup = load_setup(write_setup(l=("width_mm = 30.0", stock)))
    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    made = (Made(1, 1.0, depth),)
    plan = assemble(setup, env, 2, PlannerKind.GRID, tests, face=Face(y_mm=1.0), made=made)
    faced = plan.mid_facing
    leg = plan.legs[faced]
    feed = feed_mm_min(setup, env, leg.passes[0].rpm)
    rows = facing_rows(setup, leg.top_mm, leg.reface_mm)
    return setup, plan, faced, [(r.end - r.start) / feed * 60 for r in rows]


@pytest.mark.parametrize("facing", [session.Facing.NOT_RUN, session.Facing.CUT_SHORT])
def test_a_facing_set_by_hand_drops_what_was_heard_after_it(write_setup, tmp_path, facing):
    _, plan, faced, row_s = rows_setup(write_setup, 1)
    start = plan.first_index(faced)
    recording = synth.record(plan, seed=3, facing=synth.Facing.ROWS, row_s=row_s, stop_after_rows=2)
    timeline = synth.timeline(plan, facing=synth.Facing.ROWS, row_s=row_s, stop_after_rows=2)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    # As an older matcher could: the facing's two rows taken for the faced leg's first passes.
    passes = list(results.passes)
    for i in (start, start + 1):
        passes[i] = dataclasses.replace(
            passes[i], status=PassStatus.ANALYSED, verdict=Verdict.UNSTABLE
        )
    folder = tmp_path / "round-02"
    folder.mkdir()
    dataclasses.replace(results, passes=tuple(passes)).save(folder / session.RESULTS_FILE)
    edited = json.loads((folder / session.RESULTS_FILE).read_text())
    (folder / session.RESULTS_FILE).write_text(json.dumps({**edited, "facing": facing.value}))
    r = session.Round(folder, plan, session.Results.load(folder / session.RESULTS_FILE))
    assert {p.status for p in r.results.passes[start:]} == {PassStatus.NOT_RUN}
    face = session.face_after(r)
    end = plan.legs[faced - 1]
    assert (face.lane, face.top_mm) == (end.lane, 0.0)
    assert face.top is (Top.PART_FACED if facing is session.Facing.CUT_SHORT else Top.FLAT)
    observed = session._tests([r], lambda depth: True)
    heard_before = [p for p in r.results.passes[:start] if p.kind is PassKind.TEST and p.verdict]
    assert len(observed) == len(heard_before) > 0
    assert [(o.asked, o.engagement_mm) for o in observed] == [
        (p.rpm, p.width_mm) for p in heard_before
    ]

    # In the widget: a dropped pass takes no verdict, and a verdict set elsewhere keeps what
    # was heard after the facing in the file, should the facing be corrected to done.
    shutil.copy(tmp_path / "setup.toml", folder / session.SETUP_FILE)
    plan.save(folder / session.PLAN_FILE)
    app = App(tmp_path, Thresholds())
    dropped = next(p.id for p in plan.passes[start:] if p.kind is PassKind.TEST)
    with pytest.raises(session.SessionError, match="was never run"):
        app.set_verdict(plan.round, dropped, Verdict.STABLE)
    app.set_verdict(plan.round, heard_before[0].id, Verdict.UNSTABLE)
    corrected = json.loads((folder / session.RESULTS_FILE).read_text())
    (folder / session.RESULTS_FILE).write_text(json.dumps({**corrected, "facing": "done"}))
    (r,) = session.load_rounds(tmp_path)
    assert r.results.passes[heard_before[0].id].verdict is Verdict.UNSTABLE
    heard_after = r.results.passes[start : start + 2]
    assert {(p.status, p.verdict) for p in heard_after} == {(PassStatus.ANALYSED, Verdict.UNSTABLE)}


@pytest.mark.parametrize(
    ("rows", "outcome"),
    [
        ([], session.Facing.NOT_RUN),
        ([(2, False), (1, False), (0, True)], session.Facing.CUT_SHORT),
        ([(2, False)], session.Facing.CUT_SHORT),  # stopped between two rows
        ([(2, False), (1, False), (0, False)], session.Facing.DONE),
    ],
)
def test_the_widget_s_record_of_the_facing_says_what_it_did(write_setup, rows, outcome):
    plan, faced, before = stopped_before_facing(write_setup)
    timeline, contact = schedule(before)
    t = 1000 + contact[-1][1] + 5
    for rows_after, aborted in rows:
        row = {"t": 0, "event": "face", "leg": faced, "rows_after": rows_after}
        timeline.append(
            {**row, "start": t, "end": t + 20, **({"aborted": True} if aborted else {})}
        )
        t += 25
    recording = synth.record(before, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    assert results.facing is outcome
    after = session.face_after(session.Round(Path("round-02"), plan, results))
    assert after.top_mm == (plan.legs[faced].top_mm if outcome is session.Facing.DONE else 0.0)
    assert after.top is (Top.PART_FACED if outcome is session.Facing.CUT_SHORT else Top.FLAT)


def test_a_completed_cut_window_shorter_than_its_lead_ins_is_quiet_not_unrun(setup):
    from test_plan import round1

    plan = round1(setup)
    timeline, _ = schedule(plan)
    # Pass 2 finished at its target (not aborted), faster than 2 * lead_in_s (e.g. feed override).
    cut2 = next(e for e in timeline if e["event"] == "cut" and e["pass"] == 2)
    cut2["end"] = cut2["start"] + 0.5 * plan.passes[2].lead_in_s
    recording = synth.record(plan, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    assert results.passes[2].status is PassStatus.QUIET
    assert results.passes[2].verdict is None
    face = session.face_after(session.Round(Path("r"), plan, results))
    assert face.y_mm == pytest.approx(plan.legs[-1].face_y_end_mm)


def test_e2e_round_audio_lays_cuts_on_recorded_timeline_and_writes_dwell_cues(
    write_setup, tmp_path
):
    import importlib.util
    import zipfile

    spec = importlib.util.spec_from_file_location(
        "round_audio", Path(__file__).resolve().parent.parent / "e2e" / "round_audio.py"
    )
    round_audio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(round_audio)

    from test_plan import spilled

    setup, plan = spilled(write_setup)
    setup_text = (tmp_path / session.SETUP_FILE).read_text(encoding="utf-8")
    session._write_round(tmp_path, setup_text, setup, plan)
    rdir = session.round_dir(tmp_path, plan.round)

    def pack(dest: Path) -> Path:
        with zipfile.ZipFile(dest, "w") as zf:
            for file in sorted(rdir.iterdir()):
                zf.writestr(f"sess/{rdir.name}/{file.name}", file.read_bytes())
        return dest

    # Export before running (program-timed) and check dwell cues are written.
    planned_wav = tmp_path / "planned.wav"
    round_audio.main(pack(tmp_path / "planned.zip"), planned_wav)
    cues = json.loads((tmp_path / "planned.wav.json").read_text(encoding="utf-8"))
    assert len(cues["dwells"]) == len(plan.passes)

    # Simulate a run where mid-round facing ran 15 s faster than G-code estimate; export with
    # timeline.json and verify round_audio aligns every pass so analyze recovers true verdicts.
    timeline, _ = schedule(plan, facing=synth.Facing.HEARD)
    faced_pass = next(leg.passes[0].id for leg in plan.legs if leg.reface_mm > 0)
    reached_facing = False
    for event in timeline:
        if event.get("pass") == faced_pass and event.get("event") == "dwell":
            reached_facing = True
        if reached_facing and "start" in event:
            event["start"] -= 15.0
            event["end"] -= 15.0
    (rdir / session.TIMELINE_FILE).write_text(json.dumps(timeline), encoding="utf-8")
    recorded_wav = tmp_path / "recorded.wav"
    round_audio.main(pack(tmp_path / "recorded.zip"), recorded_wav)
    samples, rate = audio.load(recorded_wav)
    results = session.analyze(plan, samples, rate, "recorded.wav", Thresholds(), timeline)
    assert_heard_right(plan, results)
