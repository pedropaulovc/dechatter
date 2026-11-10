// Playwright init script: replaces the widget's microphone with a stream the test can play
// a WAV into, so a simulated round can be "heard". Only the getUserMedia call is replaced;
// capture, resampling, recording and analysis are the widget's own.
//
//   await page.context().addInitScript({ path: 'e2e/fake_mic.js' });
//   await widgetFrame.evaluate((url) => window.fakeMicPlay(url), '/dechatter/e2e/round.wav');

if (location.pathname.startsWith('/dechatter/')) {
  const context = new AudioContext();
  const mic = context.createMediaStreamDestination();
  navigator.mediaDevices.getUserMedia = async () => mic.stream;
  window.fakeMicPlay = async (url) => {
    await context.resume();
    const buffer = await context.decodeAudioData(await (await fetch(url)).arrayBuffer());
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(mic);
    source.start();
    return buffer.duration;
  };
}
