"""Stage 5 (map side) — self-contained HTML map renderer.

``render_map(layers, title)`` returns ONE self-contained HTML string: a
hand-rolled slippy map in vanilla JS (tiles + canvas overlay + layer toggles +
popups + grid clustering). Inline CSS/JS only — the sole network dependency is
the OSM raster tile server (https://tile.openstreetmap.org/{z}/{x}/{y}.png).
No CDN scripts, no <link> tags, no web fonts.

Rendering vocabulary (design v2):
- markers reuse the log emoji as glyphs (☯️ 💀 ⚠️ ‼️ 🚫 📢 ⚡ …)
- speed-colored track LineStrings; dashed gap spans
- tethered events: marker offset above its anchor fix with a "+N min" badge
  and a dashed tether (the honesty rule for Δt > 120 s)
- accuracy circles on the fixes layer, OFF by default
- grid clustering above 500 visible markers per layer
- layer control = checkboxes with glyph + feature count; small legend;
  dark-mode friendly neutral UI

Maps are full-precision LOCAL-ONLY triage artifacts (never pasted into
issues); the redacted digest is the shareable artifact.
"""
from __future__ import annotations

import html as _html
import json

from ..maprules import load_rules
from .geojson import COMPACT_DEFAULTS
from .icons import ICONS as _LUCIDE, LUCIDE_VIEWBOX
from .navigator import NAV_CSS, NAV_HTML, NAV_JS

# Presentation comes from vocabulary/map-rules.yaml, read HERE rather than
# through emit/geojson: the layer builder decides what a feature IS, this
# module decides what it looks like, and routing the renderer's constants
# through the data module blurred exactly that line.
_RULES = load_rules()

CLUSTER_THRESHOLD = 500


def render_map(layers: dict[str, dict], title: str, subtitle: str = "",
               sessions: list[dict] | None = None) -> str:
    """Render the layer dict (from build_layers) into one self-contained HTML
    document. ``layers`` maps layer name -> GeoJSON FeatureCollection.

    ``subtitle`` carries SDK version / device / app id when the capture had a
    launch banner — many customer logs are mid-session exports with no banner,
    so it is optional and simply omitted when unknown.

    ``sessions`` (from build_sessions) drives the time brush: captures routinely
    span days, so the map ships a range selector over the whole span with the
    tracking sessions marked on it."""
    display = _RULES.layer_display()
    order = [n for n in _RULES.layer_order() if n in layers]
    meta = {name: {**display.get(name, {"label": name, "kind": "marker", "glyph": "🔵"}),
                   "count": len(layers[name].get("features", []))}
            for name in order}

    def _js(obj) -> str:
        # `</` escaped so JSON content can never terminate the <script> block.
        return json.dumps(obj, ensure_ascii=False,
                          separators=(",", ":")).replace("</", "<\\/")

    return (_TEMPLATE
            .replace("__TITLE_HTML__", _html.escape(title))
            .replace("__TITLE_JSON__", _js(title))
            .replace("__META__", _js(meta))
            .replace("__ORDER__", _js(order))
            .replace("__SUBTITLE_HTML__", _html.escape(subtitle))
            .replace("__OFFSET_CLOCK__", _js(_RULES.offset_clocks()))
            .replace("__ICONS__", _js(_LUCIDE))
            .replace("__ICON_GRID__", str(LUCIDE_VIEWBOX))
            .replace("__LAYER_VECTOR__", _js(_RULES.layer_icons()))
            .replace("__ICON_TINT__", _js(dict(_RULES.tints)))
            .replace("__BULK_HIDE__", _js(_RULES.bulk_hide_above()))
            .replace("__PROP_DEFAULTS__", _js(COMPACT_DEFAULTS))
            .replace("__CLUSTER_THRESHOLD__", str(CLUSTER_THRESHOLD))
            .replace("__SESSIONS__", _js(sessions or []))
            .replace("__NAV_CSS__", NAV_CSS)
            .replace("__NAV_HTML__", NAV_HTML)
            .replace("__NAV_JS__", NAV_JS)
            .replace("__DATA__", _js(layers)))


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE_HTML__ — loganalyzer map</title>
<style>
:root{
  --panel: rgba(252,252,253,.94); --ink:#1c1f24; --ink-dim:#5a6470;
  --line:#c9ced6; --accent:#2673c9; --chip:#eef1f5;
}
@media (prefers-color-scheme: dark){
  :root{ --panel: rgba(24,26,31,.94); --ink:#e7e9ec; --ink-dim:#9aa3ad;
         --line:#3a4048; --accent:#5aa2e8; --chip:#2a2f36; }
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#2a2d33;
  font:13px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
#map{position:fixed;inset:0;overflow:hidden;background:#bfcbd6;cursor:grab}
#map.dragging{cursor:grabbing}
#tiles{position:absolute;inset:0}
#tiles img{position:absolute;width:256px;height:256px;user-select:none;
  -webkit-user-drag:none;image-rendering:auto}
#overlay{position:absolute;inset:0}
.hud{position:fixed;z-index:10;background:var(--panel);color:var(--ink);
  border:1px solid var(--line);border-radius:8px;
  box-shadow:0 2px 10px rgba(0,0,0,.25)}
#titlebar{top:10px;left:10px;padding:7px 12px;max-width:46vw}
#titlebar b{font-size:14px}
#titlebar .sub{color:var(--ink-dim);font-size:11px;margin-top:1px}
#titlebar #subtitle:empty{display:none}
#titlebar #subtitle{color:var(--ink);font-size:11.5px}
#panel{top:10px;right:10px;padding:8px 10px;min-width:190px;max-height:70vh;overflow:auto}
#panel h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--ink-dim)}
#panel label{display:flex;align-items:center;gap:7px;padding:2.5px 2px;cursor:pointer;
  white-space:nowrap}
#panel label.sub{margin-left:22px;color:var(--ink-dim);font-size:12px}
#panel .cnt{margin-left:auto;color:var(--ink-dim);background:var(--chip);
  border-radius:9px;padding:0 7px;font-size:11px}
#legend{bottom:116px;left:10px;padding:8px 11px;font-size:11px;color:var(--ink-dim)}
#legend .ramp{height:8px;width:170px;border-radius:4px;margin:4px 0 2px;
  background:linear-gradient(90deg,hsl(210,85%,48%),hsl(150,85%,44%),
    hsl(90,85%,44%),hsl(45,90%,48%),hsl(0,85%,50%))}
#legend .row{display:flex;justify-content:space-between}
#legend .key{display:flex;align-items:center;gap:6px;margin-top:4px;color:var(--ink)}
#legend .dash{display:inline-block;width:26px;border-top:2px dashed var(--ink-dim)}
#legend .swatch{display:inline-block;width:26px;height:4px;border-radius:2px}
#legend .reg{display:inline-block;width:13px;height:13px;border-radius:50%;
  border:1.5px dashed rgba(183,28,28,.78);background:rgba(214,52,44,.13)}
#legend .dot{display:inline-block;width:11px;height:11px;border-radius:50%;border:2px solid}
#legend .dot.green{border-color:rgba(46,160,80,.95);background:rgba(226,246,231,.97)}
#legend .dot.red{border-color:rgba(198,40,40,.95);background:rgba(253,231,229,.97)}
#legend .dot.amber{border-color:rgba(214,150,30,.95);background:rgba(255,247,224,.97)}
#legend .ring{display:inline-block;width:11px;height:11px;border-radius:50%;
  border:2px solid rgba(183,28,28,.9);background:rgba(244,67,54,.35)}
#zoomctl{bottom:116px;right:10px;display:flex;flex-direction:column;overflow:hidden;padding:0}
#zoomctl button{all:unset;width:34px;height:32px;text-align:center;font-size:17px;
  cursor:pointer;color:var(--ink)}
