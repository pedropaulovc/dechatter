"""Finding the cuts: in a real session that 0.2.0 got wrong, and in windows the widget records."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import synth

from dechatter import audio, session
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import Plan
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


def schedule(plan: Plan, rapid_s: float = 2.0) -> tuple[list, list[tuple[float, float]]]:
    """A timeline as the widget writes it for synth.record(plan), and the true cut times."""
    t, timeline, contact = 3.0, [{"t": 1000.0, "event": "record"}], []
    for p in plan.passes:
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
        truth = synth.DEFAULT_MACHINE.chatters(p.rpm, p.width_mm, plan.flutes, plan.axial_depth_mm)
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
    plan = dataclasses.replace(plan, passes=long)
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


def level_trace(plan: Plan, spindle_db: float, cut_db: float, silence: tuple[float, float]):
    """A level trace (dB per HOP_S) of running `plan`, with `silence` seconds of quiet before
    and after it, and the true cut times."""
    rng = np.random.default_rng(2)
    hop = audio.HOP_S
    parts, cuts, t = [np.full(int(silence[0] / hop), -90.0)], [], silence[0]
    for index, p in enumerate(plan.passes):
        air = 2.0 + plan.dwell_s + p.lead_in_s
        parts.append(np.full(int(air / hop), spindle_db))
        parts.append(np.full(int(p.expected_cut_s / hop), cut_db + 3.0 * (index % 3)))
        cuts.append((t + air, t + air + p.expected_cut_s))
        t += air + p.expected_cut_s
    parts.append(np.full(int(silence[1] / hop), -90.0))
    level = np.concatenate(parts)
    return level + rng.normal(0, 1.0, len(level)), cuts


@pytest.mark.parametrize(
    ("what", "spindle_db", "cut_db", "silence"),
    [
        ("loud chatter over a quiet spindle", -70.0, -25.0, (3.0, 2.0)),
        ("a recorder left running long after the round", -45.0, -35.0, (5.0, 0.0)),
    ],
)
def test_silence_is_told_from_a_quiet_spindle(setup, what, spindle_db, cut_db, silence):
    from test_plan import round1

    plan = round1(setup)
    if silence[1] == 0.0:  # silence 95 % of the recording
        _, cuts = level_trace(plan, spindle_db, cut_db, (5.0, 0.0))
        silence = (5.0, 19 * cuts[-1][1])
    level, cuts = level_trace(plan, spindle_db, cut_db, silence)
    found = audio.find_cuts_in(level, plan)
    assert sorted(found.matches) == list(range(len(plan.passes))), what
    for index, (start, end) in enumerate(cuts):
        assert found.matches[index].start_s == pytest.approx(start, abs=1.0), what
        assert found.matches[index].end_s == pytest.approx(end, abs=1.0), what


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
    face, remnant = session.face_after(session.Round(None, plan, results))
    assert remnant == plan.passes[3].width_mm

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
    face, remnant = session.face_after(session.Round(None, plan, results))
    widths = [p.width_mm for p in plan.passes]
    assert (face, remnant) == pytest.approx((plan.face_y_start_mm + sum(widths[:2]), 0.0))


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
