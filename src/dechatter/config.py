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


class Vary(StrEnum):
    """What a session steps besides the spindle speed; the other dimension it holds."""

    WIDTH = "width"  # the radial width, at a fixed axial depth
    DEPTH = "depth"  # the axial depth, at a fixed radial width


class SetupSource(StrEnum):
    TEMPLATE = "template"  # the example `init` and the widget write, as written: not this machine
    FILLED_IN = "filled-in"


class SpindleControl(StrEnum):
    PROGRAM = "program"  # the G-code starts the spindle and sets its speed
    MANUAL = "manual"  # the operator does, at a program pause before each speed


WATTS_PER_HP = 745.7


@dataclass(frozen=True)
class Machine:
    max_rpm: int
    spindle_power_hp: float  # rated motor power, as mill spindles are usually rated
    weight_kg: float
    min_rpm: int = 500
    name: str = ""
    spindle: SpindleControl = SpindleControl.PROGRAM
    # A manual spindle whose speed control only comes to rest on certain speeds: the rpm of
    # each, slowest first. Empty: any speed can be set.
    speed_stops_rpm: tuple[int, ...] = ()

    @property
    def spindle_power_w(self) -> float:
        return self.spindle_power_hp * WATTS_PER_HP


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
    # vary = "width": the deepest cut; the session settles speed and width here first
    axial_depth_mm: float | None = None
    other_depths_mm: tuple[float, ...] = ()  # shallower depths to compare afterwards
    width_max_mm: float | None = None
    vary: Vary = Vary.WIDTH
    # vary = "depth": the widest cut, where the session settles speed and depth first. The
    # cutter diameter cuts full-width slots into the top; a narrower width mills the front face.
    radial_width_mm: float | None = None
    other_widths_mm: tuple[float, ...] = ()  # narrower widths to compare afterwards
    depth_max_mm: float | None = None  # the deepest cut tried
    chip_load_mm: float | None = None
    speed_min_rpm: int | None = None
    speed_max_rpm: int | None = None
    direction: Direction = Direction.CONVENTIONAL


@dataclass(frozen=True)
class Stock:
    length_mm: float
    width_mm: float
    # Equal lengths along X the test uses one after another, from the end the cutter meets
    # first: each a fresh face at Y0, cut at the same height above the vise jaws. None: as
    # many as leave every cut at least planner.min_cut_s long (see plan.auto_lanes).
    lanes: int | None = None
    # How far the top, as first mounted (Z0), stands above anything the cutter must not
    # reach: vise jaws, parallels, clamps. Given, dechatter faces the top down itself once
    # every lane is used, and goes on cutting below it. None: never faced.
    height_mm: float | None = None
    # The stock's full size along Y, front face to back, which facing covers. None: only the
    # width the test may use, leaving the rest standing behind it.
    thickness_mm: float | None = None


@dataclass(frozen=True)
class Program:
    dwell_s: float = 4.0
    clearance_mm: float = 3.0
    safe_z_mm: float = 10.0
    plunge_feed_mm_min: float = 150.0
    truing_width_mm: float = 0.2
    coolant: Coolant = Coolant.NONE
    # Facing the top down (see stock.height_mm): the deepest level each pass takes, and how
    # far each pass steps along Y (None: a quarter of the cutter diameter).
    face_depth_mm: float = 1.0
    face_width_mm: float | None = None
    # Full-width slots (vary = "depth" at the cutter diameter): the stock left standing on
    # both sides of every slot (None: a quarter of the cutter diameter).
    slot_wall_mm: float | None = None


