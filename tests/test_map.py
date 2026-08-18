"""Tests for emit/geojson.py + emit/map.py.

Two layers:
1. Real-fixture tests over the iOS car capture (geofence EXITs + warnings,
   two suspension gaps) asserting the layer split, the 120-second
   placed/tethered honesty rule against an INDEPENDENT nearest-fix
   computation, and the self-contained-HTML guarantee.
2. Synthetic boundary tests pinning the rule edge (120 s placed, 121 s
   tethered to the last known fix).
"""
from __future__ import annotations

import dataclasses
import re
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fixtures import fixture

from loganalyzer import structs
from loganalyzer.analyze import analyze
from loganalyzer.emit.geojson import (PLACED_MAX_DT_S, LAYER_ORDER,
                                      build_layers, compact_default)
from loganalyzer.emit.map import render_map
from loganalyzer.model import IOS, Record
from loganalyzer.records import assemble
from loganalyzer.segments import split_segments
from loganalyzer.sniff import load_sources

CAR = fixture("car")
needs_car = pytest.mark.skipif(CAR is None, reason="car.log fixture not present")

def prop(feature, key, layer=None):
    """Read a feature property, re-applying compaction defaults from the SAME
    table _compact() drops them with and the renderer inflates them from — so
    this helper cannot assert a contract the map does not actually implement."""
    props = feature["properties"]
    if key in props:
        return props[key]
    return compact_default(props, key, layer)


EVENT_LAYERS = ("rejections", "lifecycle", "errors", "warnings",
                "geofence", "motionchange", "http", "mock")
PINNED_PROPS = ("ts", "category", "severity", "glyph", "popup",
                "slice_ts", "placement", "dt_s")


@pytest.fixture(scope="module")
def car():
    sources = load_sources([CAR])
    src = sources[0]
    assert src.platform == IOS      # iOS despite the filename (grammar-sniffed)
    records = assemble(src.platform, src.text, 2026)
    for rec in records:
        structs.annotate(rec)
    segments = split_segments(records, {})
    analysis = analyze(records, segments, sources)
    layers = build_layers(analysis, records)
    return records, analysis, layers


# ── real fixture: layer inventory ────────────────────────────────────────────

@needs_car
def test_car_layer_inventory(car):
    _, _, layers = car
    assert set(layers) == {"track", "fixes", "launch", "lifecycle", "warnings",
                           "geofence", "motionchange", "http", "gaps"}
    # zero-feature layers are omitted, never emitted empty
    for name, fc in layers.items():
        assert fc["type"] == "FeatureCollection"
        assert fc["features"], f"layer {name} emitted empty"
    # 8 warning records collapse to 7 markers: two identical warnings share an
    # anchor and merge into one petal carrying count=2 (no record is lost).
    assert len(layers["warnings"]["features"]) == 7
    assert sum(f["properties"].get("count", 1)
               for f in layers["warnings"]["features"]) == 8
    assert len(layers["geofence"]["features"]) == 5
    # one version banner in this capture => one app-launch marker
    assert len(layers["launch"]["features"]) == 1
    assert len(layers["fixes"]["features"]) == 144
    # the iOS car capture has no filter rejections and no error records
    assert "rejections" not in layers
    assert "errors" not in layers


@needs_car
def test_car_feature_props_complete(car):
    _, _, layers = car
    for name, fc in layers.items():
        for f in fc["features"]:
            p = {**f["properties"], "placement": prop(f, "placement"),
                 "dt_s": prop(f, "dt_s")}
            for key in PINNED_PROPS:
                # compaction omits default values; prop() applies the same
                # defaults the renderer does, so the CONTRACT is what is pinned
                assert prop(f, key, layer=name) is not None, \
                    f"{name} feature missing {key}"
            assert p["placement"] in ("placed", "tethered")
            assert len(p["popup"]) <= 400
            if p["placement"] == "tethered":
                assert p["dt_minutes"] == round(p["dt_s"] / 60, 1)
                assert p["dt_s"] > PLACED_MAX_DT_S
            g = f["geometry"]
            coords = g["coordinates"] if g["type"] == "Point" else g["coordinates"][0]
            lon, lat = coords[0], coords[1]
            assert -180 <= lon <= 180 and -90 <= lat <= 90


# ── the 120-second placed/tethered honesty rule ──────────────────────────────

@needs_car
def test_car_placed_tethered_120s_rule(car):
    records, _, layers = car
    by_seq = {r.seq: r for r in records}
    # INDEPENDENT nearest-fix index built straight from the structs annotations
    fix_ts = sorted(r.ts for r in records if r.ts and "location" in r.structs)
    assert fix_ts

    def min_dt(t: datetime) -> float:
        i = bisect_left(fix_ts, t)
        c = []
        if i > 0:
            c.append((t - fix_ts[i - 1]).total_seconds())
        if i < len(fix_ts):
            c.append((fix_ts[i] - t).total_seconds())
        return min(c)

    checked = placed = tethered = 0
    for name in EVENT_LAYERS:
        if name not in layers:
            continue
        for f in layers[name]["features"]:
            p = {**f["properties"], "placement": prop(f, "placement"), "dt_s": prop(f, "dt_s")}
            rec = by_seq[p["seq"]]
            p = {**p, "placement": prop(f, "placement"), "dt_s": prop(f, "dt_s")}
            if "location" in rec.structs:
                # events that carry their own fix are placed with dt 0
                assert p["placement"] == "placed" and p["dt_s"] == 0.0
                continue
            dt = min_dt(rec.ts)
            assert (dt <= PLACED_MAX_DT_S) == (p["placement"] == "placed"), \
                f"{name} seq={p['seq']}: nearest-fix dt={dt:.1f}s but " \
                f"placement={p['placement']}"
            if p["placement"] == "placed":
                assert abs(p["dt_s"] - dt) <= 0.15
                placed += 1
            else:
                # tether anchors to the LAST KNOWN fix — its dt is >= nearest
                assert p["dt_s"] >= dt - 0.15
                tethered += 1
            checked += 1
    assert checked > 50
    assert placed and tethered, "fixture should exercise BOTH placements"
    # empirically pinned: 4 placed + 4 tethered warning RECORDS; after dedupe
    # two tethered ones merge, so the markers are 4 placed + 3 tethered.
    warn = [prop(f, "placement") for f in layers["warnings"]["features"]]
    assert warn.count("placed") == 4
    assert warn.count("tethered") == 3


