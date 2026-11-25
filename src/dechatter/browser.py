"""Entry points the widget's worker calls when dechatter runs inside the browser (Pyodide).

Sessions are directories under one root, exactly as on disk; in the browser that root is a
filesystem persisted to IndexedDB. Every call goes through `call`, which answers with JSON:
`{"ok": value}` or `{"error": message}`.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import zipfile
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

import numpy as np

from . import gcode, materials, report, seeding, session
from .app import App
from .audio import AudioError
from .config import (
    TEMPLATE,
    ConfigError,
    SetupSource,
    Vary,
    dump_setup,
    parse_setup,
    setup_defaults,
    setup_source,
    setup_values,
    slot_wall_mm,
)
from .detect import Thresholds, Verdict
from .plan import PlanError, auto_lanes, envelope
from .session import SessionError, Stock

log = logging.getLogger(__name__)


class SetupLoad(StrEnum):
    LOADS = "loads"
    REFUSED = "refused"  # from dechatter 0.1.x, or damaged: only Delete applies
    # Its seed keeps it from loading (damaged, or measured another assembly) and no round is
    # planned: removing the seed (unseed) lets it load.
    SEED_REFUSED = "seed_refused"


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
EXPECTED = (ConfigError, PlanError, SessionError, AudioError, seeding.SeedError)


class Sessions:
    def __init__(self, root: Path, thresholds: Thresholds | None = None):
        self.root = root
        self.thresholds = thresholds or Thresholds()
        self._apps: dict[str, App] = {}
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, name: str) -> Path:
        if not NAME.fullmatch(name):
            raise SessionError(
                "session names use letters, digits, dot, dash and underscore (64 at most)"
            )
        return self.root / name

    def _app(self, name: str) -> App:
        if name not in self._apps:
            target = self._existing(name)
            session.sweep_discarded(target)
            self._apps[name] = App(target, self.thresholds)
        return self._apps[name]

    def names(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob(f"*/{session.SETUP_FILE}"))

    def create(self, name: str, source: str = "") -> str:
        """A new session: on the template setup, or started from the session `source`.

        Started from another, it takes that session's setup as its own, to be changed in what
        the new cuts differ in, and keeps its analysed passes that got a verdict, with the
        setup they were measured with, in its own folder (see seeding). The other session
        must load, rounds and all, and hold such a pass.
        """
        target = self._dir(name)
        if target.exists():
            raise SessionError(f"session {name!r} already exists")
        text, seed = TEMPLATE, None
        if source:
            seed = self._seed_of(source)
            text = seed.setup_text
        target.mkdir()
        try:
            (target / session.SETUP_FILE).write_text(text, encoding="utf-8")
            if seed is not None:
                seed.save(target / seeding.SEED_FILE)
        except Exception:
            # Not half a session: without its seed it would pass for one started on its own.
            shutil.rmtree(target, ignore_errors=True)
            raise
        if seed is not None:
            log.info("%s: started from %d passes of %s", name, len(seed.passes), source)
        return name

    def _seed_of(self, source: str) -> seeding.Seed:
        """What a session started from `source` keeps of it: its analysed passes that got a
        verdict, with the setup they were measured with."""
        origin = self._existing(source)
        # The other session must open as it would in the widget, and its rounds belong to its
        # setup: a setup changed by hand since they were cut measured other cuts.
        measured = self._app(source).source
        rounds = session.load_rounds(origin)
        session.check_measured(rounds, measured.setup)
        passes = session.seed_passes(rounds)
        if not passes:
            raise SessionError(
                f"session {source!r} has no analysed cut with a verdict to start from"
            )
        return seeding.Seed(source, measured.text, passes)

    def _before_round_1(self, target: Path, rule: str) -> None:
        """Refuse a change to what a session starts from once a round is planned: its rounds
        were planned, and its map built, with what it started from then."""
        if session.contents(target).rounds:
            raise SessionError(
                f"session {target.name!r} has rounds, and {rule}. Reset it to discard them, or "
                "create a new session"
            )

    def start_from(self, name: str, source: str) -> dict:
        """Start a session that has no round yet from the session `source`; answer its state.

        Unlike `create`, the session keeps its own setup, `[pending]` included: a prepared
        session arrives with it. It must be the tool assembly `source` measured, but for a
        stickout still to confirm on the machine, which saving the wizard confirms and checks.
        `source` must hold an analysed cut as for `create`. To start from another session
        instead, remove the seed first (`unseed`).
        """
        target = self._existing(name)
        if source == name:
            raise SessionError("a session cannot start from itself")
        self._before_round_1(target, "a session's map can only be seeded before its first round")
        app = self._app(name)  # its setup, and any seed it holds, must load
        if app.seed is not None:
            raise SessionError(
                f"session {name!r} already starts from {app.seed.source}: choose Don't start "
                f"from {app.seed.source} first"
            )
        if setup_source(app.source.text) is SetupSource.TEMPLATE:
            # The example setup is no tool assembly to compare: New takes over the earlier
            # session's setup instead.
            raise SessionError(
                f"session {name!r} still holds the example setup: fill it in with the setup "
                "wizard first, or create the session with New, starting from the earlier one"
            )
        seed = self._seed_of(source)
        pending = {at for at, _ in app.source.pending}
        found = seeding.mismatches(seed.setup, app.setup, pending)
        if found:
            raise seeding.SeedError(
                f"session {name!r} cannot start from {source}, which measured another tool "
                f"assembly: {'; '.join(found)}. A stability map belongs to one machine, holder, "
                "cutter and stickout"
            )
        path = target / seeding.SEED_FILE
        try:
            seed.save(path)
            self._apps.pop(name, None)
            shown = self.state(name)
        except Exception:
            # Not half a seed: the session goes on as it was, started on its own.
            path.unlink(missing_ok=True)
            self._apps.pop(name, None)
            raise
        later = " (its stickout once confirmed)" if seeding.STICKOUT in pending else ""
        log.info("%s: started from %d passes of %s%s", name, len(seed.passes), source, later)
        return shown

    def unseed(self, name: str) -> dict:
        """Stop a session that has no round yet starting from another; answer its state. Its
        `seed.json` is removed, so round 1 is the grid; the setup stays as it is.

        The seed is not read: one that is damaged, or measured another assembly than the
        setup, so that the session does not load, can be removed too. A session that does not
        load without it keeps it.
        """
        target = self._existing(name)
        self._before_round_1(target, "a seed can only be removed before the first round")
        path = target / seeding.SEED_FILE
        if not path.exists():
            raise SessionError(f"session {name!r} does not start from another session")
        kept = path.read_bytes()
        path.unlink()
        self._apps.pop(name, None)
        try:
            shown = self.state(name)
        except Exception:
            # It may be the only copy of the earlier session's cuts.
            path.write_bytes(kept)
            self._apps.pop(name, None)
            raise
        log.info("%s: no longer starts from another session", name)
        return shown

    def _existing(self, name: str) -> Path:
        """A session's directory, without reading anything in it: it may not load."""
        target = self._dir(name)
        if not (target / session.SETUP_FILE).exists():
            raise SessionError(f"no session named {name!r}")
        return target

    def contents(self, name: str) -> dict:
        """What deleting or resetting a session would lose, counted without loading it."""
        return asdict(session.contents(self._existing(name)))

    def _not_recording(self, name: str, action: str) -> None:
        opened = self._apps.get(name)
        if opened is not None and opened.recording_round is not None:
            raise SessionError(
                f"round {opened.recording_round} of {name!r} is being recorded; {action} the "
                "session after the job"
            )

    def reset(self, name: str) -> dict:
        """Discard every round of a session, keeping its setup; answer its new state.

        Only a session whose setup loads: one that does not (from dechatter 0.1.x, or
        damaged) is refused before anything changes; Delete is the way to be rid of it.
        """
        target = self._existing(name)
        self._not_recording(name, "reset")
        self._apps.pop(name, None)
        app = self._app(name)
        session.reset(target)
        return app.state()

    def setup_status(self, name: str) -> dict:
        """Whether a session's setup loads, its rounds aside: reset and export need it to.

        The session it loads is kept, for the state asked next.
        """
        target = self._existing(name)
        try:
            self._app(name)
        except EXPECTED as exc:
            removable = isinstance(exc, seeding.SeedError) and not session.contents(target).rounds
            status = SetupLoad.SEED_REFUSED if removable else SetupLoad.REFUSED
            return {"status": status, "reason": str(exc)}
        return {"status": SetupLoad.LOADS, "reason": ""}

    def delete(self, name: str) -> list[str]:
        """Remove a session for good, whether or not it loads; answer the names left."""
        target = self._existing(name)
        self._not_recording(name, "delete")
        self._apps.pop(name, None)
        shutil.rmtree(target)
        log.info("deleted session %s", name)
        return self.names()

    def setup_form(self, name: str) -> dict:
        """What the widget's setup wizard needs: the current values, the material choices and
        the fields the setup marks to confirm on the machine, with their notes."""
        app = self._app(name)  # the session must exist and its setup must load
        return {
            "values": setup_values(self._dir(name) / session.SETUP_FILE),
            "defaults": setup_defaults(),
            "materials": sorted(materials.MATERIALS),
            "pending": dict(app.source.pending),
            # The session this one was started from, whose tool assembly it must keep.
            "seed": None if app.seed is None else app.seed.source,
        }

    def setup_preview(self, values_json: str) -> dict:
        """What the planner makes of entries left empty in a setup the wizard is editing."""
        setup = parse_setup(dump_setup(json.loads(values_json)), "the setup")
        env = envelope(setup)
        cut = {
            "chip_load_mm": env.chip_load_mm,
            "speed_min_rpm": env.speed_min_rpm,
            "speed_max_rpm": env.speed_max_rpm,
        }
        if setup.cut.vary is Vary.WIDTH:
            cut["width_max_mm"] = round(env.engagement_max_mm, 3)
        return {
            "cut": cut,
            "stock": {"lanes": auto_lanes(setup, env)},
            "program": {"slot_wall_mm": round(slot_wall_mm(setup), 3)},
        }

    def save_setup(self, name: str, values_json: str) -> dict:
        """Replace the setup if it is valid and fits the rounds; else leave the session untouched.

        A round planned but not yet run is discarded when the setup changes its program, so
        the next one is planned from the new setup.
        """
        recording = self._app(name).recording_round
        if recording is not None:
            raise SessionError(f"round {recording} is being recorded; change the setup after it")
        text = dump_setup(json.loads(values_json)).encode("utf-8")
        path = self._dir(name) / session.SETUP_FILE
        previous = path.read_bytes()
        self._apps.pop(name, None)
        stale, aside = None, None
        try:
            path.write_bytes(text)
            app = self._app(name)
            stale = session.stale_round(self._dir(name), app.setup)
            if stale is not None:
                aside = session.set_aside(stale)
                app.discarded_round = stale.plan.round
            shown = app.state()
        except Exception:
            if aside is not None:
                session.restore(aside, stale)
            path.write_bytes(previous)
            self._apps.pop(name, None)
            raise
        if aside is None:
            return shown
        log.info("%s: discarded unrun round %d planned from the old setup", name, stale.plan.round)
        # The new setup is saved: a round that will not delete now goes with the next sweep.
        try:
            shutil.rmtree(aside)
        except OSError as exc:
            log.warning("%s: could not delete %s yet: %s", name, aside.name, exc)
        return shown

    def state(self, name: str) -> dict:
        return self._app(name).state()

    def plan(self, name: str, stock: str) -> dict:
        self._app(name).plan(Stock(stock))
        return self.state(name)

    def plan_sweep(self, name: str, stock: str) -> dict:
        self._app(name).plan_sweep(Stock(stock))
        return self.state(name)

    def plan_check(self, name: str, stock: str, level_mm: float | None = None) -> dict:
        self._app(name).plan_check(Stock(stock), level_mm)
        return self.state(name)

    def program(self, name: str, number: int, machine_units: str) -> str:
        """The round's program for a machine in `machine_units` ("mm" or "in")."""
        return self._app(name).program(number, gcode.Units(machine_units))

    def steps(self, name: str, number: int, machine_units: str) -> list[dict]:
        """The round's program as blocks to run one at a time, in `machine_units`."""
        return self._app(name).steps(number, gcode.Units(machine_units))

    def record_start(self, name: str, number: int) -> dict:
        self._app(name).record_start(number)
        return self.state(name)

    def record_cancel(self, name: str) -> dict:
        self._app(name).record_cancel()
        return self.state(name)

    def record_stop(self, name: str, samples, rate: float, timeline: str) -> dict:
        mono = np.asarray(samples, dtype=np.float32)
        self._app(name).record_stop(mono, int(round(rate)), json.loads(timeline))
        return self.state(name)

    def analyze(self, name: str, number: int) -> dict:
        self._app(name).analyze(number)
        return self.state(name)

    def set_verdict(self, name: str, number: int, pass_id: int, verdict: str) -> dict:
        self._app(name).set_verdict(number, pass_id, Verdict(verdict))
        return self.state(name)

    def report_html(self, name: str, machine_units: str) -> str:
        """The session's printable report, lengths in `machine_units` ("mm" or "in")."""
        self._app(name)
        return report.render(self._dir(name), gcode.Units(machine_units))

    def export_zip(self, name: str, machine_units: str) -> bytes:
        """The session directory as a zip, with the report as of now. Export is the way out of
        the browser's storage, so a report that cannot be made is left out rather than stopping
        it."""
        self._app(name)
        try:
            page = report.render(self._dir(name), gcode.Units(machine_units))
        except Exception:
            log.exception("%s: exporting without %s", name, report.REPORT_FILE)
            page = None
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self._dir(name).rglob("*")):
                inside = path.relative_to(self._dir(name))
                top = self._dir(name) / inside.parts[0]
                left = inside.parts[0].startswith(session.DISCARDED) or session.finished_reset(top)
                if left or inside == Path(report.REPORT_FILE):
                    continue
                if path.is_file():
                    archive.write(path, f"{name}/{inside.as_posix()}")
            if page is not None:
                archive.writestr(f"{name}/{report.REPORT_FILE}", page)
        return buffer.getvalue()

    def import_zip(self, data: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
            members = [m for m in archive.infolist() if not m.is_dir()]
            roots = {m.filename.split("/")[0] for m in members}
            if len(roots) != 1 or any(".." in m.filename.split("/") for m in members):
                raise SessionError("not a dechatter session export")
            name = roots.pop()
            target = self._dir(name)
            if f"{name}/{session.SETUP_FILE}" not in archive.namelist():
                raise SessionError("not a dechatter session export: no setup.toml")
            if target.exists():
                raise SessionError(f"session {name!r} already exists")
            archive.extractall(self.root, members)
        return name


def call(sessions: Sessions, method: str, *args) -> str | bytes:
    """Run one widget request. Bytes results pass through; everything else is JSON."""
    try:
        result = getattr(sessions, method)(*args)
        if isinstance(result, bytes):
            return result
        # NaN and infinity are not JSON: the widget's JSON.parse would fail on them.
        return json.dumps({"ok": result}, allow_nan=False)
    except EXPECTED as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        log.exception("%s failed", method)
        return json.dumps({"error": f"internal error in {method}: {exc!r}"})
