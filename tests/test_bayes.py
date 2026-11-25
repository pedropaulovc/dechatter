from __future__ import annotations

import dataclasses
import json

import numpy as np
import pytest
import synth

from dechatter import audio, bayes, session
from dechatter.bayes import Observation
from dechatter.config import TEMPLATE, SpindleControl, read_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import (
    PassKind,
    PlanError,
    PlannerKind,
    assemble,
    envelope,
    grid_tests,
    mrr_mm3_min,
)
from dechatter.session import PassResult, PassStatus, SessionError, SpeedSource, Stock

MACHINE = synth.Machine()


def p_at(stability: bayes.StabilityMap, rpm: float, width: float) -> float:
    return float(
        stability.p_stable()[stability.speed_index(rpm), stability.engagement_index(width)]
    )


def test_prior_falls_with_width(setup):
    stability = bayes.build_map(setup, envelope(setup), [])
    column = stability.p_stable()[0]
    assert np.all(np.diff(column) < 0)
    assert column[-1] == pytest.approx(0.05)


def test_stable_cut_vouches_for_lighter_cuts_near_its_speed_only(setup):
    env = envelope(setup)
    prior = bayes.build_map(setup, env, [])
    after = bayes.build_map(setup, env, [Observation(3000, 3.0, Verdict.STABLE)])
    assert p_at(after, 3000, 2.0) > 0.9
    assert p_at(after, 3000, 4.0) > p_at(prior, 3000, 4.0)
    assert p_at(after, 3030, 2.0) > p_at(prior, 3030, 2.0)
    assert p_at(after, 4000, 2.0) == pytest.approx(p_at(prior, 4000, 2.0))


def test_unstable_cut_condemns_heavier_cuts_but_not_lighter(setup):
    env = envelope(setup)
    after = bayes.build_map(setup, env, [Observation(3000, 2.0, Verdict.UNSTABLE)])
    assert p_at(after, 3000, 2.5) < 0.1
    assert p_at(after, 3000, 1.0) > 0.5


def test_one_contradicting_cut_does_not_erase_a_verdict(setup):
    env = envelope(setup)
    cuts = [Observation(3000, 2.0, Verdict.STABLE)] * 2 + [Observation(3000, 1.5, Verdict.UNSTABLE)]
    assert p_at(bayes.build_map(setup, env, cuts), 3000, 1.8) > 0.5


def test_chatter_frequency_raises_confidence_at_predicted_sweet_spot(setup):
    env = envelope(setup)
    base = [Observation(4000, 1.5, Verdict.STABLE)]
    silent = bayes.build_map(setup, env, [*base, Observation(4000, 2.5, Verdict.UNSTABLE)])
    heard = bayes.build_map(setup, env, [*base, Observation(4000, 2.5, Verdict.UNSTABLE, 950.0)])
    sweet = 60 * 950.0 / (setup.tool.flutes * 4)  # 4750 rpm
    assert p_at(heard, sweet, 1.5) > p_at(silent, sweet, 1.5) + 0.1
    assert p_at(heard, 4400, 1.5) == pytest.approx(p_at(silent, 4400, 1.5), abs=0.01)


def test_same_mode_heard_in_many_cuts_counts_once(setup):
    stable = [Observation(4000, 1.5, Verdict.STABLE), Observation(3000, 1.0, Verdict.STABLE)]
    chatter = [
        Observation(4000, 2.5, Verdict.UNSTABLE, 950.0),
        Observation(4000, 3.0, Verdict.UNSTABLE, 955.0),
        Observation(3000, 2.0, Verdict.UNSTABLE, 948.0),
        Observation(3000, 2.2, Verdict.UNSTABLE, 1810.0),
    ]
    modes = bayes.chatter_modes([*stable, *chatter], setup.planner.speed_sigma_fraction)
    assert modes == [(950.0, 1.5), (1810.0, 1.0)]


