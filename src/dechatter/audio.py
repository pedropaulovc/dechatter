"""Load a recording, find the cuts in it, and match them to the planned passes."""

from __future__ import annotations

import csv
import logging
import math
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import ndimage, signal

from .plan import PassKind, Plan

log = logging.getLogger(__name__)

HIGH_PASS_HZ = 150.0
FRAME_S = 0.05
HOP_S = 0.025
MERGE_GAP_S = 0.4
DURATION_TOLERANCE = 1.35
THRESHOLD_STEPS = 32
# Passes skipped after the last cut found: the run stopped there, which is common.
TRAILING_SKIP = 0.25
# A cut is told from the spindle running in the air by its level over the floor around it:
# the spindle's own noise rises with its speed, by 8 dB or more across a round, while a light
# cut is only a few dB over the spindle. The floor is a low percentile of the level over a
# window spanning a few passes, the percentile lower the less of a pass is spent in the air
# (dwell and lead-ins), so that it lands on air even beside long cuts.
FLOOR_PASSES = 3
FLOOR_MIN_S = 80.0
FLOOR_PERCENTILE_PER_AIR = 40  # percentile per unit of the smallest air share of a pass
FLOOR_PERCENTILE = (5, 25)
# Silence (the spindle off) is told apart by its level and where it is. It can only be at
# the lowest mode of the level's histogram (within a margin). That mode is silence when
# what lies above it runs on longer than a pass's cycle without dropping to it: the spindle
# in the air and its cuts. Over a quiet spindle, loud cuts rise from it one at a time, each
# no longer than its cycle. Then whatever sits far under the spindle (the floor percentile
# of the rest: it lands on the air) is silence, however short: a pause mid-round, or a
# recorder left running after it, between knocks. What sits only a margin under the
# spindle is silence where the spindle cannot be: in stretches longer than a pass's cycle,
# or at either end of the recording; a spindle left running after the round is as long,
# but no quieter. No fixed offset from the median or a percentile holds: silence can be
# most of a recording, and a cut 40 dB over the spindle.
SILENCE_MARGIN_DB = 6.0
SILENCE_FAR_DB = 20.0
MODE_MIN_SHARE = 0.02  # of the frames, for a histogram peak to count as a mode
LOWEST_THRESHOLD_DB = 1.0


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
    """Mono float samples and sample rate.

    16-bit WAV is read with the standard library, which is all the browser build has. Other
    formats go through libsndfile, and what that lacks (m4a, aac) through ffmpeg.
    """
    if path.suffix.lower() == ".wav":
        try:
            return _load_wav16(path)
        except wave.Error:
            pass
    import soundfile

    try:
        samples, rate = soundfile.read(path, dtype="float32", always_2d=True)
    except soundfile.LibsndfileError:
        samples, rate = _load_via_ffmpeg(path)
    return samples.mean(axis=1), rate


