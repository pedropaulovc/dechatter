"""Command line: build the static cncjs widget folder (`dechatter widget <folder>`)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .export import ExportError, export


def cmd_widget(args: argparse.Namespace) -> int:
    export(args.folder)
    folder = args.folder.resolve()
    print(f"wrote the widget to {folder}")
    print(f"start cncjs with:  cncjs --mount /dechatter:{folder}")
    print("then enable the Custom widget in cncjs and set its URL to /dechatter/")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dechatter", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log build details")
    commands = parser.add_subparsers(dest="command", required=True)

    widget = commands.add_parser("widget", help="write the cncjs widget as a static folder")
    widget.add_argument("folder", type=Path)
    widget.set_defaults(run=cmd_widget)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s"
    )
    try:
        return args.run(args)
    except (ExportError, OSError) as exc:
        print(f"dechatter: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
