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
from pathlib import Path

import numpy as np

from . import gcode, materials, report, session
from .app import RECORDING_FILE, App
from .audio import AudioError
from .config import (
    TEMPLATE,
    ConfigError,
    dump_setup,
    parse_setup,
    setup_defaults,
    setup_values,
)
from .detect import Thresholds, Verdict
from .plan import PlanError, envelope
from .session import SessionError, Stock

log = logging.getLogger(__name__)

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
EXPECTED = (ConfigError, PlanError, SessionError, AudioError)


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

    def create(self, name: str) -> str:
        target = self._dir(name)
        if target.exists():
            raise SessionError(f"session {name!r} already exists")
        target.mkdir()
        (target / session.SETUP_FILE).write_text(TEMPLATE, encoding="utf-8")
        return name

    def _existing(self, name: str) -> Path:
        """A session's directory, without reading anything in it: it may not load."""
        target = self._dir(name)
        if not (target / session.SETUP_FILE).exists():
            raise SessionError(f"no session named {name!r}")
        return target

    def contents(self, name: str) -> dict:
        """What deleting a session would lose, counted without loading it."""
        target = self._existing(name)
        rounds = [p for p in target.glob("round-*") if p.is_dir()]
        return {
            "rounds": len(rounds),
            "recordings": sum((r / RECORDING_FILE).exists() for r in rounds),
        }

    def delete(self, name: str) -> list[str]:
        """Remove a session for good, whether or not it loads; answer the names left."""
        target = self._existing(name)
        opened = self._apps.get(name)
        if opened is not None and opened.recording_round is not None:
            raise SessionError(
                f"round {opened.recording_round} of {name!r} is being recorded; delete the "
                "session after the job"
            )
        self._apps.pop(name, None)
        shutil.rmtree(target)
        log.info("deleted session %s", name)
        return self.names()

    def setup_form(self, name: str) -> dict:
        """What the widget's setup wizard needs: the current values and the material choices."""
        self._app(name)  # the session must exist and its setup must load
        return {
            "values": setup_values(self._dir(name) / session.SETUP_FILE),
            "defaults": setup_defaults(),
            "materials": sorted(materials.MATERIALS),
        }

    def setup_preview(self, values_json: str) -> dict:
        """What the planner makes of entries left empty in a setup the wizard is editing."""
        env = envelope(parse_setup(dump_setup(json.loads(values_json)), "the setup"))
        return {
            "cut": {
                "width_max_mm": round(env.width_max_mm, 3),
                "chip_load_mm": env.chip_load_mm,
                "speed_min_rpm": env.speed_min_rpm,
                "speed_max_rpm": env.speed_max_rpm,
            }
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

    def program(self, name: str, number: int, machine_units: str) -> str:
        """The round's program for a machine in `machine_units` ("mm" or "in")."""
        return self._app(name).program(number, gcode.Units(machine_units))

    def record_start(self, name: str, number: int) -> dict:
        self._app(name).record_start(number)
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
        """The session directory as a zip, usable with the command line as it is, with the
        report as of now. Export is the way out of the browser's storage, so a report that
        cannot be made is left out rather than stopping it."""
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
                if inside.parts[0].startswith(session.DISCARDED) or inside == Path(
                    report.REPORT_FILE
                ):
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
    except EXPECTED as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        log.exception("%s failed", method)
        return json.dumps({"error": f"internal error in {method}: {exc!r}"})
    if isinstance(result, bytes):
        return result
    return json.dumps({"ok": result})