#zoomctl button:hover{background:var(--chip)}
#zoomctl button + button{border-top:1px solid var(--line)}
#attrib{position:fixed;right:4px;bottom:110px;z-index:9;font-size:10.5px;
  color:#333;background:rgba(255,255,255,.72);padding:1px 6px;border-radius:4px}
__NAV_CSS__
#popup{display:none;position:fixed;z-index:20;max-width:460px;min-width:260px;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:0 4px 18px rgba(0,0,0,.35);color:var(--ink)}
#popup .hd{display:flex;align-items:center;gap:8px;padding:7px 10px;
  border-bottom:1px solid var(--line)}
#popup .hd .sev{font-size:10px;text-transform:uppercase;letter-spacing:.05em;
  padding:1px 7px;border-radius:8px;background:var(--chip);color:var(--ink-dim)}
#popup .hd .sev.warning{background:#8a6d1a;color:#fff}
#popup .hd .sev.error{background:#a5423a;color:#fff}
#popup .hd .sev.headless{background:#3d3a52;color:#fff}
#popup .hd .sev.empty{display:none}
#popup .hd .x{margin-left:auto;cursor:pointer;color:var(--ink-dim);font-size:15px;padding:0 3px}
#popup pre{margin:0;padding:8px 10px;white-space:pre-wrap;word-break:break-word;
  font:11px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;max-height:180px;overflow:auto}
#popup .meta{padding:6px 10px;border-top:1px solid var(--line);
  font-size:11px;color:var(--ink-dim)}
#popup .meta code{user-select:all;background:var(--chip);color:var(--ink);
  padding:1px 5px;border-radius:4px;font-size:10.5px}
#popup .copy{float:right;cursor:pointer;color:var(--accent);font-size:11px}
#empty{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
  color:#fff;font-size:15px;z-index:5}
</style>
</head>
<body>
<div id="map">
  <div id="tiles"></div>
  <canvas id="overlay"></canvas>
</div>
<div class="hud" id="titlebar"><b>__TITLE_HTML__</b>
  <div class="sub" id="subtitle">__SUBTITLE_HTML__</div>
  <div class="sub" id="stats"></div></div>
<div class="hud" id="panel"><h3>Layers</h3><div id="layerlist"></div></div>
<div class="hud" id="legend">
  <div class="key"><span class="swatch" style="background:rgba(58,129,245,.55);height:7px"></span> track</div>
  <div class="key"><span class="swatch" style="background:rgba(76,175,80,.85);height:6px"></span> motionchange: stationary → moving</div>
  <div class="key"><span class="ring"></span> stationary (isMoving: false)</div>
  <div class="key"><span class="dot green"></span> green = tracking resumes / geofence ENTER</div>
  <div class="key"><span class="dot red"></span> red = tracking parks / EXIT / failure</div>
  <div class="key"><span class="dot amber"></span> amber = geofence DWELL · app foreground</div>
  <div class="key"><span class="reg"></span> stationary region (radius read from the log)</div>
  <div class="key"><span style="color:rgba(33,150,243,.95)">➤</span> fix — points direction of travel (dot = no course)</div>
  <div id="speedlegend" style="display:none">
    <div style="margin-top:4px">Track speed (m/s)</div>
    <div class="ramp"></div>
    <div class="row"><span>0</span><span>15</span><span>30+</span></div>
  </div>
  <div class="key"><span class="dash"></span> gap span / tether (+N&nbsp;min = event Δt to last fix)</div>
  <div class="key"><span>✳︎</span> petals = events sharing one spot, clockwise in time (12:00 = earliest)</div>
  <div class="key"><span>◯</span> cluster (&gt;__CLUSTER_THRESHOLD__ markers/layer) — click to zoom</div>
</div>
<div class="hud" id="zoomctl">
  <button id="zin" title="Zoom in">+</button>
  <button id="zout" title="Zoom out">−</button>
  <button id="zfit" title="Fit to data">⌂</button>
</div>
<div id="attrib">© OpenStreetMap contributors</div>
__NAV_HTML__
<div id="popup">
  <div class="hd"><span id="p-glyph"></span><b id="p-title"></b>
    <span class="sev" id="p-sev"></span><span class="x" id="p-close">✕</span></div>
  <pre id="p-body"></pre>
  <div class="meta" id="p-meta"></div>
</div>
<script>
"use strict";
const TITLE = __TITLE_JSON__;
const META = __META__;
const ORDER = __ORDER__;
const CLUSTER_THRESHOLD = __CLUSTER_THRESHOLD__;
const SESSIONS = __SESSIONS__;
const LAYERS = __DATA__;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

// ── projection ──────────────────────────────────────────────────────────────
function mercX(lon){ return (lon + 180) / 360; }
function mercY(lat){
  const r = lat * Math.PI / 180;
  return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2;
}
const view = { z: 3, cx: 0.5, cy: 0.5 };
let W = 0, H = 0;
function scale(){ return 256 * Math.pow(2, view.z); }
function toScreen(mx, my){
  const s = scale();
  return [(mx - view.cx) * s + W / 2, (my - view.cy) * s + H / 2];
}
function fromScreen(px, py){
  const s = scale();
  return [(px - W / 2) / s + view.cx, (py - H / 2) / s + view.cy];
}
function metersPerPixel(){
  const lat = 2 * Math.atan(Math.exp((1 - 2 * view.cy) * Math.PI)) - Math.PI / 2;
  return 156543.03392 * Math.cos(lat) / Math.pow(2, view.z);
}

// ── time ────────────────────────────────────────────────────────────────────
// Log timestamps are LOCAL device time with no zone ("2026-08-15 16:10:33.288"),
// so they are parsed as local — which is what the reader means by "16:10".
function parseTs(s){
  if (!s) return null;
  const t = Date.parse(String(s).replace(" ", "T"));
  return isNaN(t) ? null : t;
}

// ── compaction ──────────────────────────────────────────────────────────────
// build_layers drops every property that equals its default (key names repeat
// per feature, and that is real bytes). PROP_DEFAULTS is the SAME table
// _compact() dropped from — emitted here rather than restated — so the two
// sides cannot drift. Re-inflated once at load, after which the rest of the
// renderer just reads real values.
const PROP_DEFAULTS = __PROP_DEFAULTS__;
function inflate(props, layer){
  for (const key in PROP_DEFAULTS){
    if (props[key] !== undefined) continue;
    const spec = PROP_DEFAULTS[key];
    props[key] = spec.from === "ts" ? props.ts
               : spec.from === "layer" ? layer
               : spec.value;
  }
  return props;
}

// ── feature preprocessing ───────────────────────────────────────────────────
const FEATS = {};       // layer -> [{f, t, t1, pt:[mx,my]} | {..., pts:[..]}]
const visible = {};     // layer -> bool
// Layers that are routine in bulk; above BULK_MAX they start hidden.
const BULK_HIDE = __BULK_HIDE__;   // layer -> feature count above which it starts hidden
let showAcc = false;    // accuracy circles (off by default per design)
let showSpeed = false;  // speed-colored track (off: demo-app blue)
let total = 0;
const bbox = [Infinity, Infinity, -Infinity, -Infinity];  // mx0,my0,mx1,my1
for (const name of ORDER){
  visible[name] = !(BULK_HIDE[name] !== undefined &&
                    (LAYERS[name].features || []).length > BULK_HIDE[name]);
  FEATS[name] = (LAYERS[name].features || []).map(f => {
    const g = f.geometry;
    // Times are parsed ONCE here, never per redraw: the brush filters on every
    // pointermove and re-parsing thousands of ISO strings would stutter.
    inflate(f.properties, name);
    const t = parseTs(f.properties.ts);
    const t1 = parseTs(f.properties.end_ts);
    const it = { f, t, t1: (t1 !== null ? t1 : t) };
    if (g.type === "Point"){
      it.pt = [mercX(g.coordinates[0]), mercY(g.coordinates[1])];
      grow(it.pt);
    } else {
      it.pts = g.coordinates.map(c => [mercX(c[0]), mercY(c[1])]);
      it.pts.forEach(grow);
    }
    return it;
  });
  total += FEATS[name].length;
}
function grow(p){
  bbox[0] = Math.min(bbox[0], p[0]); bbox[1] = Math.min(bbox[1], p[1]);
  bbox[2] = Math.max(bbox[2], p[0]); bbox[3] = Math.max(bbox[3], p[1]);
}

