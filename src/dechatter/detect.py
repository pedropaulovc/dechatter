"""Classify one pass as stable or chattering from its sound.

Forced vibration sits on harmonics of the spindle rotation (tooth passing is harmonic
`flutes`). Chatter is a strong tone anywhere else. So: comb out the spindle harmonics, take
the dominant remaining peak, and call it chatter when it stands clear of the cut's own noise
floor and of the spindle running in air just before the pass.

The prominence threshold is calibrated on the Purdue cutting-sound dataset; rerun
`validation/purdue.py` after changing anything here.
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
HARMONIC_SCORE_CAP_DB = 20.0


class Verdict(StrEnum):
    STABLE = "stable"
    UNSTABLE = "unstable"


class TooShort(ValueError):
    pass


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


def _spindle_hz(freqs: np.ndarray, psd: np.ndarray, nominal_hz: float, flutes: int) -> float:
    """Actual rotation frequency: the candidate with the most tooth-passing harmonics present.

    Each harmonic's score saturates, so one huge tone (chatter) landing on a single harmonic
    of a wrong candidate cannot outvote the many harmonics of the right one.
    """
    orders = np.arange(1, min(MAX_TOOTH_ORDERS, int(BAND_HZ[1] / (nominal_hz * flutes))) + 1)
    if len(orders) == 0:
        return nominal_hz
    candidates = nominal_hz * (1 + np.linspace(-RPM_TOLERANCE, RPM_TOLERANCE, RPM_SEARCH_STEPS))
    level_db = 10 * np.log10(psd / np.median(psd[freqs >= BAND_HZ[0]]) + 1e-30)
    harmonics = np.outer(candidates * flutes, orders)
    score = np.clip(np.interp(harmonics, freqs, level_db), 0, HARMONIC_SCORE_CAP_DB).sum(axis=1)
    return float(candidates[np.argmax(score)])


def _on_comb(freqs: np.ndarray, spindle_hz: float) -> np.ndarray:
    distance = np.abs(freqs - np.round(freqs / spindle_hz) * spindle_hz)
    bin_hz = freqs[1] - freqs[0]
    tolerance = np.minimum(np.maximum(2.5 * bin_hz, 0.002 * freqs), 0.2 * spindle_hz)
    return distance <= tolerance


def _db(ratio: float) -> float:
    return 10 * math.log10(max(ratio, 1e-30))


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

    spindle_hz = _spindle_hz(freqs, psd, rpm / 60, flutes)
    comb = _on_comb(freqs, spindle_hz)
    candidates = band & ~comb
    excess_ratio = None
    if air is not None and len(air) >= window:
        _, air_psd = _psd(air, rate, window)
        excess_ratio = psd / maximum_filter1d(air_psd, 5)
        louder = candidates & (excess_ratio >= 10 ** (thresholds.excess_db / 10))
        # A tone the idling spindle already makes (drive whine) is not chatter: look past it.
        candidates = louder if louder.any() else candidates
    peak = int(np.argmax(np.where(candidates, psd, 0.0)))

    prominence = _db(psd[peak] / np.median(psd[band & ~comb]))
    excess = None if excess_ratio is None else _db(excess_ratio[peak])

    chatter = prominence >= thresholds.prominence_db and (
        excess is None or excess >= thresholds.excess_db
    )
    return Reading(
        verdict=Verdict.UNSTABLE if chatter else Verdict.STABLE,
        chatter_hz=float(freqs[peak]),
        spindle_hz=spindle_hz,
        prominence_db=prominence,
        excess_db=excess,
    )
