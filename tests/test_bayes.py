from __future__ import annotations

import numpy as np
import pytest
import soundfile
import synth

from dechatter import bayes, cli, session
from dechatter.bayes import Observation
from dechatter.detect import Verdict
from dechatter.plan import envelope, grid_tests, mrr_mm3_min

MACHINE = synth.Machine()


def p_at(stability: bayes.StabilityMap, rpm: float, width: float) -> float:
    return float(stability.p_stable()[stability.speed_index(rpm), stability.width_index(width)])


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
    best = max(mrr_mm3_min(setup, env, o.rpm, o.width_mm) for o in seen)
    for rpm, width, p in proposal.tests:
        assert p == pytest.approx(p_at(measured, rpm, width))
        assert p >= setup.planner.probability_floor
        assert mrr_mm3_min(setup, env, rpm, width) > best
        assert mrr_mm3_min(setup, env, rpm, width) <= env.mrr_limit_mm3_min
    assert max(p for _, _, p in proposal.tests) >= setup.planner.min_probability


def test_nothing_proposed_once_the_width_limit_is_reached_at_top_speed(setup):
    env = envelope(setup)
    top = Observation(env.speed_max_rpm, env.widths(0.1)[-1], Verdict.STABLE)
    assert bayes.propose(setup, env, [top]).tests == []


def test_a_depth_is_left_once_no_likely_cut_would_beat_what_another_depth_reached(setup):
    env = envelope(setup)
    here = [Observation(4600, 3.0, Verdict.STABLE)]
    widest = env.widths(setup.planner.width_step_mm)[-1]
    within_reach = mrr_mm3_min(setup, env, 4600, 3.4)
    out_of_reach = 0.95 * mrr_mm3_min(setup, env, 4600, widest)
    assert bayes.propose(setup, env, here, within_reach).tests
    assert bayes.propose(setup, env, here, out_of_reach).tests == []


def true_optimum(setup, env) -> float:
    flutes = setup.tool.flutes
    return max(
        mrr_mm3_min(setup, env, rpm, min(MACHINE.limit_mm(rpm, flutes), env.width_max_mm))
        for rpm in env.speeds(10)
    )


def fill_in(work) -> None:
    """Describe the machine, as the user does after `init`, keeping the template's values."""
    path = work / session.SETUP_FILE
    path.write_text(path.read_text().replace('name = "PM-30MV"', 'name = "mill"'))


def test_session_converges_near_the_true_optimum_from_recordings_alone(tmp_path, setup):
    work = tmp_path / "session"
    assert cli.main(["init", str(work)]) == 0
    fill_in(work)
    for _ in range(10):
        planned = len(session.load_rounds(work))
        assert cli.main(["plan", str(work), "--new-stock"]) == 0
        rounds = session.load_rounds(work)
        if len(rounds) == planned:
            break
        wav = rounds[-1].path / "recording.wav"
        soundfile.write(wav, synth.record(rounds[-1].plan, seed=len(rounds)), synth.RATE)
        assert cli.main(["analyze", str(work), str(wav)]) == 0
    else:
        pytest.fail("did not converge in 10 rounds")

    env = envelope(setup)
    cuts = session.observations(session.load_rounds(work), setup.cut.axial_depth_mm)
    for obs in cuts:
        truth = MACHINE.chatters(obs.rpm, obs.width_mm, setup.tool.flutes)
        assert (obs.verdict is Verdict.UNSTABLE) == truth
    best_mrr, _ = bayes.best_stable(setup, env, cuts)[0]
    assert best_mrr >= 0.85 * true_optimum(setup, env)
    assert len(cuts) <= 40


def test_plan_refuses_until_the_setup_is_filled_in(tmp_path, capsys):
    work = tmp_path / "session"
    cli.main(["init", str(work)])
    path = work / session.SETUP_FILE
    # Re-wrapping lines or line endings in an editor fills nothing in.
    path.write_text(path.read_text().replace("\n", "\r\n  ") + "\n\n", newline="")
    assert cli.main(["plan", str(work)]) == 1
    assert "still the example template" in capsys.readouterr().err
    assert session.load_rounds(work) == []

    fill_in(work)
    assert cli.main(["plan", str(work)]) == 0
    assert [r.plan.round for r in session.load_rounds(work)] == [1]


def test_plan_refuses_until_the_last_round_is_analysed(tmp_path, capsys):
    work = tmp_path / "session"
    cli.main(["init", str(work)])
    fill_in(work)
    assert cli.main(["plan", str(work)]) == 0
    assert cli.main(["plan", str(work)]) == 1
    assert "dechatter analyze" in capsys.readouterr().err
