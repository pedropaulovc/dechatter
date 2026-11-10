"""Setup file (`setup.toml`): the machine, the tool assembly under test, and planner knobs."""

from __future__ import annotations

import dataclasses
import json
import tomllib
import typing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ConfigError(ValueError):
    pass


class Direction(StrEnum):
    CONVENTIONAL = "conventional"
    CLIMB = "climb"


class Coolant(StrEnum):
    NONE = "none"
    FLOOD = "flood"
    MIST = "mist"


class ToolMaterial(StrEnum):
    CARBIDE = "carbide"
    HSS = "hss"


class SpindleControl(StrEnum):
    PROGRAM = "program"  # the G-code starts the spindle and sets its speed
    MANUAL = "manual"  # the operator does, at a program pause before each speed


@dataclass(frozen=True)
class Machine:
    max_rpm: int
    spindle_power_w: float
    weight_kg: float
    min_rpm: int = 500
    name: str = ""
    spindle: SpindleControl = SpindleControl.PROGRAM


@dataclass(frozen=True)
class Tool:
    diameter_mm: float
    flutes: int
    stickout_mm: float
    holder: str = ""
    material: ToolMaterial = ToolMaterial.CARBIDE


@dataclass(frozen=True)
class Cut:
    material: str
    axial_depth_mm: float  # the deepest cut; the session settles speed and width here first
    other_depths_mm: tuple[float, ...] = ()  # shallower depths to compare afterwards
    width_max_mm: float | None = None
    chip_load_mm: float | None = None
    speed_min_rpm: int | None = None
    speed_max_rpm: int | None = None
    direction: Direction = Direction.CONVENTIONAL


@dataclass(frozen=True)
class Stock:
    length_mm: float
    width_mm: float


@dataclass(frozen=True)
class Program:
    dwell_s: float = 4.0
    clearance_mm: float = 3.0
    safe_z_mm: float = 10.0
    plunge_feed_mm_min: float = 150.0
    truing_width_mm: float = 0.2
    coolant: Coolant = Coolant.NONE


@dataclass(frozen=True)
class Planner:
    round1_speeds: int = 5
    round1_width_fractions: tuple[float, ...] = (0.1, 0.2, 0.35)
    batch_size: int = 8
    min_probability: float = 0.8
    probability_floor: float = 0.5
    stop_improvement: float = 0.05
    speed_step_rpm: int = 10
    width_step_mm: float = 0.1
    speed_sigma_fraction: float = 0.015
    misclassification: float = 0.05
    sweet_spot_trust: float = 0.5
    power_fraction: float = 0.6


@dataclass(frozen=True)
class Setup:
    machine: Machine
    tool: Tool
    cut: Cut
    stock: Stock
    program: Program = field(default_factory=Program)
    planner: Planner = field(default_factory=Planner)


def _coerce(hint: object, value: object) -> object:
    for candidate in typing.get_args(hint) or (hint,):
        if isinstance(candidate, type) and issubclass(candidate, StrEnum):
            return candidate(value)
    if typing.get_origin(hint) is tuple and isinstance(value, list):
        return tuple(value)
    return value


def _build[T](cls: type[T], section: str, data: dict) -> T:
    hints = typing.get_type_hints(cls)
    unknown = set(data) - set(hints)
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    try:
        return cls(**{key: _coerce(hints[key], value) for key, value in data.items()})
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{section}] {exc}") from exc


def load_setup(path: Path) -> Setup:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from exc
    sections = {f.name: f for f in dataclasses.fields(Setup)}
    unknown = set(data) - set(sections)
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(sorted(unknown))}")
    built = {}
    for name, f in sections.items():
        if name not in data and f.default_factory is dataclasses.MISSING:
            raise ConfigError(f"missing section [{name}]")
        cls = typing.get_type_hints(Setup)[name]
        built[name] = _build(cls, name, data.get(name, {}))
    setup = Setup(**built)
    _validate(setup)
    return setup


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ConfigError("text contains a broken character; retype it") from exc
        # JSON string escapes are TOML's too, except that TOML also forbids a raw DEL.
        return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list | tuple):
        return f"[{', '.join(_toml_value(item) for item in value)}]"
    if isinstance(value, int | float):
        return repr(value)
    raise ConfigError(f"cannot store a value of type {type(value).__name__}")


# Planner settings that no setup template or form shows.
UNLISTED_PLANNER = (
    "speed_step_rpm",
    "width_step_mm",
    "speed_sigma_fraction",
    "misclassification",
    "sweet_spot_trust",
    "power_fraction",
)


