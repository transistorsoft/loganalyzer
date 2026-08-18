"""Stage 5 (map side) — GeoJSON layer builder.

``build_layers(analysis, records)`` turns an Analysis + annotated Records into
per-layer GeoJSON FeatureCollections consumed by emit/map.py:

    track / fixes / rejections / lifecycle / errors / warnings / geofence /
    motionchange / http / gaps / mock          (empty layers are omitted)

Rules honored (design v2 + INTERFACES.md):
- Positions come from ``rec.structs`` location data (structs.annotate output).
  If the caller passed un-annotated records, annotation is run here as a
  fallback (annotate never mutates raw/body and never raises).
- Non-location events are time-georeferenced: binary-search the nearest fix by
  timestamp; dt <= 120 s => ``placement: "placed"`` (position interpolated
  between the bracketing fixes when they are close enough, else the nearest
  fix); larger => ``placement: "tethered"`` to the LAST KNOWN fix, with a
  ``dt_minutes`` property (the map renders a "+N min" badge + dashed tether).
- The track is one or more LineStrings, split at classified gaps (and at any
  silent stretch >= the 15-min gap threshold), carrying a per-vertex ``speeds``
  array for speed-colored rendering.
- Every feature carries: ts (ISO), category, severity, glyph (the record's
  emoji or the layer glyph), popup (the record in the two-line shape, ~400
  chars), slice_ts (copy-ready for --slice), placement, dt_s.
- Maps are full-precision LOCAL-ONLY artifacts: nothing is redacted here.
"""
from __future__ import annotations

import re
from bisect import bisect_left
from datetime import datetime
from typing import Any, Optional

from ..analyze import GAP_THRESHOLD_S, field, gap_list, iso, parse_iso
from ..locations import GF_DIAGNOSTIC, Fix, collect_fixes
from ..model import ANDROID, IOS, Record
from ..maprules import load_rules
from ..structs import annotate

# The 120-second honesty rule (empirically calibrated, design v2).
PLACED_MAX_DT_S = 120.0
# Interpolate between bracketing fixes only when they are this close together.
_INTERP_MAX_SPAN_S = 600.0
# Popup truncation.
_POPUP_MAX_CHARS = 400

# Presentation is DATA: vocabulary/map-rules.yaml owns every per-layer fact —
# order, label, draw kind, glyph, icon, bearing, bulk-hide. This module reads
# the two it needs to BUILD features (which layers exist, and the glyph a
# feature carries as its text fallback); the renderer reads the rest from
# maprules directly rather than through here.
_RULES = load_rules()
LAYER_ORDER = _RULES.layer_order()

# Offset-marker clock policy: a marker with no location of its own is drawn
# offset from its anchor fix, and each TYPE always sits at the same clock
# position. Position therefore carries meaning — launches are always at 12,
# terminations always at 6 — so a cluster is readable at a glance. Same-type
# collisions extend outward along the same spoke instead of rotating.
DEFAULT_OFFSET_CLOCK = 12

# An SDK event dispatch belongs on the layer for what it IS. Events with no
# layer of their own (heartbeat, enabledchange, …) stay on lifecycle.
# Dispatches of these events duplicate a record the map already shows (the
# geofence transition, the pace change, the flush outcome). Events with no other
# representation — heartbeat, connectivitychange, providerchange … — are kept.
_REDUNDANT_EVENTS = {"geofence", "geofenceschange", "motionchange", "http", "location"}

_EVENT_LAYER = {
    "geofence": "geofence", "geofenceschange": "geofence",
    "motionchange": "motionchange", "http": "http",
    "locationerror": "warnings", "locationfilter": "rejections",
}

# Drawn icons replace emoji on the map: one library, consistent weight,
# colourable, and able to say things no emoji says ("stationary exit
# suppressed"). The emoji survives in the feature's `glyph` as the text
# fallback for the digest and GeoJSON, which have no canvas to draw on.
OFFSET_CLOCK = _RULES.offset_clocks()

# ── small helpers ────────────────────────────────────────────────────────────

_BOX_ONLY = re.compile(r"^[\s╔╚╠╣╢╤╧═─━┃]+$")
# Real wordings, verified against captures — the transition word sits on either
# side of the noun, and Android says "Geofencing Event:", not "Geofence":
#   Android: "║ Geofencing Event: EXIT"      iOS: "📢 EXIT Geofence: Test"
_GF_TRANSITION = re.compile(
    r"[Gg]eofenc(?:e|ing)\b[^\n]{0,20}?\b(ENTER|EXIT|DWELL)\b"
    r"|\b(ENTER|EXIT|DWELL)\b[^\n]{0,4}[Gg]eofence")

_WATCHDOG_FIRE = re.compile(r"[Ww]atchdog.{0,20}(?:fired|force)|force.?unlock")

# Transitions the SDK decided NOT to deliver. On a map these matter as much as
# the ones that fired: "the fence should have triggered here and didn't".
_GF_SUPPRESSED = re.compile(
    r"Ignoring stationary geofence EXIT[^\n]*"
    r"|Ignoring (?:spurious|duplicate|deferred) geofence[^\n]*"
    r"|Deferring geofence transition[^\n]*"
    r"|Normalizing stale (?:PENDING_EXIT|PENDING_ENTER)[^\n]*")


def _popup(rec: Record) -> str:
    """The record in the authorial two-line shape: header line + first content
    line (pure box-drawing rows dropped), truncated to ~400 chars."""
    lines = [ln for ln in rec.raw.splitlines()
             if ln.strip() and not _BOX_ONLY.match(ln)]
    if not lines:
        lines = rec.raw.splitlines()[:1]
    return "\n".join(lines[:2])[:_POPUP_MAX_CHARS]


# An SDK event dispatch: "[HeadlessEventTx fire] 🛜 💀⚡️ http" or
# "[EventManager fire] 🛜 ⚡️ location". The trailing token is the event NAME —
# that is the information that matters. 💀 only says the app was headless when
# it fired, which is a modifier on the event, not the event itself.
_EVENT_FIRE = re.compile(r"⚡️?\s*([a-z][a-z]*)\s*$", re.M)
# The headless task's own dispatch line names the event differently:
#   "[HeadlessTask onHeadlessEvent] 💀  event: terminate"
_EVENT_NAMED = re.compile(r"\bevent:\s*([a-z][a-z]*)\b")

EVENT_GLYPH = {
    "http": "📶", "location": "📍", "geofence": "📢", "geofenceschange": "📢",
    "motionchange": "🚘", "heartbeat": "❤️", "activitychange": "🚘",
    "providerchange": "📡", "connectivitychange": "📶", "powersavechange": "🔋",
    "enabledchange": "🟢", "schedule": "📅", "locationerror": "⚠️",
    "locationfilter": "🔎", "notificationaction": "🔔",
    "terminate": "🔚", "boot": "🔌",
}


