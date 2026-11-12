// The widget's stepper against a scripted controller: `node --test tests/js`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { PHASE, Stepper } from '../../src/dechatter/widget/stepper.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));

// A controller as cncjs reports it: each move takes `seconds` of virtual time, shows Run
// on the way and Idle at its end. It refuses a second move while one is under way.
class Machine {
  constructor({ seconds = 2 } = {}) {
    this.t = 0;
    this.seconds = seconds;
    this.wpos = { x: '0.000', y: '0.000', z: '0.000' };
    this.log = [];
    this.moving = false;
    this.onMove = null; // a test's hook into a move under way
    this.io = {
      send: (lines) => {
        this.log.push(...lines);
        if (lines.some((line) => /^G[01]\b/.test(line))) {
          assert.equal(this.moving, false, 'a move was sent while another ran');
          this.moving = true;
          setImmediate(() => this.move(lines));
        }
      },
      feedhold: () => this.log.push('!'),
      cyclestart: () => this.log.push('~'),
      reset: () => this.log.push('^X'),
      unlock: () => this.log.push('$X'),
    };
  }

  now = () => this.t;

  sleep = async (ms) => {
    this.t += ms / 1000;
    await tick();
  };

  report(activeState) {
    this.stepper.controller({ activeState, wpos: this.wpos });
  }

  async move(lines) {
    this.report('Run');
    await this.onMove?.(lines);
    if (!this.moving) {
      return; // the test ended it
    }
    this.t += this.seconds;
    for (const line of lines) {
      for (const [, axis, value] of line.matchAll(/([XYZ])(-?[\d.]+)/g)) {
        this.wpos = { ...this.wpos, [axis.toLowerCase()]: Number(value).toFixed(3) };
      }
    }
    this.moving = false;
    this.report('Idle');
  }

  stepper_for(reportInches = false) {
    const windows = [];
    this.stepper = new Stepper({ io: this.io, reportInches, now: this.now, sleep: this.sleep, onWindow: (w) => windows.push(w) });
    return { stepper: this.stepper, windows };
  }
}

// One pass as the engine's blocks give it, at 1000 rpm; `manual` asks for the speed.
function pass(id, { manual = false, rpm = 1000 } = {}) {
  return [
    { step: 'rapid', lines: [`G0 X-8 Y${id}`], pass_id: id, rpm, seconds: null, target: { x: -8, y: id, z: 10 } },
    manual
      ? { step: 'set-spindle', lines: [], pass_id: id, rpm, seconds: null, target: null }
      : { step: 'spindle', lines: [`M3 S${rpm}`], pass_id: id, rpm, seconds: null, target: null },
    { step: 'plunge', lines: ['G0 Z3', 'G1 Z-5 F150'], pass_id: id, rpm, seconds: null, target: { x: -8, y: id, z: -5 } },
    { step: 'dwell', lines: [], pass_id: id, rpm, seconds: 4, target: null },
    { step: 'cut', lines: ['G1 X68 F500'], pass_id: id, rpm, seconds: null, target: { x: 68, y: id, z: -5 } },
    { step: 'retract', lines: [`G0 Y${id - 3}`, 'G0 Z10'], pass_id: id, rpm, seconds: null, target: { x: 68, y: id - 3, z: 10 } },
  ];
}

const round = (passes, options) => [
  { step: 'setup', lines: ['G21 G90 G94 G17', 'M5', 'G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } },
  ...passes.flatMap((id) => pass(id, options)),
  { step: 'end', lines: ['M9', 'M5'], pass_id: null, rpm: null, seconds: null, target: null },
];

test('a round runs block by block and records each dwell and cut exactly', async () => {
  const machine = new Machine();
  const { stepper, windows } = machine.stepper_for();
  const phase = await stepper.run(round([1, 2]));
  assert.equal(phase, PHASE.DONE);
  assert.ok(!machine.log.some((line) => line.startsWith('M0')));
  assert.deepEqual(machine.log.filter((line) => line.startsWith('M3')), ['M3 S1000', 'M3 S1000']);
  // setup 2 s, rapid 2 s, plunge 2 s, dwell 4 s, cut 2 s; then retract 2 s and the next pass.
  assert.deepEqual(windows, [
    { kind: 'dwell', pass: 1, start: 6, end: 10 },
    { kind: 'cut', pass: 1, start: 10, end: 12, holds: [] },
    { kind: 'dwell', pass: 2, start: 18, end: 22 },
    { kind: 'cut', pass: 2, start: 22, end: 24, holds: [] },
  ]);
});

test('a manual spindle stops for the operator before each speed, without an M0', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  const blocks = round([1], { manual: true });
  const running = stepper.run(blocks);
  for (let i = 0; i < 20 && stepper.phase !== PHASE.SET_SPINDLE; i += 1) {
    await tick();
  }
  assert.equal(stepper.phase, PHASE.SET_SPINDLE);
  assert.equal(stepper.block.rpm, 1000);
  const before = machine.log.length;
  await tick();
  assert.equal(machine.log.length, before, 'nothing is sent while it waits');
  stepper.continueSpindle();
  assert.equal(await running, PHASE.DONE);
  assert.ok(!machine.log.some((line) => line.startsWith('M0') || line.startsWith('M3')));
});

test('a hold during a cut pauses it, and the hold is kept with the cut window', async () => {
  const machine = new Machine();
  const { stepper, windows } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] !== 'G1 X68 F500') {
      return;
    }
    machine.t += 1;
    stepper.hold();
    machine.report('Hold');
    assert.equal(stepper.phase, PHASE.HELD);
    machine.t += 3;
    stepper.resume();
    machine.report('Run');
    assert.equal(stepper.phase, PHASE.MOVING);
  };
  assert.equal(await stepper.run(round([1])), PHASE.DONE);
  assert.ok(machine.log.includes('!') && machine.log.includes('~'));
  assert.deepEqual(windows.at(-1), { kind: 'cut', pass: 1, start: 10, end: 16, holds: [[11, 14]] });
});

