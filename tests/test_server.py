from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
import synth
from synth import SimulatedMic

from dechatter import cli, server
from dechatter.detect import Thresholds

MACHINE = synth.Machine()


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, path: str, body: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                is_json = response.headers["Content-Type"] == "application/json"
                return response.status, json.loads(raw) if is_json else raw.decode()
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())


@pytest.fixture
def served(tmp_path):
    work = tmp_path / "al-10mm"
    assert cli.main(["init", str(work)]) == 0
    mic = SimulatedMic()
    http = server.make_server(server.App(work, mic, Thresholds()), "127.0.0.1", 0)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield Client(f"http://127.0.0.1:{http.server_port}/"), mic, work
    http.shutdown()
    http.server_close()


def run_round(client: Client, number: int) -> dict:
    assert client.call("api/record/start", {"round": number})[0] == 200
    status, state = client.call("api/record/stop", {"timeline": [{"t": 1.0, "event": "workflow"}]})
    assert status == 200
    return state


def test_widget_assets_are_served(served):
    client, _, _ = served
    status, page = client.call("")
    assert status == 200 and 'src="widget.js"' in page
    assert client.call("widget.js")[0] == 200
    assert client.call("widget.css")[0] == 200
    assert client.call("setup.toml")[0] == 404


def test_session_runs_to_convergence_over_http(served, setup):
    client, _, work = served
    _, state = client.call("api/state")
    assert state["next"] == "plan" and state["rounds"] == []

    for _ in range(10):
        _, state = client.call("api/plan", {"stock": "fresh"})
        if state["next"] == "converged":
            break
        assert state["next"] == "run"
        number = state["rounds"][-1]["round"]
        status, program = client.call(f"api/rounds/{number}/program")
        assert status == 200 and program.rstrip().endswith("M30")
        state = run_round(client, number)
        latest = state["rounds"][-1]
        assert latest["analysed"]
        for p in latest["passes"]:
            if p["kind"] != "test":
                continue
            truth = MACHINE.chatters(p["rpm"], p["width_mm"], setup.tool.flutes)
            assert p["verdict"] == ("unstable" if truth else "stable")
    else:
        pytest.fail("did not converge in 10 rounds")

    assert state["best"][0]["mrr_cm3_min"] > 12
    assert len(state["observations"]) == sum(
        p["kind"] == "test" for r in state["rounds"] for p in r["passes"]
    )
    timeline = json.loads((work / "round-01" / server.TIMELINE_FILE).read_text())
    assert timeline == [{"t": 1.0, "event": "workflow"}]


def test_operator_verdict_overrules_the_detector_and_feeds_the_planner(served):
    client, _, _ = served
    client.call("api/plan", {})
    state = run_round(client, 1)
    tests = [p for p in state["rounds"][0]["passes"] if p["kind"] == "test"]
    stable = next(p for p in tests if p["verdict"] == "stable")
    before = sum(o["verdict"] == "unstable" for o in state["observations"])

    status, state = client.call(
        "api/rounds/1/verdict", {"pass": stable["id"], "verdict": "unstable"}
    )
    assert status == 200
    changed = next(p for p in state["rounds"][0]["passes"] if p["id"] == stable["id"])
    assert changed["verdict"] == "unstable"
    assert sum(o["verdict"] == "unstable" for o in state["observations"]) == before + 1

    assert client.call("api/rounds/1/verdict", {"pass": 0, "verdict": "unstable"})[0] == 404
    assert client.call("api/rounds/1/verdict", {"pass": stable["id"], "verdict": "loud"})[0] == 400


def test_unusable_recording_is_reported_and_the_round_can_be_run_again(served):
    client, mic, _ = served
    client.call("api/plan", {})
    mic.silent = True
    client.call("api/record/start", {"round": 1})
    status, error = client.call("api/record/stop", {})
    assert status == 409 and "no cut" in error["error"]

    _, state = client.call("api/state")
    assert state["next"] == "run"
    assert state["rounds"][0]["has_recording"] and not state["rounds"][0]["analysed"]
    assert state["recorder"]["state"] == "idle"

    mic.silent = False
    assert run_round(client, 1)["rounds"][0]["analysed"]


def test_aborted_job_still_yields_results_and_a_cleanup_next_round(served):
    client, mic, _ = served
    client.call("api/plan", {})
    mic.abort_in_pass = 9
    state = run_round(client, 1)
    statuses = [p["status"] for p in state["rounds"][0]["passes"]]
    assert statuses[9] == "aborted" and set(statuses[10:]) == {"missing"}

    mic.abort_in_pass = None
    _, state = client.call("api/plan", {"stock": "same"})
    kinds = [p["kind"] for p in state["rounds"][1]["passes"]]
    assert "cleanup" in kinds


def test_requests_out_of_order_are_refused(served):
    client, _, _ = served
    assert client.call("api/record/stop", {})[0] == 409
    assert client.call("api/record/start", {"round": 3})[0] == 404
    client.call("api/plan", {})
    assert client.call("api/plan", {})[0] == 409  # round 1 not analysed yet
    client.call("api/record/start", {"round": 1})
    assert client.call("api/record/start", {"round": 1})[0] == 409
