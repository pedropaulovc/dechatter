// dechatter cncjs widget. Everything runs in this page: cncjs is reached over its socket.io
// API (load the program, watch the job), the microphone is the browser's, and the planning
// and analysis are dechatter's Python running in a worker (engine.js).

import { Mic } from './mic.js';
import { PHASE, Stepper } from './stepper.js';
import { chipLoad, feed, length, rate } from './units.js';
import { START_FROM, STEPS, applicable, closeInfo, draw, info, placehold, read, reveal } from './wizard.js';

const params = new URLSearchParams(location.search);
const token = params.get('token') ?? '';
const cncjsOrigin = params.get('host') ?? location.origin;

const TIMELINE_LIMIT = 5000;
const LEVEL_MS = 250;
const SESSION_KEY = 'dechatter.session';
const DEVICE_KEY = 'dechatter.microphone';
const $ = (id) => document.getElementById(id);

const MACHINE_UNITS = { G20: 'in', G21: 'mm' };
const UNITS_TEXT = {
  mm: 'Lengths in mm: the machine is in G21.',
  in: 'Lengths in inches: the machine is in G20.',
  unknown: 'Lengths in mm. With the machine connected they follow its G20/G21 mode.',
};
const cnc = {
  socket: null,
  link: 'connecting', // connecting | no-port | ready | offline
  port: '',
  workflow: null, // idle | running | paused: cncjs's own sender; null until cncjs reports it
  activeState: '', // the controller's state as cncjs last reported it: Idle, Run, Hold, Alarm…
  subState: null, // with Hold, Grbl's Hold:N: 0 once the hold is complete
  wpos: null, // its work position then, as cncjs reports it
  reportInches: false, // the controller reports positions in inches (Grbl's $13=1)
  // mm | in: the machine's G21/G20 mode as cncjs last reported it; null until it has. The
  // wizard shows lengths in it and programs are written in it, so a job never changes it.
  units: null,
};
// The round the widget is running, one block at a time: { stepper, round, name }, else null.
// It is set the moment Start is pressed, so a second Start finds it and what cncjs reports
// while the round is being prepared reaches the stepper. The session stays pinned to it.
let run = null;
const mic = new Mic();
let boot = 'Starting'; // what the engine is loading; null once it is ready
let fatal = ''; // why the engine could not start; nothing works without it
let names = []; // sessions stored in this browser
let current = localStorage.getItem(SESSION_KEY) ?? '';
let state = null; // the current session, as the engine last reported it
let editing = null; // { values, defaults, materials, computed, step } while the setup wizard is open, else null
let timeline = []; // the run's windows and events, saved with its recording
// Engine requests in flight; the controls wait while there are any.
let requests = 0;
const busy = () => requests > 0;

function requestDone() {
  requests -= 1;
}
// The delete or reset confirmation: closed, being prepared, or open. While it is not
// closed, the session it names cannot change.
let dialog = 'closed';
// Whether the open session's setup loads ('loads' | 'refused' | 'seed_refused': its seed keeps
// it from loading before round 1, and can be removed), as the engine checks it; null with no
// session open. Reset and Export need it to load; the rounds need not.
let setupStatus = null;
let stockChoice = 'same'; // survives redraws: planning on the wrong face position cuts air or too deep
// What the file picked next is imported for: 'open' it as a session, or 'seed' a session from
// it in the New dialog.
let importing = 'open';
// The session the New dialog starts from an earlier one (Start from), keeping its own setup;
// '' while the dialog makes a new session.
let startFor = '';
let checkChoice = ''; // the level picked for the next check of the other direction, in mm

// ---- engine (Python in a worker) -------------------------------------------------------

const worker = new Worker(new URL('./engine.js', import.meta.url), { type: 'module' });
const pending = new Map();
let nextId = 1;

worker.onmessage = ({ data }) => {
  if ('boot' in data) {
    boot = data.boot;
    fatal = data.fatal ?? fatal;
    render();
    return;
  }
  const { resolve, reject } = pending.get(data.id);
  pending.delete(data.id);
  if ('error' in data) {
    reject(new Error(data.error));
    return;
  }
  resolve(data.ok);
};
worker.onerror = (event) => {
  fatal = `dechatter could not start its engine${event.message ? `: ${event.message}` : ''}. Export the widget again with \`dechatter widget\`.`;
  render();
};

function engine(method, ...args) {
  return new Promise((resolve, reject) => {
    const id = nextId;
    nextId += 1;
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, method, args });
  });
}

function showError(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}

// Run one request on a session that answers with its new state. The page takes that state
// only while the session is still the one open. Resolves whether the request succeeded.
async function actOn(name, method, ...args) {
  requests += 1;
  render();
  try {
    const next = await engine(method, name, ...args);
    if (name === current) {
      state = next;
    }
    showError('');
    return true;
  } catch (err) {
    showError(err.message);
    return false;
  } finally {
    requestDone();
    render();
  }
}

const act = (method, ...args) => actOn(current, method, ...args);

async function openSession(name) {
  current = name;
  editing = null;
  state = null;
  setupStatus = null;
  // A stock or level chosen for another session's block says nothing about this one.
  stockChoice = 'same';
  checkChoice = '';
  localStorage.setItem(SESSION_KEY, name);
  if (name) {
    await checkSetup(name);
    await act('state');
  }
  render();
}

// Whether a session's setup loads, apart from its rounds: a session whose rounds do not
// load can still be reset and exported.
async function checkSetup(name) {
  requests += 1;
  render();
  try {
    const { status } = await engine('setup_status', name);
    if (name === current) {
      setupStatus = status;
    }
  } catch (err) {
    if (name === current) {
      setupStatus = null;
      showError(err.message);
    }
  } finally {
    requestDone();
  }
}

// The New dialog: a name, and optionally an earlier session on the same tool assembly to
// start from. While it is open the session shown cannot change.
function newSession() {
  openSourceDialog('');
}

// Start from: the same dialog, without a name, for the open session while it has no round.
// It keeps its own setup (a prepared session arrives with one); the engine checks it is the
// tool assembly the earlier session measured.
function startFrom() {
  openSourceDialog(current);
}

function openSourceDialog(name) {
  startFor = name;
  $('new-title').textContent = name
    ? `Start ${name} from an earlier session on the same tool assembly. ${name} keeps its own setup.`
    : 'New session: one per tool assembly and kind of cut.';
  $('new-name-row').hidden = Boolean(name);
  $('new-name').required = !name;
  $('new-name').value = '';
  $('new-from').required = Boolean(name);
  $('new-create').textContent = name ? 'Start' : 'Create';
  fillSources('');
  newError('');
  closeInfo();
  dialog = 'open';
  $('new-dialog').showModal();
  render();
}

// The dialog's controls wait while it creates or imports: one request at a time, and no
// way out of the dialog until it has answered.
function renderNew() {
  for (const id of ['new-name', 'new-from', 'new-import', 'new-create', 'new-cancel']) {
    $(id).disabled = busy();
  }
}

// The sessions to start from: any other in this browser. Start from has to pick one.
function fillSources(chosen) {
  $('new-from').replaceChildren(
    el('option', { value: '' }, startFor ? 'choose a session' : 'nothing: plan the coarse grid'),
    ...names.filter((name) => name !== startFor).map((name) => el('option', { value: name }, name)),
  );
  $('new-from').value = chosen;
}

function newError(message) {
  $('new-error').textContent = message;
  $('new-error').hidden = !message;
}

// The session stays pinned from the request until its wizard is open: nothing else can
// be opened, made or imported in between.
async function createSession() {
  if (busy()) {
    return;
  }
  if (startFor) {
    await startSessionFrom();
    return;
  }
  const name = $('new-name').value.trim();
  const source = $('new-from').value;
  requests += 1;
  render();
  try {
    try {
      await engine('create', name, source);
      names = await engine('names');
    } catch (err) {
      newError(err.message);
      return;
    }
    closeInfo();
    $('new-dialog').close();
    try {
      // As openSession does: nothing of the session shown before stays, its wizard least.
      current = name;
      localStorage.setItem(SESSION_KEY, current);
      editing = null;
      state = null;
      setupStatus = null;
      await checkSetup(current);
      await openWizard();
      showError('');
    } catch (err) {
      showError(err.message);
    }
  } finally {
    requestDone();
    render();
  }
}

