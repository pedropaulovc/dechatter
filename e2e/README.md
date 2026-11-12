# cncjs end-to-end harness (WSL)

Real cncjs 1.11.x talking to a simulated Grbl 1.1h on a pty, so the dechatter widget can be driven without a machine. Everything runs inside WSL; installs, logs and state live in `~/.cache/dechatter-e2e/` (private Node 22 and cncjs are installed on the first `start.sh`).

- Start: `e2e/start.sh [--speed N] [--mount /dechatter:<widget folder>]`. This prints the URL (`http://localhost:8000/`, also reachable from Windows) and the port path (`~/.cache/dechatter-e2e/ttyGRBL`, listed in cncjs's port dropdown). Use controller `Grbl` at 115200.
- Stepper test: `uv run python e2e/round_steps.py <session> <round> [mm|in] > blocks.json`, then `node e2e/step.mjs blocks.json --speed N` runs that round's blocks through cncjs with the widget's own stepper (continuing each manual-spindle stop after 1 s) and exits 0 with PASS when the round ran to its end, no `M0` was sent, and every cut window lasted as long as its move. With `--stop-in-cut K` it presses Stop during the K-th cut instead, 300 ms into it, or with `--stop-at send` right after its move is sent (before cncjs reports anything new), and passes when the run stopped, a feed hold was written to the controller, the last change of state or position cncjs reported between that hold and the reset left the machine at Hold:0 or Idle and came at least 0.9 s before the reset, no `$X` was written, and the controller is not in alarm. It watches `serialport:write`, so it sees what reached the controller from any sender.
- Smoke test: `node e2e/drive.mjs program.nc` signs in like the web app (`POST /api/signin` with an empty token, no users configured), opens the port, loads and starts the file, and exits 0 when the workflow goes `running` and then back to `idle` with received == total.
- Stop: `e2e/stop.sh` stops cncjs and the simulator. Logs are in `~/.cache/dechatter-e2e/logs/` (`fake_grbl.log` records every line and reply with ms timestamps).

Simulator (`fake_grbl.py`, stdlib Python):
- Prints the banner `Grbl 1.1h ['$' for help]` when the port opens and after Ctrl-X. Supports realtime `?` (`Idle`/`Run`/`Hold:0` with MPos, FS and WCO), `!` and `~`.
- Answers `$`, `$$`, `$#`, `$G`, `$I`, `$N`, `$X`, `$C`, `$H` (homes to 0,0,0) and `$J=` jogs. Setting writes (`$n=v`) are accepted but ignored.
- G-code: G0/G1 (G2/G3 run as straight lines) with X/Y/Z/F, `G4 P<seconds>`, M3/M4/M5 S, M7/M8/M9, M2/M30, G20/G21, G90/G91, G17-19 and G94. Comments in `( )` and after `;` are stripped. Other words are accepted and ignored. A motion without a feed rate returns `error:22`.
- Timing runs in real time divided by `--speed`: G1 takes distance/F, G0 moves at `--rapid` (default 3000 mm/min), and G4 waits P seconds. Motions reply `ok` once they are queued in a 15-block planner. G4, M-codes and `$H` reply only after the planner has drained (buffer sync), and the status stays `Run` until all motion is finished.
- A feed hold (`!`) pauses both motion and dwell; a real Grbl would let a dwell keep counting down. Work offsets are always zero (G10/G92 are ignored).

To drive the widget:

1. `uv run dechatter widget <dir>`, then `e2e/start.sh --speed 1 --mount /dechatter:<dir as a WSL path>`. Keep the speed at 1: the round's audio plays in real time, so the job has to last as long.
2. Open `http://localhost:8000/` in a Playwright-driven Chrome with `fake_mic.js` added as an init script. It swaps the widget's microphone for a stream the test can play a WAV into. Enable the Custom widget with URL `/dechatter/` and open the simulator port.
3. Per round: plan, press Export, run `uv run python e2e/round_audio.py <export.zip> <dir>/e2e/round.wav` to get the sound of the simulated machine (`tests/synth.py`) cutting that plan, press Start in the widget, and call `window.fakeMicPlay('/dechatter/e2e/round.wav')` in the widget's frame.

Round 1 takes about five minutes. Without the init script the widget records the PC's real microphone, which is enough to check capture but hears no cuts.
