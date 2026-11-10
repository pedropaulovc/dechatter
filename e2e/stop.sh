#!/usr/bin/env bash
# Stop the dechatter e2e harness (cncjs + fake Grbl) started by e2e/start.sh.
set -uo pipefail

CACHE="${DECHATTER_E2E_CACHE:-$HOME/.cache/dechatter-e2e}"

stop_pid() {  # name pidfile
  local name="$1" pidfile="$2" pid
  [[ -f "$pidfile" ]] || return 0
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    for _ in $(seq 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -0 "$pid" 2>/dev/null && kill -KILL -- "-$pid" 2>/dev/null
    echo "stopped $name (pid $pid)"
  fi
  rm -f "$pidfile"
}

stop_pid cncjs "$CACHE/cncjs.pid"
stop_pid fake_grbl "$CACHE/fake_grbl.pid"
# Strays from a lost pidfile: only processes launched from this harness's paths.
pkill -f "$CACHE/cncjs/node_modules/.bin/cncjs" 2>/dev/null && echo "killed stray cncjs"
pkill -f "e2e/fake_grbl.py --speed" 2>/dev/null && echo "killed stray fake_grbl"
rm -f "$CACHE/ttyGRBL"
exit 0
