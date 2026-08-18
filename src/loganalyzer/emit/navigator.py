"""TimeNavigator — the time-range component the map is built on.

A capture routinely spans days, so the map ships a range selector: a strip
showing the WHOLE capture with a window you can slide or stretch, read the same
way as the range strip under a stock chart. Highcharts calls this a navigator,
D3 calls the interaction a brush, Chrome DevTools calls it the overview; this
is the navigator.

This module holds it as a self-contained component — CSS, markup and a vanilla
JS factory — spliced into the map template by ``emit/map.py``. It is kept apart
from the renderer on purpose: it knows nothing about maps, layers, GeoJSON, the
SDK or the log. It is given a span, some events to plot, some bands to shade
and some sessions to bracket, and it reports which window is selected.

JS API
------
::

    const nav = TimeNavigator({
      canvas,                        // <canvas> to own
      t0, t1,                        // capture span, epoch ms
      events:   [{t, w, sev}],       // histogram bars; sev "error"|"warning"|null
      bands:    [{a, b, tone}],      // shaded spans; tone "alert"|"muted"
      sessions: [{i, a, b, label}],  // the bracket ruler; label is pre-formatted
      initial:  {a, b},              // opening window; omit for the whole span
      onChange: ({a, b, session, reason}) => {},
    });

    nav.contains(t, t1)     // the predicate every consumer asks
    nav.window()            // -> {a, b}
    nav.isWindowed()        // -> false when the whole capture is selected
    nav.select(a, b)        // set the window (clamped, min-width enforced)
    nav.reset()             // back to the whole capture
    nav.step(+1 | -1)       // next / previous session
    nav.gotoSession(idx)
    nav.currentSession()    // -> the session the window equals, or null
    nav.describe()          // -> {range, span, session} display strings
    nav.resize()            // re-measure + re-bin; call on window resize
    nav.draw()              // repaint

``onChange`` fires on every window change. ``reason`` is one of "drag",
"resize", "zoom", "session" or "reset" — a consumer that wants to re-frame a
view on deliberate jumps, but not while the user is scrubbing, can switch on it.
"""
from __future__ import annotations

# Bottom offset (px) that page furniture must clear to sit above the navigator.
NAV_HEIGHT_PX = 116

NAV_CSS = r"""
/* ── TimeNavigator ─────────────────────────────────────────────────────── */
#navigator{left:10px;right:10px;bottom:8px;padding:5px 8px 3px;z-index:11}
#navhd{display:flex;align-items:center;gap:9px;font-size:11px;
  color:var(--ink-dim);padding:0 1px 4px;user-select:none;white-space:nowrap}
#navhd b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
#navhd .sp{margin-left:auto}
#navhd button{all:unset;cursor:pointer;color:var(--accent);padding:0 5px;
  border-radius:4px}
#navhd button:hover{background:var(--chip)}
#navhd button[disabled]{color:var(--ink-dim);opacity:.45;cursor:default;
  background:none}
#navcv{display:block;width:100%;height:66px;touch-action:none;cursor:crosshair}
/* No usable span (a single-instant capture): a navigator would be a lie. */
body.notime #navigator{display:none}
body.notime #legend,body.notime #zoomctl{bottom:26px}
body.notime #attrib{bottom:4px}
"""

NAV_HTML = r"""
<div class="hud" id="navigator">
  <div id="navhd">
    <b id="nav-range"></b>
    <span id="nav-span"></span>
    <span id="nav-session"></span>
    <span class="sp"></span>
    <span id="nav-count"></span>
    <button id="nav-prev" title="Previous session">&lsaquo;</button>
    <button id="nav-next" title="Next session">&rsaquo;</button>
    <button id="nav-all" title="Show the whole capture">all</button>
  </div>
  <canvas id="navcv"></canvas>
</div>
"""