// The open session is pinned while the dialog is open. On a refusal (another tool assembly,
// a round planned meanwhile) the reason shows in the dialog, which stays open.
async function startSessionFrom() {
  const name = startFor;
  requests += 1;
  render();
  try {
    const next = await engine('start_from', name, $('new-from').value);
    if (name === current) {
      state = next;
      editing = null;
    }
    closeInfo();
    $('new-dialog').close();
    showError('');
  } catch (err) {
    newError(err.message);
  } finally {
    requestDone();
    render();
  }
}

// An exported session to start from: stored in this browser like any import,
// then chosen in the dialog.
async function importSource(file) {
  if (busy()) {
    return;
  }
  requests += 1;
  render();
  try {
    const name = await engine('import_zip', new Uint8Array(await file.arrayBuffer()));
    names = await engine('names');
    fillSources(name);
    newError('');
  } catch (err) {
    newError(err.message);
  } finally {
    requestDone();
    render();
  }
}

// Start the wizard on the session's stored setup. The form is redrawn even when it last
// showed this same step, so nothing typed into an earlier wizard is left on screen.
async function openWizard() {
  editing = { ...(await engine('setup_form', current)), computed: null, step: 0 };
  $('setup-form').dataset.key = '';
}

// What the planner makes of the entries left empty, from what the wizard holds so far.
// Only shown as placeholders, filled in when the answer comes: the step is already drawn
// and may hold typing. Values the wizard cannot make sense of yet show none, and an answer
// overtaken by a later request is dropped.
async function preview(wizard) {
  wizard.asked = (wizard.asked ?? 0) + 1;
  const asked = wizard.asked;
  let computed = null;
  try {
    computed = await engine('setup_preview', JSON.stringify(applicable(wizard.values)));
  } catch {
    computed = null;
  }
  if (editing !== wizard || wizard.asked !== asked) {
    return;
  }
  wizard.computed = computed;
  placehold($('setup-form'), STEPS[wizard.step], units(), wizard);
}

function goToStep(step) {
  editing.step = Math.min(Math.max(step, 0), STEPS.length - 1);
  render();
  preview(editing);
}

// Busy while the form loads: nothing may change what it shows (Start from, say) meanwhile.
async function editSetup() {
  requests += 1;
  render();
  try {
    await openWizard();
  } catch (err) {
    showError(err.message);
  } finally {
    requestDone();
  }
  render();
}

// Lengths are shown in the machine's units; millimetres until cncjs has reported them.
function units() {
  return cnc.units ?? 'mm';
}

function keepStep() {
  read($('setup-form'), STEPS[editing.step], editing.values, units());
}

async function nextStep() {
  reveal($('setup-form'));
  if (!$('setup-form').reportValidity()) {
    return;
  }
  keepStep();
  if (editing.step < STEPS.length - 1) {
    goToStep(editing.step + 1);
    return;
  }
  if (busy()) {
    return; // already saving
  }
  // A job may be running one of this session's programs; a new setup could discard it.
  if (cnc.workflow === 'running' || cnc.workflow === 'paused') {
    showError('A job is running in cncjs. Save the setup once it has ended.');
    render();
    return;
  }
  const wizard = editing;
  await act('save_setup', JSON.stringify(applicable(wizard.values)));
  if (editing !== wizard) {
    return; // cancelled, or another session opened, while the save was in flight
  }
  // On a refusal the reason is shown and the wizard stays open on what was typed.
  editing = $('error').hidden ? null : wizard;
  render();
}

function previousStep() {
  keepStep();
  goToStep(editing.step - 1);
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  // cncjs sandboxes widget frames without allow-downloads, so the cncjs page itself
  // (same origin) starts the download.
  const host = window.parent.document;
  const link = host.createElement('a');
  link.href = url;
  link.download = filename;
  host.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

// Both carry the report, whose lengths follow the machine's units (mm until known).
async function exportSession(name = current) {
  try {
    const bytes = await engine('export_zip', name, units());
    download(new Blob([bytes], { type: 'application/zip' }), `${name}.zip`);
    if (run?.name === name) {
      showError(`Exported ${name} without round ${run.round}: it is still running, and is stored once it ends.`);
    }
  } catch (err) {
    showError(err.message);
  }
}

async function downloadReport() {
  try {
    const page = await engine('report_html', current, units());
    download(new Blob([page], { type: 'text/html' }), `${current}-report.html`);
  } catch (err) {
    showError(err.message);
  }
}

// The widget is running a round of this session.
function jobOf(name) {
  return run !== null && run.name === name;
}

// Why a session cannot be deleted, reset or unseeded now; '' when it can. `phrase` says what
// to do later: "Delete the session".
function busyRefusal(name, phrase) {
  if (mic.recording) {
    return `A round is being recorded. ${phrase} after the job.`;
  }
  if (jobOf(name)) {
    return `A round of ${name} is running. ${phrase} after it.`;
  }
  return '';
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// Run a request that answers with the session's new state. On a refusal or failure the
// session shows as the engine has it now.
async function changeSession(name, method) {
  try {
    const after = await engine(method, name);
    if (name === current) {
      state = after;
      editing = null;
    }
  } catch (err) {
    if (name === current) {
      state = await engine('state', name).catch(() => state);
    }
    throw err;
  }
}

// Deleting removes the session; resetting discards its rounds and keeps its setup. Both ask
// first, naming what goes, and offer Export. They read nothing but the folder, so a session
// that does not load (from dechatter 0.1.x, or damaged) can go too. Don't start from removes
// a seed before round 1: the earlier session's cuts may not be in this browser to pick again.
const ACTIONS = {
  delete: {
    verb: 'Delete',
    phrase: 'Delete the session',
    question: (name, held) => `Delete session ${name}? It holds ${plural(held.rounds, 'round')} and ${plural(held.recordings, 'recording')}.`,
    note: 'Deleting cannot be undone. Export keeps a copy as a zip.',
    async run(name) {
      names = await engine('delete', name);
      await openSession('');
    },
  },
  reset: {
    verb: 'Reset',
    phrase: 'Reset the session',
    question: (name, held) => `Reset session ${name}? Its ${plural(held.rounds, 'round')} and ${plural(held.recordings, 'recording')} are discarded; its setup stays, and planning starts again from round 1.`,
    note: 'Resetting cannot be undone. Export keeps a copy as a zip.',
    run: (name) => changeSession(name, 'reset'),
  },
  unseed: {
    verb: "Don't start from it",
    get phrase() {
      return `Don't start from ${state?.seed?.source ?? 'the earlier session'}`;
    },
    // `seed` as shown when asked; null when the session does not load (its seed may be why).
    question: (name, held, seed) => (seed
      ? `Don't start ${name} from ${seed.source}? Its copy of ${seed.source}'s ${plural(seed.passes, 'analysed cut')} is removed, and round 1 is the coarse grid. Its setup stays as it is.`
      : `Don't start ${name} from the earlier session? Its seed.json, with that session's cuts, is removed. Its setup stays as it is.`),
    note: 'Start from can pick it again while it is in this browser, or Import it. Export keeps a copy as a zip.',
    // A session its seed kept from loading loads once the engine has answered its state.
    async run(name) {
      await changeSession(name, 'unseed');
      if (name === current) {
        setupStatus = 'loads';
      }
    },
  },
};

async function ask(action) {
  const name = current;
  const { verb, phrase, question, note } = ACTIONS[action];
  const refusal = busyRefusal(name, phrase);
  if (refusal) {
    showError(refusal);
    return;
  }
  dialog = 'preparing';
  render();
  try {
    // A session whose setup does not load (from dechatter 0.1.x, or damaged) cannot be
    // exported; one whose rounds alone do not load can.
    const loads = setupStatus === 'loads';
    const shown = state?.seed ?? null;
    $('confirm-question').textContent = question(name, await engine('contents', name), shown);
    $('confirm-note').textContent = loads ? note : `${note.split('.')[0]}. This session cannot be exported: its setup does not load (it is from dechatter 0.1.x, or damaged).`;
    $('confirm-export').hidden = !loads;
    $('confirm-go').textContent = verb;
    Object.assign($('confirm-dialog').dataset, { session: name, action });
    $('confirm-dialog').showModal();
    dialog = 'open';
  } catch (err) {
    dialog = 'closed';
    showError(err.message);
    render();
  }
}

async function confirmed() {
  const { session: name, action } = $('confirm-dialog').dataset;
  $('confirm-dialog').close();
  const refusal = busyRefusal(name, ACTIONS[action].phrase);
  if (refusal) {
    showError(refusal);
    return;
  }
  requests += 1;
  render();
  try {
    showError('');
    await ACTIONS[action].run(name);
  } catch (err) {
    showError(err.message);
  } finally {
    requestDone();
  }
  render();
}

async function importSession(file) {
  try {
    const name = await engine('import_zip', new Uint8Array(await file.arrayBuffer()));
    names = await engine('names');
    await openSession(name);
  } catch (err) {
    showError(err.message);
  }
  render();
}

// ---- microphone ------------------------------------------------------------------------

async function openMic(deviceId) {
  try {
    await mic.start(deviceId);
    localStorage.setItem(DEVICE_KEY, deviceId ?? '');
    const devices = await mic.devices();
    $('device').replaceChildren(
      el('option', { value: '' }, 'default microphone'),
      ...devices.map((d) => el('option', { value: d.id }, d.label || 'microphone')),
    );
    $('device').value = deviceId ?? '';
    showError('');
  } catch (err) {
    showError(`Microphone: ${err.message}. Allow microphone access for this site, then press Enable microphone.`);
  }
  render();
}

function renderLevel() {
  const level = mic.open ? mic.levelDb : null;
  $('level').value = level ?? -100;
  $('level-db').textContent = level === null ? (mic.open ? 'no signal: click anywhere in this panel' : '') : `${Math.round(level)} dB`;
  if (mic.recording) {
    const clock = $('recording-clock');
    if (clock) {
      clock.textContent = minutes(Math.round(mic.seconds));
    }
  }
}

// ---- cncjs -----------------------------------------------------------------------------

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error(`cannot load ${src}`));
    document.head.append(script);
  });
}

