// Audio thread: hand the microphone's samples to the page in blocks, mixed down to mono.

const BLOCK = 4096;

class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(BLOCK);
    this.filled = 0;
  }

  process(inputs) {
    const channels = inputs[0];
    if (!channels.length) {
      return true;
    }
    const frames = channels[0].length;
    for (let i = 0; i < frames; i += 1) {
      let sum = 0;
      for (const channel of channels) {
        sum += channel[i];
      }
      this.block[this.filled] = sum / channels.length;
      this.filled += 1;
      if (this.filled === BLOCK) {
        this.port.postMessage(this.block.slice());
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor('capture', Capture);
