"""Classify one pass as stable or chattering from its sound.

Forced vibration sits on harmonics of the spindle rotation (tooth passing is harmonic
`flutes`). Chatter is a strong tone anywhere else. So: comb out the spindle harmonics, take
the dominant remaining peak, and call it chatter when it stands clear of the cut's own noise
floor and of the spindle running in air just before the pass.

The prominence threshold is calibrated on the Purdue cutting-sound dataset and checked against
real 1018 steel widget sessions; rerun `validation/purdue.py` after changing anything here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter1d

BAND_HZ = (150.0, 8000.0)
WINDOW_S = 0.75
MIN_WINDOW = 2048
RPM_TOLERANCE = 0.05
RPM_SEARCH_STEPS = 501
MAX_TOOTH_ORDERS = 40
MAX_SPINDLE_ORDERS = 60
HARMONIC_SCORE_CAP_DB = 20.0
RUNOUT_WEIGHT = 0.5
AIR_SPINDLE_WEIGHT = 0.25
# Regenerative chatter drifts a few Hz as engagement and compliance change along a cut,
# spreading its power across several Welch bins; excess power within this half-width around the
# peak is summed back into an equivalent single-bin height.
PEAK_WANDER_HZ = 6.0
PEAK_WANDER_NOISE = 3.0
PEAK_WANDER_BINS = 2.0
# Heavy or clipped chatter floods the non-harmonic spectrum and inflates the cut's own median;
# capping the floor's rise over the air dwell keeps such a cut from hiding itself.
MAX_FLOOR_RISE_DB = 20.0


class Verdict(StrEnum):
    STABLE = "stable"
    UNSTABLE = "unstable"


class TooShort(ValueError):
    pass


class Silent(ValueError):
    """No sound at all in the cut: nothing to measure a peak, or its prominence, against."""


@dataclass(frozen=True)
class Thresholds:
    prominence_db: float = 27.0  # peak over the median of the comb-filtered spectrum
    excess_db: float = 10.0  # peak over the air-cut spectrum at the same frequency


DEFAULT_THRESHOLDS = Thresholds()


@dataclass(frozen=True)
class Reading:
    verdict: Verdict
    chatter_hz: float  # dominant non-harmonic peak, whatever the verdict
    spindle_hz: float
    prominence_db: float
    excess_db: float | None


def _window(samples: int, rate: int) -> int:
    fit = 2 ** int(math.log2(max(samples // 2, 1)))
    return min(2 ** int(math.log2(WINDOW_S * rate)), fit)


def _psd(samples: np.ndarray, rate: int, window: int) -> tuple[np.ndarray, np.ndarray]:
    return signal.welch(samples, rate, window="hann", nperseg=window, noverlap=window // 2)


def _spindle_hz(
    freqs: np.ndarray,
    psd: np.ndarray,
    air_psd: np.ndarray | None,
    band: np.ndarray,
    nominal_hz: float,
    flutes: int,
) -> float:
    """Actual rotation frequency: the candidate with the most spindle harmonics present.

    Tooth-passing harmonics carry full weight, runout harmonics between them carry half, and
    the idling spindle's harmonics in the air dwell break ties when loud chatter drowns the
    cut's harmonics without preventing a loaded spindle from drooping slightly under a heavy
    cut. Each harmonic's score saturates, and levels are taken against the floor of the
    analysis band only.
    """
    tooth_orders = np.arange(1, min(MAX_TOOTH_ORDERS, int(BAND_HZ[1] / (nominal_hz * flutes))) + 1)
    if len(tooth_orders) == 0:
        return nominal_hz
    candidates = nominal_hz * (1 + np.linspace(-RPM_TOLERANCE, RPM_TOLERANCE, RPM_SEARCH_STEPS))
    level_db = _db(psd, np.median(psd[band]))
    score = np.clip(
        np.interp(np.outer(candidates * flutes, tooth_orders), freqs, level_db),
        0,
        HARMONIC_SCORE_CAP_DB,
    ).sum(axis=1)
    runout_orders = np.array(
        [
            k
            for k in range(1, min(MAX_SPINDLE_ORDERS, int(BAND_HZ[1] / nominal_hz)) + 1)
            if k % flutes != 0
        ]
    )
    if len(runout_orders):
        score += RUNOUT_WEIGHT * np.clip(
            np.interp(np.outer(candidates, runout_orders), freqs, level_db),
            0,
            HARMONIC_SCORE_CAP_DB,
        ).sum(axis=1)
    if air_psd is not None and air_psd[band].any():
        air_orders = np.arange(1, min(MAX_TOOTH_ORDERS, int(BAND_HZ[1] / nominal_hz)) + 1)
        if len(air_orders):
            air_level_db = _db(air_psd, np.median(air_psd[band]))
            score += AIR_SPINDLE_WEIGHT * np.clip(
                np.interp(np.outer(candidates, air_orders), freqs, air_level_db),
                0,
                HARMONIC_SCORE_CAP_DB,
            ).sum(axis=1)
    return float(candidates[np.argmax(score)])


def _on_comb(freqs: np.ndarray, spindle_hz: float) -> np.ndarray:
    distance = np.abs(freqs - np.round(freqs / spindle_hz) * spindle_hz)
    bin_hz = freqs[1] - freqs[0]
    tolerance = np.minimum(np.maximum(2.5 * bin_hz, 0.002 * freqs), 0.2 * spindle_hz)
    return distance <= tolerance


def _db(power: np.ndarray | float, reference: np.ndarray | float) -> np.ndarray | float:
    """`power` over `reference` in dB, finite even where either is zero (digital silence)."""
    return 10 * np.log10(np.maximum(power, 1e-30) / np.maximum(reference, 1e-30))


def classify(
    cut: np.ndarray,
    air: np.ndarray | None,
    rate: int,
    rpm: float,
    flutes: int,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> Reading:
    window = _window(len(cut), rate)
    if window < MIN_WINDOW:
        raise TooShort(f"{len(cut) / rate:.2f} s of cut is too short to analyse")
    freqs, psd = _psd(cut, rate, window)
    band = (freqs >= BAND_HZ[0]) & (freqs <= min(BAND_HZ[1], 0.45 * rate))
    if not psd[band].any():
        raise Silent(f"{len(cut) / rate:.2f} s of cut is silent: no sound was recorded in it")

    air_psd = None
    if air is not None and len(air) >= window:
        _, air_psd = _psd(air, rate, window)

    spindle_hz = _spindle_hz(freqs, psd, air_psd, band, rpm / 60, flutes)
    comb = _on_comb(freqs, spindle_hz)
    candidates = band & ~comb
    excess = None
    if air_psd is not None:
        excess = _db(psd, maximum_filter1d(air_psd, 5))
        louder = candidates & (excess >= thresholds.excess_db)
        # A tone the idling spindle already makes (drive whine) is not chatter: look past it.
        candidates = louder if louder.any() else candidates
    peak = int(np.argmax(np.where(candidates, psd, 0.0)))

    cut_floor = float(np.median(psd[band & ~comb]))
    floor = cut_floor
    if air_psd is not None and air_psd[band & ~comb].any():
        air_floor = float(np.median(air_psd[band & ~comb]))
        floor = min(floor, air_floor * 10 ** (MAX_FLOOR_RISE_DB / 10))
    wander = max(1, int(round(PEAK_WANDER_HZ / (freqs[1] - freqs[0]))))
    near = np.zeros_like(candidates)
    near[max(0, peak - wander) : min(len(psd), peak + wander + 1)] = True
    above = np.maximum(psd[near & candidates] - PEAK_WANDER_NOISE * cut_floor, 0.0)
    peak_power = max(float(psd[peak]), float(np.sum(above)) / PEAK_WANDER_BINS + cut_floor)
    prominence = float(_db(peak_power, floor))
    peak_excess = None if excess is None else float(excess[peak])
    chatter = prominence >= thresholds.prominence_db and (
        peak_excess is None or peak_excess >= thresholds.excess_db
    )
    return Reading(
        verdict=Verdict.UNSTABLE if chatter else Verdict.STABLE,
        chatter_hz=float(freqs[peak]),
        spindle_hz=spindle_hz,
        prominence_db=prominence,
        excess_db=peak_excess,
    )
