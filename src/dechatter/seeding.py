"""A session started from an earlier one: what of that session's cuts carries over.

A stability map belongs to one tool assembly: machine, holder, cutter and stickout. A new
session on the same assembly (another material, depth, width or direction) may start from
an earlier one's analysed passes, kept in `seed.json` with the setup they were measured
with, so the session stays whole when exported.

What carries over, after the usual stability-lobe picture:

- The speeds. Chatter bands and stable pockets come from the tool tip's dynamics and the
  tooth count, which the new session shares.
- The limits, converted. The limiting depth goes with 1 / (Ks x teeth in the cut), so a cut
  is taken to chatter as much as any other with the same Ks x depth x arc of engagement
  (Ks: the material's specific energy; the arc: acos(1 - 2 width / diameter), the angle a
  tooth stays in the cut). An earlier pass becomes the engagement of equal load here.
- How far to trust that. Carried-over passes are not cuts made here: each counts as a cut
  misheard far more often than a new one (see WEIGHT). A pass that was this session's own
  cut in another material is trusted most; one converted across depths or widths less; one
  made in the other direction, or a slot against a side cut or the reverse, hardly at all:
  between those the load compares poorly, and only the speeds are relied on.

Round 1 of such a session confirms instead of cutting the grid (see `confirmation`), and
the carried-over passes are dropped when the confirmation cuts disagree (see `judged`).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path

from . import bayes, materials
from .bayes import Observation
from .config import Setup, Vary, parse_setup
from .detect import Verdict
from .plan import (
    ENGAGEMENT_TOLERANCE_MM,
    Envelope,
    Milling,
    engagement_step_mm,
    fixed_mm,
    grid_engagements,
    milling,
    mrr_mm3_min,
)

SCHEMA = 1
SEED_FILE = "seed.json"
# The assembly is the same while the stickout is within this (a cutter put back in its
# holder against a stop or a scale) and the diameter within that (a regrind is another tool).
STICKOUT_TOLERANCE_MM = 0.6
DIAMETER_TOLERANCE_MM = 0.05
# A pass is at this session's own depth (or width) when it is this close to it.
SAME_LEVEL_MM = 0.01
STEP_DOWN = 2 / 3  # the second confirmation cut, as a share of the predicted best one
POCKETS = 2  # speeds confirmed when only the speeds carry over
# Two speeds are in different pockets, or one clear of a chatter band, when this share of
# the speed apart: over three of the map's standard deviations in speed.
POCKET_APART = 0.05


class SeedError(RuntimeError):
    pass


class Carry(StrEnum):
    """How one earlier pass carries over to a session's depth (or width)."""

    SAME = "same"  # the same cut, at most in another material: scaled by the Ks ratio
    CONVERTED = "converted"  # another depth or width, side milling both: by equal load
    SPEEDS = "speeds"  # another direction, or slot against side cut: its speed, in the main


# What a carried-over pass weighs against a cut made here: it counts as misheard
# misclassification ** weight of the time (22%, 41% and 64% at the default 5%).
WEIGHT = {Carry.SAME: 0.5, Carry.CONVERTED: 0.3, Carry.SPEEDS: 0.15}


class Transfer(StrEnum):
    """What a session relies on of the one it was started from."""

    LIMITS = "limits"  # its limits, converted: round 1 confirms the predicted best cut
    SPEEDS = "speeds"  # its speeds only: round 1 cuts the grid's sizes at its best speeds


class SeedStatus(StrEnum):
    UNCONFIRMED = "unconfirmed"  # the confirmation round is still to be cut
    CONFIRMED = "confirmed"
    REJECTED = "rejected"  # the confirmation cuts disagreed: dropped, the grid follows
    # No stable pass of it inside the search: nothing to confirm. It stays on the map, and
    # round 1 is the grid.
    UNTESTED = "untested"
    UNUSED = "unused"  # nothing of it lies inside this session's search


