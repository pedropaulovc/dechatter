"""A session as one printable HTML page: what to cut, the map behind it, and how it was found.

Self-contained (no scripts, fonts or images from elsewhere), so it opens offline and prints
on US Letter or A4 in black and white. Lengths follow the machine's units, with mm beside.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from . import __version__, bayes, materials, seeding, session
from .config import WATTS_PER_HP, Direction, SpindleControl, Vary, read_setup
from .detect import Verdict
from .gcode import MM_PER_INCH, Units
from .plan import (
    SPINDLE_EFFICIENCY,
    Milling,
    Order,
    PassKind,
    Plan,
    PlannerKind,
    envelope,
    feed_mm_min,
)
from .session import PassStatus, Round, SpeedSource, SpindleFit, VerdictSource

REPORT_FILE = session.REPORT_FILE
CUTS_PER_DEPTH = 3
MM3_PER_IN3 = MM_PER_INCH**3

# Map plot geometry, in SVG units.
PLOT_W, PLOT_H = 600, 220
LEFT, BOTTOM, TOP, RIGHT = 56, 34, 8, 8

PLANNER_NAMES = {
    PlannerKind.GRID: "grid of round 1",
    PlannerKind.BAYES: "chosen from the map",
    PlannerKind.SWEEP: "chip-load sweep",
    PlannerKind.CONFIRM: "confirmation of the session it was started from",
}

SEED_STATUS = {
    seeding.SeedStatus.UNCONFIRMED: "its confirmation round is still to be cut",
    seeding.SeedStatus.CONFIRMED: "confirmed by this session's first cuts, and part of the map",
    seeding.SeedStatus.REJECTED: "rejected by this session's confirmation cuts: half or more "
    "of those it predicted came out the other way, so it is left out of the map and the "
    "round-1 grid was planned after all",
    seeding.SeedStatus.UNTESTED: "on the map untested: this session's first cuts checked "
    "nothing it predicted (it holds no stable cut inside this session's search to confirm, "
    "or no confirmation cut it predicted got a verdict)",
    seeding.SeedStatus.UNUSED: "not used: none of it lies inside this session's search",
}
CARRY = {
    seeding.Carry.SAME: "same cut",
    seeding.Carry.CONVERTED: "converted by equal load",
    seeding.Carry.SPEEDS: "speed only",
}


def _planner_name(plan: Plan) -> str:
    if plan.planner is PlannerKind.CHECK:
        return f"{plan.direction} check"
    return PLANNER_NAMES[plan.planner]


def _heard(cut: session.CheckCut) -> str:
    """What a check's cut did, as its table says."""
    if cut.status in session.UNHEARD and cut.verdict is None:
        return "not run"
    if cut.verdict is None:
        return "not heard"
    return "held" if cut.verdict is Verdict.STABLE else "chatter"


STABLE_FILL, UNSURE_FILL, CHATTER_FILL = "#ffffff", "#c8c8c8", "#555555"


def _e(value: object) -> str:
    return html.escape(str(value))


def _n(value: float, places: int) -> str:
    return f"{value:.{places}f}"


def _length(mm: float, units: Units, places_mm: int = 2) -> str:
    if units is Units.MM:
        return f"{_n(mm, places_mm)} mm"
    return f"{_n(mm / MM_PER_INCH, places_mm + 2)} in ({_n(mm, places_mm)} mm)"


def _feed(mm_min: float, units: Units) -> str:
    if units is Units.MM:
        return f"{mm_min:.0f} mm/min"
    return f"{mm_min / MM_PER_INCH:.1f} in/min ({mm_min:.0f} mm/min)"


def _rate(mm3_min: float, units: Units) -> str:
    if units is Units.MM:
        return f"{mm3_min / 1000:.2f} cm³/min"
    return f"{mm3_min / MM3_PER_IN3:.3f} in³/min ({mm3_min / 1000:.2f} cm³/min)"


def _verdict(verdict: Verdict | None) -> str:
    if verdict is None:
        return "not heard"
    return "chatter" if verdict is Verdict.UNSTABLE else "stable"