test('stop holds, resets once the machine has decelerated, and ends the run', async () => {
  const machine = new Machine();
  const { stepper, windows } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      await stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1, 2])), PHASE.STOPPED);
  assert.deepEqual(machine.log.slice(-2), ['!', '^X']);
  assert.ok(!machine.log.includes('G0 Y-2'), 'nothing more is sent');
  assert.deepEqual(windows.map((w) => w.kind), ['dwell']);
});

test('a stop during the dwell ends it at once', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  machine.sleep = async (ms) => {
    if (stepper.phase === PHASE.DWELLING) {
      stepper.stop();
    }
    machine.t += ms / 1000;
    await tick();
  };
  stepper.sleep = machine.sleep;
  assert.equal(await stepper.run(round([1])), PHASE.STOPPED);
  assert.ok(!machine.log.includes('G1 X68 F500'));
});

test('an error line, an alarm or a lost connection ends the run as failed', async () => {
  for (const [what, act] of [
    ['error', (stepper) => stepper.error('error:22 (Undefined feed rate)')],
    ['alarm', (stepper, machine) => machine.report('Alarm')],
    ['disconnect', (stepper) => stepper.disconnected()],
  ]) {
    const machine = new Machine();
    const { stepper } = machine.stepper_for();
    machine.onMove = async (lines) => {
      if (lines[0] === 'G1 X68 F500') {
        machine.moving = false;
        act(stepper, machine);
      }
    };
    assert.equal(await stepper.run(round([1, 2])), PHASE.FAILED, what);
    assert.ok(stepper.reason, what);
    assert.ok(!machine.log.includes('G0 Y-2'), `${what}: nothing more is sent`);
    if (what === 'disconnect') {
      assert.ok(!machine.log.includes('!'), 'nothing is sent to a lost connection');
    }
  }
});

test('targets in mm match positions reported in mm or, with $13=1, in inches', async () => {
  // A program in inches: G0 Z0.3937 is 9.99998 mm.
  const blocks = [{ step: 'setup', lines: ['G20 G90 G94 G17', 'G0 Z0.3937'], pass_id: null, rpm: null, seconds: null, target: { z: 9.99998 } }];
  const inches = new Machine();
  inches.stepper_for(true);
  assert.equal(await inches.stepper.run(blocks), PHASE.DONE, 'reported in inches');
  const mm = new Machine();
  mm.stepper_for(false);
  mm.move = async function () {
    this.report('Run');
    this.t += 2;
    this.wpos = { ...this.wpos, z: '10.000' };
    this.moving = false;
    this.report('Idle');
  };
  assert.equal(await mm.stepper.run(blocks), PHASE.DONE, 'reported in mm');
  const short = new Machine();
  short.stepper_for(false);
  short.move = async function () {
    this.report('Run');
    this.wpos = { ...this.wpos, z: '9.950' };
    this.moving = false;
    this.report('Idle');
    short.stepper.disconnected();
  };
  assert.equal(await short.stepper.run(blocks), PHASE.FAILED, 'Idle short of the target is not the end');
});

test('a move to where the machine already stands is done without a new report', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  stepper.controller({ activeState: 'Idle', wpos: { x: '0.000', y: '0.000', z: '10.000' } });
  machine.move = async function () {}; // cncjs reports nothing: nothing changed
  const blocks = [{ step: 'setup', lines: ['G21 G90 G94 G17', 'G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } }];
  assert.equal(await stepper.run(blocks), PHASE.DONE);
});
