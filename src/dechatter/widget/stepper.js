// Runs a round through cncjs's controller one block at a time, so the widget knows when
// each cut starts and ends. A controller buffers whatever it is sent and reports no line
// numbers by default (FluidNC, stock Grbl), so the only way to know where the machine is
// is to send one move, then wait until it reports Idle at that move's end.
//
// The stepper talks to the machine through `io` (cncjs's socket commands) and learns its
// state from controller() (cncjs's controller:state), error() (an error: or ALARM: line)
// and disconnected(). Nothing here touches the page.

// What the run is doing; the widget shows it.
export const PHASE = {
  READY: 'ready', // not started
  MOVING: 'moving', // a block was sent; waiting for Idle at its end
  DWELLING: 'dwelling', // spindle at speed in the air beside the stock
  SET_SPINDLE: 'set-spindle', // manual spindle: waiting for the operator's Continue
  HELD: 'held', // the controller holds (feed hold, from here or from cncjs)
  STOPPING: 'stopping',
  DONE: 'done', // every block ran
  STOPPED: 'stopped', // the operator stopped it
  FAILED: 'failed', // an error, an alarm or a lost connection ended it
};

const FINISHED = new Set([PHASE.DONE, PHASE.STOPPED, PHASE.FAILED]);
const HOLD_STATES = new Set(['Hold', 'Door']);
// Stop: hold, wait until the controller reports the hold complete (Hold:0) or Idle, then
// reset. A reset while the machine still moves loses its position (Grbl ALARM:3), so if it
// does not settle in this long, nothing is reset and the operator is told.
const SETTLE_TIMEOUT_MS = 15000;
const ALARM_ADVICE = 'Clear it in cncjs (home, or unlock if you are sure the position is still right).';

class Ended extends Error {}

// Positions match within this many mm: controllers report three decimals in mm, or four in
// inches, and a program in inches rounds its words to four decimals.
const TOLERANCE_MM = 0.02;

export class Stepper {
  // io: { send(lines), feedhold(), cyclestart(), reset() }. It never unlocks ($X): an
  // alarm is for the operator to clear.
  // reportInches: the controller reports positions in inches (Grbl's $13=1); targets are mm.
  constructor({ io, reportInches = false, now = () => Date.now() / 1000, sleep = (ms) => new Promise((r) => setTimeout(r, ms)), onChange = () => {}, onWindow = () => {} }) {
    this.io = io;
    this.scale = reportInches ? 25.4 : 1;
    this.now = now;
    this.sleep = sleep;
    this.onChange = onChange;
    this.onWindow = onWindow; // a dwell or cut window, as soon as it is complete
    this.phase = PHASE.READY;
    this.block = null; // the block being run
    this.index = -1;
    this.total = 0;
    this.reason = ''; // why it stopped or failed
    this.state = { activeState: '', subState: null, wpos: null };
    this.watchers = []; // what halt() waits for; unlike waiters, they outlive the end of the run
    this.waiters = [];
    this.ended = null; // the Ended error every wait rejects with, once the run is over
    this.holds = []; // [start, end] of holds during the current block
    // Since when the controller has said Idle somewhere other than where the block ends: a
    // move it dropped, or a position it reports differently. null while that is not so.
    this.idleAway = null;
    this.continued = null;
    this.halting = null;
  }

  get finished() {
    return FINISHED.has(this.phase);
  }

  // ---- what the controller says ------------------------------------------------------

  // subState: Grbl's Hold:N, as cncjs reports it: 0 once the hold is complete.
  controller({ activeState, subState = null, wpos }) {
    this.state = { activeState: activeState ?? '', subState, wpos: wpos ?? this.state.wpos };
    for (const watcher of [...this.watchers]) {
      if (watcher.until(this.state)) {
        this.watchers.splice(this.watchers.indexOf(watcher), 1);
        watcher.resolve(true);
      }
    }
    if (activeState === 'Alarm') {
      // The controller has stopped the machine itself: nothing is sent, not even a reset.
      this.end(PHASE.FAILED, `the controller is in alarm. ${ALARM_ADVICE}`);
      return;
    }
    const away = this.phase === PHASE.MOVING && activeState === 'Idle' && !this.at(this.state.wpos, this.block?.target ?? {});
    this.idleAway = away ? (this.idleAway ?? this.now()) : null;
    const held = HOLD_STATES.has(activeState);
    if (held && this.phase === PHASE.MOVING) {
      this.holds.push([this.now(), null]);
      this.set(PHASE.HELD);
    }
    if (!held && this.phase === PHASE.HELD && activeState) {
      this.holds.at(-1)[1] = this.now();
      this.set(PHASE.MOVING);
    }
    for (const waiter of [...this.waiters]) {
      if (waiter.until(this.state)) {
        this.waiters.splice(this.waiters.indexOf(waiter), 1);
        waiter.resolve(this.now());
      }
    }
  }

  error(message) {
    this.fail(message);
  }

  disconnected() {
    this.end(PHASE.FAILED, 'the connection to cncjs or the machine was lost');
  }

  // ---- what the operator does --------------------------------------------------------

  hold() {
    if (this.phase === PHASE.MOVING) {
      this.io.feedhold();
    }
  }

  resume() {
    if (this.phase === PHASE.HELD) {
      this.io.cyclestart();
    }
  }

  continueSpindle() {
    if (this.phase === PHASE.SET_SPINDLE) {
      this.continued?.();
    }
  }

