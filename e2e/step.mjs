#!/usr/bin/env node
// Run a round's blocks through real cncjs with the widget's own stepper, as the widget does,
// and check what it recorded. Usage:
//   node e2e/step.mjs blocks.json [--speed N] [--stop-in-cut K] [--url URL] [--port PATH]
// blocks.json comes from e2e/round_steps.py. A manual-spindle round is continued after 1 s
// at each speed. Exits 0 with PASS when the round ran to its end, every cut window lasted
// about as long as its move, and no M0 was sent.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { PHASE, Stepper } from '../src/dechatter/widget/stepper.js';

const cache = process.env.DECHATTER_E2E_CACHE || join(homedir(), '.cache', 'dechatter-e2e');
const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args.splice(i, 2)[1] : dflt;
};
const url = opt('--url', 'http://localhost:8000');
const port = opt('--port', join(cache, 'ttyGRBL'));
const speed = Number(opt('--speed', '1'));
// Press Stop halfway through the K-th cut: the run must hold, reset once the hold is
// complete, end STOPPED, and leave the controller out of alarm.
const stopInCut = Number(opt('--stop-in-cut', '0'));
let cutsStarted = 0;
const blocks = JSON.parse(readFileSync(args[0], 'utf8'));

const require = createRequire(join(cache, 'cncjs', 'node_modules', 'cncjs', 'package.json'));
const io = require('socket.io-client');
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1).padStart(6)}s]`, ...a);
const fail = (msg) => {
  log(`FAIL: ${msg}`);
  process.exit(1);
};
setTimeout(() => fail('timeout'), 900_000).unref();

const res = await fetch(`${url}/api/signin`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: '' }) });
const { token } = await res.json();
const socket = io.connect(url, { query: `token=${token}`, transports: ['websocket'] });
const command = (...a) => socket.emit('command', port, ...a);
const sent = [];
const windows = [];
const stepper = new Stepper({
  io: {
    send: (lines) => {
      sent.push(...lines);
      command('gcode', lines);
    },
    feedhold: () => command('feedhold'),
    cyclestart: () => command('cyclestart'),
    reset: () => command('reset'),
  },
  onChange: (s) => {
    if (stopInCut && s.phase === PHASE.MOVING && s.block?.step === 'cut') {
      cutsStarted += 1;
      if (cutsStarted === stopInCut) setTimeout(() => s.stop(), 300);
    }
    if (s.phase === PHASE.SET_SPINDLE) {
      log(`set the spindle to ${s.block.rpm} rpm`);
      setTimeout(() => s.continueSpindle(), 1000);
    }
  },
  onWindow: (w) => {
    windows.push(w);
    log(`${w.kind} pass ${w.pass}: ${(w.end - w.start).toFixed(2)} s`);
  },
});

let started = false;
const start = async () => {
  if (started) return;
  started = true;
  log(`running ${blocks.length} blocks`);
  const phase = await stepper.run(blocks);
  log(`run ended: ${phase} ${stepper.reason}`);
  if (stopInCut) {
    await new Promise((r) => setTimeout(r, 1000));
    const ok = phase === PHASE.STOPPED && !sent.includes('$X') && lastState !== 'Alarm';
    log(`${ok ? 'PASS' : 'FAIL'}: stopped in cut ${stopInCut}: ${phase}, controller ${lastState}, aborted window: ${JSON.stringify(windows.at(-1))}`);
    socket.emit('close', port, () => process.exit(ok ? 0 : 1));
    return;
  }
  if (phase !== PHASE.DONE) {
    socket.emit('close', port, () => fail(`ended ${phase}`));
    return;
  }
  if (sent.some((line) => line.startsWith('M0'))) fail('an M0 was sent');
  // A cut's window runs from sending its move to Idle at its end: the move's own time,
  // plus up to one status poll (250 ms) of lag.
  const cuts = blocks.filter((b) => b.step === 'cut');
  for (const w of windows.filter((w) => w.kind === 'cut')) {
    const block = cuts.find((b) => b.pass_id === w.pass);
    const from = blocks[blocks.indexOf(block) - 2].target; // where the plunge ended
    const mm = Math.abs(block.target.x - from.x);
    // Targets are in mm; a program in inches gives its feeds in in/min.
    const inches = blocks[0].lines[0].startsWith('G20');
    const feed = Number(/F([\d.]+)/.exec(block.lines[0])[1]) * (inches ? 25.4 : 1);
    const expected = mm / feed * 60 / speed;
    const took = w.end - w.start;
    if (took < expected - 0.05 || took > expected + 0.6) fail(`cut ${w.pass}: ${took.toFixed(2)} s, expected ${expected.toFixed(2)} s`);
  }
  const dwells = windows.filter((w) => w.kind === 'dwell');
  if (dwells.length !== cuts.length) fail(`${dwells.length} dwell windows for ${cuts.length} cuts`);
  log(`PASS: ${windows.length} windows`);
  socket.emit('close', port, () => process.exit(0));
};

socket.on('startup', () => socket.emit('open', port, { controllerType: 'Grbl', baudrate: 115200 }, (err) => err && fail(String(err))));
// A port opened afresh announces its settings; one left open replays its state.
socket.on('controller:settings', () => setTimeout(start, 500));
socket.on('serialport:open', () => setTimeout(start, 1500));
let lastState = '';
socket.on('controller:state', (_t, state) => (lastState = state?.status?.activeState) && stepper.controller({ activeState: state?.status?.activeState, subState: state?.status?.subState, wpos: state?.status?.wpos }));
socket.on('serialport:read', (line) => {
  if (/^(error|ALARM):/.test(line)) {
    log(`serialport:read ${line}`);
    stepper.error(line);
  }
});
socket.on('disconnect', () => stepper.disconnected());