def _event_name(rec: Record) -> str | None:
    """The dispatched event's NAME — the thing that actually happened.

    Covers both dispatch shapes: `*EventTx fire` / `EventManager fire` (⚡️ then
    the name) and `HeadlessTask onHeadlessEvent` ("event: terminate"). 💀 on
    either only means the app was headless at the time; it is never the event.
    """
    tm = rec.tag_method or ""
    if "fire" in tm and "⚡" in rec.raw:
        m = _EVENT_FIRE.search(rec.raw)
        if m:
            return m.group(1)
    if "HeadlessEvent" in tm or "onHeadlessEvent" in rec.raw:
        m = _EVENT_NAMED.search(rec.raw)
        if m:
            return m.group(1)
    return None


_DELIVERY_WINDOW_S = 5.0


def _collapse_event_pairs(records: list[Record]) -> tuple[set[int], dict[int, dict]]:
    """One event, one marker.

    A headless event is logged TWICE, milliseconds apart: `HeadlessEventTx fire`
    when the SDK dispatches it and `HeadlessTask onHeadlessEvent` when the
    headless task receives it. Mapping both double-counts every event. Keep the
    dispatch, suppress the matching receipt, and record whether delivery was
    confirmed — a dispatch with NO receipt means the headless task never ran it,
    which is a finding, not noise.
    """
    suppress: set[int] = set()
    extra: dict[int, dict] = {}
    pending: dict[str, Record] = {}
    for rec in records:
        ev = _event_name(rec)
        if not ev:
            continue
        if "fire" in (rec.tag_method or ""):
            pending[ev] = rec
            extra.setdefault(rec.seq, {})["delivered"] = False
            continue
        tx = pending.pop(ev, None)
        if tx is None or not (tx.ts and rec.ts):
            continue                      # receipt with no dispatch: keep as-is
        dt = (rec.ts - tx.ts).total_seconds()
        if 0 <= dt <= _DELIVERY_WINDOW_S:
            suppress.add(rec.seq)
            extra[tx.seq] = {"delivered": True, "delivery_ms": round(dt * 1000)}
    return suppress, extra


# Numbers AND booleans are masked: `onWindowFocusChanged: true` / `: false`
# flapping at one spot is one story ("focus flapped here"), not four. The
# collapsed marker keeps every occurrence in its popup, so nothing is lost.
_DEDUPE_STRIP = re.compile(r"\d+|\b(?:true|false)\b")


def _dedupe_key(props: dict) -> str:
    """What makes two markers 'the same event' for map purposes: the event name
    if there is one, else the message shape with numbers masked (so
    `onWindowFocusChanged: true/false` chatter collapses into one petal)."""
    ev = props.get("event")
    if ev:
        return f"event:{ev}"
    # Use the first line that actually SAYS something. On iOS a banner record's
    # first line is a bare timestamp, which would strip to nothing and collapse
    # every such record at one anchor into a single marker.
    text = ""
    for line in (props.get("popup") or "").splitlines():
        cand = re.sub(r"^\S+ \S+ ", "", line).strip()
        if sum(ch.isalpha() for ch in cand) >= 3:
            text = cand
            break
    return _DEDUPE_STRIP.sub("#", text)[:90]


# THE compaction contract. Property KEY NAMES repeat on every feature, so
# default values cost real bytes — a 31k-record Android log was 1.7 MB of layer
# JSON. Each of these is dropped when it holds its default and reconstructed on
# the other side.
#
# This table is the ONLY statement of that contract: _compact() drops from it,
# the map is handed it as __PROP_DEFAULTS__ and re-inflates from it, and the
# tests read it. It used to be written out three times, and a drift between any
# two of them silently changed what the map drew.
#
#   {"value": x}      the default is a literal
#   {"from": "ts"}    the default is the feature's own ts
#   {"from": "layer"} the default is the name of the layer it lives on
COMPACT_DEFAULTS: dict[str, dict] = {
    "slice_ts":     {"from": "ts"},
    "category":     {"from": "layer"},
    "placement":    {"value": "placed"},
    "dt_s":         {"value": 0.0},
    "own_position": {"value": False},
    "severity":     {"value": "normal"},
    "count":        {"value": 1},
}
# Dropped alongside their owner rather than on their own account.
_COMPACT_COMPANIONS = {"count": ("occurrences",)}


def compact_default(props: dict, key: str, layer: str) -> Any:
    """The value `key` holds when it was compacted away."""
    spec = COMPACT_DEFAULTS.get(key)
    if spec is None:
        return None
    if "from" in spec:
        return props.get("ts") if spec["from"] == "ts" else layer
    return spec["value"]


def _compact(features: list[dict], layer: str) -> list[dict]:
    """Drop every property that equals its default, so a big capture stays a
    loadable file. Driven entirely by COMPACT_DEFAULTS."""
    for f in features:
        p = f["properties"]
        for key in COMPACT_DEFAULTS:
            if key not in p:
                continue
            default = compact_default(p, key, layer)
            # dt_s/own_position are falsy-by-default; anything falsy is the
            # default for them, which `==` alone would miss for 0 vs 0.0.
            if p[key] == default or (not default and not p[key]):
                p.pop(key)
                for companion in _COMPACT_COMPANIONS.get(key, ()):
                    p.pop(companion, None)
        geom = f["geometry"]
        if geom["type"] == "Point":                    # ~0.1 m is plenty
            geom["coordinates"] = [round(c, 6) for c in geom["coordinates"]]
    return features


