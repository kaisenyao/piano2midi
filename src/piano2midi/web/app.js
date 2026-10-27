import {animateOrb} from './orb.js';
import {createCues} from './cues.js';

const element = document.getElementById('orb');
const toggle = document.getElementById('voice-toggle');
const orb = animateOrb(element);
const cues = createCues();
const STATES = ['waiting', 'listening', 'thinking', 'speaking', 'conversation'];
const LISTENING = ['waiting', 'listening', 'conversation'];
const LABELS = {
  idle: '开启语音', starting: '正在连接', active: '语音已开启，点击暂停',
  paused: '已暂停，点击继续', resuming: '正在恢复，点击暂停',
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// The private address carries #token=…, and keeps it: Add to Home Screen saves this address,
// and the Home Screen app does not share Safari's storage.
let token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
try {
  token ||= localStorage.getItem('voice-token') || '';
  if (token) localStorage.setItem('voice-token', token);
} catch {}
if (token && !location.hash) history.replaceState(null, '', `#token=${encodeURIComponent(token)}`);

// idle (gray) → starting → active ⇄ paused → resuming → active. Any end returns to idle.
// `session` holds one connection's resources; every async step checks it is still current.
let phase = 'idle', session = null;

function render() {
  element.dataset.active = phase === 'active';
  element.dataset.waking = phase === 'starting' || phase === 'resuming';
  element.dataset.state = session?.state || 'idle';
  toggle.setAttribute('aria-label', LABELS[phase]);
}

function feedback(connected = false) {
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    element.animate?.(connected
      ? [{scale: 1}, {scale: .96, offset: .25}, {scale: 1.07, offset: .6}, {scale: 1}]
      : [{scale: 1}, {scale: .95}, {scale: 1}],
    {duration: connected ? 460 : 180, easing: 'ease-in-out'});
  }
  // The native switch gives its own haptic on iOS 18+; elsewhere, a short vibration.
  if (!connected) navigator.vibrate?.(12);
}

function stopTracks(stream) {
  stream?.getTracks().forEach(track => track.stop());
}

async function openMicrophone(s, t) {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true},
  });
  const [track] = stream.getAudioTracks();
  if (s !== session || s.transition !== t) { stopTracks(stream); return false; } // tapped away meanwhile
  if (track?.readyState !== 'live') { stopTracks(stream); throw Error('Microphone unavailable'); }
  track.enabled = false; // silent until activate()
  track.onended = () => { if (s.mic === stream) end(s); }; // revoked, or taken by a call
  s.mic = stream;
  return true;
}

function releaseMicrophone(s) {
  s.source?.disconnect();
  stopTracks(s.mic);
  s.source = s.mic = null;
}

// Nothing leaves the phone unless capture is on: the track is muted and the worklet discards input.
function capture(s, on) {
  s.mic?.getAudioTracks().forEach(track => { track.enabled = on; });
  s.node?.port.postMessage(on);
}

function send(s, type) {
  if (s.socket.readyState === WebSocket.OPEN) s.socket.send(JSON.stringify({type}));
}

function end(s = session) {
  if (!s || s !== session) return;
  session = null;
  phase = 'idle';
  clearTimeout(s.timeout);
  cancelAnimationFrame(s.frame);
  s.open(); // waiting speech sees the session is gone
  s.socket?.close();
  stopTracks(s.mic);
  s.ctx?.close().catch(() => {});
  s.wakeLock?.release().catch(() => {});
  cues.suspend();
  orb.level(0);
  render();
}

// Resolves true once the session is active again, or false if it ended meanwhile.
async function whenActive(s) {
  while (s === session && phase !== 'active') await s.paused;
  return s === session;
}

function activate(s) {
  phase = 'active';
  capture(s, true);
  s.open();
  render();
  cues.play('active');
}

function pause(s) {
  const t = ++s.transition;
  if (phase === 'active') s.paused = new Promise(resolve => { s.open = resolve; });
  phase = 'paused';
  capture(s, false); // nothing more leaves the phone, at once
  s.ctx.suspend().catch(() => {}); // her speech stops exactly where it is
  send(s, 'pause_session'); // the Mac holds the next step of a task and pauses the piano
  orb.level(0);
  render();
  // Release the (already silent) microphone only after the end sound: stopping it reconfigures
  // iOS audio, which pops and swallows whatever is playing at that moment.
  Promise.race([cues.play('paused'), delay(3000)]).then(() => {
    if (s === session && s.transition === t) releaseMicrophone(s);
  });
}

async function resume(s) {
  const t = ++s.transition;
  phase = 'resuming';
  render();
  try {
    if (!s.mic) {
      if (!await openMicrophone(s, t)) return;
      s.source = s.ctx.createMediaStreamSource(s.mic);
      s.source.connect(s.node);
    }
    await s.ctx.resume();
    if (s !== session || s.transition !== t) return;
    send(s, 'continue_session');
    activate(s);
  } catch {
    // Microphone refused or unavailable: stay paused, conversation kept; a tap retries.
    if (s !== session || s.transition !== t) return;
    releaseMicrophone(s);
    phase = 'paused';
    render();
  }
}