// ── time window ─────────────────────────────────────────────────────────────
// A capture routinely spans days. [T0,T1] is the whole of it; the selected
// slice belongs to the TimeNavigator (see emit/navigator.py) and every layer
// asks the same question — inWindow() — without knowing it exists.
let T0 = Infinity, T1 = -Infinity;
for (const name of ORDER){
  for (const it of FEATS[name]){
    if (it.t === null) continue;
    if (it.t < T0) T0 = it.t;
    if (it.t1 > T1) T1 = it.t1;
  }
}
// Under a minute of span there is nothing to select: a navigator would be theatre.
const HAS_TIME = isFinite(T0) && (T1 - T0) >= 60000;
if (!HAS_TIME){ T0 = 0; T1 = 1; }
let nav = null;                       // assigned once the navigator is built
function windowed(){ return HAS_TIME && nav !== null && nav.isWindowed(); }
function inWindow(it){
  // An untimed feature is never hidden — the window says "when", and we do not
  // know when this happened, so we cannot honestly exclude it.
  if (!windowed() || it.t === null) return true;
  return nav.contains(it.t, it.t1);
}

// Draw order: lines and dense layers first, severities on top.
const DRAW_ORDER = ["track","gaps","fixes","mock","rejections","geofence",
                    "motionchange","lifecycle","http","warnings","errors","launch"]
                   .filter(n => n in FEATS);

// ── DOM / canvas setup ──────────────────────────────────────────────────────
const mapEl = document.getElementById("map");
const tilesDiv = document.getElementById("tiles");
const canvas = document.getElementById("overlay");
const ctx = canvas.getContext("2d");
const popup = document.getElementById("popup");
const dpr = window.devicePixelRatio || 1;
let needFit = true;   // first fit deferred until the viewport has a real size
function resize(){
  W = mapEl.clientWidth; H = mapEl.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.width = W + "px"; canvas.style.height = H + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  if (nav) nav.resize();
  if (needFit && W > 0 && H > 0){ needFit = false; fitBounds(); }
  else draw();
}
window.addEventListener("resize", resize);

// ── tiles ───────────────────────────────────────────────────────────────────
const tileCache = new Map();
function renderTiles(){
  const s = scale(), n = Math.pow(2, view.z);
  const left = view.cx * s - W / 2, top = view.cy * s - H / 2;
  const tx0 = Math.floor(left / 256), ty0 = Math.floor(top / 256);
  const tx1 = Math.floor((left + W) / 256), ty1 = Math.floor((top + H) / 256);
  const want = new Set();
  for (let tx = tx0; tx <= tx1; tx++){
    for (let ty = ty0; ty <= ty1; ty++){
      if (ty < 0 || ty >= n) continue;
      const wx = ((tx % n) + n) % n;
      const key = view.z + "/" + wx + "/" + ty + "@" + tx;
      want.add(key);
      let img = tileCache.get(key);
      if (!img){
        img = document.createElement("img");
        img.alt = "";
        img.src = TILE_URL.replace("{z}", view.z).replace("{x}", wx).replace("{y}", ty);
        tileCache.set(key, img);
        tilesDiv.appendChild(img);
      }
      img.style.left = (tx * 256 - left) + "px";
      img.style.top = (ty * 256 - top) + "px";
    }
  }
  for (const [key, img] of tileCache){
    if (!want.has(key)){ img.remove(); tileCache.delete(key); }
  }
}