def _dedupe_markers(features: list[dict]) -> list[dict]:
    """Collapse repeats of the same event at the same anchor into one marker
    carrying a count. Nine identical ☯️ petals say nothing nine times; one
    petal reading '☯️ ×9' says it once and the popup lists every occurrence."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for f in features:
        p = f["properties"]
        if (f["geometry"]["type"] != "Point" or p.get("own_position")
                or p.get("role") in ("track", "gap", "fix", "mock-fix")):
            out.append(f)             # lines, and real positions, are never merged
            continue
        key = (tuple(f["geometry"]["coordinates"]), _dedupe_key(p))
        first = seen.get(key)
        if first is None:
            seen[key] = f
            p["count"] = 1
            p["occurrences"] = [p.get("ts")]
            out.append(f)
            continue
        fp = first["properties"]
        fp["count"] += 1
        if len(fp["occurrences"]) < 12:
            fp["occurrences"].append(p.get("ts"))
        fp["last_ts"] = p.get("ts")
    return out


# One flush, one marker. A single HTTP flush logs ~5 records (beginFlush,
# armWatchdog, schedulePost, doPost, post, handleResponse, finish) and mapping
# each one puts 3+ identical cloud markers on one spot. What matters is the
# OUTCOME: did this flush succeed, and with what status. So keep the outcome
# record and drop the rest — and drop no-op flushes entirely, where the SDK had
# nothing queued to send.
_HTTP_FINISH = re.compile(
    r"finish:error:\]\s*success=(?P<ok>[01])"
    r"(?:[^\n]*?queued_before=(?P<queued>\d+))?"
    r"(?:[^\n]*?synced=(?P<synced>\d+))?"
    r"(?:[^\n]*?pages=(?P<pages>\d+))?"
    r"(?:[^\n]*?duration_ms=(?P<ms>\d+))?")
_HTTP_RESPONSE = re.compile(r"Response:\s*(\d{3})")
_HTTP_COUNT = re.compile(r"HTTP Service \(count:\s*(\d+)\)")


def _collapse_http(records: list[Record]) -> tuple[set[int], dict[int, dict]]:
    suppress: set[int] = set()
    extra: dict[int, dict] = {}
    last_status: Optional[int] = None
    for rec in records:
        # Scan EVERY record: on iOS the status lives on a `Response:` line that
        # is deliberately not an http-layer marker, so it must still be read to
        # annotate the outcome that follows it.
        m_status = _HTTP_RESPONSE.search(rec.raw)
        if m_status and not _HTTP_FINISH.search(rec.raw):
            last_status = int(m_status.group(1))
        if not _is_http(rec):
            continue
        fin = _HTTP_FINISH.search(rec.raw)
        if fin is None:
            # not the outcome record; remember any status it carried, drop it
            if m_status:
                last_status = int(m_status.group(1))
                if rec.platform == ANDROID:
                    # Android has no finish banner — the Response line IS the outcome
                    ok = 200 <= last_status < 300
                    extra[rec.seq] = {"http_ok": ok, "http_status": last_status,
                                      "icon_name": "http" if ok else "http-error",
                                      "severity": "normal" if ok else "warning"}
                    continue
            cnt = _HTTP_COUNT.search(rec.raw)
            if cnt and int(cnt.group(1)) == 0:
                suppress.add(rec.seq)          # nothing queued: a no-op flush
                continue
            suppress.add(rec.seq)
            continue
        ok = fin.group("ok") == "1"
        synced = int(fin.group("synced") or 0)
        pages = int(fin.group("pages") or 0)
        if ok and synced == 0 and pages == 0:
            suppress.add(rec.seq)              # nothing to send — not an event
            continue
        info = {"http_ok": ok, "icon_name": "http" if ok else "http-error",
                "severity": "normal" if ok else "warning",
                "http_synced": synced, "http_pages": pages}
        if fin.group("ms"):
            info["http_ms"] = int(fin.group("ms"))
        if fin.group("queued"):
            info["http_queued"] = int(fin.group("queued"))
        if last_status is not None:
            info["http_status"] = last_status
        extra[rec.seq] = info
        last_status = None
    return suppress, extra


_GF_EPISODE_WINDOW_S = 5.0
_GF_TRANSITION_TAG = re.compile(r"handleGeofencingEvent|setTriggerLocation")


def _collapse_geofence(records: list[Record]) -> tuple[set[int], dict[int, dict]]:
    """One geofence transition, one marker.

    A single trigger logs ~4 records: the transition itself, two diagnostic
    banners (`Trigger vs Geofence center`, `Trigger vs last location`) and a
    `GeofenceDAO updateState` DB write — plus, when headless, an event dispatch
    for the same transition. Mapping all of them put 2,236 markers on a log with
    ~535 transitions. Keep the transition, fold the diagnostics' spatial numbers
    into it, and suppress the rest.
    """
    suppress: set[int] = set()
    extra: dict[int, dict] = {}
    current: Optional[Record] = None
    for rec in records:
        if not _is_geofence(rec):
            continue
        if _GF_TRANSITION_TAG.search(rec.tag_method or "") or \
                (rec.structs.get("geofence") or {}).get("action"):
            current = rec                       # the transition owns the episode
            continue
        if current is None or rec.ts is None or current.ts is None:
            continue
        if abs((rec.ts - current.ts).total_seconds()) > _GF_EPISODE_WINDOW_S:
            current = None
            continue
        # a diagnostic/bookkeeping record inside the episode: fold + drop
        info = extra.setdefault(current.seq, {})
        for key, pat in (("dist_m", r"dist(?:ance)?=([\d.]+)m"),
                         ("radius_m", r"radius=([\d.]+)m"),
                         ("outside_by_m", r"outsideBy=([\d.]+)"),
                         ("required_speed_mps", r"requiredSpeed=([\d.]+)")):
            m = re.search(pat, rec.raw)
            if m and key not in info:
                info[key] = float(m.group(1))
        suppress.add(rec.seq)
    return suppress, extra


def _glyph(rec: Record, layer: str) -> str:
    ev = _event_name(rec)
    if ev:
        # Event TYPE drives the icon; headless is shown as a modifier instead.
        return EVENT_GLYPH.get(ev, "⚡️")
    if "💀" in rec.raw:
        return "💀"
    return rec.icon or _RULES.layer_glyph(layer)


def _category(rec: Record, layer: str) -> str:
    ev = _event_name(rec)
    if ev:
        return f"{ev} event"          # "http event", not "lifecycle"
    if rec.klass is not None and rec.klass.category:
        return rec.klass.category
    return layer


def _feature(geom_type: str, coords: Any, props: dict) -> dict:
    return {"type": "Feature",
            "geometry": {"type": geom_type, "coordinates": coords},
            "properties": props}


def _base_props(rec: Record, layer: str, placement: str, dt_s: float) -> dict:
    props: dict[str, Any] = {
        "seq": rec.seq,
        "ts": iso(rec.ts),
        "category": _category(rec, layer),
        "severity": rec.severity,
        "glyph": _glyph(rec, layer),
        "popup": _popup(rec),
        "slice_ts": iso(rec.ts) or rec.ts_raw,
        "placement": placement,
        "dt_s": round(dt_s, 1),
    }
    if placement == "tethered":
        props["dt_minutes"] = round(dt_s / 60, 1)
    ev = _event_name(rec)
    rule = _rule_for(rec, layer)
    icon = (_RULES.event_icon(ev)
            or (rule.icon if rule else None)
            or _RULES.layer_icon(layer))
    if rule and rule.tint:
        props_tint = rule.tint
    else:
        props_tint = None
    if icon:
        props["icon_name"] = icon
    if props_tint:
        props["tint"] = props_tint
    if ev:
        props["event"] = ev
        # Headless = the event fired with MainActivity destroyed. A property of
        # the delivery, not of the event type — surfaced in the popup.
        props["headless"] = "💀" in rec.raw
    return props


_LABELLED_LOC = re.compile(r"(\w+)=Location\[")


def _labelled_locations(rec: Record) -> dict[str, dict]:
    """`Stationary=Location[…]` / `Trigger=Location[…]` -> {label: parsed loc}.

    The labels are the whole point: a suppressed stationary EXIT is only
    legible when you can compare where the device was parked (tight accuracy)
    against the fix that claimed it left (usually terrible accuracy).
    """
    labels = [m.group(1).lower() for m in _LABELLED_LOC.finditer(rec.raw)]
    locs = rec.structs.get("locations") or []
    if not labels or len(locs) < len(labels):
        return {}
    return {lab: loc for lab, loc in zip(labels, locs) if isinstance(loc, dict)}


class _FixIndex:
    """Timestamp-sorted fix positions with binary-search georeferencing."""

    def __init__(self, fixes: list[Fix]):
        timed = sorted((f for f in fixes if f.t is not None), key=lambda f: f.t)
        self._ts: list[datetime] = [f.t for f in timed]          # type: ignore[misc]
        self._lon = [f.lon for f in timed]
        self._lat = [f.lat for f in timed]

    @property
    def empty(self) -> bool:
        return not self._ts

    def last_at_or_before(self, t: datetime) -> Optional[tuple[float, float]]:
        i = bisect_left(self._ts, t)
        if i < len(self._ts) and self._ts[i] == t:
            return self._lon[i], self._lat[i]
        return (self._lon[i - 1], self._lat[i - 1]) if i > 0 else None

    def first_at_or_after(self, t: datetime) -> Optional[tuple[float, float]]:
        i = bisect_left(self._ts, t)
        return (self._lon[i], self._lat[i]) if i < len(self._ts) else None

    def nearest(self, t: datetime) -> Optional[tuple[float, float, float]]:
        """Closest fix in EITHER direction -> (lon, lat, signed dt_s).

        Negative dt = the anchor fix precedes the event, positive = follows it.
        Used by markers that have no location of their own (app launch, and any
        other offset marker): they anchor to whichever fix is nearest in time,
        backwards or forwards, and the map draws a pointer to it.
        """
        if self.empty:
            return None
        i = bisect_left(self._ts, t)
        best: Optional[tuple[float, float, float]] = None
        for j in (i - 1, i):
            if 0 <= j < len(self._ts):
                dt = (self._ts[j] - t).total_seconds()
                if best is None or abs(dt) < abs(best[2]):
                    best = (self._lon[j], self._lat[j], dt)
        return best

    def locate(self, t: datetime) -> Optional[tuple[float, float, str, float]]:
        """-> (lon, lat, placement, dt_s) or None when no fixes exist."""
        if self.empty:
            return None
        i = bisect_left(self._ts, t)
        prev = i - 1 if i > 0 else None
        nxt = i if i < len(self._ts) else None
        dt_prev = (t - self._ts[prev]).total_seconds() if prev is not None else None
        dt_next = (self._ts[nxt] - t).total_seconds() if nxt is not None else None

        if dt_next is None or (dt_prev is not None and dt_prev <= dt_next):
            near, dt_near = prev, dt_prev
        else:
            near, dt_near = nxt, dt_next
        assert near is not None and dt_near is not None

        if dt_near <= PLACED_MAX_DT_S:
            if prev is not None and nxt is not None:
                span = (self._ts[nxt] - self._ts[prev]).total_seconds()
                if 0 < span <= _INTERP_MAX_SPAN_S:
                    f = (t - self._ts[prev]).total_seconds() / span
                    lon = self._lon[prev] + (self._lon[nxt] - self._lon[prev]) * f
                    lat = self._lat[prev] + (self._lat[nxt] - self._lat[prev]) * f
                    return lon, lat, "placed", dt_near
            return self._lon[near], self._lat[near], "placed", dt_near

        # Tether to the LAST KNOWN fix (the first fix when the event precedes
        # the whole track); dt_s reports the distance to that anchor.
        anchor = prev if prev is not None else nxt
        dt_anchor = dt_prev if prev is not None else dt_next
        assert anchor is not None and dt_anchor is not None
        return self._lon[anchor], self._lat[anchor], "tethered", dt_anchor


# ── layer routing ────────────────────────────────────────────────────────────

# The version banner marks an app PROCESS LAUNCH — on Android it also carries the
# app id and device; on iOS only a build number. Not every capture has one (a
# mid-session export starts after it), so every consumer must degrade.
#   Android: "║ TSLocationManager version: 4.4.2 (4070)"  + "╟─ app.id" + "╟─ vendor MODEL @ API (fw)"
#   iOS:     "║ TSLocationManager (build 388)"
_LAUNCH_RE = re.compile(
    r"TSLocationManager (?:version:\s*(?P<ver>[\d.]+)\s*\((?P<vbuild>\d+)\)"
    r"|\(build (?P<build>\d+)\))")
_DEVICE_ROW = re.compile(r"^╟─\s*(.+?@\s*\d+.*)$")
_APPID_ROW = re.compile(r"^╟─\s*([A-Za-z][\w.]*\.[\w.]+)\s*$")


# Did the process come up headless (no UI)? The marker sits NEXT TO the version
# banner, not inside it — Android logs "☯️ HeadlessMode? true" just before it,
# iOS logs "Booted in background" / didLaunchInBackground=1 just after. So the
# launch record has to look both ways.
_HEADLESS_LAUNCH = re.compile(
    r"HeadlessMode\?\s*(?P<hm>true|false)"
    r"|Booted in (?P<boot>background|foreground)"
    r"|didLaunchInBackground=(?P<dlb>[01])")
_HEADLESS_WINDOW_S = 15.0
_HEADLESS_WINDOW_RECS = 60


def _is_launch(rec: Record) -> bool:
    return bool(_LAUNCH_RE.search(rec.raw))


def _headless_launch(records: list[Record], idx: int) -> Optional[bool]:
    """True/False if a headless-launch marker sits near records[idx]; else None
    (the capture simply does not say — never guess)."""
    origin = records[idx].ts
    lo = max(0, idx - _HEADLESS_WINDOW_RECS)
    hi = min(len(records), idx + _HEADLESS_WINDOW_RECS + 1)
    # nearest-first so the closest marker to this launch wins
    for j in sorted(range(lo, hi), key=lambda j: abs(j - idx)):
        rec = records[j]
        if origin and rec.ts and abs((rec.ts - origin).total_seconds()) > _HEADLESS_WINDOW_S:
            continue
        m = _HEADLESS_LAUNCH.search(rec.raw)
        if not m:
            continue
        if m.group("hm"):
            return m.group("hm") == "true"
        if m.group("boot"):
            return m.group("boot") == "background"
        return m.group("dlb") == "1"
    return None


def launch_info(rec: Record) -> dict:
    """version / build / app id / device / config text from a launch banner.
    Every field is optional — iOS banners carry only a build number."""
    info: dict[str, Any] = {}
    m = _LAUNCH_RE.search(rec.raw)
    if m:
        if m.group("ver"):
            info["version"] = m.group("ver")
            info["build"] = m.group("vbuild")
        else:
            info["build"] = m.group("build")
    config: list[str] = []
    for line in rec.body:
        if not info.get("device"):
            d = _DEVICE_ROW.match(line)
            if d:
                info["device"] = d.group(1).strip()
                continue
        if not info.get("app_id"):
            a = _APPID_ROW.match(line)
            if a:
                info["app_id"] = a.group(1)
                continue
        # config dump: everything that isn't a box row
        if line[:1] not in ("╔", "╠", "╚", "║", "╟", ""):
            config.append(line)
    if config:
        info["config_text"] = "\n".join(config)
        # Android truncates the dump at ~4KB mid-key — say so rather than
        # letting a reader assume the config ends there.
        info["config_truncated"] = not config[-1].rstrip().endswith(("}", ";"))
    return info


# Authorization records are routine bookkeeping in the overwhelming majority:
# "Application became active - refreshing authorization state",
# "Desired policy changed to: Always", "status changed: 3 (state: HasAlways)",
# "Permission granted". None of that is a map event. Only a PROBLEM is —
# a denial, a restriction, a downgrade away from Always, or a request failure.
_AUTH_PROBLEM = re.compile(
    r"state:\s*(?:Denied|Restricted|NotDetermined|WhenInUse|Undetermined)"
    r"|[Aa]uthorization (?:denied|restricted|failed|error)"
    r"|Permission denied"
    r"|denied by (?:the )?user"
    r"|(?:location|background) permission (?:denied|revoked)"
    r"|isAuthorizedForPolicy=0"
    r"|kCLErrorDenied")
_AUTH_ROUTINE_CLASS = ("TSLocationAuthorization", "LocationAuthorization",
                       "PermissionManager")


# The stationary geofence is 150 m — always. The `stationaryRadius` config
# value is a DIFFERENT thing (it drives the "still within stationaryRadius"
# proximity arbitration, e.g. 25 m), so it must never be drawn as the fence.
DEFAULT_STATIONARY_RADIUS_M = 150.0
_STATIONARY_FENCE_RADIUS = re.compile(
    # Bounded newline-crossing: the radius sits on a later ╟─ row of the same
    # banner record, so the window has to span lines (but stay tight).
    r"stationary\s*(?:geofence|region)[\s\S]{0,160}?radius=([\d.]+)m", re.I)


def resolve_stationary_radius(records: list[Record]) -> tuple[float, str]:
    """-> (radius_m, source). 150 m unless the log states the fence's own radius."""
    for rec in records:
        m = _STATIONARY_FENCE_RADIUS.search(rec.raw)
        if m:
            try:
                return float(m.group(1)), "log"
            except ValueError:
                continue
    return DEFAULT_STATIONARY_RADIUS_M, "default (150 m)"