function note(event, detail) {
  if (timeline.length < TIMELINE_LIMIT) {
    timeline.push({ t: performance.now() / 1000, event, ...detail });
  }
}

function setPort(port) {
  // Another port may be another machine: its units are unknown until cncjs reports them.
  if (port !== cnc.port) {
    showIn(null);
    cnc.activeState = '';
    run?.stepper.disconnected();
  }
  cnc.port = port;
  cnc.link = port ? 'ready' : 'no-port';
  render();
}

function showIn(next) {
  if (next === cnc.units) {
    return;
  }
  if (editing) {
    keepStep();
  }
  cnc.units = next;
}

async function connectCncjs() {
  try {
    await loadScript(`${cncjsOrigin}/socket.io/socket.io.js`);
  } catch (err) {
    cnc.link = 'offline';
    showError(`${err.message}. Open this widget from inside cncjs.`);
    render();
    return;
  }
  const socket = window.io.connect(cncjsOrigin, { query: `token=${token}` });
  cnc.socket = socket;

  socket.on('connect', () => {
    cnc.link = cnc.port ? 'ready' : 'no-port';
    // Ask the cncjs page which port it has open; it answers with a "change" message.
    window.parent.postMessage({ token, action: { type: 'connect' } }, '*');
    render();
  });
  socket.on('disconnect', () => {
    cnc.link = 'offline';
    run?.stepper.disconnected(); // the machine finishes the one move it has, then stands
    render();
  });
  socket.on('error', (err) => {
    cnc.link = 'offline';
    showError(`cncjs refused the connection: ${err?.message ?? err}`);
    render();
  });
  socket.on('serialport:open', ({ port }) => setPort(port));
  socket.on('serialport:close', () => setPort(''));
  socket.on('serialport:read', (line) => {
    // A refused block ends the run: the stepper holds and resets. An alarm ends it too, but
    // the controller has stopped the machine itself: nothing is sent.
    const text = String(line);
    if (!run || !/^(error|ALARM):/.test(text)) {
      return;
    }
    note('controller', { line: text });
    if (text.startsWith('ALARM:')) {
      run.stepper.alarm(text);
      return;
    }
    run.stepper.error(text);
  });
  socket.on('controller:settings', (_type, settings) => {
    cnc.reportInches = String(settings?.settings?.$13 ?? '0') === '1';
  });
  socket.on('workflow:state', (workflow) => {
    cnc.workflow = workflow;
    // cncjs's own sender started a job while the widget runs a round: two senders on one
    // machine. The widget's round stops.
    if (workflow === 'running') {
      run?.stepper.fail('cncjs started a job of its own while the round ran');
    }
    render();
  });
  socket.on('controller:state', (_type, controller) => {
    showIn(MACHINE_UNITS[controller?.parserstate?.modal?.units] ?? cnc.units);
    const status = controller?.status ?? {};
    cnc.wpos = status.wpos ?? cnc.wpos;
    if (status.activeState !== cnc.activeState) {
      cnc.activeState = status.activeState ?? '';
      if (run) {
        note('controller', { state: cnc.activeState });
      }
    }
    cnc.subState = status.subState ?? null;
    run?.stepper.controller({ activeState: status.activeState, subState: cnc.subState, wpos: status.wpos });
    render();
  });
}

window.addEventListener('message', (event) => {
  const { token: theirs, action } = { ...event.data };
  if (theirs !== token || action?.type !== 'change') {
    return;
  }
  const port = action.payload?.port ?? '';
  if (port) {
    // Joining an open port makes cncjs replay its sender and workflow state to this socket.
    cnc.socket.emit('open', port, {});
  }
  setPort(port);
});

// ---- the widget runs the round --------------------------------------------------------

function plannedRound() {
  const last = state?.rounds.at(-1);
  return last && !last.analysed ? last : null;
}

// Why a round cannot start now; '' when it can.
function startRefusal() {
  if (run) {
    return 'A round is already running.';
  }
  if (!mic.open) {
    return 'Enable the microphone first.';
  }
  return machineRefusal();
}

// Why the machine cannot take a round now; '' when it can.
function machineRefusal() {
  if (cnc.link !== 'ready') {
    return 'Open the machine\'s port in cncjs first.';
  }
  if (cnc.units === null) {
    return 'cncjs has not reported G20 or G21 yet: say which the machine is in.';
  }
  if (cnc.workflow === 'running' || cnc.workflow === 'paused') {
    return 'cncjs is running a job of its own. Let it end, or stop it, first.';
  }
  if (cnc.activeState === 'Alarm') {
    return 'The machine is in alarm. Clear it in cncjs (home, or unlock) first.';
  }
  if (cnc.activeState === 'Hold' || cnc.activeState === 'Door') {
    return `The machine is in ${cnc.activeState}. Resume or reset it in cncjs first.`;
  }
  return '';
}

const command = (...args) => cnc.socket.emit('command', cnc.port, ...args);

// Record while the widget runs the round one block at a time; then analyse what ran.
async function startRound(round) {
  const refusal = startRefusal();
  if (refusal) {
    showError(refusal);
    return;
  }
  const name = current;
  // Before any wait: Start is now refused, and a lost link, an error or alarm line or a
  // job of cncjs's own while the round is prepared ends the stepper before it sends a thing.
  const stepper = new Stepper({
    io: {
      send: (lines) => command('gcode', lines),
      feedhold: () => command('feedhold'),
      cyclestart: () => command('cyclestart'),
      reset: () => command('reset'),
    },
    reportInches: cnc.reportInches,
    onChange: () => {
      if (stepper.phase === PHASE.DWELLING) {
        window.dispatchEvent(new CustomEvent('dechatter:dwell', { detail: { pass: stepper.block.pass_id } }));
      }
      render();
    },
    onWindow: ({ kind, ...window }) => note(kind, window),
  });
  // cncjs reports the controller only when it changes: start from what it said last.
  stepper.controller({ activeState: cnc.activeState, subState: cnc.subState, wpos: cnc.wpos });
  run = { stepper, round: round.round, name };
  render();
  let blocks;
  try {
    await mic.resume(); // capture has to be running before the first cut
    blocks = await engine('steps', name, round.round, cnc.units);
  } catch (err) {
    run = null;
    showError(err.message);
    render();
    return;
  }
  // The machine may have changed during those waits: check again before recording.
  if (notStarted(stepper)) {
    return;
  }
  timeline = [];
  mic.record();
  note('record', { round: round.round, units: cnc.units });
  window.dispatchEvent(new CustomEvent('dechatter:record', { detail: { round: round.round } }));
  if (!(await actOn(name, 'record_start', round.round))) {
    mic.finish();
    run = null;
    render();
    return;
  }
  // Once more: nothing has been sent, so a refusal now drops the recording unsaved.
  const late = notStartedReason(stepper);
  if (late) {
    mic.finish();
    await actOn(name, 'record_cancel');
    run = null;
    showError(late);
    render();
    return;
  }
  const outcome = await stepper.run(blocks);
  note('run', { outcome, reason: stepper.reason });
  const samples = mic.finish();
  run = null;
  await actOn(name, 'record_stop', samples, mic.rate, JSON.stringify(timeline));
  if (outcome === PHASE.FAILED) {
    // Both, when the analysis refused too: why the round ended, then why it was not analysed.
    const analysis = $('error').hidden ? '' : ` ${$('error').textContent}`;
    showError(`The round ended early: ${stepper.reason.replace(/\.$/, '')}. The cuts made before it count.${analysis}`);
  }
}

