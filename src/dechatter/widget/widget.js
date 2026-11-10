// dechatter cncjs widget. Talks to cncjs over its socket.io API (load the program, watch the
// job) and to the dechatter server that serves this page (plan, record, analyse).

const params = new URLSearchParams(location.search);
const token = params.get('token') ?? '';
const cncjsOrigin =
  params.get('host') ?? (document.referrer ? new URL(document.referrer).origin : location.origin);

const POLL_MS = 1000;
const $ = (id) => document.getElementById(id);

const TIMELINE_LIMIT = 5000;
const cnc = {
  socket: null,
  link: 'connecting', // connecting | no-port | ready | offline
  port: '',
  workflow: null, // idle | running | paused; null until cncjs reports it
  program: '', // name of the G-code file cncjs has loaded
};
let server = null; // last state from the dechatter server
let timeline = []; // what cncjs reported since the job last started; saved with the recording
let busy = false; // a request to the dechatter server is in flight
let stockChoice = 'same'; // survives redraws: planning on the wrong face position cuts air or too deep

// ---- dechatter server ------------------------------------------------------------------

async function api(path, body) {
  const response = await fetch(new URL(`api/${path}`, location.href), {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (path.endsWith('/program') && response.ok) {
    return response.text();
  }
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error ?? `${response.status} ${response.statusText}`);
  }
  return payload;
}

function showError(message) {
  $('error').textContent = message;
  $('error').hidden = !message;
}

// Run one state-changing request. Whatever happens, re-read the state afterwards.
async function act(path, body) {
  busy = true;
  render();
  try {
    server = await api(path, body);
    showError('');
  } catch (err) {
    showError(err.message);
    await refresh();
  } finally {
    busy = false;
    render();
  }
}

async function refresh() {
  try {
    server = await api('state');
  } catch (err) {
    showError(`dechatter server: ${err.message}`);
  }
  syncRecording();
  render();
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

function openPort(port) {
  cnc.port = port;
  cnc.link = port ? 'ready' : 'no-port';
  if (port) {
    // Joining an open port makes cncjs replay its sender and workflow state to this socket.
    cnc.socket.emit('open', port, {});
  }
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
  socket.on('serialport:open', ({ port }) => openPortState(port));
  socket.on('serialport:close', () => openPortState(''));
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
  });
  socket.on('workflow:state', (state) => {
    if (state === 'running' && cnc.workflow !== 'paused') {
      timeline = [];
    }
    cnc.workflow = state;
    note('workflow', { state });
    syncRecording();
    render();
  });
}

function openPortState(port) {
  cnc.port = port;
  cnc.link = port ? 'ready' : 'no-port';
  render();
}

window.addEventListener('message', (event) => {
  const { token: theirs, action } = { ...event.data };
  if (theirs !== token || action?.type !== 'change') {
    return;
  }
  openPort(action.payload?.port ?? '');
});

// ---- recording follows the job ---------------------------------------------------------

function plannedRound() {
  const last = server?.rounds.at(-1);
  return last && !last.analysed ? last : null;
}

// Record exactly while cncjs runs the planned round's program.
function syncRecording() {
  if (!server || busy) {
    return;
  }
  const round = plannedRound();
  const recording = server.recorder.state === 'recording';
  if (!recording && round && cnc.workflow === 'running' && cnc.program === round.program) {
    act('record/start', { round: round.round });
    return;
  }
  if (recording && cnc.workflow === 'idle' && cnc.link === 'ready') {
    act('record/stop', { timeline });
  }
}

