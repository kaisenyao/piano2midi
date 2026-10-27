// All textures are generated on the device; gray and color share the same motion.
const vertex = `attribute vec2 position; varying vec2 p;
void main() { p = position; gl_Position = vec4(position, 0., 1.); }`;
const fragment = `
        precision highp float;
        varying vec2 p;
        uniform float time,level,quiet;
        float hash(vec2 v){return fract(sin(dot(v,vec2(127.1,311.7)))*43758.5453);}
        float noise(vec2 v){vec2 i=floor(v),f=fract(v);f=f*f*(3.-2.*f);return mix(mix(hash(i),hash(i+vec2(1.,0.)),f.x),mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),f.x),f.y);}
        float fbm(vec2 v){float f=0.,a=.5;for(int i=0;i<4;i++){f+=a*noise(v);v=mat2(.8,-.6,.6,.8)*v*2.03+7.1;a*=.5;}return f;}
        void main(){
          float t=time;
          float dome=sqrt(max(0.,1.-dot(p,p)));
          vec2 uv=p*(1.+.12*dome);
          vec2 drift=vec2(t*.12,-t*.09);
          vec2 warp=vec2(fbm(uv*1.7+drift),fbm(uv*1.7-drift+4.7));
          float cloud=fbm(uv*1.8+warp*1.2+vec2(-t*.14,t*.08));
          float curl=fbm(uv*2.8+warp*1.6+vec2(t*.07,0.));
          float boundary=(cloud-.46)*1.1+(curl-.5)*.14;
          float white=1.-smoothstep(-.34,.37,p.y-boundary-.05);
          vec3 blue=mix(vec3(.17,.38,.92),vec3(.34,.57,1.),cloud);
          vec3 milk=mix(vec3(.65,.75,.98),vec3(.98,.99,1.),smoothstep(-.9,.4,p.y)+cloud*.18);
          vec3 color=mix(blue,milk,white);
          color+=vec3(.035,.04,.045)*dome*(.5+.5*sin(time*1.4)+level*.5);
          color=mix(color,vec3(dot(color,vec3(.22,.65,.13)))*.42,quiet);
          gl_FragColor=vec4(color,1.);
        }`;

function renderer(gl) {
  const program = gl.createProgram();
  const shaders = [];
  let buffer;
  try {
    for (const [type, source] of [[gl.VERTEX_SHADER, vertex], [gl.FRAGMENT_SHADER, fragment]]) {
      const shader = gl.createShader(type);
      shaders.push(shader);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw Error('Shader unavailable');
      gl.attachShader(program, shader);
    }
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw Error('Orb program unavailable');
    gl.useProgram(program);
    buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'position');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const uniforms = Object.fromEntries(['time', 'level', 'quiet'].map(name => [name, gl.getUniformLocation(program, name)]));
    return (time, level, quiet) => {
      gl.viewport(0, 0, gl.canvas.width, gl.canvas.height);
      gl.uniform1f(uniforms.time, time);
      gl.uniform1f(uniforms.level, level);
      gl.uniform1f(uniforms.quiet, quiet);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    };
  } catch {
    if (buffer) gl.deleteBuffer(buffer);
    gl.deleteProgram(program);
    return null;
  } finally {
    for (const shader of shaders) gl.deleteShader(shader);
  }
}

export function animateOrb(element) {
  const canvas = element.querySelector('canvas');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  let gl, render, input = 0, volume = 0, last = 0, clock = 0, quiet = 1, frame = null;
  const fallback = () => {
    canvas.hidden = true;
    element.classList.add('orb-fallback');
    render = () => {};
  };
  try { gl = canvas.getContext('webgl', {alpha: false, antialias: false, powerPreference: 'low-power'}); } catch {}
  render = gl ? renderer(gl) : null;
  if (!render) fallback();
  const resize = () => {
    const size = Math.round(element.clientWidth * Math.min(devicePixelRatio || 1, 2));
    if (canvas.width !== size) canvas.width = canvas.height = size;
  };
  new ResizeObserver(resize).observe(element);
  resize();

  function draw(now) {
    frame = null;
    if (document.hidden) return;
    frame = requestAnimationFrame(draw);
    if (last && now - last < 32) return;
    const dt = last ? Math.min((now - last) / 1000, .1) : 0;
    last = now;
    const blend = 1 - Math.exp(-dt * 9);
    volume += (input - volume) * blend;
    // Gray when off, color when active, and half way while it is starting up.
    const target = element.dataset.active === 'true' ? 0 : element.dataset.waking === 'true' ? .5 : 1;
    quiet += (target - quiet) * blend;
    const energy = element.dataset.state === 'speaking' ? .7 : element.dataset.state === 'thinking' ? .4 : .16;
    if (!reduced.matches) clock += dt * (1 + energy + volume * 1.5);
    render(clock, reduced.matches ? 0 : volume, quiet);
    element.style.setProperty('--scale', reduced.matches ? '1' : String(1 + Math.sin(clock * 1.4) * .012 + volume * .11));
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelAnimationFrame(frame); frame = null; last = 0; }
    else if (frame === null) frame = requestAnimationFrame(draw);
  });
  canvas.addEventListener('webglcontextlost', event => { event.preventDefault(); fallback(); });
  canvas.addEventListener('webglcontextrestored', () => {
    const restored = gl && renderer(gl);
    if (restored) { render = restored; canvas.hidden = false; element.classList.remove('orb-fallback'); }
  });
  if (!document.hidden) frame = requestAnimationFrame(draw);
  return {level(value) { input = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0; }};
}