// Why a round about to start cannot: the machine now refuses, the microphone closed, or
// the stepper was ended (a lost link, an error or alarm line, a cncjs job) before it sent
// a block; '' if it can.
function notStartedReason(stepper) {
  if (stepper.finished) {
    return `The round did not start: ${stepper.reason.replace(/\.$/, '')}. Nothing was sent.`;
  }
  if (!mic.open) {
    return 'The round did not start: the microphone closed. Enable it, then press Start again.';
  }
  return machineRefusal();
}

// Whether the round cannot start after all; if so, says why and lets Start go again.
function notStarted(stepper) {
  const reason = notStartedReason(stepper);
  if (!reason) {
    return false;
  }
  run = null;
  showError(reason);
  render();
  return true;
}

// ---- rendering -------------------------------------------------------------------------

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function button(label, onclick, props = {}) {
  return el('button', { onclick, ...props }, label);
}

const LINK_TEXT = {
  connecting: 'connecting',
  'no-port': 'connected; open a serial port in cncjs',
  offline: 'not connected',
};

function renderLinks() {
  const job = cnc.workflow ?? 'state unknown';
  $('cncjs').textContent = cnc.link === 'ready' ? `${cnc.port}, ${job}` : LINK_TEXT[cnc.link];
  $('device').hidden = !mic.open;
  $('level').hidden = !mic.open;
  $('enable-mic').hidden = mic.open;
  $('mic-warning').hidden = mic.processing.length === 0;
  $('mic-warning').textContent = `The browser kept ${mic.processing.join(', ')} on. Verdicts may be wrong; try another microphone or browser.`;
}

function renderSessions() {
  const select = $('sessions');
  select.replaceChildren(
    el('option', { value: '' }, names.length ? 'choose a session' : 'no sessions yet'),
    ...names.map((name) => el('option', { value: name }, name)),
  );
  select.value = names.includes(current) ? current : '';
  // A run records into its own session: another one cannot be opened, made or imported.
  const pinned = busy() || dialog !== 'closed' || mic.recording || run !== null;
  select.disabled = pinned;
  $('new-session').disabled = pinned;
  $('import').disabled = pinned;
  $('summary').textContent = state ? summary(state) : '';
  $('edit-setup').disabled = !state || pinned;
  // Export reads only what is stored: it works while a round runs, without that round.
  $('export').disabled = setupStatus !== 'loads' || busy();
  // Also for a session that failed to load: deleting it is the way to be rid of it.
  for (const [id, action] of [['delete-session', 'delete'], ['reset-session', 'reset']]) {
    $(id).disabled = !names.includes(current) || busy() || Boolean(busyRefusal(current, ACTIONS[action].phrase)) || dialog !== 'closed';
  }
  // Reset keeps the setup, so the setup has to load; Delete is the way out otherwise.
  $('reset-session').disabled ||= setupStatus !== 'loads';
  // What a session starts from is chosen before its first round, and not while its wizard
  // is open: the wizard checks the tool assembly against it.
  const offered = seedAction();
  $('seed-session').hidden = offered === null;
  $('seed-session').textContent = offered === 'unseed' ? ACTIONS.unseed.phrase : 'Start from…';
  $('seed-session').disabled = pinned || editing !== null;
}

// What the button beside Setup offers: 'start' from an earlier session or 'unseed', before
// round 1; null otherwise. A seed that keeps the session from loading can still go; the
// example setup is filled in first.
function seedAction() {
  if (state === null) {
    return setupStatus === 'seed_refused' ? 'unseed' : null;
  }
  if (setupStatus !== 'loads' || state.rounds.length > 0) {
    return null;
  }
  if (state.seed) {
    return 'unseed';
  }
  return state.next === 'setup' ? null : 'start';
}

// The session's tool and cut in one line, lengths in the machine's units, the session it was
// started from, and how many values prepared ahead of time are still to be confirmed on the
// machine.
function summary({ summary: s, spindle, vary, pending, seed }) {
  const levels = s.levels_mm.map((level) => length(level, units())).join(', ');
  const cut = vary === 'depth' ? `width ${levels}, depth stepped` : `depth ${levels}`;
  const count = Object.keys(pending).length;
  const confirm = count === 0 ? '' : `. ${plural(count, 'value')} to confirm on the machine`;
  return `Ø ${length(s.diameter_mm, units())}, ${s.flutes} flutes, ${s.material}, ${cut}, ${chipLoad(s.chip_load_mm, units())} per tooth${spindle === 'manual' ? ', manual spindle' : ''}${seed ? `, started from ${seed.source}` : ''}${confirm}`;
}

// A setup field by its path ("stock.height_mm"), as the wizard names it and the step it is on.
function wizardField(at) {
  for (const step of STEPS) {
    const field = step.fields.find((each) => each.at === at);
    if (field) {
      return `${field.label} (${step.title})`;
    }
  }
  return at;
}

// What a session started from another plans first, or why it plans the grid after all;
// null when the usual text applies.
function seedPlanText() {
  const seed = state.seed;
  if (!seed) {
    return null;
  }
  if (state.next_planner === 'confirm') {
    const cuts = seed.transfer === 'speeds'
      ? `the round-1 cuts at the speeds of ${seed.source}'s best cuts only (its limits do not carry over to this kind of cut)`
      : `a few cuts around the best cut ${seed.source} predicts here`;
    return `Plan a short confirmation round instead of the coarse grid: ${cuts}, and, where ${seed.source} heard chatter within reach, one cut in its chatter band as a control. If they disagree with ${seed.source}, the grid follows.`;
  }
  if (state.next_planner !== 'grid') {
    return null;
  }
  if (seed.status === 'rejected') {
    return `The confirmation cuts disagreed with ${seed.source}: half or more of those it predicted came out the other way. Its cuts are left out of the map. Plan the coarse grid of test cuts.`;
  }
  if (seed.status === 'untested') {
    return `${seed.source} predicts no best cut to confirm here (none of its stable cuts lies inside this session's search and power cap): what it measured stays on the map. Plan a coarse grid of test cuts.`;
  }
  return `Nothing ${seed.source} measured lies inside this session's search, so it is not used. Plan a coarse grid of test cuts.`;
}

// A round's level, as the dimension its cuts share: "5 mm deep", or where the session steps
// the depth, "12.7 mm wide".
function held(level, options) {
  return `${length(level, units(), options)} ${state.vary === 'depth' ? 'wide' : 'deep'}`;
}

