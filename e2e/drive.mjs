#!/usr/bin/env node
// Headless cncjs client: open the fake Grbl port, load + start a G-code file, and
// exit 0 once the workflow returns to idle after running (with received == total).
// Usage: node e2e/drive.mjs <file.nc> [--url http://localhost:8000] [--port PATH]
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { basename, join } from 'node:path';

const cache = process.env.DECHATTER_E2E_CACHE || join(homedir(), '.cache', 'dechatter-e2e');
const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args.splice(i, 2)[1] : dflt;
};
const url = opt('--url', 'http://localhost:8000');
const port = opt('--port', join(cache, 'ttyGRBL'));
const timeoutSec = Number(opt('--timeout', '600'));
const file = args[0];
if (!file) {
  console.error('usage: node e2e/drive.mjs <file.nc> [--url URL] [--port PATH] [--timeout SEC]');
  process.exit(2);
}

// cncjs 1.11 serves socket.io 2.2; reuse the matching client from the cncjs install.
const require = createRequire(join(cache, 'cncjs', 'node_modules', 'cncjs', 'package.json'));
const io = require('socket.io-client');

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s]`, ...a);
const fail = (msg) => { log(`FAIL: ${msg}`); process.exit(1); };
setTimeout(() => fail(`timeout after ${timeoutSec}s`), timeoutSec * 1000).unref();

// Same as the cncjs web app with no users configured: sign in with an empty token.
const res = await fetch(`${url}/api/signin`, {
  method: 'POST',
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify({ token: '' }),
});
if (!res.ok) fail(`POST /api/signin -> HTTP ${res.status}`);
const { token, enabled } = await res.json();
log(`signin: enabled=${enabled}, token=${token.slice(0, 16)}...`);

const gcode = readFileSync(file, 'utf8');
const socket = io.connect(url, { query: `token=${token}`, transports: ['websocket'] });
let loaded = false;
let sawRunning = false;
let lastStatus = null;
let lastStatusLog = 0;

const fmtStatus = (s) => `sent=${s.sent} received=${s.received} total=${s.total}`;

socket.on('connect_error', (err) => fail(`socket connect_error: ${err.message || err}`));
socket.on('error', (err) => fail(`socket error: ${err.message || err}`));
socket.on('startup', ({ loadedControllers, ports }) => {
  log(`startup: controllers=${loadedControllers.join(',')} ports=${JSON.stringify(ports)}`);
  socket.emit('open', port, { controllerType: 'Grbl', baudrate: 115200 }, (err) => {
    if (err) fail(`open ${port}: ${err.message || err}`);
  });
});
socket.on('serialport:open', (o) => log(`serialport:open ${JSON.stringify(o)}`));
socket.on('serialport:error', (o) => fail(`serialport:error ${JSON.stringify(o)}`));

const loadAndStart = (why) => {
  if (loaded) return;
  loaded = true;
  log(`controller ready (${why}); gcode:load ${basename(file)} (${gcode.length} bytes)`);
  socket.emit('command', port, 'gcode:load', basename(file), gcode, {}, (err, state) => {
    if (err) fail(`gcode:load: ${err.message || err}`);
    log(`gcode:load ok: total=${state.total}`);
    socket.emit('command', port, 'gcode:start');
  });
};
socket.on('serialport:read', (line) => {
  if (/^Grbl \d/.test(line)) loadAndStart(`banner "${line.trim()}"`);
  else if (/^(error|ALARM)/.test(line)) log(`serialport:read ${line}`);
});
socket.on('controller:settings', (type) => loadAndStart(`controller:settings ${type}`));

socket.on('sender:status', (s) => {
  lastStatus = s;
  if (Date.now() - lastStatusLog >= 1000) {
    lastStatusLog = Date.now();
    log(`sender:status ${fmtStatus(s)}`);
  }
});
socket.on('workflow:state', (state) => {
  log(`workflow:state ${state}`);
  if (state === 'running') sawRunning = true;
  if (state === 'idle' && sawRunning) {
    const s = lastStatus || {};
    log(`final sender:status ${fmtStatus(s)} elapsed=${((s.elapsedTime || 0) / 1000).toFixed(1)}s`);
    socket.emit('close', port, () => {
      socket.close();
      if (s.received !== s.total || !s.total) fail('received != total');
      log('PASS');
      process.exit(0);
    });
  }
});
