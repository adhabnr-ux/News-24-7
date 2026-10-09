/* Foretape launch scene: a live WebGL "photograph".
 *
 * The backdrop is a real launch photo (launch.webp) with a painted depth map (depth.png), rendered
 * by one fragment shader that adds everything a still can't have:
 *   - parallax from pointer / gyro tilt and from scroll (near smoke moves more than far sky)
 *   - drifting clouds and billowing smoke (flow-noise warps, masked away from the rocket)
 *   - the plume flickers, heat shimmers above the pad, embers rise and cool
 *   - cheap bloom, film grain, vignette, chromatic fringing and a warm/cool grade
 *   - the camera climbs with the page: through the cirrus, past the photo's top edge, into a
 *     starfield (altitude is continuous, so scrolling feels like riding the rocket)
 * Without WebGL it falls back to the plain photo with CSS parallax.
 */
(function () {
  "use strict";

  const VERT = `
attribute vec2 aPos;
varying vec2 vUv;
void main() { vUv = aPos * 0.5 + 0.5; vUv.y = 1.0 - vUv.y; gl_Position = vec4(aPos, 0.0, 1.0); }`;

  const FRAG = `
precision highp float;
varying vec2 vUv;
uniform sampler2D uPhoto;
uniform sampler2D uDepth;
uniform vec2  uRes;
uniform float uTime;
uniform float uClimb;
uniform vec2  uTilt;
uniform float uIntro;
uniform float uAspect;
uniform float uMotion;
uniform float uPulse;
uniform float uMood;
uniform float uTension;
uniform vec2  uTexel;
uniform float uSharp;

float hash(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32); return fract(p.x * p.y); }
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), f.x), f.y);
}
float fbm(vec2 p) {
  float v = 0.0, a = 0.5;
  for (int i = 0; i < 4; i++) { v += a * noise(p); p = p * 2.03 + 11.7; a *= 0.5; }
  return v;
}
float luma(vec3 c) { return dot(c, vec3(0.299, 0.587, 0.114)); }

vec3 photo(vec2 uv) { return texture2D(uPhoto, clamp(uv, vec2(0.001, 0.0005), vec2(0.999, 0.9995))).rgb; }

// distant stars + a faint galactic band, shown above the top of the photograph
vec3 space(vec2 uv, float amt, float t) {
  vec2 p = vec2(uv.x * uAspect, uv.y) * 90.0;
  vec2 g = floor(p), f = fract(p) - 0.5;
  float h = hash(g);
  float on = step(0.945, h);
  float tw = 0.6 + 0.4 * sin(t * (1.0 + 3.0 * hash(g + 3.1)) + h * 40.0);
  float star = on * (1.0 - smoothstep(0.0, 0.22, length(f + (hash(g + 7.7) - 0.5) * 0.5))) * tw;
  vec2 q = vec2(uv.x * uAspect, uv.y) * 22.0;
  vec2 fq = fract(q) - 0.5;
  float bigOn = step(0.985, hash(floor(q)));
  float core = exp(-dot(fq, fq) * 420.0);                                                    // a hard little core...
  float spikes = (exp(-abs(fq.x) * 90.0) * exp(-abs(fq.y) * 9.0) + exp(-abs(fq.y) * 90.0) * exp(-abs(fq.x) * 9.0)) * 0.32;   // ...with diffraction spikes
  float big = bigOn * (core + spikes) * (0.75 + 0.25 * sin(t * 2.0 + h * 9.0));
  float band = 1.0 - smoothstep(0.0, 0.55, abs((uv.x * uAspect - uv.y * 0.9) - 0.15 - 0.2 * fbm(uv * 3.0)));
  vec3 milky = vec3(0.30, 0.38, 0.62) * band * (0.25 + 0.55 * fbm(uv * 14.0)) * 0.55;
  return (vec3(0.85, 0.9, 1.0) * (star * 1.25 + big * 1.1) + milky) * amt;
}

void main() {
  float t = uTime * uMotion;
  vec2 s = vUv;
  float sa = uRes.x / uRes.y;

  // ---- camera: starts on the pad, climbs with scroll
  float hh0 = min(0.5, 0.5 * uAspect / sa);
  float k1 = smoothstep(0.0, 0.55, uClimb);
  float k2 = smoothstep(0.35, 2.2, uClimb);
  float zoom = (1.0 + 0.38 * k1 + 0.30 * k2) * (1.0 + 0.11 * (1.0 - uIntro) * (1.0 - uIntro));
  float anchor = mix(0.5, 0.565, 1.0 - smoothstep(0.2, 0.5, hh0));   // wide screens frame the whole rocket + plume
  float cy = mix(anchor, anchor - 0.30, k1) - 0.75 * k2;
  float hh = hh0 / zoom, hw = hh * sa / uAspect;
  // handheld breathing, big at ignition, barely there afterwards
  float shake = (0.0012 + 0.012 * (1.0 - uIntro) * (1.0 - uIntro) * (1.0 - uIntro) + 0.004 * uPulse) * uMotion;
  vec2 sh = vec2(noise(vec2(t * 3.1, 1.7)) - 0.5, noise(vec2(2.9, t * 3.3)) - 0.5) * shake;
  vec2 c = vec2(0.5, cy) + sh;
  vec2 uv0 = c + (s - 0.5) * 2.0 * vec2(hw, hh);

  // ---- depth parallax (tilt + climb)
  float d = texture2D(uDepth, clamp(uv0, 0.0, 1.0)).r;
  float dz = d - 0.32;
  vec2 par = vec2(-uTilt.x * 0.020, -uTilt.y * 0.012) * dz - vec2(0.0, uClimb * 0.060) * dz;
  vec2 uv = uv0 + par;

  // ---- living clouds and smoke (masked so the rocket and tower never wobble)
  float colX = abs(uv.x - 0.49);
  float noRocket = smoothstep(0.035, 0.085, colX) + (1.0 - smoothstep(0.43, 0.46, uv.y)) + smoothstep(0.69, 0.72, uv.y);
  noRocket = clamp(noRocket, 0.0, 1.0);
  float cloudBand = smoothstep(0.24, 0.40, uv.y) * (1.0 - smoothstep(0.64, 0.80, uv.y));
  vec2 fl = vec2(fbm(uv * 5.0 + vec2(t * 0.020, 0.0)), fbm(uv * 5.0 + vec2(7.1, -t * 0.016))) - 0.5;
  uv += fl * 0.0085 * cloudBand * noRocket;
  float smokeL = (1.0 - smoothstep(0.0, 0.34, uv.x)) * smoothstep(0.44, 0.52, uv.y) * (1.0 - smoothstep(0.70, 0.78, uv.y));
  float smokeR = smoothstep(0.62, 1.0, uv.x) * smoothstep(0.40, 0.48, uv.y) * (1.0 - smoothstep(0.72, 0.80, uv.y));
  float smoke = max(smokeL, smokeR) + 0.6 * smoothstep(0.62, 0.66, uv.y) * (1.0 - smoothstep(0.72, 0.76, uv.y)) * noRocket;
  vec2 rise = vec2(fbm(uv * vec2(9.0, 7.0) + vec2(t * 0.05, -t * 0.22)), fbm(uv * vec2(7.0, 9.0) + vec2(-t * 0.04, -t * 0.28))) - 0.5;
  uv += rise * 0.014 * smoke * noRocket;

  // ---- heat shimmer above the pad
  float pdx = (uv.x - 0.49) / 0.075;
  float pad = exp(-pdx * pdx) * smoothstep(0.50, 0.62, uv.y) * (1.0 - smoothstep(0.69, 0.74, uv.y));
  uv.x += sin(uv.y * 170.0 - t * 9.0 + noise(uv * 40.0) * 5.0) * 0.0011 * pad;

  // ---- the photograph, with a touch of lateral chromatic fringing toward the edges
  vec2 ca = (s - 0.5) * 0.0016;
  vec3 col = vec3(photo(uv + ca * vec2(1.0, 0.5)).r, photo(uv).g, photo(uv - ca * vec2(1.0, 0.5)).b);

  // ---- local contrast: the photo is a 2x upscale, so a light unsharp mask restores crispness at retina scale
  vec2 tx = uTexel * 1.35;
  vec3 nb = (photo(uv + vec2(tx.x, 0.0)) + photo(uv - vec2(tx.x, 0.0)) + photo(uv + vec2(0.0, tx.y)) + photo(uv - vec2(0.0, tx.y))) * 0.25;
  col += (col - nb) * 0.7 * uSharp;

  // ---- bloom from the brightest parts (plume, sunlit cloud tops)
  vec3 bl = vec3(0.0);
  for (int i = 0; i < 6; i++) {
    float a = float(i) * 1.0472 + 0.4;
    vec2 o = vec2(cos(a), sin(a) * uAspect * 0.9) * (0.010 + 0.006 * float(i - (i / 2) * 2));
    vec3 q = photo(uv + o);
    bl += q * smoothstep(0.62, 1.0, luma(q));
  }
  bl /= 6.0;
  col += bl * vec3(1.05, 0.78, 0.5) * 0.55;

  // ---- plume: white-hot core that flickers, ignition flash
  float pcx = (uv.x - 0.488) / 0.0105;
  float px = exp(-pcx * pcx);
  float pyy = smoothstep(0.545, 0.60, uv.y) * (1.0 - smoothstep(0.672, 0.700, uv.y));
  float fk = 0.78 + 0.22 * noise(vec2(uv.y * 70.0 - t * 22.0, t * 2.3)) + 0.12 * sin(t * 31.0);
  vec3 hot = vec3(1.0, 0.82, 0.58) * px * pyy * fk * (0.34 + 0.9 * (1.0 - uIntro) * (1.0 - uIntro) + 0.8 * uPulse);
  col += hot;
  float bl0 = length((uv - vec2(0.489, 0.676)) * vec2(sa * 0.9, 1.3)) / 0.055;
  float base = exp(-bl0 * bl0);
  col += vec3(1.0, 0.55, 0.22) * base * (0.18 + 0.12 * fk + 0.8 * (1.0 - uIntro) * (1.0 - uIntro) + 0.7 * uPulse);

  // ---- embers rising from the pad (only evaluated near the pad: the loop is the costliest part)
  if (uv.x > 0.20 && uv.x < 0.78 && uv.y > 0.30 && uv.y < 0.74 && uMotion > 0.0) {
    for (int i = 0; i < 28; i++) {
      float id = float(i);
      float life = fract(t * (0.10 + 0.07 * hash(vec2(id, 9.0))) + hash(vec2(id, 1.0)));
      vec2 b = vec2(0.488 + (hash(vec2(id, 2.0)) - 0.5) * 0.10, 0.672 - hash(vec2(id, 5.0)) * 0.015);
      vec2 v = vec2((hash(vec2(id, 3.0)) - 0.5) * 0.34, -0.06 - 0.20 * hash(vec2(id, 4.0)));
      vec2 p = b + v * life + vec2(sin(life * 7.0 + id) * 0.012 * life, 0.05 * life * life);
      vec2 dd = (uv - p) * vec2(uAspect, 1.0);
      float sz = 0.0016 * (1.0 - life) + 0.0004;
      float g = exp(-dot(dd, dd) / (sz * sz)) * (1.0 - life) * (0.6 + 0.4 * sin(t * 12.0 + id * 3.0));
      col += vec3(1.0, 0.62 + 0.3 * life, 0.28) * g * 0.9;
    }
  }

  // ---- sunlit glints on the wet ground
  if (uv.y > 0.72) {
    float wet = smoothstep(0.72, 0.80, uv.y);
    float gl = pow(max(noise(uv * vec2(420.0, 260.0) + vec2(t * 0.6, 0.0)), 0.0001), 14.0) * smoothstep(0.35, 0.8, luma(col));
    col += vec3(1.0, 0.8, 0.55) * gl * wet * 0.8;
  }

  // ---- above the top of the photograph: the sky deepens into space
  if (uv0.y < 0.18) {
    float over = -uv0.y;
    vec3 topCol = (photo(vec2(0.12, 0.012)) + photo(vec2(0.30, 0.012)) + photo(vec2(0.50, 0.012)) + photo(vec2(0.70, 0.012)) + photo(vec2(0.88, 0.012))) * 0.2;
    float alt = smoothstep(0.0, 0.65, over);
    vec3 skyAbove = mix(topCol, vec3(0.010, 0.026, 0.080), pow(max(alt, 0.0001), 0.6));
    skyAbove += vec3(0.30, 0.20, 0.12) * pow(max(1.0 - alt, 0.0001), 8.0) * 0.05;
    vec3 above = skyAbove;
    if (over > 0.05) above += space(uv0, smoothstep(0.05, 0.55, over), t);
    float seam = 1.0 - smoothstep(0.0, 0.16, uv0.y);          // feather the photo's top edge into the sky
    col = mix(col, above, clamp(seam * seam * (3.0 - 2.0 * seam) + step(uv0.y, 0.0), 0.0, 1.0));
  }

  // ---- the lower frame darkens a little as we leave the pad behind
  col *= 1.0 - 0.18 * k1;

  // ---- grade: filmic curve, warm highlights, cool shadows, vignette, grain, ignition fade-in
  col *= 0.35 + 0.65 * smoothstep(0.0, 1.0, uIntro) + 0.0;
  col = col / (1.0 + 0.10 * col);                       // gentle shoulder
  col = pow(max(col, vec3(0.0001)), vec3(0.97));
  float l = luma(col);
  col = mix(col, col * vec3(1.06, 1.0, 0.93), smoothstep(0.45, 0.95, l) * 0.6);
  col = mix(col, col * vec3(0.92, 1.0, 1.10), (1.0 - smoothstep(0.0, 0.35, l)) * 0.5);
  float vig = 1.0 - smoothstep(0.30, 1.15, length((s - 0.5) * vec2(1.0, 1.15)));
  col *= mix(0.62, 1.0, vig);
  col *= mix(0.88, 1.04, uMood);                        // dusk while the market is closed, brighter while it's open
  col = mix(vec3(luma(col)), col, mix(0.92, 1.06, uMood));
  float edge = 1.0 - vig;                               // a recent CRITICAL alert warms the edges of the frame
  col += vec3(0.55, 0.16, 0.04) * edge * edge * uTension * 0.55;
  float gr = hash(s * uRes + fract(uTime * 7.0)) - 0.5;
  col += gr * 0.028 * (1.0 - l * 0.5);
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}`;

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src); gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
    return sh;
  }
  function loadImage(src) {
    return new Promise((res, rej) => { const im = new Image(); im.decoding = "async"; im.onload = () => res(im); im.onerror = () => rej(new Error("image " + src)); im.src = src; });
  }

  const Scene = {
    ok: false, climb: 0, climbTarget: 0, tilt: [0, 0], tiltTarget: [0, 0], intro: 0, introStart: null,
    scale: 1, frames: 0, slow: 0, paused: false, reduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
    onReady: null,

    async start(canvas, opts) {
      opts = opts || {};
      this.canvas = canvas;
      const q = parseFloat(new URLSearchParams(location.search).get("scale"));  // ?scale=0.4 renders fewer pixels (testing, slow GPUs)
      if (q > 0.2 && q <= 1) this.scale = q;
      const photoUrl = opts.photo || "/app/launch.webp", depthUrl = opts.depth || "/app/depth.png";
      let gl = canvas.getContext("webgl2", { antialias: false, alpha: false, powerPreference: "high-performance" })
        || canvas.getContext("webgl", { antialias: false, alpha: false, powerPreference: "high-performance" });
      if (!gl) return this.fallback(photoUrl);
      try {
        const prog = gl.createProgram();
        gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
        gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
        gl.linkProgram(prog);
        if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
        gl.useProgram(prog);
        const buf = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
        const loc = gl.getAttribLocation(prog, "aPos");
        gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
        const [photo, depth] = await Promise.all([loadImage(photoUrl), loadImage(depthUrl)]);
        const isGL2 = typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext;
        const tex = (unit, img, mip) => {
          const t = gl.createTexture();
          gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
          gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          if (mip && isGL2) { gl.generateMipmap(gl.TEXTURE_2D); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR); }
          else gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        };
        tex(0, photo, true); tex(1, depth, false);
        this.gl = gl;
        this.u = {};
        ["uPhoto", "uDepth", "uRes", "uTime", "uClimb", "uTilt", "uIntro", "uAspect", "uMotion", "uPulse", "uMood", "uTension", "uTexel", "uSharp"].forEach((n) => (this.u[n] = gl.getUniformLocation(prog, n)));
        gl.uniform1i(this.u.uPhoto, 0); gl.uniform1i(this.u.uDepth, 1);
        gl.uniform1f(this.u.uAspect, photo.width / photo.height);
        gl.uniform2f(this.u.uTexel, 1 / photo.width, 1 / photo.height);
        gl.uniform1f(this.u.uSharp, opts.sharp ?? parseFloat(new URLSearchParams(location.search).get("sharp") ?? "1"));
        this.resize();
        addEventListener("resize", () => this.resize());
        document.addEventListener("visibilitychange", () => { this.paused = document.hidden; if (!this.paused) this.loop(performance.now()); });
        this.aspect = photo.width / photo.height;
        this.ok = true;
        document.documentElement.classList.add("gl");
        this.t0 = performance.now();
        requestAnimationFrame((n) => this.loop(n));
        if (this.onReady) this.onReady();
      } catch (e) {
        console.warn("scene: falling back to the photo", e);
        this.fallback(photoUrl);
      }
    },

    fallback(photoUrl) {
      document.documentElement.classList.add("nogl");
      document.body.style.setProperty("--photo", `url(${photoUrl})`);
      if (this.onReady) this.onReady();
    },

    resize() {
      if (!this.gl) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2) * this.scale;
      const w = Math.max(2, Math.round(innerWidth * dpr)), h = Math.max(2, Math.round(innerHeight * dpr));
      if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; }
      this.gl.viewport(0, 0, w, h);
      this.gl.uniform2f(this.u.uRes, w, h);
    },

    /* Where a point of the photograph (u, v in 0..1) is on screen right now: the same camera as the
       shader, so interface callouts can ride on the rocket. depth is the point's painted depth. */
    project(u, v, depth) {
      const w = innerWidth, h = innerHeight, sa = w / h, ia = this.aspect || 0.566;
      const sm = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
      const hh0 = Math.min(0.5, (0.5 * ia) / sa), cl = this.climb;
      const k1 = sm(0, 0.55, cl), k2 = sm(0.35, 2.2, cl), iv = 1 - this.intro;
      const zoom = (1 + 0.38 * k1 + 0.3 * k2) * (1 + 0.11 * iv * iv);
      const anchor = 0.5 + 0.065 * (1 - sm(0.2, 0.5, hh0));
      const cy = anchor + (anchor - 0.3 - anchor) * k1 - 0.75 * k2;
      const hh = hh0 / zoom, hw = (hh * sa) / ia, dz = (depth ?? 0.46) - 0.32;
      const px = -this.tilt[0] * 0.02 * dz, py = -this.tilt[1] * 0.012 * dz - cl * 0.06 * dz;
      const u0 = u - px, v0 = v - py;
      return [((u0 - 0.5) / (2 * hw) + 0.5) * w, ((v0 - cy) / (2 * hh) + 0.5) * h];
    },

    ignite() { this.introStart = performance.now(); },
    pulse() { this.pulseAt = performance.now(); },
    setMood(open, tension) { this.moodTarget = open ? 1 : 0; this.tensionTarget = tension || 0; },
    setClimb(v) { this.climbTarget = v; },
    setTilt(x, y) { this.tiltTarget = [Math.max(-1, Math.min(1, x)), Math.max(-1, Math.min(1, y))]; },

    loop(now) {
      if (this.paused || !this.gl) return;
      const dt = Math.min(0.1, (now - (this.last || now)) / 1000); this.last = now;
      // the camera is a critically-damped spring: it glides to a new altitude (tab switch) in about a
      // second, and trails the page a little while scrolling, which is what makes the sky feel deep
      const w = 7.0;
      this.vel = this.vel || 0;
      const a = (this.climbTarget - this.climb) * w * w - 2 * w * this.vel;
      this.vel += a * dt; this.climb += this.vel * dt;
      const k = 1 - Math.exp(-dt * 6.0);
      this.tilt[0] += (this.tiltTarget[0] - this.tilt[0]) * k;
      this.tilt[1] += (this.tiltTarget[1] - this.tilt[1]) * k;
      if (this.introStart == null) this.introStart = now;
      const ip = Math.min(1, (now - this.introStart) / 2600);
      this.intro = 1 - Math.pow(1 - ip, 3);
      const gl = this.gl;
      const motion = this.reduced ? 0 : 1;
      gl.uniform1f(this.u.uTime, this.reduced ? 3.0 : (now - this.t0) / 1000);
      gl.uniform1f(this.u.uClimb, this.climb);
      gl.uniform2f(this.u.uTilt, this.tilt[0], this.tilt[1]);
      gl.uniform1f(this.u.uIntro, this.reduced ? 1 : this.intro);
      gl.uniform1f(this.u.uMotion, motion);
      this.mood = (this.mood ?? 0.5) + ((this.moodTarget ?? 0.5) - (this.mood ?? 0.5)) * (1 - Math.exp(-dt * 0.8));
      this.tension = (this.tension ?? 0) + ((this.tensionTarget ?? 0) - (this.tension ?? 0)) * (1 - Math.exp(-dt * 0.6));
      gl.uniform1f(this.u.uMood, this.mood); gl.uniform1f(this.u.uTension, this.tension);
      const pk = this.pulseAt ? Math.max(0, 1 - (now - this.pulseAt) / 1800) : 0;
      gl.uniform1f(this.u.uPulse, pk * pk * (this.reduced ? 0 : 1));
      if (this.climb > 1.1 && (this.frames & 1) && !this.pulseAt) { this.frames++; requestAnimationFrame((n) => this.loop(n)); return; }
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      // adaptive resolution: if the phone struggles, render fewer pixels (the photo stays sharp enough)
      this.frames++;
      if (dt > 0.034) this.slow++; else this.slow = Math.max(0, this.slow - 0.25);
      if (this.slow > 24 && this.scale > 0.55) { this.scale = Math.max(0.55, this.scale - 0.15); this.slow = 0; this.resize(); }
      if (!(this.reduced && this.frames > 2)) requestAnimationFrame((n) => this.loop(n));
    },
  };

  window.Scene = Scene;
})();
