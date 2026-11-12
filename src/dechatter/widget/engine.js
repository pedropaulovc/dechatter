// Web worker: dechatter's Python running in Pyodide. Sessions live in a filesystem that is
// persisted to IndexedDB, so they survive reloads and browser restarts.

import { loadPyodide } from './pyodide/pyodide.js';

const ROOT = '/sessions';
// Calls that change files; the filesystem is flushed to IndexedDB after each of them.
const WRITES = new Set(['create', 'save_setup', 'plan', 'plan_sweep', 'record_stop', 'analyze', 'set_verdict', 'import_zip']);

let pyodide;
let call;

// cncjs serves .wasm as application/octet-stream, which streaming compilation refuses.
// Relabel those responses; the bytes still stream straight through.
const plainFetch = self.fetch.bind(self);
self.fetch = async (input, init) => {
  const response = await plainFetch(input, init);
  const url = typeof input === 'string' ? input : (input.url ?? String(input));
  if (!response.ok || !url.split('?')[0].endsWith('.wasm')) {
    return response;
  }
  return new Response(response.body, { status: response.status, headers: { 'Content-Type': 'application/wasm' } });
};

function sync(populate) {
  return new Promise((resolve, reject) => {
    pyodide.FS.syncfs(populate, (err) => (err ? reject(err) : resolve()));
  });
}

async function boot() {
  postMessage({ boot: 'Loading Python' });
  pyodide = await loadPyodide({ indexURL: new URL('./pyodide/', import.meta.url).href });
  postMessage({ boot: 'Loading numpy and scipy' });
  await pyodide.loadPackage(['numpy', 'scipy']);
  const source = await fetch(new URL('./dechatter.zip', import.meta.url));
  if (!source.ok) {
    throw new Error(`cannot load dechatter.zip: ${source.status}`);
  }
  pyodide.unpackArchive(await source.arrayBuffer(), 'zip');
  pyodide.FS.mkdirTree(ROOT);
  pyodide.FS.mount(pyodide.FS.filesystems.IDBFS, {}, ROOT);
  await sync(true);
  call = pyodide.runPython(`
import logging
from pathlib import Path
from dechatter import browser

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
_sessions = browser.Sessions(Path("${ROOT}"))

def _call(method, *args):
    return browser.call(_sessions, method, *args)

_call
`);
}

async function handle({ method, args }) {
  const converted = args.map((arg) => (ArrayBuffer.isView(arg) ? pyodide.toPy(arg) : arg));
  const result = call(method, ...converted);
  for (const arg of converted) {
    arg?.destroy?.();
  }
  if (typeof result !== 'string') {
    const bytes = result.toJs();
    result.destroy();
    return { ok: bytes };
  }
  const reply = JSON.parse(result);
  if ('ok' in reply && WRITES.has(method)) {
    await sync(false);
  }
  return reply;
}

const ready = boot().then(
  () => postMessage({ boot: null }),
  (err) => {
    postMessage({ boot: null, fatal: `dechatter could not start: ${err.message ?? err}` });
    throw err;
  },
);

onmessage = async ({ data }) => {
  try {
    await ready;
    postMessage({ id: data.id, ...(await handle(data)) });
  } catch (err) {
    postMessage({ id: data.id, error: `${err.message ?? err}` });
  }
};
