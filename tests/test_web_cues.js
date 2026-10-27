// The real cue player, with the browser's audio and network replaced.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('src/piano2midi/web/cues.js', 'utf8').replace('export function', 'function');
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

function harness(options = {}) {
  const contexts = [], sources = [], gains = [], requests = [], decodes = [];
  class Context {
    constructor() { this.state = 'suspended'; this.currentTime = 10; this.resumes = 0; contexts.push(this); }
    resume() { this.resumes++; this.state = 'running'; return Promise.resolve(); }
    suspend() { this.state = 'suspended'; return Promise.resolve(); }
    decodeAudioData() {
      if (options.badAudio) return Promise.reject(Error('bad audio'));
      return new Promise(resolve => decodes.push(() => resolve({duration: 1.6})));
    }
    createGain() {
      const gain = {events: [], connect: next => next, disconnect() { gain.disconnected = true; }, gain: {
        setValueAtTime: (value, at) => gain.events.push(['set', value, at]),
        linearRampToValueAtTime: (value, at) => gain.events.push(['ramp', value, at]),
      }};
      gains.push(gain);
      return gain;
    }
    createBufferSource() {
      const item = {connect: next => next,
        start() { if (options.noOutput) throw Error('no output'); item.started = true; },
        stop(at) { item.stopAt = at; }};
      sources.push(item);
      return item;
    }
  }
  const scope = {AudioContext: Context, fetch: async url => {
    requests.push(url);
    return {ok: !options.missing, arrayBuffer: async () => new ArrayBuffer(8)};
  }};
  vm.createContext(scope);
  vm.runInContext(source + '\nthis.cues = createCues();', scope);
  const decodeAll = async () => { await tick(); decodes.splice(0).forEach(done => done()); await tick(); };
  return {cues: scope.cues, contexts, sources, gains, requests, decodeAll};
}

(async () => {
  const h = harness();
  await tick();
  assert.deepEqual(h.requests, ['/cue-active.mp3', '/cue-paused.mp3'], 'Both files load with the page');
  assert.equal(h.contexts.length, 0, 'Loading the page opens no audio');
  assert.equal(h.sources.length, 0);

  h.cues.unlock();
  await h.decodeAll();
  assert.equal(h.contexts[0].state, 'running', 'The tap starts the cue clock');

  // A cue resolves only once it has finished sounding: pausing waits for this to release the microphone.
  let finished = false;
  const ending = h.cues.play('paused').then(() => { finished = true; });
  await tick();
  assert.equal(h.sources[0].started, true);
  assert.equal(finished, false);
  h.sources[0].onended();
  await ending;
  assert.equal(finished, true);
  assert.equal(h.sources[0].stopAt, undefined, 'An isolated cue keeps its whole natural tail');
  assert.equal(h.gains[0].disconnected, true);

  // A new tap fades the playing cue in 15 ms, never a hard cut.
  h.cues.unlock();
  h.cues.play('active');
  await tick();
  h.cues.stop();
  assert.equal(h.sources[1].stopAt, 10.015);
  assert.deepEqual(h.gains[1].events, [['set', 1, 10], ['ramp', 0, 10.015]]);
  h.cues.stop();
  assert.equal(h.gains[1].events.length, 2, 'A cue fades out once');

  // Many taps: one clock, one decode per file, nothing suspended by a cue ending.
  for (let i = 0; i < 8; i++) {
    h.cues.unlock();
    const playing = h.cues.play(i % 2 ? 'active' : 'paused');
    await tick();
    h.sources.at(-1).onended();
    await playing;
  }
  assert.equal(h.contexts.length, 1);
  assert.equal(h.contexts[0].state, 'running');
  assert.equal(h.contexts[0].resumes, 1, 'A running clock is not restarted on every tap');

  // A cue still decoding never sounds after a later tap.
  const slow = harness();
  slow.cues.unlock();
  const late = slow.cues.play('active');
  slow.cues.stop();
  await slow.decodeAll();
  await late;
  assert.equal(slow.sources.length, 0);

  // The session ends: silence, rest the hardware, and come back on the next tap.
  const ended = harness();
  ended.cues.unlock();
  await ended.decodeAll();
  ended.cues.play('active');
  await tick();
  ended.cues.suspend();
  assert.equal(ended.gains[0].disconnected, true);
  assert.equal(ended.contexts[0].state, 'suspended');
  ended.cues.unlock();
  assert.equal(ended.contexts[0].state, 'running');
  assert.equal(ended.contexts.length, 1);

  // Sounds are optional: without them a tap still works, and nothing is left to stop.
  for (const options of [{missing: true}, {badAudio: true}, {noOutput: true}]) {
    const bad = harness(options);
    bad.cues.play('active');
    await tick();
    bad.cues.unlock();
    await bad.decodeAll();
    await bad.cues.play('paused');
    bad.cues.stop();
    assert.ok(bad.sources.every(item => !item.started));
  }
  console.log('Cues passed: silent page load, tap unlock, resolve after the full tail, 15 ms fades, one clock and decode, stale and failed cues, suspend and recovery.');
})().catch(error => { console.error(error); process.exitCode = 1; });