def _table(head: Iterable[str], rows: Iterable[Iterable[str]], cls: str = "") -> str:
    header = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f'<table class="{cls}"><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>'


# The dimension a session steps, as a column heads it, and how a level of the other reads.
STEPPED = {Vary.WIDTH: "Width", Vary.DEPTH: "Depth"}
HELD = {Vary.WIDTH: "deep", Vary.DEPTH: "wide"}


def _where(plan: Plan, units: Units) -> str:
    """Where a round cuts, as it goes: its lanes, and where it faces the top down."""
    parts = []
    for number, leg in enumerate(plan.legs):
        # Lanes count from the end the cutter meets first: the right one when it climbs.
        end = " from the right" if plan.direction is Direction.CLIMB else ""
        lane = f"lane {leg.lane} of {plan.lanes}{end}" if plan.lanes > 1 else ""
        faced = f"top faced down {_length(leg.reface_mm, units)}" if leg.reface_mm > 0 else ""
        start = leg.passes[0].id
        if number == 0:
            parts += [p for p in (lane, faced and f"{faced} first") if p]
            continue
        if faced:
            again = "the top" if plan.milling is Milling.SLOT else "the front face"
            parts.append(f"{faced} before pass {start}, then {lane or again} again")
            continue
        parts.append(f"{lane} from pass {start}")
    return "".join(f", {p}" for p in parts)


def _dates(rounds: list[Round]) -> str:
    days = sorted(r.plan.planned_on for r in rounds)
    if not days:
        return "none yet"
    return days[0] if days[0] == days[-1] else f"{days[0]} to {days[-1]}"