// ── drawing ─────────────────────────────────────────────────────────────────
let hits = [];   // {x, y, layer, i} or {x, y, cluster:true, cx, cy, count}
const offsetSlots = new Map();   // anchor cell + spoke -> how many already drawn
let pendingMarkers = [];         // deferred so co-located markers can be splayed
const OFFSET_CLOCK_BY_LAYER = __OFFSET_CLOCK__;
function speedColor(v){
  if (v === null || v === undefined) return "#8a8f98";
  const t = Math.min(Math.max(v, 0), 30) / 30;
  return "hsl(" + Math.round(210 * (1 - t)) + ",85%,48%)";
}
function speedBucket(v){
  if (v === null || v === undefined) return -1;
  return Math.min(7, Math.floor(Math.min(Math.max(v, 0), 29.99) / 30 * 8));
}
function bucketColor(k){
  if (k < 0) return "#8a8f98";
  return "hsl(" + Math.round(210 * (1 - (k + 0.5) / 8)) + ",85%,48%)";
}
// Which vertex ranges of a polyline survive the time window. A track segment
// can span hours, so the window has to CLIP it rather than show or hide it
// whole: drawing a whole leg because one minute of it is selected would assert
// a route the device did not travel in that minute. `vt` (per-vertex seconds
// from the segment's own start, emitted by _track_features) makes the cut
// exact. Lines without it — gap spans, motionchange connectors — are short
// two-point hops, so an all-or-nothing overlap test is the honest answer.
function windowRuns(it, n){
  if (!windowed() || it.t === null) return [[0, n - 1]];
  const vt = it.f.properties.vt;
  if (!vt || vt.length !== n) return inWindow(it) ? [[0, n - 1]] : [];
  const runs = [];
  let start = -1;
  for (let i = 1; i < n; i++){
    const ta = it.t + vt[i - 1] * 1000, tb = it.t + vt[i] * 1000;
    if (nav.contains(ta, tb)){         // does this leg overlap the window?
      if (start < 0) start = i - 1;
    } else if (start >= 0){
      runs.push([start, i - 1]); start = -1;
    }
  }
  if (start >= 0) runs.push([start, n - 1]);
  return runs;
}
function drawLine(it){
  const p = it.f.properties;
  const scr = it.pts.map(q => toScreen(q[0], q[1]));
  const runs = windowRuns(it, scr.length);
  if (!runs.length) return;
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  // One path across every surviving run: the window can cut a segment into
  // several pieces, and each piece keeps its own round caps.
  function strokeRuns(){
    ctx.beginPath();
    for (const r of runs){
      ctx.moveTo(scr[r[0]][0], scr[r[0]][1]);
      for (let i = r[0] + 1; i <= r[1]; i++) ctx.lineTo(scr[i][0], scr[i][1]);
    }
    ctx.stroke();
  }
  if (p.role === "gap-span"){
    ctx.save();
    ctx.setLineDash([7, 6]);
    ctx.strokeStyle = "rgba(200,90,80,.85)";
    ctx.lineWidth = 2;
    strokeRuns();
    ctx.restore();
    return;
  }
  if (p.role === "suppressed-exit-path"){
    // Same routeBlue as the track: this is the leg the route WOULD have taken
    // had the suppressed transition been accepted.
    ctx.save();
    ctx.strokeStyle = "rgba(58,129,245,.5)"; ctx.lineWidth = 10;
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    strokeRuns();
    ctx.restore();
    return;
  }
  if (p.role === "motionchange-path"){
    ctx.save();
    ctx.strokeStyle = "rgba(76,175,80,.85)";   // demo-app green (#4CAF50)
    ctx.lineWidth = 7;                          // thicker than the track — it marks a state change
    strokeRuns();
    ctx.restore();
    return;
  }
  if (!showSpeed){
    // DemoApp2 route style: routeBlue #3A81F5 @ 0.50 opacity, lineWidth 10
    ctx.globalAlpha = 1; ctx.lineWidth = 10;
    ctx.strokeStyle = "rgba(58,129,245,.5)";
    strokeRuns();
    ctx.globalAlpha = 1;
    return;
  }
  const speeds = p.speeds || [];
  const buckets = {};
  for (const r of runs){
  for (let i = r[0] + 1; i <= r[1]; i++){
    const a = scr[i - 1], b = scr[i];
    if (Math.max(a[0], b[0]) < -50 || Math.min(a[0], b[0]) > W + 50 ||
        Math.max(a[1], b[1]) < -50 || Math.min(a[1], b[1]) > H + 50) continue;
    const k = speedBucket(speeds[i]);
    (buckets[k] = buckets[k] || []).push([a, b]);
  }
  }
  ctx.globalAlpha = 0.88; ctx.lineWidth = 3;
  for (const k in buckets){
    ctx.strokeStyle = bucketColor(+k);
    ctx.beginPath();
    for (const [a, b] of buckets[k]){ ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); }
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
function sevFill(sev){
  if (sev === "error") return "rgba(214,69,56,.95)";
  if (sev === "warning") return "rgba(233,166,28,.95)";
  return "rgba(250,250,251,.92)";
}
// ── vector icons (vendored Lucide, ISC) ─────────────────────────────────────
// Emoji render differently on every platform, cannot be coloured semantically,
// and several of these concepts have no emoji at all ("stationary exit
// suppressed"). ICON_OPS holds Lucide geometry on its native 24-grid; we replay
// it with a stroke, scaled to the marker. One library, drawn consistently.
const ICON_OPS = __ICONS__, ICON_GRID = __ICON_GRID__;
const _pathCache = new Map();
function iconPath(d){
  let p = _pathCache.get(d);
  if (!p){ p = new Path2D(d); _pathCache.set(d, p); }
  return p;
}
// Hand-drawn icons for concepts no icon library covers. Same 24-grid and
// stroke weight as the vendored Lucide set, so they read as one family.
// A traffic light says "go" / "stop" better than any abstract arrow: the
// stop/start cycle is literally the SDK deciding whether to move.
const CUSTOM_ICONS = {
  // red on top, amber middle, green bottom — as on the street
  "traffic-go":   (col) => trafficLight(col, 2),
  "traffic-stop": (col) => trafficLight(col, 0),
};
function trafficLight(col, litIndex){
  ctx.strokeStyle = col; ctx.fillStyle = col;
  ctx.lineWidth = 1.9; ctx.lineCap = "round"; ctx.lineJoin = "round";
  ctx.beginPath();                                   // housing
  if (ctx.roundRect) ctx.roundRect(7, 1.6, 10, 20.8, 3.4);
  else ctx.rect(7, 1.6, 10, 20.8);
  ctx.stroke();
  const ys = [6.4, 12, 17.6];
  ys.forEach((y, i) => {
    ctx.beginPath();
    ctx.arc(12, y, i === litIndex ? 2.5 : 1.5, 0, 6.2832);
    if (i === litIndex) ctx.fill(); else ctx.stroke();
  });
}
function drawIcon(name, size, col){
  const custom = CUSTOM_ICONS[name];
  if (custom){
    const s = size / ICON_GRID;
    ctx.save();
    ctx.scale(s, s); ctx.translate(-ICON_GRID / 2, -ICON_GRID / 2);
    ctx.lineWidth = 2 * (ICON_GRID / size) * 0.62;
    custom(col);
    ctx.restore();
    return true;
  }
  const icon = ICON_OPS[name];
  if (!icon) return false;
  const s = size / ICON_GRID;
  ctx.save();
  ctx.scale(s, s);
  ctx.translate(-ICON_GRID / 2, -ICON_GRID / 2);
  ctx.strokeStyle = col;
  // Lucide draws 2px on a 24 grid; keep the stroke visible when scaled down.
  ctx.lineWidth = 2 * (ICON_GRID / size) * 0.62;
  ctx.lineCap = "round"; ctx.lineJoin = "round";
  for (const op of icon.ops){
    if (op[0] === "p"){ ctx.stroke(iconPath(op[1])); continue; }
    ctx.beginPath();
    if (op[0] === "c"){ ctx.arc(op[1], op[2], op[3], 0, 6.2832); }
    else if (op[0] === "l"){ ctx.moveTo(op[1], op[2]); ctx.lineTo(op[3], op[4]); }
    else if (op[0] === "e"){ ctx.ellipse(op[1], op[2], op[3], op[4], 0, 0, 6.2832); }
    else if (op[0] === "r"){
      if (ctx.roundRect) ctx.roundRect(op[1], op[2], op[3], op[4], op[5] || 0);
      else ctx.rect(op[1], op[2], op[3], op[4]);
    } else if (op[0] === "y"){
      const pts = op[1].split(/\s+/).map(q => q.split(",").map(Number));
      pts.forEach((q, i) => i ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]));
      if (op[2]) ctx.closePath();
    }
    ctx.stroke();
  }
  ctx.restore();
  return true;
}
// Semantic colour per icon: the state an event moves the SDK INTO. Green =
// tracking resumes (left the stationary fence); red = tracking parks (stop
// timeout fired, stationary fence created). Colour and glyph agree, so the
// stop/start cycle is readable without reading a single label.
// Named tints. A feature may carry `tint` directly (a geofence ENTER/EXIT/DWELL
// decides its own colour); otherwise the icon implies one. Semantics: green =
// tracking resumes / entered, red = tracking parks / exited / failed,
// amber = dwell or app-foreground, indigo = app-background.
const TINTS = {
  green:  { ink: "rgba(21,115,52,.95)",  ring: "rgba(46,160,80,.95)",  fill: "rgba(226,246,231,.97)" },
  red:    { ink: "rgba(154,25,20,.95)",  ring: "rgba(198,40,40,.95)",  fill: "rgba(253,231,229,.97)" },
  amber:  { ink: "rgba(150,96,4,.95)",   ring: "rgba(214,150,30,.95)", fill: "rgba(255,247,224,.97)" },
  indigo: { ink: "rgba(58,64,110,.95)",  ring: "rgba(96,104,160,.95)", fill: "rgba(235,238,250,.97)" },
};
const LAYER_VECTOR = __LAYER_VECTOR__;
const ICON_TINT = __ICON_TINT__;
function iconInk(sev){
  if (sev === "error") return "rgba(255,255,255,.97)";
  if (sev === "warning") return "rgba(58,40,4,.95)";
  return "rgba(26,30,36,.92)";
}
function drawStationaryRegion(x, y, p){
  const r = p.stationary_radius_m / metersPerPixel();
  if (!(r > 1.5) || r > 6000) return;      // off-scale: skip rather than mislead
  ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832);
  ctx.fillStyle = "rgba(214,52,44,.13)"; ctx.fill();
  ctx.save();
  ctx.setLineDash([5, 4]);
  ctx.lineWidth = 1.6; ctx.strokeStyle = "rgba(183,28,28,.78)"; ctx.stroke();
  ctx.restore();
}
function drawVectorMarker(x, y, name, p){
  // A tint never overrides a genuine error/warning severity — a failure must
  // keep looking like a failure.
  // An explicit feature tint wins; otherwise the icon implies one. Neither
  // overrides a genuine error/warning — a failure must keep looking like one.
  const tintName = p.tint || ICON_TINT[name];
  // severity is omitted when "normal" (compaction), so absent === normal
  const tint = (!p.severity || p.severity === "normal") ? TINTS[tintName] : null;
  ctx.beginPath(); ctx.arc(x, y, 12, 0, 6.2832);
  ctx.fillStyle = tint ? tint.fill : sevFill(p.severity); ctx.fill();
  ctx.lineWidth = tint ? 1.6 : 1;
  ctx.strokeStyle = tint ? tint.ring : "rgba(20,22,26,.45)"; ctx.stroke();
  ctx.save(); ctx.translate(x, y);
  drawIcon(name, 15, tint ? tint.ink : iconInk(p.severity));
  ctx.restore();
  if (p.count > 1) drawCountBadge(x + 10, y - 10, p.count);
}