# ── geofence / track / gaps ──────────────────────────────────────────────────

@needs_car
def test_car_geofence_exit_placed(car):
    _, _, layers = car
    exits = [f for f in layers["geofence"]["features"]
             if "EXIT" in f["properties"]["popup"]]
    assert exits, "geofence EXIT features not found"
    assert any("Test" in f["properties"]["popup"] for f in exits)
    # the EXIT burst happens mid-tracking: fixes are milliseconds away
    assert all(prop(f, "placement") == "placed" for f in exits)


@needs_car
def test_car_track_split_at_gaps(car):
    _, analysis, layers = car
    assert [g.classification for g in analysis.timeline.gaps] == \
        ["suspension", "suspension"]
    tracks = layers["track"]["features"]
    assert len(tracks) == 2          # split at the classified suspension gaps
    for t in tracks:
        assert t["geometry"]["type"] == "LineString"
        coords = t["geometry"]["coordinates"]
        assert len(coords) >= 2
        assert len(t["properties"]["speeds"]) == len(coords)
        assert t["properties"]["n"] == len(coords)

    gap_feats = layers["gaps"]["features"]
    points = [f for f in gap_feats if f["properties"]["role"] == "gap"]
    spans = [f for f in gap_feats if f["properties"]["role"] == "gap-span"]
    assert len(points) == 2 and len(spans) == 2
    for f in points:
        assert f["properties"]["classification"] == "suspension"
        assert f["properties"]["duration_s"] >= 900


# ── self-contained HTML ──────────────────────────────────────────────────────

@needs_car
def test_render_map_self_contained(car):
    _, _, layers = car
    html = render_map(layers, "car triage map")
    assert "<script src" not in html
    assert "<link" not in html
    assert "@import" not in html
    # The only thing the page ever FETCHES is the OSM raster tile template.
    # URLs may legitimately appear as data (a launch popup embeds the TSConfig
    # dump, which contains the customer's own `url`), so assert on resource
    # loads rather than on the presence of a URL string.
    assert not re.search(r"<(?:script|link|img|iframe|source|video|audio)\b[^>]*"
                         r"\b(?:src|href)\s*=", html)
    assert not re.search(r"url\(\s*['\"]?https?://", html)     # no CSS-loaded assets
    assert "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in html
    assert not re.search(r"\b(?:fetch|XMLHttpRequest|WebSocket|importScripts)\s*\(", html)
    assert "car triage map" in html
    assert "accuracy circles" in html            # fixes sub-toggle (off by default)
    assert "acb.checked = false" in html
    for name in layers:
        assert f'"{name}"' in html


@needs_car
def test_render_map_escapes_title(car):
    _, _, layers = car
    html = render_map(layers, 'x</script><b>"y"</b>')
    assert "</script><b>" not in html


# ── synthetic: the 120-second boundary + dict-round-trip analysis ────────────

def _rec(seq: int, ts: datetime, raw: str, icon: str | None = None) -> Record:
    return Record(platform=IOS, seq=seq, line_no=seq + 1, ts=ts,
                  ts_raw=ts.isoformat(sep=" ", timespec="milliseconds"),
                  level=None, icon=icon, tag_class="TSTest",
                  tag_method_raw="test", tag_method="test",
                  header_msg=raw, raw=raw)


def _synthetic():
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    fix = _rec(0, t0, "fix record")
    fix.structs["location"] = {"lat": 45.5, "lon": -73.6, "acc": 5.0, "speed": 1.0}
    fix2 = _rec(1, t0 + timedelta(seconds=10), "fix record 2")
    fix2.structs["location"] = {"lat": 45.6, "lon": -73.7, "acc": 5.0, "speed": 2.0}
    on_boundary = _rec(2, t0 + timedelta(seconds=130), "boundary warn", icon="⚠")
    beyond = _rec(3, t0 + timedelta(seconds=131.4), "beyond warn", icon="⚠")
    records = [fix, fix2, on_boundary, beyond]
    segments = split_segments(records, {})
    analysis = analyze(records, segments, [])
    return records, analysis


