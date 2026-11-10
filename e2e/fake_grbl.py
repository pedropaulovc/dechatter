#!/usr/bin/env python3
"""Grbl 1.1h simulator on a pseudo-terminal, good enough for cncjs's Grbl controller.

Stdlib only (Python 3.12, Linux). Creates a pty, optionally symlinks its slave to
--link, and prints the Grbl banner whenever a client opens the port or sends a soft
reset (0x18). Motion and dwell run in (scaled) real time:

  G1 time = distance / F, G0 time = distance / rapid rate, G4 time = P seconds,
  all divided by --speed.

Plain motion lines reply `ok` once queued in a 15-block planner; G4, M3/M4/M5,
M7/M8/M9, M2/M30 and $H first wait for the planner to drain (Grbl's buffer sync).
Realtime: `?` status, `!` feed hold, `~` cycle start, 0x18 soft reset.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass

BANNER = "Grbl 1.1h ['$' for help]"
PLANNER_BLOCKS = 15
RX_BUFFER = 128
TICK = 0.01
INCH = 25.4

SETTINGS = [
    (0, "10"),
    (1, "25"),
    (2, "0"),
    (3, "0"),
    (4, "0"),
    (5, "0"),
    (6, "0"),
    (10, "1"),
    (11, "0.010"),
    (12, "0.002"),
    (13, "0"),
    (20, "0"),
    (21, "0"),
    (22, "0"),
    (23, "0"),
    (24, "25.000"),
    (25, "500.000"),
    (26, "250"),
    (27, "1.000"),
    (30, "24000"),
    (31, "0"),
    (32, "0"),
    (100, "800.000"),
    (101, "800.000"),
    (102, "800.000"),
    (110, "3000.000"),
    (111, "3000.000"),
    (112, "3000.000"),
    (120, "100.000"),
    (121, "100.000"),
    (122, "100.000"),
    (130, "300.000"),
    (131, "200.000"),
    (132, "100.000"),
]

WORD = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")
COMMENT = re.compile(r"\([^)]*\)")


@dataclass
class Block:
    kind: str  # "move" or "dwell"
    target: tuple[float, float, float] = (0.0, 0.0, 0.0)
    feed: float = 0.0  # mm/min actually used for the move
    seconds: float = 0.0  # dwell length (unscaled)


def fmt(v: float) -> str:
    return f"{v:.3f}"


class Grbl:
    def __init__(self, fd: int, speed: float, rapid: float, verbose: bool):
        self.fd = fd
        self.speed = speed
        self.rapid = rapid
        self.verbose = verbose
        self.write_lock = threading.Lock()
        self.cv = threading.Condition()
        self.lines: deque[str] = deque()  # serial RX buffer (complete lines)
        self.planner: deque[Block] = deque()
        self.generation = 0  # bumped on reset; aborts waiters
        self.hold = False
        self.executing = False  # a block is being executed
        self.pos = [0.0, 0.0, 0.0]
        self.cur_feed = 0.0
        self.reset_modal()

    # ---- state ------------------------------------------------------------
    def reset_modal(self) -> None:
        self.motion = "G0"
        self.units_mm = True
        self.absolute = True
        self.feed = 0.0
        self.spindle = "M5"
        self.rpm = 0.0
        self.coolant = "M9"
        self.plane = "G17"

    def log(self, msg: str) -> None:
        if self.verbose:
            now = time.time()
            stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
            print(f"{stamp} {msg}", flush=True)

    def send(self, text: str) -> None:
        data = (text + "\r\n").encode()
        with self.write_lock:
            try:
                os.write(self.fd, data)
            except OSError:
                pass  # nobody attached; output is dropped like on a closed port
        if not text.startswith("<"):
            self.log(f"> {text}")

    def soft_reset(self, reason: str) -> None:
        with self.cv:
            self.generation += 1
            self.lines.clear()
            self.planner.clear()
            self.hold = False
            self.executing = False
            self.cur_feed = 0.0
            self.reset_modal()
            self.cv.notify_all()
        self.log(f"reset ({reason})")
        self.send("")
        self.send(BANNER)

    def busy(self) -> bool:
        return self.executing or bool(self.planner)

    def status(self) -> str:
        with self.cv:
            if self.hold:
                state = "Hold:0"
            elif self.busy():
                state = "Run"
            else:
                state = "Idle"
            feed = self.cur_feed if state == "Run" else 0.0
            rpm = self.rpm if self.spindle != "M5" else 0.0
            mpos = ",".join(fmt(v) for v in self.pos)
        return f"<{state}|MPos:{mpos}|FS:{feed:.0f},{rpm:.0f}|WCO:0.000,0.000,0.000>"

    # ---- serial input -----------------------------------------------------
    def feed_bytes(self, data: bytes, partial: bytearray) -> None:
        for b in data:
            if b == 0x3F:  # ?
                self.send(self.status())
            elif b == 0x21:  # !
                with self.cv:
                    self.hold = True
                    self.cv.notify_all()
                self.log("feed hold")
            elif b == 0x7E:  # ~
                with self.cv:
                    self.hold = False
                    self.cv.notify_all()
                self.log("cycle start")
            elif b == 0x18:  # ctrl-x
                partial.clear()
                self.soft_reset("0x18")
            elif b >= 0x80:  # jog cancel, overrides, etc.: accepted, ignored
                pass
            elif b in (0x0A, 0x0D):
                if partial:
                    line = partial.decode(errors="replace")
                    partial.clear()
                    with self.cv:
                        self.lines.append(line)
                        self.cv.notify_all()
            elif len(partial) < RX_BUFFER * 2:
                partial.append(b)

    # ---- line processing (one line at a time, like Grbl's protocol loop) --
    def line_loop(self) -> None:
        while True:
            with self.cv:
                while not self.lines:
                    self.cv.wait()
                line = self.lines.popleft()
                gen = self.generation
            self.log(f"< {line}")
            try:
                reply = self.execute_line(line, gen)
            except _Aborted:
                continue
            if reply is not None:
                self.send(reply)

    def wait_sync(self, gen: int) -> None:
        """protocol_buffer_synchronize(): block until planner empty and idle."""
        with self.cv:
            while self.busy():
                self.cv.wait()
                if self.generation != gen:
                    raise _Aborted
            if self.generation != gen:
                raise _Aborted

    def enqueue(self, block: Block, gen: int) -> None:
        with self.cv:
            while len(self.planner) >= PLANNER_BLOCKS:
                self.cv.wait()
                if self.generation != gen:
                    raise _Aborted
            if self.generation != gen:
                raise _Aborted
            self.planner.append(block)
            self.cv.notify_all()

    def execute_line(self, raw: str, gen: int) -> str | None:
        line = COMMENT.sub("", raw).split(";", 1)[0]
        line = "".join(line.split()).upper()
        if not line:
            return "ok"
        if line.startswith("$"):
            return self.dollar(line, gen)
        return self.gcode(line, gen, jog=False)

    def dollar(self, line: str, gen: int) -> str:
        if line == "$":
            self.send("[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]")
        elif line == "$$":
            for num, val in SETTINGS:
                self.send(f"${num}={val}")
        elif line == "$#":
            for name in ("G54", "G55", "G56", "G57", "G58", "G59", "G28", "G30", "G92"):
                self.send(f"[{name}:0.000,0.000,0.000]")
            self.send("[TLO:0.000]")
            self.send("[PRB:0.000,0.000,0.000:0]")
        elif line == "$G":
            self.send(f"[GC:{self.modal_string()}]")
        elif line == "$I":
            self.send("[VER:1.1h.20190825:]")
            self.send(f"[OPT:V,{PLANNER_BLOCKS},{RX_BUFFER}]")
        elif line == "$N":
            self.send("$N0=")
            self.send("$N1=")
        elif line == "$X":
            self.send("[MSG:Caution: Unlocked]")
        elif line == "$H":
            self.wait_sync(gen)
            self.enqueue(Block("move", (0.0, 0.0, 0.0), self.rapid), gen)
            self.wait_sync(gen)
        elif line == "$C":
            self.send("[MSG:Enabled]")
        elif line.startswith("$J="):
            return self.gcode(line[3:], gen, jog=True)
        elif re.fullmatch(r"\$N?\d+=.*|\$SLP|\$RST=[$#*]", line):
            pass  # settings writes / sleep / restore: accepted, not modelled
        else:
            return "error:3"
        return "ok"

    def modal_string(self) -> str:
        return " ".join(
            [
                self.motion,
                "G54",
                self.plane,
                "G21" if self.units_mm else "G20",
                "G90" if self.absolute else "G91",
                "G94",
                "G49",
                self.spindle,
                self.coolant,
                "T0",
                f"F{self.feed:g}",
                f"S{self.rpm:g}",
            ]
        )

    def gcode(self, line: str, gen: int, jog: bool) -> str:
        words = WORD.findall(line)
        if "".join(letter + value for letter, value in words) != line:
            return "error:1"  # stray characters / malformed words (also N/checksum)
        scale = 1.0 if self.units_mm else INCH
        absolute = self.absolute and not jog
        motion = self.motion if not jog else "G1"
        axes: dict[str, float] = {}
        dwell = None
        sync_m: list[str] = []
        feed = None
        for letter, value in words:
            num = float(value)
            if letter == "G":
                code = f"G{num:g}"
                if code in ("G0", "G1", "G2", "G3"):
                    motion = code
                elif code == "G4":
                    dwell = 0.0
                elif code in ("G20", "G21"):
                    self.units_mm = code == "G21"
                    scale = 1.0 if self.units_mm else INCH
                elif code in ("G90", "G91"):
                    absolute = code == "G90"
                    if not jog:
                        self.absolute = absolute
                elif code in ("G17", "G18", "G19"):
                    self.plane = code
                # G53/G54..G59/G93/G94/G40/G49/G80 etc.: accepted, not modelled
            elif letter == "M":
                sync_m.append(f"M{num:g}")
            elif letter in "XYZ":
                axes[letter] = num
            elif letter == "F":
                feed = num
            elif letter == "S":
                self.rpm = num
            elif letter == "P" and dwell is not None:
                dwell = num
            # I/J/K/R/T/N/L/P: accepted, not modelled
        if feed is not None:
            feed *= scale
            if not jog:
                self.feed = feed
        if not jog:
            self.motion = motion

        for m in sync_m:
            self.wait_sync(gen)
            if m in ("M3", "M4", "M5"):
                self.spindle = m
            elif m in ("M7", "M8", "M9"):
                self.coolant = m
            elif m in ("M2", "M30"):
                self.spindle, self.coolant = "M5", "M9"
                # Grbl's program end resets motion, plane, distance and feed mode; not units.
                self.motion, self.absolute = "G1", True
                self.log("program end")

        if dwell is not None:
            self.wait_sync(gen)
            self.enqueue(Block("dwell", seconds=dwell), gen)
            self.wait_sync(gen)

        if axes:
            with self.cv:  # moves start where the last queued move ends
                moves = [b for b in self.planner if b.kind == "move"]
                target = list(moves[-1].target) if moves else list(self.pos)
            for i, axis in enumerate("XYZ"):
                if axis in axes:
                    v = axes[axis] * scale
                    target[i] = v if absolute else target[i] + v
            if motion == "G0" and not jog:
                rate = self.rapid
            else:
                rate = feed if jog and feed is not None else self.feed
                if rate <= 0:
                    return "error:22"  # undefined feed rate
            self.enqueue(Block("move", tuple(target), rate), gen)
        return "ok"

    # ---- motion executor ---------------------------------------------------
    def run_loop(self) -> None:
        while True:
            with self.cv:
                while not self.planner:
                    self.cv.wait()
                block = self.planner[0]
                gen = self.generation
                self.executing = True
                start = tuple(self.pos)
            if block.kind == "move":
                dist = math.dist(start, block.target)
                duration = dist / block.feed * 60.0 if dist else 0.0
            else:
                duration = block.seconds
            duration /= self.speed
            elapsed = 0.0
            last = time.monotonic()
            while True:
                time.sleep(TICK)
                now = time.monotonic()
                with self.cv:
                    if self.generation != gen:
                        break
                    if not self.hold:
                        elapsed += now - last
                    last = now
                    frac = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
                    if block.kind == "move":
                        self.pos = [
                            a + (b - a) * frac for a, b in zip(start, block.target, strict=True)
                        ]
                        self.cur_feed = 0.0 if self.hold else block.feed
                    else:
                        self.cur_feed = 0.0
                    if frac >= 1.0:
                        self.planner.popleft()
                        self.executing = False
                        self.cur_feed = 0.0
                        self.cv.notify_all()
                        break


class _Aborted(Exception):
    """Raised in the line thread when a soft reset invalidates the pending line."""


def slave_attached(master: int) -> bool:
    """True while some process holds the pty slave open (no POLLHUP on master)."""
    p = select.poll()
    p.register(master, select.POLLIN)
    return not any(ev & select.POLLHUP for _, ev in p.poll(0))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--speed", type=float, default=1.0, help="divide all durations by N")
    ap.add_argument("--link", help="create this symlink to the pty slave")
    ap.add_argument("--rapid", type=float, default=3000.0, help="G0 rate, mm/min")
    ap.add_argument("--quiet", action="store_true", help="do not log traffic to stdout")
    args = ap.parse_args()
    if args.speed <= 0:
        ap.error("--speed must be > 0")

    def on_sigterm(*_: object) -> None:  # stop.sh: unwind like Ctrl-C so --link is removed
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)

    master, slave = os.openpty()
    tty.setraw(slave)  # no echo / CRLF translation on the slave side
    attrs = termios.tcgetattr(slave)
    attrs[4] = attrs[5] = termios.B115200
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    slave_path = os.ttyname(slave)
    os.close(slave)  # so POLLHUP tells us when a client attaches

    if args.link:
        link = os.path.expanduser(args.link)
        os.makedirs(os.path.dirname(os.path.abspath(link)), exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(slave_path, link)
    print(
        f"fake Grbl on {slave_path}"
        + (f" (link {args.link})" if args.link else "")
        + f", speed x{args.speed:g}, rapid {args.rapid:g} mm/min",
        flush=True,
    )

    grbl = Grbl(master, args.speed, args.rapid, verbose=not args.quiet)
    threading.Thread(target=grbl.line_loop, daemon=True).start()
    threading.Thread(target=grbl.run_loop, daemon=True).start()

    attached = False
    partial = bytearray()
    poller = select.poll()
    poller.register(master, select.POLLIN)
    try:
        while True:
            now_attached = slave_attached(master)
            if now_attached != attached:
                attached = now_attached
                if attached:
                    # Like an Arduino's DTR reset: let the client finish configuring, then boot.
                    time.sleep(0.2)
                    termios.tcflush(master, termios.TCIOFLUSH)
                    partial.clear()
                    grbl.log("client attached")
                    grbl.soft_reset("open")
                else:
                    grbl.log("client detached")
            if not attached:
                time.sleep(0.1)
                continue
            for _, ev in poller.poll(100):
                if ev & select.POLLIN:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        continue
                    grbl.feed_bytes(data, partial)
    except KeyboardInterrupt:
        pass
    finally:
        if args.link and os.path.islink(os.path.expanduser(args.link)):
            os.unlink(os.path.expanduser(args.link))
    return 0


if __name__ == "__main__":
    sys.exit(main())