@dataclass(frozen=True)
class Planner:
    round1_speeds: int = 5
    round1_width_fractions: tuple[float, ...] = (0.1, 0.2, 0.35)
    round1_depth_fractions: tuple[float, ...] = (0.2, 0.4, 0.6)  # vary = "depth": of depth_max
    batch_size: int = 8
    min_probability: float = 0.8
    probability_floor: float = 0.5
    stop_improvement: float = 0.05
    speed_step_rpm: int = 10
    width_step_mm: float = 0.1
    # None: about a fiftieth of depth_max_mm, see plan.engagement_step_mm.
    depth_step_mm: float | None = None
    speed_sigma_fraction: float = 0.015
    misclassification: float = 0.05
    sweet_spot_trust: float = 0.5
    power_fraction: float = 0.6
    # The chip-load sweep after convergence, as multiples of the session's chip load.
    chip_load_sweep_low: float = 0.6
    chip_load_sweep_high: float = 1.7
    # The check of the other direction offered once a level has a best cut: that cut at the
    # session's chip load, at these fractions of its width (or depth) and itself, lightest
    # first, then this many steps of the width (or depth) beyond it; 0: none beyond.
    check_fractions: tuple[float, ...] = (0.5, 0.75, 1.0)
    check_steps_beyond: int = 1
    # The shortest cut worth listening to, at the fastest test speed: bounds the automatic
    # number of lanes.
    min_cut_s: float = 6.0


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
    return parse_setup(path.read_text(encoding="utf-8"), path.name)


# Field path ("stock.height_mm") and note, for each value written ahead of time that is
# still to be confirmed on the machine.
type Pending = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class SetupFile:
    """A setup and the file text it was parsed from, which a planned round keeps."""

    text: str
    setup: Setup
    # Planning waits until these are confirmed; saving the wizard confirms them.
    pending: Pending = ()


def read_setup(text: str, origin: str) -> SetupFile:
    setup, pending = _parse(text, origin)
    return SetupFile(text, setup, pending)


def parse_setup(text: str, origin: str) -> Setup:
    """A setup from setup file text; `origin` names it in errors."""
    return _parse(text, origin)[0]


PENDING = "pending"

# The fields that go with one choice of another field, as `section.field` and its value: the
# wizard shows them only then (the `when` of its fields, in wizard.js).
ONLY_WITH = {
    "cut.axial_depth_mm": ("cut.vary", Vary.WIDTH),
    "cut.other_depths_mm": ("cut.vary", Vary.WIDTH),
    "cut.width_max_mm": ("cut.vary", Vary.WIDTH),
    "planner.round1_width_fractions": ("cut.vary", Vary.WIDTH),
    "cut.radial_width_mm": ("cut.vary", Vary.DEPTH),
    "cut.depth_max_mm": ("cut.vary", Vary.DEPTH),
    "cut.other_widths_mm": ("cut.vary", Vary.DEPTH),
    "program.slot_wall_mm": ("cut.vary", Vary.DEPTH),
    "planner.round1_depth_fractions": ("cut.vary", Vary.DEPTH),
    "machine.speed_stops_rpm": ("machine.spindle", SpindleControl.MANUAL),
}


def _value(setup: Setup, at: str) -> object:
    section, key = at.split(".")
    return getattr(getattr(setup, section), key)


def _shown_fields() -> set[str]:
    """Every field the wizard can show, as `section.field`."""
    hints = typing.get_type_hints(Setup)
    shown = {
        f"{section.name}.{entry.name}"
        for section in dataclasses.fields(Setup)
        for entry in dataclasses.fields(hints[section.name])
    }
    return shown - {f"planner.{key}" for key in UNLISTED_PLANNER}