def test_sweet_spot_speeds_put_tooth_passing_on_a_subharmonic():
    spots = bayes.sweet_spot_speeds(1000.0, 4, 2400, 8000)
    assert spots == [(2, 7500.0), (3, 5000.0), (4, 3750.0), (5, 3000.0), (6, 2500.0)]


def test_proposals_respect_probability_floors_and_power_cap(setup):
    env = envelope(setup)
    seen = [Observation(rpm, w, Verdict.STABLE) for rpm, w, _ in grid_tests(setup, env)]
    proposal = bayes.propose(setup, env, seen)
    measured = bayes.build_map(setup, env, seen)
    assert 0 < len(proposal.tests) <= setup.planner.batch_size
    best = max(mrr_mm3_min(setup, env, o.rpm, o.engagement_mm) for o in seen)
    for rpm, width, p in proposal.tests:
        assert p == pytest.approx(p_at(measured, rpm, width))
        assert p >= setup.planner.probability_floor
        assert mrr_mm3_min(setup, env, rpm, width) > best
        assert mrr_mm3_min(setup, env, rpm, width) <= env.mrr_limit_mm3_min
    assert max(p for _, _, p in proposal.tests) >= setup.planner.min_probability


def test_nothing_proposed_once_the_width_limit_is_reached_at_top_speed(setup):
    env = envelope(setup)
    top = Observation(env.speed_max_rpm, env.engagements(0.1)[-1], Verdict.STABLE)
    assert bayes.propose(setup, env, [top]).tests == []


def test_a_depth_is_left_once_no_likely_cut_would_beat_what_another_depth_reached(setup):
    env = envelope(setup)
    here = [Observation(4600, 3.0, Verdict.STABLE)]
    widest = env.engagements(setup.planner.width_step_mm)[-1]
    within_reach = mrr_mm3_min(setup, env, 4600, 3.4)
    out_of_reach = 0.95 * mrr_mm3_min(setup, env, 4600, widest)
    assert bayes.propose(setup, env, here, within_reach).tests
    assert bayes.propose(setup, env, here, out_of_reach).tests == []


def test_each_cut_goes_on_the_map_at_the_speed_heard_in_it_else_at_the_one_asked(setup, tmp_path):
    env = envelope(setup)
    plan = assemble(setup, env, 1, PlannerKind.GRID, grid_tests(setup, env))
    tests = [p for p in plan.passes if p.kind is PassKind.TEST]
    heard, edge, quiet, unheard = tests[:4]
    # Wider than `heard` at its speed: with it stable below, the chatter sets sweet spots.
    chatter = next(p for p in tests if p.rpm == heard.rpm and p.width_mm > heard.width_mm)
    off = {heard.id: 1.03, edge.id: 1.048, quiet.id: 1.02, chatter.id: 1.03}
    passes = []
    for p in plan.passes:
        if p.kind is not PassKind.TEST:
            passes.append(PassResult(p.id, p.kind, p.rpm, p.width_mm, PassStatus.ANALYSED))
            continue
        spindle = None if p is unheard else p.rpm * off.get(p.id, 0.98) / 60
        verdict = Verdict.UNSTABLE if p is chatter else Verdict.STABLE
        status = PassStatus.QUIET if p is quiet else PassStatus.ANALYSED
        passes.append(
            PassResult(
                p.id,
                p.kind,
                p.rpm,
                p.width_mm,
                status,
                verdict=verdict,
                chatter_hz=900.0 if p is chatter else None,
                spindle_hz=spindle,
                detected=verdict,
            )
        )
    results = session.Results(1, session.RECORDING_FILE, Thresholds(), tuple(passes))
    r = session.Round(tmp_path, plan, results)

    # Heard: at the speed found. At the edge of the search ("speed?"), with no cut heard, or
    # with no speed found at all: at the speed asked for.
    asked = {edge.id, quiet.id, unheard.id}
    expected = [
        Observation(
            float(p.rpm) if p.id in asked else round(p.rpm * off.get(p.id, 0.98) / 60 * 60, 1),
            p.width_mm,
            Verdict.UNSTABLE if p is chatter else Verdict.STABLE,
            900.0 if p is chatter else None,
            None if p.id in asked else float(p.rpm),
        )
        for p in tests
    ]
    seen = session.observations([r], plan.fixed_mm, env)
    assert seen == expected
    sigma = setup.planner.speed_sigma_fraction
    assert bayes.chatter_modes(seen, sigma) == [(900.0, heard.width_mm)]
    # The map, and the sweet spots the chatter predicts, are those of cuts at those speeds.
    plain = [Observation(o.rpm, o.engagement_mm, o.verdict, o.chatter_hz) for o in expected]
    assert np.array_equal(
        bayes.build_map(setup, env, seen).p_stable(), bayes.build_map(setup, env, plain).p_stable()
    )

    best = session.best_cuts(setup, env, [r])
    found = next(c for c in best if c.width_mm == heard.width_mm and c.asked_rpm == heard.rpm)
    assert (found.rpm, found.speed) == (expected[0].rpm, SpeedSource.HEARD)
    # Its removal rate is that of the feed programmed from the speed asked for.
    assert found.mrr_mm3_min == pytest.approx(mrr_mm3_min(setup, env, heard.rpm, heard.width_mm))
    guessed = next(c for c in best if c.width_mm == unheard.width_mm and c.asked_rpm == unheard.rpm)
    assert (guessed.rpm, guessed.speed) == (unheard.rpm, SpeedSource.ASKED)
    # The chip load the cut really took, at the speed heard.
    taken = session.chip_load_cut_mm(passes[plan.passes.index(heard)], heard.feed_mm_min, 3)
    assert taken == pytest.approx(env.chip_load_mm / 1.03, rel=1e-3)
    assert session.chip_load_cut_mm(passes[plan.passes.index(unheard)], 100.0, 3) is None