function drawGlyphMarker(x, y, p){
  if (p.icon_name && (ICON_OPS[p.icon_name] || CUSTOM_ICONS[p.icon_name])){
    drawVectorMarker(x, y, p.icon_name, p);
    return;
  }
  if (p.marker === "stationary"){
    // demo-app stationary circle: red fill + dark-red stroke, dot at center
    ctx.beginPath(); ctx.arc(x, y, 10, 0, 6.2832);
    ctx.fillStyle = "rgba(244,67,54,.35)"; ctx.fill();
    ctx.lineWidth = 2; ctx.strokeStyle = "rgba(183,28,28,.9)"; ctx.stroke();
    ctx.beginPath(); ctx.arc(x, y, 3, 0, 6.2832);
    ctx.fillStyle = "rgba(183,28,28,.9)"; ctx.fill();
    return;
  }
  ctx.beginPath(); ctx.arc(x, y, 11, 0, 6.2832);
  ctx.fillStyle = sevFill(p.severity); ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = "rgba(20,22,26,.4)"; ctx.stroke();
  ctx.font = "13px system-ui, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillStyle = "#1c1f24";
  ctx.fillText(p.glyph || "•", x, y + 1);
  if (p.count > 1) drawCountBadge(x + 9, y - 9, p.count);
}
function drawCountBadge(x, y, n){
  const txt = "×" + n;
  ctx.font = "9px system-ui, sans-serif";
  const w = Math.max(15, ctx.measureText(txt).width + 7);
  ctx.beginPath(); roundRect(x - w / 2, y - 7, w, 14, 7);
  ctx.fillStyle = "rgba(38,115,201,.95)"; ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.stroke();
  ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(txt, x, y + 0.5);
}
function drawBadge(x, y, text){
  ctx.font = "10px system-ui, sans-serif";
  const w = ctx.measureText(text).width + 10;
  ctx.fillStyle = "rgba(30,33,38,.88)";
  roundRect(x, y - 8, w, 16, 8); ctx.fill();
  ctx.fillStyle = "#f2f3f5"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillText(text, x + 5, y + 1);
}
function roundRect(x, y, w, h, r){
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
// Offset markers: events with no location of their own (app launch, and any
// future type that sets offset_marker). The feature's coordinates are the
// ANCHOR fix — nearest in time either direction — so the glyph is drawn offset
// in SCREEN space (zoom-invariant) with a hairline pointing at that fix.
// `slot` fans markers that share an anchor so they never stack.
// Clock policy: each marker TYPE keeps a fixed bearing from its anchor (12 =
// up, 3 = right, 6 = down, 9 = left), so position itself tells you what kind of
// event it is. Same-type collisions step OUTWARD along the same spoke rather
// than rotating, which would break the policy.
const OFFSET_R = 52, OFFSET_STEP = 30;
function offsetPos(x, y, clock, slot){
  const ang = ((clock % 12) * 30 - 90) * Math.PI / 180;   // 12 o'clock = up
  const r = OFFSET_R + (slot || 0) * OFFSET_STEP;
  return [x + Math.cos(ang) * r, y + Math.sin(ang) * r];
}
function drawOffsetMarker(x, y, p, slot){
  const [mx, my] = offsetPos(x, y, p.offset_clock || 12, slot);
  ctx.save();
  ctx.strokeStyle = "rgba(15,17,20,.92)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(mx, my); ctx.stroke();
  ctx.restore();
  ctx.beginPath(); ctx.arc(x, y, 2.5, 0, 6.2832);
  ctx.fillStyle = "rgba(15,17,20,.92)"; ctx.fill();
  // One marker renderer for every path: an offset marker must draw the same
  // vector icon and tint as an inline one, or the same event type shows two
  // different faces depending on whether it happened to be co-located.
  drawGlyphMarker(mx, my, p);
  return [mx, my];
}
function drawTethered(x, y, p){
  const my = y - 34;
  ctx.save();
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = "rgba(120,126,134,.9)"; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, my + 10); ctx.stroke();
  ctx.restore();
  ctx.beginPath(); ctx.arc(x, y, 2.5, 0, 6.2832);
  ctx.fillStyle = "rgba(120,126,134,.9)"; ctx.fill();
  drawGlyphMarker(x, my, p);
  drawBadge(x + 13, my - 8, "+" + p.dt_minutes + " min");
  return my;
}
function drawDot(x, y, p){
  const fill = p.mock ? "rgba(200,80,180,.9)"
             : "rgba(33,150,243,.9)";   // demo-app location blue — always; only the track colors by speed
  if (typeof p.course === "number"){
    // demo-app chevron: navigation arrow rotated to course (0° = north, cw),
    // center-anchored like the demoapps' rotation(heading)/rotationEffect(course)
    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(p.course * Math.PI / 180);
    ctx.beginPath();
    ctx.moveTo(0, -6.5);        // tip
    ctx.lineTo(4.6, 5.2);       // right tail
    ctx.lineTo(0, 2.4);         // notch
    ctx.lineTo(-4.6, 5.2);      // left tail
    ctx.closePath();
    ctx.fillStyle = fill; ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(13,71,161,.95)";   // dark blue edge, demo-app arrow style
    ctx.stroke();
    ctx.restore();
    return;
  }
  ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 6.2832);
  ctx.fillStyle = fill; ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = "rgba(13,71,161,.9)"; ctx.stroke();
}
function drawCluster(x, y, count, name){
  const r = Math.min(26, 13 + Math.log2(count) * 1.6);
  ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832);
  ctx.fillStyle = name === "errors" ? "rgba(214,69,56,.82)"
               : name === "warnings" ? "rgba(233,166,28,.82)"
               : "rgba(70,110,160,.82)";
  ctx.fill();
  ctx.lineWidth = 2; ctx.strokeStyle = "rgba(255,255,255,.85)"; ctx.stroke();
  const layerIcon = LAYER_VECTOR[name];
  if (layerIcon && (ICON_OPS[layerIcon] || CUSTOM_ICONS[layerIcon])){
    ctx.save(); ctx.translate(x, y - r * 0.28);
    drawIcon(layerIcon, 13, "rgba(255,255,255,.97)");
    ctx.restore();
    ctx.font = "10px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#fff";
    ctx.fillText("×" + count, x, y + r * 0.48);
  } else {
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillStyle = "#fff";
    ctx.fillText((META[name] ? META[name].glyph + " " : "") + count, x, y + 1);
  }
}
function drawLayer(name){
  const items = FEATS[name];
  const markers = [];
  for (let i = 0; i < items.length; i++){
    const it = items[i];
    if (it.pts){ drawLine(it); continue; }
    if (!inWindow(it)) continue;
    const [x, y] = toScreen(it.pt[0], it.pt[1]);
    if (x < -60 || x > W + 60 || y < -60 || y > H + 60) continue;
    markers.push({ x, y, i });
  }
  if (name === "fixes" && showAcc){
    const mpp = metersPerPixel();
    for (const m of markers){
      const acc = items[m.i].f.properties.acc;
      if (!acc) continue;
      const r = acc / mpp;
      if (r < 3 || r > 4000) continue;
      ctx.beginPath(); ctx.arc(m.x, m.y, r, 0, 6.2832);
      ctx.fillStyle = "rgba(60,130,210,.08)"; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = "rgba(60,130,210,.35)"; ctx.stroke();
    }
  }
  if (markers.length > CLUSTER_THRESHOLD){
    const cells = new Map();
    for (const m of markers){
      const key = Math.floor(m.x / 64) + ":" + Math.floor(m.y / 64);
      let cell = cells.get(key);
      if (!cell){ cell = []; cells.set(key, cell); }
      cell.push(m);
    }
    for (const cell of cells.values()){
      if (cell.length === 1){ pendingMarkers.push({name, m: cell[0], items}); continue; }
      let sx = 0, sy = 0;
      for (const m of cell){ sx += m.x; sy += m.y; }
      const cx = sx / cell.length, cy = sy / cell.length;
      drawCluster(cx, cy, cell.length, name);
      hits.push({ x: cx, y: cy, cluster: true, count: cell.length });
    }
  } else {
    for (const m of markers) pendingMarkers.push({ name, m, items });
  }
}
function drawSingle(name, m, items){
  const p = items[m.i].f.properties;
  if (META[name].kind === "dot"){
    drawDot(m.x, m.y, p);
    hits.push({ x: m.x, y: m.y, layer: name, i: m.i });
    return;
  }
  if (p.offset_marker){
    // slot = how many same-spoke markers already share this anchor cell
    const key = Math.round(m.x / 8) + ":" + Math.round(m.y / 8) + ":" + (p.offset_clock || 12);
    const slot = offsetSlots.get(key) || 0;
    offsetSlots.set(key, slot + 1);
    const [mx, my] = drawOffsetMarker(m.x, m.y, p, slot);
    hits.push({ x: mx, y: my, layer: name, i: m.i });
    return;
  }
  if (p.placement === "tethered"){
    const my = drawTethered(m.x, m.y, p);
    hits.push({ x: m.x, y: my, layer: name, i: m.i });
    return;
  }
  drawGlyphMarker(m.x, m.y, p);
  if (p.role === "gap") drawBadge(m.x + 13, m.y - 12, "+" + p.dt_minutes + " min");
  hits.push({ x: m.x, y: m.y, layer: name, i: m.i });
}
// Petal layout: markers that land on the same spot — across ALL layers, since
// a launch, a warning and an http event routinely share one fix — are splayed
// evenly around a ring centred on that spot, each with a hairline pointing
// back to it. One glance shows both "these all happened here" and what each
// one was, instead of a stack of overlapping discs.
const PETAL_CELL = 18, PETAL_MAX = 8;
// Regions are geography, not decoration: each is centred on its feature's OWN
// coordinates and drawn before any marker, so a petaled or offset icon can
// never drag the circle away from the position it describes.
function drawStationaryRegions(){
  for (const name of DRAW_ORDER){
    if (!visible[name]) continue;
    for (const it of FEATS[name]){
      const p = it.f.properties;
      if (!p.stationary_radius_m || !it.pt || !inWindow(it)) continue;
      const [x, y] = toScreen(it.pt[0], it.pt[1]);
      if (x < -4000 || x > W + 4000 || y < -4000 || y > H + 4000) continue;
      drawStationaryRegion(x, y, p);
    }
  }
}
function drawPetals(){
  const groups = new Map();
  for (const pm of pendingMarkers){
    // Markers at their OWN recorded coordinates (fixes, geofence triggers) are
    // at a real place — never move them for legibility. Only time-georeferenced
    // markers, which merely borrow a nearby fix's position, get splayed.
    if (pm.items[pm.m.i].f.properties.own_position){
      drawSingle(pm.name, pm.m, pm.items);
      continue;
    }
    const key = Math.round(pm.m.x / PETAL_CELL) + ":" + Math.round(pm.m.y / PETAL_CELL);
    let g = groups.get(key);
    if (!g){ g = []; groups.set(key, g); }
    g.push(pm);
  }
  for (const g of groups.values()){
    if (g.length === 1){ drawSingle(g[0].name, g[0].m, g[0].items); continue; }
    if (g.length > PETAL_MAX){
      // Beyond a handful, petals become a thicket of spokes that hides the
      // route. Collapse to a count disc; click to zoom in and split it.
      const cx0 = g.reduce((s, pm) => s + pm.m.x, 0) / g.length;
      const cy0 = g.reduce((s, pm) => s + pm.m.y, 0) / g.length;
      drawCluster(cx0, cy0, g.length, g[0].name);
      hits.push({ x: cx0, y: cy0, cluster: true, count: g.length });
      continue;
    }
    // Petals run CLOCKWISE IN TIME: 12:00 is the earliest event at this spot,
    // so the ring reads as a timeline and you can trace what happened in order.
    // (Record seq is assembly order, i.e. chronological.) Type clock only
    // breaks ties, so a lone marker still keeps its policy bearing.
    g.sort((a, b) => ((a.items[a.m.i].f.properties.seq || 0) -
                      (b.items[b.m.i].f.properties.seq || 0)) ||
                     (clockOf(a) - clockOf(b)));
    const cx = g.reduce((s, pm) => s + pm.m.x, 0) / g.length;
    const cy = g.reduce((s, pm) => s + pm.m.y, 0) / g.length;
    const r = Math.max(44, 15 + g.length * 8);
    ctx.beginPath(); ctx.arc(cx, cy, 2.5, 0, 6.2832);
    ctx.fillStyle = "rgba(15,17,20,.9)"; ctx.fill();
    g.forEach((pm, k) => {
      const ang = (k / g.length) * 2 * Math.PI - Math.PI / 2;   // start at 12:00
      const px = cx + Math.cos(ang) * r, py = cy + Math.sin(ang) * r;
      const p = pm.items[pm.m.i].f.properties;
      ctx.save();
      ctx.strokeStyle = "rgba(15,17,20,.30)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(px, py); ctx.stroke();
      ctx.restore();
      if (META[pm.name].kind === "dot") drawDot(px, py, p);
      else drawGlyphMarker(px, py, p);
      hits.push({ x: px, y: py, layer: pm.name, i: pm.m.i });
    });
  }
}
function clockOf(pm){
  const p = pm.items[pm.m.i].f.properties;
  return p.offset_clock || OFFSET_CLOCK_BY_LAYER[pm.name] || 12;
}