def _pending(entries: object, setup: Setup) -> Pending:
    """The `[pending]` table: each key a field the wizard shows this setup, each value a note
    saying how to check it."""
    if not isinstance(entries, dict):
        raise ConfigError(f'[{PENDING}] must be a table of "section.field" = "note"')
    if not entries:
        return ()
    flat = []
    for at, note in entries.items():
        # Unquoted, stock.height_mm is a TOML dotted key: a table of the section's fields. An
        # empty table stays as it is, to be refused below.
        if isinstance(note, dict) and note:
            flat.extend((f"{at}.{key}", value) for key, value in note.items())
        else:
            flat.append((at, note))
    shown, seen = _shown_fields(), set()
    for at, note in flat:
        if at in seen:
            raise ConfigError(f'[{PENDING}] "{at}" is listed twice')
        seen.add(at)
        if at not in shown:
            raise ConfigError(f'[{PENDING}] "{at}" is not a setup field the wizard shows')
        choice, wanted = ONLY_WITH.get(at, (None, None))
        if choice is not None and _value(setup, choice) is not wanted:
            raise ConfigError(
                f'[{PENDING}] "{at}" does not apply with {choice.split(".")[1]} = '
                f'"{_value(setup, choice)}", so the wizard never shows it'
            )
        if not isinstance(note, str) or not note.strip():
            raise ConfigError(f'[{PENDING}] "{at}" needs a note in quotes: how to check it')
    return tuple(flat)


def _parse(text: str, origin: str) -> tuple[Setup, Pending]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{origin} is not valid TOML: {exc}") from exc
    if "spindle_power_w" in data.get("machine", {}):
        raise ConfigError(
            "this setup is from dechatter 0.1.x, which this version refuses: start a new "
            "session. (In a setup written by hand, [machine] spindle_power_w is now "
            "spindle_power_hp: the spindle motor's rated power in horsepower, "
            f"1 hp = {WATTS_PER_HP} W.)"
        )
    entries = data.pop(PENDING, {})
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
    return setup, _pending(entries, setup)


def setup_source(text: str) -> SetupSource:
    """Whether setup file text is still the template. Whitespace alone does not fill it in:
    an editor that only re-wrapped lines or line endings left the example values in place.
    """
    if text.split() == TEMPLATE.split():
        return SetupSource.TEMPLATE
    return SetupSource.FILLED_IN


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
    "depth_step_mm",
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
    one written by hand survives a save and the rest keep following the defaults. The
    `[pending]` table is left out: saving what the form showed confirms those fields.
    """
    values = dataclasses.asdict(load_setup(path))
    written = tomllib.loads(path.read_text(encoding="utf-8")).get("planner", {})
    for key in UNLISTED_PLANNER:
        if key not in written:
            del values["planner"][key]
    return values


def setup_defaults() -> dict:
    """Each section's default values, for the entries that have one."""
    hints = typing.get_type_hints(Setup)
    return {
        section.name: {
            entry.name: entry.default
            for entry in dataclasses.fields(hints[section.name])
            if entry.default is not dataclasses.MISSING
        }
        for section in dataclasses.fields(Setup)
    }


def dump_setup(values: dict) -> str:
    """Setup file text for sections of values. Entries left empty (None) take their default."""
    blocks = []
    for section, entries in values.items():
        if not isinstance(entries, dict):
            raise ConfigError(f"[{section}] must be a section of values")
        lines = [f"{key} = {_toml_value(v)}" for key, v in entries.items() if v is not None]
        blocks.append("\n".join([f"[{section}]", *lines]))
    return "\n\n".join(blocks) + "\n"


FULL_WIDTH_TOLERANCE_MM = 1e-6


def full_width(setup: Setup) -> bool:
    """Whether every test cut takes the whole cutter diameter: a slot in the top."""
    cut = setup.cut
    if cut.vary is not Vary.DEPTH:
        return False
    return cut.radial_width_mm >= setup.tool.diameter_mm - FULL_WIDTH_TOLERANCE_MM


def slot_wall_mm(setup: Setup) -> float:
    """The stock left standing on both sides of a full-width slot."""
    if setup.program.slot_wall_mm is None:
        return setup.tool.diameter_mm / 4
    return setup.program.slot_wall_mm


def face_step_mm(setup: Setup) -> float:
    """How far apart the rows of facing, and of a relief between lanes, are:
    program.face_width_mm, else a quarter of the cutter."""
    return setup.program.face_width_mm or setup.tool.diameter_mm / 4


