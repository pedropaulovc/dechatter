"""Finding the cuts: in a real session that 0.2.0 got wrong, and in windows the widget records."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import synth

from dechatter import audio, session
from dechatter.app import App
from dechatter.config import load_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import (
    REFACE_EXTRA_MM,
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

# Cut times found by hand (s), from issue #32. Round 1's passes from 7 on were matched right.
HAND = {
    "round-01": {0: (19, 51), 1: (71, 103), 2: (123, 155), 3: (175, 205), 4: (240, 257),
                 5: (273, 290), 6: (305.5, 323)},
    "round-02": {0: (25.5, 35), 1: (47.5, 57.3), 2: (70, 80.1), 3: (108.2, 116.9)},
}  # fmt: skip
NOT_RUN = {"round-01": set(), "round-02": {4, 5}}
# Round 1's two lightest 570 rpm cuts are no louder than the spindle: nothing can hear them.
INAUDIBLE = {"round-01": {0, 1}, "round-02": set()}


def owner(round_name: str, t: float) -> int | None:
    """The pass whose hand-found cut holds time t."""
    for index, (start, end) in HAND[round_name].items():
        if start - 1 <= t <= end + 1:
            return index
    return None


@pytest.mark.parametrize("round_name", ["round-01", "round-02"])
def test_0_2_0_put_the_cuts_of_this_session_on_the_wrong_passes(round_name):
    old = json.loads((FIXTURES / round_name / "results-0.2.0.json").read_text())
    wrong = [
        (p["id"], owner(round_name, p["start_s"]))
        for p in old["passes"]
        if p["start_s"] is not None and owner(round_name, p["start_s"]) not in (None, p["id"])
    ]
    # Round 1: the 570 x 2.2 cut taken for 570 x 0.6, and the 990 rpm cuts each one pass early.
    # Round 2: every cut one pass late, and the 2180 rpm cut reported as a 2250 rpm pass.
    assert (
        wrong
        == {"round-01": [(1, 3), (4, 5), (5, 6)], "round-02": [(1, 0), (2, 1), (3, 2), (4, 3)]}[
            round_name
        ]
    )


@pytest.mark.parametrize("round_name", ["round-01", "round-02"])
def test_cuts_are_found_against_the_local_spindle_floor_and_kept_in_order(round_name):
    plan = Plan.load(FIXTURES / round_name / "plan.json")
    level = np.load(FIXTURES / round_name / "level.npy").astype(float)
    found = audio.find_cuts_in(level, plan)
    old = json.loads((FIXTURES / round_name / "results-0.2.0.json").read_text())["passes"]
    for index in range(len(plan.passes)):
        segment = found.matches.get(index)
        if index in HAND[round_name] and index not in INAUDIBLE[round_name]:
            middle = (segment.start_s + segment.end_s) / 2
            assert owner(round_name, middle) == index, f"pass {index}"
            continue
        if index in INAUDIBLE[round_name] | NOT_RUN[round_name]:
            assert segment is None, f"pass {index}"
            continue
        # Matched right by 0.2.0 already (round 1 from pass 7 on): the same cuts.
        assert segment.start_s == pytest.approx(old[index]["start_s"], abs=1.5), f"pass {index}"
    # What was never run comes after the last cut heard: analysis calls it "not run".
    assert all(index > max(found.matches) for index in NOT_RUN[round_name])


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
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
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
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=stopped)
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


def test_cuts_longer_than_a_minute_are_found_too(setup):
    """The floor's window follows the plan: it must hold air on both sides of the longest cut."""
    import dataclasses

    from test_plan import round1

    plan = round1(setup)
    long = tuple(dataclasses.replace(p, expected_cut_s=90.0) for p in plan.passes[:4])
    plan = dataclasses.replace(plan, legs=(dataclasses.replace(plan.legs[0], passes=long),))
    recording = synth.record(plan, seed=5)
    found = audio.find_cuts(recording, synth.RATE, plan)
    assert sorted(found.matches) == [0, 1, 2, 3]
    assert all(s.duration_s == pytest.approx(90, abs=2) for s in found.matches.values())
    assert audio.floor_window_s(plan) > 3 * 90


def test_cuts_are_found_when_silence_is_most_of_the_recording(setup):
    """A recorder left running after the run: the spindle's level is no longer the median."""
    from test_plan import round1

    plan = round1(setup)
    recording = synth.record(plan, seed=3)
    rng = np.random.default_rng(1)
    silence = 0.0005 * rng.standard_normal(int(1.5 * len(recording)))
    alone = audio.find_cuts(recording, synth.RATE, plan)
    padded = audio.find_cuts(np.concatenate((recording, silence)), synth.RATE, plan)
    assert sorted(padded.matches) == sorted(alone.matches)
    for index, segment in alone.matches.items():
        assert padded.matches[index].start_s == pytest.approx(segment.start_s, abs=1.0)
        assert padded.matches[index].end_s == pytest.approx(segment.end_s, abs=1.0)


