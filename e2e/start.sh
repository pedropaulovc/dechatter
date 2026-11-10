#!/usr/bin/env bash
# Start the dechatter e2e harness inside WSL: fake Grbl on a pty + cncjs 1.11.x.
# Usage: e2e/start.sh [--speed N] [--mount ROUTE:TARGET]...
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${DECHATTER_E2E_CACHE:-$HOME/.cache/dechatter-e2e}"
NODE_MAJOR=22                 # cncjs 1.11 declares node >=18; 22 LTS runs serialport 10 prebuilds
CNCJS_SPEC="cncjs@~1.11.0"
HTTP_PORT=8000
SPEED=1
MOUNTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --speed) SPEED="$2"; shift 2 ;;
    --speed=*) SPEED="${1#*=}"; shift ;;
    --mount) MOUNTS+=(--mount "$2"); shift 2 ;;
    --mount=*) MOUNTS+=(--mount "${1#*=}"); shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "start.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$CACHE/home" "$CACHE/logs"
PORT_LINK="$CACHE/ttyGRBL"
CNCRC="$CACHE/cncrc.json"

if [[ -e "$CACHE/cncjs.pid" ]] && kill -0 "$(cat "$CACHE/cncjs.pid")" 2>/dev/null; then
  echo "start.sh: harness already running (run e2e/stop.sh first)" >&2
  exit 1
fi

# --- Node (private copy; WSL PATH may leak Windows node/npm) -----------------
NODE_HOME="$CACHE/node"
if [[ ! -x "$NODE_HOME/bin/node" ]]; then
  tarball="$(curl -fsSL "https://nodejs.org/dist/latest-v${NODE_MAJOR}.x/SHASUMS256.txt" \
             | awk '/linux-x64\.tar\.gz$/ {print $2}')"
  echo "start.sh: installing $tarball into $NODE_HOME"
  rm -rf "$NODE_HOME.tmp" && mkdir -p "$NODE_HOME.tmp"
  curl -fsSL "https://nodejs.org/dist/latest-v${NODE_MAJOR}.x/$tarball" \
    | tar -xz -C "$NODE_HOME.tmp" --strip-components=1
  mv "$NODE_HOME.tmp" "$NODE_HOME"
fi
export PATH="$NODE_HOME/bin:/usr/local/bin:/usr/bin:/bin"

# --- cncjs ------------------------------------------------------------------
CNCJS_PREFIX="$CACHE/cncjs"
CNCJS_BIN="$CNCJS_PREFIX/node_modules/.bin/cncjs"
if [[ ! -x "$CNCJS_BIN" ]]; then
  echo "start.sh: installing $CNCJS_SPEC into $CNCJS_PREFIX (log: $CACHE/logs/npm-install.log)"
  mkdir -p "$CNCJS_PREFIX"
  [[ -f "$CNCJS_PREFIX/package.json" ]] || echo '{"private":true}' > "$CNCJS_PREFIX/package.json"
  npm install --prefix "$CNCJS_PREFIX" --no-audit --no-fund "$CNCJS_SPEC" \
    > "$CACHE/logs/npm-install.log" 2>&1 \
    || { tail -30 "$CACHE/logs/npm-install.log" >&2; exit 1; }
fi

# --- cncrc: simulator port listed, no users ---------------------------------
if [[ -f "$CNCRC" ]]; then
  # Keep cncjs's persisted UI state, but force the harness-owned keys.
  node -e '
    const fs = require("fs"); const [f, port] = process.argv.slice(1);
    const c = JSON.parse(fs.readFileSync(f, "utf8"));
    c.ports = [{ path: port, manufacturer: "dechatter fake Grbl" }];
    c.users = [];
    fs.writeFileSync(f, JSON.stringify(c, null, 2));
  ' "$CNCRC" "$PORT_LINK"
else
  cat > "$CNCRC" <<EOF
{
  "ports": [{ "path": "$PORT_LINK", "manufacturer": "dechatter fake Grbl" }],
  "baudrates": [115200],
  "users": [],
  "watchDirectory": "",
  "accessTokenLifetime": "30d",
  "allowRemoteAccess": false,
  "state": { "checkForUpdates": false }
}
EOF
fi

# --- simulator --------------------------------------------------------------
rm -f "$PORT_LINK"
setsid nohup /usr/bin/python3 "$HERE/fake_grbl.py" --speed "$SPEED" --link "$PORT_LINK" \
  > "$CACHE/logs/fake_grbl.log" 2>&1 < /dev/null &
echo $! > "$CACHE/fake_grbl.pid"
for _ in $(seq 50); do [[ -L "$PORT_LINK" ]] && break; sleep 0.1; done
if [[ ! -L "$PORT_LINK" ]]; then
  echo "start.sh: simulator did not create $PORT_LINK" >&2
  cat "$CACHE/logs/fake_grbl.log" >&2
  exit 1
fi

# --- cncjs ------------------------------------------------------------------
# HOME is redirected so cncjs's sessions/state stay inside the cache dir.
HOME="$CACHE/home" setsid nohup "$CNCJS_BIN" --host 0.0.0.0 --port "$HTTP_PORT" \
  --config "$CNCRC" "${MOUNTS[@]}" \
  > "$CACHE/logs/cncjs.log" 2>&1 < /dev/null &
echo $! > "$CACHE/cncjs.pid"

for _ in $(seq 120); do
  if curl -fs -o /dev/null "http://localhost:$HTTP_PORT/"; then
    echo "cncjs $("$CNCJS_BIN" --version 2>/dev/null) on node $(node -v), simulator speed x$SPEED"
    echo "URL:  http://localhost:$HTTP_PORT/"
    echo "Port: $PORT_LINK -> $(readlink "$PORT_LINK")"
    echo "Logs: $CACHE/logs/"
    exit 0
  fi
  if ! kill -0 "$(cat "$CACHE/cncjs.pid")" 2>/dev/null; then break; fi
  sleep 0.5
done
echo "start.sh: cncjs did not answer on port $HTTP_PORT" >&2
tail -30 "$CACHE/logs/cncjs.log" >&2
"$HERE/stop.sh" >/dev/null 2>&1 || true
exit 1