def _map_svg(
    stability: bayes.StabilityMap,
    cuts: list[bayes.Observation],
    units,
    cfg,
    vary: Vary,
    seeded: list[bayes.Observation] = (),
) -> str:
    shown = bayes.coarse(stability)
    p, rpm, mm = shown.p, shown.speeds, shown.engagements
    width_px, height_px = PLOT_W - LEFT - RIGHT, PLOT_H - TOP - BOTTOM
    cell_w, cell_h = width_px / len(rpm), height_px / len(mm)

    def x(value: float) -> float:
        # A spindle that ran off the speed asked can be heard just beyond the speeds searched:
        # its cut is drawn at the edge.
        span = rpm[-1] - rpm[0] or 1.0
        inside = min(max(value, rpm[0]), rpm[-1])
        return LEFT + (inside - rpm[0]) / span * (width_px - cell_w) + cell_w / 2

    def y(value: float) -> float:
        span = mm[-1] - mm[0] or 1.0
        return TOP + height_px - cell_h / 2 - (value - mm[0]) / span * (height_px - cell_h)

    parts = []
    for i in range(len(rpm)):
        for j in range(len(mm)):
            fill = UNSURE_FILL
            if p[i, j] >= cfg.min_probability:
                fill = STABLE_FILL
            if p[i, j] <= 1 - cfg.min_probability:
                fill = CHATTER_FILL
            parts.append(
                f'<rect x="{LEFT + i * cell_w:.1f}" y="{TOP + height_px - (j + 1) * cell_h:.1f}" '
                f'width="{cell_w + 0.3:.1f}" height="{cell_h + 0.3:.1f}" fill="{fill}"/>'
            )
    parts.append(
        f'<rect x="{LEFT}" y="{TOP}" width="{width_px}" height="{height_px}" '
        'fill="none" stroke="#000" stroke-width="0.8"/>'
    )
    # Carried over from the session this one was started from: not cut here, so drawn apart.
    for cut in seeded:
        cx, cy = x(cut.rpm), y(cut.engagement_mm)
        if cut.verdict is Verdict.STABLE:
            parts.append(
                f'<rect x="{cx - 2.4:.1f}" y="{cy - 2.4:.1f}" width="4.8" height="4.8" '
                'fill="none" stroke="#000" stroke-width="0.7"/>'
            )
            continue
        parts.append(
            f'<path d="M{cx - 3:.1f},{cy:.1f}L{cx + 3:.1f},{cy:.1f}'
            f'M{cx:.1f},{cy - 3:.1f}L{cx:.1f},{cy + 3:.1f}" stroke="#000" stroke-width="0.7"/>'
        )
    for cut in cuts:
        cx, cy = x(cut.rpm), y(cut.engagement_mm)
        if cut.verdict is Verdict.STABLE:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" fill="#fff" stroke="#000" '
                'stroke-width="1.2"/>'
            )
            continue
        parts.append(
            f'<path d="M{cx - 3.2:.1f},{cy - 3.2:.1f}L{cx + 3.2:.1f},{cy + 3.2:.1f}'
            f'M{cx - 3.2:.1f},{cy + 3.2:.1f}L{cx + 3.2:.1f},{cy - 3.2:.1f}" '
            'stroke="#fff" stroke-width="3.4"/>'
            f'<path d="M{cx - 3.2:.1f},{cy - 3.2:.1f}L{cx + 3.2:.1f},{cy + 3.2:.1f}'
            f'M{cx - 3.2:.1f},{cy + 3.2:.1f}L{cx + 3.2:.1f},{cy - 3.2:.1f}" '
            'stroke="#000" stroke-width="1.4"/>'
        )
    unit = "mm" if units is Units.MM else "in"
    scale = 1.0 if units is Units.MM else 1 / MM_PER_INCH
    for tick in np.linspace(rpm[0], rpm[-1], 6):
        parts.append(
            f'<text x="{x(tick):.1f}" y="{PLOT_H - BOTTOM + 13}" text-anchor="middle">'
            f"{tick:.0f}</text>"
        )
    for tick in np.linspace(mm[0], mm[-1], 5):
        label = _n(tick * scale, 2 if units is Units.MM else 3)
        parts.append(f'<text x="{LEFT - 4}" y="{y(tick) + 3:.1f}" text-anchor="end">{label}</text>')
    parts.append(
        f'<text x="{LEFT + width_px / 2}" y="{PLOT_H - 4}" text-anchor="middle">'
        "spindle speed (rpm)</text>"
        f'<text x="12" y="{TOP + height_px / 2}" text-anchor="middle" '
        f'transform="rotate(-90 12 {TOP + height_px / 2})">{STEPPED[vary].lower()} ({unit})</text>'
    )
    return (
        f'<svg viewBox="0 0 {PLOT_W} {PLOT_H}" role="img" '
        f'aria-label="stability map">{"".join(parts)}</svg>'
    )


def _seed_section(
    seed: seeding.Seed, status, setup, env, units: Units, stickout_pending: bool
) -> str:
    """The session this one was started from: what it measured, and how each of its passes
    carries over to this session's own depth (or width). `stickout_pending`: this session's
    stickout is a placeholder still to confirm on the machine, not yet compared with it."""
    old = seed.setup
    assembly = (
        "machine, holder and cutter (this session's stickout is still to confirm on the "
        f"machine, and must then be within {_length(seeding.STICKOUT_TOLERANCE_MM, units, 1)} "
        f"of its {_length(old.tool.stickout_mm, units, 1)})"
        if stickout_pending
        else "machine, holder, cutter and stickout"
    )
    vary = setup.cut.vary
    rows = []
    for c in seeding.carried(seed, setup, env):
        here = "outside the search" if c.observation is None else _verdict(c.observation.verdict)
        size = "" if c.observation is None else _length(c.observation.engagement_mm, units)
        rows.append(
            [
                f"{c.source.round}.{c.source.id}",
                (
                    f"{c.source.rpm} as asked: not heard"
                    if c.source.heard_rpm is None
                    else f"{c.source.heard_rpm:.0f}"
                ),
                _length(c.source.width_mm, units),
                _length(c.source.depth_mm, units),
                _verdict(c.source.verdict),
                CARRY[c.carry],
                size,
                here,
                f"{setup.planner.misclassification ** seeding.WEIGHT[c.carry]:.0%}",
            ]
        )
    relies = ""
    if status in (seeding.SeedStatus.UNCONFIRMED, seeding.SeedStatus.CONFIRMED):
        relied = seeding.transfer(seed, setup, env)
        how = (
            "its limits, converted: round 1 confirms the predicted best cut"
            if relied is seeding.Transfer.LIMITS
            else "its speeds only: round 1 cuts the grid's sizes at its best speeds"
        )
        relies = f" This session relies on {how}."
    return f"""<section>
<h2>Seeded from {_e(seed.source)}</h2>
<p class="note">This session was started from session {_e(seed.source)}, measured on the same
{assembly} in {_e(old.cut.material)}, {_e(old.cut.direction.value)}
milling. Its {len(seed.passes)} analysed passes are kept with this session and are not cuts
of it. Each becomes the {STEPPED[vary].lower()} that loads the tool as much here (specific
energy × depth × arc of engagement), and counts as a cut misheard as often as its last
column says, against {setup.planner.misclassification:.0%} for a cut made here. A pass in the
other direction, or a slot against a side cut, carries over as a speed in the main.{relies}
The seed is {_e(SEED_STATUS[status])}.</p>
{
        _table(
            [
                "Round.pass there",
                "Heard there (rpm)",
                "Width there",
                "Depth there",
                "Verdict there",
                "Carried over as",
                f"{STEPPED[vary]} here",
                "Says here",
                "Taken as misheard",
            ],
            rows,
            "log",
        )
    }
</section>"""


