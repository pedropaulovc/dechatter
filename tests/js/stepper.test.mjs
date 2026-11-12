// The widget's stepper against a scripted controller: `node --test tests/js`.

import assert from 'node:assert/strict';
import { test } from 'node:test';

import { PHASE, Stepper } from '../../src/dechatter/widget/stepper.js';

const tick = () => new Promise((resolve) => setImmediate(resolve));

// What a controller does with a feed hold.
const HOLD = {
  COMPLETES: 'completes', // decelerates, then reports Hold:0
  NEVER: 'never', // never reports the hold complete
  ALARMS: 'alarms', // goes into alarm instead
  SLOWS: 'slows', // reports Hold:0 while its position still changes (Grbl 0.9's bare Hold)
  // cncjs emits an Idle from a `?` answered before the move started (a $G reply changed the
  // parser state), then the move and its hold.
  STALE_IDLE: 'stale-idle',
  // Reports Hold:0, then keeps reporting it unchanged: cncjs emits on any change of its
  // state, feed, spindle speed or planner buffer among them, none of which the stepper sees.
  CHATTY: 'chatty',
};

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
    this.hold = HOLD.COMPLETES; // what a feed hold does
    this.lastReport = null; // when the controller last reported
    this.resetAt = null;
    this.io = {
      send: (lines) => {
        this.log.push(...lines);
        if (lines.some((line) => /^G[01]\b/.test(line))) {
          assert.equal(this.moving, false, 'a move was sent while another ran');
          this.moving = true;
          setImmediate(() => this.move(lines));
        }
      },
      feedhold: () => {
        this.log.push('!');
        this.feedhold();
      },
      cyclestart: () => this.log.push('~'),
      reset: () => {
        this.log.push('^X');
        this.resetAt = this.t;
      },
    };
  }

  now = () => this.t;

  sleep = async (ms) => {
    this.t += ms / 1000;
    await tick();
  };

  report(activeState, subState = null) {
    this.lastReport = this.t;
    this.stepper.controller({ activeState, subState, wpos: this.wpos });
  }

  async feedhold() {
    if (this.hold === HOLD.NEVER) {
      return;
    }
    await tick();
    if (this.hold === HOLD.ALARMS) {
      this.report('Alarm');
      return;
    }
    if (this.hold === HOLD.STALE_IDLE) {
      this.report('Idle');
      await tick();
      this.wpos = { ...this.wpos, x: (Number(this.wpos.x) + 0.5).toFixed(3) };
      this.report('Run');
      await tick();
      this.report('Hold', 0);
      return;
    }
    if (this.hold === HOLD.CHATTY) {
      this.report('Hold', 0);
      this.heldAt = this.t;
      for (let i = 0; i < 40; i += 1) {
        await tick();
        this.report('Hold', 0);
      }
      return;
    }
    if (this.hold === HOLD.SLOWS) {
      // Grbl 0.9 says a bare Hold, which cncjs reads as Hold:0, while it still decelerates.
      for (let i = 0; i < 3; i += 1) {
        this.wpos = { ...this.wpos, x: (Number(this.wpos.x) + 0.5).toFixed(3) };
        this.report('Hold', 0);
        await tick();
      }
      return;
    }
    this.report('Hold', 0); // decelerated: the hold is complete
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
  // The cut it stopped is kept, marked aborted, up to the stop.
  assert.deepEqual(windows, [
    { kind: 'dwell', pass: 1, start: 6, end: 10 },
    { kind: 'cut', pass: 1, start: 10, end: 10, holds: [], aborted: true },
  ]);
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

test('an Idle report away from where the move ends is noted, so the widget can point to Stop', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  machine.move = async function () {
    this.report('Run');
    this.t += 1;
    this.wpos = { ...this.wpos, z: '7.000' }; // short of the target, and idle
    this.moving = false;
    this.report('Idle');
    assert.equal(stepper.idleAway, 1);
    this.t += 5;
    this.report('Idle');
    assert.equal(stepper.idleAway, 1, 'since the first such report');
    await stepper.stop();
  };
  const blocks = [{ step: 'setup', lines: ['G21 G90 G94 G17', 'G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } }];
  assert.equal(await stepper.run(blocks), PHASE.STOPPED);
});

test('a move to where the machine already stands is done without a new report', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  stepper.controller({ activeState: 'Idle', wpos: { x: '0.000', y: '0.000', z: '10.000' } });
  machine.move = async function () {}; // cncjs reports nothing: nothing changed
  const blocks = [{ step: 'setup', lines: ['G21 G90 G94 G17', 'G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } }];
  assert.equal(await stepper.run(blocks), PHASE.DONE);
});

test('stop resets only once the hold is complete, and never without it', async () => {
  const settled = new Machine();
  const { stepper } = settled.stepper_for();
  settled.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      settled.moving = false;
      settled.report('Run');
      await stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.STOPPED);
  assert.deepEqual(settled.log.slice(-2), ['!', '^X'], 'reset after Hold:0');

  const moving = new Machine();
  moving.hold = HOLD.NEVER;
  const second = moving.stepper_for().stepper;
  moving.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      moving.moving = false;
      moving.report('Run');
      second.stop();
    }
  };
  assert.equal(await second.run(round([1])), PHASE.FAILED);
  assert.equal(moving.log.at(-1), '!', 'no reset while it may still move');
  assert.match(second.reason, /did not report the hold complete, so it was not reset/);
});

test('an alarm ends the run without a reset or an unlock: the operator clears it', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      machine.report('Alarm');
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.FAILED);
  assert.ok(!machine.log.includes('^X') && !machine.log.includes('!') && !machine.log.includes('$X'));
  assert.match(stepper.reason, /alarm.*home, or unlock/);
});

