from __future__ import annotations

import numpy as np
import pytest
import synth

from dechatter import audio, detect, session
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import PassKind, PlannerKind, assemble, envelope, grid_tests
from dechatter.session import PassStatus

MACHINE = synth.Machine()
RNG_SEED = 7


def cut(
    rpm: float, chatter: bool, machine: synth.Machine = MACHINE
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(RNG_SEED)
    spindle_hz = rpm * (1 + machine.rpm_error) / 60
    return (
        synth._cut(6.0, spindle_hz, 3, 1.5, chatter, machine, rng),
        synth._air(3.0, spindle_hz, machine, rng),
    )


@pytest.mark.parametrize("rpm", [1250, 3120, 5000])
def test_forced_vibration_alone_is_stable(rpm):
    samples, air = cut(rpm, chatter=False)
    assert detect.classify(samples, air, synth.RATE, rpm, 3).verdict is Verdict.STABLE


@pytest.mark.parametrize("rpm", [1250, 3120, 5000])
def test_chatter_is_detected_despite_spindle_speed_error(rpm):
    samples, air = cut(rpm, chatter=True)
    reading = detect.classify(samples, air, synth.RATE, rpm, 3)
    assert reading.verdict is Verdict.UNSTABLE
    assert reading.spindle_hz == pytest.approx(rpm * 0.98 / 60, rel=2e-3)


@pytest.mark.parametrize("rpm", [3120, 5000])
def test_chatter_frequency_is_reported(rpm):
    samples, air = cut(rpm, chatter=True)
    reading = detect.classify(samples, air, synth.RATE, rpm, 3)
    assert reading.chatter_hz == pytest.approx(MACHINE.natural_hz * 1.03, abs=3)


def test_tone_present_in_the_air_cut_is_not_chatter():
    loud_whine = synth.Machine(whine_hz=4127.0)
    samples, air = cut(3120, chatter=False, machine=loud_whine)
    t = np.arange(len(samples)) / synth.RATE
    t_air = np.arange(len(air)) / synth.RATE
    samples = samples + 0.5 * np.sin(2 * np.pi * 2511.0 * t)
    with_tone = air + 0.5 * np.sin(2 * np.pi * 2511.0 * t_air)
    assert detect.classify(samples, with_tone, synth.RATE, 3120, 3).verdict is Verdict.STABLE
    assert detect.classify(samples, air, synth.RATE, 3120, 3).verdict is Verdict.UNSTABLE


def test_cut_too_short_to_resolve_is_refused():
    with pytest.raises(detect.TooShort):
        detect.classify(np.zeros(1000), None, synth.RATE, 3000, 3)


def round1(setup):
    env = envelope(setup)
    return assemble(setup, env, 1, PlannerKind.GRID, 0.0, 0.0, grid_tests(setup, env))


def test_recording_is_split_into_passes_and_each_classified(setup):
    plan = round1(setup)
    results = session.analyze(plan, synth.record(plan), synth.RATE, "r.wav", Thresholds())
    assert [p.status for p in results.passes] == [PassStatus.ANALYSED] * len(plan.passes)
    for p, result in zip(plan.passes, results.passes, strict=True):
        truth = MACHINE.chatters(p.rpm, p.width_mm, plan.flutes)
        assert (result.verdict is Verdict.UNSTABLE) == truth, p
    starts = [r.start_s for r in results.passes]
    assert starts == sorted(starts)


def test_aborted_run_marks_the_cut_short_pass_and_the_rest_not_run(setup):
    plan = round1(setup)
    stopped = 9
    recording = synth.record(plan, abort_in_pass=stopped)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    statuses = [p.status for p in results.passes]
    assert statuses[:stopped] == [PassStatus.ANALYSED] * stopped
    assert statuses[stopped] is PassStatus.ABORTED
    assert set(statuses[stopped + 1 :]) == {PassStatus.NOT_RUN}

    face, remnant = session.face_after(session.Round(None, plan, results))
    assert remnant == plan.passes[stopped].width_mm
    assert face == pytest.approx(sum(p.width_mm for p in plan.passes[:stopped]))


def test_quiet_truing_pass_going_undetected_does_not_shift_the_matching(setup):
    plan = round1(setup)
    recording = synth.record(plan)
    truing = plan.passes[0]
    assert truing.kind is PassKind.TRUING
    lead = 3.0 + 2.0 + plan.dwell_s + truing.lead_in_s
    silent = slice(int(lead * synth.RATE), int((lead + truing.expected_cut_s) * synth.RATE))
    recording[silent] *= 0.02
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    assert results.passes[0].status is PassStatus.MISSING
    for p, result in zip(plan.passes[1:], results.passes[1:], strict=True):
        assert result.status is PassStatus.ANALYSED
        assert (result.verdict is Verdict.UNSTABLE) == MACHINE.chatters(p.rpm, p.width_mm, 3)


def test_manual_segments_replace_detection(setup, tmp_path):
    plan = round1(setup)
    recording = synth.record(plan)
    auto = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds())
    labels = tmp_path / "labels.txt"
    labels.write_text(
        "".join(f"{r.start_s}\t{r.end_s}\tpass {r.id}\n" for r in auto.passes), encoding="utf-8"
    )
    manual = session.analyze(
        plan, recording, synth.RATE, "r.wav", Thresholds(), audio.read_segments(labels)
    )
    assert [r.verdict for r in manual.passes] == [r.verdict for r in auto.passes]