# Icon/colour for a record, resolved from vocabulary/map-rules.yaml. One
# ordered, platform-scoped rule list replaces what used to be several
# hardcoded tuples of regexes here.
def _rule_for(rec: Record, layer: str):
    return _RULES.match(rec.platform, layer, rec.raw)


def _is_auth_noise(rec: Record) -> bool:
    """True for routine authorization bookkeeping that should NOT be mapped."""
    if (rec.tag_class or "") not in _AUTH_ROUTINE_CLASS:
        return False
    return not _AUTH_PROBLEM.search(rec.raw)


def _is_lifecycle(rec: Record) -> bool:
    if _is_auth_noise(rec):
        return False
    tc = rec.tag_class or ""
    if tc in ("TSAppState", "LifecycleManager"):
        return True
    raw = rec.raw
    return ("☯" in raw or "💀" in raw or "onWindowFocusChanged" in raw
            or "onEnterForeground" in raw or "onEnterBackground" in raw
            or "applicationDidBecomeActive" in raw
            or "applicationWillTerminate" in raw)


def _is_geofence(rec: Record) -> bool:
    gf = rec.structs.get("geofence")
    if isinstance(gf, dict) and gf.get("action"):
        return True
    raw = rec.raw
    if GF_DIAGNOSTIC.search(raw):
        return True
    if rec.platform == IOS and rec.tag_class == "TSGeofenceManager":
        tm = rec.tag_method or ""
        if tm.startswith(("locationManager:didEnterRegion",
                          "locationManager:didExitRegion")):
            return True
    if "eofenc" in raw and _GF_TRANSITION.search(raw):
        return True
    return False