function minutes(seconds) {
  return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

function renderSetup() {
  $('setup').hidden = editing === null;
  if (editing === null) {
    $('setup-form').dataset.key = '';
    return;
  }
  const step = STEPS[editing.step];
  $('setup-title').textContent = `Setup ${editing.step + 1} of ${STEPS.length}: ${step.title}`;
  const kept = editing.seed && editing.step < 2 ? ` Started from ${editing.seed}: the machine, holder, cutter and stickout must stay as they were there.` : '';
  $('setup-hint').textContent = `${step.hint}${kept}`;
  $('setup-back').hidden = editing.step === 0;
  $('setup-next').textContent = editing.step === STEPS.length - 1 ? 'Save' : 'Next';
  $('setup-next').disabled = busy();
  $('units').textContent = UNITS_TEXT[cnc.units ?? 'unknown'];
  // Redraw the fields only when the step or the machine's units change, so typing is not
  // interrupted otherwise. A change of units drops an entry that is only half typed.
  const key = `${current}|${editing.step}|${units()}`;
  if ($('setup-form').dataset.key !== key) {
    draw($('setup-form'), step, editing.values, units(), editing);
    $('setup-form').dataset.key = key;
  }
}

// Planning on the wrong face position cuts air or too deep: the choice survives redraws.
function stockSelect() {
  const stock = el('select', { className: 'stock-choice' }, el('option', { value: 'same' }, 'same block'), el('option', { value: 'fresh' }, 'fresh block, face at Y0'));
  stock.value = stockChoice;
  stock.onchange = () => {
    stockChoice = stock.value;
    // Every stock choice on the page shows the one that planning uses.
    for (const other of document.querySelectorAll('select.stock-choice')) {
      other.value = stockChoice;
    }
  };
  return stock;
}

const STEP_TEXT = {
  setup: 'Setting up',
  rapid: 'Moving to the next pass',
  spindle: 'Setting the spindle speed',
  plunge: 'Plunging beside the stock',
  dwell: 'Spindle at speed in the air',
  cut: 'Cutting',
  face: 'Facing the top down',
  retract: 'Backing off',
  end: 'Finishing',
};

const HELD_LONG_S = 60;
const IDLE_AWAY_S = 5;

// Why the run seems stuck, pointing to Stop; '' while it is not.
function stall(stepper) {
  const now = performance.now() / 1000;
  if (stepper.phase === PHASE.HELD && now - stepper.holds.at(-1)[0] >= HELD_LONG_S) {
    return `Held for ${Math.round(now - stepper.holds.at(-1)[0])} s. Resume, or Stop the round: the cuts made so far are analysed.`;
  }
  if (stepper.idleAway !== null && now - stepper.idleAway >= IDLE_AWAY_S) {
    return 'The machine reports Idle, but not where this move ends. Check it in cncjs; if it will not move on, Stop the round: the cuts made so far are analysed.';
  }
  if (cnc.link !== 'ready') {
    return 'The connection to cncjs or the machine is lost.';
  }
  return '';
}

// While the widget runs a round: where it is, and Hold, Resume, Continue and Stop. The panel
// is built once per run and updated in place: cncjs reports the controller several times a
// second, and controls rebuilt that often would lose a click.
let runView = null;

function buildRunView(current) {
  const view = { run: current };
  view.title = el('p', { className: 'recording' }, '', el('span', { id: 'recording-clock' }, '0 s'));
  view.doing = el('p', {});
  view.ask = el('p', { className: 'instruction', hidden: true });
  view.askText = el('span', {});
  view.continue = button('Continue', () => current.stepper.continueSpindle(), { className: 'primary' });
  view.ask.append(view.askText, view.continue);
  view.hold = button('Hold', () => current.stepper.hold());
  view.resume = button('Resume', () => current.stepper.resume(), { className: 'primary' });
  view.stop = button('Stop', () => current.stepper.stop(), { className: 'danger' });
  view.stuck = el('p', { className: 'instruction', hidden: true });
  view.root = el('div', {},
    view.title,
    view.doing,
    view.ask,
    el('p', {}, view.hold, ' ', view.resume, ' ', view.stop),
    view.stuck,
    el('p', { className: 'muted' }, 'Stop if a cut gets violent: the cuts made so far still count. Feed hold in cncjs works too.'),
  );
  return view;
}

function updateRunView(view) {
  const { stepper, round } = view.run;
  view.title.firstChild.textContent = `Recording round ${round}: `;
  const block = stepper.block;
  const where = block?.pass_id === null || block?.pass_id === undefined ? '' : `, pass ${block.pass_id}`;
  const doing = stepper.phase === PHASE.HELD ? 'Held' : (STEP_TEXT[block?.step] ?? 'Starting');
  view.doing.textContent = `${doing}${where} (block ${Math.max((stepper.index ?? 0) + 1, 1)} of ${stepper.total || '…'}).`;
  const asking = stepper.phase === PHASE.SET_SPINDLE;
  view.ask.hidden = !asking;
  const stop = block?.stop ? ` (speed stop ${block.stop} of ${block.stops}, counting from the slowest)` : '';
  view.askText.textContent = asking ? `Set the spindle to ${block.rpm} rpm${stop}, then ` : '';
  view.hold.hidden = stepper.phase !== PHASE.MOVING;
  view.resume.hidden = stepper.phase !== PHASE.HELD;
  // Nothing moves before the first block: a double-clicked Start must not land on Stop.
  view.stop.disabled = stepper.phase === PHASE.READY || stepper.finished || stepper.phase === PHASE.STOPPING;
  const stuck = stall(stepper);
  view.stuck.hidden = !stuck;
  view.stuck.textContent = stuck;
}

function renderStep() {
  const step = $('step');
  step.hidden = !state || editing !== null;
  if (run && !step.hidden) {
    if (runView?.run !== run) {
      runView = buildRunView(run);
    }
    if (step.firstChild !== runView.root || step.childNodes.length !== 1) {
      step.replaceChildren(runView.root);
    }
    updateRunView(runView);
    return;
  }
  runView = null;
  step.replaceChildren();
  if (step.hidden) {
    return;
  }
  const round = plannedRound();

  if (busy()) {
    step.append(el('p', {}, 'Working…'));
    return;
  }

  if (state.discarded_round !== null) {
    step.append(el('p', {}, `Round ${state.discarded_round} had not been run and was planned from the old setup, so it was discarded.`));
  }

  if (state.next === 'converged') {
    step.append(
      el('p', {}, 'Converged: no further cut is expected to add removal rate. The best stable cuts are below.'),
      el('p', {}, button('Download the report', downloadReport, { className: 'primary' }), ' to print and keep with the tool.'),
    );
    return;
  }

  if (state.next === 'sweep') {
    const best = state.best[0];
    const stock = stockSelect();
    step.append(
      el('p', {}, 'Converged: no further cut is expected to add removal rate. The best stable cuts are below.'),
      el('p', {}, `One more round can find how hard the best cut (${bestSpeed(best)}, ${length(best.width_mm, units())} wide, ${length(best.depth_mm, units())} deep) can be pushed: the same cut at rising chip loads, asked for at ${best.ask_rpm} rpm, lightest first. Its cuts do not change the map.`),
      el('p', {}, button('Plan the chip-load sweep', async () => {
        if (await act('plan_sweep', stockChoice)) {
          stockChoice = 'same';
        }
      }, { className: 'primary' }), ' on the ', stock),
      ...(state.check ? checkOffer('secondary') : []),
      el('p', {}, button('Download the report', downloadReport), ' as it stands.'),
    );
    return;
  }

  if (state.next === 'check') {
    step.append(
      el('p', {}, 'Converged: no further cut is expected to add removal rate. The best stable cuts are below.'),
      ...checkOffer('primary'),
      el('p', {}, button('Download the report', downloadReport), ' as it stands.'),
    );
    return;
  }

  if (state.next === 'setup') {
    step.append(
      el('p', {}, 'This session still holds the example setup, not your machine. Fill it in before planning.'),
      el('p', {}, button('Open the setup wizard', editSetup, { className: 'primary' })),
    );
    return;
  }

  if (state.next === 'confirm') {
    step.append(
      el('p', {}, 'This setup was prepared ahead of time. Measure these on the machine and enter them in the setup wizard; saving it confirms them. Nothing is planned until then.'),
      el('ul', {}, ...Object.entries(state.pending).map(([at, text]) => el('li', {}, `${wizardField(at)}: ${text}`))),
    );
    // Start from left the placeholder stickout alone; the confirmed one must match.
    if (state.seed && 'tool.stickout_mm' in state.pending) {
      step.append(el('p', {}, `Started from ${state.seed.source}, whose stickout was ${length(state.seed.stickout_mm, units(), { mmToo: true })}: saving the wizard checks the stickout against it, and refuses one more than ${length(state.seed.stickout_tolerance_mm, units(), { mmToo: true })} off.`));
    }
    step.append(
      el('p', {}, button('Open the setup wizard', editSetup, { className: 'primary' })),
    );
    return;
  }

  if (state.next === 'plan') {
    const first = state.rounds.length === 0;
    const stock = stockSelect();
    const deep = state.vary === 'depth';
    const moved = !first && state.next_level_mm !== state.level_mm;
    const label = `Plan round ${state.rounds.length + 1}${moved ? ` at ${length(state.next_level_mm, units())} ${deep ? 'width' : 'depth'}` : ''}`;
    const plan = button(label, async () => {
      if (await act('plan', first ? 'same' : stockChoice)) {
        stockChoice = 'same';
      }
    }, { className: 'primary' });
    const settled = deep
      ? `${length(state.level_mm, units(), { mmToo: true })} width is settled. Next come cuts ${held(state.next_level_mm, { mmToo: true })}, starting from the depths that were stable.`
      : `${length(state.level_mm, units(), { mmToo: true })} depth is settled. Next come cuts ${held(state.next_level_mm, { mmToo: true })}, starting from the widths that were stable.`;
    const next = moved ? settled : 'Plan the next cuts from what was measured.';
    step.append(el('p', {}, seedPlanText() ?? (first ? 'Plan a coarse grid of test cuts.' : next)));
    step.append(el('p', {}, plan, ...(first ? [] : [' on the ', stock])));
    // A session an older dechatter converged may search on here: its check is on offer anyway.
    if (state.check) {
      step.append(
        el('p', { className: 'muted' }, `The map is still searching. A ${state.check.direction} check now repeats today's best cuts instead:`),
        ...checkOffer('secondary'),
      );
    }
    return;
  }

  const tests = round.passes.filter((p) => p.kind === 'test').length;
  const speedChanges = (r) => r.passes.filter((p, i) => i === 0 || p.rpm !== r.passes[i - 1].rpm).length;
  const refusal = startRefusal();
  const start = button('Start', () => startRound(round), { className: 'primary', disabled: Boolean(refusal) || busy() });
  // cncjs reports the mode from the controller's parser state. Until it does, the operator
  // can say which it is: the program sets that mode itself, so a wrong choice cannot
  // misplace a cut, only leave the machine in the other mode afterwards.
  const declare = el('select', {}, el('option', { value: '' }, 'machine units?'), el('option', { value: 'mm' }, 'mm (G21)'), el('option', { value: 'in' }, 'inches (G20)'));
  declare.onchange = () => {
    showIn(declare.value || null);
    render();
  };
  const unknownUnits = cnc.link === 'ready' && cnc.units === null;
  const [first, ...later] = round.legs;
  const midFacings = later.filter((leg) => leg.reface_mm > 0);
  const kind = round.planner === 'check' ? ` of the ${round.direction} check` : '';
  step.append(
    el('p', {}, `Round ${round.round}${kind}: ${tests} test cuts ${held(round.level_mm, { mmToo: true })}${round.milling === 'slot' ? ', each a full-width slot' : ''}, about ${minutes(round.run_time_s)} plus rapids. ${legsText(round)}`),
    el('ol', {},
      el('li', {}, first.reface_mm > 0
        ? `First the program faces the top down ${length(first.reface_mm, units(), { mmToo: true })}, to Z ${length(-first.top_mm, units(), { mmToo: true })}, and starts again at the first lane. Keep the zero the block was first cut with (X at the left end, Y on the front face as first mounted, Z on the top as first mounted). The facing is not analysed.`
        : first.top_mm > 0 || round.start === 'in use'
          ? 'Keep the zero the block was first cut with (X at the left end of the stock, Y on its front face as first mounted, Z on its top as first mounted).'
          : 'Zero X at the left end of the stock, Y on its front face, Z on top.'),
      ...(round.planner === 'check' ? [el('li', {}, `Every pass cuts ${round.direction}, feeding ${round.direction === 'climb' ? '−X from the right end' : '+X from the left end'} of the stock, the other way to the session's rounds; its lanes start from that end. Its verdicts do not change the map.`)] : []),
      ...(round.direction === 'climb' ? [el('li', {}, 'Climb milling needs a machine without backlash (ball screws). With backlash the cutter can pull the table into the cut: feed-hold at the first snatch.')] : []),
      el('li', {}, start, ' runs the round from here, one move at a time, and records it. Keep this tab open.', ...(unknownUnits ? [' cncjs has not reported G20 or G21 yet; the machine is in ', declare] : [])),
      ...midFacings.map((leg) => el('li', {}, `Before pass ${leg.first_pass}, as ${round.lanes > 1 ? 'the lanes left have' : round.milling === 'slot' ? 'the top has' : 'the face has'} no room for the next cut, the program faces the top down ${length(leg.reface_mm, units(), { mmToo: true })}, to Z ${length(-leg.top_mm, units(), { mmToo: true })}, and goes on at the first lane with the same zero${state.spindle === 'manual' ? '' : ', without stopping'}. The recording runs on; the facing is not analysed.`)),
      ...(round.order === 'deepest-first' ? [el('li', {}, 'The deepest cuts come first: stock is only clear down to the shallowest cut made in front of it. The first pass is the heaviest, so be ready to stop it.')] : []),
      ...(state.spindle === 'manual' ? [el('li', {}, `Before each of its ${speedChanges(round)} spindle speeds the round stops; set the speed shown here and press Continue.`)] : []),
    ),
  );
  if (refusal && !unknownUnits) {
    step.append(el('p', { className: 'muted' }, refusal));
  }
  if (round.has_recording) {
    step.append(el('p', {}, 'The last run could not be analysed. Run the round again, or ', button('Analyse the last recording again', () => act('analyze', round.round))));
  }
}

// The checks of the other direction on offer: a level to pick, each with its best cut and
// the sizes the check cuts, and the button that plans it on the block chosen.
// `emphasis` is 'primary' where the check is what comes next, else 'secondary'.
function checkOffer(emphasis) {
  const { direction, offers } = state.check;
  const u = units();
  const deep = state.vary === 'depth';
  const unit = u === 'in' ? 'in' : 'mm';
  const sizes = (offer) => offer.cuts_mm.map((mm) => length(mm, u).split(' ')[0]).join(', ');
  const pick = el('select', {}, ...offers.map((offer) => el('option', { value: String(offer.level_mm) }, `${held(offer.level_mm)}: ${offer.rpm} rpm, ${sizes(offer)} ${unit} ${deep ? 'deep' : 'wide'}`)));
  if (offers.some((offer) => String(offer.level_mm) === checkChoice)) {
    pick.value = checkChoice;
  }
  pick.onchange = () => {
    checkChoice = pick.value;
  };
  const stock = stockSelect();
  const plan = button(`Plan the ${direction} check`, async () => {
    if (await act('plan_check', stockChoice, Number(pick.value))) {
      stockChoice = 'same';
    }
  }, emphasis === 'primary' ? { className: 'primary' } : {});
  const order = deep ? 'deepest first, as the stock demands' : 'lightest first';
  return [
    el('p', {}, `A ${direction} check cuts a level's best cut ${direction} at its speed: lighter ${deep ? 'depths' : 'widths'} up to it, the cut itself and a step beyond (Advanced planner settings), ${order}. It answers whether that cut also holds ${direction}, as on the passes of a zigzag face that go the other way. Its verdicts do not change the map. One level per round; pick it here.`),
    el('p', {}, plan, ' at ', pick, ' on the ', stock),
    ...(direction === 'climb' ? [el('p', { className: 'muted' }, 'Climb milling needs a machine without backlash (ball screws).')] : []),
  ];
}

// Where on the stock a leg of a round cuts: its lane, when the stock is in lanes.
function legPlace(round, leg) {
  const slots = round.milling === 'slot' ? 'slots in ' : '';
  if (round.lanes === 1) {
    return slots ? `${slots}the top` : 'the front face';
  }
  const [x0, x1] = leg.lane_x_mm;
  return `${slots}lane ${leg.lane} of ${round.lanes} (X ${length(x0, units())} to ${length(x1, units())})`;
}

// Where along the stock a round cuts, leg by leg, and where it faces the top down.
function legsText(round) {
  const parts = round.legs.map((leg, i) => {
    const faces = `from Y ${length(leg.face_y_start_mm, units())} to ${length(leg.face_y_end_mm, units(), { mmToo: true })}`;
    if (i === 0) {
      return `It cuts ${legPlace(round, leg)} ${faces}`;
    }
    if (leg.reface_mm > 0) {
      return `then, from pass ${leg.first_pass}, ${legPlace(round, leg)} below the faced top ${faces}`;
    }
    return `then, from pass ${leg.first_pass}, ${legPlace(round, leg)} ${faces}`;
  });
  return `${parts.join('; ')}.`;
}

// A row before each leg of a round in more than one: where its passes cut.
function legRow(round, leg) {
  const faced = leg.reface_mm > 0 ? `Top faced down ${length(leg.reface_mm, units())}, then ` : '';
  const place = legPlace(round, leg);
  const text = `${faced}${faced ? place : place[0].toUpperCase() + place.slice(1)}, from Y ${length(leg.face_y_start_mm, units())}`;
  return el('tr', { className: 'leg' }, el('td', { colSpan: 6 }, text));
}

function verdictCell(round, pass) {
  if (pass.kind !== 'test') {
    return el('td', {}, pass.kind);
  }
  if (!round.analysed) {
    return el('td', {}, '');
  }
  if (pass.status === 'not run' && !pass.verdict) {
    return el('td', { className: 'muted' }, 'not run');
  }
  const select = el('select', { disabled: busy() },
    ...(pass.verdict ? [] : [el('option', { value: '' }, pass.status === 'likely not run' || pass.status === 'no cut heard' ? pass.status : 'not heard')]),
    el('option', { value: 'stable' }, pass.status === 'aborted' ? 'stable (aborted)' : 'stable'),
    el('option', { value: 'unstable' }, pass.status === 'aborted' ? 'chatter (aborted)' : 'chatter'),
  );
  select.value = pass.verdict ?? '';
  const quiet = pass.over_air_db === null
    ? 'Nothing could be analysed where the run cut it (silent or too short). The face still moved on.'
    : `No louder than the spindle in the air (${pass.over_air_db} dB over it): it may have cut nothing. Was the face where the plan put it?`;
  select.title = pass.status === 'no cut heard'
    ? quiet
    : pass.chatter_hz ? `peak ${Math.round(pass.chatter_hz)} Hz, ${pass.prominence_db} dB over the floor` : '';
  select.onchange = () => act('set_verdict', round.round, pass.id, select.value);
  // The spindle speed heard sits at the edge of the search around this pass's: the sound
  // may be another pass's. Worth a listen before trusting the verdict.
  const edge = pass.spindle_fit === 'edge'
    ? [el('span', { className: 'muted', title: 'The spindle speed heard is at the edge of what this pass could be: check it.' }, ' speed?')]
    : [];
  return el('td', { className: pass.verdict ?? '' }, select, ...edge);
}

function renderRound() {
  const round = editing === null ? state?.rounds.at(-1) : null;
  $('round').hidden = !round;
  if (!round) {
    return;
  }
  const kind = { sweep: ' (chip-load sweep)', confirm: ' (confirmation)', check: ` (${round.direction} check)` }[round.planner] ?? '';
  const lanes = round.legs.map((leg) => leg.lane);
  const where = round.lanes > 1 ? `, ${lanes.length > 1 ? 'lanes' : 'lane'} ${lanes.join(', ')} of ${round.lanes}` : '';
  const faced = round.legs.slice(1).some((leg) => leg.reface_mm > 0) ? ', top faced midway' : '';
  $('round-title').textContent = `Round ${round.round}${kind}${round.analysed ? ' results' : ''}, ${held(round.level_mm, { mmToo: true })}${where}${faced}`;
  const deep = state.vary === 'depth';
  $('passes-size').textContent = deep ? 'depth' : 'width';
  const starts = new Map(round.legs.length > 1 ? round.legs.map((leg) => [leg.first_pass, leg]) : []);
  $('passes').replaceChildren(
    ...round.passes.flatMap((p) => [
      ...(starts.has(p.id) ? [legRow(round, starts.get(p.id))] : []),
      el('tr', { className: p.kind === 'test' ? '' : 'prep' },
        el('td', {}, String(p.id)),
        speedCell(round, p),
        el('td', {}, length(deep ? p.depth_mm : p.width_mm, units())),
        el('td', {}, feed(p.feed_mm_min, units())),
        el('td', {}, p.expected === 'unstable' ? 'control' : p.p_stable === null ? '' : p.p_stable.toFixed(2)),
        verdictCell(round, p),
      ),
    ]),
  );
}

function renderMap() {
  // A level that has no cuts of its own yet still has a map: what deeper (or wider) cuts
  // showed stable. Its rows are the dimension the session steps.
  const seeded = state?.seeded ?? [];
  $('map-section').hidden = editing !== null || !state || !(state.rounds.some((r) => r.analysed) || seeded.length);
  if ($('map-section').hidden) {
    return;
  }
  $('map-title').textContent = state.vary === 'depth'
    ? `Stability map at ${length(state.level_mm, units(), { mmToo: true })} width: depth against speed`
    : `Stability map at ${length(state.level_mm, units(), { mmToo: true })} depth`;
  const { speeds, engagements, p } = state.map;
  const canvas = $('map');
  const ctx = canvas.getContext('2d');
  const pad = { left: units() === 'in' ? 56 : 40, bottom: 16, top: 4, right: 4 };
  const w = canvas.width - pad.left - pad.right;
  const h = canvas.height - pad.top - pad.bottom;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const cellW = w / speeds.length;
  const cellH = h / engagements.length;
  speeds.forEach((_, s) => {
    engagements.forEach((_, i) => {
      ctx.fillStyle = `hsl(${Math.round(120 * p[s][i])} 65% 60%)`;
      ctx.fillRect(pad.left + s * cellW, pad.top + h - (i + 1) * cellH, cellW + 0.5, cellH + 0.5);
    });
  });

  // A spindle that ran off the speed asked can be heard just beyond the speeds searched: its
  // cut is drawn at the edge.
  const x = (rpm) => pad.left + ((Math.min(Math.max(rpm, speeds[0]), speeds.at(-1)) - speeds[0]) / (speeds.at(-1) - speeds[0])) * (w - cellW) + cellW / 2;
  const y = (size) => pad.top + h - ((size - engagements[0]) / (engagements.at(-1) - engagements[0])) * (h - cellH) - cellH / 2;
  // Carried over from the session this one was started from: not cut here, so drawn apart,
  // as small squares (stable) and plus signs (chatter).
  ctx.lineWidth = 1;
  ctx.strokeStyle = '#555';
  for (const o of seeded) {
    const [cx, cy] = [x(o.rpm), y(o.engagement_mm)];
    ctx.beginPath();
    if (o.verdict === 'stable') {
      ctx.rect(cx - 2, cy - 2, 4, 4);
    } else {
      ctx.moveTo(cx - 3, cy);
      ctx.lineTo(cx + 3, cy);
      ctx.moveTo(cx, cy - 3);
      ctx.lineTo(cx, cy + 3);
    }
    ctx.stroke();
  }
  $('seed-legend').hidden = seeded.length === 0;
  $('seed-legend').textContent = seeded.length ? ` Squares and plus signs are seeded from ${state.seed.source}: its cuts as they carry over, not cut here, and counted for less.` : '';
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = '#000';
  for (const o of state.observations) {
    const [cx, cy] = [x(o.rpm), y(o.engagement_mm)];
    ctx.beginPath();
    if (o.verdict === 'stable') {
      ctx.fillStyle = '#fff';
      ctx.arc(cx, cy, 3, 0, 2 * Math.PI);
      ctx.fill();
    } else {
      ctx.moveTo(cx - 3, cy - 3);
      ctx.lineTo(cx + 3, cy + 3);
      ctx.moveTo(cx + 3, cy - 3);
      ctx.lineTo(cx - 3, cy + 3);
    }
    ctx.stroke();
  }

  ctx.fillStyle = '#777';
  ctx.font = '10px Arial';
  ctx.textBaseline = 'top';
  ctx.textAlign = 'left';
  ctx.fillText(`${speeds[0]} rpm`, pad.left, pad.top + h + 3);
  ctx.textAlign = 'right';
  ctx.fillText(String(speeds.at(-1)), pad.left + w, pad.top + h + 3);
  ctx.fillText(length(engagements.at(-1), units()).split(' ')[0], pad.left - 3, pad.top);
  ctx.textBaseline = 'bottom';
  ctx.fillText(length(engagements[0], units()), pad.left - 3, pad.top + h);
}

// A pass's speed: the one asked for, then the one heard in its cut where they differ. A test
// cut with a verdict goes on the map at the speed heard; where none was heard reliably, at
// the one asked for. A chip-load sweep's cuts stay off the map.
function speedCell(round, pass) {
  const heard = pass.heard_rpm === null ? null : Math.round(pass.heard_rpm);
  const mapped = pass.kind === 'test' && pass.verdict !== null && round.planner !== 'sweep';
  if (heard !== null && heard !== pass.rpm) {
    const use = mapped ? ': the map and the results use the speed heard' : '';
    return el('td', { title: `Asked for ${pass.rpm} rpm, heard ${heard} rpm in the cut${use}.` }, `${pass.rpm} → ${heard}`);
  }
  if (heard === null && mapped) {
    return el('td', { title: 'No spindle speed heard reliably in this cut: the map uses the speed asked for.' }, String(pass.rpm));
  }
  return el('td', {}, String(pass.rpm));
}

// A stable cut's speed as heard, with the speed asked for beside it where they differ, and
// the speed to set where that is another (speed stops changed since the cut).
function bestSpeed(best) {
  const heard = Math.round(best.rpm);
  const asked = Math.round(best.asked_rpm);
  const set = Math.round(best.ask_rpm) === asked ? '' : `; set ${Math.round(best.ask_rpm)}`;
  if (best.speed !== 'heard') {
    return `${asked} rpm asked, none heard${set}`;
  }
  return (heard === asked ? `${heard} rpm` : `${heard} rpm heard, asked ${asked}`) + set;
}

function renderBest() {
  const best = editing === null ? (state?.best ?? []) : [];
  $('best-section').hidden = best.length === 0;
  $('best').replaceChildren(
    ...best.map((b) =>
      el('tr', {},
        el('td', {}, bestSpeed(b)),
        el('td', {}, length(b.width_mm, units())),
        el('td', {}, length(b.depth_mm, units())),
        el('td', {}, feed(b.feed_mm_min, units())),
        el('td', {}, rate(b.mrr_cm3_min, units())),
      ),
    ),
  );
  const sweep = state?.sweep;
  $('sweep-result').hidden = !sweep || best.length === 0;
  renderChecks(best.length === 0 ? [] : state?.checks ?? []);
  if (!sweep) {
    return;
  }
  const u = units();
  const heard = sweep.heard_rpm === null ? '' : ` (heard ${Math.round(sweep.heard_rpm)})`;
  const where = `at ${sweep.rpm} rpm${heard}, ${length(sweep.width_mm, u)} wide and ${length(sweep.depth_mm, u)} deep`;
  $('sweep-result').textContent = sweep.chip_load_mm === null
    ? `Chip-load sweep: ${where}, even its lightest pass chattered. Keep the session's ${chipLoad(sweep.session_chip_load_mm, u, { mmToo: true })} per tooth.`
    : `Chip-load sweep: ${where}, the cut stayed stable ${sweep.untopped ? 'at every chip load tried, up to' : 'up to'} ${chipLoad(sweep.chip_load_mm, u, { mmToo: true })} per tooth (the session used ${chipLoad(sweep.session_chip_load_mm, u)}): feed ${feed(sweep.feed_mm_min, u, { mmToo: true })}, ${rate(sweep.mrr_cm3_min, u, { mmToo: true })}.${sweep.untopped ? ' None chattered, so its limit lies higher.' : ''}`;
}

const HEARD = { stable: 'held', unstable: 'chatter' };
// The statuses of a pass the run never cut, as the engine's session.UNHEARD lists them.
const UNHEARD = ['missing', 'likely not run', 'not run'];

// One table per check of the other direction: which of its cuts held, apart from the map.
function renderChecks(checks) {
  const u = units();
  const deep = state?.vary === 'depth';
  $('check-results').hidden = checks.length === 0;
  $('check-results').replaceChildren(
    ...checks.flatMap((check) => {
      const size = (cut) => length(deep ? cut.depth_mm : cut.width_mm, u);
      const reach = check.held_mm === null
        ? check.cuts.some((cut) => cut.verdict === 'stable')
          ? `the lightest cut run did not hold ${check.direction} or was not heard, so none counts as held`
          : `no cut was heard to hold ${check.direction}`
        : `held ${check.direction} up to ${length(check.held_mm, u, { mmToo: true })} ${deep ? 'deep' : 'wide'}`;
      return [
        el('h3', {}, `${check.direction[0].toUpperCase()}${check.direction.slice(1)} check, round ${check.round}, ${held(check.level_mm)}: ${reach}`),
        el('table', {},
          el('thead', {}, el('tr', {}, ...['rpm', deep ? 'depth' : 'width', 'feed', 'removal', check.direction].map((h) => el('th', {}, h)))),
          el('tbody', {}, ...check.cuts.map((cut) => el('tr', {},
            el('td', {}, String(cut.rpm)),
            el('td', {}, size(cut)),
            el('td', {}, feed(cut.feed_mm_min, u)),
            el('td', {}, rate(cut.mrr_cm3_min, u)),
            el('td', { className: cut.verdict ?? '' }, cut.verdict ? HEARD[cut.verdict] : UNHEARD.includes(cut.status) ? 'not run' : 'not heard'),
          ))),
        ),
      ];
    }),
  );
}

function render() {
  $('boot').hidden = boot === null && !fatal;
  $('boot').className = fatal ? 'error' : 'muted';
  $('boot').textContent = fatal || (boot === null ? '' : `${boot}. The first load takes a few seconds.`);
  $('main').hidden = boot !== null || Boolean(fatal);
  if ($('main').hidden) {
    return;
  }
  renderSessions();
  renderLinks();
  renderNew();
  renderSetup();
  renderStep();
  renderRound();
  renderMap();
  renderBest();
}

// ---- wiring ----------------------------------------------------------------------------

$('sessions').onchange = () => openSession($('sessions').value);
$('new-session').onclick = newSession;
$('new-form').onsubmit = (event) => {
  event.preventDefault();
  createSession();
};
$('new-cancel').onclick = () => $('new-dialog').close();
// Escape closes an open (i) bubble first, not the dialog with it; and nothing closes the
// dialog while it waits for an answer. Heard on the page, wherever the focus is.
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !$('new-dialog').open) {
    return;
  }
  if (busy() || $('new-dialog').querySelector('.bubble:not([hidden])')) {
    event.preventDefault();
  }
}, true);
$('new-dialog').addEventListener('cancel', (event) => {
  if (busy()) {
    event.preventDefault();
  }
});
$('new-dialog').addEventListener('close', () => {
  closeInfo();
  dialog = 'closed';
  render();
});
$('new-import').onclick = () => {
  importing = 'seed';
  $('import-file').click();
};
$('new-from-row').append(...Object.values(info('new-from', 'Start from', START_FROM)));
$('edit-setup').onclick = editSetup;
$('seed-session').onclick = () => (seedAction() === 'unseed' ? ask('unseed') : startFrom());
$('setup-back').onclick = previousStep;
$('setup-form').onsubmit = (event) => {
  event.preventDefault();
  nextStep();
};
$('cancel-setup').onclick = () => {
  editing = null;
  showError('');
  // A session just created has no state yet: show it as it stands, on the setup it holds.
  if (state === null && current) {
    act('state');
  }
  render();
};
$('export').onclick = () => exportSession();
$('delete-session').onclick = () => ask('delete');
$('reset-session').onclick = () => ask('reset');
// The page's error line is behind the dialog: say there how the export went.
$('confirm-dialog').addEventListener('close', () => {
  dialog = 'closed';
  render();
});
$('confirm-export').onclick = async () => {
  showError('');
  await exportSession($('confirm-dialog').dataset.session);
  const { phrase } = ACTIONS[$('confirm-dialog').dataset.action];
  $('confirm-note').textContent = $('error').hidden ? `Exported. ${phrase} now, or cancel.` : `Not exported: ${$('error').textContent}`;
};
$('confirm-go').onclick = confirmed;
$('confirm-cancel').onclick = () => $('confirm-dialog').close();
$('download-report').onclick = downloadReport;
$('import').onclick = () => {
  importing = 'open';
  $('import-file').click();
};
$('import-file').onchange = () => {
  const [file] = $('import-file').files;
  $('import-file').value = '';
  if (!file) {
    return;
  }
  if (importing === 'seed') {
    importSource(file);
    return;
  }
  importSession(file);
};
$('enable-mic').onclick = () => openMic(localStorage.getItem(DEVICE_KEY) || undefined);
$('device').onchange = () => openMic($('device').value || undefined);

