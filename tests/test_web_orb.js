// Exercise rendering lifecycle and motion invariants, without a GPU or browser.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('src/piano2midi/web/orb.js', 'utf8').replace('export function animateOrb', 'function animateOrb');
function harness({active = false, denied = false, reduced = false} = {}) {
  const frames = new Map(), canvasEvents = {}, documentEvents = {}, styles = {}, classes = new Set(), uniforms = {};
  let sequence = 0, draws = 0, deleted = 0;
  const canvas = {width: 0, height: 0, hidden: false, addEventListener: (name, fn) => canvasEvents[name] = fn};
  const gl = {canvas, createProgram: () => ({}), createShader: () => ({}), createBuffer: () => ({}),
    getShaderParameter: () => true, getProgramParameter: () => true, getAttribLocation: () => 0,
    getUniformLocation: (_, name) => name, uniform1f: (name, value) => uniforms[name] = value,
    drawArrays: () => draws++, deleteShader: () => deleted++};
  for (const name of ['shaderSource', 'compileShader', 'attachShader', 'linkProgram', 'useProgram', 'bindBuffer', 'bufferData', 'enableVertexAttribArray', 'vertexAttribPointer', 'viewport', 'deleteBuffer', 'deleteProgram']) gl[name] = () => {};
  canvas.getContext = () => { if (denied) throw Error('WebGL blocked'); return gl; };
  const element = {clientWidth: 200, querySelector: () => canvas, dataset: {active: String(active), state: 'waiting'},
    classList: {add: name => classes.add(name), remove: name => classes.delete(name)}, style: {setProperty: (name, value) => styles[name] = value}};
  const sandbox = {element, matchMedia: () => ({matches: reduced}), document: {hidden: false, addEventListener: (name, fn) => documentEvents[name] = fn},
    devicePixelRatio: 2, ResizeObserver: class { constructor(fn) { this.fn = fn; } observe() { this.fn(); } },
    requestAnimationFrame(fn) { const id = ++sequence; frames.set(id, fn); return id; }, cancelAnimationFrame: id => frames.delete(id)};
  vm.createContext(sandbox); vm.runInContext(source, sandbox);
  const orb = vm.runInContext('animateOrb(element)', sandbox);
  function step(now) { const batch = [...frames.values()]; frames.clear(); for (const fn of batch) fn(now); }
  return {orb, step, sandbox, canvas, element, frames, classes, styles, uniforms, canvasEvents, documentEvents, get draws() { return draws; }, get deleted() { return deleted; }};
}
const gray = harness(), blue = harness({active: true});
for (const now of [1000, 1040, 1080, 1120, 1160]) { gray.step(now); blue.step(now); }
assert.equal(gray.uniforms.time, blue.uniforms.time, 'Color must not change flow speed');
assert.equal(gray.styles['--scale'], blue.styles['--scale'], 'Color must not change breathing');
assert.notEqual(gray.uniforms.quiet, blue.uniforms.quiet);
assert.equal(gray.deleted, 2, 'Compiled shader handles must be released after linking');
const before = Number(blue.styles['--scale']); blue.orb.level(.8); blue.step(1200);
assert.ok(Number(blue.styles['--scale']) > before, 'Real volume drives scale');
blue.orb.level(NaN); blue.step(1240); assert.ok(Number.isFinite(Number(blue.styles['--scale'])));
const time = gray.uniforms.time;
gray.sandbox.document.hidden = true; gray.documentEvents.visibilitychange(); assert.equal(gray.frames.size, 0);
gray.sandbox.document.hidden = false; gray.documentEvents.visibilitychange(); gray.step(100000);
assert.equal(gray.uniforms.time, time, 'Resuming must not jump the animation clock');
gray.canvasEvents.webglcontextlost({preventDefault() {}}); const draws = gray.draws;
gray.step(100040); assert.equal(gray.draws, draws); assert.ok(gray.classes.has('orb-fallback'));
gray.canvasEvents.webglcontextrestored(); gray.step(100080);
assert.equal(gray.canvas.hidden, false); assert.ok(gray.draws > draws); assert.equal(gray.classes.size, 0);
const fallback = harness({denied: true}); fallback.step(1000); fallback.step(1040);
assert.equal(fallback.canvas.hidden, true); assert.ok(fallback.classes.has('orb-fallback'));
const waking = harness();
waking.element.dataset.waking = 'true';
for (const now of [1000, 1040, 1080, 1120, 1160, 1200, 1240, 1280]) waking.step(now);
assert.ok(waking.uniforms.quiet > .4 && waking.uniforms.quiet < .7, 'Half colored while starting');
const still = harness({active: true, reduced: true}); still.orb.level(1); still.step(1000); still.step(1100);
assert.equal(still.styles['--scale'], '1'); assert.equal(still.uniforms.time, 0);
console.log('Orb passed: gray/color motion parity, volume response, reduced motion, background suspension and GPU loss/fallback/recovery.');