# Internal timer bookkeeping — arming, resetting and cancelling the
# motion-trigger — is not a pace change and does not belong on a map: it was
# 20 of 32 motion markers in one fixture, burying the 12 real transitions.
_MOTION_TIMER_NOISE = re.compile(
    r"Motion-trigger timer|startMotionTriggerTimer|resetMotionTriggerTimer"
    r"|MOTION_TRIGGER_DELAY|Motion-trigger ignored")


def _is_motionchange(rec: Record) -> bool:
    raw = rec.raw
    return ("changePace" in raw
            or "motionchange: true" in raw or "motionchange: false" in raw
            or "Acquired motionchange position" in raw
            or "Exit stationary region" in raw
            or "stopTimeout fired" in raw
            or "OneShot event fired: STOP_TIMEOUT" in raw)


_MC_TRUE = ("isMoving: true", "isMoving: 1", "motionchange: true")
_MC_FALSE = ("isMoving: false", "isMoving: 0", "motionchange: false")


def _motion_direction(rec: Record) -> Optional[bool]:
    """True = entered moving, False = entered stationary, None = not a pace
    transition (timers, stop-detection chatter). Earliest token in the record
    wins when both appear."""
    raw = rec.raw
    best: tuple[int, bool] | None = None
    for tokens, value in ((_MC_TRUE, True), (_MC_FALSE, False)):
        for tok in tokens:
            i = raw.find(tok)
            if i != -1 and (best is None or i < best[0]):
                best = (i, value)
    return best[1] if best else None


def _is_http(rec: Record) -> bool:
    """Only a flush's OUTCOME (or a failure) belongs on the map.

    A flush logs ~5 records; mapping each puts three identical cloud markers on
    one spot when the single useful fact is "did it succeed". Outcome records:
    iOS `finish:error: success=N`, Android `[HttpService$HttpCallback onResponse]
    Response: N`. Watchdog force-unlocks and non-2xx statuses also qualify —
    those ARE the finding.
    """
    raw = rec.raw
    if _HTTP_FINISH.search(raw):
        return True                                   # iOS flush outcome
    if "onResponse" in (rec.tag_method or "") and _HTTP_RESPONSE.search(raw):
        return True                                   # Android flush outcome
    h = rec.structs.get("http")
    if isinstance(h, dict):
        st = h.get("status")
        if isinstance(st, int) and not (200 <= st < 300):
            return True                               # a failure is always an event
        if "error" in h:
            return True
    return bool(_WATCHDOG_FIRE.search(raw))


# Capture-level facts that some suppressions depend on ("these stop-detection
# warnings are only noise because this capture is a mock route"). Computed once
# per log and PASSED DOWN — never module state, so _route stays a pure function
# of its arguments and cannot inherit whatever the previous capture left behind.
def _build_context(records: list[Record]) -> dict:
    mock = any(r.structs.get("location", {}).get("mock") for r in records
               if isinstance(r.structs.get("location"), dict)) or \
        any("Mock location detected" in r.raw for r in records[:4000])
    return {"mock_locations": mock}


