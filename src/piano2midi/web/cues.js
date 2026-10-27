// The start and end sounds. They have their own audio clock, because pausing suspends the
// speech clock and must not freeze its own confirmation sound.
export function createCues() {
  const files = {};
  for (const name of ['active', 'paused']) {
    files[name] = fetch(`/cue-${name}.mp3`).then(r => (r.ok ? r.arrayBuffer() : null)).catch(() => null);
  }
  const buffers = {};
  let context = null, current = null, generation = 0;

  // Call synchronously inside every tap: iOS starts audio only from a user gesture.
  function unlock() {
    try {
      context ||= new AudioContext();
      if (context.state !== 'running') context.resume().catch(() => {});
      for (const name in files) {
        buffers[name] ||= files[name].then(data => data && context.decodeAudioData(data)).catch(() => null);
      }
    } catch {}
  }

  // Fade the playing cue out over 15 ms rather than cutting its waveform.
  function stop() {
    generation++;
    if (!current) return;
    const {source, gain} = current, now = context.currentTime;
    current = null;
    gain.gain.setValueAtTime(1, now);
    gain.gain.linearRampToValueAtTime(0, now + .015);
    source.stop(now + .015);
  }

  // Resolves when the cue has finished sounding, or at once if it cannot play.
  async function play(name) {
    stop();
    const id = generation;
    try {
      const buffer = await buffers[name];
      if (!buffer || id !== generation) return;
      if (context.state !== 'running') await context.resume();
      if (id !== generation) return;
      const source = context.createBufferSource(), gain = context.createGain();
      source.buffer = buffer;
      source.connect(gain).connect(context.destination);
      await new Promise(resolve => {
        source.onended = resolve;
        source.start();
        current = {source, gain};
      });
      if (current?.source === source) current = null;
      gain.disconnect();
    } catch {}
  }

  // When the conversation ends: silence any cue and let the audio hardware rest.
  function suspend() {
    generation++;
    current?.gain.disconnect();
    current = null;
    context?.suspend().catch(() => {});
  }

  return {unlock, stop, play, suspend};
}