def test_synthetic_boundary_placed_vs_tethered():
    records, analysis = _synthetic()
    layers = build_layers(analysis, records)
    warns = {f["properties"]["seq"]: f for f in layers["warnings"]["features"]}
    # 120.0 s to the nearest fix — exactly on the boundary: placed
    at = warns[2]["properties"]
    assert prop(warns[2], "placement") == "placed"
    assert at["dt_s"] == 120.0
    assert "dt_minutes" not in at
    # 121.4 s: tethered to the LAST KNOWN fix, badge-ready dt_minutes
    over = warns[3]["properties"]
    assert over["placement"] == "tethered"
    assert over["dt_s"] == 121.4
    assert over["dt_minutes"] == 2.0
    assert warns[3]["geometry"]["coordinates"] == [-73.7, 45.6]  # anchor = fix2


def test_synthetic_accepts_dict_analysis():
    records, analysis = _synthetic()
    as_dict = dataclasses.asdict(analysis)      # JSON round-trip shape
    layers = build_layers(as_dict, records)
    assert set(layers) == {"track", "fixes", "warnings"}
    assert len(layers["fixes"]["features"]) == 2


def test_layer_order_covers_all_emitted_layers():
    records, analysis = _synthetic()
    layers = build_layers(analysis, records)
    assert set(layers) <= set(LAYER_ORDER)


# ── geofence routing: real wordings + spatial diagnostics ────────────────────

def test_gf_transition_matches_real_wordings():
    """Regression: the original regex required 'Geofence <TRANSITION>', which
    neither platform actually emits — Android says 'Geofencing Event: EXIT' and
    iOS puts the verb first ('📢 EXIT Geofence: Test')."""
    from loganalyzer.emit.geojson import _GF_TRANSITION
    for s in ("║ Geofencing Event: EXIT",
              "📢 EXIT Geofence: Test",
              "📢 ENTER Geofence: home",
              "Geofence DWELL"):
        assert _GF_TRANSITION.search(s), s
    for s in ("geofences monitored: 13", "Geofence radius: 50.0"):
        assert not _GF_TRANSITION.search(s), s


def test_geofence_diagnostic_banner_routes_with_coords_and_verdict():
    """'Trigger vs Geofence center' carries the trigger fix AND its distance to
    the fence — the evidence a trigger was spurious. It must reach the map at
    real coordinates, not be time-tethered."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.segments import split_segments
    from loganalyzer.structs import annotate
    from loganalyzer.analyze import analyze
    from loganalyzer.emit.geojson import build_layers

    text = (
        "07-21 01:41:32.241 INFO [GeofencingProcessor a] \n"
        "╔═════════════════════════════════════════════\n"
        "║ Trigger vs Geofence center: waypoint:93cf4dd6\n"
        "╠═════════════════════════════════════════════\n"
        "╟─ 📍 Trigger=Location[fused 40.717000,-99.175178 hAcc=116.1 et=+4d23h34m10s315ms]\n"
        "╟─ dist=568.0191m radius=300.0m\n"
        "╟─ minPossibleDist=451.9191m (outsideBy=151.9191m)\n"
        "╚═════════════════════════════════════════════\n")
    recs = assemble(ANDROID, text, 2026)
    for r in recs:
        annotate(r)
    segs = split_segments(recs)
    feats = build_layers(analyze(recs, segs, []), recs)["geofence"]["features"]
    assert len(feats) == 1
    p = feats[0]["properties"]
    assert prop(feats[0], "placement") == "placed"
    assert feats[0]["geometry"]["coordinates"] == [-99.175178, 40.717]
    assert p["dist_m"] == 568.0191 and p["radius_m"] == 300.0
    assert p["outside_by_m"] == 151.9191 and p["verdict"] == "outside fence"


def test_headless_event_icon_is_event_type_not_skull():
    """💀 says WHEN an event fired (app headless), not WHAT fired. The icon must
    carry the event type; headless is surfaced as a popup modifier."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import _glyph, _category, _base_props, _event_name

    text = ("07-21 01:41:32.935 DEBUG [HeadlessEventTx fire] 🛜 💀⚡️ http\n"
            "07-21 01:41:33.000 DEBUG [EventManager fire]    🛜 ⚡️ location\n")
    recs = assemble(ANDROID, text, 2026)
    for r in recs:
        annotate(r)
    http_rec, loc_rec = recs[0], recs[1]

    assert _event_name(http_rec) == "http"
    assert _glyph(http_rec, "lifecycle") == "📶"        # not 💀
    assert _category(http_rec, "lifecycle") == "http event"
    p = _base_props(http_rec, "lifecycle", "placed", 0.0)
    assert p["event"] == "http" and p["headless"] is True

    assert _glyph(loc_rec, "lifecycle") == "📍"
    assert _base_props(loc_rec, "lifecycle", "placed", 0.0)["headless"] is False


def test_launch_marker_records_headless_state():
    """The headless marker sits NEXT TO the version banner, not inside it:
    Android logs it just before, iOS just after. Unknown must stay None — never
    guessed."""
    from loganalyzer.model import ANDROID, IOS
    from loganalyzer.records import assemble
    from loganalyzer.emit.geojson import _headless_launch, _is_launch

    android = assemble(ANDROID,
        "08-15 01:44:36.878 DEBUG [LoggerFacade$a a] ☯️  onCreate\n"
        "08-15 01:44:36.878 DEBUG [LoggerFacade$a a] \n"
        "╔═══\n║ ☯️  HeadlessMode? true\n╠═══\n"
        "08-15 01:44:36.879 INFO [LoggerFacade$a a] \n"
        "╔═══\n║ TSLocationManager version: 4.4.2 (4070)\n"
        "╟─ app.serendipity\n╟─ samsung SM-M556B @ 16 (react)\n", 2026)
    li = next(i for i, r in enumerate(android) if _is_launch(r))
    assert _headless_launch(android, li) is True

    ios = assemble(IOS,
        "2026-07-28 13:13:33.000 🔵 -[TSLocationManager init] \n"
        "╔═══\n║ TSLocationManager (build 388)\n╚═══\n\n"
        "2026-07-28 13:13:34.608 🔵 -[TSLocationManager ready]_block_invoke Booted in background\n\n",
        2026)
    li = next(i for i, r in enumerate(ios) if _is_launch(r))
    assert _headless_launch(ios, li) is True

    # no marker anywhere near => unknown, not False
    bare = assemble(ANDROID,
        "08-15 01:44:36.879 INFO [LoggerFacade$a a] \n"
        "╔═══\n║ TSLocationManager version: 4.4.2 (4070)\n", 2026)
    assert _headless_launch(bare, 0) is None


