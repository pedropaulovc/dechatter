"""Load a recording, find the cuts in it, and match them to the planned passes."""

from __future__ import annotations

import csv
import logging
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile
from scipy import signal

from .plan import PassKind, Plan

log = logging.getLogger(__name__)

HIGH_PASS_HZ = 150.0
FRAME_S = 0.05
HOP_S = 0.025
MERGE_GAP_S = 0.4
DURATION_TOLERANCE = 1.35
THRESHOLD_STEPS = 32
TRAILING_SKIP = 0.5


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def load(path: Path) -> tuple[np.ndarray, int]:
    """Mono float samples and sample rate. Formats libsndfile lacks (m4a, aac) go through ffmpeg."""
    try:
        samples, rate = soundfile.read(path, dtype="float32", always_2d=True)
    except soundfile.LibsndfileError:
        samples, rate = _load_via_ffmpeg(path)
    return samples.mean(axis=1), rate


def _load_via_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioError(f"cannot decode {path.name}: install ffmpeg or convert it to WAV")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "decoded.wav"
        done = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-ac", "1", str(wav)],
            capture_output=True,
            text=True,
        )
        if done.returncode != 0:
            raise AudioError(f"ffmpeg could not decode {path.name}: {done.stderr.strip()}")
        return soundfile.read(wav, dtype="float32", always_2d=True)


def envelope_db(samples: np.ndarray, rate: int) -> np.ndarray:
    """Short-time level in dB, above the rumble band. One value per HOP_S."""
    sos = signal.butter(4, HIGH_PASS_HZ, "highpass", fs=rate, output="sos")
    power = signal.sosfilt(sos, samples) ** 2
    frame, hop = int(FRAME_S * rate), int(HOP_S * rate)
    cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    starts = np.arange(0, len(power) - frame + 1, hop)
    if len(starts) == 0:
        raise AudioError("recording is shorter than one analysis frame")
    mean = (cumulative[starts + frame] - cumulative[starts]) / frame
    return 10 * np.log10(mean + 1e-14)


def segments_above(level_db: np.ndarray, threshold_db: float, min_s: float) -> list[Segment]:
    edges = np.flatnonzero(np.diff(np.concatenate(([0], level_db > threshold_db, [0]))))
    pairs = zip(edges[::2], edges[1::2], strict=True)
    runs = [[a * HOP_S, b * HOP_S + FRAME_S - HOP_S] for a, b in pairs]
    merged: list[list[float]] = []
    for run in runs:
        if merged and run[0] - merged[-1][1] <= MERGE_GAP_S:
            merged[-1][1] = run[1]
            continue
        merged.append(run)
    return [Segment(a, b) for a, b in merged if b - a >= min_s]


@dataclass(frozen=True)
class Alignment:
    cost: float
    matches: dict[int, Segment]  # index into plan.passes -> segment
    unmatched: list[Segment]  # detected, in time order, but fitting no pass


def align(plan: Plan, segments: list[Segment]) -> Alignment:
    """Order-preserving match of detected cuts to planned passes by cut duration.

    A pass may be skipped (not run, or too quiet to detect) and a segment may be dropped
    (a noise that is not a cut); each costs 1, or 0.3 for skipping a light preparation pass.
    Passes skipped after the last cut cost half: a run stopped early is likelier than one
    whose first passes went unheard, and equal-speed passes make the two look alike.
    """
    passes = plan.passes
    skip = [1.0 if p.kind is PassKind.TEST else 0.3 for p in passes]
    n, m = len(passes), len(segments)
    cost = np.full((n + 1, m + 1), math.inf)
    cost[0, :] = np.arange(m + 1)
    cost[1:, 0] = np.cumsum(skip)
    move = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 match, 1 skip pass, 2 drop segment
    move[0, 1:] = 2
    move[1:, 0] = 1
    limit = math.log(DURATION_TOLERANCE)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            mismatch = abs(math.log(segments[j - 1].duration_s / passes[i - 1].expected_cut_s))
            options = (
                cost[i - 1, j - 1] + (mismatch / limit if mismatch <= limit else math.inf),
                cost[i - 1, j] + skip[i - 1] * (TRAILING_SKIP if j == m else 1.0),
                cost[i, j - 1] + 1.0,
            )
            move[i, j] = int(np.argmin(options))
            cost[i, j] = options[move[i, j]]

    matches: dict[int, Segment] = {}
    i, j = n, m
    while i > 0 or j > 0:
        step = move[i, j]
        if step == 0:
            matches[i - 1] = segments[j - 1]
        i -= step != 2
        j -= step != 1
    used = set(matches.values())
    return Alignment(float(cost[n, m]), matches, [s for s in segments if s not in used])


def find_cuts(samples: np.ndarray, rate: int, plan: Plan) -> Alignment:
    """Sweep the level threshold; keep the one whose cuts best fit the plan's durations."""
    level = envelope_db(samples, rate)
    low, high = np.percentile(level, [2, 98])
    min_s = 0.5 * min(p.expected_cut_s for p in plan.passes)
    thresholds = np.linspace(low, high, THRESHOLD_STEPS + 2)[1:-1]
    alignments = [align(plan, segments_above(level, t, min_s)) for t in thresholds]
    costs = np.array([a.cost for a in alignments])
    # Middle of the near-optimal thresholds: the widest margin from both noise and cuts.
    good = np.flatnonzero(costs <= costs.min() + 0.1)
    chosen = good[len(good) // 2]
    log.info(
        "segmentation: threshold %.1f dB (range %.1f..%.1f), cost %.2f, %d of %d passes matched",
        thresholds[chosen],
        low,
        high,
        costs[chosen],
        len(alignments[chosen].matches),
        len(plan.passes),
    )
    if not alignments[chosen].matches:
        raise AudioError(
            "no cut in the recording matches the plan's pass durations; "
            "pass --segments with start,end times per cut"
        )
    return alignments[chosen]


def read_segments(path: Path) -> list[Segment]:
    """CSV of `start,end` seconds per cut (an Audacity label export works with tabs too)."""
    text = path.read_text(encoding="utf-8")
    dialect = csv.excel_tab if "\t" in text else csv.excel
    rows = [row for row in csv.reader(text.splitlines(), dialect) if row]
    segments = []
    for row in rows:
        try:
            segments.append(Segment(float(row[0]), float(row[1])))
        except (ValueError, IndexError):
            if segments:
                raise AudioError(f"{path.name}: bad row {row}") from None
    if not segments:
        raise AudioError(f"{path.name}: no start,end rows")
    return segments
