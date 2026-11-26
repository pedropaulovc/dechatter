"""Where lanes meet: what a pass engages at the ends of its lane (#63), and which lane a
round starts in (#64).

The load is simulated on the stock: a slice through it just above the passes' bottom, on a
grid, from which every cutting move of the program takes what its cutter sweeps. A pass's
load is the area it removes per millimetre of feed, which is its radial engagement (a cut
`w` wide removes `w` per millimetre). It must stay at the pass's width everywhere: through
the pass, where it starts and where it stops. A slice holds for the whole depth: every
pass of these rounds goes the same depth, and in a round that steps the depth, the passes
reaching a slice are the deepest ones, which come first.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from dechatter import gcode, session
from dechatter.browser import Sessions
from dechatter.config import Direction, load_setup
from dechatter.detect import Thresholds, Verdict
from dechatter.plan import (
    Face,
    Milling,
    PlannerKind,
    assemble,
    cutting,
    envelope,
    grid_tests,
)
from dechatter.session import PassResult, PassStatus, Results

FIXTURE = Path(__file__).parent / "fixtures" / "issue3427"
HX, HY = 0.05, 0.02  # the grid along X and Y, mm
STEP_MM = 0.05  # how far the cutter moves between looks at what it removed
TOLERANCE_MM = 0.06  # the grid's own error on a load


class Slice:
    """A slice of the stock at one depth, on a grid; the cutter takes what it sweeps."""

    def __init__(self, length_mm: float, thickness_mm: float, depth_mm: float, radius: float):
        self.xs = (np.arange(round(length_mm / HX)) + 0.5) * HX
        self.ys = (np.arange(round(thickness_mm / HY)) + 0.5) * HY
        self.stock = np.ones((self.xs.size, self.ys.size), bool)
        self.depth, self.radius = depth_mm, radius

    def take(self, x: float, y: float) -> float:
        """Remove what the cutter centred at (x, y) holds; the area removed, mm²."""
        r = self.radius
        ci = np.nonzero(np.abs(self.xs - x) <= r)[0]
        ri = np.nonzero(np.abs(self.ys - y) <= r)[0]
        if not ci.size or not ri.size:
            return 0.0
        inside = (self.xs[ci, None] - x) ** 2 + (self.ys[None, ri] - y) ** 2 <= r * r
        held = self.stock[np.ix_(ci, ri)]
        taken = held & inside
        self.stock[np.ix_(ci, ri)] = held & ~inside
        return float(taken.sum()) * HX * HY

    def move(self, start: tuple[float, float], end: tuple[float, float]) -> np.ndarray:
        """Sweep the cutter from `start` to `end`: the area removed per mm at each step."""
        (x0, y0), (x1, y1) = start, end
        length = float(np.hypot(x1 - x0, y1 - y0))
        steps = max(1, int(np.ceil(length / STEP_MM)))
        return np.array(
            [
                self.take(x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps) / (length / steps)
                for k in range(1, steps + 1)
            ]
        )


def cut(stock: Slice, plan, setup) -> dict[int, float]:
    """Run the plan's program on the slice: the most area per mm each pass removed. Rapids
    and a pass's plunge, all in the air, must remove nothing; a relief row's plunge takes
    what the row before left of its level."""
    at = {"x": 0.0, "y": -50.0, "z": 50.0}
    loads: dict[int, float] = {}
    for block in gcode.blocks(plan, setup, gcode.Units.MM):
        for line in block.lines:
            words = line.split()
            code, axes = words[0], {w[0].lower(): float(w[1:]) for w in words[1:] if w[0] in "XYZ"}
            if code not in ("G0", "G1") or not axes:
                continue
            target = {**at, **axes}
            below = target["z"] <= -stock.depth
            if below and at["z"] > -stock.depth:  # a plunge into the slice
                taken = stock.take(target["x"], target["y"])
                assert block.pass_id is None or taken == 0, f"pass {block.pass_id} plunges in stock"
            elif below:
                load = stock.move((at["x"], at["y"]), (target["x"], target["y"]))
                if code == "G0":
                    assert not load.any(), f"a rapid ({block.step}) cuts the stock"
                # A pass's moves under its id; the rows milled between passes under None.
                loads[block.pass_id] = max(loads.get(block.pass_id, 0.0), float(load.max()))
            at = target
    return loads


def assert_within_width(plan, loads: dict[int | None, float], setup) -> None:
    """Every pass removes no more per mm than its width; every row milled between passes
    (a relief's) no more than the row step, `face_width_mm`."""
    heavy = {
        p.id: (p.width_mm, round(loads[p.id], 2))
        for p in plan.passes
        if loads[p.id] > p.width_mm + TOLERANCE_MM
    }
    assert not heavy, f"passes removing more per mm than their width (width, load): {heavy}"
    step = setup.program.face_width_mm or setup.tool.diameter_mm / 4
    assert loads.get(None, 0.0) <= step + TOLERANCE_MM, "a relief row cuts wider than its step"


@pytest.fixture
def deep():
    """harmonic-analyzer#3427's setup: a 1/2 in cutter 13.5 mm deep in 1018, two lanes."""
    return load_setup(FIXTURE / "setup.toml")


def ran(plan) -> session.Round:
    """The plan, every pass run and heard."""
    passes = tuple(
        PassResult(p.id, p.kind, p.rpm, p.width_mm, PassStatus.ANALYSED, verdict=Verdict.STABLE)
        for p in plan.passes
    )
    return session.Round(
        Path(f"round-{plan.round:02d}"), plan, Results(plan.round, "", Thresholds(), passes)
    )


def test_no_lane_pass_engages_more_than_its_width(deep):
    """Round 1 of #3427 (widths 0.2, 0.2, 0.4, 0.8, ... at 13.5 mm): on 0.8.0 pass 12 ends
    removing 5.6 mm² per mm, a cut as wide as the face is deep. Each pass now ends in a
    relief milled where the lanes meet, and the second lane's passes start in it."""
    env = envelope(deep)
    plan = assemble(deep, env, 1, PlannerKind.GRID, grid_tests(deep, env), lanes=2)
    assert [p.width_mm for p in plan.passes[:4]] == [0.2, 0.2, 0.4, 0.8]
    stock = Slice(deep.stock.length_mm, deep.stock.thickness_mm, 13.49, deep.tool.diameter_mm / 2)
    assert_within_width(plan, cut(stock, plan, deep), deep)


def test_0_8_0_round_1_ends_every_pass_in_a_heavy_cut(deep):
    """The program 0.8.0 planned for #3427's round 1, as it still renders: its passes stop in
    the stock, and near the stop each engages up to its face's depth."""
    from dechatter.plan import LaneEnd

    plan = session.load_rounds(FIXTURE)[0].plan
    assert plan.lane_end is LaneEnd.STOP
    stock = Slice(deep.stock.length_mm, deep.stock.thickness_mm, 13.49, deep.tool.diameter_mm / 2)
    loads = cut(stock, plan, session.as_planned(session.load_rounds(FIXTURE)[0]))
    assert loads[12] > 5.0, "pass 12, 0.8 mm wide, ends as wide as the face is deep"
    assert loads[12] < 6.0


def test_follow_on_rounds_and_a_climb_check_stay_within_width_at_both_ends(deep, tmp_path):
    """Round 1 in lane 1, a follow-on round in lane 2 (its passes start in the relief round 1
    milled, extended further back first), then a climb check, whose lanes count from the
    right, in X0-63 (its passes start in the relief and run out past X0): every pass of each,
    where it starts, along the lane and where it ends."""
    env = envelope(deep)
    stock = Slice(deep.stock.length_mm, deep.stock.thickness_mm, 13.49, deep.tool.diameter_mm / 2)
    first = assemble(deep, env, 1, PlannerKind.GRID, grid_tests(deep, env), lanes=2)
    assert_within_width(first, cut(stock, first, deep), deep)
    rounds = [ran(first)]
    tests = [(2046.0, w, 0.9) for w in (1.4, 1.9, 2.8, 1.8, 2.6, 1.5)]
    left = session._stock_left(rounds, Direction.CONVENTIONAL, Milling.SIDE)
    second = assemble(
        deep,
        env,
        2,
        PlannerKind.BAYES,
        tests,
        face=left.face,
        made=left.made,
        lanes=2,
        reliefs=left.reliefs,
        pick=True,
    )
    assert [leg.lane for leg in second.legs] == [2], "lane 2, fresh, has the most stock left"
    (extended,) = second.legs[0].reliefs
    assert (extended.done_y_mm, extended.y_mm) == (7.2, second.legs[0].face_y_end_mm)
    assert_within_width(second, cut(stock, second, deep), deep)
    rounds.append(ran(second))
    climb = cutting(deep, Direction.CLIMB)
    check = [(2046.0, w, None) for w in (0.4, 0.6, 0.8, 0.9)]
    left = session._stock_left(rounds, Direction.CLIMB, Milling.SIDE)
    third = assemble(
        climb,
        env,
        3,
        PlannerKind.CHECK,
        check,
        face=left.face,
        made=left.made,
        lanes=2,
        reliefs=left.reliefs,
        pick=True,
    )
    assert [leg.lane for leg in third.legs] == [2], "X0-63: 5.8 mm left, X63-126 0.8 mm"
    assert third.legs[0].reliefs == (), "the relief already reaches as far back"
    assert_within_width(third, cut(stock, third, climb), climb)


def test_a_new_round_after_0_8_0_ones_clears_the_steps_their_stops_left(deep):
    """#3427's round 1 as 0.8.0 cut it, stopping every pass in the stock, then a round of
    0.8.1 going on in lane 1 and into lane 2: the relief it mills first takes away the
    steps the stopped passes left, and no pass engages more than its width."""
    rounds = session.load_rounds(FIXTURE)[:1]
    stock = Slice(deep.stock.length_mm, deep.stock.thickness_mm, 13.49, deep.tool.diameter_mm / 2)
    cut(stock, rounds[0].plan, session.as_planned(rounds[0]))
    left = session._stock_left(rounds, Direction.CONVENTIONAL, Milling.SIDE)
    assert left.reliefs == ()
    tests = [(2046.0, w, 0.9) for w in (1.4, 1.9, 2.8, 1.8, 2.6)]
    lane_1 = Face(lane=1, y_mm=7.2)
    plan = assemble(
        deep, envelope(deep), 2, PlannerKind.BAYES, tests, face=lane_1, made=left.made, lanes=2
    )
    assert [leg.lane for leg in plan.legs] == [1, 2]
    assert plan.legs[0].reliefs[0].done_y_mm == 0.0, "milled from the front"
    assert_within_width(plan, cut(stock, plan, deep), deep)


def test_a_round_starts_in_the_lane_with_the_most_stock_left(tmp_path):
    """#64: after round 3's climb check cut X63-126 back to Y2.9, a conventional round went
    back to X0-63, cut back to Y10.7, and with width_mm lowered to 11 found no room at all.
    It now starts where 8.1 mm is left, and the unrun round 0.8.0 planned is discarded."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        for path in FIXTURE.rglob("*"):
            if path.is_file():
                z.write(path, f"issue3427/{path.relative_to(FIXTURE).as_posix()}")
    sessions = Sessions(tmp_path)
    name = sessions.import_zip(data.getvalue())
    for width, left in ((13.0, 10.1), (11.0, 8.1)):
        # The setup as it is, then width_mm lowered: the unrun round 4 goes with the change.
        values = sessions.setup_form(name)["values"]
        values["stock"]["width_mm"] = width
        sessions.save_setup(name, json.dumps(values))
        planned = sessions.plan(name, "same")["rounds"][-1]
        assert (planned["round"], planned["direction"]) == (4, "conventional")
        assert planned["legs"][0]["lane"] == 2 and planned["legs"][0]["lane_x_mm"] == [63, 126]
        assert planned["legs"][0]["face_y_start_mm"] == 2.9
        stock = {s["lane"]: s for s in planned["lane_stock"]}
        assert stock[1]["left_mm"] == pytest.approx(width - 10.7)
        assert stock[2] == {"lane": 2, "left_mm": left, "open": True}
        relief = planned["legs"][0]["reliefs"][0]
        assert (relief["boundary"], relief["x_mm"], relief["depth_mm"]) == (1, 63, 13.5)
        program = sessions.program(name, 4, "mm")
        assert "(STARTS IN lane 2 of 2, X63 to X126: of the lanes its first cut fits on" in program
        assert "(RELIEF: a pocket between lanes 1 and 2 at X63" in program