SILENT_DB = -90.0


def level_trace(
    plan: Plan,
    spindle_db: float,
    cut_db: float,
    trailing_share: float = 0.0,
    gap: tuple[int, float] | None = None,
    knock_every_s: float | None = None,
):
    """A level trace (dB per HOP_S) of running `plan`, and the true cut times.

    3 s of silence lead it; silence trails it for `trailing_share` of the whole. `gap`:
    (pass index, seconds) of spindle-off silence before that pass. `knock_every_s`: a
    0.5 s knock breaks the trailing silence up that often.
    """
    rng = np.random.default_rng(2)
    hop = audio.HOP_S

    def part(seconds: float, db: float) -> np.ndarray:
        return np.full(int(round(seconds / hop)), db)

    parts, cuts, t = [part(3.0, SILENT_DB)], [], 3.0
    for index, p in enumerate(plan.passes):
        if gap is not None and gap[0] == index:
            parts.append(part(gap[1], SILENT_DB))
            t += gap[1]
        air = 2.0 + plan.dwell_s + p.lead_in_s
        parts.append(part(air, spindle_db))
        parts.append(part(p.expected_cut_s, cut_db + 3.0 * (index % 3)))
        cuts.append((t + air, t + air + p.expected_cut_s))
        t += air + p.expected_cut_s
    trailing = t * trailing_share / (1 - trailing_share)
    while trailing > 0:
        quiet = min(trailing, knock_every_s or trailing)
        parts.append(part(quiet, SILENT_DB))
        trailing -= quiet
        if knock_every_s and trailing > 0:
            parts.append(part(0.5, spindle_db + 10))
            trailing -= 0.5
    level = np.concatenate(parts)
    return level + rng.normal(0, 1.0, len(level)), cuts


def assert_cuts_found(plan: Plan, level: np.ndarray, cuts: list) -> None:
    found = audio.find_cuts_in(level, plan)
    assert sorted(found.matches) == list(range(len(plan.passes)))
    for index, (start, end) in enumerate(cuts):
        assert found.matches[index].start_s == pytest.approx(start, abs=1.0), index
        assert found.matches[index].end_s == pytest.approx(end, abs=1.0), index


@pytest.mark.parametrize(
    ("spindle_db", "cut_db", "trailing_share"),
    [
        (-70.0, -25.0, 0.0),  # loud chatter over a quiet spindle
        (-45.0, -35.0, 0.95),  # a recorder left running long after the round
    ],
)
def test_silence_is_told_from_a_quiet_spindle(setup, spindle_db, cut_db, trailing_share):
    from test_plan import round1

    plan = round1(setup)
    assert_cuts_found(plan, *level_trace(plan, spindle_db, cut_db, trailing_share))


@pytest.mark.parametrize("gap_s", [20.0, 40.0])
def test_a_spindle_stopped_mid_round_is_silence(setup, gap_s):
    from test_plan import round1

    plan = round1(setup)
    assert_cuts_found(plan, *level_trace(plan, -40.0, -32.0, gap=(7, gap_s)))


def test_trailing_silence_broken_up_by_knocks_is_silence(setup):
    from test_plan import round1

    plan = round1(setup)
    assert_cuts_found(plan, *level_trace(plan, -40.0, -32.0, 0.6, knock_every_s=10.0))


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
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
    assert results.passes[2].status is PassStatus.ANALYSED and results.passes[2].verdict is not None


def test_a_cut_stopped_just_after_contact_keeps_its_step(setup):
    from test_plan import round1

    plan = round1(setup)
    timeline, _ = schedule(plan)
    stopped = [e for e in timeline if e.get("pass", 0) <= 3]
    cut3 = next(e for e in stopped if e["event"] == "cut" and e["pass"] == 3)
    cut3.update(aborted=True, end=cut3["start"] + plan.passes[3].lead_in_s + 0.2)
    recording = synth.record(plan, seed=3)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=stopped)
    # Too short to hear, but the cutter reached the face: aborted, no verdict.
    assert results.passes[3].status is PassStatus.ABORTED and results.passes[3].verdict is None
    face = session.face_after(session.Round(None, plan, results))
    assert face.remnant_mm == plan.passes[3].width_mm

    # Stopped during the lead-in: the cutter never met the stock.
    cut3["end"] = cut3["start"] + plan.passes[3].lead_in_s / 2
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=stopped)
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
            timeline=stopped_in_lead_in(plan, timeline, 0),
        )  # fmt: skip
    # A later cut: not run, and the face is where the pass before it left it, with no step.
    stopped = stopped_in_lead_in(plan, timeline, 2)
    assert 2 not in session.placements_from_timeline(plan, stopped)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=stopped)
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
        session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)