NAV_JS = r"""
// ── TimeNavigator ───────────────────────────────────────────────────────────
// Self-contained time-range component; see emit/navigator.py for the API.
// Knows nothing about maps, layers or logs: a span in, a window out.
function TimeNavigator(opts){
  const canvas = opts.canvas;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const T0 = opts.t0, T1 = opts.t1;
  const SPAN = Math.max(1, T1 - T0);
  const events = opts.events || [];
  const bands = opts.bands || [];
  const onChange = opts.onChange || function(){};
  // Clamped to the navigator's own span. A session is padded around its data
  // and can reach past the first or last event; select() clamps to [T0,T1], so
  // an unclamped session would set a window that no longer equals itself and
  // could never light up as the current one.
  const sessions = (opts.sessions || [])
    .map(s => Object.assign({}, s, { a: Math.max(s.a, T0), b: Math.min(s.b, T1) }))
    .filter(s => s.b > s.a);

  // Opening window. A consumer may hand one in (see `initial`); otherwise the
  // whole capture, which is only readable when the capture is short.
  const win = { a: T0, b: T1 };
  if (opts.initial && opts.initial.a != null && opts.initial.b != null) {
    win.a = Math.max(T0, opts.initial.a);
    win.b = Math.min(T1, opts.initial.b);
    if (win.b <= win.a) { win.a = T0; win.b = T1; }
  }
  const MIN_WIN = Math.max(1000, SPAN / 5000);
  let W = 0, H = 0, bins = null, drag = null;

  // ── geometry ──────────────────────────────────────────────────────────────
  function xOf(t){ return (t - T0) / SPAN * W; }
  function tOf(x){ return T0 + Math.max(0, Math.min(1, x / W)) * SPAN; }
  // Vertical layout: [0,3] severity ticks · [4,base] histogram ·
  // [base+3,base+7] session ruler · the rest, time labels.
  function baseY(){ return H - 23; }

  // ── formatting ────────────────────────────────────────────────────────────
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  function p2(n){ return n < 10 ? "0" + n : "" + n; }
  function fmtClock(t){ const d = new Date(t); return p2(d.getHours()) + ":" + p2(d.getMinutes()); }
  function fmtDay(t){ const d = new Date(t); return MONTHS[d.getMonth()] + " " + d.getDate(); }
  function fmtStamp(t){ return fmtDay(t) + " " + fmtClock(t); }
  function fmtDur(ms){
    const s = Math.round(ms / 1000);
    if (s < 90) return s + "s";
    const m = Math.round(s / 60);
    if (m < 90) return m + "m";
    const h = Math.floor(m / 60), rm = m % 60;
    if (h < 36) return h + "h" + (rm ? " " + rm + "m" : "");
    const d = Math.floor(h / 24), rh = h % 24;
    return d + "d" + (rh ? " " + rh + "h" : "");
  }

  // ── binning ───────────────────────────────────────────────────────────────
  function rebin(){
    const n = Math.max(40, Math.min(600, Math.round(W)));
    const tot = new Float64Array(n);
    const err = new Uint8Array(n), wrn = new Uint8Array(n);
    for (const e of events){
      const k = Math.max(0, Math.min(n - 1, Math.floor((e.t - T0) / SPAN * n)));
      tot[k] += (e.w || 1);
      if (e.sev === "error") err[k] = 1;
      else if (e.sev === "warning") wrn[k] = 1;
    }
    let max = 1;
    for (let i = 0; i < n; i++) if (tot[i] > max) max = tot[i];
    bins = { n, tot, err, wrn, max };
  }

  const AXIS_STEPS = [6e4, 3e5, 9e5, 18e5, 36e5, 108e5, 216e5, 432e5, 864e5, 1728e5, 6048e5];
  function axisStep(){
    for (const s of AXIS_STEPS) if (SPAN / s <= 9) return s;
    return AXIS_STEPS[AXIS_STEPS.length - 1];
  }

  // ── paint ─────────────────────────────────────────────────────────────────
  function draw(){
    if (W <= 0 || !bins) return;
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const base = baseY();
    ctx.clearRect(0, 0, W, H);

    // Shaded bands behind everything: dead air is part of the capture's shape.
    for (const g of bands){
      const x0 = xOf(g.a), x1 = Math.max(xOf(g.b), x0 + 1);
      ctx.fillStyle = g.tone === "alert"
        ? "rgba(205,70,60,.20)" : "rgba(140,148,160,.16)";
      ctx.fillRect(x0, 4, x1 - x0, base - 4);
    }
    // Activity. sqrt scale: a 900-record burst must not flatten everything
    // else to a hairline, which is what a linear scale does to these captures.
    const w = W / bins.n;
    ctx.fillStyle = dark ? "rgba(150,178,214,.75)" : "rgba(74,116,166,.60)";
    for (let i = 0; i < bins.n; i++){
      const v = bins.tot[i];
      if (!v) continue;
      const h = Math.max(1.5, Math.sqrt(v / bins.max) * (base - 10));
      ctx.fillRect(i * w, base - h, Math.max(1, w - 0.35), h);
    }
    // Severity ticks along the very top. One error inside a bucket of 500
    // routine records is the whole reason someone opened this, so it gets its
    // own row rather than an invisible sliver of a stacked bar.
    for (let i = 0; i < bins.n; i++){
      if (!bins.wrn[i] && !bins.err[i]) continue;
      ctx.fillStyle = bins.err[i] ? "rgba(214,69,56,.98)" : "rgba(233,166,28,.95)";
      ctx.fillRect(i * w, 0, Math.max(1.5, w), 3);
    }
    // Time axis, stepped from local midnight so day boundaries land on ticks.
    const step = axisStep();
    const d0 = new Date(T0); d0.setHours(0, 0, 0, 0);
    ctx.font = "9.5px system-ui,-apple-system,sans-serif";
    ctx.textBaseline = "bottom";
    for (let t = d0.getTime(); t <= T1; t += step){
      if (t < T0) continue;
      const x = xOf(t);
      ctx.fillStyle = dark ? "rgba(190,198,210,.20)" : "rgba(60,70,84,.18)";
      ctx.fillRect(Math.round(x), 4, 1, base - 4);
      const d = new Date(t);
      const midnight = d.getHours() === 0 && d.getMinutes() === 0;
      ctx.fillStyle = dark ? "rgba(190,198,210,.72)" : "rgba(60,70,84,.66)";
      ctx.textAlign = x < 22 ? "left" : (x > W - 22 ? "right" : "center");
      ctx.fillText((step >= 864e5 || midnight) ? fmtDay(t) : fmtClock(t), x, H - 0.5);
    }
    // Session ruler: the selected bracket is lit and ticked, the rest muted.
    const cur = currentSession();
    for (const s of sessions){
      const x0 = xOf(s.a), x1 = Math.max(xOf(s.b), x0 + 2);
      const active = cur && cur.i === s.i;
      ctx.fillStyle = active ? "rgba(90,162,232,.98)"
                             : (dark ? "rgba(150,178,214,.45)" : "rgba(74,116,166,.42)");
      ctx.fillRect(x0, base + 3, x1 - x0, 4);
      if (active){
        ctx.fillRect(x0, base + 1, 1.5, 8);
        ctx.fillRect(x1 - 1.5, base + 1, 1.5, 8);
      }
    }
    // Selection: everything outside is scrimmed, so the window reads as the
    // lit part of the capture rather than a box drawn on top of it.
    const xa = xOf(win.a), xb = xOf(win.b);
    ctx.fillStyle = dark ? "rgba(20,22,27,.66)" : "rgba(246,247,249,.70)";
    ctx.fillRect(0, 0, Math.max(0, xa), base + 1);
    ctx.fillRect(xb, 0, Math.max(0, W - xb), base + 1);
    ctx.strokeStyle = "rgba(90,162,232,.95)"; ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(xa) + 0.5, 0.5,
                   Math.max(1, Math.round(xb - xa) - 1), base);
    // Handles inset at the extremes so both stay visible — and grabbable —
    // when the window is the whole capture.
    for (const x of [Math.max(xa, 2.5), Math.min(xb, W - 2.5)]){
      ctx.fillStyle = "rgba(90,162,232,.95)";
      ctx.fillRect(x - 2.5, 1, 5, base - 1);
      ctx.fillStyle = "rgba(255,255,255,.92)";
      ctx.fillRect(x - 0.5, base / 2 - 5, 1, 10);
    }
  }

  function resize(){
    const r = canvas.getBoundingClientRect();
    W = r.width; H = r.height;
    if (W <= 0) return;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    rebin();
    draw();
  }

  // ── window ────────────────────────────────────────────────────────────────
  function isWindowed(){ return win.a > T0 || win.b < T1; }
  function contains(t, t1){
    if (!isWindowed() || t === null || t === undefined) return true;
    return t <= win.b && (t1 === null || t1 === undefined ? t : t1) >= win.a;
  }
  function select(a, b, reason){
    if (b - a < MIN_WIN){
      const mid = (a + b) / 2;
      a = mid - MIN_WIN / 2; b = mid + MIN_WIN / 2;
    }
    if (a < T0){ b += T0 - a; a = T0; }
    if (b > T1){ a -= b - T1; b = T1; }
    win.a = Math.max(T0, a); win.b = Math.min(T1, b);
    draw();
    onChange({ a: win.a, b: win.b, session: currentSession(),
               reason: reason || "select" });
  }

  // ── sessions ──────────────────────────────────────────────────────────────
  function currentSession(){
    // Tolerance follows MIN_WIN: a session shorter than the minimum window
    // gets widened by select(), and it is still that session.
    const tol = Math.max(1500, MIN_WIN);
    for (const s of sessions){
      if (Math.abs(s.a - win.a) <= tol && Math.abs(s.b - win.b) <= tol) return s;
    }
    return null;
  }
  function gotoSession(idx){
    const s = sessions[Math.max(0, Math.min(sessions.length - 1, idx))];
    if (s) select(s.a, s.b, "session");
  }
  function step(dir){
    if (!sessions.length) return;
    const cur = currentSession();
    let idx;
    if (cur){
      idx = sessions.indexOf(cur) + dir;
    } else if (!isWindowed()){
      // From the whole capture, step in from the end you are heading towards.
      idx = dir > 0 ? 0 : sessions.length - 1;
    } else {
      const mid = (win.a + win.b) / 2;
      if (dir > 0){
        idx = sessions.findIndex(s => s.a > mid);
        if (idx < 0) idx = sessions.length - 1;
      } else {
        idx = 0;
        for (let k = 0; k < sessions.length; k++) if (sessions[k].b < mid) idx = k;
      }
    }
    gotoSession(idx);
  }

  function describe(){
    const cur = currentSession();
    return {
      range: fmtStamp(win.a) + "  →  " + fmtStamp(win.b),
      span: fmtDur(win.b - win.a) +
            (isWindowed() ? " of " + fmtDur(SPAN) : " · whole capture"),
      session: cur ? (cur.label || ("session " + cur.i))
                   : (sessions.length
                      ? sessions.length + " session" + (sessions.length > 1 ? "s" : "")
                      : ""),
    };
  }

  // ── interaction ───────────────────────────────────────────────────────────
  function pos(e){
    const r = canvas.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  }
  canvas.addEventListener("pointerdown", e => {
    const xy = pos(e), x = xy[0], y = xy[1];
    // The session ruler is its own control surface: a click there selects that
    // session outright, wherever the current window happens to be.
    if (y >= baseY() && sessions.length){
      const hit = sessions.find(s => x >= xOf(s.a) - 2 && x <= xOf(s.b) + 2);
      if (hit){ gotoSession(sessions.indexOf(hit)); return; }
    }
    const xa = xOf(win.a), xb = xOf(win.b);
    let mode;
    if (Math.abs(x - xa) <= 6) mode = "l";
    else if (Math.abs(x - xb) <= 6) mode = "r";
    else if (x > xa && x < xb) mode = "move";
    else { win.a = win.b = tOf(x); mode = "r"; }   // drag out a fresh selection
    drag = { mode, x0: x, a0: win.a, b0: win.b };
    canvas.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  canvas.addEventListener("pointermove", e => {
    const xy = pos(e), x = xy[0], y = xy[1];
    if (!drag){
      const xa = xOf(win.a), xb = xOf(win.b);
      canvas.style.cursor =
        (y >= baseY() && sessions.length) ? "pointer" :
        (Math.abs(x - xa) <= 6 || Math.abs(x - xb) <= 6) ? "ew-resize" :
        (x > xa && x < xb) ? "grab" : "crosshair";
      return;
    }
    const dt = (x - drag.x0) / W * SPAN;
    if (drag.mode === "move"){
      select(drag.a0 + dt, drag.b0 + dt, "drag");
    } else {
      // Dragging one edge past the other flips the window rather than pinning it.
      const fixed = drag.mode === "l" ? drag.b0 : drag.a0;
      const moving = (drag.mode === "l" ? drag.a0 : drag.b0) + dt;
      select(Math.min(fixed, moving), Math.max(fixed, moving), "resize");
    }
  });
  function endDrag(e){
    if (!drag) return;
    drag = null;
    try { canvas.releasePointerCapture(e.pointerId); } catch (_){}
  }
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);
  canvas.addEventListener("wheel", e => {
    e.preventDefault();
    const t = tOf(pos(e)[0]), f = e.deltaY < 0 ? 0.75 : 1 / 0.75;
    select(t - (t - win.a) * f, t + (win.b - t) * f, "zoom");
  }, { passive: false });
  canvas.addEventListener("dblclick", e => {
    e.preventDefault();
    select(T0, T1, "reset");
  });

  return {
    window(){ return { a: win.a, b: win.b }; },
    isWindowed: isWindowed,
    contains: contains,
    select(a, b){ select(a, b, "select"); },
    reset(){ select(T0, T1, "reset"); },
    sessions(){ return sessions.slice(); },
    currentSession: currentSession,
    gotoSession: gotoSession,
    step: step,
    describe: describe,
    resize: resize,
    draw: draw,
  };
}
"""
