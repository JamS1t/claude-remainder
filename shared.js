// Helpers shared by the main process (Node), the renderer (browser global `Shared`) and tests.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Shared = factory();
})(this, function () {
  'use strict';

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const lerpRect = (a, b, t) => ({ x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t), w: lerp(a.w, b.w, t), h: lerp(a.h, b.h, t) });
  const inside = (r, p, pad = 0) =>
    !!p && p.x >= r.x - pad && p.x <= r.x + r.w + pad && p.y >= r.y - pad && p.y <= r.y + r.h + pad;

  // 1234 -> "1.2K", 905700 -> "906K", 1.1e9 -> "1.1B"
  function tokens(n) {
    for (const [d, s] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
      if (n >= d) {
        const v = n / d;
        return `${v >= 100 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, '')}${s}`;
      }
    }
    return String(Math.round(n));
  }

  // Apple-style relative duration: "2 days 4 hr", "1 hr 22 min", "8 min", "now"
  function duration(ms) {
    if (!(ms > 0)) return 'now';
    const mins = Math.floor(ms / 60000);
    const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
    if (d) return `${d} day${d > 1 ? 's' : ''}${h ? ` ${h} hr` : ''}`;
    if (h) return `${h} hr${m ? ` ${m} min` : ''}`;
    return m ? `${m} min` : 'under a minute';
  }

  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

  // Damped harmonic spring, integrated in fixed 1/240 s substeps so it is frame-rate independent.
  class Spring {
    constructor(value, stiffness = 300, damping = 30) {
      this.value = this.target = value;
      this.velocity = 0;
      this.k = stiffness;
      this.c = damping;
    }
    set(target) { this.target = target; }
    snap(value) { this.value = this.target = value; this.velocity = 0; }
    step(dt) {
      const h = 1 / 240;
      for (let n = Math.ceil(dt / h); n > 0; n--) {
        const a = -this.k * (this.value - this.target) - this.c * this.velocity;
        this.velocity += a * h;
        this.value += this.velocity * h;
      }
      if (Math.abs(this.value - this.target) < 1e-4 && Math.abs(this.velocity) < 1e-3) this.snap(this.target);
    }
    get moving() { return this.value !== this.target || this.velocity !== 0; }
  }

  return { clamp, lerp, lerpRect, inside, tokens, duration, esc, Spring };
});
