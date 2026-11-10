"""HTTP server behind the cncjs widget: serves the widget and runs one session for it.

The widget talks to cncjs (load the program, watch the job) and to this server (plan,
record, analyse). The microphone is this machine's, so run the server on the PC that sits
next to the mill.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import numpy as np

from . import bayes, session
from .audio import AudioError
from .config import ConfigError, load_setup
from .detect import Thresholds, Verdict
from .plan import PassKind, PlanError, envelope, feed_mm_min
from .recorder import Recorder, RecorderError
from .session import PassStatus, SessionError, Stock

log = logging.getLogger(__name__)

RECORDING_FILE = "recording.wav"
TIMELINE_FILE = "timeline.json"
MAP_SPEEDS = 48
MAP_WIDTHS = 24
TOP_RESULTS = 5
MAX_BODY = 4 * 1024 * 1024
STATIC = {
    "": ("index.html", "text/html; charset=utf-8"),
    "widget.js": ("widget.js", "text/javascript; charset=utf-8"),
    "widget.css": ("widget.css", "text/css; charset=utf-8"),
}


class Next(StrEnum):
    PLAN = "plan"  # plan the first or the next round
    RUN = "run"  # a round is planned: load it, cut it, record it
    CONVERGED = "converged"


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class App:
    """One session and one recorder. Every method that changes them holds the lock."""

    def __init__(self, session_dir: Path, recorder: Recorder, thresholds: Thresholds):
        self.session_dir = session_dir
        self.recorder = recorder
        self.thresholds = thresholds
        self.setup = load_setup(session_dir / session.SETUP_FILE)
        self.env = envelope(self.setup)
        self.lock = threading.Lock()
        self.recording_round: int | None = None

    def program_name(self, number: int) -> str:
        return f"dechatter-{self.session_dir.resolve().name}-round-{number:02d}.nc"

    def _round(self, number: int) -> session.Round:
        for candidate in session.load_rounds(self.session_dir):
            if candidate.plan.round == number:
                return candidate
        raise ApiError(HTTPStatus.NOT_FOUND, f"round {number} does not exist")

    def state(self) -> dict:
        rounds = session.load_rounds(self.session_dir)
        observations = session.observations(rounds)
        stability = bayes.build_map(self.setup, self.env, observations)
        speeds = np.unique(
            np.linspace(0, len(stability.speeds) - 1, MAP_SPEEDS).round().astype(int)
        )
        widths = np.unique(
            np.linspace(0, len(stability.widths) - 1, MAP_WIDTHS).round().astype(int)
        )

        upcoming = Next.PLAN
        if rounds and rounds[-1].results is None:
            upcoming = Next.RUN
        if rounds and rounds[-1].results is not None:
            proposal = bayes.propose(self.setup, self.env, observations)
            upcoming = Next.PLAN if proposal.tests else Next.CONVERGED

        status = self.recorder.status()
        tool, cut = self.setup.tool, self.setup.cut
        return {
            "session": self.session_dir.resolve().name,
            "summary": (
                f"D{tool.diameter_mm:g} mm, {tool.flutes} flutes, {cut.material}, "
                f"depth {cut.axial_depth_mm:g} mm, {self.env.chip_load_mm:g} mm/tooth"
            ),
            "next": upcoming,
            "recorder": {**asdict(status), "round": self.recording_round},
            "rounds": [self._round_json(r) for r in rounds],
            "observations": [
                {"rpm": o.rpm, "width_mm": o.width_mm, "verdict": o.verdict} for o in observations
            ],
            "best": [
                {
                    "rpm": o.rpm,
                    "width_mm": o.width_mm,
                    "feed_mm_min": round(feed_mm_min(self.setup, self.env, o.rpm)),
                    "mrr_cm3_min": round(mrr / 1000, 2),
                }
                for mrr, o in bayes.best_stable(self.setup, self.env, observations)[:TOP_RESULTS]
            ],
            "map": {
                "speeds": stability.speeds[speeds].tolist(),
                "widths": stability.widths[widths].round(2).tolist(),
                "p": stability.p_stable()[np.ix_(speeds, widths)].round(3).tolist(),
            },
        }

    def _round_json(self, r: session.Round) -> dict:
        results = {p.id: p for p in r.results.passes} if r.results else {}
        passes = []
        for p in r.plan.passes:
            result = results.get(p.id)
            passes.append(
                {
                    "id": p.id,
                    "kind": p.kind,
                    "rpm": p.rpm,
                    "width_mm": p.width_mm,
                    "feed_mm_min": p.feed_mm_min,
                    "p_stable": p.p_stable,
                    "status": result.status if result else None,
                    "verdict": result.verdict if result else None,
                    "chatter_hz": result.chatter_hz if result else None,
                    "prominence_db": result.prominence_db if result else None,
                }
            )
        return {
            "round": r.plan.round,
            "planner": r.plan.planner,
            "program": self.program_name(r.plan.round),
            "run_time_s": round(r.plan.run_time_s),
            "face_y_end_mm": round(r.plan.face_y_start_mm + r.plan.consumed_mm, 2),
            "analysed": r.results is not None,
            "has_recording": (r.path / RECORDING_FILE).exists(),
            "passes": passes,
        }

    def plan(self, stock: Stock) -> None:
        with self.lock:
            session.plan_next(self.session_dir, self.setup, self.env, stock)

    def program(self, number: int) -> str:
        return (self._round(number).path / session.PROGRAM_FILE).read_text(encoding="ascii")

    def record_start(self, number: int) -> None:
        with self.lock:
            target = self._round(number)
            self.recorder.start(target.path / RECORDING_FILE)
            self.recording_round = number

    def record_stop(self, timeline: list) -> None:
        """Stop, keep the job timeline cncjs reported, and analyse what was recorded."""
        with self.lock:
            if self.recording_round is None:
                raise ApiError(HTTPStatus.CONFLICT, "not recording")
            target = self._round(self.recording_round)
            self.recording_round = None
            recording = self.recorder.stop()
            (target.path / TIMELINE_FILE).write_text(json.dumps(timeline), encoding="utf-8")
            session.analyze_recording(target, recording, self.thresholds)

    def analyze(self, number: int) -> None:
        with self.lock:
            target = self._round(number)
            recording = target.path / RECORDING_FILE
            if not recording.exists():
                raise ApiError(HTTPStatus.CONFLICT, f"round {number} has no recording")
            session.analyze_recording(target, recording, self.thresholds)

    def set_verdict(self, number: int, pass_id: int, verdict: Verdict) -> None:
        """The operator's ears overrule the detector."""
        with self.lock:
            target = self._round(number)
            if target.results is None:
                raise ApiError(HTTPStatus.CONFLICT, f"round {number} is not analysed")
            passes = []
            for p in target.results.passes:
                if p.id == pass_id:
                    status = PassStatus.ANALYSED if p.status is PassStatus.MISSING else p.status
                    p = session.PassResult(**{**asdict(p), "verdict": verdict, "status": status})
                passes.append(p)
            if all(p.id != pass_id or p.kind is not PassKind.TEST for p in passes):
                raise ApiError(HTTPStatus.NOT_FOUND, f"round {number} has no test pass {pass_id}")
            updated = session.Results(
                target.results.round,
                target.results.recording,
                target.results.thresholds,
                tuple(passes),
            )
            updated.save(target.path / session.RESULTS_FILE)
            log.info("round %d pass %d set to %s by the operator", number, pass_id, verdict)