def test_headless_task_event_uses_event_glyph_not_skull():
    """`[HeadlessTask onHeadlessEvent] 💀 event: terminate` is a TERMINATE
    event that happened to fire headless — the marker must say terminate."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import _glyph, _category, _base_props, _event_name

    recs = assemble(ANDROID,
        "08-15 01:44:37.001 DEBUG [HeadlessTask onHeadlessEvent] 💀  event: terminate\n"
        "08-15 01:44:38.001 DEBUG [HeadlessTask onHeadlessEvent] 💀  event: location\n", 2026)
    for r in recs:
        annotate(r)
    term, loc = recs

    assert _event_name(term) == "terminate"
    assert _glyph(term, "lifecycle") == "🔚"          # not 💀
    assert _category(term, "lifecycle") == "terminate event"
    p = _base_props(term, "lifecycle", "placed", 0.0)
    assert p["event"] == "terminate" and p["headless"] is True

    assert _glyph(loc, "lifecycle") == "📍"


def test_event_dispatch_receipt_pairs_collapse_to_one_marker():
    """A headless event is logged twice — `HeadlessEventTx fire` (dispatch) and
    `HeadlessTask onHeadlessEvent` (receipt) ms apart. Map one marker per event,
    and treat a dispatch with no receipt as a finding, not a silent drop."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.emit.geojson import _collapse_event_pairs

    recs = assemble(ANDROID,
        "08-15 01:44:36.941 DEBUG [HeadlessEventTx fire] 🛜 💀⚡️ terminate\n"
        "08-15 01:44:37.001 DEBUG [HeadlessTask onHeadlessEvent] 💀  event: terminate\n"
        "08-15 01:45:00.000 DEBUG [HeadlessEventTx fire] 🛜 💀⚡️ location\n", 2026)
    suppress, delivery = _collapse_event_pairs(recs)

    assert suppress == {recs[1].seq}                      # receipt collapsed away
    assert delivery[recs[0].seq] == {"delivered": True, "delivery_ms": 60}
    assert delivery[recs[2].seq] == {"delivered": False}  # dispatched, never received


def test_offset_marker_anchors_to_nearest_fix_either_direction():
    """Offset markers (no location of their own) anchor to the closest fix in
    EITHER direction and record which way, so the pointer never implies the
    event happened at a fix that is minutes away in the wrong direction."""
    from datetime import datetime, timedelta
    from loganalyzer.emit.geojson import _FixIndex
    from loganalyzer.locations import Fix

    t0 = datetime(2026, 8, 15, 1, 0, 0)
    fixes = [Fix(t=t0, lon=-73.0, lat=45.0, rec=None, loc={}),
             Fix(t=t0 + timedelta(minutes=10), lon=-73.5, lat=45.5, rec=None, loc={})]
    idx = _FixIndex(fixes)

    lon, lat, dt = idx.nearest(t0 + timedelta(minutes=1))     # backwards is closer
    assert (lon, lat) == (-73.0, 45.0) and dt == -60.0
    lon, lat, dt = idx.nearest(t0 + timedelta(minutes=9))     # forwards is closer
    assert (lon, lat) == (-73.5, 45.5) and dt == 60.0
    assert idx.nearest(t0 - timedelta(hours=1))[2] == 3600.0  # only forward exists


def test_offset_clock_policy_is_per_type():
    """Each offset-marker type keeps a fixed bearing so position is meaningful."""
    from loganalyzer.emit.geojson import OFFSET_CLOCK
    assert OFFSET_CLOCK["launch"] == 12
    assert len(set(OFFSET_CLOCK.values())) == len(OFFSET_CLOCK)   # no two types collide


def test_petal_dedupe_collapses_repeats_and_keeps_count():
    """Repeats of the same event at one anchor collapse to a single petal with
    a count — nine identical ☯️ petals say nothing nine times. Nothing is lost:
    the marker records how many and when."""
    from loganalyzer.emit.geojson import _dedupe_markers, _dedupe_key

    def feat(ts, popup, ev=None, own=False):
        p = {"ts": ts, "popup": popup, "placement": "placed", "own_position": own}
        if ev:
            p["event"] = ev
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-73.6, 45.5]},
                "properties": p}

    feats = [
        feat("t1", "08-15 01:44:25 DEBUG [LifecycleManager d] onWindowFocusChanged: false"),
        feat("t2", "08-15 02:02:20 DEBUG [LifecycleManager d] onWindowFocusChanged: true"),
        feat("t3", "08-15 02:02:21 DEBUG [LifecycleManager d] onWindowFocusChanged: false"),
        feat("t4", "x", ev="http"),
        feat("t5", "y", ev="http"),
        feat("t6", "z", ev="terminate"),
    ]
    out = _dedupe_markers(feats)
    by = {_dedupe_key(f["properties"]): f["properties"] for f in out}
    assert len(out) == 3                                  # focus-chatter, http, terminate
    focus = next(v for k, v in by.items() if "onWindowFocusChanged" in k)
    assert focus["count"] == 3 and focus["occurrences"] == ["t1", "t2", "t3"]
    assert by["event:http"]["count"] == 2
    assert by["event:terminate"]["count"] == 1

    # a marker at its own coordinates is never merged away
    own = _dedupe_markers([feat("t7", "a", own=True), feat("t8", "a", own=True)])
    assert len(own) == 2