  // Stop sending, hold, wait for the hold to complete, then reset.
  async stop(reason = 'stopped by the operator') {
    if (this.finished || this.phase === PHASE.STOPPING) {
      return;
    }
    const wasRunning = this.phase !== PHASE.READY;
    this.end(PHASE.STOPPING, reason);
    this.halting = (async () => {
      const settled = wasRunning ? await this.halt() : true;
      this.set(settled ? PHASE.STOPPED : PHASE.FAILED);
    })();
    await this.halting;
  }

  // Hold; reset once the hold is complete or the machine idle. Resolves false, with the
  // reason said, if it never settles: then nothing is reset.
  async halt() {
    const settled = (state) => state.activeState === 'Idle' || (state.activeState === 'Hold' && state.subState === 0);
    if (!settled(this.state)) {
      // Watch before holding: the controller may answer at once.
      const holdComplete = new Promise((resolve) => this.watchers.push({ until: settled, resolve }));
      this.io.feedhold();
      const done = await Promise.race([holdComplete, this.sleep(SETTLE_TIMEOUT_MS).then(() => false)]);
      this.watchers = [];
      if (!done) {
        this.reason = `${this.reason}; the machine did not report the hold complete, so it was not reset. Make sure it has stopped, then reset it in cncjs.`;
        return false;
      }
    }
    this.io.reset();
    return true;
  }

  fail(reason) {
    if (this.finished || this.phase === PHASE.STOPPING) {
      return;
    }
    const wasRunning = this.phase !== PHASE.READY;
    this.end(PHASE.FAILED, reason);
    if (wasRunning) {
      this.halting = this.halt();
    }
  }

  // A Hold already in place when a move starts (cncjs's own feed hold) holds that move.
  enterHoldIfHeld() {
    if (HOLD_STATES.has(this.state.activeState) && this.phase === PHASE.MOVING) {
      this.holds.push([this.now(), null]);
      this.set(PHASE.HELD);
    }
  }

  // ---- the run -----------------------------------------------------------------------

  // Run the blocks; resolves with the phase it ended in (done, stopped or failed).
  async run(blocks) {
    this.total = blocks.length;
    try {
      for (const [index, block] of blocks.entries()) {
        this.index = index;
        this.block = block;
        await this.runBlock(block);
      }
      this.set(PHASE.DONE);
    } catch (err) {
      if (!(err instanceof Ended)) {
        this.fail(err.message);
      }
    }
    await this.halting; // a stop or failure finishes holding and resetting first
    return this.phase;
  }

  async runBlock(block) {
    this.check();
    if (block.step === 'set-spindle') {
      this.set(PHASE.SET_SPINDLE);
      const waiter = { until: () => false };
      await new Promise((resolve, reject) => {
        Object.assign(waiter, { resolve, reject });
        this.continued = resolve;
        this.waiters.push(waiter);
      });
      this.waiters.splice(this.waiters.indexOf(waiter), 1);
      this.continued = null;
      return;
    }
    if (block.step === 'dwell') {
      this.set(PHASE.DWELLING);
      const start = this.now();
      await this.pause(block.seconds * 1000);
      this.onWindow({ kind: 'dwell', pass: block.pass_id, start, end: this.now() });
      return;
    }
    if (block.target === null) {
      this.io.send(block.lines); // moves nothing: no position to wait for
      return;
    }
    this.holds = [];
    this.idleAway = null;
    this.set(PHASE.MOVING);
    const start = this.now();
    this.io.send(block.lines);
    this.enterHoldIfHeld();
    const window = (end, more = {}) => ({ kind: 'cut', pass: block.pass_id, start, end, holds: this.holds.map(([s, e]) => [s, e ?? end]), ...more });
    let end;
    try {
      end = await this.wait((state) => state.activeState === 'Idle' && this.at(state.wpos, block.target));
    } catch (err) {
      // A cut cut short still counts, up to where it stopped.
      if (block.step === 'cut' && err instanceof Ended) {
        this.onWindow(window(this.endedAt, { aborted: true }));
      }
      throw err;
    }
    if (block.step === 'cut') {
      this.onWindow(window(end));
    }
  }

  at(wpos, target) {
    if (!wpos) {
      return false;
    }
    return Object.entries(target).every(([axis, mm]) => Math.abs(Number(wpos[axis]) * this.scale - mm) <= TOLERANCE_MM);
  }

  // cncjs reports the controller's state only when it changes: a move that leaves the
  // machine where it stood (already Idle there) is done at once.
  wait(until) {
    this.check();
    if (until(this.state)) {
      return Promise.resolve(this.now());
    }
    return new Promise((resolve, reject) => this.waiters.push({ until, resolve, reject }));
  }

  // A dwell: the stepper's own clock, cut short by a stop.
  async pause(ms) {
    let stop;
    const ended = new Promise((_, reject) => {
      stop = { until: () => false, resolve: () => {}, reject };
      this.waiters.push(stop);
    });
    await Promise.race([this.sleep(ms), ended]);
    this.waiters.splice(this.waiters.indexOf(stop), 1);
    this.check();
  }

  check() {
    if (this.ended) {
      throw this.ended;
    }
  }

  end(phase, reason) {
    this.reason = reason;
    this.endedAt = this.now(); // where a cut under way stopped counting
    this.ended = new Ended(reason);
    for (const waiter of this.waiters.splice(0)) {
      waiter.reject(this.ended);
    }
    this.set(phase);
  }

  set(phase) {
    this.phase = phase;
    this.onChange(this);
  }
}