def _load_wav16(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise wave.Error("not 16-bit PCM")
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        samples = frames.reshape(-1, wav.getnchannels()).astype(np.float32) / 32768
        return samples.mean(axis=1), wav.getframerate()


def save_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    """Mono 16-bit WAV."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


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
        import soundfile

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


def pass_cycle_s(plan: Plan) -> float:
    """The longest pass, from its dwell to the end of its lead-out."""
    return max(p.expected_cut_s + 2 * p.lead_in_s for p in plan.passes) + plan.dwell_s


def floor_window_s(plan: Plan) -> float:
    return max(FLOOR_MIN_S, FLOOR_PASSES * pass_cycle_s(plan))


def floor_percentile(plan: Plan) -> float:
    def air(p) -> float:
        return plan.dwell_s + 2 * p.lead_in_s

    share = min(air(p) / (air(p) + p.expected_cut_s) for p in plan.passes)
    low, high = FLOOR_PERCENTILE
    return float(np.clip(FLOOR_PERCENTILE_PER_AIR * share, low, high))


def lowest_mode_db(level_db: np.ndarray) -> float:
    """The lowest level the recording dwells at: the lowest peak of its histogram (1 dB
    bins, smoothed) that holds at least MODE_MIN_SHARE of the frames."""
    low, high = np.floor(level_db.min()), np.ceil(level_db.max()) + 1
    counts, edges = np.histogram(level_db, bins=np.arange(low, high + 1))
    smooth = np.convolve(counts, np.ones(3) / 3)[1:-1]  # "same" would pad a short histogram
    padded = np.concatenate(([-1.0], smooth, [-1.0]))
    peaks = (padded[1:-1] >= padded[:-2]) & (padded[1:-1] > padded[2:])
    peaks &= smooth >= MODE_MIN_SHARE * len(level_db)
    first = int(np.flatnonzero(peaks)[0]) if peaks.any() else int(np.argmax(smooth))
    return float(edges[first] + 0.5)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask, [0]))))
    return list(zip(edges[::2], edges[1::2], strict=True))


def silence(level_db: np.ndarray, plan: Plan, percentile: float) -> np.ndarray:
    """Frames of silence: see the comment on SILENCE_MARGIN_DB."""
    n = len(level_db)
    cycle = pass_cycle_s(plan)
    low = level_db < lowest_mode_db(level_db) + SILENCE_MARGIN_DB
    if low.all():
        return np.zeros(n, dtype=bool)
    above = max(end - start for start, end in _runs(~low)) * HOP_S
    far = np.zeros(n, dtype=bool)
    if above > cycle:
        spindle = np.percentile(level_db[~low], percentile)
        far = low & (level_db < spindle - SILENCE_FAR_DB)
    stretches = np.zeros(n, dtype=bool)
    for start, end in _runs(low):
        if (end - start) * HOP_S > cycle or start == 0 or end == n:
            stretches[start:end] = True
    if stretches.all():
        return far
    spindle = np.percentile(level_db[~stretches], percentile)
    return far | (stretches & (level_db < spindle - SILENCE_MARGIN_DB))


def local_floor(level_db: np.ndarray, plan: Plan) -> np.ndarray:
    """The level the spindle alone makes around each moment.

    Silence (before the spindle starts, after it stops, or while it is off) is no spindle
    noise: it is left out by ranking it above everything, so the floor beside it is the
    spindle's.
    """
    percentile = floor_percentile(plan)
    size = max(1, int(floor_window_s(plan) / HOP_S))
    ranked = np.where(silence(level_db, plan, percentile), level_db.max(), level_db)
    return ndimage.percentile_filter(ranked, percentile, size=size, mode="reflect")


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
    (a noise that is not a cut, costing 1). Skipping a pass costs more the wider it is: a
    narrow cut is the one likeliest to go unheard, and at one speed the cuts run narrowest
    first, so passes of equal length are told apart by which ones are loud enough to hear.
    Passes skipped after the last cut cost a quarter: a run stopped early is likelier than
    one whose passes went unheard.
    """
    passes = plan.passes
    widest = max(p.width_mm for p in passes)
    skip = [0.3 + 0.7 * p.width_mm / widest if p.kind is PassKind.TEST else 0.3 for p in passes]
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
            matches[int(i) - 1] = segments[j - 1]
        i -= step != 2
        j -= step != 1
    used = set(matches.values())
    return Alignment(float(cost[n, m]), matches, [s for s in segments if s not in used])


def find_cuts(samples: np.ndarray, rate: int, plan: Plan) -> Alignment:
    """The cuts in a recording, matched to the plan's passes in order."""
    return find_cuts_in(envelope_db(samples, rate), plan)


def find_cuts_in(level: np.ndarray, plan: Plan) -> Alignment:
    """Sweep the threshold over the local floor; keep the one whose cuts best fit the plan."""
    over = level - local_floor(level, plan)
    low, high = LOWEST_THRESHOLD_DB, max(float(np.percentile(over, 98)), LOWEST_THRESHOLD_DB + 1)
    min_s = 0.5 * min(p.expected_cut_s for p in plan.passes)
    thresholds = np.linspace(low, high, THRESHOLD_STEPS + 2)[1:-1]
    alignments = [align(plan, segments_above(over, t, min_s)) for t in thresholds]
    # A threshold above every cut explains the recording as a run that never started, which
    # costs little now that trailing skips are cheap: only thresholds that hear a cut count.
    costs = np.array([a.cost if a.matches else math.inf for a in alignments])
    if not np.isfinite(costs).any():
        raise AudioError(
            "no cut in the recording matches the plan's pass durations; check the microphone "
            "level, or mark the cuts yourself (`dechatter analyze --segments`)"
        )
    # Middle of the near-optimal thresholds: the widest margin from both noise and cuts.
    good = np.flatnonzero(costs <= costs.min() + 0.1)
    chosen = good[len(good) // 2]
    log.info(
        "segmentation: %.1f dB over the local floor (range %.1f..%.1f), cost %.2f, "
        "%d of %d passes matched",
        thresholds[chosen],
        low,
        high,
        costs[chosen],
        len(alignments[chosen].matches),
        len(plan.passes),
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