def test_reference_coords_are_not_tracked_fixes():
    """A geofence diagnostic REFERS to coordinates (trigger point, stationary
    anchor); it does not report a location. Treating those as fixes invents GPS
    points and bends the track through them."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import build_layers
    from loganalyzer.locations import collect_fixes
    from loganalyzer.segments import split_segments
    from loganalyzer.analyze import analyze

    text = (
        "07-21 01:00:00.000 DEBUG [TSLocationManager onLocationResult] "
        "Location[fused 45.500000,-73.500000 hAcc=5.0]\n"
        "08-15 01:51:58.084 WARN [GeofenceManagerStationaryExitStrategy handleExit] \n"
        "╔═══\n║ Ignoring stationary geofence EXIT (poor accuracy)\n"
        "╟─ 📍 Stationary=Location[fused 29.872127,74.128350 hAcc=20.0]\n"
        "╟─ 📍 Trigger=Location[fused 29.843473,74.121610 hAcc=899.999]\n╚═══\n")
    recs = assemble(ANDROID, text, 2026)
    for r in recs:
        annotate(r)

    fixes = collect_fixes(recs)
    assert len(fixes) == 1                       # only the real location update
    assert (round(fixes[0].lat, 4), round(fixes[0].lon, 4)) == (45.5, -73.5)

    L = build_layers(analyze(recs, split_segments(recs), []), recs)
    gf = L["geofence"]["features"]
    marker = next(f for f in gf if f["geometry"]["type"] == "Point")
    p = marker["properties"]
    # a drawn vector icon, not an emoji: no emoji means "stationary exit
    # suppressed", and emoji render differently on every platform
    assert p["icon_name"] == "geofence-suppressed"   # vendored Lucide map-pin-x
    assert p["glyph"] == "🚫"                    # text fallback for digest/geojson
    assert p["suppressed"].startswith("Ignoring stationary geofence EXIT")
    assert p["category"] == "geofence transition suppressed"
    # the marker sits on the TRIGGER fix (the questionable one), and carries
    # both labelled coordinates with their accuracies
    assert marker["geometry"]["coordinates"] == [74.12161, 29.843473]
    assert p["trigger_acc"] == 899.999 and p["stationary_acc"] == 20.0

    # …plus the leg the track would have taken had the exit been accepted
    leg = next(f for f in gf if f["geometry"]["type"] == "LineString")
    assert leg["properties"]["role"] == "suppressed-exit-path"
    assert leg["geometry"]["coordinates"] == [[74.12835, 29.872127],
                                              [74.12161, 29.843473]]


def test_two_emoji_slot_parses_the_selector():
    """Regression: a two-emoji level slot ("📌 🔒") left a leading space before
    the selector, so every iOS authorization record parsed with tag_class=None."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    recs = assemble(IOS,
        "2026-07-28 14:48:13.117 📌 🔒 -[TSLocationAuthorization "
        "applicationDidBecomeActive] Application became active\n\n", 2026)
    assert recs[0].tag_class == "TSLocationAuthorization"
    assert recs[0].tag_method == "applicationDidBecomeActive"
    assert recs[0].icon == "📌🔒"


def test_routine_auth_records_are_not_mapped_but_failures_are():
    """Authorization bookkeeping is noise; only a denial/downgrade is an event."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import _route, _is_auth_noise

    routine, problem = assemble(IOS,
        "2026-07-28 14:48:13.117 📌 🔒 -[TSLocationAuthorization "
        "applicationDidBecomeActive] Application became active - refreshing\n\n"
        "2026-07-28 14:49:00.000 📌 🔒 -[TSLocationAuthorization "
        "onAuthorizationStatusChanged:] Authorization status changed: 2 (state: Denied)\n\n",
        2026)
    for r in (routine, problem):
        annotate(r)
    # context is passed explicitly: _route is a pure function of its arguments,
    # never of whatever capture happened to be built last.
    ctx = {"mock_locations": False}
    assert _is_auth_noise(routine) and _route(routine, ctx) == []
    assert not _is_auth_noise(problem) and _route(problem, ctx) != []


def test_http_flush_collapses_to_one_outcome_marker():
    """A flush logs ~5 records; only the outcome is mapped, no-op flushes are
    dropped, and a failure becomes its own icon + severity."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import _collapse_http, _is_http

    recs = assemble(IOS,
        "2026-07-28 13:55:11.448 🔵 -[TSHttpService beginFlushWithCallback:] \n\n"
        "2026-07-28 13:55:11.500 🔵 -[TSHttpResponse handleResponse] Response: 200\n\n"
        "2026-07-28 13:55:11.600 🔵 -[TSHttpService finish:error:] success=1 "
        "queued_before=1 synced=1 pages=1 duration_ms=516\n\n"
        "2026-07-28 14:00:00.000 🔵 -[TSHttpService finish:error:] success=1 "
        "queued_before=0 synced=0 pages=0 duration_ms=0\n\n"
        "2026-07-28 14:10:00.000 ⚠️ -[TSHttpService finish:error:] success=0 "
        "queued_before=1 synced=0 pages=0 duration_ms=30000\n\n", 2026)
    for r in recs:
        annotate(r)
    suppress, info = _collapse_http(recs)

    good, noop, bad = recs[2], recs[3], recs[4]
    assert info[good.seq]["http_ok"] is True
    assert info[good.seq]["http_status"] == 200        # read off the Response line
    assert info[good.seq]["http_ms"] == 516
    assert info[good.seq]["icon_name"] == "http"
    assert noop.seq in suppress                       # nothing queued => not an event
    assert info[bad.seq]["http_ok"] is False
    assert info[bad.seq]["icon_name"] == "http-error"
    assert info[bad.seq]["severity"] == "warning"


