"""Read and write WAV recordings, and measure cutting sound level."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

HIGH_PASS_HZ = 150.0


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
    """Mono float32 samples in [-1, 1] and sample rate from a WAV file."""
    try:
        return _load_wav16(path)
    except wave.Error:
        pass
    try:
        rate, data = wavfile.read(path)
    except Exception as exc:
        raise AudioError(f"cannot read WAV file {path.name}: {exc}") from exc
    samples = np.atleast_2d(data.T).T
    if np.issubdtype(samples.dtype, np.unsignedinteger):
        samples = (samples.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(samples.dtype, np.signedinteger):
        scale = float(1 << (8 * samples.dtype.itemsize - 1))
        samples = samples.astype(np.float32) / scale
    else:
        samples = samples.astype(np.float32)
    return samples.mean(axis=1), int(rate)


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


def level_db(samples: np.ndarray, rate: int) -> float:
    """The mean level of a stretch of recording, in dB, above the rumble band."""
    if len(samples) == 0:
        return -140.0
    sos = signal.butter(4, HIGH_PASS_HZ, "highpass", fs=rate, output="sos")
    return float(10 * np.log10(np.mean(signal.sosfilt(sos, samples) ** 2) + 1e-14))