def test_a_stable_cut_is_asked_for_again_at_its_stop_even_heard_nearer_the_next(setup):
    stops = (1513, 1839, 1950, 2275, 2380, 2482, 2590, 2692, 2795)
    manual = dataclasses.replace(
        setup, machine=dataclasses.replace(setup.machine, spindle=SpindleControl.MANUAL)
    )

    def env_with(stops):
        machine = dataclasses.replace(manual.machine, speed_stops_rpm=stops)
        return envelope(dataclasses.replace(manual, machine=machine))

    # Asked at 2795 and heard 2% slow, nearer 2692: the dial is set to 2795 again.
    cut = session.StableCut(1000.0, 5.0, 2739.1, 1.0, 2795.0, SpeedSource.HEARD)
    assert session.ask_rpm(env_with(stops), cut) == 2795.0
    # Stops edited since, without 2795: the stop nearest the speed heard.
    assert session.ask_rpm(env_with(stops[:-1]), cut) == 2692.0
    # Without stops: the speed asked for, which ran as heard.
    assert session.ask_rpm(env_with(()), cut) == 2795.0


def true_optimum(setup, env) -> float:
    flutes = setup.tool.flutes
    return max(
        mrr_mm3_min(setup, env, rpm, min(MACHINE.limit_mm(rpm, flutes), env.engagement_max_mm))
        for rpm in env.speeds(10)
    )


def init_session(work) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / session.SETUP_FILE).write_text(TEMPLATE, encoding="utf-8")


def fill_in(work) -> None:
    """Describe the machine, keeping the template's numeric values."""
    path = work / session.SETUP_FILE
    path.write_text(path.read_text().replace('name = "PM-30MV"', 'name = "mill"'))


def source_at(work):
    text = (work / session.SETUP_FILE).read_text(encoding="utf-8")
    source = read_setup(text, session.SETUP_FILE)
    return source, envelope(source.setup)


def run_round(target: session.Round, seed: int) -> None:
    wav = target.path / session.RECORDING_FILE
    audio.save_wav(wav, synth.record(target.plan, seed=seed), synth.RATE)
    (target.path / session.TIMELINE_FILE).write_text(
        json.dumps(synth.timeline(target.plan)), encoding="utf-8"
    )
    session.analyze_recording(target, wav, Thresholds())