def _route(rec: Record, context: dict) -> list[str]:
    # vocabulary/map-rules.yaml decides what is not worth mapping, and why
    if _RULES.suppressed(rec.platform, rec.raw, rec.tag_class or "", context):
        return []
    if (rec.tag_class or "") in _AUTH_ROUTINE_CLASS:
        # not suppressed above => this auth record describes a PROBLEM
        return ["errors"] if rec.severity == "error" else ["warnings"]
    out: list[str] = []
    sev = rec.severity
    # A suppressed geofence transition is a WARN record, but its geofence
    # marker already carries the severity and far better detail — routing it to
    # `warnings` too would put two markers on one event at one spot.
    suppressed_gf = bool(_GF_SUPPRESSED.search(rec.raw))
    if sev == "error":
        out.append("errors")
    elif sev == "warning" and not suppressed_gf:
        out.append("warnings")
    fr = rec.structs.get("filter_result")
    if isinstance(fr, dict):
        decision = str(fr.get("decision", "")).upper()
        if "REJECT" in decision or fr.get("anomaly") is True:
            out.append("rejections")
    ev = _event_name(rec)
    if ev in _REDUNDANT_EVENTS:
        return []          # the transition/flush itself is already on the map
    ev_layer = _EVENT_LAYER.get(ev or "")
    if ev_layer:
        out.append(ev_layer)
    elif _is_lifecycle(rec):
        out.append("lifecycle")
    if _is_geofence(rec):
        out.append("geofence")
    if _is_motionchange(rec):
        out.append("motionchange")
    if _is_http(rec):
        out.append("http")
    if "🐞" in rec.raw and "location" not in rec.structs:
        out.append("mock")
    return out


# ── track + gaps ─────────────────────────────────────────────────────────────

def _gap_bounds(gaps: list) -> list[tuple[datetime, datetime]]:
    out = []
    for g in gaps:
        s, e = parse_iso(field(g, "start_ts")), parse_iso(field(g, "end_ts"))
        if s and e:
            out.append((s, e))
    return out


def _track_features(fixes: list[Fix], gaps: list) -> list[dict]:
    timed = sorted((f for f in fixes if f.t is not None), key=lambda f: f.t)
    if len(timed) < 2:
        return []
    bounds = _gap_bounds(gaps)

    runs: list[list[Fix]] = []
    cur: list[Fix] = []
    last_t: Optional[datetime] = None
    for fx in timed:
        if last_t is not None:
            dt = (fx.t - last_t).total_seconds()          # type: ignore[operator]
            crosses = any(last_t <= s < fx.t for s, _ in bounds)  # type: ignore[operator]
            if (dt >= GAP_THRESHOLD_S or crosses) and cur:
                runs.append(cur)
                cur = []
        last_t = fx.t
        if cur and cur[-1].lon == fx.lon and cur[-1].lat == fx.lat:
            continue                                      # collapse duplicate vertex
        cur.append(fx)
    if cur:
        runs.append(cur)

    features: list[dict] = []
    for run in runs:
        if len(run) < 2:
            continue
        first, last = run[0], run[-1]
        coords = [[round(f.lon, 7), round(f.lat, 7)] for f in run]
        speeds = [None if not isinstance(f.loc.get("speed"), (int, float))
                  else round(float(f.loc["speed"]), 2) for f in run]
        # Per-vertex seconds from this segment's own start. A segment can span
        # hours, so the map's time window has to CLIP the polyline, not just
        # show or hide it whole; without per-vertex times it would have to
        # assume the device moved at a constant rate, which is exactly wrong
        # for the long stationary stretches these captures are full of.
        vt = [int((f.t - first.t).total_seconds()) if (f.t and first.t) else 0
              for f in run]
        dur_min = ((last.t - first.t).total_seconds() / 60
                   if first.t and last.t else None)
        props: dict[str, Any] = {
            "role": "track",
            "ts": iso(first.t),
            "end_ts": iso(last.t),
            "n": len(run),
            "speeds": speeds,
            "vt": vt,
            "category": "track",
            "severity": "normal",
            "glyph": _RULES.layer_glyph("track"),
            "popup": (f"Track segment: {len(run)} fixes\n"
                      f"{iso(first.t)} → {iso(last.t)}"
                      + (f"  ({dur_min:.1f} min)" if dur_min is not None else "")),
            "slice_ts": iso(first.t) or first.rec.ts_raw,
            "placement": "placed",
            "dt_s": 0.0,
        }
        features.append(_feature("LineString", coords, props))
    return features


def _gap_features(gaps: list, index: _FixIndex) -> list[dict]:
    features: list[dict] = []
    for g in gaps:
        s, e = parse_iso(field(g, "start_ts")), parse_iso(field(g, "end_ts"))
        if s is None or e is None or index.empty:
            continue
        cls = str(field(g, "classification", "gap"))
        dur = float(field(g, "duration_s", 0.0) or 0.0)
        anchor = index.last_at_or_before(s)
        after = index.first_at_or_after(e)
        pos = anchor or after
        if pos is None:
            continue
        props: dict[str, Any] = {
            "role": "gap",
            "ts": iso(s),
            "end_ts": iso(e),
            "classification": cls,
            "duration_s": round(dur, 1),
            "dt_minutes": round(dur / 60, 1),
            "category": "gaps",
            "severity": "warning" if cls == "wedge-candidate" else "normal",
            "glyph": _RULES.layer_glyph("gaps"),
            "popup": (f"GAP — {cls}: {dur / 60:.1f} min of silence\n"
                      f"{iso(s)} → {iso(e)}"),
            "slice_ts": iso(s),
            "placement": "placed",
            "dt_s": 0.0,
        }
        features.append(_feature("Point", [round(pos[0], 7), round(pos[1], 7)], props))
        if anchor and after and anchor != after:
            span_props = dict(props)
            span_props["role"] = "gap-span"
            features.append(_feature(
                "LineString",
                [[round(anchor[0], 7), round(anchor[1], 7)],
                 [round(after[0], 7), round(after[1], 7)]],
                span_props))
    return features


# ── entry point ──────────────────────────────────────────────────────────────

def _fix_feature(fx: Fix, layer: str) -> dict:
    props = _base_props(fx.rec, layer, "placed", 0.0)
    props["role"] = "fix" if layer == "fixes" else "mock-fix"
    props["glyph"] = "🐞" if fx.loc.get("mock") else props["glyph"]
    for key in ("acc", "speed", "course", "alt", "provider", "batch_index"):
        v = fx.loc.get(key)
        if v is not None:
            props[key] = v
    props["mock"] = bool(fx.loc.get("mock"))
    props["own_position"] = True
    return _feature("Point", [round(fx.lon, 7), round(fx.lat, 7)], props)