def test_stationary_exit_and_stop_timeout_get_opposite_icons():
    """The two ends of a stop/start cycle must look like opposites, not both
    like a generic car: leaving the fence vs settling into stationary."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.emit.geojson import _base_props

    exit_rec, stop_rec, moving = assemble(IOS,
        "2026-07-28 15:19:35.749 🔵 -[TSTrackingService locationManager:didExitRegion:] "
        "Exit stationary region\n\n"
        "2026-07-28 15:11:20.952 🔵 -[TSTrackingService onStopTimeout] 🛑 stopTimeout fired\n\n"
        "2026-07-28 15:20:00.000 🟢 -[TSTrackingService changePace:] isMoving: 1\n\n", 2026)
    for r in (exit_rec, stop_rec, moving):
        annotate(r)

    # a traffic light with the green lamp lit vs the red one: same housing,
    # opposite meaning — the SDK deciding whether to move
    assert _base_props(exit_rec, "motionchange", "placed", 0.0)["icon_name"] \
        == "traffic-go"
    assert _base_props(stop_rec, "motionchange", "placed", 0.0)["icon_name"] \
        == "traffic-stop"
    assert _base_props(moving, "motionchange", "placed", 0.0)["icon_name"] \
        == "motion-vehicle"


def test_stationary_region_radius_is_150_not_the_config_value():
    """The stationary geofence is 150 m. `stationaryRadius` config (e.g. 25 m)
    is a different thing — the proximity arbitration — and must never be drawn
    as the fence."""
    from loganalyzer.model import ANDROID, IOS
    from loganalyzer.records import assemble
    from loganalyzer.emit.geojson import (resolve_stationary_radius,
                                          DEFAULT_STATIONARY_RADIUS_M)

    # an iOS capture that mentions stationaryRadius 25 must still resolve to 150
    ios = assemble(IOS,
        "2026-07-28 15:00:00.000 🔵 -[TSTrackingService onUpdateState:] Location still "
        "within stationaryRadius (25 m)\n\n", 2026)
    assert resolve_stationary_radius(ios) == (DEFAULT_STATIONARY_RADIUS_M, "default (150 m)")

    # an explicit stationary-geofence radius in the log does win
    android = assemble(ANDROID,
        "07-21 01:00:00.000 WARN [GeofenceManagerStationaryExitStrategy handleExit] \n"
        "╔═══\n║ Ignoring stationary geofence EXIT (poor accuracy)\n"
        "╟─ distance=3242.0m, accuracy=899.9m, radius=150.0m\n", 2026)
    assert resolve_stationary_radius(android) == (150.0, "log")


def test_geofence_transition_tints_by_action():
    """ENTER/EXIT/DWELL is the whole story of a transition, so it picks the
    colour: entered = green, left = red, dwelling = amber."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    from loganalyzer.segments import split_segments
    from loganalyzer.structs import annotate
    from loganalyzer.analyze import analyze
    from loganalyzer.emit.geojson import build_layers

    text = ""
    for ts, action in (("15:36:29.660", "ENTER"), ("15:40:00.000", "EXIT"),
                       ("15:45:00.000", "DWELL")):
        text += (f"2026-07-28 {ts} 🔵 -[TSTrackingService didUpdateLocations:] "
                 "📍<+45.5,-73.6> +/- 5.00m\n\n"
                 f"2026-07-28 {ts} \n╔═══\n"
                 f"║ -[TSGeofenceTransition setTriggerLocation:] 📢 {action} Geofence: Test\n"
                 "╚═══\n\n")
    recs = assemble(IOS, text, 2026)
    for r in recs:
        annotate(r)
    feats = build_layers(analyze(recs, split_segments(recs), []), recs)["geofence"]["features"]
    by_action = {f["properties"].get("action"): f["properties"] for f in feats}
    assert by_action["ENTER"]["tint"] == "green"
    assert by_action["EXIT"]["tint"] == "red"
    assert by_action["DWELL"]["tint"] == "amber"