def relief_wall_mm(setup: Setup) -> float:
    """How far a relief's walls stand along the feed from the lane boundary it is milled at
    (see plan.Relief): a cutter radius and a row step, so the pocket is a cutter diameter and
    two steps long."""
    return setup.tool.diameter_mm / 2 + face_step_mm(setup)


def relief_room_mm(setup: Setup, lanes: int) -> float:
    """The stock the most hemmed-in of `lanes` side-milling lanes keeps between the reliefs
    at its inner ends (one for two lanes, two for more)."""
    inner = 1 if lanes == 2 else 2
    return setup.stock.length_mm / lanes - inner * relief_wall_mm(setup)


def _number(value: object) -> bool:
    return type(value) in (int, float)


def _lower(values: object, top: float) -> bool:
    return isinstance(values, tuple) and all(_number(v) and 0 < v < top for v in values)


def _validate_cut(setup: Setup) -> str:
    """Check the dimension the session holds and the one it steps; answer the name of the
    [cut] entry that gives the deepest cut."""
    cut, diameter = setup.cut, setup.tool.diameter_mm
    if cut.vary is Vary.DEPTH:
        unused = [
            name
            for name in ("axial_depth_mm", "other_depths_mm", "width_max_mm")
            if getattr(cut, name) not in (None, ())
        ]
        if unused:
            raise ConfigError(
                f'[cut] {", ".join(unused)} do not apply with vary = "depth": the width is '
                "radial_width_mm, and depth_max_mm bounds the depth"
            )
        if not _number(cut.radial_width_mm) or not 0 < cut.radial_width_mm <= diameter:
            raise ConfigError(
                '[cut] vary = "depth" needs radial_width_mm within (0, tool diameter]: the '
                "width every cut takes (the tool diameter cuts full-width slots)"
            )
        if not _number(cut.depth_max_mm) or cut.depth_max_mm <= 0:
            raise ConfigError(
                '[cut] vary = "depth" needs depth_max_mm above 0: the deepest cut to try'
            )
        if not _lower(cut.other_widths_mm, cut.radial_width_mm):
            raise ConfigError(
                "[cut] other_widths_mm must list widths above 0 and narrower than radial_width_mm"
            )
        return "depth_max_mm"
    unused = [
        name
        for name in ("radial_width_mm", "other_widths_mm", "depth_max_mm")
        if getattr(cut, name) not in (None, ())
    ]
    if unused:
        raise ConfigError(
            f'[cut] {", ".join(unused)} apply only with vary = "depth"; with vary = "width" '
            "the depth is axial_depth_mm, and width_max_mm bounds the width"
        )
    if not _number(cut.axial_depth_mm) or cut.axial_depth_mm <= 0:
        raise ConfigError("[cut] axial_depth_mm must be given, above 0: the depth of every cut")
    if cut.width_max_mm is not None and not 0 < cut.width_max_mm <= diameter:
        raise ConfigError("[cut] width_max_mm must be within (0, tool diameter]")
    if not _lower(cut.other_depths_mm, cut.axial_depth_mm):
        raise ConfigError(
            "[cut] other_depths_mm must list depths above 0 and shallower than axial_depth_mm"
        )
    return "axial_depth_mm"