@dataclass(frozen=True)
class SeedPass:
    """An analysed test cut of the earlier session, as it was made there."""

    round: int
    id: int
    rpm: int
    width_mm: float
    depth_mm: float
    verdict: Verdict
    chatter_hz: float | None = None


@dataclass(frozen=True)
class Seed:
    source: str  # the earlier session's name
    setup_text: str  # its setup.toml, as it stood
    passes: tuple[SeedPass, ...]
    schema: int = SCHEMA

    @cached_property
    def setup(self) -> Setup:
        return parse_setup(self.setup_text, f"the setup in {SEED_FILE}")

    def save(self, path: Path) -> None:
        data = {
            "schema": self.schema,
            "source": self.source,
            "setup": self.setup_text,
            "passes": [asdict(p) for p in self.passes],
        }
        path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def load(session_dir: Path) -> Seed | None:
    """The seed a session was started from; None for a session started on its own."""
    path = session_dir / SEED_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema"] != SCHEMA:
            raise ValueError(f"schema {data['schema']}")
        passes = tuple(SeedPass(**{**p, "verdict": Verdict(p["verdict"])}) for p in data["passes"])
        seed = Seed(str(data["source"]), str(data["setup"]), passes)
        seed.setup  # noqa: B018  the setup must load
    except (KeyError, TypeError, ValueError) as exc:
        problem = f"missing {exc}" if isinstance(exc, KeyError) else str(exc)
        raise SeedError(
            f"{SEED_FILE} cannot be read ({problem}). Fix it by hand, or remove it to go on "
            "without the earlier session's cuts"
        ) from exc
    return seed


def _label(text: str) -> str:
    return " ".join(text.casefold().split())


def mismatches(source: Setup, new: Setup) -> list[str]:
    """Why `new` is not the tool assembly `source` measured; empty when it is.

    A machine name or holder left empty on either side says nothing, so it passes. What the
    cut may differ in (material, depth, width, what is stepped, direction, chip load) is
    not looked at here: `carry` deals with those.
    """
    found = []
    old_tool, tool = source.tool, new.tool
    named = source.machine.name and new.machine.name
    if named and _label(source.machine.name) != _label(new.machine.name):
        found.append(f"the machine was {source.machine.name!r}, not {new.machine.name!r}")
    if source.machine.weight_kg != new.machine.weight_kg:
        found.append(
            f"the machine weighed {source.machine.weight_kg:g} kg, not {new.machine.weight_kg:g} kg"
        )
    if old_tool.holder and tool.holder and _label(old_tool.holder) != _label(tool.holder):
        found.append(f"the holder was {old_tool.holder!r}, not {tool.holder!r}")
    if abs(old_tool.diameter_mm - tool.diameter_mm) > DIAMETER_TOLERANCE_MM:
        found.append(
            f"the cutter diameter was {old_tool.diameter_mm:g} mm, not {tool.diameter_mm:g} mm"
        )
    if old_tool.flutes != tool.flutes:
        found.append(f"the cutter had {old_tool.flutes} flutes, not {tool.flutes}")
    if old_tool.material is not tool.material:
        found.append(f"the cutter was {old_tool.material.value}, not {tool.material.value}")
    if abs(old_tool.stickout_mm - tool.stickout_mm) > STICKOUT_TOLERANCE_MM:
        found.append(
            f"the stickout was {old_tool.stickout_mm:g} mm, not {tool.stickout_mm:g} mm "
            f"(more than {STICKOUT_TOLERANCE_MM:g} mm apart)"
        )
    return found


def check(seed: Seed, setup: Setup) -> None:
    """Refuse a setup that is another tool assembly than the seed's."""
    found = mismatches(seed.setup, setup)
    if not found:
        return
    raise SeedError(
        f"this session starts from {seed.source}, which measured another tool assembly: "
        f"{'; '.join(found)}. A stability map belongs to one machine, holder, cutter and "
        "stickout: put them back, or create the session without starting from another"
    )


