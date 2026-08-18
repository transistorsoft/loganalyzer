"""Tests for emit/digest.py — Redactor, render_markdown, render_json, redact_slice.

Layers:
1. Redactor unit tests (alias stability, idempotence, allowlists, registration).
2. Synthetic-Analysis tests: a small hand-written Android log run through the
   real pipeline (assemble -> split_segments -> analyze), then rendered.
3. Real-fixture smoke tests (skipped when fixtures are absent): digest renders
   in the scannable size range and leaks no raw coordinates/uuids.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from loganalyzer.analyze import analyze
from loganalyzer.emit.digest import (
    _COORD_IOS,
    _COORD_PAIR,
    _UUID,
    Redactor,
    redact_slice,
    register_from_records,
    render_json,
    render_markdown,
)
from loganalyzer.model import ANDROID, Classification
from loganalyzer.records import assemble
from loganalyzer.segments import split_segments
from loganalyzer.sniff import load_sources

from fixtures import fixture, read
LOG62 = fixture("log62")
BIG_IOS = fixture("big-ios")

needs_62 = pytest.mark.skipif(LOG62 is None, reason="fixture (62).log not present")
needs_big_ios = pytest.mark.skipif(BIG_IOS is None, reason="big iOS log not present")


# ═════════════════════════════════════════════════════════════════════════════
# Redactor unit tests
# ═════════════════════════════════════════════════════════════════════════════

class TestRedactor:
    def test_coordinate_pair_stable_alias(self):
        r = Redactor()
        a = r.redact("fix at 45.518862,-73.600546 ok")
        b = r.redact("again 45.518862,-73.600546 ok")
        assert "45.518862" not in a
        assert "COORD-A" in a
        assert "COORD-A" in b            # same pair -> same alias
        c = r.redact("elsewhere 45.520001,-73.610001")
        assert "COORD-B" in c            # different pair -> next alias

    def test_ios_form_and_pair_share_alias(self):
        r = Redactor()
        a = r.redact("📍<+45.123456,-73.123456>")
        b = r.redact("raw 45.123456,-73.123456 end")
        assert a == "📍<COORD-A>"
        assert "COORD-A" in b            # sign-normalized: same place, same alias

    def test_coordinate_range_validated(self):
        r = Redactor()
        s = "resolution 123.456789,999.123456 raw"
        assert r.redact(s) == s          # lon > 180: not a coordinate

    def test_version_pairs_untouched(self):
        r = Redactor()
        s = "versions 4.5.0, 4.4.3 and maxRange=156.9064, resolution=0.001"
        assert r.redact(s) == s

    def test_uuid_alias(self):
        r = Redactor()
        out = r.redact("uuid: 55c8b45c-097b-4146-8a67-0417cbf22a45 done")
        assert "REC-1" in out
        assert "55c8b45c" not in out
        again = r.redact("55C8B45C-097B-4146-8A67-0417CBF22A45")
        assert again == "REC-1"          # case-insensitive same uuid

    def test_package_and_intent_cmp(self):
        r = Redactor()
        s = ("Intent { act=motionchange dat=tslocationmanager://service/x "
             "cmp=com.acme.fleet/com.transistorsoft.locationmanager.service.TrackingService }")
        out = r.redact(s)
        assert "com.acme.fleet" not in out
        assert "PKG-1" in out
        assert "tslocationmanager://service/x" not in out
        assert "URL-1" in out            # scheme-agnostic dat= URI
        # SDK-internal namespace stays readable (identifies nobody):
        assert "com.transistorsoft.locationmanager.service.TrackingService" in out

    def test_platform_namespaces_allowlisted(self):
        r = Redactor()
        s = "android.intent.action.MY_PACKAGE_REPLACED via java.lang.Thread"
        assert r.redact(s) == s

    def test_url_alias_and_verdict(self):
        r = Redactor()
        out = r.redact("posting to http://192.168.1.10:9000/locations.")
        assert "192.168.1.10" not in out
        assert "URL-1." in out           # trailing punctuation preserved
        v = r.url_with_verdict("http://192.168.1.10:9000/locations", "private-lan")
        assert v == "URL-1 (private-LAN — unreachable from cellular)"

    def test_geofence_registration_not_guessing(self):
        r = Redactor()
        s = "ENTER: office_237 (officer on duty)"
        assert r.redact(s) == s          # unregistered: never guessed
        r.register_geofence("office_237")
        out = r.redact(s)
        assert "GF-1" in out
        assert "office_237" not in out
        assert "officer" in out          # word-boundary: substrings survive

    def test_device_registration(self):
        r = Redactor()
        r.register_device("Google Pixel 10 @ 16 (native)")
        out = r.redact("Google Pixel 10 @ 16 (native)")
        assert out == "DEV-1 @ 16 (native)"   # OS level survives

    def test_mapping(self):
        r = Redactor()
        r.register_geofence("home")
        r.redact("55c8b45c-097b-4146-8a67-0417cbf22a45 at 45.123456,-73.654321")
        m = r.mapping()
        assert m["GF-1"] == "home"
        assert m["REC-1"] == "55c8b45c-097b-4146-8a67-0417cbf22a45"
        assert m["COORD-A"] == "45.123456,-73.654321"

    def test_idempotence(self):
        r = Redactor()
        s = ("📍<+45.111222,-73.333444> uuid 55c8b45c-097b-4146-8a67-0417cbf22a45 "
             "pkg com.acme.fleet url https://tracker.acme.io/v1")
        once = r.redact(s)
        assert r.redact(s) == once       # same input string -> same aliases
        assert r.redact(once) == once    # aliases are never re-redacted

    def test_disabled_redactor_identity_but_config_masking_works(self):
        r = Redactor(enabled=False)
        s = "45.123456,-73.123456 http://192.168.1.10/x"
        assert r.redact(s) == s
        # the ALWAYS-masked config path still allocates aliases:
        assert r.url_with_verdict("http://192.168.1.10/x", "private-lan").startswith("URL-1")


# ═════════════════════════════════════════════════════════════════════════════
# Synthetic Analysis
# ═════════════════════════════════════════════════════════════════════════════

SYNTH = """\
07-03 21:00:00.100 INFO [TSLocationManagerActivity onCreate]
╔═════════════════════════════════════
║ TSLocationManager version: 4.5.0 (5000)
╟─ com.acme.fleet
╟─ Google Pixel 10 @ 16 (native)
╚═════════════════════════════════════
07-03 21:00:00.200 DEBUG [TSConfig print]
{
  "distanceFilter": 10,
  "autoSync": true,
  "logLevel": 5,
  "heartbeatInterval": 60,
  "url": "http://192.168.1.10:9000/locations",
  "authorization": {"strategy": "JWT", "accessToken": "SECRET-TOKEN-VALUE"}
}
07-03 21:00:01.000 DEBUG [TSLocationManager onLocationResult]
╟─ 📍 Location[fused 45.518862,-73.600546 hAcc=13.5 et=+10h25m6s54ms]
07-03 21:00:02.000 WARN [TrackingService performStopDetection]
  ⚠️  Waiting for location 55c8b45c-097b-4146-8a67-0417cbf22a45 last=45.518862,-73.600546