def _validate(setup: Setup) -> None:
    tool, cut, machine = setup.tool, setup.cut, setup.machine
    if tool.diameter_mm <= 0 or tool.flutes < 1:
        raise ConfigError("[tool] diameter_mm and flutes must be positive")
    deepest = _validate_cut(setup)
    stock = setup.stock
    if stock.height_mm is not None and stock.height_mm <= getattr(cut, deepest):
        raise ConfigError(f"[stock] height_mm must stand above the deepest cut, {deepest}")
    # A session that steps the depth cuts as deep as depth_max_mm wherever the search leads:
    # the deepest cut must clear what the stock stands on, as facing the top down has to.
    reach = None if cut.vary is Vary.WIDTH else cut.depth_max_mm + setup.program.clearance_mm
    if stock.height_mm is not None and reach is not None and stock.height_mm < reach:
        raise ConfigError(
            "[stock] height_mm must leave [program] clearance_mm "
            f"({setup.program.clearance_mm:g} mm) below the deepest cut, depth_max_mm: at "
            f"least {reach:g} mm"
        )
    if stock.thickness_mm is not None and stock.thickness_mm < stock.width_mm:
        raise ConfigError("[stock] thickness_mm cannot be less than width_mm, the width used")
    wall = slot_wall_mm(setup)
    if wall <= 0:
        raise ConfigError("[program] slot_wall_mm must be positive")
    across = stock.thickness_mm or stock.width_mm
    if full_width(setup) and across + 1e-9 < tool.diameter_mm + 2 * wall:
        raise ConfigError(
            f"[stock] {across:g} mm along Y (thickness_mm, or width_mm) holds no "
            f"{tool.diameter_mm:g} mm slot with {wall:g} mm of stock on both sides "
            "([program] slot_wall_mm)"
        )
    face_width = setup.program.face_width_mm
    if setup.program.face_depth_mm <= 0 or (
        face_width is not None and not 0 < face_width <= tool.diameter_mm / 2
    ):
        raise ConfigError(
            "[program] face_depth_mm must be positive and face_width_mm within "
            "(0, half the tool diameter]"
        )
    if stock.lanes is not None and (type(stock.lanes) is not int or stock.lanes < 1):
        raise ConfigError("[stock] lanes must be a whole number, at least 1")
    if (
        stock.lanes is not None
        and stock.lanes > 1
        and stock.length_mm / stock.lanes < 2 * tool.diameter_mm
    ):
        raise ConfigError(
            "[stock] each lane must be at least twice the tool diameter long: "
            f"{stock.length_mm:g} mm in {stock.lanes} lanes is "
            f"{stock.length_mm / stock.lanes:.1f} mm each"
        )
    narrower = any(
        w < tool.diameter_mm - FULL_WIDTH_TOLERANCE_MM for w in setup.cut.other_widths_mm
    )
    if stock.lanes is not None and stock.lanes > 1 and (not full_width(setup) or narrower):
        # Side milling, at the setup's width or another: the reliefs where lanes meet take
        # relief_wall_mm of each lane at each inner end, and the most hemmed-in lane must
        # keep a cutter radius of stock between them. Planning checks lanes it settles.
        between = relief_room_mm(setup, stock.lanes)
        if between < tool.diameter_mm / 2 - 1e-9:
            raise ConfigError(
                "[stock] each lane must keep at least a cutter radius of stock between the "
                "reliefs milled where it meets the lanes beside it (each reaching a cutter "
                "radius and [program] face_width_mm, or a quarter of the cutter, into it): "
                f"{stock.length_mm:g} mm in {stock.lanes} lanes leaves {between:.1f} mm"
            )
    if machine.min_rpm >= machine.max_rpm:
        raise ConfigError("[machine] min_rpm must be below max_rpm")
    _validate_speed_stops(machine)
    for name in ("round1_width_fractions", "round1_depth_fractions", "check_fractions"):
        fractions = getattr(setup.planner, name)
        if (
            not isinstance(fractions, tuple)
            or not fractions
            or not all(type(f) in (int, float) and 0 < f <= 1 for f in fractions)
        ):
            raise ConfigError(f"[planner] {name} must list numbers in (0, 1]")
    steps_beyond = setup.planner.check_steps_beyond
    if type(steps_beyond) is not int or steps_beyond < 0:
        raise ConfigError("[planner] check_steps_beyond must be a whole number, 0 or more")
    depth_step = setup.planner.depth_step_mm
    if setup.planner.width_step_mm <= 0 or (depth_step is not None and depth_step <= 0):
        raise ConfigError("[planner] width_step_mm and depth_step_mm must be positive")
    if cut.vary is Vary.DEPTH and depth_step is not None and depth_step > cut.depth_max_mm:
        raise ConfigError("[planner] depth_step_mm cannot be more than [cut] depth_max_mm")
    if not 0 < setup.planner.probability_floor <= setup.planner.min_probability < 1:
        raise ConfigError("[planner] need 0 < probability_floor <= min_probability < 1")
    if not 0 < setup.planner.min_cut_s:
        raise ConfigError("[planner] min_cut_s must be positive")
    if not 0 < setup.planner.chip_load_sweep_low < setup.planner.chip_load_sweep_high:
        raise ConfigError("[planner] need 0 < chip_load_sweep_low < chip_load_sweep_high")


