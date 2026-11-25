from __future__ import annotations

import numpy as np
import pytest
import synth
from scipy.io import wavfile

from dechatter import audio, detect, session
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import PlannerKind, assemble, envelope, grid_tests
from dechatter.session import PassStatus

MACHINE = synth.Machine()
RNG_SEED = 7


def cut(
    rpm: float, chatter: bool, machine: synth.Machine = MACHINE
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(RNG_SEED)
    spindle_hz = rpm * (1 + machine.rpm_error) / 60
    return (
        synth._cut(6.0, spindle_hz, 3, machine.load(1.5, 3.0), chatter, machine, rng),
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


def test_a_silent_cut_is_refused_and_silent_air_reads_finite():
    with pytest.raises(detect.Silent):
        detect.classify(np.zeros(synth.RATE * 5), None, synth.RATE, 3000, 3)
    samples, air = cut(3120, chatter=True)
    reading = detect.classify(samples, np.zeros_like(air), synth.RATE, 3120, 3)
    assert np.isfinite(reading.prominence_db) and np.isfinite(reading.excess_db)


def round1(setup):
    env = envelope(setup)
    return assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env))


def test_recording_is_split_by_timeline_and_each_pass_classified(setup):
    plan = round1(setup)
    results = session.analyze(
        plan, synth.record(plan), synth.RATE, "r.wav", Thresholds(), synth.timeline(plan)
    )
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
    timeline = synth.timeline(plan, abort_in_pass=stopped)
    results = session.analyze(plan, recording, synth.RATE, "r.wav", Thresholds(), timeline)
    statuses = [p.status for p in results.passes]
    assert statuses[:stopped] == [PassStatus.ANALYSED] * stopped
    assert statuses[stopped] is PassStatus.ABORTED
    assert set(statuses[stopped + 1 :]) == {PassStatus.NOT_RUN}

    face = session.face_after(session.Round(None, plan, results))
    assert face.remnant_mm == plan.passes[stopped].width_mm
    assert face.y_mm == pytest.approx(sum(p.width_mm for p in plan.passes[:stopped]))


def test_wav_loader_reads_16bit_and_float_wav_and_refuses_corrupt_files(tmp_path):
    tone = (0.5 * np.sin(np.linspace(0, 50, 4000))).astype(np.float32)
    pcm_path = tmp_path / "pcm16.wav"
    audio.save_wav(pcm_path, tone, 16000)
    samples, rate = audio.load(pcm_path)
    assert rate == 16000 and samples == pytest.approx(tone, abs=1e-4)

    float_path = tmp_path / "float32.wav"
    wavfile.write(float_path, 24000, np.column_stack([tone, tone]))
    samples_f, rate_f = audio.load(float_path)
    assert rate_f == 24000 and samples_f == pytest.approx(tone, abs=1e-6)

    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(audio.AudioError, match="cannot read WAV"):
        audio.load(bad)