def test_motion_trigger_timer_bookkeeping_is_not_mapped():
    """Arming/resetting the motion-trigger timer is internal bookkeeping, not a
    pace change — it was 20 of 32 motion markers in one fixture, burying the
    real transitions."""
    from loganalyzer.model import IOS
    from loganalyzer.records import assemble
    from loganalyzer.emit.geojson import _is_motionchange

    noise, real = assemble(IOS,
        "2026-07-28 14:41:37.458 🔵 -[TSTrackingService startMotionTriggerTimer] "
        "Motion-trigger timer engaged: Query location-state will trigger in 10 seconds...\n\n"
        "2026-07-28 14:42:29.805 🟢 -[TSTrackingService changePace:] isMoving: 1\n\n", 2026)
    assert not _is_motionchange(noise)
    assert _is_motionchange(real)


def test_map_rules_config_drives_presentation():
    """Icons/colours/bearings live in vocabulary/map-rules.yaml, not in code —
    and the Android lifecycle patterns actually match (they previously used \\b
    inside a non-raw Python string, which is a BACKSPACE, so `onResume` and
    friends silently fell back to a generic gear on 98 records in one fixture)."""
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate
    from loganalyzer.maprules import load_rules
    from loganalyzer.emit.geojson import _base_props

    rules = load_rules()
    assert rules.layer_icon("http") == "http"
    assert rules.layer_clock("http") == 2
    assert rules.event_icon("terminate") == "terminate"
    assert rules.tints["traffic-stop"] == "red"
    assert rules.geofence_actions["ENTER"] == "green"

    for line, want in (("[LifecycleManager f] ☯️  onResume", "foreground"),
                       ("[LifecycleManager g] ☯️  onStart", "foreground"),
                       ("[LifecycleManager h] ☯️  onPause", "background"),
                       ("[LoggerFacade$a a] ☯️  onCreate", "launch")):
        rec = assemble(ANDROID, f"07-04 12:00:00.000 DEBUG {line}\n", 2026)[0]
        annotate(rec)
        assert _base_props(rec, "lifecycle", "placed", 0.0)["icon_name"] == want, line

    # suppression is config-driven and states a reason
    noise = assemble(ANDROID,
        "07-04 12:00:00.000 DEBUG [TSTrackingService startMotionTriggerTimer] "
        "Motion-trigger timer engaged\n", 2026)[0]
    assert rules.suppressed(ANDROID, noise.raw, noise.tag_class or "")


def test_stop_detection_warnings_suppressed_only_under_mock_locations():
    """`mStoppedAtLocation == null` is expected while mock locations are being
    injected (stop detection has no real anchor) — noise there, a finding
    elsewhere. Suppression is therefore conditional on the CAPTURE."""
    from loganalyzer.model import ANDROID
    from loganalyzer.maprules import load_rules

    rules = load_rules()
    raw = ("07-04 12:00:00.000 WARN [TrackingService performStopDetection] \n"
           "  ⚠️  performStopDetection found mStoppedAtLocation == null")
    assert rules.suppressed(ANDROID, raw, "TrackingService", {"mock_locations": True})
    assert not rules.suppressed(ANDROID, raw, "TrackingService", {"mock_locations": False})
    assert not rules.suppressed(ANDROID, raw, "TrackingService", None)


# ── TimeNavigator: sessions, per-vertex times, component splice ──────────────

@needs_car
def test_sessions_split_on_fix_silence_not_record_silence(car):
    """A tracking session is a run of FIXES. Splitting on record silence
    instead turns an idle night of heartbeats into dozens of fixless
    "sessions" — 82 of them in one 4-day Pixel 6 capture."""
    from loganalyzer.locations import collect_fixes
    from loganalyzer.sessions import SESSION_GAP_S, build_sessions
    records, analysis, _ = car
    sessions = build_sessions(analysis, records)

    assert len(sessions) == 3, [s["i"] for s in sessions]
    assert [s["i"] for s in sessions] == [1, 2, 3]      # 1-based, contiguous
    for s in sessions:
        assert s["fixes"] >= 2                          # a lone fix is not an outing
        assert s["records"] > 0
        assert s["duration_s"] > 0
        assert _parse(s["start"]) < _parse(s["end"])
    # never overlapping, always ordered
    for a, b in zip(sessions, sessions[1:]):
        assert _parse(a["end"]) <= _parse(b["start"])

    # every boundary is a real silence in the LOCATION stream
    fixes = sorted(f.t for f in collect_fixes(records) if f.t is not None)
    for a, b in zip(sessions, sessions[1:]):
        between = [t for t in fixes
                   if _parse(a["end"]) <= t <= _parse(b["start"])]
        assert not between, "a fix fell inside a session boundary"
    # this capture's two suspensions are what closed sessions 1 and 2
    assert [s["ended_by"] for s in sessions] == ["suspension", "suspension", None]
    # the drive is session 2; the two short stints bracket it
    assert sessions[1]["distance_m"] > 10_000
    assert max(s["fixes"] for s in sessions) == sessions[1]["fixes"]
    assert SESSION_GAP_S >= 900        # >= GAP_THRESHOLD_S: see the constant's note


def _parse(s):
    return datetime.fromisoformat(s)


@needs_car
def test_track_carries_per_vertex_times_for_clipping(car):
    """The navigator CLIPS the track rather than showing or hiding it whole, so
    every segment carries one second-offset per vertex."""
    _, _, layers = car
    for f in layers["track"]["features"]:
        p = f["properties"]
        vt = p["vt"]
        assert len(vt) == len(f["geometry"]["coordinates"]) == p["n"]
        assert vt[0] == 0                       # relative to the segment's own start
        assert all(b >= a for a, b in zip(vt, vt[1:]))       # non-decreasing
        span = (datetime.fromisoformat(p["end_ts"])
                - datetime.fromisoformat(p["ts"])).total_seconds()
        assert abs(vt[-1] - span) <= 1          # last vertex == the segment end


