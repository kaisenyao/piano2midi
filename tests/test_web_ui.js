// The real page controller, with browser, microphone, network and audio replaced; no API calls.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const controller = fs.readFileSync('src/piano2midi/web/app.js', 'utf8').replace(/^import[^\n]+\n/gm, '');
const tick = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const PIECE = Buffer.alloc(4800).toString('base64'); // 0.1 s of 24 kHz PCM16

function harness(options = {}) {
  const h = {options, tracks: [], sockets: [], contexts: [], clips: [], sources: [], nodes: [],
    cues: [], cueEnds: [], levels: [], vibrations: [], animations: 0, mics: 0, timers: new Map()};
  const store = new Map(options.token === '' ? [] : [['voice-token', options.token || 'test-token']]);
  h.store = store;
  // Like DOMStringMap, the dataset stores strings.
  const dataset = new Proxy({}, {set: (target, key, value) => { target[key] = String(value); return true; }});
  const orb = h.orb = {dataset, animate() { h.animations++; }};
  const toggle = h.toggle = {checked: false, setAttribute(name, value) { this[name] = value; }};
  const node = () => ({connect: next => next, disconnect() { this.disconnected = true; }});
  class Context {
    constructor() {
      this.state = 'suspended';
      this.currentTime = 5;
      this.audioWorklet = {addModule: async () => {}};
      h.contexts.push(this);
    }
    resume() {
      if (options.holdResume) return new Promise(resolve => { h.finishResume = () => { this.state = 'running'; resolve(); }; });
      this.state = 'running';
      return Promise.resolve();
    }
    suspend() { this.state = 'suspended'; return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createAnalyser() { return {...node(), getByteTimeDomainData: samples => samples.fill(160)}; }
    createGain() { return {...node(), gain: {}}; }
    createMediaStreamSource(stream) { const item = {...node(), stream}; h.sources.push(item); return item; }
    createBuffer(channels, length, rate) {
      const samples = new Float32Array(length);
      return {duration: length / rate, getChannelData: () => samples};
    }
    createBufferSource() {
      const clip = {...node(), start(at) { clip.at = at; }, stop() {}};
      h.clips.push(clip);
      return clip;
    }
  }
  class Socket {
    static OPEN = 1;
    constructor() {
      this.readyState = 1;
      this.bufferedAmount = 0;
      this.sent = [];
      this.frames = 0;
      h.sockets.push(this);
      queueMicrotask(() => this.onopen());
    }
    send(raw) {
      if (typeof raw !== 'string') { this.frames++; return; }
      const msg = JSON.parse(raw);
      this.sent.push(msg.type || 'token:' + msg.token);
      if (msg.token && !options.slowServer) { this.event({type: 'ready'}); this.event({type: 'state', state: 'waiting'}); }
    }
    event(msg) { this.onmessage({data: typeof msg === 'string' ? msg : JSON.stringify(msg)}); }
    close() { this.closed = true; }
  }
  const events = {};
  const sandbox = h.sandbox = {
    createCues: () => ({
      unlock() { assert.ok(h.inTap, 'Cue audio is unlocked inside the tap'); },
      stop() { h.cues.push('stop'); },
      suspend() { h.cues.push('suspend'); },
      play(name) { h.cues.push(name); return new Promise(resolve => h.cueEnds.push({name, resolve})); },
    }),
    animateOrb: () => ({level: value => h.levels.push(value)}),
    matchMedia: () => ({matches: false}),
    document: {hidden: false, getElementById: id => (id === 'orb' ? orb : toggle), addEventListener: (name, fn) => { events[name] = fn; }},
    addEventListener: (name, fn) => { events[name] = fn; },
    location: {hash: options.hash || '', search: '', pathname: '/', protocol: 'https:', host: 'mac.local'},
    history: {replaceState(state, title, url) { h.address = url; }},
    localStorage: {
      getItem: key => { if (options.blockStorage) throw Error('denied'); return store.get(key); },
      setItem: (key, value) => { if (options.blockStorage) throw Error('denied'); store.set(key, value); },
      removeItem: key => { if (options.blockStorage) throw Error('denied'); store.delete(key); },
    },
    URLSearchParams, WebSocket: Socket, AudioContext: Context, queueMicrotask,
    AudioWorkletNode: class {
      constructor() { this.port = {postMessage: on => { this.capturing = on; }}; h.nodes.push(this); }
      connect(next) { return next; }
    },
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    navigator: {vibrate: ms => h.vibrations.push(ms), mediaDevices: {getUserMedia: async () => {
      h.mics++;
      if (options.holdMic) await new Promise(resolve => { h.grantMic = resolve; });
      if (options.denyMic) throw Error('denied');
      const track = {enabled: true, readyState: 'live', stop() { track.readyState = 'ended'; }};
      h.tracks.push(track);
      return {getTracks: () => [track], getAudioTracks: () => [track]};
    }}},
    window: {isSecureContext: options.insecure !== true},
    setTimeout: (fn, ms) => { const id = Symbol(); h.timers.set(id, {fn, ms}); return id; },
    clearTimeout: id => h.timers.delete(id),
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(controller, sandbox);
  h.tap = async () => {
    toggle.checked = !toggle.checked; // the native switch flips itself before onchange
    h.inTap = true;
    try { toggle.onchange(); } finally { h.inTap = false; }
    await tick();
  };
  h.hide = () => { sandbox.document.hidden = true; events.visibilitychange(); };
  sandbox.dispatch = name => events[name]();
  h.socket = () => h.sockets.at(-1);
  h.ctx = () => h.contexts.at(-1);
  h.track = () => h.tracks.at(-1);
  h.active = () => orb.dataset.active;
  h.state = state => h.socket().event({type: 'state', state});
  h.frame = (level = .2) => h.nodes[0].port.onmessage({data: {pcm: new ArrayBuffer(2560), level}});
  h.endCue = async () => { h.cueEnds.findLast(cue => cue.name === 'paused').resolve(); await tick(); };
  h.fireTimer = async ms => {
    for (const [id, timer] of h.timers) if (timer.ms === ms) { h.timers.delete(id); timer.fn(); }
    await tick();
  };
  return h;
}

(async () => {
  // Opening the page: gray and silent, nothing requested.
  const h = harness();
  assert.equal(h.active(), 'false');
  assert.equal(h.mics + h.sockets.length + h.cues.length + h.vibrations.length + h.animations, 0);

  // Tap: immediate feedback; color and the start sound only once the Mac is ready.
  const slow = harness({slowServer: true});
  await slow.tap();
  assert.equal(slow.animations, 1);
  assert.deepEqual(slow.vibrations, [12]);
  assert.equal(slow.active(), 'false');
  assert.equal(slow.orb.dataset.waking, 'true', 'Half colored while it starts');
  assert.equal(slow.cues.includes('active'), false);
  assert.equal(slow.mics, 1, 'The microphone is asked for while the Mac prepares');
  slow.socket().event({type: 'ready'});
  await tick();
  assert.equal(slow.active(), 'true');
  assert.equal(slow.orb.dataset.waking, 'false');
  assert.equal(slow.cues.at(-1), 'active');
  assert.equal(slow.animations, 2, 'A connected bounce');
  assert.deepEqual(slow.vibrations, [12], 'Connecting does not vibrate again');
  assert.equal(slow.timers.size, 0, 'No connection timeout is left behind');

  // Listening: audio goes to the Mac only while listening; the orb follows the voice.
  await h.tap();
  assert.equal(h.nodes[0].capturing, true);
  assert.equal(h.track().enabled, true);
  h.frame(.2);
  assert.equal(h.socket().frames, 1);
  assert.ok(h.levels.at(-1) > 1);
  h.state('thinking');
  h.frame(.2);
  assert.equal(h.socket().frames, 1, 'Nothing is sent while she thinks or speaks');
  assert.equal(h.levels.at(-1), 0);
  h.socket().bufferedAmount = 40000;
  h.state('conversation');
  h.frame(.2);
  assert.equal(h.socket().frames, 1, 'A backed-up network drops audio instead of queueing it');
  assert.equal(h.active(), 'true');

  // Her reply arrives in pieces that play back to back; the next turn is asked for after the last.
  const talk = harness();
  await talk.tap();
  talk.state('speaking');
  talk.socket().event({type: 'audio', data: PIECE});
  talk.socket().event({type: 'audio', data: '/w=='}); // not PCM16: skipped
  talk.socket().event({type: 'audio', data: PIECE});
  talk.socket().event({type: 'turn_end', sleep: false});
  await tick();
  assert.equal(talk.clips.length, 2, 'A bad piece is skipped, the reply goes on');
  assert.equal(talk.clips[0].at, 5, 'The first piece plays at once');
  assert.ok(Math.abs(talk.clips[1].at - 5.1) < 1e-9, 'The next starts exactly where it ends');
  talk.clips[0].onended();
  await tick();
  assert.equal(talk.socket().sent.includes('resume'), false);
  talk.clips[1].onended();
  await tick();
  assert.equal(talk.socket().sent.at(-1), 'resume');
  talk.socket().event({type: 'turn_end', sleep: true});
  await tick();
  assert.equal(talk.socket().sent.at(-1), 'sleep');
  assert.equal(talk.active(), 'true');

  // Pause: gray and private at once; the microphone is released only after the end sound.
  const p = harness();
  await p.tap();
  const firstTrack = p.track();
  await p.tap();
  assert.equal(p.active(), 'false');
  assert.equal(p.nodes[0].capturing, false);
  assert.equal(firstTrack.enabled, false, 'Muted at once');
  assert.equal(p.ctx().state, 'suspended', 'Her speech is frozen where it is');
  assert.equal(p.socket().sent.at(-1), 'pause_session');
  assert.equal(p.cues.at(-1), 'paused');
  p.frame(.5);
  assert.equal(p.socket().frames, 0, 'Nothing is sent while paused');
  assert.equal(firstTrack.readyState, 'live', 'The end sound plays before the microphone is released');
  await p.endCue();
  assert.equal(firstTrack.readyState, 'ended', 'Then the microphone is released');
  assert.equal(p.sources[0].disconnected, true);
  assert.equal(p.socket().closed, undefined, 'The conversation stays connected');
  // Continue: the same conversation and speech clock, a new microphone.
  await p.tap();
  assert.equal(p.mics, 2);
  assert.equal(p.active(), 'true');
  assert.equal(p.track().enabled, true);
  assert.equal(p.ctx().state, 'running');
  assert.equal(p.contexts.length, 1);
  assert.equal(p.sockets.length, 1);
  assert.equal(p.socket().sent.at(-1), 'continue_session');
  assert.equal(p.cues.at(-1), 'active');

  // An end sound that never finishes still releases the microphone after three seconds.
  const stuck = harness();
  await stuck.tap();
  await stuck.tap();
  assert.equal(stuck.track().readyState, 'live');
  await stuck.fireTimer(3000);
  assert.equal(stuck.track().readyState, 'ended');

  // Continue during the end sound: the sound fades, the microphone is kept and reused.
  const quick = harness();
  await quick.tap();
  await quick.tap();
  await quick.tap();
  assert.equal(quick.cues.slice(-2).join(), 'stop,active');
  assert.equal(quick.mics, 1);
  await quick.endCue();
  await quick.fireTimer(3000);
  assert.equal(quick.track().readyState, 'live', 'A late end sound cannot release the reused microphone');
  assert.equal(quick.track().enabled, true);
  assert.equal(quick.active(), 'true');

  // Paused speech stays where it is: nothing restarts, nothing new starts, then it continues.
  const held = harness();
  await held.tap();
  held.state('speaking');
  held.socket().event({type: 'audio', data: PIECE});
  await tick();
  await held.tap();
  held.socket().event({type: 'audio', data: PIECE}); // the Mac finished its step meanwhile
  held.socket().event({type: 'turn_end', sleep: false});
  await tick();
  assert.equal(held.clips.length, 1, 'The playing clip is frozen, not stopped or restarted');
  assert.equal(held.ctx().state, 'suspended');
  assert.equal(held.socket().sent.includes('resume'), false);
  await held.tap();
  assert.equal(held.clips.length, 2, 'Continue goes on with the same piece, the next right after it');
  assert.ok(Math.abs(held.clips[1].at - (held.clips[0].at + .1)) < 1e-9);
  held.clips[0].onended();
  await tick();
  assert.equal(held.socket().sent.includes('resume'), false);
  held.clips[1].onended();
  await tick();
  assert.equal(held.socket().sent.at(-1), 'resume');

  // Microphone refused on continue: stays paused, same conversation; a tap retries.
  const denied = harness();
  await denied.tap();
  await denied.tap();
  await denied.endCue();
  denied.options.denyMic = true;
  await denied.tap();
  assert.equal(denied.active(), 'false');
  assert.equal(denied.socket().closed, undefined);
  assert.equal(denied.socket().sent.at(-1), 'pause_session', 'The Mac stays paused');
  denied.options.denyMic = false;
  await denied.tap();
  assert.equal(denied.active(), 'true');
  assert.equal(denied.sockets.length, 1);

  // A tap while the microphone is coming back cancels; a late stream is stopped.
  const cancel = harness();
  await cancel.tap();
  await cancel.tap();
  await cancel.endCue();
  cancel.options.holdMic = true;
  await cancel.tap();
  assert.equal(cancel.active(), 'false');
  await cancel.tap();
  cancel.grantMic();
  await tick();
  assert.equal(cancel.track().readyState, 'ended');
  assert.equal(cancel.active(), 'false');
  assert.equal(cancel.socket().sent.includes('continue_session'), false);

  // The speech clock is resumed only after the microphone; a tap meanwhile keeps it frozen.
  const clock = harness();
  await clock.tap();
  await clock.tap();
  await clock.endCue();
  clock.options.holdResume = true;
  await clock.tap();
  assert.equal(clock.ctx().state, 'suspended');
  await clock.tap();
  clock.finishResume();
  await tick();
  assert.equal(clock.active(), 'false');
  assert.equal(clock.socket().sent.includes('continue_session'), false);

  // A slow first start: more taps each give feedback, but do not cancel it.
  const eager = harness({holdMic: true});
  for (let i = 0; i < 4; i++) await eager.tap();
  assert.equal(eager.animations, 4, 'Every tap is felt');
  assert.deepEqual(eager.vibrations, [12, 12, 12, 12]);
  assert.equal(eager.sockets.length, 1);
  assert.equal(eager.socket().closed, undefined);
  eager.grantMic();
  await tick();
  assert.equal(eager.active(), 'true', 'However many taps, it comes on');
  assert.equal(eager.track().readyState, 'live');
  await eager.tap();
  assert.equal(eager.active(), 'false', 'Once on, one tap pauses');

  // Unexpected messages are ignored; the Mac ends a conversation by closing the connection.
  const odd = harness();
  await odd.tap();
  for (const msg of ['not json', 'null', {type: 'error'}, {type: 'session', paused: true}, {type: 'state', state: 'x'}]) odd.socket().event(msg);
  await tick();
  assert.equal(odd.active(), 'true');
  assert.equal(odd.socket().closed, undefined);

  // Ends: background, page closed, a revoked microphone, a lost connection.
  for (const ending of [
    x => x.hide(),
    x => x.sandbox.dispatch('pagehide'),
    x => x.track().onended(),
    x => x.socket().onclose({code: 1006, reason: ''}),
  ]) {
    const x = harness();
    await x.tap();
    ending(x);
    await tick();
    assert.equal(x.active(), 'false');
    assert.equal(x.track().readyState, 'ended');
    assert.equal(x.socket().closed, true);
    assert.equal(x.ctx().state, 'closed');
    assert.equal(x.cues.at(-1), 'suspend', 'Ending is silent');
    x.frame();
    assert.equal(x.socket().frames, 0);
  }
  const back = harness();
  await back.tap();
  back.hide();
  back.sandbox.document.hidden = false;
  await tick();
  assert.equal(back.sockets.length, 1, 'Coming back stays gray until tapped');

  // Credentials: the address keeps the token, for Add to Home Screen; a rejected token is forgotten.
  const launch = harness({token: '', hash: '#token=launch'});
  assert.equal(launch.store.get('voice-token'), 'launch');
  assert.equal(launch.address, undefined, 'The token stays in the address');
  await launch.tap();
  assert.equal(launch.socket().sent[0], 'token:launch');
  const bare = harness({token: 'saved'});
  assert.equal(bare.address, '#token=saved', 'A saved token is put back into the address');
  const blocked = harness({blockStorage: true, hash: '#token=launch'});
  await blocked.tap();
  assert.equal(blocked.active(), 'true', 'Without storage the address alone is enough');
  const rejected = harness();
  await rejected.tap();
  rejected.socket().onclose({code: 1008, reason: 'Invalid access token'});
  assert.equal(rejected.store.has('voice-token'), false);
  for (const options of [{token: ''}, {insecure: true}]) {
    const none = harness(options);
    await none.tap();
    assert.equal(none.mics + none.sockets.length, 0);
    assert.equal(none.active(), 'false');
  }
  console.log('Voice UI passed: gray start, tap feedback, listening and replies, pause privacy, microphone released after the end sound, continue and quick continue, refusals and cancellations, ends and credentials.');
})().catch(error => { console.error(error); process.exitCode = 1; });