test('a hold already in place when a move is sent holds that move', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  stepper.controller({ activeState: 'Hold', subState: 0, wpos: machine.wpos });
  const blocks = [{ step: 'setup', lines: ['G21 G90 G94 G17', 'G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } }];
  machine.move = async function () {}; // held: nothing moves
  const running = stepper.run(blocks);
  await tick();
  assert.equal(stepper.phase, PHASE.HELD);
  stepper.disconnected();
  assert.equal(await running, PHASE.FAILED);
});

// The review's log: Idle at Z10 from the setup move, the next move sent, Stop before
// cncjs reports anything new. That Idle predates the move: a reset now loses position.
test('stop or a failure right after a move is sent holds before it resets', async () => {
  for (const [what, act] of [
    ['stop', (stepper) => stepper.stop()],
    ['error', (stepper) => stepper.error('error:9 (G-code locked out during alarm or jog state)')],
  ]) {
    const machine = new Machine();
    const { stepper } = machine.stepper_for();
    const blocks = [
      { step: 'setup', lines: ['G0 Z10'], pass_id: null, rpm: null, seconds: null, target: { z: 10 } },
      { step: 'rapid', lines: ['G0 X-8 Y1'], pass_id: 1, rpm: 1000, seconds: null, target: { x: -8, y: 1, z: 10 } },
    ];
    const move = machine.move.bind(machine);
    machine.move = async function (lines) {
      if (lines[0] !== 'G0 X-8 Y1') {
        return move(lines);
      }
      this.moving = false;
      act(stepper); // before cncjs's next poll: the last report is still Idle
    };
    await stepper.run(blocks);
    assert.deepEqual(machine.log, ['G0 Z10', 'G0 X-8 Y1', '!', '^X'], what);
  }
});

test('an alarm while stopping ends it at once, without a reset or blame on the hold', async () => {
  const machine = new Machine();
  machine.hold = HOLD.ALARMS;
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.FAILED);
  assert.ok(!machine.log.includes('^X'));
  assert.ok(machine.t < 5 + 10 + 2, 'it does not wait out the settle time');
  assert.match(stepper.reason, /alarm/);
  assert.doesNotMatch(stepper.reason, /did not report the hold complete/);
});

test('an ALARM: line ends the run without a hold, a reset or an unlock', async () => {
  const machine = new Machine();
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.alarm('ALARM:1 (Hard limit)');
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.FAILED);
  assert.ok(!machine.log.includes('!') && !machine.log.includes('^X') && !machine.log.includes('$X'));
  assert.match(stepper.reason, /ALARM:1.*home, or unlock/);
});

test('a hold counts as complete only once the position stops changing', async () => {
  const machine = new Machine();
  machine.hold = HOLD.SLOWS;
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.STOPPED);
  assert.ok(machine.resetAt - machine.lastReport >= 1, `reset ${machine.resetAt - machine.lastReport} s after the last move`);
});

test('an Idle reported after the hold counts only once nothing changes', async () => {
  const machine = new Machine();
  machine.hold = HOLD.STALE_IDLE;
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.STOPPED);
  assert.ok(machine.resetAt - machine.lastReport >= 1, `reset ${machine.resetAt - machine.lastReport} s after the last report`);
});

test('a lost connection during a stop ends it without a reset, then or on reconnect', async () => {
  const machine = new Machine();
  machine.hold = HOLD.NEVER;
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.stop();
      await tick();
      machine.report('Hold', 0); // the hold completes, then the link drops before the reset
      stepper.disconnected();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.FAILED);
  assert.ok(!machine.log.includes('^X'), 'no reset to be buffered and sent on reconnect');
  assert.ok(machine.t < 5 + 10 + 2, 'it does not wait out the settle time');
  assert.match(stepper.reason, /^stopped by the operator; the connection was lost while stopping, so it was not reset/);
});

test('reports that change nothing the stepper sees do not delay the reset', async () => {
  const machine = new Machine();
  machine.hold = HOLD.CHATTY;
  const { stepper } = machine.stepper_for();
  machine.onMove = async (lines) => {
    if (lines[0] === 'G1 X68 F500') {
      machine.moving = false;
      stepper.stop();
    }
  };
  assert.equal(await stepper.run(round([1])), PHASE.STOPPED);
  const after = machine.resetAt - machine.heldAt;
  assert.ok(after >= 1 && after < 1.5, `reset ${after} s after Hold:0`);
});
