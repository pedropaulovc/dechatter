// Playwright init script: replaces the widget's microphone with a stream the test can play
// a WAV into, so a simulated round can be "heard". Only getUserMedia is replaced; capture,
// recording and analysis are the widget's own.
//
//   await page.context().addInitScript({ path: 'e2e/fake_mic.js' });
//   await widgetFrame.evaluate((url) => window.fakeMicCue(url), '/dechatter/e2e/round.wav');
//   // Then press Start in the widget: playback starts on `dechatter:record` and re-anchors
//   // each pass on `dechatter:dwell` when `<url>.json` is present alongside the WAV.

if (/^\/dechatter(\/|$)/.test(location.pathname)) {
  const context = new AudioContext();
  const mic = context.createMediaStreamDestination();
  let active = null;
  let cued = null;

  const stopActive = () => {
    if (active) {
      try {
        active.stop();
      } catch {}
      active.disconnect();
      active = null;
    }
  };

  const playFrom = (buffer, offset = 0) => {
    stopActive();
    const clamped = Math.max(0, Math.min(offset, Math.max(buffer.duration - 0.01, 0)));
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(mic);
    source.start(0, clamped);
    active = source;
  };

  const load = async (url) => {
    await context.resume();
    const [wavRes, cuesRes] = await Promise.all([
      fetch(url),
      fetch(`${url}.json`).catch(() => null),
    ]);
    const buffer = await context.decodeAudioData(await wavRes.arrayBuffer());
    const cues = cuesRes && cuesRes.ok ? await cuesRes.json() : null;
    return { buffer, dwells: cues?.dwells ?? null };
  };

  // Clone per openMic() call so Mic.close() stopping its track never deafens the destination.
  navigator.mediaDevices.getUserMedia = async () => {
    await context.resume();
    return mic.stream.clone();
  };

  window.addEventListener('dechatter:record', () => {
    if (cued) {
      playFrom(cued.buffer, 0);
    }
  });

  window.addEventListener('dechatter:dwell', (event) => {
    const pass = String(event.detail?.pass ?? '');
    if (cued?.dwells && Object.hasOwn(cued.dwells, pass)) {
      playFrom(cued.buffer, cued.dwells[pass]);
    }
  });

  window.fakeMicCue = async (url) => {
    cued = await load(url);
    return cued.buffer.duration;
  };

  window.fakeMicPlay = async (url) => {
    cued = await load(url);
    playFrom(cued.buffer, 0);
    return cued.buffer.duration;
  };
}
