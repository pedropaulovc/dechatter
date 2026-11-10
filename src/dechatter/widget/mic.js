// The browser's microphone, raw: the voice-call processing browsers apply by default
// (echo cancellation, noise suppression, automatic gain) would reshape the very spectrum the
// chatter detector reads, so it is switched off and checked.

const PROCESSING = ['echoCancellation', 'noiseSuppression', 'autoGainControl'];

export class Mic {
  constructor() {
    this.context = null;
    this.stream = null;
    this.label = '';
    this.rate = 0;
    this.levelDb = null;
    this.processing = []; // processing the browser kept on despite being asked not to
    this.chunks = null; // Float32Array blocks while recording, else null
    this.frames = 0;
  }

  get open() {
    return this.stream !== null;
  }

  get recording() {
    return this.chunks !== null;
  }

  get seconds() {
    return this.rate ? this.frames / this.rate : 0;
  }

  async devices() {
    const all = await navigator.mediaDevices.enumerateDevices();
    return all.filter((d) => d.kind === 'audioinput' && d.deviceId).map((d) => ({ id: d.deviceId, label: d.label }));
  }

  async start(deviceId) {
    await this.close();
    const audio = { channelCount: 1 };
    for (const name of PROCESSING) {
      audio[name] = false;
    }
    if (deviceId) {
      audio.deviceId = { exact: deviceId };
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio });
    const track = stream.getAudioTracks()[0];
    const settings = track.getSettings();
    const context = new AudioContext();
    await context.audioWorklet.addModule(new URL('./mic-worklet.js', import.meta.url));
    const capture = new AudioWorkletNode(context, 'capture');
    capture.port.onmessage = ({ data }) => this.onBlock(data);
    context.createMediaStreamSource(stream).connect(capture);

    this.context = context;
    this.stream = stream;
    this.label = track.label;
    this.rate = context.sampleRate;
    this.processing = PROCESSING.filter((name) => settings[name] === true);
    track.onended = () => this.close();
  }

  onBlock(block) {
    let power = 0;
    for (const sample of block) {
      power += sample * sample;
    }
    this.levelDb = 10 * Math.log10(power / block.length + 1e-12);
    if (this.chunks) {
      this.chunks.push(block);
      this.frames += block.length;
    }
  }

  record() {
    this.chunks = [];
    this.frames = 0;
  }

  // Everything heard since record(), as one Float32Array.
  finish() {
    const samples = new Float32Array(this.frames);
    let offset = 0;
    for (const chunk of this.chunks ?? []) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    this.chunks = null;
    this.frames = 0;
    return samples;
  }

  async close() {
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.context?.close();
    this.stream = null;
    this.context = null;
    this.levelDb = null;
  }
}
