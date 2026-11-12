// Runs a round through cncjs's controller one block at a time, so the widget knows when
// each cut starts and ends. A controller buffers whatever it is sent and reports no line
// numbers by default (FluidNC, stock Grbl), so the only way to know where the machine is
// is to send one move, then wait until it reports Idle at that move's end.
//
// The stepper talks to the machine through `io` (cncjs's socket commands) and learns its
// state from controller() (cncjs's controller:state), error() (an error: line), alarm()
// (an ALARM: line) and disconnected(). Nothing here touches the page.

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
// Stop: hold, wait until the controller reports the hold complete or Idle, then reset. A
// reset while the machine still moves loses its position (Grbl ALARM:3), so if it does not
// settle in this long, nothing is reset and the operator is told.
const SETTLE_TIMEOUT_S = 15;
// The machine is at rest once it has said Hold:0 or Idle and nothing has changed for this
// long: cncjs polls about every 250 ms and reports changes, the position among them. Grbl
// 0.9 reports a bare Hold, which cncjs reads as Hold:0, while it still decelerates, and an
// Idle can be stale (see holdThenReset).
const QUIET_S = 1;
const POLL_MS = 250;
const ALARM_ADVICE = 'Clear it in cncjs (home, or unlock if you are sure the position is still right).';

class Ended extends Error {}

// How a halt ended.
const HALT = {
  RESET: 'reset', // the machine had stopped; it was reset
  UNSETTLED: 'unsettled', // it never said it stopped: nothing was reset
  ALARM: 'alarm', // the controller went into alarm: it stopped itself
};

// The link to cncjs and the machine. Once lost, nothing more is sent: socket.io buffers
// what is emitted while disconnected and sends it on reconnect, maybe minutes later.
const LINK = { UP: 'up', LOST: 'lost' };

// A report that says the machine is at rest, once nothing changes for QUIET_S.
const atRest = (state) => state.activeState === 'Idle' || (state.activeState === 'Hold' && state.subState === 0);
const UNSETTLED_ADVICE = 'so it was not reset. Make sure it has stopped, then reset it in cncjs.';

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
    this.watchers = []; // halt() waiting for the next report; unlike waiters, they outlive the end of the run
    this.alarmedBy = null; // the alarm that ended the run, if one did
    this.link = LINK.UP;
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
    if (activeState === 'Alarm') {
      this.alarm('an alarm');
      return;
    }
    for (const resolve of this.watchers.splice(0)) {
      resolve(this.state);
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

  // The controller has stopped the machine itself: nothing is sent, not even a hold or a
  // reset, and a halt under way ends at once.
  alarm(what) {
    if (this.alarmedBy !== null || this.phase === PHASE.DONE || this.phase === PHASE.STOPPED) {
      return;
    }
    this.alarmedBy = what;
    this.end(PHASE.FAILED, `the controller reported ${what}. ${ALARM_ADVICE}`);
    for (const resolve of this.watchers.splice(0)) {
      resolve(this.state);
    }
  }

  // The socket or the port went away. A halt under way ends at once, sending nothing.
  disconnected() {
    this.link = LINK.LOST;
    this.end(PHASE.FAILED, 'the connection to cncjs or the machine was lost');
    for (const resolve of this.watchers.splice(0)) {
      resolve(this.state);
    }
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
    const was = this.phase;
    this.end(PHASE.STOPPING, reason);
    this.halting = (async () => {
      const halt = was === PHASE.READY ? HALT.RESET : await this.halt(was);
      this.set(halt === HALT.RESET ? PHASE.STOPPED : PHASE.FAILED);
    })();
    await this.halting;
  }

  // Bring the machine to rest and reset it. `was`: the phase the run was in.
  async halt(was) {
    if (this.alarmedBy !== null) {
      return HALT.ALARM;
    }
    if (this.link === LINK.LOST) {
      return HALT.UNSETTLED;
    }
    if (was !== PHASE.MOVING && was !== PHASE.HELD) {
      // Dwelling or waiting for the spindle: the last move was seen to end where it should.
      this.io.reset();
      return HALT.RESET;
    }
    try {
      return await this.holdThenReset();
    } finally {
      this.watchers = [];
    }
  }

  // A move was sent: the last report may predate it (cncjs reports only changes), and even
  // a report after the hold can carry the status of a `?` answered before the move started
  // (cncjs re-emits the last status when the parser state changes). So Idle or Hold:0
  // counts only once reported after the hold and unchanged for QUIET_S. A hold already
  // complete when the move was sent keeps that move from starting: it counts the same way.
  async holdThenReset() {
    const since = this.now();
    let report = this.nextReport(); // watch before holding: the controller may answer at once
    this.io.feedhold();
    let quietSince = this.state.activeState === 'Hold' && this.state.subState === 0 ? since : null;
    for (;;) {
      const state = await Promise.race([report, this.sleep(POLL_MS).then(() => null)]);
      if (this.alarmedBy !== null) {
        return HALT.ALARM;
      }
      if (this.link === LINK.LOST) {
        this.reason = `${this.reason} before the machine reported the hold complete, ${UNSETTLED_ADVICE}`;
        return HALT.UNSETTLED;
      }
      if (state) {
        report = this.nextReport();
        quietSince = atRest(state) ? this.now() : null;
      }
      if (quietSince !== null && this.now() - quietSince >= QUIET_S) {
        this.io.reset();
        return HALT.RESET;
      }
      if (this.now() - since >= SETTLE_TIMEOUT_S) {
        this.reason = `${this.reason}; the machine did not report the hold complete, ${UNSETTLED_ADVICE}`;
        return HALT.UNSETTLED;
      }
    }
  }

  nextReport() {
    return new Promise((resolve) => this.watchers.push(resolve));
  }

  fail(reason) {
    if (this.finished || this.phase === PHASE.STOPPING) {
      return;
    }
    const was = this.phase;
    this.end(PHASE.FAILED, reason);
    if (was !== PHASE.READY) {
      this.halting = this.halt(was);
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
