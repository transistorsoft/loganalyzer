"""Tests for analyze.py — Stage 4 analysis.

Two layers:
1. Real-fixture tests over the committed fixture corpus (skipped when the
   fixture files are absent) asserting the empirically verified numbers.
2. Synthetic-log tests pinning the design rules (gap taxonomy, dedup shape
   normalization, iOS heartbeat-stretch expectation, app-state lane).
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from loganalyzer.analyze import ABSENCE_NOTE, GAP_THRESHOLD_S, analyze
from loganalyzer.model import Classification
from loganalyzer.records import assemble
from loganalyzer.segments import split_segments
from loganalyzer.sniff import load_sources

from fixtures import fixture, read
LOG62 = fixture("log62")
WALK = fixture("slc-walk")

needs_62 = pytest.mark.skipif(LOG62 is None, reason="fixture (62).log not present")
needs_walk = pytest.mark.skipif(WALK is None, reason="fixture slc-walk.log not present")


def _pipeline(path: Path):
    sources = load_sources([path])
    src = sources[0]
    records = assemble(src.platform, src.text, 2026)
    segments = split_segments(records, {})
    return analyze(records, segments, sources)


def _analyze_text(platform: str, text: str):
    records = assemble(platform, text, 2026)
    segments = split_segments(records, {})
    return analyze(records, segments, [])


@pytest.fixture(scope="module")
def a62():
    return _pipeline(LOG62)


@pytest.fixture(scope="module")
def awalk():
    return _pipeline(WALK)


# ── real fixture: (62).log — Android, 7 launches, overnight schedule ─────────

@needs_62
def test_62_segments(a62):
    assert len(a62.timeline.segments) == 7
    assert all(s.version == "4.2.1" for s in a62.timeline.segments)
    assert all(s.version_source == "banner" for s in a62.timeline.segments)


@needs_62
def test_62_gap_taxonomy(a62):
    gaps = a62.timeline.gaps
    assert a62.timeline.gap_threshold_s == GAP_THRESHOLD_S == 900
    assert len(gaps) >= 21
    by_class = {}
    for g in gaps:
        by_class[g.classification] = by_class.get(g.classification, 0) + 1
    # One process death (fresh banner at 00:20:33), the rest are scheduler
    # windows bounded by "Schedule alarm fired!" records.
    assert by_class.get("death") == 1
    assert by_class.get("scheduler-window", 0) >= 20
    assert "wedge-candidate" not in by_class
    assert "suspension" not in by_class  # Android log: suspension is iOS-only
    for g in gaps:
        assert g.duration_s >= GAP_THRESHOLD_S
        assert g.app_state in ("foreground", "background", "headless", "unknown")
    sched = [g for g in gaps if g.classification == "scheduler-window"]
    assert any("Schedule alarm fired" in g.evidence for g in sched)


@needs_62
def test_62_fgs_denial_warn_group(a62):
    groups = [g for g in a62.warning_groups
              if g.tag_class == "AbstractService" and g.tag_method == "startForegroundService"]
    assert groups, "FGS-denial warn group not found"
    main = max(groups, key=lambda g: g.count)
    assert main.count >= 48
    assert "Background FGS launch denied" in main.representative
    assert main.first_ts and main.last_ts and main.first_ts <= main.last_ts
    assert sum(main.app_states.values()) == main.count


@needs_62
def test_62_dedup_group_counts(a62):
    top = a62.warning_groups[0]
    # performStopDetection warn repeats 986x and must collapse to ONE group.
    assert top.tag_class == "TrackingService"
    assert top.tag_method == "performStopDetection"
    assert top.count == 986
    assert top.severity == "warning"


@needs_62
def test_62_parity_imbalance(a62):
    p = a62.parity
    assert p.persisted > 1500
    assert p.destroyed < 10
    assert p.autosync is True
    assert p.imbalance is True
    assert "persisted" in p.imbalance_note
    kinds = [x.kind for x in a62.anomalies]
    assert "sync-backlog" in kinds
    assert a62.anomalies[0].severity == "high"  # sorted most-severe first


@needs_62
def test_62_header_and_config(a62):
    h = a62.header
    assert h.platform == "android"
    assert "Pixel 10" in h.device
    assert h.app_id == "com.transistorsoft.tslocationmanager.demo"
    assert h.sdk_versions == ["4.2.1"]
    assert h.record_count > 30000
    c = h.config
    assert c.present is True
    assert c.notable.get("autoSync") is True
    assert c.authorization_present is True
    # 4.2.1's Android dump carries no logLevel key: effective level is inferred.
    assert "inferred" in c.effective_log_level
    assert c.defaults_note == "defaults comparison: not yet implemented"


@needs_62
def test_62_pairs_and_http(a62):
    gate = next(p for p in a62.pairs if "GATE" in p.name)
    assert gate.firsts == gate.seconds > 1000
    assert gate.unpaired_first == 0 and gate.orphan_second == 0
    assert a62.http.statuses == {"200": 4}
    assert a62.http.final_queue_depth == 0
    assert len(a62.http.connectivity) > 30
    assert a62.http.flushes_after_connectivity >= 1


@needs_62
def test_62_geofence_and_motion(a62):
    gf = a62.geofence
    assert gf.spurious == 10
    assert gf.registered_max == 8
    assert gf.max_geofences == 97
    assert gf.enters + gf.exits + gf.dwells > 0
    assert a62.motion.activity_histogram.get("STILL", 0) > 50
    assert a62.motion.pace_changes > 0


@needs_62
def test_62_heartbeat_and_end_state(a62):
    hb = {h.segment: h for h in a62.power.heartbeat}
    assert len(hb) == 7
    assert hb[6].count > 50                      # active tracking segment
    assert hb[6].expected_interval_s == 60.0     # from config heartbeatInterval
    assert a62.end_state.enabled is True
    assert a62.end_state.queue_depth == 0
    assert a62.end_state.abrupt_end is False
    assert a62.absence_note == ABSENCE_NOTE


@needs_62
def test_62_asdict_json_roundtrip(a62):
    d = dataclasses.asdict(a62)
    s = json.dumps(d)                            # must not raise (no datetimes)
    back = json.loads(s)
    assert back["timeline"]["gap_threshold_s"] == 900
    assert back["header"]["platform"] == "android"


# ── real fixture: slc-walk — mid-session capture, no banner/config ───────────

@needs_walk
def test_walk_degraded_header(awalk):
    assert len(awalk.timeline.segments) == 1
    assert awalk.timeline.segments[0].version is None
    assert awalk.header.config.present is False
    assert awalk.header.device.startswith("unknown")
    assert not awalk.timeline.gaps


@needs_walk
def test_walk_geofence_and_heartbeat(awalk):
    assert awalk.geofence.spurious == 4
    assert awalk.geofence.registered_max == 8
    hb = awalk.power.heartbeat[0]
    assert hb.count == 4
    assert hb.expected_interval_s is None        # no config dump in this capture
    assert hb.stretched is False                 # can't be stretched without an expectation


@needs_walk
def test_walk_app_state_lane(awalk):
    states = {iv.state for iv in awalk.timeline.app_state}
    assert "foreground" in states
    assert "background" in states


@needs_walk
def test_walk_unknowns_tolerate_missing_classification(awalk):
    u = awalk.unknowns
    assert u.classified is False                 # classify.py did not run
    assert u.unclassified == u.total > 0
    assert u.unknown_rate is None
    assert u.regen_warning is False


# ── synthetic: gap taxonomy rules ────────────────────────────────────────────

def test_gap_death_on_fresh_banner():
    text = (
        "07-01 10:00:00.000 DEBUG [Foo bar] hello\n"
        "07-01 10:00:10.000 DEBUG [Foo bar] world\n"
        "07-01 10:21:00.000 INFO [LoggerFacade$Entry log] \n"
        "╔═════════════════════════════════════════════\n"
        "║ TSLocationManager version: 4.4.1 (4090)\n"
        "╠═════════════════════════════════════════════\n"
        "07-01 10:21:01.000 DEBUG [Foo bar] resumed\n"
    )
    a = _analyze_text("android", text)
    assert len(a.timeline.gaps) == 1
    assert a.timeline.gaps[0].classification == "death"


def test_gap_scheduler_window_on_alarm():
    text = (
        "07-01 10:00:00.000 DEBUG [Foo bar] a\n"
        "07-01 10:21:00.000 DEBUG [ScheduleEvent onScheduleEvent] \n"
        "07-01 10:21:00.010 INFO [ScheduleEvent onScheduleEvent] \n"
        "╔═════════════════════════════════════════════\n"
        "║ 📅  Schedule alarm fired!  enabled: true, trackingMode: 1\n"
        "╠═════════════════════════════════════════════\n"
    )
    a = _analyze_text("android", text)
    assert len(a.timeline.gaps) == 1
    g = a.timeline.gaps[0]
    assert g.classification == "scheduler-window"
    assert "Schedule alarm fired" in g.evidence


def test_gap_wedge_candidate_android_remainder():
    text = (
        "07-01 10:00:00.000 DEBUG [Foo bar] a\n"
        "07-01 10:21:00.000 DEBUG [Foo bar] b\n"
    )
    a = _analyze_text("android", text)
    assert len(a.timeline.gaps) == 1
    assert a.timeline.gaps[0].classification == "wedge-candidate"
    assert any(x.kind == "wedge-candidate-gap" and x.severity == "high"
               for x in a.anomalies)


def test_gap_suspension_ios_without_banner():
    text = (
        "2026-07-01 10:00:00.000 🔵 -[TSTrackingService x] a\n"
        "\n"
        "2026-07-01 10:21:00.000 🔵 -[TSTrackingService x] b\n"
        "\n"
    )
    a = _analyze_text("ios", text)
    assert len(a.timeline.gaps) == 1
    assert a.timeline.gaps[0].classification == "suspension"
    assert not any(x.kind == "wedge-candidate-gap" for x in a.anomalies)


def test_sub_threshold_gap_is_not_a_gap():
    text = (
        "07-01 10:00:00.000 DEBUG [Foo bar] a\n"
        "07-01 10:14:59.000 DEBUG [Foo bar] b\n"
    )
    a = _analyze_text("android", text)
    assert not a.timeline.gaps


# ── synthetic: iOS heartbeat stretch is EXPECTED, never an anomaly ───────────

def test_ios_heartbeat_stretch_expected_not_anomalous():
    parts = [
        "2026-07-01 09:59:58.000 🔵 -[TSLocationManager init] \n"
        "╔═══════════════════════════════════════════════════════════\n"
        "║ TSLocationManager (build 388)\n"
        "╠═══════════════════════════════════════════════════════════\n"
        "{\n"
        "    distanceFilter = 50;\n"
        "    heartbeatInterval = 60;\n"
        "    stopTimeout = 1;\n"
        "}\n",
        "2026-07-01 09:59:59.000 🔵 -[TSAppState onEnterBackground] \n",
    ]
    hb_times = ["10:00:00", "10:01:00", "10:07:00", "10:13:00", "10:19:00"]
    for t in hb_times:
        parts.append(f"2026-07-01 {t}.000 🔵 -[TSHeartbeatService onHeartbeat:] ❤️\n")
    text = "\n".join(parts)
    a = _analyze_text("ios", text)
    hb = a.power.heartbeat[0]
    assert hb.expected_interval_s == 60.0
    assert hb.count == 5
    assert hb.stretched is True                   # median 360s > 1.5 * 60s
    assert hb.stretch_expected is True            # iOS suspension stretch: EXPECTED
    assert "EXPECTED" in hb.note
    assert not any(x.kind == "heartbeat-cadence" for x in a.anomalies)


# ── synthetic: warn/error dedup with shape normalization ─────────────────────

def test_dedup_shape_strips_uuids_and_digits():
    text = (
        "07-01 10:00:00.000 WARN [HttpService flush] \n"
        "  ⚠️  Failed to post 11111111-2222-3333-4444-555555555555 code 500\n"
        "07-01 10:00:05.000 WARN [HttpService flush] \n"
        "  ⚠️  Failed to post aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee code 503\n"
        "07-01 10:00:06.000 ERROR [HttpService flush] \n"
        "  ‼️  boom 42\n"
    )
    a = _analyze_text("android", text)
    assert len(a.warning_groups) == 1
    g = a.warning_groups[0]
    assert g.count == 2
    assert "«id»" in g.shape and "#" in g.shape
    assert "11111111-2222-3333-4444-555555555555" in g.representative  # FULL raw kept
    assert len(a.error_groups) == 1
    assert a.error_groups[0].count == 1
    assert a.error_groups[0].severity == "error"


def test_app_state_lane_and_group_annotation():
    text = (
        "07-01 10:00:00.000 DEBUG [LifecycleManager handleOnResume] ☯️  onResume\n"
        "07-01 10:00:01.000 WARN [Foo bar] \n"
        "  ⚠️  problem in foreground\n"
        "07-01 10:00:02.000 DEBUG [LifecycleManager handleOnPause] ☯️  onPause\n"
        "07-01 10:00:03.000 WARN [Foo bar] \n"
        "  ⚠️  problem in foreground\n"
    )
    a = _analyze_text("android", text)
    states = [iv.state for iv in a.timeline.app_state]
    assert states == ["foreground", "background"]
    g = a.warning_groups[0]
    assert g.count == 2
    assert g.app_states == {"foreground": 1, "background": 1}


# ── synthetic: pair collapse leftovers ───────────────────────────────────────

def test_pair_unpaired_leftover_flagged():
    text = (
        "07-01 10:00:00.000 DEBUG [FgsLaunchGate dbg] 🛃 GATE CLOSE epoch=1\n"
        "07-01 10:00:01.000 DEBUG [FgsLaunchGate dbg] 🛃 GATE OPEN  epoch=1\n"
        "07-01 10:00:02.000 DEBUG [FgsLaunchGate dbg] 🛃 GATE CLOSE epoch=2\n"
    )
    a = _analyze_text("android", text)
    gate = next(p for p in a.pairs if "GATE" in p.name)
    assert gate.firsts == 2 and gate.seconds == 1
    assert gate.paired == 1 and gate.unpaired_first == 1
    assert any(x.kind.startswith("unpaired-") for x in a.anomalies)


# ── synthetic: classification summary when klass IS present ──────────────────

def test_unknown_summary_with_classifications():
    text = (
        "07-01 10:00:00.000 DEBUG [Foo bar] a\n"
        "07-01 10:00:01.000 DEBUG [Foo bar] b\n"
        "07-01 10:00:02.000 DEBUG [Foo bar] c\n"
    )
    records = assemble("android", text, 2026)
    records[0].klass = Classification(status="matched", sites=["Foo.java:1"])
    records[1].klass = Classification(status="unknown")
    records[2].klass = Classification(status="drift", drift_of="Foo.bar#1")
    segments = split_segments(records, {})
    a = analyze(records, segments, [])
    u = a.unknowns
    assert u.classified is True
    assert (u.matched, u.unknown, u.drift, u.unclassified) == (1, 1, 1, 0)
    assert u.unknown_rate == pytest.approx(1 / 3, abs=1e-4)
    assert u.regen_warning is True               # 33% > 5%
    assert any(x.kind == "vocabulary-drift" for x in a.anomalies)
    assert len(u.drift_examples) == 1 and "Foo.bar#1" in u.drift_examples[0]


# ── synthetic: empty input never crashes ─────────────────────────────────────

def test_empty_input():
    a = analyze([], [], [])
    assert a.header.record_count == 0
    assert not a.timeline.gaps and not a.timeline.segments
    assert json.dumps(dataclasses.asdict(a))