// Tell the cncjs page how tall the widget wants to be.
new ResizeObserver(() => {
  const { scrollHeight, scrollWidth } = document.body;
  window.parent.postMessage({ token, action: { type: 'resize', payload: { scrollHeight, scrollWidth } } }, '*');
}).observe(document.body);

// Browsers can hold audio capture back until the user interacts with the page, for instance
// after a reload. Any click, tap or key in the widget lets it run. Touch only counts as
// interaction when the finger lifts, hence pointerup as well.
for (const type of ['pointerdown', 'pointerup', 'keydown']) {
  document.addEventListener(type, () => {
    mic.resume().catch((err) => console.warn(`dechatter: microphone did not resume: ${err.message}`));
  });
}

// Leaving mid-recording loses the run: the samples are only in this page.
// Leaving mid-round loses the recording; the machine finishes its one move and stands.
window.addEventListener('beforeunload', (event) => {
  if (run || mic.recording) {
    event.preventDefault();
  }
});

render();
connectCncjs();
openMic(localStorage.getItem(DEVICE_KEY) || undefined);
setInterval(renderLevel, LEVEL_MS);
// cncjs reports only changes: a run that stalls gets no news, so its panel is redrawn anyway.
setInterval(() => run && renderStep(), 1000);

try {
  names = await engine('names');
  if (!names.includes(current)) {
    current = '';
  }
  await openSession(current);
} catch (err) {
  showError(err.message);
}