def test_a_cut_no_louder_than_the_spindle_in_the_air_is_not_called_stable(setup):
    """A face planned where there was none (fresh stock picked, none mounted) cuts air,
    and the air sounds stable. Those passes say no cut was heard instead."""
    from test_plan import round1

    plan = round1(setup)
    timeline, _ = schedule(plan)
    lost = frozenset({3, 4})
    recording = synth.record(plan, in_air=lost)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
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
        # The fix: the windows go on over a silent capture (detect.Silent), which analysed
        # to a NaN prominence that broke results.json.
        "went silent",
        # A regression check of the older path: the capture is cut short, so the last
        # window is too short to analyse (detect.TooShort).
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
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
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
    results = session.analyze(plan, heard, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
    assert_heard_right(plan, results, contact)

    # From the command line, the recording paused through the facing as the program says:
    # the cuts found by their sound alone fall on the right passes, across every leg.
    paused = synth.record(plan, seed=3)
    results = session.analyze(plan, paused, synth.RATE, "r.wav", Thresholds())
    assert_heard_right(plan, results)
    # The sound cannot say whether the facing between them ran to its end: it is asked.
    assert session.facing_asked(plan, results.passes) == faced[0] and results.facing is None
    done = session.analyze(
        plan, paused, synth.RATE, "r.wav", Thresholds(), facing=session.Facing.DONE
    )
    assert_heard_right(plan, done)

    # Left running through the facing: the facing is heard as one long stretch, where the
    # plan has it, and left out; every cut still falls on its pass.
    found = audio.find_cuts(heard, synth.RATE, plan)
    assert set(found.facings) == {faced[0]}
    assert_heard_right(plan, session.analyze(plan, heard, synth.RATE, "r.wav", Thresholds()))


def stopped_before_facing(write_setup):
    """A spilled plan (see test_plan.spilled), the leg its facing comes before, and the plan
    up to that facing."""
    from test_plan import spilled

    _, plan = spilled(write_setup)
    faced = next(n for n, leg in enumerate(plan.legs) if n > 0 and leg.reface_mm > 0)
    return plan, faced, dataclasses.replace(plan, legs=plan.legs[:faced])


def test_a_run_stopped_at_a_facing_is_not_guessed_from_its_sound(write_setup):
    plan, faced, before = stopped_before_facing(write_setup)
    last = before.passes[-1].id
    # Not paused at the stop before the facing, then stopped 30 s into it: the facing's
    # sound follows the last cut before it.
    rng = np.random.default_rng(9)
    hz = plan.legs[faced].passes[0].rpm / 60
    quarter = plan.diameter_mm / 4
    recording = np.concatenate(
        [
            synth.record(before, seed=3)[: -2 * synth.RATE],
            synth._air(6.0, hz, synth.DEFAULT_MACHINE, rng),
            synth._cut(30.0, hz, plan.flutes, quarter, True, synth.DEFAULT_MACHINE, rng),
            0.0005 * rng.standard_normal(2 * synth.RATE),
        ]
    ).astype(np.float32)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    statuses = [p.status for p in results.passes]
    # The sound after it is no pass of the faced leg, stopped early: the facing may be it.
    assert statuses[last] is PassStatus.ANALYSED
    assert set(statuses[last + 1 :]) == {PassStatus.LIKELY_NOT_RUN}
    assert session.facing_asked(plan, results.passes) == faced
    with pytest.raises(session.SessionError, match=r"--facing done` if it ran to its end"):
        session.face_after(session.Round(Path("round-02"), plan, results))

    # Once the operator says, the next round starts from what the facing did.
    end = before.legs[-1]
    new = plan.legs[faced]
    told = {
        session.Facing.DONE: Face(new.lane, 0.0, 0.0, None, new.top_mm),
        session.Facing.NOT_RUN: Face(end.lane, end.face_y_end_mm, 0.0, end.lane_cap_mm, 0.0),
        session.Facing.CUT_SHORT: Face(
            end.lane, end.face_y_end_mm, 0.0, end.lane_cap_mm, 0.0, Top.PART_FACED
        ),
    }
    for facing, face in told.items():
        said = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), facing=facing)
        assert said.facing is facing
        after = session.face_after(session.Round(Path("round-02"), plan, said))
        assert after == dataclasses.replace(face, y_mm=pytest.approx(face.y_mm))

    # Said of a run that stopped short of the facing, it is refused.
    short = synth.record(plan, seed=3, abort_in_pass=before.legs[0].passes[-1].id)
    with pytest.raises(session.SessionError, match="leave it out"):
        session.analyze(plan, short, synth.RATE, "r.wav", Thresholds(), facing=session.Facing.DONE)