def _launch_features(records: list[Record], index: _FixIndex) -> list[dict]:
    """App-process launch banners, anchored to the nearest fix.

    A launch has no position of its own, so putting it on the route would
    assert a location the log never recorded. The geometry is the ANCHOR fix
    and the map draws the glyph offset in screen space with a hairline back
    to it — the offset-marker contract, reusable by any such marker.
    """
    out: list[dict] = []
    for idx, rec in enumerate(records):
        if not _is_launch(rec) or rec.ts is None:
            continue
        anchor = index.nearest(rec.ts)
        if anchor is None:
            continue
        lon, lat, anchor_dt = anchor
        props = _base_props(rec, "launch", "placed", abs(anchor_dt))
        props.update(launch_info(rec))
        props["role"] = "launch"
        # Offset-marker contract (reusable by any marker with no location of its
        # own): the geometry is the ANCHOR fix; the map draws the glyph offset in
        # screen space with a pointer back to it.
        props["offset_marker"] = True
        props["offset_clock"] = OFFSET_CLOCK.get("launch", DEFAULT_OFFSET_CLOCK)
        props["anchor_dt_s"] = round(anchor_dt, 1)
        props["anchor_dir"] = "before" if anchor_dt < 0 else "after"
        headless = _headless_launch(records, idx)
        props["headless_launch"] = headless
        props["glyph"] = "⚙️"
        props["category"] = ("app launch — headless" if headless
                             else "app launch" if headless is False
                             else "app launch")
        out.append(
            _feature("Point", [round(lon, 7), round(lat, 7)], props))
    return out


def _collapse_duplicates(records: list[Record]) -> tuple[set[int], dict[int, dict]]:
    """One event, one marker.

    A single HTTP flush logs ~5 records, a geofence episode ~4, and every
    dispatch has a matching receipt. Each pass returns the record seqs to skip
    plus the facts worth folding into the surviving marker.
    -> (seqs to suppress, seq -> extra properties)
    """
    suppress, delivery = _collapse_event_pairs(records)
    gf_suppress, gf_info = _collapse_geofence(records)
    suppress |= gf_suppress
    for seq, info in gf_info.items():
        delivery.setdefault(seq, {}).update(info)
    http_suppress, http_info = _collapse_http(records)
    suppress |= http_suppress
    for seq, info in http_info.items():
        delivery.setdefault(seq, {}).update(info)
    return suppress, delivery


def _stamp_headless_locations(records: list[Record], fixes: list[Fix],
                              fix_feats: list[dict], suppress: set[int]) -> None:
    """Fold `location` event dispatches into the fix they describe.

    Mutates `fix_feats` (stamping delivery onto the chevron) and `suppress`.
    """
    # A `location` event dispatch describes a fix the map ALREADY draws as a
    # chevron — a second 📍 marker on top of it says nothing new. Suppress the
    # marker and stamp the fix instead, so clicking the chevron reveals that
    # this location was delivered headless.
    loc_dispatches: list[tuple[datetime, bool]] = []
    for rec in records:
        if rec.seq in suppress or rec.ts is None:
            continue
        if _event_name(rec) == "location":
            loc_dispatches.append((rec.ts, "💀" in rec.raw))
            suppress.add(rec.seq)
    loc_dispatches.sort()
    if loc_dispatches:
        dts = [d[0] for d in loc_dispatches]
        for fx, feat in zip(fixes, fix_feats):
            if fx.t is None:
                continue
            i = bisect_left(dts, fx.t)
            for j in (i - 1, i):
                if 0 <= j < len(dts) and abs((dts[j] - fx.t).total_seconds()) <= 2.0:
                    if loc_dispatches[j][1]:
                        feat["properties"]["headless"] = True
                        feat["properties"]["event"] = "location"
                    break


