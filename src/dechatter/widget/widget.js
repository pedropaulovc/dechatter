// dechatter cncjs widget. Everything runs in this page: cncjs is reached over its socket.io
// API (load the program, watch the job), the microphone is the browser's, and the planning
// and analysis are dechatter's Python running in a worker (engine.js).

import { Mic } from './mic.js';
import { STEPS, draw, read } from './wizard.js';

const params = new URLSearchParams(location.search);
const token = params.get('token') ?? '';
const cncjsOrigin = params.get('host') ?? location.origin;

const TIMELINE_LIMIT = 5000;
const LEVEL_MS = 250;
const SESSION_KEY = 'dechatter.session';
const DEVICE_KEY = 'dechatter.microphone';
const UNITS_KEY = 'dechatter.units';
const $ = (id) => document.getElementById(id);

const cnc = {
  socket: null,
  link: 'connecting', // connecting | no-port | ready | offline
  port: '',
  workflow: null, // idle | running | paused; null until cncjs reports it
  program: '', // name of the G-code file cncjs has loaded
  pause: null, // { text, settled } while the program waits at an M0, else null
};
const mic = new Mic();
let boot = 'Starting'; // what the engine is loading; null once it is ready
let fatal = ''; // why the engine could not start; nothing works without it
let names = []; // sessions stored in this browser
let current = localStorage.getItem(SESSION_KEY) ?? '';
let state = null; // the current session, as the engine last reported it
let editing = null; // { values, materials, step } while the setup wizard is open, else null
let units = localStorage.getItem(UNITS_KEY) ?? 'mm'; // how the wizard shows lengths: mm | in
let timeline = []; // what cncjs reported since the job last started; saved with the recording
let busy = false; // an engine request is in flight
let stockChoice = 'same'; // survives redraws: planning on the wrong face position cuts air or too deep

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

// Run one request that answers with the session's new state.
async function act(method, ...args) {
  busy = true;
  render();
  try {
    state = await engine(method, current, ...args);
    showError('');
  } catch (err) {
    showError(err.message);
  } finally {
    busy = false;
    syncRecording();
    render();
  }
}

async function openSession(name) {
  current = name;
  editing = null;
  state = null;
  localStorage.setItem(SESSION_KEY, name);
  if (name) {
    await act('state');
  }
  render();
}

async function newSession() {
  const name = window.prompt('Name this tool assembly, for example al-10mm-3fl');
  if (!name) {
    return;
  }
  try {
    await engine('create', name.trim());
    names = await engine('names');
    current = name.trim();
    localStorage.setItem(SESSION_KEY, current);
    state = null;
    editing = { ...(await engine('setup_form', current)), step: 0 };
    showError('');
  } catch (err) {
    showError(err.message);
  }
  render();
}

async function editSetup() {
  try {
    editing = { ...(await engine('setup_form', current)), step: 0 };
  } catch (err) {
    showError(err.message);
  }
  render();
}

function keepStep() {
  read($('setup-form'), STEPS[editing.step], editing.values, units);
}

async function nextStep() {
  if (!$('setup-form').reportValidity()) {
    return;
  }
  keepStep();
  if (editing.step < STEPS.length - 1) {
    editing.step += 1;
    render();
    return;
  }
  const wizard = editing;
  await act('save_setup', JSON.stringify(wizard.values));
  if (editing !== wizard) {
    return; // cancelled, or another session opened, while the save was in flight
  }
  // On a refusal the reason is shown and the wizard stays open on what was typed.
  editing = $('error').hidden ? null : wizard;
  render();
}

function previousStep() {
  keepStep();
  editing.step -= 1;
  render();
}

function changeUnits(next) {
  keepStep();
  units = next;
  localStorage.setItem(UNITS_KEY, units);
  render();
}

async function exportSession() {
  try {
    const bytes = await engine('export_zip', current);
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
    // cncjs sandboxes widget frames without allow-downloads, so the cncjs page itself
    // (same origin) starts the download.
    const host = window.parent.document;
    const link = host.createElement('a');
    link.href = url;
    link.download = `${current}.zip`;
    host.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch (err) {
    showError(err.message);
  }
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
    timeline.push({ t: Date.now() / 1000, event, ...detail });
  }
}