def rows_setup(write_setup, lanes: int):
    """Review45's case: a round on a 6 mm wide block of `lanes` lanes that faces the top
    partway, the leg it faces before, and the seconds of each row of that facing's cut."""
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


@pytest.mark.parametrize(("lanes", "rows"), [(1, 2), (1, 3), (3, 2)])
def test_a_facing_stopped_after_a_few_rows_is_never_taken_for_cuts(write_setup, lanes, rows):
    setup, plan, faced, row_s = rows_setup(write_setup, lanes)
    start = plan.first_index(faced)
    # Not paused through the facing, and stopped after a few of its rows.
    recording = synth.record(
        plan, seed=3, facing=synth.Facing.ROWS, row_s=row_s, stop_after_rows=rows
    )
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    # Every cut before the facing is on its pass, whatever the rows after it matched.
    assert {p.status for p in results.passes[:start]} == {PassStatus.ANALYSED}
    # Sound alone never says what the facing did: planning waits for the operator.
    assert session.facing_asked(plan, results.passes) == faced and results.facing is None
    with pytest.raises(session.SessionError, match="cannot tell what that facing did"):
        session.face_after(session.Round(Path("round-02"), plan, results))

    # Cut short: whatever was heard after it is dropped, and the next round faces the top
    # down again from the old top before any cut.
    said = session.analyze(
        plan, recording, synth.RATE, "r.wav", Thresholds(), facing=session.Facing.CUT_SHORT
    )
    r = session.Round(Path("round-02"), plan, said)
    assert {p.status for p in r.results.passes[start:]} == {PassStatus.NOT_RUN}
    face = session.face_after(r)
    assert (face.top, face.top_mm) == (Top.PART_FACED, 0.0)

    env = envelope(setup)
    depth = setup.cut.axial_depth_mm
    tests = grid_tests(setup, env)
    made = (Made(face.lane, face.y_mm, depth),)
    nxt = assemble(setup, env, 3, PlannerKind.GRID, tests, face=face, made=made)
    first = nxt.legs[0]
    assert (first.lane, first.face_y_start_mm, first.top_mm) == (1, 0.0, first.reface_mm)
    assert first.reface_mm == pytest.approx(depth + REFACE_EXTRA_MM)


@pytest.mark.parametrize("facing", [session.Facing.NOT_RUN, session.Facing.CUT_SHORT])
def test_a_facing_set_by_hand_drops_what_was_heard_after_it(write_setup, tmp_path, facing):
    _, plan, faced, row_s = rows_setup(write_setup, 1)
    start = plan.first_index(faced)
    recording = synth.record(plan, seed=3, facing=synth.Facing.ROWS, row_s=row_s, stop_after_rows=2)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    # As an older matcher could: the facing's two rows taken for the faced leg's first passes.
    passes = list(results.passes)
    for i in (start, start + 1):
        passes[i] = dataclasses.replace(
            passes[i], status=PassStatus.ANALYSED, verdict=Verdict.UNSTABLE
        )
    folder = tmp_path / "round-02"
    folder.mkdir()
    dataclasses.replace(results, passes=tuple(passes)).save(folder / session.RESULTS_FILE)
    # The operator answers in results.json by hand, as the refusal offers.
    edited = json.loads((folder / session.RESULTS_FILE).read_text())
    (folder / session.RESULTS_FILE).write_text(json.dumps({**edited, "facing": facing.value}))
    r = session.Round(folder, plan, session.Results.load(folder / session.RESULTS_FILE))
    assert {p.status for p in r.results.passes[start:]} == {PassStatus.NOT_RUN}
    # The next round starts before the facing, on the old top; the passes after it are no
    # evidence.
    face = session.face_after(r)
    end = plan.legs[faced - 1]
    assert (face.lane, face.top_mm) == (end.lane, 0.0)
    assert face.top is (Top.PART_FACED if facing is session.Facing.CUT_SHORT else Top.FLAT)
    observed = session._tests([r], lambda depth: True)
    heard_before = [p for p in r.results.passes[:start] if p.kind is PassKind.TEST and p.verdict]
    assert len(observed) == len(heard_before) > 0
    assert [(o.rpm, o.engagement_mm) for o in observed] == [
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
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline)
    assert results.facing is outcome
    after = session.face_after(session.Round(Path("round-02"), plan, results))
    assert after.top_mm == (plan.legs[faced].top_mm if outcome is session.Facing.DONE else 0.0)
    assert after.top is (Top.PART_FACED if outcome is session.Facing.CUT_SHORT else Top.FLAT)
    with pytest.raises(session.SessionError, match="record of the run says"):
        session.analyze(
            plan, recording, synth.RATE, "r.wav", Thresholds(), timeline=timeline, facing=outcome
        )