def setup_values(path: Path) -> dict:
    """The setup file as plain sections of values, for a form to edit and hand back.

    Everything a form shows is included, so saving pins what the user saw even where it
    equals today's default: a safe height someone confirmed must not move with a later
    release. The unlisted planner settings are included only where the file sets them, so
    one written by hand survives a save and the rest keep following the defaults.
    """
    values = dataclasses.asdict(load_setup(path))
    written = tomllib.loads(path.read_text(encoding="utf-8")).get("planner", {})
    for key in UNLISTED_PLANNER:
        if key not in written:
            del values["planner"][key]
    return values


def dump_setup(values: dict) -> str:
    """Setup file text for sections of values. Entries left empty (None) take their default."""
    blocks = []
    for section, entries in values.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"[{section}] must be a section of values")
        lines = [f"{key} = {_toml_value(v)}" for key, v in entries.items() if v is not None]
        blocks.append("\n".join([f"[{section}]", *lines]))
    return "\n\n".join(blocks) + "\n"


def _validate(setup: Setup) -> None:
    tool, cut, machine = setup.tool, setup.cut, setup.machine
    if tool.diameter_mm <= 0 or tool.flutes < 1:
        raise ConfigError("[tool] diameter_mm and flutes must be positive")
    if cut.width_max_mm is not None and not 0 < cut.width_max_mm <= tool.diameter_mm:
        raise ConfigError("[cut] width_max_mm must be within (0, tool diameter]")
    if machine.min_rpm >= machine.max_rpm:
        raise ConfigError("[machine] min_rpm must be below max_rpm")
    depths = cut.other_depths_mm
    if not isinstance(depths, tuple) or not all(
        type(d) in (int, float) and 0 < d < cut.axial_depth_mm for d in depths
    ):
        raise ConfigError(
            "[cut] other_depths_mm must list depths above 0 and shallower than axial_depth_mm"
        )
    fractions = setup.planner.round1_width_fractions
    if (
        not isinstance(fractions, tuple)
        or not fractions
        or not all(type(f) in (int, float) and 0 < f <= 1 for f in fractions)
    ):
        raise ConfigError("[planner] round1_width_fractions must list numbers in (0, 1]")
    if not 0 < setup.planner.probability_floor <= setup.planner.min_probability < 1:
        raise ConfigError("[planner] need 0 < probability_floor <= min_probability < 1")


TEMPLATE = """\
# dechatter setup: one file per tool assembly (machine + holder + cutter + stickout).
# Change any of those four and the stability map no longer applies: start a new session.

[machine]
name = "PM-30MV"
max_rpm = 5000
min_rpm = 500
spindle_power_w = 1500   # rated spindle motor power
spindle = "program"      # program: the G-code sets the speed (M3 S) | manual: the program
                         # pauses (M0) before each speed and you set it on the machine
weight_kg = 250          # derates the starting chip load on light machines

[tool]
diameter_mm = 9.525
flutes = 3
stickout_mm = 30.0       # recorded for traceability; shifts chatter frequencies
holder = "ER32"
material = "carbide"     # carbide | hss

[cut]
material = "aluminum-6061"   # see `dechatter materials`
axial_depth_mm = 5.0         # the deepest cut; the test steps speed and radial width at it
# other_depths_mm = [2.5]    # shallower depths to compare once that one is settled
direction = "conventional"   # conventional | climb
# width_max_mm = 4.7         # default: half the tool diameter
# chip_load_mm = 0.05        # default: from the material table, derated by machine weight
# speed_min_rpm = 1250       # default: a quarter of the top test speed
# speed_max_rpm = 5000       # default: min(machine max, material surface-speed limit)

[stock]
length_mm = 60.0   # along X: every pass cuts the full length
width_mm = 30.0    # along Y: material the test may consume from the front face

[program]
dwell_s = 4.0            # spindle-at-speed pause before each pass; also the air-cut reference
clearance_mm = 3.0
safe_z_mm = 10.0
plunge_feed_mm_min = 150.0
truing_width_mm = 0.2
coolant = "none"         # none | flood | mist (coolant noise hurts detection)

[planner]
round1_speeds = 5
round1_width_fractions = [0.1, 0.2, 0.35]   # of width_max
batch_size = 8
min_probability = 0.8      # each cut is this likely stable, if the cut before it passes
probability_floor = 0.5    # ... and this likely stable on measured data alone
stop_improvement = 0.05    # converged when no cut promises this much more removal rate
"""