def _arc(width_mm: float, diameter_mm: float) -> float:
    """The angle a tooth stays in a cut `width_mm` wide: pi in a full-width slot."""
    return math.acos(1 - 2 * min(width_mm, diameter_mm) / diameter_mm)


def carry(seed: Seed, p: SeedPass, setup: Setup) -> Carry:
    """How pass `p` carries over to `setup`'s depth (or where it steps the depth, width)."""
    diameter = setup.tool.diameter_mm
    slot = p.width_mm >= diameter - ENGAGEMENT_TOLERANCE_MM
    if slot is not (milling(setup) is Milling.SLOT):
        return Carry.SPEEDS
    if seed.setup.cut.direction is not setup.cut.direction:
        return Carry.SPEEDS
    held = p.width_mm if setup.cut.vary is Vary.DEPTH else p.depth_mm
    if abs(held - fixed_mm(setup)) <= SAME_LEVEL_MM:
        return Carry.SAME
    return Carry.CONVERTED


def equivalent_mm(seed: Seed, p: SeedPass, setup: Setup) -> float:
    """The engagement in `setup` that loads the tool as pass `p` did: the same specific
    energy x depth x arc of engagement. A width beyond a full slot is the cutter diameter."""
    diameter = setup.tool.diameter_mm
    old = materials.lookup(seed.setup.cut.material).specific_energy_j_mm3
    new = materials.lookup(setup.cut.material).specific_energy_j_mm3
    load = old * p.depth_mm * _arc(p.width_mm, diameter)
    if setup.cut.vary is Vary.DEPTH:
        return load / (new * _arc(setup.cut.radial_width_mm, diameter))
    arc = load / (new * setup.cut.axial_depth_mm)
    if arc >= math.pi:
        return diameter
    return diameter / 2 * (1 - math.cos(arc))


@dataclass(frozen=True)
class Carried:
    """An earlier pass as a session sees it at one depth (or width)."""

    source: SeedPass
    carry: Carry
    engagement_mm: float  # of equal load here, before it is fitted to the map
    # What it tells the map searched now; None when it tells nothing: its speed is outside
    # the search, a stable cut is under the smallest step, or chatter is over the largest.
    observation: Observation | None


def _observe(setup: Setup, env: Envelope, p: SeedPass, size_mm: float, weight: float):
    """A stable pass says the map is stable up to the step under its size, chatter that it
    chatters from the step over it: neither claims more than was measured."""
    if not env.holds_speed(p.rpm):
        return None
    step = engagement_step_mm(setup)
    if p.verdict is Verdict.STABLE:
        steps = min(math.floor(size_mm / step + 1e-6), round(env.engagement_top_mm / step))
        if steps < 1:
            return None
        return Observation(p.rpm, steps * step, p.verdict, p.chatter_hz, weight)
    steps = max(1, math.ceil(size_mm / step - 1e-6))
    if steps * step > env.engagement_top_mm + ENGAGEMENT_TOLERANCE_MM:
        return None
    return Observation(p.rpm, steps * step, p.verdict, p.chatter_hz, weight)


def carried(seed: Seed, setup: Setup, env: Envelope) -> list[Carried]:
    """Every pass of the seed, as it carries over to `setup` (at one depth or width)."""
    found = []
    for p in seed.passes:
        how = carry(seed, p, setup)
        size = equivalent_mm(seed, p, setup)
        found.append(Carried(p, how, size, _observe(setup, env, p, size, WEIGHT[how])))
    return found


def prior(seed: Seed, setup: Setup, env: Envelope) -> list[Observation]:
    """What the seed tells the map searched now, each pass at its weight."""
    return [c.observation for c in carried(seed, setup, env) if c.observation is not None]


def _relied(seed: Seed, setup: Setup, env: Envelope) -> list[Observation]:
    """The passes whose limits carry over: all but those that carry over as speeds only."""
    found = carried(seed, setup, env)
    return [c.observation for c in found if c.observation and c.carry is not Carry.SPEEDS]