function setPort(port) {
  cnc.port = port;
  cnc.link = port ? 'ready' : 'no-port';
  render();
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
    render();
  });
  socket.on('error', (err) => {
    cnc.link = 'offline';
    showError(`cncjs refused the connection: ${err?.message ?? err}`);
    render();
  });
  socket.on('serialport:open', ({ port }) => setPort(port));
  socket.on('serialport:close', () => setPort(''));
  socket.on('gcode:load', (name) => {
    cnc.program = name;
    render();
  });
  socket.on('gcode:unload', () => {
    cnc.program = '';
    render();
  });
  socket.on('sender:status', (status) => {
    cnc.program = status.name ?? cnc.program;
    note('sender', { sent: status.sent, received: status.received, total: status.total });
    // A manual-spindle program stops at "M0 (set the spindle to N rpm, then resume)". cncjs
    // reports the pause as soon as it sends the line; the machine has only stopped once
    // everything sent was acknowledged.
    const reason = status.hold ? status.holdReason : null;
    const pause = reason?.data === 'M0'
      ? { text: /\(([^)]*)\)/.exec(reason.msg ?? '')?.[1] ?? 'program pause', settled: status.received >= status.sent }
      : null;
    if (JSON.stringify(pause) !== JSON.stringify(cnc.pause)) {
      cnc.pause = pause;
      render();
    }
  });
  socket.on('workflow:state', (workflow) => {
    if (workflow === 'running' && cnc.workflow !== 'paused') {
      timeline = [];
    }
    cnc.workflow = workflow;
    note('workflow', { state: workflow });
    syncRecording();
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

// ---- recording follows the job ---------------------------------------------------------

function plannedRound() {
  const last = state?.rounds.at(-1);
  return last && !last.analysed ? last : null;
}

// Record exactly while cncjs runs the planned round's program.
function syncRecording() {
  if (!state || busy) {
    return;
  }
  const round = plannedRound();
  if (!mic.recording && round && cnc.workflow === 'running' && cnc.program === round.program) {
    if (!mic.open) {
      showError('The job is running but the microphone is not open, so this run cannot be analysed. Enable the microphone and run the round again.');
      return;
    }
    mic.record();
    act('record_start', round.round);
    return;
  }
  if (mic.recording && cnc.workflow === 'idle' && cnc.link === 'ready') {
    const samples = mic.finish();
    act('record_stop', samples, mic.rate, JSON.stringify(timeline));
  }
}

async function loadProgram(round) {
  busy = true;
  render();
  try {
    // Capture has to be running before the job can start; the listener below does not wait.
    await mic.resume();
    const gcode = await engine('program', current, round.round);
    await new Promise((resolve, reject) => {
      cnc.socket.emit('command', cnc.port, 'gcode:load', round.program, gcode, {}, (err) =>
        err ? reject(new Error(`cncjs could not load the program: ${err.message ?? err}`)) : resolve(),
      );
    });
    cnc.program = round.program;
    showError('');
  } catch (err) {
    showError(err.message);
  } finally {
    busy = false;
    render();
  }
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
  select.disabled = busy || mic.recording;
  $('summary').textContent = state?.summary ?? '';
  for (const id of ['edit-setup', 'export']) {
    $(id).disabled = !state || busy || mic.recording;
  }
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
  $('setup-hint').textContent = step.hint;
  $('setup-back').hidden = editing.step === 0;
  $('setup-next').textContent = editing.step === STEPS.length - 1 ? 'Save' : 'Next';
  $('setup-next').disabled = busy;
  $('units').value = units;
  // Redraw the fields only when the step or units change, so typing is never interrupted.
  const key = `${current}|${editing.step}|${units}`;
  if ($('setup-form').dataset.key !== key) {
    draw($('setup-form'), step, editing.values, units, editing.materials);
    $('setup-form').dataset.key = key;
  }
}

function renderStep() {
  const step = $('step');
  step.replaceChildren();
  step.hidden = !state || editing !== null;
  if (step.hidden) {
    return;
  }
  const round = plannedRound();

  if (mic.recording) {
    step.append(
      el('p', { className: 'recording' }, `Recording round ${state.recording_round ?? ''}: `, el('span', { id: 'recording-clock' }, '0 s')),
      el('p', {}, 'Feed-hold and stop the job in cncjs if a cut gets violent. The passes that ran still count.'),
      ...(cnc.pause ? [el('p', { className: 'instruction' }, cnc.pause.settled ? `Now ${cnc.pause.text} in cncjs.` : 'Wait for the machine to stop.')] : []),
    );
    return;
  }

  if (busy) {
    step.append(el('p', {}, 'Working…'));
    return;
  }

  if (state.next === 'converged') {
    step.append(el('p', {}, 'Converged: no further cut is expected to add removal rate. The best stable cuts are below.'));
    return;
  }

  if (state.next === 'plan') {
    const first = state.rounds.length === 0;
    const stock = el('select', {}, el('option', { value: 'same' }, 'same block'), el('option', { value: 'fresh' }, 'fresh block, face at Y0'));
    stock.value = stockChoice;
    stock.onchange = () => {
      stockChoice = stock.value;
    };
    const plan = button(first ? 'Plan round 1' : `Plan round ${state.rounds.length + 1}`, async () => {
      await act('plan', first ? 'same' : stockChoice);
      stockChoice = 'same';
    }, { className: 'primary' });
    step.append(el('p', {}, first ? 'Plan a coarse grid of test cuts.' : 'Plan the next cuts from what was measured.'));
    step.append(el('p', {}, plan, ...(first ? [] : [' on the ', stock])));
    return;
  }

  const tests = round.passes.filter((p) => p.kind === 'test').length;
  const speedChanges = (r) => r.passes.filter((p, i) => i === 0 || p.rpm !== r.passes[i - 1].rpm).length;
  const loaded = cnc.program === round.program;
  const ready = cnc.link === 'ready' && cnc.workflow === 'idle' && mic.open;
  const load = button(loaded ? 'Reload program' : 'Load into cncjs', () => loadProgram(round), { className: loaded ? '' : 'primary', disabled: !ready });
  step.append(
    el('p', {}, `Round ${round.round}: ${tests} test cuts, about ${minutes(round.run_time_s)} plus rapids. The front face ends at Y${round.face_y_end_mm}.`),
    el('ol', {},
      el('li', {}, 'Zero X at the left end of the stock, Y on its front face, Z on top.'),
      el('li', {}, load, mic.open ? '' : ' (enable the microphone first)'),
      el('li', {}, loaded ? 'Press play in cncjs. Recording starts and stops with the job. Keep this tab open.' : 'Press play in cncjs.'),
      ...(state.spindle === 'manual' ? [el('li', {}, `The program pauses before each of its ${speedChanges(round)} spindle speeds. Set the speed shown here, then resume in cncjs.`)] : []),
    ),
  );
  if (round.has_recording) {
    step.append(el('p', {}, 'The last run could not be analysed. Run the round again, or ', button('Analyse the last recording again', () => act('analyze', round.round))));
  }
}

function verdictCell(round, pass) {
  if (pass.kind !== 'test') {
    return el('td', {}, pass.kind);
  }
  if (!round.analysed) {
    return el('td', {}, '');
  }
  const select = el('select', { disabled: busy },
    ...(pass.verdict ? [] : [el('option', { value: '' }, 'not heard')]),
    el('option', { value: 'stable' }, pass.status === 'aborted' ? 'stable (aborted)' : 'stable'),
    el('option', { value: 'unstable' }, pass.status === 'aborted' ? 'chatter (aborted)' : 'chatter'),
  );
  select.value = pass.verdict ?? '';
  select.title = pass.chatter_hz ? `peak ${Math.round(pass.chatter_hz)} Hz, ${pass.prominence_db} dB over the floor` : '';
  select.onchange = () => act('set_verdict', round.round, pass.id, select.value);
  return el('td', { className: pass.verdict ?? '' }, select);
}

function renderRound() {
  const round = editing === null ? state?.rounds.at(-1) : null;
  $('round').hidden = !round;
  if (!round) {
    return;
  }
  $('round-title').textContent = `Round ${round.round}${round.analysed ? ' results' : ''}`;
  $('passes').replaceChildren(
    ...round.passes.map((p) =>
      el('tr', { className: p.kind === 'test' ? '' : 'prep' },
        el('td', {}, String(p.id)),
        el('td', {}, String(p.rpm)),
        el('td', {}, p.width_mm.toFixed(2)),
        el('td', {}, String(Math.round(p.feed_mm_min))),
        el('td', {}, p.p_stable === null ? '' : p.p_stable.toFixed(2)),
        verdictCell(round, p),
      ),
    ),
  );
}

function renderMap() {
  $('map-section').hidden = editing !== null || !state || state.observations.length === 0;
  if ($('map-section').hidden) {
    return;
  }
  const { speeds, widths, p } = state.map;
  const canvas = $('map');
  const ctx = canvas.getContext('2d');
  const pad = { left: 40, bottom: 16, top: 4, right: 4 };
  const w = canvas.width - pad.left - pad.right;
  const h = canvas.height - pad.top - pad.bottom;
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const cellW = w / speeds.length;
  const cellH = h / widths.length;
  speeds.forEach((_, s) => {
    widths.forEach((_, i) => {
      ctx.fillStyle = `hsl(${Math.round(120 * p[s][i])} 65% 60%)`;
      ctx.fillRect(pad.left + s * cellW, pad.top + h - (i + 1) * cellH, cellW + 0.5, cellH + 0.5);
    });
  });

  const x = (rpm) => pad.left + ((rpm - speeds[0]) / (speeds.at(-1) - speeds[0])) * (w - cellW) + cellW / 2;
  const y = (width) => pad.top + h - ((width - widths[0]) / (widths.at(-1) - widths[0])) * (h - cellH) - cellH / 2;
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = '#000';
  for (const o of state.observations) {
    const [cx, cy] = [x(o.rpm), y(o.width_mm)];
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
  ctx.fillText(`${widths.at(-1)}`, pad.left - 3, pad.top);
  ctx.textBaseline = 'bottom';
  ctx.fillText(`${widths[0]} mm`, pad.left - 3, pad.top + h);
}

function renderBest() {
  const best = editing === null ? (state?.best ?? []) : [];
  $('best-section').hidden = best.length === 0;
  $('best').replaceChildren(
    ...best.map((b) =>
      el('tr', {},
        el('td', {}, String(b.rpm)),
        el('td', {}, `${b.width_mm.toFixed(2)} mm`),
        el('td', {}, String(b.feed_mm_min)),
        el('td', {}, b.mrr_cm3_min.toFixed(2)),
      ),
    ),
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
  renderSetup();
  renderStep();
  renderRound();
  renderMap();
  renderBest();
}

// ---- wiring ----------------------------------------------------------------------------

$('sessions').onchange = () => openSession($('sessions').value);
$('new-session').onclick = newSession;
$('edit-setup').onclick = editSetup;
$('setup-back').onclick = previousStep;
$('units').onchange = () => changeUnits($('units').value);
$('setup-form').onsubmit = (event) => {
  event.preventDefault();
  nextStep();
};
$('cancel-setup').onclick = () => {
  editing = null;
  showError('');
  render();
};
$('export').onclick = exportSession;
$('import').onclick = () => $('import-file').click();
$('import-file').onchange = () => {
  const [file] = $('import-file').files;
  $('import-file').value = '';
  if (file) {
    importSession(file);
  }
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
window.addEventListener('beforeunload', (event) => {
  if (mic.recording) {
    event.preventDefault();
  }
});

render();
connectCncjs();
openMic(localStorage.getItem(DEVICE_KEY) || undefined);
setInterval(renderLevel, LEVEL_MS);

try {
  names = await engine('names');
  if (!names.includes(current)) {
    current = '';
  }
  await openSession(current);
} catch (err) {
  showError(err.message);
}