def _check_section(setup, env, check: session.Check, found, units: Units, power) -> str:
    """A check of the other direction: which of its cuts held, the session's best marked."""
    deep = check.vary
    best = next(
        (c for c in found if abs(session.level_of(setup, c) - check.level_mm) <= 1e-6), None
    )
    rows = []
    for cut in check.cuts:
        size = _length(check.engagement_mm(cut), units)
        same = (
            best is not None
            and cut.rpm in (round(best.asked_rpm), round(session.ask_rpm(env, best)))
            and abs(check.engagement_mm(cut) - session.engagement_of(setup, best)) <= 1e-6
        )
        mrr = cut.width_mm * cut.depth_mm * cut.feed_mm_min
        rows.append(
            [
                str(cut.rpm),
                f"{size} (the best {setup.cut.direction} cut)" if same else size,
                _feed(cut.feed_mm_min, units),
                _rate(mrr, units),
                power(mrr),
                _heard(cut),
            ]
        )
    held = check.held
    if held is None and any(cut.verdict is Verdict.STABLE for cut in check.cuts):
        summary = (
            f"In {check.direction} milling the lightest cut run did not hold or was not heard, "
            "so none counts as held from there up; each cut is below."
        )
    elif held is None:
        summary = f"In {check.direction} milling no cut was heard to hold."
    else:
        reach = _length(check.engagement_mm(held), units)
        size = "deep" if deep is Vary.DEPTH else "wide"
        place = check.cuts.index(held)
        rest = (
            "every cut tried"
            if held is check.cuts[-1]
            else f"the next heavier one: {_heard(check.cuts[place + 1])}"
        )
        summary = f"In {check.direction} milling the cuts held up to {reach} {size}; {rest}."
    title = (
        f"{check.direction.capitalize()} check, round {check.round}: "
        f"{_length(check.level_mm, units)} {HELD[deep]}"
    )
    return (
        f"<h3>{_e(title)}</h3>"
        f"<p>{_e(summary)} The map and the cuts above were measured {_e(setup.cut.direction)}; "
        "these cuts do not change them.</p>"
        + _table(
            [
                "Speed (rpm)",
                STEPPED[deep],
                "Feed",
                "Removal rate",
                "Spindle power",
                check.direction.capitalize(),
            ],
            rows,
        )
    )


