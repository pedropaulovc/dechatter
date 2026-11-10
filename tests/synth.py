"""A simulated machine: a stability-lobe ground truth and the sound of running a plan on it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile

from dechatter.plan import Plan
from dechatter.recorder import RecorderError, RecorderState, RecorderStatus
from dechatter.session import PLAN_FILE

RATE = 22050


@dataclass(frozen=True)
class Machine:
    """One dominant mode: stable pockets where tooth passing divides the natural frequency."""

    natural_hz: float = 930.0
    floor_mm: float = 1.2  # stable at any speed below this width
    pocket_gain: float = 2.0  # limit at a sweet spot = floor * (1 + gain)
    pocket_width: float = 0.12  # as a fraction of lobe spacing
    rpm_error: float = -0.02  # actual speed vs commanded
    whine_hz: float = 4127.0  # drive whine: a loud tone that is not chatter

    def limit_mm(self, rpm: float, flutes: int) -> float:
        lobes = 60 * self.natural_hz / (flutes * rpm)
        offset = abs(lobes - round(lobes))
        return self.floor_mm * (
            1 + self.pocket_gain * np.exp(-0.5 * (offset / self.pocket_width) ** 2)
        )

    def chatters(self, rpm: float, width_mm: float, flutes: int) -> bool:
        return width_mm > self.limit_mm(rpm, flutes)


DEFAULT_MACHINE = Machine()


def _tone(t: np.ndarray, hz: float, amplitude: float, rng: np.random.Generator) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * hz * t + rng.uniform(0, 2 * np.pi))


def _air(
    seconds: float, spindle_hz: float, machine: Machine, rng: np.random.Generator
) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    out = 0.004 * rng.standard_normal(len(t)) + _tone(t, machine.whine_hz, 0.02, rng)
    for order in range(1, 6):
        out += _tone(t, order * spindle_hz, 0.003 / order, rng)
    return out


def _cut(
    seconds: float,
    spindle_hz: float,
    flutes: int,
    width_mm: float,
    chatter: bool,
    machine: Machine,
    rng: np.random.Generator,
) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    out = _air(seconds, spindle_hz, machine, rng) + 0.02 * rng.standard_normal(len(t))
    load = 0.03 + 0.03 * width_mm
    tooth_hz = spindle_hz * flutes
    for order in range(1, min(int(8000 / tooth_hz), 24) + 1):
        out += _tone(t, order * tooth_hz, load / order, rng)
        out += _tone(t, order * tooth_hz + spindle_hz, 0.2 * load / order, rng)  # runout
    if chatter:
        chatter_hz = machine.natural_hz * 1.03
        out += _tone(t, chatter_hz, 0.6, rng)
        out += _tone(t, chatter_hz + tooth_hz, 0.15, rng)
        out += _tone(t, abs(chatter_hz - tooth_hz), 0.15, rng)
    return out


def record(
    plan: Plan,
    machine: Machine = DEFAULT_MACHINE,
    seed: int = 0,
    abort_in_pass: int | None = None,
    rapid_s: float = 2.0,
) -> np.ndarray:
    """The recording of running `plan`: silence, then per pass rapid, dwell, lead-in, cut."""
    rng = np.random.default_rng(seed)
    parts = [0.0005 * rng.standard_normal(3 * RATE)]
    for index, p in enumerate(plan.passes):
        spindle_hz = p.rpm * (1 + machine.rpm_error) / 60
        parts.append(_air(rapid_s + plan.dwell_s + p.lead_in_s, spindle_hz, machine, rng))
        seconds = p.expected_cut_s * (0.4 if index == abort_in_pass else 1.0)
        chatter = machine.chatters(p.rpm, p.width_mm, plan.flutes)
        parts.append(_cut(seconds, spindle_hz, plan.flutes, p.width_mm, chatter, machine, rng))
        if index == abort_in_pass:
            break
    parts.append(0.0005 * rng.standard_normal(2 * RATE))
    return np.concatenate(parts).astype(np.float32)


class SimulatedMic:
    """Stands in for the microphone: 'hears' the simulated machine run the round's plan."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.abort_in_pass: int | None = None
        self.silent = False

    def start(self, path: Path) -> None:
        if self.path is not None:
            raise RecorderError("already recording")
        self.path = path

    def stop(self) -> Path:
        path, self.path = self.path, None
        plan = Plan.load(path.with_name(PLAN_FILE))
        heard = record(plan, seed=plan.round, abort_in_pass=self.abort_in_pass)
        soundfile.write(path, heard * (0.0 if self.silent else 1.0), RATE)
        return path

    def status(self) -> RecorderStatus:
        state = RecorderState.IDLE if self.path is None else RecorderState.RECORDING
        return RecorderStatus(state, "simulated", -30.0, 0.0)

    def close(self) -> None:
        pass