def transfer(seed: Seed, setup: Setup, env: Envelope) -> Transfer:
    """What round 1 confirms: limits, when the seed predicts a best cut here; else speeds."""
    predicted = _limit_tests(setup, env, _relied(seed, setup, env))
    return Transfer.LIMITS if predicted else Transfer.SPEEDS


def _sigma(setup: Setup, rpm: float) -> float:
    return setup.planner.speed_sigma_fraction * rpm


def _apart(rpm: float, others: list[float]) -> bool:
    """Whether a speed is in another pocket than each of `others`."""
    return all(abs(rpm - o) > POCKET_APART * o for o in others)


def _within_power(setup: Setup, env: Envelope, rpm: float, size_mm: float) -> float | None:
    """`size_mm`, or the largest step under it the power cap allows at `rpm`; None if none."""
    step = engagement_step_mm(setup)
    per_mm = float(mrr_mm3_min(setup, env, rpm, 1.0))
    steps = min(round(size_mm / step), math.floor(env.mrr_limit_mm3_min / per_mm / step + 1e-9))
    return steps * step if steps >= 1 else None


def _band(setup: Setup, chatter: list[tuple[float, float]], taken: list[float]):
    """(rpm, size) of the control: in the chatter band heard most often, clear of the speeds
    in `taken`, its lightest cut that chattered."""
    free = [(rpm, size) for rpm, size in chatter if _apart(rpm, taken)]
    if not free:
        return None

    def heard(rpm: float) -> int:
        return sum(abs(other - rpm) <= _sigma(setup, rpm) for other, _ in free)

    return min(free, key=lambda cut: (-heard(cut[0]), cut[1], cut[0]))


def _limit_tests(setup: Setup, env: Envelope, known: list[Observation]) -> list[tuple]:
    """The predicted best cut, the same speed a third lighter, the best cut of another
    pocket, and the control; each with the seeded map's P(stable) and the verdict expected."""
    stability = bayes.build_map(setup, env, known)
    p = stability.p_stable()
    chatter = [o for o in known if o.verdict is Verdict.UNSTABLE]
    step = engagement_step_mm(setup)

    def test(rpm: float, size_mm: float, expected: Verdict) -> tuple:
        s, e = stability.speed_index(rpm), stability.engagement_index(size_mm)
        speed, size = float(stability.speeds[s]), float(stability.engagements[e])
        return (speed, size, float(p[s, e]), None, expected)

    stable = []
    for o in known:
        if o.verdict is not Verdict.STABLE:
            continue
        size = _within_power(setup, env, o.rpm, o.engagement_mm)
        # Chatter carried over at this speed, at no more than this size, contradicts it.
        denied = any(
            abs(c.rpm - o.rpm) <= _sigma(setup, o.rpm)
            and c.engagement_mm <= (size or 0.0) + ENGAGEMENT_TOLERANCE_MM
            for c in chatter
        )
        if size is not None and not denied:
            stable.append((float(mrr_mm3_min(setup, env, o.rpm, size)), o.rpm, size))
    if not stable:
        return []
    stable.sort(key=lambda cut: (-cut[0], cut[1]))
    _, best_rpm, best_size = stable[0]
    tests = [test(best_rpm, best_size, Verdict.STABLE)]
    lighter = math.floor(STEP_DOWN * best_size / step + 1e-6) * step
    if lighter >= step - ENGAGEMENT_TOLERANCE_MM:
        tests.append(test(best_rpm, lighter, Verdict.STABLE))
    taken = [best_rpm]
    for _, rpm, size in stable:
        if _apart(rpm, taken):
            tests.append(test(rpm, size, Verdict.STABLE))
            taken.append(rpm)
            break
    loud = [
        (c.rpm, c.engagement_mm)
        for c in chatter
        if mrr_mm3_min(setup, env, c.rpm, c.engagement_mm) <= env.mrr_limit_mm3_min
        # A stable pass carried over at this speed and size or above contradicts it.
        and not any(
            abs(o.rpm - c.rpm) <= _sigma(setup, c.rpm) and o.engagement_mm >= c.engagement_mm
            for o in known
            if o.verdict is Verdict.STABLE
        )
    ]
    control = _band(setup, loud, taken)
    if control is not None:
        tests.append(test(*control, Verdict.UNSTABLE))
    unique = {(rpm, size): (rpm, size, *rest) for rpm, size, *rest in tests}
    return list(unique.values())