def test_session_converges_near_the_true_optimum(tmp_path, setup):
    work = tmp_path / "session"
    init_session(work)
    fill_in(work)
    source, env = source_at(work)
    for _ in range(10):
        planned = session.plan_next(work, source, env, Stock.FRESH)
        if planned.plan is None:
            break
        rounds = session.load_rounds(work)
        run_round(rounds[-1], seed=len(rounds))
    else:
        pytest.fail("did not converge in 10 rounds")

    rounds = session.load_rounds(work)
    cuts = session.observations(rounds, setup.cut.axial_depth_mm, env)
    for obs in cuts:
        truth = MACHINE.chatters(obs.asked, obs.engagement_mm, setup.tool.flutes)
        assert (obs.verdict is Verdict.UNSTABLE) == truth
    best_mrr, _ = bayes.best_stable(setup, env, cuts)[0]
    assert best_mrr >= 0.85 * true_optimum(setup, env)
    assert len(cuts) <= 40

    # Converged: a chip-load sweep of the best cut is offered, planned, and analysed.
    found = session.best_cuts(setup, env, rounds)
    done = session.swept(env, found, session.sweeps(rounds, env))
    assert session.to_sweep(setup, env, found, done)
    session.plan_sweep(work, source, env, Stock.FRESH)
    sweep = session.load_rounds(work)[-1]
    assert sweep.plan.planner is PlannerKind.SWEEP
    run_round(sweep, seed=99)
    rounds = session.load_rounds(work)
    assert len(session.sweeps(rounds, env)) == 1
    after = session.observations(rounds, setup.cut.axial_depth_mm, env)
    assert after == cuts, "the sweep's cuts stay off the map"


def test_plan_refuses_until_the_setup_is_filled_in(tmp_path):
    work = tmp_path / "session"
    init_session(work)
    path = work / session.SETUP_FILE
    # Re-wrapping lines or line endings in an editor fills nothing in.
    path.write_text(path.read_text().replace("\n", "\r\n  ") + "\n\n", newline="")
    source, env = source_at(work)
    with pytest.raises(PlanError, match="still the example template"):
        session.plan_next(work, source, env, Stock.SAME)
    assert session.load_rounds(work) == []

    fill_in(work)
    source, env = source_at(work)
    session.plan_next(work, source, env, Stock.SAME)
    assert [r.plan.round for r in session.load_rounds(work)] == [1]


def test_plan_refuses_until_the_last_round_is_analysed(tmp_path):
    work = tmp_path / "session"
    init_session(work)
    fill_in(work)
    source, env = source_at(work)
    session.plan_next(work, source, env, Stock.SAME)
    with pytest.raises(SessionError, match="has no results"):
        session.plan_next(work, source, env, Stock.SAME)


def test_plan_refuses_a_setup_that_changed_what_a_cut_round_measured(tmp_path):
    work = tmp_path / "session"
    init_session(work)
    fill_in(work)
    source, env = source_at(work)
    session.plan_next(work, source, env, Stock.SAME)
    first = session.load_rounds(work)[0]
    assert (first.path / session.SETUP_FILE).read_bytes() == (
        work / session.SETUP_FILE
    ).read_bytes()
    run_round(first, seed=1)

    path = work / session.SETUP_FILE
    text = path.read_text()
    path.write_text(text.replace("flutes = 3", "flutes = 2"))
    source, env = source_at(work)
    with pytest.raises(SessionError, match=r"tool\.flutes"):
        session.plan_next(work, source, env, Stock.SAME)
    assert len(session.load_rounds(work)) == 1

    path.write_text(text.replace("safe_z_mm = 10.0", "safe_z_mm = 20.0"))
    source, env = source_at(work)
    session.plan_next(work, source, env, Stock.SAME)
    assert len(session.load_rounds(work)) == 2
