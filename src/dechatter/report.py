"""A session as one printable HTML page: what to cut, the map behind it, and how it was found.

Self-contained (no scripts, fonts or images from elsewhere), so it opens offline and prints
on US Letter or A4 in black and white. Lengths follow the machine's units, with mm beside.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from . import __version__, bayes, materials, session
from .config import WATTS_PER_HP, SpindleControl, load_setup
from .detect import Verdict
from .gcode import MM_PER_INCH, Units
from .plan import SPINDLE_EFFICIENCY, PassKind, envelope, feed_mm_min
from .session import Round, VerdictSource

REPORT_FILE = "report.html"
CUTS_PER_DEPTH = 3
MM3_PER_IN3 = MM_PER_INCH**3

# Map plot geometry, in SVG units.
PLOT_W, PLOT_H = 600, 220
LEFT, BOTTOM, TOP, RIGHT = 56, 34, 8, 8

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


def _dates(rounds: list[Round]) -> str:
    days = sorted(r.plan.planned_on for r in rounds)
    if not days:
        return "none yet"
    return days[0] if days[0] == days[-1] else f"{days[0]} to {days[-1]}"


def _map_svg(stability: bayes.StabilityMap, cuts: list[bayes.Observation], units, cfg) -> str:
    shown = bayes.coarse(stability)
    p, rpm, mm = shown.p, shown.speeds, shown.widths
    width_px, height_px = PLOT_W - LEFT - RIGHT, PLOT_H - TOP - BOTTOM
    cell_w, cell_h = width_px / len(rpm), height_px / len(mm)

    def x(value: float) -> float:
        span = rpm[-1] - rpm[0] or 1.0
        return LEFT + (value - rpm[0]) / span * (width_px - cell_w) + cell_w / 2

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
    for cut in cuts:
        cx, cy = x(cut.rpm), y(cut.width_mm)
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
        f'transform="rotate(-90 12 {TOP + height_px / 2})">width ({unit})</text>'
    )
    return (
        f'<svg viewBox="0 0 {PLOT_W} {PLOT_H}" role="img" '
        f'aria-label="stability map">{"".join(parts)}</svg>'
    )


def render(session_dir: Path, units: Units) -> str:
    """The session's report as one HTML document; lengths in `units`, with mm beside."""
    setup = load_setup(session_dir / session.SETUP_FILE)
    env = envelope(setup)
    rounds = session.load_rounds(session_dir)
    machine, tool, cut, cfg = setup.machine, setup.tool, setup.cut, setup.planner
    manual = machine.spindle is SpindleControl.MANUAL
    material = materials.lookup(cut.material)
    name = session_dir.resolve().name

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
            ["Depths", ", ".join(_length(d, units) for d in session.depths(setup))],
            ["Chip load", f"{_length(env.chip_load_mm, units, 3)} per tooth"],
        ],
        "facts",
    )

    found = session.best_cuts(setup, env, rounds)
    recommended = []
    for depth in sorted({c.depth_mm for c in found}, reverse=True):
        rows = []
        for best in [c for c in found if c.depth_mm == depth][:CUTS_PER_DEPTH]:
            watts = best.mrr_mm3_min / 60 * material.specific_energy_j_mm3 / SPINDLE_EFFICIENCY
            rows.append(
                [
                    f"{best.rpm:.0f}",
                    _length(best.width_mm, units),
                    _feed(feed_mm_min(setup, env, best.rpm), units),
                    _rate(best.mrr_mm3_min, units),
                    f"{watts / WATTS_PER_HP:.2f} hp ({watts / machine.spindle_power_w:.0%})",
                ]
            )
        speed = "Set the spindle to (rpm)" if manual else "Speed (rpm)"
        recommended.append(
            f"<h3>{_e(_length(depth, units))} deep</h3>"
            + _table([speed, "Width", "Feed", "Removal rate", "Spindle power"], rows)
        )
    if not recommended:
        recommended.append("<p>No stable cut has been measured yet.</p>")

    maps = []
    for depth in sorted({r.plan.axial_depth_mm for r in rounds}, reverse=True):
        stability = bayes.build_map(
            session.at_depth(setup, depth), env, session.evidence(rounds, depth, env)
        )
        cuts = session.observations(rounds, depth, env)
        maps.append(
            f"<figure><figcaption>{_e(_length(depth, units))} deep</figcaption>"
            f"{_map_svg(stability, cuts, units, cfg)}</figure>"
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
            by_hand = result is not None and session.verdict_source(result) is (
                VerdictSource.OPERATOR
            )
            rows.append(
                [
                    str(p.id),
                    p.kind.value,
                    str(p.rpm),
                    _length(p.width_mm, units),
                    _feed(p.feed_mm_min, units),
                    "" if result is None else result.status.value,
                    verdict,
                    "by hand" if by_hand and p.kind is PassKind.TEST else "",
                ]
            )
        state = "analysed" if r.results else "not analysed"
        log.append(
            f"<h3>Round {r.plan.round}: {_e(_length(r.plan.axial_depth_mm, units))} deep, "
            f"{_e(r.plan.planner)} planner, planned {_e(r.plan.planned_on)}"
            f", {state}</h3>"
            + _table(
                ["Pass", "Kind", "rpm", "Width", "Feed", "Status", "Verdict", "Verdict set"],
                rows,
                "log",
            )
        )
    if not log:
        log.append("<p>No round has been planned yet.</p>")

    setup_text = (session_dir / session.SETUP_FILE).read_text(encoding="utf-8")
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
<p class="note">The best stable cuts measured at each depth, most material removed first.
{"The spindle is set by hand: set the speed shown. " if manual else ""}Power is
the spindle's estimated draw, and its share of the rated {machine.spindle_power_hp:g} hp.</p>
{"".join(recommended)}
</section>
<section>
<h2>Stability map</h2>
<p class="note">White: likely stable (at least {cfg.min_probability:.0%}). Dark: likely chatter.
Grey: not settled by the cuts made. Circles are stable cuts, crosses chatter.</p>
{"".join(maps)}
</section>
<section class="log">
<h2>Round log</h2>
{"".join(log)}
</section>
<section>
<h2>Setup</h2>
<p class="note">The session's setup.toml, which this report was computed from. Each round
keeps a copy of the setup it was planned from; those may differ from this one in what
may change after a cut, such as the safe height or the speed limits.</p>
<pre>{_e(setup_text)}</pre>
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