def _event_features(records: list[Record], index: _FixIndex, suppress: set[int],
                    delivery: dict[int, dict], context: dict,
                    stationary_radius: float, radius_src: str
                    ) -> tuple[dict[str, list[dict]], list[tuple]]:
    """Every non-fix record that earns a marker, routed to its layer(s).

    -> ({layer: features}, motionchange events for the stop->moving connectors)
    """
    feats: dict[str, list[dict]] = {name: [] for name in LAYER_ORDER}
    mc_events: list[tuple[datetime, float, float, bool, Record]] = []
    for rec in records:
        if rec.seq in suppress:
            continue                      # duplicate log line of an event already mapped
        layers = _route(rec, context)
        if not layers:
            continue
        loc = rec.structs.get("location")
        if isinstance(loc, dict) and isinstance(loc.get("lat"), (int, float)) \
                and isinstance(loc.get("lon"), (int, float)):
            lon, lat = float(loc["lon"]), float(loc["lat"])
            placement, dt_s = "placed", 0.0
            own_position = True
        else:
            if rec.ts is None:
                continue
            located = index.locate(rec.ts)
            if located is None:
                continue                     # no fixes at all: nothing to anchor to
            lon, lat, placement, dt_s = located
            own_position = False
        for layer in layers:
            props = _base_props(rec, layer, placement, dt_s)
            props["role"] = "event"
            # True = the record carried these coordinates itself, so the marker
            # is at a real place and must NEVER be moved for legibility.
            props["own_position"] = own_position
            if not own_position:
                props["offset_marker"] = True
                props["offset_clock"] = OFFSET_CLOCK.get(layer, DEFAULT_OFFSET_CLOCK)
            props.update(delivery.get(rec.seq, {}))
            override_pos = None
            if layer == "geofence":
                # A transition the SDK DECLINED to deliver is its own kind of
                # event — "the fence didn't fire, and here is why" — so it gets
                # a distinct glyph rather than looking like a normal trigger.
                sup = _GF_SUPPRESSED.search(rec.raw)
                if sup:
                    # vector icon — no emoji means "stationary exit suppressed"
                    props["icon_name"] = "geofence-suppressed"
                    props["glyph"] = "🚫"          # text fallback (digest/geojson)
                    props["suppressed"] = sup.group(0)
                    props["category"] = "geofence transition suppressed"
                    props["severity"] = rec.severity     # keep WARN styling here
                    labelled = _labelled_locations(rec)
                    for label, loc in labelled.items():
                        props[f"{label}_lat"] = loc.get("lat")
                        props[f"{label}_lon"] = loc.get("lon")
                        props[f"{label}_acc"] = loc.get("acc")
                    # The trigger fix is what claimed the transition — put the
                    # marker there, not on the stationary anchor. Position is
                    # applied AFTER the metric extraction below.
                    trig = labelled.get("trigger")
                    if trig and isinstance(trig.get("lat"), (int, float)):
                        override_pos = (round(float(trig["lon"]), 7),
                                        round(float(trig["lat"]), 7))
                        # The leg the track WOULD have taken had this exit been
                        # accepted: last known position -> trigger fix. Drawn in
                        # the track's own blue so the implausible jump reads at
                        # a glance against the real route.
                        anchor_loc = (labelled.get("stationary")
                                      or labelled.get("last")
                                      or labelled.get("b"))
                        from_pt = None
                        if anchor_loc and isinstance(anchor_loc.get("lat"), (int, float)):
                            from_pt = (round(float(anchor_loc["lon"]), 7),
                                       round(float(anchor_loc["lat"]), 7))
                        elif rec.ts is not None:
                            prev = index.last_at_or_before(rec.ts)
                            if prev:
                                from_pt = (round(prev[0], 7), round(prev[1], 7))
                        if from_pt and from_pt != override_pos:
                            feats[layer].append(_feature(
                                "LineString", [list(from_pt), list(override_pos)],
                                {"role": "suppressed-exit-path", "layer": layer,
                                 "seq": rec.seq, "ts": iso(rec.ts),
                                 "category": "suppressed geofence exit — implied leg",
                                 "severity": "warning", "glyph": "🚷",
                                 "slice_ts": iso(rec.ts) or rec.ts_raw,
                                 "placement": "placed", "dt_s": 0.0,
                                 "own_position": True,
                                 "popup": (f"{sup.group(0)}\n"
                                           f"leg the track would have taken: "
                                           f"last known → trigger fix")}))
                gf = rec.structs.get("geofence")
                if isinstance(gf, dict):
                    action = (gf.get("action") or "").upper()
                    props["action"] = gf.get("action")
                    props["identifier"] = gf.get("identifier")
                    props.setdefault("icon_name", "geofence")
                    # ENTER/EXIT/DWELL is the whole story of a transition, so it
                    # picks the colour: entered = green, left = red, dwelling
                    # = amber. Reads without opening the popup.
                    tint = {"ENTER": "green", "EXIT": "red", "DWELL": "amber"}.get(action)
                    if tint:
                        props["tint"] = tint
                # dist/radius/outsideBy is the evidence that a trigger was
                # spurious: a fix N metres outside a fence of radius R.
                for key, pat in (("dist_m", r"dist(?:ance)?=([\d.]+)m"),
                                 ("radius_m", r"radius=([\d.]+)m"),
                                 ("outside_by_m", r"outsideBy=([\d.]+)"),
                                 ("min_possible_m", r"minPossibleDist(?:ance)?=([\d.]+)"),
                                 # the banner's own `accuracy=` is the TRIGGER's,
                                 # unlike a bare hAcc= which may be the anchor's
                                 ("trigger_acc_m", r"accuracy=([\d.]+)m")):
                    mm = re.search(pat, rec.raw)
                    if mm:
                        props[key] = float(mm.group(1))
                if props.get("dist_m") and props.get("radius_m"):
                    props["verdict"] = (
                        "outside fence" if props["dist_m"] > props["radius_m"]
                        else "inside fence")
            if layer == "motionchange":
                # The region belongs to the state ENTRY only. stopTimeout fires
                # ~3 ms earlier at the same spot; drawing a circle there too
                # just stacks a second identical ring on the first.
                rule = _rule_for(rec, layer)
                if rule and rule.stationary_region:
                    props["stationary_radius_m"] = stationary_radius
                    props["stationary_radius_src"] = radius_src
                moving = _motion_direction(rec)
                if moving is not None:
                    props["moving"] = moving
                    state = "MOVING" if moving else "STATIONARY"
                    props["popup"] = (f"⚡ motionchange — SDK entered {state} "
                                      f"(isMoving: {str(moving).lower()})\n" + props["popup"])
                    if moving is False:
                        props["marker"] = "stationary"   # demo-app red circle
                    if rec.ts is not None:
                        mc_events.append((rec.ts, lon, lat, moving, rec))
            pos = override_pos or (round(lon, 7), round(lat, 7))
            feats[layer].append(_feature("Point", list(pos), props))
    return feats, mc_events


def _motion_connectors(mc_events: list[tuple]) -> list[dict]:
    """Demo-app parity: a green LineString from each stationary point to where
    the next motionchange fired."""
    out: list[dict] = []
    # Green stop→moving connectors (demo-app parity): a LineString from each
    # stationary point to the location where the next motionchange fired.
    mc_events.sort(key=lambda e: e[0])
    last_stop: tuple[datetime, float, float] | None = None
    for ts, lon, lat, moving, rec in mc_events:
        if moving is False:
            last_stop = (ts, lon, lat)
        elif last_stop is not None:
            s_ts, s_lon, s_lat = last_stop
            if (round(s_lon, 7), round(s_lat, 7)) != (round(lon, 7), round(lat, 7)):
                out.append(_feature(
                    "LineString",
                    [[round(s_lon, 7), round(s_lat, 7)], [round(lon, 7), round(lat, 7)]],
                    {"role": "motionchange-path", "layer": "motionchange",
                     "seq": rec.seq, "ts": iso(ts), "from_ts": iso(s_ts),
                     "popup": f"motionchange: stationary → moving\nstopped {iso(s_ts)} → moving {iso(ts)}",
                     "glyph": "⚡", "category": "motion", "severity": "normal",
                     "slice_ts": iso(rec.ts) or rec.ts_raw,
                     "placement": "placed", "dt_s": 0.0}))
            last_stop = None
    return out


def build_layers(analysis: Any, records: list[Record]) -> dict[str, dict]:
    """-> {layer_name: GeoJSON FeatureCollection}; empty layers omitted.

    Orchestration only. Each phase below is its own function, and the
    capture-level `context` is threaded through rather than held in module
    state, so nothing here depends on what a previous call left behind.
    """
    if records and not any(r.structs for r in records):
        for rec in records:                 # caller skipped Stage 2 — fallback
            annotate(rec)

    context = _build_context(records)
    fixes = collect_fixes(records)
    index = _FixIndex(fixes)
    gaps = gap_list(analysis)

    feats: dict[str, list[dict]] = {name: [] for name in LAYER_ORDER}
    # A mock fix is a fix: it is flagged (`mock: true`) and drawn magenta in the
    # fixes layer. Emitting a duplicate into a `mock` layer doubled the geometry
    # of a mock-route capture (2,151 redundant features) without adding anything.
    feats["fixes"] = [_fix_feature(fx, "fixes") for fx in fixes]
    feats["track"] = _track_features(fixes, gaps)
    feats["gaps"] = _gap_features(gaps, index)
    feats["launch"] = _launch_features(records, index)

    suppress, delivery = _collapse_duplicates(records)
    _stamp_headless_locations(records, fixes, feats["fixes"], suppress)

    events, mc_events = _event_features(records, index, suppress, delivery,
                                        context, *resolve_stationary_radius(records))
    for name, fs in events.items():
        feats[name].extend(fs)
    feats["motionchange"].extend(_motion_connectors(mc_events))

    deduped = {name: _compact(_dedupe_markers(f), name) for name, f in feats.items()}
    return {name: {"type": "FeatureCollection", "features": f}
            for name, f in deduped.items() if f}