def test_sessions_need_two_fixes_and_a_real_silence():
    """Synthetic boundary: fixes 19 min apart stay ONE session, 21 min apart
    split into two, and a trailing lone fix never becomes a session."""
    from loganalyzer.sessions import SESSION_GAP_S, build_sessions
    from loganalyzer.model import ANDROID
    from loganalyzer.records import assemble
    from loganalyzer.structs import annotate

    assert SESSION_GAP_S == 20 * 60

    def capture(offsets_s):
        t0 = datetime(2026, 7, 4, 12, 0, 0)
        lines = []
        for i, off in enumerate(offsets_s):
            ts = (t0 + timedelta(seconds=off)).strftime("%m-%d %H:%M:%S.%f")[:-3]
            lines.append(
                f"{ts} DEBUG [TSLocationManager onLocationResult] \n"
                f"╟─ 📍 Location[fused 45.5{i:02d}00,-73.6{i:02d}00 hAcc=5.0]")
        records = assemble(ANDROID, "\n".join(lines) + "\n", 2026)
        for rec in records:
            annotate(rec)
        return records

    class _NoGaps:
        timeline = None

    below = build_sessions(_NoGaps(), capture([0, 19 * 60]))
    assert len(below) == 1 and below[0]["fixes"] == 2

    above = build_sessions(_NoGaps(), capture([0, 60, 21 * 60, 21 * 60 + 60]))
    assert len(above) == 2
    assert [s["fixes"] for s in above] == [2, 2]
    assert _parse(above[0]["end"]) <= _parse(above[1]["start"])

    # a single trailing fix, alone after a long silence, is not a session
    trailing = build_sessions(_NoGaps(), capture([0, 60, 40 * 60]))
    assert len(trailing) == 1 and trailing[0]["fixes"] == 2


@needs_car
def test_navigator_component_is_spliced_and_self_contained(car):
    """The TimeNavigator ships as a component (emit/navigator.py): its CSS,
    markup and JS are spliced in, and the map only wires it up."""
    from loganalyzer.sessions import build_sessions
    from loganalyzer.emit.navigator import NAV_CSS, NAV_HTML, NAV_JS

    records, analysis, layers = car
    sessions = build_sessions(analysis, records)
    html = render_map(layers, title="car", sessions=sessions)

    for placeholder in ("__NAV_CSS__", "__NAV_HTML__", "__NAV_JS__",
                        "__SESSIONS__", "__DATA__"):
        assert placeholder not in html
    assert NAV_CSS.strip() in html and NAV_HTML.strip() in html
    assert NAV_JS.strip() in html
    assert 'id="navcv"' in html and "function TimeNavigator(" in html
    # the component's public API, as documented in emit/navigator.py
    for member in ("contains:", "isWindowed:", "currentSession:", "gotoSession:",
                   "step:", "describe:", "resize:", "draw:"):
        assert member in NAV_JS, member
    # it stays a pure time component: no map/layer/SDK vocabulary inside it
    for leak in ("FEATS", "LAYERS", "GeoJSON", "mercX", "geofence", "wedge"):
        assert leak not in NAV_JS, f"navigator leaked domain concept: {leak}"
    # sessions reached the page
    assert '"ended_by":"suspension"' in html.replace(", ", ",")
    # still one self-contained document — OSM tiles remain the only network dep
    assert "<script src" not in html and "<link " not in html


# ── where the navigator opens ────────────────────────────────────────────────

@needs_car
def test_map_opens_on_the_first_movement_not_the_whole_capture(car):
    """A multi-day capture at full span draws every trip, every night and every
    heartbeat at once. It opens on the first motionchange with isMoving: true —
    the SDK deciding the device started moving — rather than on a distance
    heuristic or a session boundary, because a session can begin hours of
    stationary chatter before the device actually departs.
    """
    from loganalyzer.maprules import load_rules
    from loganalyzer.sessions import build_sessions

    records, analysis, layers = car
    html = render_map(layers, title="car", sessions=build_sessions(analysis, records))

    cfg = load_rules().navigator
    assert cfg["initial_span_minutes"] > 0
    # the opening window is computed in the page from the same signal the
    # motionchange layer carries, so both halves must be present
    assert "function firstMovement()" in html
    assert 'properties.moving !== true' in html
    assert "initial: initialWindow(" in html

    moving = [f for f in layers["motionchange"]["features"]
              if f["properties"].get("moving") is True]
    assert moving, "fixture must contain a motionchange to anchor on"


def test_navigator_accepts_an_opening_window():
    """The component takes the window as data; deciding WHICH window is the
    map's job, so the navigator stays free of SDK concepts."""
    from loganalyzer.emit.navigator import NAV_JS

    assert "opts.initial" in NAV_JS
    # and it clamps rather than trusting the caller
    assert "Math.max(T0, opts.initial.a)" in NAV_JS
    assert "Math.min(T1, opts.initial.b)" in NAV_JS
    # the navigator must not learn what a motionchange is. ("moving" alone is
    # not a valid needle — it names the dragged edge of the window in there.)
    for leak in ("motionchange", "isMoving", "geofence"):
        assert leak not in NAV_JS, f"navigator leaked a domain concept: {leak}"
