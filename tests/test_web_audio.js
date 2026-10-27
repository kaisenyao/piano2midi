// The real AudioWorklet, without a browser, at common iPhone hardware rates.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('src/piano2midi/web/capture.js', 'utf8');
const block = (n, value) => [[new Float32Array(n).fill(value)]];

for (const rate of [16000, 44100, 48000]) {
  const frames = [];
  let Processor;
  vm.runInNewContext(source, {
    sampleRate: rate,
    AudioWorkletProcessor: class { constructor() { this.port = {postMessage: frame => frames.push(frame)}; } },
    registerProcessor: (_, constructor) => { Processor = constructor; },
  });
  const capture = new Processor();
  capture.process(block(rate, .9));
  assert.equal(frames.length, 0, 'Off until the page turns capture on');
  capture.port.onmessage({data: true});
  for (let i = 0; i < rate; i += 128) capture.process(block(Math.min(128, rate - i), .25));
  assert.equal(frames.length, 12, 'One second is 12 whole 80 ms frames');
  assert.equal(capture.n, 640);
  for (const frame of frames) {
    assert.equal(frame.pcm.byteLength, 2560);
    assert.ok(Math.abs(frame.level - .25) < 1e-6);
    for (const sample of new Int16Array(frame.pcm)) assert.equal(sample, 8192);
  }
  capture.port.onmessage({data: false});
  assert.equal(capture.n, 0, 'Pausing discards the partial frame');
  capture.process(block(rate, .9));
  assert.equal(frames.length, 12, 'Nothing is captured while paused');
  capture.port.onmessage({data: true});
  capture.process(block(Math.round(rate * .08), .1));
  assert.equal(frames.length, 13);
  assert.ok(Math.abs(frames.at(-1).level - .1) < 1e-6, 'Continuing cannot leak audio from before the pause');
}
console.log('Capture passed: 16 / 44.1 / 48 kHz resampling, 80 ms PCM16 frames, levels, off by default, pause privacy.');
