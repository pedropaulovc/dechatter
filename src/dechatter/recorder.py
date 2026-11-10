"""Record the microphone of the machine dechatter runs on, straight to a WAV file.

The stream stays open while serving so the widget can show a level before any cutting.
Blocks go from the audio callback through a queue to a writer thread, so a recording is on
disk as it happens and survives a crash of the server.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile

log = logging.getLogger(__name__)


class RecorderError(RuntimeError):
    pass


class RecorderState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"


@dataclass(frozen=True)
class RecorderStatus:
    state: RecorderState
    device: str
    level_db: float | None  # of the last block heard; None before the first
    seconds: float  # length of the recording in progress


class Recorder(Protocol):
    def start(self, path: Path) -> None: ...
    def stop(self) -> Path: ...
    def status(self) -> RecorderStatus: ...
    def close(self) -> None: ...


def input_devices() -> list[tuple[int, str]]:
    import sounddevice

    apis = sounddevice.query_hostapis()
    return [
        (index, f"{d['name']} ({apis[d['hostapi']]['name']})")
        for index, d in enumerate(sounddevice.query_devices())
        if d["max_input_channels"] > 0
    ]


def default_device() -> int:
    """The system default input, or the first input when the system names none."""
    import sounddevice

    default = sounddevice.default.device[0]
    if default is not None and default >= 0:
        return default
    devices = input_devices()
    if not devices:
        raise RecorderError("no audio input device found; plug in a microphone")
    return devices[0][0]


class MicRecorder:
    def __init__(self, device: int | None = None):
        import sounddevice

        self._device = default_device() if device is None else device
        info = sounddevice.query_devices(self._device)
        if info["max_input_channels"] < 1:
            raise RecorderError(f"device {self._device} ({info['name']}) has no input channels")
        self._name = info["name"]
        self._rate = int(info["default_samplerate"])
        self._lock = threading.Lock()
        self._blocks: queue.Queue[np.ndarray | None] | None = None
        self._writer: threading.Thread | None = None
        self._path: Path | None = None
        self._started = 0.0
        self._level_db: float | None = None
        try:
            self._stream = sounddevice.InputStream(
                device=self._device,
                samplerate=self._rate,
                channels=1,
                dtype="float32",
                callback=self._on_block,
            )
            self._stream.start()
        except sounddevice.PortAudioError as exc:
            raise RecorderError(f"cannot open {self._name}: {exc}") from exc
        log.info("listening on device %d (%s) at %d Hz", self._device, self._name, self._rate)

    def _on_block(self, block: np.ndarray, _frames, _time, status) -> None:
        if status:
            log.warning("audio input: %s", status)
        power = float(np.mean(block**2))
        self._level_db = 10 * math.log10(power + 1e-12)
        blocks = self._blocks
        if blocks is not None:
            blocks.put(block.copy())

    def _write(self, path: Path, blocks: queue.Queue[np.ndarray | None]) -> None:
        with soundfile.SoundFile(path, "w", self._rate, 1, "PCM_16") as wav:
            while (block := blocks.get()) is not None:
                wav.write(block)
                wav.flush()

    def start(self, path: Path) -> None:
        with self._lock:
            if self._blocks is not None:
                raise RecorderError("already recording")
            blocks: queue.Queue[np.ndarray | None] = queue.Queue()
            self._writer = threading.Thread(target=self._write, args=(path, blocks), daemon=True)
            self._writer.start()
            self._path, self._started, self._blocks = path, time.monotonic(), blocks
        log.info("recording to %s", path)

    def stop(self) -> Path:
        with self._lock:
            if self._blocks is None or self._writer is None or self._path is None:
                raise RecorderError("not recording")
            blocks, self._blocks = self._blocks, None
            blocks.put(None)
            self._writer.join()
            log.info("recorded %.1f s to %s", time.monotonic() - self._started, self._path)
            return self._path

    def status(self) -> RecorderStatus:
        recording = self._blocks is not None
        return RecorderStatus(
            state=RecorderState.RECORDING if recording else RecorderState.IDLE,
            device=self._name,
            level_db=self._level_db,
            seconds=time.monotonic() - self._started if recording else 0.0,
        )

    def close(self) -> None:
        if self._blocks is not None:
            self.stop()
        self._stream.close()