async function loadProgram(round) {
  busy = true;
  render();
  try {
    const gcode = await api(`rounds/${round.round}/program`);
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

const LINK_TEXT = {
  connecting: 'connecting',
  'no-port': 'connected; open a serial port in cncjs',
  offline: 'not connected',
};

function renderLinks() {
  const job = cnc.workflow ?? 'state unknown';
  $('cncjs').textContent = cnc.link === 'ready' ? `${cnc.port}, ${job}` : LINK_TEXT[cnc.link];
  const { device, level_db: level } = server.recorder;
  $('device').textContent = device;
  $('level').value = level ?? -80;
  $('level-db').textContent = level === null ? 'silent' : `${Math.round(level)} dB`;
}

function minutes(seconds) {
  return seconds < 90 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

function renderStep() {
  const step = $('step');
  step.replaceChildren();
  const round = plannedRound();
  const recorder = server.recorder;

  if (recorder.state === 'recording') {
    step.append(
      el('p', { className: 'recording' }, `Recording round ${recorder.round}: ${minutes(Math.round(recorder.seconds))}`),
      el('p', {}, 'Feed-hold and stop the job in cncjs if a cut gets violent. The passes that ran still count.'),
    );
    return;
  }

  if (server.next === 'converged') {
    step.append(el('p', {}, 'Converged: no further cut is expected to add removal rate. The best stable cuts are below.'));
    return;
  }

  if (server.next === 'plan') {
    const first = server.rounds.length === 0;
    const stock = el('select', {}, el('option', { value: 'same' }, 'same block'), el('option', { value: 'fresh' }, 'fresh block, face at Y0'));
    stock.value = stockChoice;
    stock.onchange = () => {
      stockChoice = stock.value;
    };
    const button = el('button', { className: 'primary', disabled: busy }, first ? 'Plan round 1' : `Plan round ${server.rounds.length + 1}`);
    button.onclick = async () => {
      await act('plan', { stock: first ? 'same' : stockChoice });
      stockChoice = 'same';
    };
    step.append(el('p', {}, first ? 'Plan a coarse grid of test cuts.' : 'Plan the next cuts from what was measured.'));
    step.append(el('p', {}, button, ...(first ? [] : [' on the ', stock])));
    return;
  }

  const tests = round.passes.filter((p) => p.kind === 'test').length;
  const loaded = cnc.program === round.program;
  const load = el('button', { className: loaded ? '' : 'primary', disabled: busy || cnc.link !== 'ready' || cnc.workflow !== 'idle' }, loaded ? 'Reload program' : 'Load into cncjs');
  load.onclick = () => loadProgram(round);
  step.append(
    el('p', {}, `Round ${round.round}: ${tests} test cuts, about ${minutes(round.run_time_s)}. The front face ends at Y${round.face_y_end_mm}.`),
    el('ol', {},
      el('li', {}, 'Zero X at the left end of the stock, Y on its front face, Z on top.'),
      el('li', {}, load),
      el('li', {}, loaded ? 'Press play in cncjs. Recording starts and stops with the job.' : 'Press play in cncjs.'),
    ),
  );
  if (round.has_recording) {
    const again = el('button', { disabled: busy }, 'Analyse the last recording again');
    again.onclick = () => act(`rounds/${round.round}/analyze`, {});
    step.append(el('p', {}, 'The last run could not be analysed. Run the round again, or ', again));
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
  select.onchange = () => act(`rounds/${round.round}/verdict`, { pass: pass.id, verdict: select.value });
  return el('td', { className: pass.verdict ?? '' }, select);
}

function renderRound() {
  const round = server.rounds.at(-1);
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
  const { speeds, widths, p } = server.map;
  $('map-section').hidden = server.observations.length === 0;
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
  for (const o of server.observations) {
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
  $('best-section').hidden = server.best.length === 0;
  $('best').replaceChildren(
    ...server.best.map((b) =>
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
  if (!server) {
    return;
  }
  $('session').textContent = server.session;
  $('summary').textContent = server.summary;
  renderLinks();
  renderStep();
  renderRound();
  renderMap();
  renderBest();
}

// Tell the cncjs page how tall the widget wants to be.
new ResizeObserver(() => {
  const { scrollHeight, scrollWidth } = document.body;
  window.parent.postMessage({ token, action: { type: 'resize', payload: { scrollHeight, scrollWidth } } }, '*');
}).observe(document.body);

await refresh();
connectCncjs();
setInterval(() => {
  // A select being edited or a request in flight must not be redrawn underneath the user.
  if (!busy && document.activeElement?.tagName !== 'SELECT') {
    refresh();
  }
}, POLL_MS);