// Her voice arrives in pieces of 24 kHz PCM16 while it is being made; they play back to back.
async function play(s, data) {
  const pcm = new Int16Array(Uint8Array.from(atob(data), c => c.charCodeAt(0)).buffer);
  if (!await whenActive(s)) return;
  const audio = s.ctx.createBuffer(1, pcm.length, 24000);
  audio.getChannelData(0).set(Float32Array.from(pcm, value => value / 32768));
  const clip = s.ctx.createBufferSource();
  clip.buffer = audio;
  clip.connect(s.analyser);
  const at = Math.max(s.ctx.currentTime, s.until);
  s.until = at + audio.duration;
  s.clips.add(clip);
  s.spoken = new Promise(resolve => {
    clip.onended = () => { clip.disconnect(); s.clips.delete(clip); resolve(); };
  });
  clip.start(at);
}

function onFrame(s, {pcm, level}) {
  if (s !== session || phase !== 'active') return;
  const listening = LISTENING.includes(s.state);
  if (!s.clips.size) orb.level(listening ? (level - .008) * 8 : 0);
  // Drop rather than queue audio when the network falls about a second behind.
  if (listening && s.socket.readyState === WebSocket.OPEN && s.socket.bufferedAmount < 32000) s.socket.send(pcm);
}

function meter(s) {
  if (s !== session) return;
  if (phase === 'active' && s.clips.size) {
    s.analyser.getByteTimeDomainData(s.samples);
    let sum = 0;
    for (const value of s.samples) sum += ((value - 128) / 128) ** 2;
    orb.level(Math.sqrt(sum / s.samples.length) * 8);
  }
  s.frame = requestAnimationFrame(() => meter(s));
}

function connect(s) {
  return new Promise((resolve, reject) => {
    const socket = s.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/voice`);
    s.timeout = setTimeout(() => end(s), 120000); // the first start may load models
    socket.onopen = () => socket.send(JSON.stringify({token}));
    socket.onclose = event => {
      if (event.code === 1008 && event.reason === 'Invalid access token') {
        token = '';
        try { localStorage.removeItem('voice-token'); } catch {}
      }
      reject(Error('Connection closed'));
      end(s);
    };
    // Anything unexpected is ignored: the Mac ends a conversation by closing the connection.
    socket.onmessage = ({data}) => {
      if (s !== session) return;
      let msg = null;
      try { msg = JSON.parse(data); } catch {}
      if (msg?.type === 'ready') {
        clearTimeout(s.timeout);
        resolve();
      } else if (msg?.type === 'state' && STATES.includes(msg.state)) {
        s.state = msg.state;
        render();
      } else if (msg?.type === 'audio' && typeof msg.data === 'string') {
        s.playback = s.playback.then(() => play(s, msg.data)).catch(() => {}); // a bad piece is skipped
      } else if (msg?.type === 'turn_end') {
        // Ask for the next turn only once her reply has actually been heard.
        s.playback = s.playback.then(async () => {
          await s.spoken;
          if (await whenActive(s)) send(s, msg.sleep ? 'sleep' : 'resume');
        });
      }
    };
  });
}

async function start() {
  if (!token || !window.isSecureContext || !navigator.mediaDevices) return render();
  const s = session = {transition: 0, state: 'idle', playback: Promise.resolve(),
    clips: new Set(), until: 0, spoken: Promise.resolve()};
  s.paused = new Promise(resolve => { s.open = resolve; });
  phase = 'starting';
  render();
  try {
    s.ctx = new AudioContext();
    const resumed = s.ctx.resume(); // inside the tap
    const ready = connect(s); // the Mac prepares while the phone asks for the microphone
    ready.catch(() => {});
    await resumed;
    if (!await openMicrophone(s, 0)) return;
    await s.ctx.audioWorklet.addModule('/capture.js');
    if (s !== session) return;
    const {ctx} = s;
    s.analyser = ctx.createAnalyser();
    s.analyser.fftSize = 256;
    s.samples = new Uint8Array(256);
    s.analyser.connect(ctx.destination);
    s.node = new AudioWorkletNode(ctx, 'phone-capture');
    s.node.port.onmessage = event => onFrame(s, event.data);
    const silent = ctx.createGain();
    silent.gain.value = 0;
    s.node.connect(silent).connect(ctx.destination); // keeps the worklet running, inaudibly
    s.source = ctx.createMediaStreamSource(s.mic);
    s.source.connect(s.node);
    meter(s);
    await ready;
    if (s !== session) return;
    activate(s);
    feedback(true);
    const lock = await navigator.wakeLock?.request('screen').catch(() => null);
    if (s === session) s.wakeLock = lock;
    else lock?.release().catch(() => {});
  } catch { end(s); }
}

// The hidden switch flips on every tap, so every tap gets the system's haptic tick; what a tap
// does is decided here. While the first start is under way, more taps do not cancel it.
toggle.onchange = () => {
  feedback();
  if (phase === 'starting') return;
  cues.unlock(); // iOS lets audio start only inside the tap
  cues.stop();
  if (phase === 'idle') start();
  else if (phase === 'paused') resume(session);
  else pause(session);
};
document.addEventListener('gesturestart', event => event.preventDefault()); // Safari's own pinch zoom
document.addEventListener('visibilitychange', () => { if (document.hidden) end(); });
addEventListener('pagehide', () => end());
render();
