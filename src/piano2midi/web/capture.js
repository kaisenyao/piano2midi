// Resamples the microphone from any hardware rate to 16 kHz PCM16, sent in 80 ms frames.
// Off until the page turns it on; turning it on or off discards any partial frame.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.enabled = false;
    this.reset();
    this.port.onmessage = ({data}) => { this.enabled = data; this.reset(); };
  }
  reset() { this.frame = new Int16Array(1280); this.n = 0; this.sum = 0; this.weight = 0; this.square = 0; }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!this.enabled || !samples) return true;
    for (const sample of samples) {
      let remaining = 1;
      while (remaining > 1e-8) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.sum += sample * take; this.weight += take; remaining -= take;
        if (this.weight >= this.ratio - 1e-8) {
          const value = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.frame[this.n++] = Math.round(value * 32767); this.square += value * value;
          this.sum = 0; this.weight = 0;
          if (this.n === 1280) {
            this.port.postMessage({pcm: this.frame.buffer, level: Math.sqrt(this.square / 1280)}, [this.frame.buffer]);
            this.frame = new Int16Array(1280); this.n = 0; this.square = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('phone-capture', Capture);
