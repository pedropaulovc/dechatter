"""Bayesian stability map over spindle speed x cut width, and the next batch of test cuts.

After Karandikar et al., "Evaluation of automated stability testing in machining through
closed-loop control and Bayesian machine learning", MSSP 181 (2022): a probability of
stability on a grid, tests chosen by expected improvement in material removal rate, a floor
on the probability of stability of any test, stop when the expected improvement is under 5%.

Differences, because one round here is a batch run without feedback between cuts:
- The paper omits its likelihood. Here each speed has a distribution over its stability
  limit (the largest stable width). A stable cut says the limit is above it, an unstable one
  below, each wrong with probability `misclassification`; evidence fades with speed distance.
- A batch is picked greedily. Later picks assume earlier ones pass (so widths can ramp at a
  speed) but must also clear `probability_floor` on measured data alone.
- A chatter frequency predicts speeds where a tooth passes once per vibration cycle. Those
  speeds get partial credit for the width that was stable where the chatter was heard.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Setup
from .detect import Verdict
from .plan import Envelope, mrr_mm3_min

PRIOR_AT_MAX_WIDTH = 0.05
EXCLUSION_SIGMAS = 2.0
SWEET_SPOT_POCKET = 0.25  # pocket half-width as a fraction of the spacing to the next lobe
MODE_TOLERANCE = 0.02  # chatter frequencies this close together are the same mode


@dataclass(frozen=True)
class Observation:
    rpm: float
    width_mm: float
    verdict: Verdict
    chatter_hz: float | None = None


class StabilityMap:
    def __init__(self, speeds: np.ndarray, widths: np.ndarray, misclassification: float):
        self.speeds = speeds
        self.widths = widths
        self.misclassification = misclassification
        survival = 1 - (1 - PRIOR_AT_MAX_WIDTH) * widths / widths[-1]
        # limit[j]: exactly the first j width levels are stable, j = 0..len(widths)
        limit = -np.diff(np.concatenate(([1.0], survival, [0.0])))
        self._log_limit = np.tile(np.log(np.maximum(limit, 1e-12)), (len(speeds), 1))

    def copy(self) -> StabilityMap:
        clone = object.__new__(StabilityMap)
        clone.__dict__ = {**self.__dict__, "_log_limit": self._log_limit.copy()}
        return clone

    def width_index(self, width_mm: float) -> int:
        return int(np.argmin(np.abs(self.widths - width_mm)))

    def speed_index(self, rpm: float) -> int:
        return int(np.argmin(np.abs(self.speeds - rpm)))

    def update(
        self, rpm: float, width_mm: float, verdict: Verdict, sigma_rpm: float, weight: float = 1.0
    ) -> None:
        levels = np.arange(len(self.widths) + 1)
        agrees = levels > self.width_index(width_mm)
        if verdict is Verdict.UNSTABLE:
            agrees = ~agrees
        log_likelihood = np.where(agrees, 0.0, np.log(self.misclassification))
        reach = weight * np.exp(-((self.speeds - rpm) ** 2) / (2 * sigma_rpm**2))
        self._log_limit += np.outer(reach, log_likelihood)

    def p_stable(self) -> np.ndarray:
        """P(stable) with shape (speeds, widths)."""
        limit = np.exp(self._log_limit - self._log_limit.max(axis=1, keepdims=True))
        limit /= limit.sum(axis=1, keepdims=True)
        return np.cumsum(limit[:, ::-1], axis=1)[:, ::-1][:, 1:]


def sweet_spot_speeds(chatter_hz: float, flutes: int, low_rpm: float, high_rpm: float):
    """(lobe, rpm) where tooth passing divides the chatter frequency: n = 60 f / (flutes k)."""
    first = max(1, int(np.ceil(60 * chatter_hz / (flutes * high_rpm))))
    last = int(60 * chatter_hz / (flutes * low_rpm))
    return [(k, 60 * chatter_hz / (flutes * k)) for k in range(first, last + 1)]


def build_map(setup: Setup, env: Envelope, observations: list[Observation]) -> StabilityMap:
    cfg = setup.planner
    stability = StabilityMap(
        env.speeds(cfg.speed_step_rpm), env.widths(cfg.width_step_mm), cfg.misclassification
    )
    for obs in observations:
        stability.update(obs.rpm, obs.width_mm, obs.verdict, cfg.speed_sigma_fraction * obs.rpm)

    for chatter_hz, width_mm in chatter_modes(observations, cfg.speed_sigma_fraction):
        spots = sweet_spot_speeds(
            chatter_hz, setup.tool.flutes, env.speed_min_rpm, env.speed_max_rpm
        )
        for lobe, rpm in spots:
            pocket = SWEET_SPOT_POCKET * rpm / (lobe + 1)
            stability.update(
                rpm,
                width_mm,
                Verdict.STABLE,
                min(pocket, cfg.speed_sigma_fraction * rpm),
                cfg.sweet_spot_trust,
            )
    return stability


def chatter_modes(observations: list[Observation], sigma_fraction: float):
    """(chatter Hz, widest width stable just below that chatter), one per vibration mode.

    Chatter heard in many cuts at about the same frequency is one mode, hence one hint:
    counting it once per cut would let the hint outweigh measured cuts.
    """
    heard = []
    for obs in observations:
        if obs.verdict is not Verdict.UNSTABLE or obs.chatter_hz is None:
            continue
        below = [
            o.width_mm
            for o in observations
            if o.verdict is Verdict.STABLE
            and abs(o.rpm - obs.rpm) <= sigma_fraction * obs.rpm
            and o.width_mm < obs.width_mm
        ]
        if below:
            heard.append((obs.chatter_hz, max(below)))

    modes: list[list[tuple[float, float]]] = []
    for item in sorted(heard):
        if modes and item[0] <= modes[-1][0][0] * (1 + MODE_TOLERANCE):
            modes[-1].append(item)
            continue
        modes.append([item])
    return [
        (float(np.median([hz for hz, _ in mode])), max(width for _, width in mode))
        for mode in modes
    ]


def mrr_grid(setup: Setup, env: Envelope, stability: StabilityMap) -> np.ndarray:
    return mrr_mm3_min(setup, env, stability.speeds[:, None], stability.widths[None, :])


def best_stable(
    setup: Setup, env: Envelope, observations: list[Observation]
) -> list[tuple[float, Observation]]:
    """Stable observations, highest removal rate first, as (mm3/min, observation)."""
    stable = [o for o in observations if o.verdict is Verdict.STABLE]
    rated = [(float(mrr_mm3_min(setup, env, o.rpm, o.width_mm)), o) for o in stable]
    return sorted(rated, key=lambda pair: -pair[0])


@dataclass(frozen=True)
class Proposal:
    tests: list[tuple[float, float, float]]  # rpm, width_mm, P(stable) on measured data
    expected_improvement: float  # of the most promising cut, as a fraction of the best MRR


def propose(setup: Setup, env: Envelope, observations: list[Observation]) -> Proposal:
    cfg = setup.planner
    measured = build_map(setup, env, observations)
    assumed = measured.copy()
    p_measured = measured.p_stable()
    mrr = mrr_grid(setup, env, measured)

    ranked = best_stable(setup, env, observations)
    best = ranked[0][0] if ranked else float(mrr[-1, 0])
    improvement = p_measured * np.clip(mrr / best - 1, 0, None)

    allowed = (mrr <= env.mrr_limit_mm3_min) & (p_measured >= cfg.probability_floor)
    for obs in observations:
        allowed[measured.speed_index(obs.rpm), measured.width_index(obs.width_mm)] = False

    tests: list[tuple[float, float, float]] = []
    headline = 0.0
    for _ in range(cfg.batch_size):
        candidates = np.where(
            allowed & (assumed.p_stable() >= cfg.min_probability), improvement, 0.0
        )
        s, w = np.unravel_index(np.argmax(candidates), candidates.shape)
        if not tests:
            headline = float(candidates[s, w])
        if candidates[s, w] < cfg.stop_improvement:
            break
        rpm, width = float(measured.speeds[s]), float(measured.widths[w])
        tests.append((rpm, width, float(p_measured[s, w])))
        sigma = cfg.speed_sigma_fraction * rpm
        assumed.update(rpm, width, Verdict.STABLE, sigma)
        near = np.abs(measured.speeds - rpm) <= EXCLUSION_SIGMAS * sigma
        allowed[np.ix_(near, np.arange(len(measured.widths)) <= w)] = False
    return Proposal(tests, headline)