def render(session_dir: Path, units: Units) -> str:
    """The session's report as one HTML document; lengths in `units`, with mm beside."""
    path = session_dir / session.SETUP_FILE
    source = read_setup(path.read_text(encoding="utf-8"), path.name)
    setup = source.setup
    env = envelope(setup)
    rounds = session.load_rounds(session_dir)
    machine, tool, cut, cfg = setup.machine, setup.tool, setup.cut, setup.planner
    manual = machine.spindle is SpindleControl.MANUAL
    material = materials.lookup(cut.material)
    name = session_dir.resolve().name
    deep = cut.vary
    seed = seeding.load(session_dir)
    status = None if seed is None else session.seed_status(seed, setup, env, rounds)

    header = _table(
        ["", ""],
        [
            ["Rounds", f"{len(rounds)}, planned {_dates(rounds)}"],
            [
                "Machine",
                f"{machine.name or 'unnamed'}: {machine.min_rpm}–{machine.max_rpm} rpm, "
                f"{machine.spindle_power_hp:g} hp, spindle set "
                f"{'by hand' if manual else 'by the program'}",
            ],
            [
                "Tool",
                f"Ø {_length(tool.diameter_mm, units)}, {tool.flutes} flutes, {tool.material}, "
                f"stickout {_length(tool.stickout_mm, units, 1)}, "
                f"holder {tool.holder or 'not given'}",
            ],
            ["Work material", cut.material],
            ["Direction", f"{cut.direction}: the map and the recommended cuts"],
            [
                "Varied",
                "radial width, at each depth below"
                if deep is Vary.WIDTH
                else "axial depth, at each width below (the cutter diameter: full-width slots "
                "in the top; narrower: side milling, deepest cut first)",
            ],
            [
                "Depths" if deep is Vary.WIDTH else "Widths",
                ", ".join(_length(level, units) for level in session.levels(setup)),
            ],
            ["Chip load", f"{_length(env.chip_load_mm, units, 3)} per tooth"],
            *([] if seed is None else [["Started from", f"{seed.source}: {SEED_STATUS[status]}"]]),
        ],
        "facts",
    )

    def power(mrr_mm3_min: float) -> str:
        watts = mrr_mm3_min / 60 * material.specific_energy_j_mm3 / SPINDLE_EFFICIENCY
        return f"{watts / WATTS_PER_HP:.2f} hp ({watts / machine.spindle_power_w:.0%})"

    found = session.best_cuts(setup, env, rounds)
    recommended = []
    for level in sorted({session.level_of(setup, c) for c in found}, reverse=True):
        rows = []
        for best in [c for c in found if session.level_of(setup, c) == level][:CUTS_PER_DEPTH]:
            heard = "not heard" if best.speed is SpeedSource.ASKED else f"{best.rpm:.0f}"
            ask = session.ask_rpm(env, best)
            # As made, unless the speed stops changed since: then at the stop to set now.
            rate = best.mrr_mm3_min * ask / best.asked_rpm
            rows.append(
                [
                    heard,
                    f"{ask:.0f}",
                    _length(session.engagement_of(setup, best), units),
                    _feed(feed_mm_min(setup, env, ask), units),
                    _rate(rate, units),
                    power(rate),
                ]
            )
        speed = "Set the spindle to (rpm)" if manual else "Asked (rpm)"
        recommended.append(
            f"<h3>{_e(_length(level, units))} {HELD[deep]}</h3>"
            + _table(
                ["Heard (rpm)", speed, STEPPED[deep], "Feed", "Removal rate", "Spindle power"],
                rows,
            )
        )
    if not recommended:
        recommended.append("<p>No stable cut has been measured yet.</p>")
    for sweep in session.sweeps(rounds, env):
        heard = "" if sweep.heard_rpm is None else f" (heard {sweep.heard_rpm:.0f})"
        where = (
            f"Chip-load sweep, round {sweep.round}: {sweep.rpm:.0f} rpm{heard}, "
            f"{_length(sweep.width_mm, units)} wide, {_length(sweep.depth_mm, units)} deep"
        )
        found_load = sweep.stable_chip_load_mm
        if found_load is None:
            recommended.append(
                f"<h3>{_e(where)}</h3><p>Even the lightest pass chattered: keep the session's "
                f"{_e(_length(sweep.session_chip_load_mm, units, 3))} per tooth.</p>"
            )
            continue
        untopped = (
            "<p>Every chip load tried stayed stable: the cut's limit lies above the heaviest "
            "one, which is no limit itself.</p>"
            if sweep.untopped
            else ""
        )
        recommended.append(
            f"<h3>{_e(where)}</h3>"
            + untopped
            + _table(
                [
                    "Stable at least up to (per tooth)"
                    if sweep.untopped
                    else "Stable up to (per tooth)",
                    "Of the session's",
                    "Feed",
                    "Removal rate",
                    "Spindle power",
                ],
                [
                    [
                        _length(found_load, units, 3),
                        f"{found_load / sweep.session_chip_load_mm:.2f}×",
                        _feed(sweep.feed_mm_min, units),
                        _rate(sweep.mrr_mm3_min, units),
                        power(sweep.mrr_mm3_min),
                    ]
                ],
            )
        )
    for check in session.checks(rounds):
        recommended.append(_check_section(setup, env, check, found, units, power))

    maps = []
    shown_levels = {r.plan.fixed_mm for r in rounds}
    if session.carried(seed, status, setup, env, session.levels(setup)[0]):
        shown_levels.add(session.levels(setup)[0])
    carried_over = False
    for level in sorted(shown_levels, reverse=True):
        seeded = session.carried(seed, status, setup, env, level)
        carried_over = carried_over or bool(seeded)
        stability = bayes.build_map(
            session.at_level(setup, level),
            env,
            [*session.evidence(rounds, level, env), *seeded],
        )
        cuts = session.observations(rounds, level, env)
        maps.append(
            f"<figure><figcaption>{_e(_length(level, units))} {HELD[deep]}</figcaption>"
            f"{_map_svg(stability, cuts, units, cfg, deep, seeded)}</figure>"
        )
    seeded_note = ""
    if carried_over:
        seeded_note = (
            f" Small squares (stable) and plus signs (chatter) are seeded from {_e(seed.source)}:"
            " cuts of that session as they carry over, not cut in this one, and counted for"
            " less."
        )
    stickout_pending = seeding.STICKOUT in dict(source.pending)
    seed_section = (
        "" if seed is None else _seed_section(seed, status, setup, env, units, stickout_pending)
    )
    if not maps:
        maps.append("<p>No round has been cut yet.</p>")

    log = []
    for r in rounds:
        results = {p.id: p for p in r.results.passes} if r.results else {}
        rows = []
        for p in r.plan.passes:
            result = results.get(p.id)
            verdict = (
                "" if result is None or p.kind is not PassKind.TEST else _verdict(result.verdict)
            )
            # A pass not run, or by its sound likely not run, shows no verdict, not "not heard".
            not_run = (PassStatus.NOT_RUN, PassStatus.LIKELY_NOT_RUN)
            if result is not None and result.status in not_run and not result.verdict:
                verdict = ""
            if result is not None and session.spindle_fit(result) is SpindleFit.EDGE:
                verdict += " (spindle speed at the edge: check)"
            by_hand = result is not None and session.verdict_source(result) is (
                VerdictSource.OPERATOR
            )
            heard, cut_load = "", ""
            if result is not None and session.heard_rpm(result) is not None:
                heard = f"{session.heard_rpm(result):.0f}"
                cut_load = _length(
                    session.chip_load_cut_mm(result, p.feed_mm_min, r.plan.flutes), units, 3
                )
            ran = p.kind is PassKind.TEST and result is not None
            if ran and not heard and result.status not in session.UNHEARD:
                mapped = result.verdict is not None and r.plan.planner is not PlannerKind.SWEEP
                heard = "not heard: mapped as asked" if mapped else "not heard"
            rows.append(
                [
                    str(p.id),
                    f"{p.kind.value}, control" if p.expected is Verdict.UNSTABLE else p.kind.value,
                    str(p.rpm),
                    heard,
                    _length(r.plan.engagement_mm(p), units),
                    _length(p.chip_load_mm or r.plan.chip_load_mm, units, 3),
                    cut_load,
                    _feed(p.feed_mm_min, units),
                    "" if result is None else result.status.value,
                    verdict,
                    "by hand" if by_hand and p.kind is PassKind.TEST else "",
                ]
            )
        state = "analysed" if r.results else "not analysed"
        slots = ", full-width slots" if r.plan.milling is Milling.SLOT else ""
        first = ", deepest cut first" if r.plan.order is Order.DEEPEST_FIRST else ""
        log.append(
            f"<h3>Round {r.plan.round}: {_e(_length(r.plan.fixed_mm, units))} {HELD[deep]}"
            f"{slots}{first}, "
            f"{_e(_planner_name(r.plan))}, planned {_e(r.plan.planned_on)}"
            + _e(_where(r.plan, units))
            + f", {state}</h3>"
            + _table(
                [
                    "Pass",
                    "Kind",
                    "Asked (rpm)",
                    "Heard (rpm)",
                    STEPPED[deep],
                    "Chip load",
                    "Chip load cut",
                    "Feed",
                    "Status",
                    "Verdict",
                    "Verdict set",
                ],
                rows,
                "log",
            )
        )
    if not log:
        log.append("<p>No round has been planned yet.</p>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dechatter report: {_e(name)}</title>
<style>{STYLE}</style>
</head>
<body>
<header>
<h1>dechatter report: {_e(name)}</h1>
{header}
</header>
<section>
<h2>Recommended cuts</h2>
<p class="note">The session stepped the {STEPPED[deep].lower()} of cut. The best stable cuts
measured at each {"width" if deep is Vary.DEPTH else "depth"}, most material removed first,
each at the spindle speed heard in it ("not heard": none was found, and the speed asked for
stands in). {"The spindle is set by hand: set the speed shown. " if manual else ""}Feed,
removal rate and power follow the speed asked for, as the program feeds it. Power is
the spindle's estimated draw, and its share of the rated {machine.spindle_power_hp:g} hp.</p>
{"".join(recommended)}
</section>
<section>
<h2>Stability map</h2>
<p class="note">White: likely stable (at least {cfg.min_probability:.0%}). Dark: likely chatter.
Grey: not settled by the cuts made. Circles are stable cuts, crosses chatter, each at the
spindle speed heard in it, or where none was heard, the speed asked for.{seeded_note}</p>
{"".join(maps)}
</section>
{seed_section}
<section class="log">
<h2>Round log</h2>
{"".join(log)}
</section>
<section>
<h2>Setup</h2>
<p class="note">The session's setup.toml, which this report was computed from. Each round
keeps a copy of the setup it was planned from; those may differ from this one in what
may change after a cut, such as the safe height or the speed limits.</p>
<pre>{_e(source.text)}</pre>
</section>
<footer>dechatter {_e(__version__)}</footer>
</body>
</html>
"""


STYLE = """
@page { margin: 14mm; }
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 16px; max-width: 190mm; color: #000; background: #fff;
  font: 10pt/1.35 "Helvetica Neue", Helvetica, Arial, sans-serif; }
h1 { font-size: 16pt; margin: 0 0 8px; }
h2 { font-size: 12.5pt; margin: 18px 0 6px; border-bottom: 1.5px solid #000;
  break-after: avoid; }
h3 { font-size: 10.5pt; margin: 12px 0 4px; break-after: avoid; }
.note { margin: 0 0 6px; color: #333; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 2px 6px; text-align: left; border-bottom: 0.5px solid #999;
  vertical-align: top; }
th { font-weight: bold; border-bottom: 1px solid #000; }
tr { break-inside: avoid; }
table.facts thead { display: none; }
table.facts td:first-child { width: 28mm; font-weight: bold; }
table.log { font-size: 8.5pt; }
figure { margin: 6px 0 12px; break-inside: avoid; }
figcaption { font-weight: bold; margin-bottom: 2px; }
svg { width: 100%; height: auto; font: 9px Helvetica, Arial, sans-serif; }
pre { font-size: 8pt; white-space: pre-wrap; border: 0.5px solid #999; padding: 6px; }
footer { margin-top: 14px; font-size: 8pt; color: #333; }
@media print { body { padding: 0; } section.log { break-before: page; } }
"""