07-03 21:00:03.000 WARN [TrackingService performStopDetection]
  ⚠️  Waiting for location 55c8b45c-097b-4146-8a67-0417cbf22a45 last=45.518862,-73.600546
07-03 21:00:04.000 INFO [ConnectivityMonitor onConnectivityChange] Connectivity change: connected? true
07-03 21:00:05.000 INFO [HttpService flush]
  🔵  HTTP Service (count: 3)
07-03 21:00:06.000 INFO [AbstractService startForegroundService]
  ℹ️  Background FGS launch denied: Intent { act=motionchange dat=tslocationmanager://service/x cmp=com.acme.fleet/com.transistorsoft.locationmanager.service.TrackingService }
07-03 21:00:07.000 ERROR [HttpService onFailure]
  ‼️  HTTP request failed: http://192.168.1.10:9000/locations
07-03 21:25:00.000 DEBUG [TSLocationManager onLocationResult]
╟─ 📍 Location[fused 45.520001,-73.610001 hAcc=9.9]
07-03 21:25:01.000 DEBUG [TrackingService stop] isMoving: false
"""

RAW_URL = "http://192.168.1.10:9000/locations"


@pytest.fixture(scope="module")
def synth():
    records = assemble(ANDROID, SYNTH, 2026)
    for rec in records:
        if rec.level == "WARN":
            rec.klass = Classification(
                status="matched",
                sites=["service/TrackingService.java:512",
                       "service/TrackingService.java:530"],
                confidence="ambiguous")
    segments = split_segments(records, {})
    return analyze(records, segments, [])


@pytest.fixture(scope="module")
def md_redacted(synth):
    return render_markdown(synth, redact=True)


@pytest.fixture(scope="module")
def md_unredacted(synth):
    return render_markdown(synth, redact=False)


class TestMarkdown:
    def test_section_order(self, md_redacted):
        headings = ["## Header", "## Timeline", "## Warnings & Errors", "## Health",
                    "## State at end of log", "## Anomalies", "## Unknown lines",
                    "## Missing evidence — ask the customer"]
        positions = [md_redacted.index(h) for h in headings]
        assert positions == sorted(positions)

    def test_redaction_applied(self, md_redacted):
        assert "45.518862" not in md_redacted
        assert "COORD-A" in md_redacted
        assert "55c8b45c" not in md_redacted
        assert "REC-1" in md_redacted
        assert "com.acme.fleet" not in md_redacted
        assert "PKG-" in md_redacted
        assert RAW_URL not in md_redacted
        assert "URL-1" in md_redacted
        assert "Google Pixel" not in md_redacted
        assert "DEV-1 @ 16 (native)" in md_redacted

    def test_url_verdict_survives_redaction(self, md_redacted):
        assert "private-LAN — unreachable from cellular" in md_redacted

    def test_two_line_quote_shape(self, md_redacted):
        # representative quoted verbatim: header line, body below (redacted)
        assert "07-03 21:00:02.000 WARN [TrackingService performStopDetection]" in md_redacted
        assert "⚠️  Waiting for location REC-1 last=COORD-A" in md_redacted

    def test_dedup_counts_and_source_links(self, md_redacted):
        assert "#### 2× warning — `TrackingService performStopDetection`" in md_redacted
        assert "→ `service/TrackingService.java:512` · `service/TrackingService.java:530`" in md_redacted
        assert "*(multi-candidate tie)*" in md_redacted
        assert "#### 1× error — `HttpService onFailure`" in md_redacted

    def test_gap_and_anomaly(self, md_redacted):
        assert "wedge-candidate" in md_redacted
        assert "**HIGH** `wedge-candidate-gap`" in md_redacted

    def test_config_always_masked_even_unredacted(self, md_unredacted):
        cfg = md_unredacted.split("### Config")[1].split("## Timeline")[0]
        assert RAW_URL not in cfg
        assert "URL-1 (private-LAN — unreachable from cellular) — always masked" in cfg
        assert "present — values always masked" in cfg
        assert "SECRET-TOKEN-VALUE" not in md_unredacted

    def test_unredacted_keeps_everything_else(self, md_unredacted):
        assert "45.518862,-73.600546" in md_unredacted
        assert "Google Pixel 10 @ 16 (native)" in md_unredacted
        assert "never paste" in md_unredacted

    def test_alias_stability_across_sections(self, synth):
        red = Redactor()
        render_markdown(synth, redact=True, redactor=red)
        m = red.mapping()
        assert m["COORD-A"] == "45.518862,-73.600546"
        assert m["URL-1"] == RAW_URL
        assert m["DEV-1"] == "Google Pixel 10"

    def test_deterministic(self, synth, md_redacted):
        assert render_markdown(synth, redact=True) == md_redacted

    def test_scannable_size(self, md_redacted):
        n = md_redacted.count("\n")
        assert n < 250, f"synthetic digest unexpectedly long: {n} lines"

    def test_end_state_and_health(self, md_redacted):
        assert "isMoving | false" in md_redacted
        assert "final queue depth | 3" in md_redacted
        assert "flushes ≤60 s after reconnect | 1" in md_redacted

    def test_missing_evidence_present(self, md_redacted):
        sec = md_redacted.split("## Missing evidence — ask the customer")[1]
        assert "precise vs approximate" in sec

    def test_unknown_lines_classified(self, md_redacted):
        sec = md_redacted.split("## Unknown lines")[1].split("## Missing evidence")[0]
        assert "matched | 2" in sec


class TestJson:
    def test_render_json_serializable_and_unredacted(self, synth):
        d = render_json(synth)
        s = json.dumps(d)
        assert isinstance(d, dict)
        assert d["header"]["config"]["url"] == RAW_URL      # NEVER redacted
        assert "45.518862" in s
        assert d["header"]["record_count"] == 11
        assert d["timeline"]["gaps"][0]["classification"] == "wedge-candidate"
        # timestamps are ISO strings
        assert d["header"]["log_start"] == "2026-07-03 21:00:00.100"


SLICE = """\
07-03 21:00:02.000 INFO [TSGeofenceManager onGeofence]
  📢 ENTER: office_237 at 45.518862,-73.600546
07-03 21:00:03.000 DEBUG [TSLocationManager onLocationResult]
╟─ 📍 Location[fused 45.518862,-73.600546 hAcc=13.5]
"""


class TestSlice:
    def test_redact_slice_two_line_shape(self):
        recs = assemble(ANDROID, SLICE, 2026)
        recs[0].structs["geofence"] = {"action": "ENTER", "identifier": "office_237"}
        red = Redactor()
        out = redact_slice(recs, red)
        blocks = out.strip().split("\n\n")
        assert len(blocks) == 2                      # blank line between records
        assert blocks[0].splitlines()[0] == "07-03 21:00:02.000 INFO [TSGeofenceManager onGeofence]"
        assert blocks[0].splitlines()[1].startswith("  📢")   # body below header
        assert "office_237" not in out
        assert "GF-1" in out
        assert out.count("COORD-A") == 2             # same pair, both records
        assert red.mapping()["GF-1"] == "office_237"

    def test_redact_slice_no_redact(self):
        recs = assemble(ANDROID, SLICE, 2026)
        out = redact_slice(recs, Redactor(enabled=False))
        assert "45.518862,-73.600546" in out

    def test_empty(self):
        assert redact_slice([], Redactor()) == ""

    def test_register_from_records_shared_with_digest(self):
        """CLI flow: pre-register geofence ids from records, then render the
        digest with the SAME redactor — GF aliases redact in quoted records."""
        records = assemble(ANDROID, SLICE + SYNTH, 2026)
        records[0].structs["geofence"] = {"action": "ENTER", "identifier": "office_237"}
        segments = split_segments(records, {})
        a = analyze(records, segments, [])
        red = Redactor()
        register_from_records(records, red)
        md = render_markdown(a, redact=True, redactor=red)
        assert "office_237" not in md
        assert red.mapping()["GF-1"] == "office_237"
        # slices rendered with the same redactor share the alias space
        out = redact_slice(records[:1], red)
        assert "GF-1" in out


class TestEmptyAnalysis:
    def test_render_empty(self):
        a = analyze([], [], [])
        md = render_markdown(a)
        assert "## Header" in md
        assert "## Missing evidence — ask the customer" in md
        json.dumps(render_json(a))


# ═════════════════════════════════════════════════════════════════════════════
# Real fixtures (smoke)
# ═════════════════════════════════════════════════════════════════════════════

def _pipeline(path: Path):
    sources = load_sources([path])
    src = sources[0]
    records = assemble(src.platform, src.text, 2026)
    segments = split_segments(records, {})
    return analyze(records, segments, sources)


def _leaks_coordinates(md: str) -> bool:
    for m in _COORD_PAIR.finditer(md):
        if abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180:
            return True
    for m in _COORD_IOS.finditer(md):
        if abs(float(m.group(1))) <= 90 and abs(float(m.group(2))) <= 180:
            return True
    return False


@needs_62
def test_fixture_62_digest():
    a = _pipeline(LOG62)
    md = render_markdown(a, redact=True)
    n = md.count("\n")
    assert 60 <= n <= 700, f"digest size out of range: {n} lines"
    for h in ("## Header", "## Timeline", "## Warnings & Errors", "## Health",
              "## State at end of log", "## Anomalies", "## Unknown lines",
              "## Missing evidence — ask the customer"):
        assert h in md
    assert not _leaks_coordinates(md)
    assert not _UUID.search(md)
    json.dumps(render_json(a))


@needs_big_ios
def test_fixture_big_ios_digest_size():
    a = _pipeline(BIG_IOS)
    md = render_markdown(a, redact=True)
    n = md.count("\n")
    # design target: 200-500 rendered lines for a ~75k-record log
    assert 60 <= n <= 700, f"digest size out of range: {n} lines"
    assert not _leaks_coordinates(md)
    assert "## Health" in md


# ── source-link provenance note ──────────────────────────────────────────────

def test_source_link_note_only_when_there_are_line_refs():
    """The note qualifies `file:line` links. Without the private source map the
    sites fall back to vocabulary entry ids — symbolic, useful, and nothing a
    line number applies to — so the note must stay quiet."""
    from loganalyzer.emit.digest import _source_link_note

    class G:
        def __init__(self, sites): self.sites = sites

    class H:
        platform = "ios"
        sdk_versions = ["4.4.2"]

    class A:
        header = H()
        def __init__(self, sites): self.error_groups, self.warning_groups = [], [G(sites)]

    versions = {"ios": "4.4.4", "android": "4.5.0"}

    # file:line present -> note, naming ONLY this capture's platform
    note = _source_link_note(A(["native/ios/TSLocationManager/Util/Foo.m:367"]), versions)
    assert note and "ios 4.4.4" in note[0]
    assert "android" not in note[0]          # citing the other platform is noise
    assert "4.4.2" in note[0]                # and it names the capture's own version

    # symbolic entry-id sites -> silent
    assert _source_link_note(A(["TSLocationMetricsEngine.computeFor:previous:#2"]), versions) == []
    # no sites at all -> silent
    assert _source_link_note(A([]), versions) == []
    # no harvest metadata -> silent
    assert _source_link_note(A(["native/ios/Foo.m:1"]), None) == []
