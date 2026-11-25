"""A simulated machine: a stability-lobe ground truth and the sound of running a plan on it."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from dechatter.plan import Plan

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
    # With a reference depth, cutting shallower widens the limit: (reference / depth) ** power,
    # which makes a depth limit of it at any one width; and a cut sounds louder the deeper it
    # is, as loud as without one at the reference depth.
    reference_depth_mm: float | None = None
    depth_power: float = 1.5

    def limit_mm(self, rpm: float, flutes: int, depth_mm: float | None = None) -> float:
        """The widest stable cut at a commanded `rpm`: the machine runs it `rpm_error` off,
        and its lobes follow the speed it really runs at."""
        lobes = 60 * self.natural_hz / (flutes * rpm * (1 + self.rpm_error))
        offset = abs(lobes - round(lobes))
        limit = self.floor_mm * (
            1 + self.pocket_gain * np.exp(-0.5 * (offset / self.pocket_width) ** 2)
        )
        if self.reference_depth_mm is None or depth_mm is None:
            return limit
        return limit * (self.reference_depth_mm / depth_mm) ** self.depth_power

    def chatters(
        self, rpm: float, width_mm: float, flutes: int, depth_mm: float | None = None
    ) -> bool:
        return width_mm > self.limit_mm(rpm, flutes, depth_mm)

    def depth_limit_mm(self, rpm: float, flutes: int, width_mm: float) -> float:
        """The deepest stable cut `width_mm` wide; needs a reference depth."""
        widest = self.limit_mm(rpm, flutes)
        return self.reference_depth_mm * (widest / width_mm) ** (1 / self.depth_power)

    def load(self, width_mm: float, depth_mm: float) -> float:
        """How loud the teeth are in a cut, over the spindle in the air."""
        if self.reference_depth_mm is None:
            return 0.03 + 0.03 * width_mm
        return 0.03 + 0.03 * width_mm * depth_mm / self.reference_depth_mm

    def masked(self, rpm: float) -> bool:
        """Whether the chatter tone lands on a spindle harmonic at `rpm`.

        There it is told from forced vibration by nothing in the sound, here or on a machine.
        """
        spindle_hz = rpm * (1 + self.rpm_error) / 60
        orders = self.natural_hz * 1.03 / spindle_hz
        return abs(orders - round(orders)) * spindle_hz < 2.0


DEFAULT_MACHINE = Machine()
# A machine whose limit also falls with depth: on it a cut of any one width has a depth limit,
# 0.25 to 0.5 mm for a 12.7 mm slot and deeper the narrower the cut.
DEPTH_MACHINE = Machine(reference_depth_mm=1.2)


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
    load: float,
    chatter: bool,
    machine: Machine,
    rng: np.random.Generator,
) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    out = _air(seconds, spindle_hz, machine, rng) + 0.02 * rng.standard_normal(len(t))
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


class Facing(StrEnum):
    """Whether a recording holds the program facing the top down, and how."""

    PAUSED = "paused"  # the recorder was paused through it, as the program asks
    HEARD = "heard"  # it ran on through it, as the widget's does: one long loud stretch
    ROWS = "rows"  # it ran on through it, row by row: each row a cut, with air between


def record(
    plan: Plan,
    machine: Machine = DEFAULT_MACHINE,
    seed: int = 0,
    abort_in_pass: int | None = None,
    rapid_s: float = 2.0,
    speed_change_s: float = 0.0,
    air_s: Sequence[float] | None = None,
    cut_s: Sequence[float] | None = None,
    in_air: frozenset[int] = frozenset(),
    facing: Facing = Facing.PAUSED,
    row_s: Sequence[float] = (),
    stop_after_rows: int | None = None,
) -> np.ndarray:
    """The recording of running `plan`: silence, then per pass rapid, dwell, lead-in, cut.

    With `speed_change_s`, the program stops before each new speed (manual spindle): the
    operator knocks the machine reaching for the dial, then the spindle idles at the new speed.
    `air_s` gives the seconds of spindle-only sound before each pass's cut, for a recording
    that has to line up with a real program run; the default guesses 2 s of rapids. With
    `air_s` there is no leading silence: the spindle is heard from the first sample. The
    passes in `in_air` (indices) move as planned but cut nothing: no stock was where they ran.
    With `facing` HEARD, each facing of the top is in the recording, as long as it takes,
    after a rapid to it and right before the first pass after it. With ROWS, it is its rows
    (`row_s`: each row's cut), each after twice the rapid (back to the start, then down); with
    `stop_after_rows`, the run stops once that many rows of the first facing are cut.
    """
    rng = np.random.default_rng(seed)
    parts = [0.0005 * rng.standard_normal(0 if air_s is not None else 3 * RATE)]
    faced = {leg.passes[0].id: leg.face_s for leg in plan.legs if leg.reface_mm > 0}
    # Facing takes a quarter of the cutter: how loud that is.
    heavy = machine.load(plan.diameter_mm / 4, machine.reference_depth_mm or 0.0)
    previous_rpm = None
    for index, p in enumerate(plan.passes):
        spindle_hz = p.rpm * (1 + machine.rpm_error) / 60
        if facing is Facing.ROWS and p.id in faced:
            for row, seconds in enumerate(row_s):
                if row == stop_after_rows:
                    parts.append(0.0005 * rng.standard_normal(2 * RATE))
                    return np.concatenate(parts).astype(np.float32)
                parts.append(_air(2 * rapid_s, spindle_hz, machine, rng))
                parts.append(_cut(seconds, spindle_hz, plan.flutes, heavy, False, machine, rng))
        if facing is Facing.HEARD and p.id in faced:
            parts.append(_air(rapid_s, spindle_hz, machine, rng))
            # The whole top at a quarter of the cutter: louder than any test cut, and chatter.
            parts.append(_cut(faced[p.id], spindle_hz, plan.flutes, heavy, True, machine, rng))
        if speed_change_s and p.rpm != previous_rpm:
            parts.append(0.3 * rng.standard_normal(int(0.3 * RATE)))
            parts.append(_air(speed_change_s, spindle_hz, machine, rng))
        previous_rpm = p.rpm
        air = rapid_s + plan.dwell_s + p.lead_in_s if air_s is None else air_s[index]
        parts.append(_air(air, spindle_hz, machine, rng))
        seconds = (
            p.expected_cut_s * (0.4 if index == abort_in_pass else 1.0)
            if cut_s is None
            else cut_s[index]
        )
        chatter = machine.chatters(p.rpm, p.width_mm, plan.flutes, p.depth_mm)
        load = machine.load(p.width_mm, p.depth_mm)
        if index in in_air:
            parts.append(_air(seconds, spindle_hz, machine, rng))
        else:
            parts.append(_cut(seconds, spindle_hz, plan.flutes, load, chatter, machine, rng))
        if index == abort_in_pass:
            break
    parts.append(0.0005 * rng.standard_normal(2 * RATE))
    return np.concatenate(parts).astype(np.float32)


def timeline(
    plan: Plan,
    *,
    abort_in_pass: int | None = None,
    rapid_s: float = 2.0,
    speed_change_s: float = 0.0,
    facing: Facing = Facing.PAUSED,
    row_s: Sequence[float] = (),
    stop_after_rows: int | None = None,
) -> list[dict]:
    """The widget's run timeline matching `record(plan, ...)`."""
    t0, t = 1000.0, 3.0
    events: list[dict] = [{"t": t0, "event": "record", "round": plan.round, "units": "mm"}]
    faced = {
        leg.passes[0].id: (n, leg.face_s) for n, leg in enumerate(plan.legs) if leg.reface_mm > 0
    }
    previous_rpm = None
    for index, p in enumerate(plan.passes):
        if facing is Facing.ROWS and p.id in faced:
            leg_num, _ = faced[p.id]
            for row, seconds in enumerate(row_s):
                if row == stop_after_rows:
                    events.append(
                        {
                            "t": t0 + t,
                            "event": "run",
                            "outcome": "stopped",
                            "reason": "stopped by the operator",
                        }
                    )
                    return events
                t += 2 * rapid_s
                events.append(
                    {
                        "t": 0,
                        "event": "face",
                        "leg": leg_num,
                        "rows_after": len(row_s) - 1 - row,
                        "start": t0 + t,
                        "end": t0 + t + seconds,
                    }
                )
                t += seconds
        if facing is Facing.HEARD and p.id in faced:
            leg_num, face_s = faced[p.id]
            t += rapid_s
            events.append(
                {
                    "t": 0,
                    "event": "face",
                    "leg": leg_num,
                    "rows_after": 0,
                    "start": t0 + t,
                    "end": t0 + t + face_s,
                }
            )
            t += face_s
        if speed_change_s and p.rpm != previous_rpm:
            t += 0.3 + speed_change_s
        previous_rpm = p.rpm
        dwell_start = t + rapid_s
        dwell_end = dwell_start + plan.dwell_s
        events.append(
            {
                "t": 0,
                "event": "dwell",
                "pass": p.id,
                "start": t0 + dwell_start,
                "end": t0 + dwell_end,
            }
        )
        contact_start = dwell_end + p.lead_in_s
        seconds = p.expected_cut_s * (0.4 if index == abort_in_pass else 1.0)
        if index == abort_in_pass:
            events.append(
                {
                    "t": 0,
                    "event": "cut",
                    "pass": p.id,
                    "start": t0 + dwell_end,
                    "end": t0 + contact_start + seconds,
                    "aborted": True,
                    "holds": [],
                }
            )
            events.append(
                {
                    "t": t0 + contact_start + seconds,
                    "event": "run",
                    "outcome": "stopped",
                    "reason": "stopped by the operator",
                }
            )
            return events
        events.append(
            {
                "t": 0,
                "event": "cut",
                "pass": p.id,
                "start": t0 + dwell_end,
                "end": t0 + contact_start + seconds + p.lead_in_s,
                "holds": [],
            }
        )
        t = contact_start + seconds
    events.append({"t": t0 + t, "event": "run", "outcome": "done", "reason": ""})
    return events