def _validate_speed_stops(machine: Machine) -> None:
    stops = machine.speed_stops_rpm
    if stops == ():
        return
    if machine.spindle is not SpindleControl.MANUAL:
        raise ConfigError(
            '[machine] speed_stops_rpm applies only with spindle = "manual": a program sets '
            "any speed it asks for"
        )
    whole = isinstance(stops, tuple) and all(type(s) is int for s in stops)
    if not whole or list(stops) != sorted(set(stops)):
        raise ConfigError("[machine] speed_stops_rpm must list whole rpm, slowest first, each once")
    if stops[0] < machine.min_rpm or stops[-1] > machine.max_rpm:
        raise ConfigError(
            f"[machine] speed_stops_rpm must lie within min_rpm and max_rpm "
            f"({machine.min_rpm}..{machine.max_rpm} rpm)"
        )


TEMPLATE = """\
# dechatter setup: one file per tool assembly (machine + holder + cutter + stickout).
# Change any of those four and the stability map no longer applies: start a new session.

[machine]
name = "PM-30MV"
max_rpm = 5000
min_rpm = 500
spindle_power_hp = 2.0   # rated spindle motor power, in hp
spindle = "program"      # program: the G-code sets the speed (M3 S) | manual: the round
                         # pauses before each speed and you set it on the machine
weight_kg = 250          # derates the starting chip load on light machines

[tool]
diameter_mm = 9.525
flutes = 3
stickout_mm = 30.0       # recorded for traceability; shifts chatter frequencies
holder = "ER32"
material = "carbide"     # carbide | hss

[cut]
material = "aluminum-6061"
# vary = "width"             # width: hold the depth, step the radial width | depth: see below
axial_depth_mm = 5.0         # the deepest cut; the test steps speed and radial width at it
# other_depths_mm = [2.5]    # shallower depths to compare once that one is settled
direction = "conventional"   # conventional | climb
# width_max_mm = 4.7         # default: half the tool diameter
# With vary = "depth" the test holds a radial width and steps speed and axial depth. In
# place of axial_depth_mm, other_depths_mm and width_max_mm it takes:
# radial_width_mm = 9.525    # the cutter diameter: full-width slots in the top; narrower:
#                            # side milling on the front face, deepest cut first
# other_widths_mm = [4.0]    # narrower widths to compare once that one is settled
# depth_max_mm = 3.0         # the deepest cut to try
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
# slot_wall_mm = 2.4       # full-width slots: stock left on both sides of each slot; default:
                         # a quarter of the tool diameter
coolant = "none"         # none | flood | mist (coolant noise hurts detection)

[planner]
round1_speeds = 5
round1_width_fractions = [0.1, 0.2, 0.35]   # of width_max
# round1_depth_fractions = [0.2, 0.4, 0.6]  # vary = "depth": of depth_max
batch_size = 8
min_probability = 0.8      # each cut is this likely stable, if the cut before it passes
probability_floor = 0.5    # ... and this likely stable on measured data alone
stop_improvement = 0.05    # converged when no cut promises this much more removal rate
chip_load_sweep_low = 0.6  # after convergence the best cut can be tried at chip loads
chip_load_sweep_high = 1.7 # ... from low to high times the session's chip load
"""
