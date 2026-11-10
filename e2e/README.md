# cncjs end-to-end harness (WSL)

Real cncjs 1.11.x talking to a simulated Grbl 1.1h on a pty, so the dechatter widget can be driven without a machine. Everything runs inside WSL; installs, logs and state live in `~/.cache/dechatter-e2e/` (private Node 22 and cncjs are installed on the first `start.sh`).

- Start: `e2e/start.sh [--speed N] [--mount /dechatter:http://127.0.0.1:8765/]`. This prints the URL (`http://localhost:8000/`, also reachable from Windows) and the port path (`~/.cache/dechatter-e2e/ttyGRBL`, listed in cncjs's port dropdown). Use controller `Grbl` at 115200.
- Smoke test: `node e2e/drive.mjs program.nc` signs in like the web app (`POST /api/signin` with an empty token, no users configured), opens the port, loads and starts the file, and exits 0 when the workflow goes `running` and then back to `idle` with received == total.
- Stop: `e2e/stop.sh` stops cncjs and the simulator. Logs are in `~/.cache/dechatter-e2e/logs/` (`fake_grbl.log` records every line and reply with ms timestamps).

Simulator (`fake_grbl.py`, stdlib Python):
- Prints the banner `Grbl 1.1h ['$' for help]` when the port opens and after Ctrl-X. Supports realtime `?` (`Idle`/`Run`/`Hold:0` with MPos, FS and WCO), `!` and `~`.
- Answers `$`, `$$`, `$#`, `$G`, `$I`, `$N`, `$X`, `$C`, `$H` (homes to 0,0,0) and `$J=` jogs. Setting writes (`$n=v`) are accepted but ignored.
- G-code: G0/G1 (G2/G3 run as straight lines) with X/Y/Z/F, `G4 P<seconds>`, M3/M4/M5 S, M7/M8/M9, M2/M30, G20/G21, G90/G91, G17-19 and G94. Comments in `( )` and after `;` are stripped. Other words are accepted and ignored. A motion without a feed rate returns `error:22`.
- Timing runs in real time divided by `--speed`: G1 takes distance/F, G0 moves at `--rapid` (default 3000 mm/min), and G4 waits P seconds. Motions reply `ok` once they are queued in a 15-block planner. G4, M-codes and `$H` reply only after the planner has drained (buffer sync), and the status stays `Run` until all motion is finished.
- A feed hold (`!`) pauses both motion and dwell; a real Grbl would let a dwell keep counting down. Work offsets are always zero (G10/G92 are ignored).

To drive the widget: `uv run python e2e/serve_simulated.py <session-dir>` (on the host or in WSL) serves it at `http://127.0.0.1:8765/` with the simulated machine of `tests/synth.py` as the microphone. Enable the Custom widget in cncjs, set that URL, open the simulator port, then plan, load and press play. At `--speed 12` a round takes about 30 s.
