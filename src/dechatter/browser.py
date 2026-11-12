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
import zipfile
from pathlib import Path

import numpy as np

from . import gcode, materials, session
from .app import App
from .audio import AudioError
from .config import TEMPLATE, ConfigError, dump_setup, setup_values
from .detect import Thresholds, Verdict
from .plan import PlanError
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
            if not (self._dir(name) / session.SETUP_FILE).exists():
                raise SessionError(f"no session named {name!r}")
            self._apps[name] = App(self._dir(name), self.thresholds)
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

    def setup_form(self, name: str) -> dict:
        """What the widget's setup wizard needs: the current values and the material choices."""
        self._app(name)  # the session must exist and its setup must load
        return {
            "values": setup_values(self._dir(name) / session.SETUP_FILE),
            "materials": sorted(materials.MATERIALS),
        }

    def save_setup(self, name: str, values_json: str) -> dict:
        """Replace the setup if it is valid and fits the rounds; else leave the session untouched.

        A round planned but not yet run is discarded when the setup changes, so the next
        one is planned from the new setup.
        """
        current = self._app(name)
        if current.recording_round is not None:
            raise SessionError(
                f"round {current.recording_round} is being recorded; change the setup after it"
            )
        text = dump_setup(json.loads(values_json)).encode("utf-8")
        path = self._dir(name) / session.SETUP_FILE
        previous = path.read_bytes()
        self._apps.pop(name, None)
        try:
            path.write_bytes(text)
            app = self._app(name)
            stale = session.stale_round(self._dir(name), current.setup, app.setup)
            shown = app.state()
        except Exception:
            path.write_bytes(previous)
            self._apps.pop(name, None)
            raise
        if stale is None:
            return shown
        session.discard_round(stale)
        app.discarded_round = stale.plan.round
        return app.state()

    def state(self, name: str) -> dict:
        return self._app(name).state()

    def plan(self, name: str, stock: str) -> dict:
        self._app(name).plan(Stock(stock))
        return self.state(name)

    def program(self, name: str, number: int, machine_units: str) -> str:
        """The round's program for a machine in `machine_units` ("mm" or "in")."""
        return gcode.for_machine(self._app(name).program(number), gcode.Units(machine_units))

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

    def export_zip(self, name: str) -> bytes:
        """The session directory as a zip, usable with the command line as it is."""
        self._app(name)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(self._dir(name).rglob("*")):
                if path.is_file():
                    archive.write(path, f"{name}/{path.relative_to(self._dir(name)).as_posix()}")
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
