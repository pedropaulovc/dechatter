"""Command line: init -> plan -> (cut, record) -> analyze -> plan -> ... until converged."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

from . import audio, bayes, materials, report, session
from .audio import AudioError
from .config import TEMPLATE, ConfigError, Setup, load_setup, read_setup
from .detect import Thresholds, Verdict
from .export import ExportError, export
from .gcode import Units
from .plan import Envelope, PassKind, Plan, PlanError, envelope
from .session import PassStatus, SessionError, Stock

TOP_RESULTS = 5
MAP_COLUMNS = 16
MAP_ROWS = 10


def cmd_init(args: argparse.Namespace) -> int:
    target = args.session / session.SETUP_FILE
    if target.exists():
        raise SessionError(f"{target} already exists")
    args.session.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE, encoding="utf-8")
    print(f"wrote {target}; edit it, then run: dechatter plan {args.session}")
    return 0


def cmd_materials(_: argparse.Namespace) -> int:
    print(f"{'material':<16} {'m/min':>6} {'fz/D':>6} {'J/mm3':>6}")
    for name, m in sorted(materials.MATERIALS.items()):
        print(
            f"{name:<16} {m.surface_speed_m_min:>6.0f} {m.chip_load_per_diameter:>6.3f} "
            f"{m.specific_energy_j_mm3:>6.2f}"
        )
    return 0


def _print_known(setup: Setup, env: Envelope, rounds: list[session.Round]) -> None:
    """The stability map of each depth that was cut, then the best cuts of them all."""
    for depth in sorted({r.plan.axial_depth_mm for r in rounds}, reverse=True):
        stability = bayes.build_map(
            session.at_depth(setup, depth), env, session.evidence(rounds, depth, env)
        )
        print(f"depth {depth:g} mm:")
        _print_map(stability, session.observations(rounds, depth, env))
    ranked = session.best_cuts(setup, env, rounds)
    if not ranked:
        print("no stable cut measured yet")
        return
    print("best stable cuts measured:")
    for cut in ranked[:TOP_RESULTS]:
        feed = env.chip_load_mm * setup.tool.flutes * cut.rpm
        print(
            f"  S{cut.rpm:.0f}  width {cut.width_mm:.2f} mm  depth {cut.depth_mm:g} mm"
            f"  F{feed:.0f} mm/min  {cut.mrr_mm3_min / 1000:.2f} cm3/min"
        )
    for sweep in session.sweeps(rounds):
        _print_sweep(setup, sweep)


def _print_sweep(setup: Setup, sweep: session.Sweep) -> None:
    where = f"S{sweep.rpm:.0f} width {sweep.width_mm:.2f} mm depth {sweep.depth_mm:g} mm"
    found = sweep.stable_chip_load_mm
    if found is None:
        print(f"chip-load sweep (round {sweep.round}) at {where}: its lightest pass chattered")
        return
    print(
        f"chip-load sweep (round {sweep.round}) at {where}: stable up to {found:g} mm/tooth"
        f" ({found / sweep.session_chip_load_mm:.2f}x the session's),"
        f" F{sweep.feed_mm_min:.0f} mm/min"
    )


def _print_map(stability: bayes.StabilityMap, observations: list[bayes.Observation]) -> None:
    """Probability of stability in tenths (9 = at least 90%); S/U mark measured cuts."""
    p = stability.p_stable()
    columns = np.unique(np.linspace(0, len(stability.speeds) - 1, MAP_COLUMNS).round().astype(int))
    rows = np.unique(np.linspace(0, len(stability.widths) - 1, MAP_ROWS).round().astype(int))
    cells = {
        (r, c): str(min(int(p[columns[c], rows[r]] * 10), 9))
        for r in range(len(rows))
        for c in range(len(columns))
    }
    for obs in observations:
        c = int(np.argmin(np.abs(stability.speeds[columns] - obs.rpm)))
        r = int(np.argmin(np.abs(stability.widths[rows] - obs.width_mm)))
        unstable = obs.verdict is Verdict.UNSTABLE
        if unstable or cells[(r, c)] != "U":
            cells[(r, c)] = "U" if unstable else "S"
    print("stability map (width mm down, rpm across):")
    for r in reversed(range(len(rows))):
        line = "    ".join(cells[(r, c)] for c in range(len(columns)))
        print(f"  {stability.widths[rows[r]]:5.2f} | {line}")
    print("        | " + " ".join(f"{stability.speeds[c]:<4.0f}" for c in columns))


def _print_round(session_dir: Path, plan: Plan) -> None:
    tests = [p for p in plan.passes if p.kind is PassKind.TEST]
    print(
        f"round {plan.round}, {plan.axial_depth_mm:g} mm deep: {len(tests)} test cuts, "
        f"about {plan.run_time_s / 60:.0f} min, "
        f"uses {plan.consumed_mm:.1f} mm of stock width"
    )
    for p in tests:
        chance = "" if p.p_stable is None else f"  P(stable) {p.p_stable:.2f}"
        chip = "" if p.chip_load_mm is None else f"  {p.chip_load_mm:g} mm/tooth"
        print(
            f"  pass {p.id:2d}: S{p.rpm} width {p.width_mm:.2f} mm F{p.feed_mm_min:.0f}"
            f"{chance}{chip}"
        )
    print(f"wrote {session.round_dir(session_dir, plan.round) / session.PROGRAM_FILE}")
    print(f"run it while recording, then: dechatter analyze {session_dir} <recording>")


def cmd_plan(args: argparse.Namespace) -> int:
    source = read_setup(
        (args.session / session.SETUP_FILE).read_text(encoding="utf-8"), session.SETUP_FILE
    )
    setup = source.setup
    env = envelope(setup)
    rounds = session.load_rounds(args.session)
    stock = Stock.FRESH if args.new_stock else Stock.SAME
    if args.sweep:
        plan = session.plan_sweep(args.session, source, env, stock)
        _print_round(args.session, plan)
        return 0
    planned = session.plan_next(args.session, source, env, stock)
    if any(r.results is not None for r in rounds):
        _print_known(setup, env, rounds)
    if planned.plan is None:
        print(
            f"converged: no further cut is expected to add {setup.planner.stop_improvement:.0%} "
            f"removal rate (best candidate {planned.expected_improvement:.1%})"
        )
        if session.to_sweep(setup, env, rounds):
            print(
                f"to find how far the chip load can go at the best cut: "
                f"dechatter plan {args.session} --sweep"
            )
        return 0
    _print_round(args.session, planned.plan)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    rounds = session.load_rounds(args.session)
    if not rounds:
        raise SessionError("no rounds planned; run `dechatter plan` first")
    by_number = {r.plan.round: r for r in rounds}
    number = args.round or rounds[-1].plan.round
    if number not in by_number:
        raise SessionError(f"round {number} does not exist")
    target = by_number[number]

    manual = audio.read_segments(args.segments) if args.segments else None
    thresholds = Thresholds(args.prominence_db, args.excess_db)
    results = session.analyze_recording(target, args.recording, thresholds, manual)

    print(f"round {number}: {args.recording.name}")

    print(" pass kind       rpm  width  status    verdict   peak Hz  prom  vs air")
    for p in results.passes:
        if p.status is PassStatus.MISSING:
            print(f"  {p.id:3d} {p.kind:<8} {p.rpm:5d} {p.width_mm:6.2f}  missing")
            continue
        air = "     -" if p.excess_db is None else f"{p.excess_db:6.1f}"
        print(
            f"  {p.id:3d} {p.kind:<8} {p.rpm:5d} {p.width_mm:6.2f}  {p.status:<9} {p.verdict:<8} "
            f"{p.chatter_hz:8.1f} {p.prominence_db:5.1f} {air}"
        )
    missing = [
        p.id for p in results.passes if p.status is PassStatus.MISSING and p.kind is PassKind.TEST
    ]
    if missing:
        print(f"test passes not found in the recording: {missing}")
    print(
        f"wrote {target.path / session.RESULTS_FILE} (edit a verdict there if your ears disagree)"
    )
    print(f"next: dechatter plan {args.session}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if args.html:
        units = Units(args.units or Units.MM)
        args.html.write_text(report.render(args.session, units), encoding="utf-8")
        print(f"wrote {args.html}")
        return 0
    setup = load_setup(args.session / session.SETUP_FILE)
    env = envelope(setup)
    rounds = session.load_rounds(args.session)
    print(
        f"envelope: {env.speed_min_rpm}..{env.speed_max_rpm} rpm, width up to "
        f"{env.width_max_mm:.2f} mm, {env.chip_load_mm} mm/tooth, "
        f"power cap {env.mrr_limit_mm3_min / 1000:.1f} cm3/min"
    )
    _print_known(setup, env, rounds)
    return 0


def cmd_widget(args: argparse.Namespace) -> int:
    export(args.folder)
    folder = args.folder.resolve()
    print(f"wrote the widget to {folder}")
    print(f"start cncjs with:  cncjs --mount /dechatter:{folder}")
    print("then enable the Custom widget in cncjs and set its URL to /dechatter/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dechatter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log analysis details")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a session directory with a setup.toml template")
    init.add_argument("session", type=Path)
    init.set_defaults(run=cmd_init)

    plan = commands.add_parser("plan", help="write the next round's G-code, or report convergence")
    plan.add_argument("session", type=Path)
    plan.add_argument(
        "--new-stock", action="store_true", help="fresh stock is mounted: front face is back at Y0"
    )
    plan.add_argument(
        "--sweep",
        action="store_true",
        help="once converged: try the best cut at rising chip loads",
    )
    plan.set_defaults(run=cmd_plan)

    analyze = commands.add_parser(
        "analyze", help="classify each pass of a round from its recording"
    )
    analyze.add_argument("session", type=Path)
    analyze.add_argument("recording", type=Path)
    analyze.add_argument("--round", type=int, help="default: the latest round")
    analyze.add_argument("--segments", type=Path, help="CSV of start,end seconds per pass run")
    defaults = Thresholds()
    analyze.add_argument("--prominence-db", type=float, default=defaults.prominence_db)
    analyze.add_argument("--excess-db", type=float, default=defaults.excess_db)
    analyze.set_defaults(run=cmd_analyze)

    summary = commands.add_parser(
        "report", help="show the stability map and the best stable cuts, or write them as HTML"
    )
    summary.add_argument("session", type=Path)
    summary.add_argument("--html", type=Path, help="write a printable HTML report to this file")
    summary.add_argument(
        "--units",
        choices=[u.value for u in Units],
        help="with --html: lengths in the machine's mm (G21, the default) or in (G20)",
    )
    summary.set_defaults(run=cmd_report)

    listing = commands.add_parser("materials", help="list the built-in material table")
    listing.set_defaults(run=cmd_materials)

    widget = commands.add_parser("widget", help="write the cncjs widget as a static folder")
    widget.add_argument("folder", type=Path)
    widget.set_defaults(run=cmd_widget)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "units", None) and not args.html:
        parser.error("--units applies to the HTML report only: add --html <file>")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    try:
        return args.run(args)
    except (
        ConfigError,
        PlanError,
        SessionError,
        AudioError,
        ExportError,
        FileNotFoundError,
    ) as exc:
        print(f"dechatter: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