def _snap(setup: Setup, env: Envelope, rpm: float) -> float:
    step = setup.planner.speed_step_rpm
    return float(min(max(round(rpm / step) * step, env.speed_min_rpm), env.speed_max_rpm))


def _speed_tests(seed: Seed, setup: Setup, env: Envelope) -> list[tuple]:
    """Round 1's sizes at the speeds of the earlier session's best cuts, one per pocket, and
    as a control its heaviest size within the power cap in the chatter band heard most often,
    expected to chatter. The sizes are the grid's: no limit is predicted for them."""
    inside = [p for p in seed.passes if env.holds_speed(p.rpm)]
    stable = [p for p in inside if p.verdict is Verdict.STABLE]
    stable.sort(key=lambda p: (-p.width_mm * p.depth_mm * p.rpm, p.rpm))
    pockets: list[float] = []
    for p in stable:
        rpm = _snap(setup, env, p.rpm)
        if len(pockets) < POCKETS and _apart(rpm, pockets):
            pockets.append(rpm)
    sizes = grid_engagements(setup, env)

    def allowed(rpm: float) -> list[float]:
        return [e for e in sizes if mrr_mm3_min(setup, env, rpm, e) <= env.mrr_limit_mm3_min]

    tests = [(rpm, size, None, None, None) for rpm in pockets for size in allowed(rpm)]
    if not tests:
        return []
    chatter = [(_snap(setup, env, p.rpm), 0.0) for p in inside if p.verdict is Verdict.UNSTABLE]
    control = _band(setup, chatter, pockets)
    if control is not None and allowed(control[0]):
        tests.append((control[0], allowed(control[0])[-1], None, None, Verdict.UNSTABLE))
    return tests


def confirmation(seed: Seed, setup: Setup, env: Envelope) -> list[tuple]:
    """Round 1 of a seeded session, instead of the grid: (rpm, engagement, P(stable), None,
    the verdict expected) per cut; empty when nothing of the seed lies inside the search.

    Where limits carry over (see Transfer): the predicted best cut, that is the earlier
    session's stable pass of the highest removal rate here, within the power cap; the same
    speed at two thirds of it; the best such pass of another speed pocket; and a control,
    the lightest cut that chattered in the chatter band heard most often. The first three
    are expected stable, the control to chatter.

    Where only the speeds do: the grid's own sizes at the speeds of the earlier session's two
    best pockets, with nothing expected of them, and the control at the heaviest of those
    sizes in its chatter band, expected to chatter.
    """
    tests = _limit_tests(setup, env, _relied(seed, setup, env))
    return tests or _speed_tests(seed, setup, env)


def judged(cuts: list[tuple[Verdict | None, Verdict]]) -> SeedStatus | None:
    """What the confirmation cuts made of the seed, from (expected, verdict) of each one
    that got a verdict; None while none has.

    Rejected when half or more of the cuts the seed predicted came out the other way, the
    control among them: one surprise in four leaves it standing (the cut itself corrects
    the map there), two do not. Where only the speeds carried over the control is the one
    cut predicted, so the seed is rejected when its chatter band did not chatter.
    """
    if not cuts:
        return None
    predicted = [(expected, verdict) for expected, verdict in cuts if expected is not None]
    wrong = sum(expected is not verdict for expected, verdict in predicted)
    if predicted and 2 * wrong >= len(predicted):
        return SeedStatus.REJECTED
    return SeedStatus.CONFIRMED