def _handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            log.debug("%s %s", self.address_string(), format % args)

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large")
            if length == 0:
                return {}
            try:
                body = json.loads(self.rfile.read(length))
            except json.JSONDecodeError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"bad JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return body

        def _route(self) -> list[str]:
            return [part for part in self.path.split("?")[0].split("/") if part]

        def _guard(self, action) -> None:
            try:
                action()
            except ApiError as exc:
                self._json(exc.status, {"error": str(exc)})
            except (KeyError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"bad request: {exc}"})
            except (SessionError, PlanError, AudioError, RecorderError, ConfigError) as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except Exception as exc:  # report it to the widget instead of dropping the socket
                log.exception("%s %s failed", self.command, self.path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"server error: {exc}"})

        def do_OPTIONS(self) -> None:
            self._send(HTTPStatus.NO_CONTENT, b"", "text/plain")

        def do_GET(self) -> None:
            self._guard(self._get)

        def do_POST(self) -> None:
            self._guard(self._post)

        def _get(self) -> None:
            route = self._route()
            name = "/".join(route)
            if name in STATIC:
                file, content_type = STATIC[name]
                body = resources.files("dechatter.widget").joinpath(file).read_bytes()
                return self._send(HTTPStatus.OK, body, content_type)
            if route == ["api", "state"]:
                return self._json(HTTPStatus.OK, app.state())
            if len(route) == 4 and route[:2] == ["api", "rounds"] and route[3] == "program":
                return self._send(
                    HTTPStatus.OK, app.program(int(route[2])).encode(), "text/plain; charset=utf-8"
                )
            raise ApiError(HTTPStatus.NOT_FOUND, f"no such resource: {self.path}")

        def _post(self) -> None:
            route, body = self._route(), self._body()
            if route == ["api", "plan"]:
                app.plan(Stock(body.get("stock", Stock.SAME)))
            elif route == ["api", "record", "start"]:
                app.record_start(int(body["round"]))
            elif route == ["api", "record", "stop"]:
                app.record_stop(list(body.get("timeline", [])))
            elif len(route) == 4 and route[:2] == ["api", "rounds"] and route[3] == "analyze":
                app.analyze(int(route[2]))
            elif len(route) == 4 and route[:2] == ["api", "rounds"] and route[3] == "verdict":
                app.set_verdict(int(route[2]), int(body["pass"]), Verdict(body["verdict"]))
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, f"no such resource: {self.path}")
            self._json(HTTPStatus.OK, app.state())

    return Handler


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _handler(app))


def serve(app: App, host: str, port: int) -> None:
    server = make_server(app, host, port)
    print(f"dechatter widget at http://{host}:{server.server_port}/")
    print(f"microphone: {app.recorder.status().device}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.recorder.close()