function draw(){
  renderTiles();
  ctx.clearRect(0, 0, W, H);
  hits = [];
  offsetSlots.clear();
  pendingMarkers = [];
  drawStationaryRegions();
  for (const name of DRAW_ORDER){
    if (visible[name]) drawLayer(name);
  }
  drawPetals();
}

// ── popup ───────────────────────────────────────────────────────────────────
function esc(s){
  return String(s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function showPopup(hit){
  const it = FEATS[hit.layer][hit.i];
  const p = it.f.properties;
  // 💀 belongs in the header as a MODIFIER — the marker itself shows what
  // happened (🔚 terminate, 📶 http …), the header says it fired headless.
  const headless = (p.role === "launch" && p.headless_launch === true) ||
                   p.headless === true;
  document.getElementById("p-glyph").textContent =
    (headless ? "💀" : "") + (p.glyph || "");
  const label = META[hit.layer].label || hit.layer;
  const cat = p.category || "";
  document.getElementById("p-title").textContent =
    (cat && cat.toLowerCase() !== label.toLowerCase()) ? label + " · " + cat : label;
  const sev = document.getElementById("p-sev");
  if (p.role === "launch"){
    // "normal" severity says nothing about a launch; the useful chip is whether
    // it came up headless. Unknown => no chip at all rather than a false claim.
    if (p.headless_launch === true)       { sev.textContent = "💀 headless"; sev.className = "sev headless"; }
    else if (p.headless_launch === false) { sev.textContent = "has UI";      sev.className = "sev"; }
    else                                  { sev.textContent = "";            sev.className = "sev empty"; }
  } else if (p.severity && p.severity !== "normal"){
    sev.textContent = p.severity;
    sev.className = "sev " + p.severity;
  } else if (p.headless === true){
    sev.textContent = "💀 headless";
    sev.className = "sev headless";
  } else {
    sev.textContent = "";                 // "normal" on every marker is noise
    sev.className = "sev empty";
  }
  const body = document.getElementById("p-body");
  if (p.role === "launch" && p.config_text){
    // The whole TSConfig state, scrollable — the config IS the diagnosis for
    // most tickets, so it belongs in the launch popup verbatim.
    body.textContent = p.popup + "\n\n── TSConfig ──\n" + p.config_text +
      (p.config_truncated ? "\n\n[…truncated by the SDK at ~4KB — ask for getState()]" : "");
    body.style.maxHeight = "min(56vh, 420px)";
  } else {
    body.textContent = p.popup || "";
    body.style.maxHeight = "";
  }
  let meta = "";
  if (p.role === "launch"){
    const bits = [];
    if (p.version) bits.push("v" + p.version + (p.build ? " (" + p.build + ")" : ""));
    else if (p.build) bits.push("build " + p.build);
    if (p.device) bits.push(esc(p.device));
    if (p.app_id) bits.push(esc(p.app_id));
    const hl = p.headless_launch === true  ? " 💀 <b>launched HEADLESS</b> (no UI — background/boot start)"
             : p.headless_launch === false ? " 🖥️ launched with UI (foreground)"
             : " (headless state not stated in this capture)";
    meta += "⚙️ app process launched" + (bits.length ? " — " + bits.join(" · ") : "") +
            "<br>" + hl + "<br>";
  }
  if (p.count > 1){
    meta += "<b>×" + p.count + "</b> occurrences merged at this spot: " +
            p.occurrences.map(esc).join(", ") +
            (p.count > p.occurrences.length ? " … (" + p.count + " total)" : "") + "<br>";
  }
  if (p.offset_marker){
    const secs = Math.abs(p.anchor_dt_s || 0);
    const when = secs >= 90 ? (secs / 60).toFixed(1) + " min" : secs.toFixed(1) + " s";
    meta += "marker is offset — the hairline points at the nearest fix, " +
            when + " " + (p.anchor_dir === "before" ? "earlier" : "later") +
            " (this event has no position of its own)<br>";
  }
  if (p.placement === "tethered"){
    meta += "⚠ tethered: nearest fix is +" + p.dt_minutes +
            " min away (Δt " + p.dt_s + " s — beyond the 120 s placement rule)<br>";
  } else if (p.role === "event"){
    meta += "Δt to nearest fix: " + p.dt_s + " s (placed)<br>";
  }
  if (p.event){
    meta += "event: <b>" + esc(p.event) + "</b>" +
            (p.headless ? " — 💀 fired while HEADLESS (MainActivity destroyed)" : "") +
            "<br>";
    if (p.delivered === true){
      meta += "delivered to the headless task in " + p.delivery_ms + " ms<br>";
    } else if (p.delivered === false){
      meta += "⚠ dispatched but NO receipt logged — the headless task may not have run it<br>";
    }
  }
  if (p.suppressed){
    meta += "🚷 <b>transition NOT delivered</b> — " + esc(p.suppressed) + "<br>";
    const pt = (lab) => {
      const la = p[lab + "_lat"], lo = p[lab + "_lon"], ac = p[lab + "_acc"];
      if (la === undefined || la === null) return "";
      return "&nbsp;&nbsp;" + lab + ": <code>" + la.toFixed(6) + ", " + lo.toFixed(6) +
             "</code>" + (ac !== undefined && ac !== null
               ? " ± <b>" + ac + " m</b>" + (ac >= 100 ? " ⚠ poor" : "") : "") + "<br>";
    };
    meta += pt("trigger") + pt("stationary") + pt("a") + pt("b");
    if (p.min_possible_m !== undefined){
      meta += "&nbsp;&nbsp;closest the device could actually be: " +
              p.min_possible_m + " m<br>";
    }
  }
  if (p.dist_m !== undefined && p.radius_m !== undefined){
    meta += "trigger " + p.dist_m + " m from fence center (radius " + p.radius_m + " m)" +
            (p.verdict ? " — " + esc(p.verdict) : "") +
            (p.trigger_acc_m !== undefined ? " · fix accuracy " + p.trigger_acc_m + " m" : "") +
            "<br>";
  }
  if (p.role === "gap"){
    meta += "classification: " + esc(p.classification) +
            " · " + p.dt_minutes + " min silence<br>";
  }
  if (p.acc !== undefined) meta += "accuracy: " + p.acc + " m · ";
  if (p.speed !== undefined && p.speed !== null) meta += "speed: " + p.speed + " m/s · ";
  if (p.ts) meta += esc(p.ts);
  if (p.slice_ts){
    meta += '<br>slice: <code>--slice "' + esc(p.slice_ts) + '±120"</code>' +
            ' <span class="copy" data-slice="' + esc(p.slice_ts) + '">copy</span>';
  }
  document.getElementById("p-meta").innerHTML = meta;
  popup.style.display = "block";
  const pw = popup.offsetWidth, ph = popup.offsetHeight;
  let px = hit.x + 16, py = hit.y - ph / 2;
  if (px + pw > W - 8) px = hit.x - pw - 16;
  py = Math.max(8, Math.min(py, H - ph - 8));
  popup.style.left = Math.max(8, px) + "px";
  popup.style.top = py + "px";
}
document.getElementById("p-close").addEventListener("click",
  () => { popup.style.display = "none"; });
document.getElementById("p-meta").addEventListener("click", e => {
  const t = e.target;
  if (t.classList && t.classList.contains("copy") && navigator.clipboard){
    navigator.clipboard.writeText('--slice "' + t.dataset.slice + '±120"');
    t.textContent = "copied";
    setTimeout(() => { t.textContent = "copy"; }, 1200);
  }
});

// ── interaction ─────────────────────────────────────────────────────────────
let dragging = false, moved = 0, lastX = 0, lastY = 0;
mapEl.addEventListener("mousedown", e => {
  dragging = true; moved = 0; lastX = e.clientX; lastY = e.clientY;
  mapEl.classList.add("dragging");
});
window.addEventListener("mousemove", e => {
  if (!dragging) return;
  const dx = e.clientX - lastX, dy = e.clientY - lastY;
  lastX = e.clientX; lastY = e.clientY;
  moved += Math.abs(dx) + Math.abs(dy);
  view.cx -= dx / scale(); view.cy -= dy / scale();
  draw();
});
window.addEventListener("mouseup", e => {
  if (!dragging) return;
  dragging = false;
  mapEl.classList.remove("dragging");
  if (moved < 4) handleClick(e);
});
mapEl.addEventListener("wheel", e => {
  e.preventDefault();
  zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1 : -1);
}, { passive: false });
mapEl.addEventListener("dblclick", e => zoomAt(e.clientX, e.clientY, 1));
function zoomAt(px, py, dir){
  const nz = Math.max(2, Math.min(19, view.z + dir));
  if (nz === view.z) return;
  const [mx, my] = fromScreen(px, py);
  view.z = nz;
  view.cx = mx - (px - W / 2) / scale();
  view.cy = my - (py - H / 2) / scale();
  draw();
}
function handleClick(e){
  const rect = mapEl.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  let best = null, bestD = 15;
  for (let i = hits.length - 1; i >= 0; i--){
    const h = hits[i];
    const d = Math.hypot(h.x - x, h.y - y);
    if (d < bestD){ best = h; bestD = d; }
  }
  if (!best){ popup.style.display = "none"; return; }
  if (best.cluster){ zoomAt(best.x, best.y, 2); return; }
  showPopup(best);
}
document.getElementById("zin").addEventListener("click",
  () => zoomAt(W / 2, H / 2, 1));
document.getElementById("zout").addEventListener("click",
  () => zoomAt(W / 2, H / 2, -1));
document.getElementById("zfit").addEventListener("click", fitBounds);
// "Fit" means fit WHAT IS SHOWN. With a time window on, framing the whole
// capture would zoom out past everything the window selected — the one thing
// the reader just said they were not looking at.
function windowBounds(){
  if (!windowed()) return bbox;
  const bb = [Infinity, Infinity, -Infinity, -Infinity];
  const add = p => {
    bb[0] = Math.min(bb[0], p[0]); bb[1] = Math.min(bb[1], p[1]);
    bb[2] = Math.max(bb[2], p[0]); bb[3] = Math.max(bb[3], p[1]);
  };
  for (const name of ORDER){
    if (!visible[name]) continue;
    for (const it of FEATS[name]){
      if (it.pt){ if (inWindow(it)) add(it.pt); continue; }
      for (const r of windowRuns(it, it.pts.length)){
        for (let i = r[0]; i <= r[1]; i++) add(it.pts[i]);
      }
    }
  }
  return isFinite(bb[0]) ? bb : bbox;
}
function fitBounds(){
  if (W <= 0 || H <= 0){ needFit = true; return; }
  const bb = windowBounds();
  if (!isFinite(bb[0])){ view.z = 3; view.cx = 0.5; view.cy = 0.5; draw(); return; }
  const dx = Math.max(bb[2] - bb[0], 1e-9);
  const dy = Math.max(bb[3] - bb[1], 1e-9);
  const z = Math.floor(Math.log2(Math.min((W * 0.8) / (256 * dx),
                                          (H * 0.8) / (256 * dy))));
  view.z = Math.max(2, Math.min(17, z));
  view.cx = (bb[0] + bb[2]) / 2;
  view.cy = (bb[1] + bb[3]) / 2;
  draw();
}

__NAV_JS__

// ── navigator wiring ────────────────────────────────────────────────────────
// The only code that knows about both the map and the TimeNavigator. It feeds
// the component generic time data (events to plot, bands to shade, sessions to
// bracket) and reacts to the window it reports. Everything domain-specific —
// what a "wedge-candidate" is, what a session should be called — is decided
// here, never inside the component.
const countEls = {};                 // layer -> the count chip in the panel
const navEls = {
  range: document.getElementById("nav-range"),
  span: document.getElementById("nav-span"),
  session: document.getElementById("nav-session"),
  count: document.getElementById("nav-count"),
  all: document.getElementById("nav-all"),
  prev: document.getElementById("nav-prev"),
  next: document.getElementById("nav-next"),
};

function navEvents(){
  const out = [];
  for (const name of ORDER){
    for (const it of FEATS[name]){
      if (it.t === null || it.pts) continue;    // lines are spans, not events
      out.push({ t: it.t, w: it.f.properties.count || 1,
                 sev: it.f.properties.severity || null });
    }
  }
  return out;
}
function navBands(){
  // The analyzer's classified silences. A wedge-candidate is the one worth
  // seeing from across the room, so it alone gets the alert tone.
  const out = [];
  for (const it of (FEATS["gaps"] || [])){
    if (it.t === null || it.f.properties.role !== "gap") continue;
    out.push({ a: it.t, b: it.t1,
               tone: it.f.properties.classification === "wedge-candidate"
                     ? "alert" : "muted" });
  }
  return out;
}
function navSessions(){
  return SESSIONS.map(s => {
    const a = parseTs(s.start), b = parseTs(s.end);
    if (a === null || b === null) return null;
    const bits = ["session " + s.i + " of " + SESSIONS.length];
    if (s.distance_m >= 100) bits.push((s.distance_m / 1000).toFixed(1) + " km");
    if (s.ended_by) bits.push("ended by " + s.ended_by);
    return { i: s.i, a: a, b: b, label: bits.join(" · ") };
  }).filter(Boolean);
}

function windowStats(){
  const per = {}; let vis = 0;
  for (const name of ORDER){
    let c = 0;
    for (const it of FEATS[name]) if (inWindow(it)) c++;
    per[name] = c; vis += c;
  }
  return { per, vis };
}
function updateNavReadout(){
  if (!HAS_TIME || !nav) return;
  const d = nav.describe();
  navEls.range.textContent = d.range;
  navEls.span.textContent = "· " + d.span;
  navEls.session.textContent = d.session ? "· " + d.session : "";
  const st = windowStats();
  navEls.count.textContent =
    st.vis.toLocaleString() +
    (nav.isWindowed() ? " of " + total.toLocaleString() : "") + " features";
  for (const name of ORDER){
    if (countEls[name]) countEls[name].textContent = st.per[name].toLocaleString();
  }
  navEls.all.disabled = !nav.isWindowed();
}

let rafPending = false;
if (HAS_TIME){
  nav = TimeNavigator({
    canvas: document.getElementById("navcv"),
    t0: T0, t1: T1,
    events: navEvents(), bands: navBands(), sessions: navSessions(),
    onChange: change => {
      popup.style.display = "none";   // the marker behind it may have just left
      if (change.reason === "session" || change.reason === "reset"){
        // A deliberate jump should show the geography; scrubbing should not
        // yank the view out from under the reader.
        fitBounds();
        updateNavReadout();
        return;
      }
      if (rafPending) return;
      rafPending = true;
      requestAnimationFrame(() => {
        rafPending = false;
        draw(); updateNavReadout();
      });
    },
  });
  navEls.prev.addEventListener("click", () => nav.step(-1));
  navEls.next.addEventListener("click", () => nav.step(1));
  navEls.all.addEventListener("click", () => nav.reset());
  if (nav.sessions().length < 2){
    navEls.prev.style.display = "none";
    navEls.next.style.display = "none";
  }
} else {
  document.body.classList.add("notime");
}


// ── layer panel ─────────────────────────────────────────────────────────────
(function buildPanel(){
  const list = document.getElementById("layerlist");
  for (const name of ORDER){
    const m = META[name];
    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    // Routine high-volume layers (e.g. a few hundred successful HTTP flushes)
    // start OFF so the route and the anomalies are legible before the noise.
    cb.checked = visible[name];
    cb.addEventListener("change", () => { visible[name] = cb.checked; draw(); });
    label.appendChild(cb);
    const txt = document.createElement("span");
    txt.textContent = m.glyph + " " + m.label;
    label.appendChild(txt);
    const cnt = document.createElement("span");
    cnt.className = "cnt"; cnt.textContent = m.count;
    countEls[name] = cnt;          // the brush rewrites these to windowed counts
    label.appendChild(cnt);
    list.appendChild(label);
    if (name === "track"){
      const sub = document.createElement("label");
      sub.className = "sub";
      const scb = document.createElement("input");
      scb.type = "checkbox"; scb.checked = false;   // demo-app blue by default
      scb.addEventListener("change", () => {
        showSpeed = scb.checked;
        document.getElementById("speedlegend").style.display = showSpeed ? "" : "none";
        draw();
      });
      sub.appendChild(scb);
      const stxt = document.createElement("span");
      stxt.textContent = "color by speed";
      sub.appendChild(stxt);
      list.appendChild(sub);
    }
    if (name === "fixes"){
      const sub = document.createElement("label");
      sub.className = "sub";
      const acb = document.createElement("input");
      acb.type = "checkbox"; acb.checked = false;   // off by default per design
      acb.addEventListener("change", () => { showAcc = acb.checked; draw(); });
      sub.appendChild(acb);
      const stxt = document.createElement("span");
      stxt.textContent = "accuracy circles";
      sub.appendChild(stxt);
      list.appendChild(sub);
    }
  }
  document.getElementById("stats").textContent =
    ORDER.length + " layers · " + total + " features"
    + (SESSIONS.length ? " · " + SESSIONS.length + " session"
                         + (SESSIONS.length > 1 ? "s" : "") : "")
    + " · local-only, full precision";
  if (total === 0){
    const d = document.createElement("div");
    d.id = "empty";
    d.textContent = "No georeferenced features in this capture.";
    document.body.appendChild(d);
  }
})();

resize();
updateNavReadout();
</script>
</body>
</html>
"""
