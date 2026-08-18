"""Tests for structs.py mini-parsers.

Every example here is copied VERBATIM from the fixture corpus in
tests/fixtures/ + the private corpus (Android: `background-geolocation (62).log`, slc-pixel10;
iOS: background-geolocation-bike.log and the big iOS capture) — grep'd out,
not invented. Records are built through the real Stage-1 assembler so the
parsers see exactly what they will see in production.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from loganalyzer import records, structs
from loganalyzer.model import ANDROID, IOS

from fixtures import fixture, read


def one(platform: str, text: str, index: int = 0):
    recs = records.assemble(platform, text, 2026)
    for r in recs:
        structs.annotate(r)
    return recs[index]


# ── Android Location[…] ──────────────────────────────────────────────────────

ANDROID_LOC_BANNER = """\
07-03 23:36:42.113 INFO [TSLocationManager b]
╔═════════════════════════════════════════════
║ motionchange LocationResult: 2
╠═════════════════════════════════════════════
╟─ 📍 Location[fused 45.518862,-73.600546 hAcc=13.524 et=+10h25m6s54ms alt=47.89999771118164 vAcc=0.52 vel=0.118783034 sAcc=1.5 bear=8.124804 bAcc=45.0 {Bundle[{persist=false}]}], time: 1783136202571
"""


def test_android_location_full():
    rec = one(ANDROID, ANDROID_LOC_BANNER)
    loc = rec.structs["location"]
    assert loc["provider"] == "fused"
    assert loc["lat"] == 45.518862
    assert loc["lon"] == -73.600546
    assert loc["acc"] == 13.524
    # et=+10h25m6s54ms — boot-relative elapsed in ms
    assert loc["et"] == ((10 * 3600 + 25 * 60 + 6) * 1000) + 54
    assert loc["speed"] == 0.118783034
    assert loc["course"] == 8.124804
    assert loc["alt"] == 47.89999771118164
    assert loc["mock"] is False
    assert loc["time"] == 1783136202571


ANDROID_LOC_MOCK = """\
07-03 23:47:03.855 DEBUG [TSLocationManager onLocationResult]
  📍 Location[gps 45.530392,-73.579642 hAcc=2.3492765 et=+10h34m1s850ms alt=0.0 vel=13.888889 bear=136.20145 mock]
"""


def test_android_location_mock_flag():
    loc = one(ANDROID, ANDROID_LOC_MOCK).structs["location"]
    assert loc["provider"] == "gps"
    assert loc["mock"] is True
    assert loc["speed"] == 13.888889
    assert loc["et"] == ((10 * 3600 + 34 * 60 + 1) * 1000) + 850


def test_android_location_multiday_et():
    # from slc-pixel10: et spans a day boundary
    text = (
        "07-05 12:52:07.997 INFO [GeofencingProcessor logTriggerVsGeofence] \n"
        "╟─ 📍 Trigger=Location[fused 45.518876,-73.600528 hAcc=12.314 "
        "et=+1d23h40m16s428ms alt=47.79999923706055 vAcc=0.61222684 "
        "vel=0.5170421 sAcc=1.5 bear=19.993404 bAcc=45.0]\n"
    )
    loc = one(ANDROID, text).structs["location"]
    assert loc["et"] == (((1 * 24 + 23) * 3600 + 40 * 60 + 16) * 1000) + 428
    assert loc["mock"] is False


def test_android_location_truncated_never_raises():
    rec = one(ANDROID, "07-03 23:47:03.855 DEBUG [X y] \n  📍 Location[gps 45.5303")
    # truncated mid-struct: parse what's recoverable, never raise
    assert "location" not in rec.structs or rec.structs["location"]["provider"] == "gps"


# ── LocationFilterResult / filter metrics ────────────────────────────────────

ANDROID_FILTER = (
    "07-03 23:36:49.960 DEBUG [TSLocationManager onLocationResult] "
    "LocationFilterResult: LocationFilterResult{decision=ACCEPTED, reason='ok', "
    "selected=0.0, raw=2.1746432781219482, effective=0.0, anomaly=false, "
    "acc(cur)=13.652, acc(prev)=13.524}\n"
)


def test_android_filter_result():
    fr = one(ANDROID, ANDROID_FILTER).structs["filter_result"]
    assert fr["decision"] == "ACCEPTED"
    assert fr["reason"] == "ok"
    assert fr["raw"] == 2.1746432781219482
    assert fr["effective"] == 0.0
    assert fr["anomaly"] is False
    assert fr["acc_cur"] == 13.652
    assert fr["acc_prev"] == 13.524


IOS_FILTER_METRICS = (
    "2026-07-28 12:37:03.564 📌 -[TSLocationFilter evaluateWithMetrics:] "
    "decision=Accepted reason=OK raw=264.6m effective=245.9m smoothed=245.9m "
    "cap=20825.2m acc=16.3m speed=6.55 sigma=0.0m df=50.0m\n"
)


def test_ios_filter_metrics():
    fr = one(IOS, IOS_FILTER_METRICS).structs["filter_result"]
    assert fr["decision"] == "Accepted"
    assert fr["reason"] == "OK"
    assert fr["raw"] == 264.6          # metres unit stripped
    assert fr["effective"] == 245.9
    assert fr["cap"] == 20825.2
    assert fr["speed"] == 6.55
    assert fr["df"] == 50.0


# ── DetectedActivity / CMMotionActivity ──────────────────────────────────────

def test_detected_activity_with_stray_variation_selector():
    # the 🚘 icon in this fixture line carries a stray VS16 after the space
    text = (
        "07-03 23:36:44.209 DEBUG [MotionActivityProcessor onActivityResult] \n"
        "  🚘 ️DetectedActivity [type=STILL, confidence=96]\n"
    )
    da = one(ANDROID, text).structs["detected_activity"]
    assert da == {"type": "STILL", "confidence": 96}


IOS_MOTION_ACTIVITY = (
    "2026-07-28 12:10:34.284 🔵 -[TSMotionDetector activitySourceDidUpdate:]_block_invoke "
    "MotionActivity Rx <CMMotionActivity st:0 walk:0 run:0 auto:0 cyc:0 conf:2 "
    "start:2026-07-28 16:10:28 +0000 test:->\n"
)


def test_cmmotionactivity():
    ma = one(IOS, IOS_MOTION_ACTIVITY).structs["motion_activity"]
    assert ma["st"] == 0
    assert ma["walk"] == 0
    assert ma["auto"] == 0
    assert ma["cyc"] == 0
    assert ma["conf"] == 2
    assert ma["start"] == "2026-07-28 16:10:28 +0000"
    assert ma["test"] == "-"


def test_cmmotionactivity_truncated():
    rec = one(IOS, "2026-07-28 12:10:34.284 🔵 -[TSMotionDetector activitySourceDidUpdate:]_block_invoke MotionActivity Rx <CMMotionActivity st:1 walk:0 ru\n")
    ma = rec.structs.get("motion_activity")
    assert ma is None or ma["st"] == 1  # partial fields OK, no raise


# ── Intent { act= dat= cmp= } ────────────────────────────────────────────────

ANDROID_INTENT = (
    "07-03 23:45:41.812 DEBUG [FgsLaunchGate dbg] \n"
    "  ℹ️  Background FGS launch denied:  Retrying with "
    "TSLocationManager::FOREGROUND_SERVICE_GEOFENCE...Intent { act=motionchange "
    "dat=tslocationmanager://service/... xflg=0x4 "
    "cmp=com.transistorsoft.tslocationmanager.demo/"
    "com.transistorsoft.locationmanager.service.TrackingService (has extras) }\n"
)


def test_intent():
    it = one(ANDROID, ANDROID_INTENT).structs["intent"]
    assert it["act"] == "motionchange"
    assert it["dat"] == "tslocationmanager://service/..."
    assert it["cmp"] == ("com.transistorsoft.tslocationmanager.demo/"
                         "com.transistorsoft.locationmanager.service.TrackingService")


def test_intent_nested_bundle_braces():
    text = (
        "07-03 23:45:41.812 DEBUG [X y] Intent { act=boot "
        "cmp=com.foo/.Svc extras=Bundle[{a={b=1}}] }\n"
    )
    it = one(ANDROID, text).structs["intent"]
    assert it["act"] == "boot"
    assert it["cmp"] == "com.foo/.Svc"


# ── Android truncated config JSON ────────────────────────────────────────────

# Verbatim head of `background-geolocation (62).log`, abridged in the middle
# (full nested "app" object kept) — the dump ends mid-key exactly like the
# fixture: `  "heartbeatEnabled":` with no value and no closing brace.
ANDROID_CONFIG_TRUNCATED = """\
07-03 23:36:43.937 INFO [LoggerFacade$Entry log]
╔═════════════════════════════════════════════
║ TSLocationManager version: 4.2.1 (4063)
╠═════════════════════════════════════════════
╟─ com.transistorsoft.tslocationmanager.demo
╟─ Google Pixel 10 @ 16 (native)
{
  "actions": [],
  "activity": {
    "activityRecognitionInterval": 10000,
    "disableMotionActivityUpdates": false,
    "disableStopDetection": false,
    "minimumActivityRecognitionConfidence": 75,
    "motionTriggerDelay": 0,
    "stopOnStationary": false,
    "triggerActivities": "in_vehicle, on_bicycle, on_foot, running, walking"
  },
  "activityRecognitionInterval": 10000,
  "app": {
    "backgroundPermissionRationale": {},
    "enableHeadless": true,
    "foregroundService": true,
    "headlessJobService": "com.transistorsoft.locationmanager.demo.HeadlessTask",
    "heartbeatInterval": 60,
    "mainActivityName": null,
    "notification": {
      "actions": [],
      "allowTap": true,
      "channelId": "bggeo",
      "largeIcon": "drawable\\/ic_service_icon",
      "priority": -1,
      "text": "NOTI TEXT",
      "title": "NOTI TITLE"
    },
    "schedule": [],
    "scheduleUseAlarmManager": false,
    "serviceLaunchDelay": 1000,
    "startOnBoot": true,
    "stopOnTerminate": false
  },
  "autoSync": true,
  "desiredAccuracy": 100,
  "distanceFilter": 50,
  "heartbeatEnabled":
07-03 23:36:43.937 INFO [LoggerFacade$Entry log]
╔═════════════════════════════════════════════
║ DEVICE SENSORS
╠═════════════════════════════════════════════
"""


def test_android_config_truncated():
    rec = one(ANDROID, ANDROID_CONFIG_TRUNCATED)
    cd = rec.structs["config_dump"]
    assert cd["truncated"] is True
    data = cd["data"]
    # complete top-level keys extracted
    assert data["actions"] == []
    assert data["activityRecognitionInterval"] == 10000
    assert data["autoSync"] is True
    assert data["desiredAccuracy"] == 100
    assert data["distanceFilter"] == 50
    # nested objects parsed whole
    assert data["app"]["heartbeatInterval"] == 60
    assert data["app"]["notification"]["largeIcon"] == "drawable/ic_service_icon"
    assert data["activity"]["triggerActivities"].startswith("in_vehicle")
    # the incomplete trailing key is NOT present
    assert "heartbeatEnabled" not in data


def test_android_config_complete_block():
    text = (
        "07-03 23:36:43.937 INFO [TSConfig print] \n"
        "{\n"
        '  "enabled": true,\n'
        '  "extras": {},\n'
        "}\n".replace(",\n}", "\n}")  # valid JSON tail
    )
    cd = one(ANDROID, text).structs["config_dump"]
    assert cd["truncated"] is False
    assert cd["data"] == {"enabled": True, "extras": {}}


# ── iOS 📍 pins + N: batches ─────────────────────────────────────────────────

IOS_PIN_BATCH_1 = """\
2026-07-28 12:10:34.284
1:📍<+45.51889802,-73.60049090> +/- 9.18m (speed -1.00 mps / course -1.00) @ 2026-07-28, 12:10:34 PM Eastern Daylight Time | age: 148 ms
"""

IOS_PIN_BATCH_2 = """\
2026-07-28 12:37:04.359
2:📍<+45.51780643,-73.59747025> +/- 16.18m (speed 6.59 mps / course -1.00) @ 2026-07-28, 12:37:04 PM Eastern Daylight Time | age: 66 ms
"""


def test_ios_pin_batch_index_and_sentinels():
    rec = one(IOS, IOS_PIN_BATCH_1)
    loc = rec.structs["location"]
    assert loc["batch_index"] == 1
    assert loc["lat"] == 45.51889802
    assert loc["lon"] == -73.6004909
    assert loc["acc"] == 9.18
    assert loc["speed"] is None       # -1.00 sentinel → unknown
    assert loc["course"] is None
    assert loc["age_ms"] == 148
    assert "12:10:34" in loc["time_text"]
    # batch-indexed pins collect into `locations`
    assert rec.structs["locations"] == [loc]


def test_ios_pin_batch_2_with_speed():
    loc = one(IOS, IOS_PIN_BATCH_2).structs["location"]
    assert loc["batch_index"] == 2
    assert loc["speed"] == 6.59
    assert loc["course"] is None
    assert loc["age_ms"] == 66


def test_ios_pin_unprefixed_header_slot():
    # pin in the header slot itself, real speed/course (big iOS capture)
    text = ("2026-08-12 16:16:00.079 📍<+45.50013822,-73.59385360> +/- 6.07m "
            "(speed 6.27 mps / course 214.89) @ 2026-08-12, 4:16:00 PM "
            "Eastern Daylight Time | age: 79 ms\n")
    rec = one(IOS, text)
    loc = rec.structs["location"]
    assert "batch_index" not in loc
    assert loc["speed"] == 6.27
    assert loc["course"] == 214.89
    assert "locations" not in rec.structs   # no batch index, single pin


def test_ios_pin_truncated_never_raises():
    rec = one(IOS, "2026-08-12 16:16:00.079 📍<+45.50013822,-73.5\n")
    assert rec.structs.get("location", {"lat": 45.50013822})["lat"] == 45.50013822


# ── iOS plist config dump ────────────────────────────────────────────────────

IOS_PLIST_CONFIG = """\
2026-07-28 12:10:34.018 🔵 -[TSLocationManager init]
╔═══════════════════════════════════════════════════════════
║ TSLocationManager (build 388)
╠═══════════════════════════════════════════════════════════
{
    activity =     {
        activityRecognitionInterval = 10000;
        disableMotionActivityUpdates = 0;
        triggerActivities = "";
    };
    activityType = 1;
    authorization =     {
        accessToken = "eyJhb<redacted>";
        expires = "-1";
        refreshHeaders =         {
            Authorization = "Bearer {accessToken}";
        };
        refreshPayload =         {
            "refresh_token" = "{refreshToken}";
        };
        refreshUrl = "https://tracker.transistorsoft.com/api/register";
        strategy = JWT;
    };
    desiredAccuracy = "-1";
    distanceFilter = 50;
    extras =     {
        "config-extra" = CONFIG;
    };
    odometer = "15.3163639165963";
    schedule =     (
    );
    url = "https://tracker.transistorsoft.com/api/locations";
}
"""


def test_ios_plist_config_dump():
    cd = one(IOS, IOS_PLIST_CONFIG).structs["config_dump"]
    assert cd["truncated"] is False
    data = cd["data"]
    assert data["activityType"] == 1
    assert data["desiredAccuracy"] == -1          # quoted NSNumber → int
    assert data["distanceFilter"] == 50
    assert data["odometer"] == 15.3163639165963   # quoted float
    assert data["schedule"] == []                 # empty ObjC array
    assert data["authorization"]["strategy"] == "JWT"
    assert data["authorization"]["refreshHeaders"]["Authorization"] == "Bearer {accessToken}"
    assert data["authorization"]["refreshPayload"]["refresh_token"] == "{refreshToken}"
    assert data["extras"]["config-extra"] == "CONFIG"
    assert data["url"] == "https://tracker.transistorsoft.com/api/locations"


IOS_BARE_DICT_DUMP = """\
2026-07-28 12:37:03.521 {
    allowStale = 1;
    desiredAccuracy = 10;
    extras = "<null>";
    finished = 0;
    geofenceEvent =     {
        action = EXIT;
        identifier = Test;
        timestamp = "2026-07-28T16:37:03.521Z";
    };
    label = "TSGeofenceTransition:EXIT:Test";
    maximumAge = 10000;
    persist = 1;
    samples = 3;
    timeout = 30;
    type = 5;
}
"""


def test_ios_bare_dict_dump():
    cd = one(IOS, IOS_BARE_DICT_DUMP).structs["config_dump"]
    assert cd["truncated"] is False
    data = cd["data"]
    assert data["extras"] is None                 # "<null>" sentinel
    assert data["geofenceEvent"]["action"] == "EXIT"
    assert data["geofenceEvent"]["identifier"] == "Test"
    assert data["label"] == "TSGeofenceTransition:EXIT:Test"
    assert data["samples"] == 3


def test_ios_plist_truncated():
    text = ("2026-07-28 12:37:03.521 {\n"
            "    allowStale = 1;\n"
            "    geofenceEvent =     {\n"
            "        action = EXIT;\n")
    cd = one(IOS, text).structs["config_dump"]
    assert cd["truncated"] is True
    assert cd["data"]["allowStale"] == 1
    assert cd["data"]["geofenceEvent"]["action"] == "EXIT"


def test_ios_nsset_dump():
    text = ("2026-07-28 12:19:50.483 ✅ -[TSBackgroundTaskManager "
            "stopBackgroundTask:]_block_invoke 2 OF {(\n    2\n)}\n")
    rec = one(IOS, text)  # NSSet in prose position — must simply not raise
    assert rec.raw.endswith(")}")


# ── HTTP ─────────────────────────────────────────────────────────────────────

def test_android_http_flush_count():
    text = ("07-03 23:36:44.452 INFO [HttpService flush] \n"
            "╔═════════════════════════════════════════════\n"
            "║ HTTP Service (count: 1)\n"
            "╠═════════════════════════════════════════════\n")
    assert one(ANDROID, text).structs["http"]["count"] == 1


def test_android_http_response():
    text = ("07-03 23:36:44.779 INFO [HttpService$HttpCallback onResponse] \n"
            "  🔵  Response: 200\n")
    assert one(ANDROID, text).structs["http"]["status"] == 200


def test_android_http_post_uuid():
    text = ("07-03 23:36:44.527 INFO [HttpService createRequest] \n"
            "  🔵  HTTP POST: 55c8b45c-097b-4146-8a67-0417cbf22a45\n")
    assert one(ANDROID, text).structs["http"]["post_uuid"] == \
        "55c8b45c-097b-4146-8a67-0417cbf22a45"


def test_ios_http_post_status():
    text = ("2026-07-28 12:37:04.359 🔵 -[TSHttpService doPost:callback:]_block_invoke "
            "flush=B5EBDE12-F56A-4DFF-A849-2CF699C15D0E post status=200 retry=0 busy=1\n")
    h = one(IOS, text).structs["http"]
    assert h["status"] == 200
    assert h["retry"] == 0
    assert h["busy"] == 1
    assert h["flush"] == "B5EBDE12-F56A-4DFF-A849-2CF699C15D0E"


def test_ios_http_finish():
    text = ("2026-07-28 12:10:34.277 \n"
            "╔═══════════════════════════════════════════════════════════\n"
            "║ -[TSHttpService finish:error:] success=1 queued_before=0 synced=0 pages=0 duration_ms=0\n"
            "╚═══════════════════════════════════════════════════════════\n")
    h = one(IOS, text).structs["http"]
    assert h["success"] is True
    assert h["queued_before"] == 0
    assert h["synced"] == 0
    assert h["pages"] == 0
    assert h["duration_ms"] == 0


# The 3-part HTTP-error record — verbatim from the big iOS capture:
# ⚠ status-0 header line + lone '*' + NSError dict.
IOS_HTTP_ERROR_3PART = r"""2026-08-12 16:16:00.019 ⚠️ -[TSHttpResponse handleResponse] HTTP ERROR: 0* The request timed out.
*
{
    NSErrorFailingURLKey = "https://tracker.transistorsoft.com/api/locations";
    NSErrorFailingURLStringKey = "https://tracker.transistorsoft.com/api/locations";
    NSLocalizedDescription = "The request timed out.";
    NSUnderlyingError = "Error Domain=kCFErrorDomainCFNetwork Code=-1001 \"(null)\" UserInfo={_kCFStreamErrorCodeKey=-2102, _kCFStreamErrorDomainKey=4}";
    "_NSURLErrorFailingURLSessionTaskErrorKey" = "LocalDataTask <BF4833B5-04DD-4EA3-8305-8018483DC06F>.<1>";
    "_NSURLErrorRelatedURLSessionTaskErrorKey" =     (
        "LocalDataTask <BF4833B5-04DD-4EA3-8305-8018483DC06F>.<1>"
    );
    "_kCFStreamErrorCodeKey" = "-2102";
    "_kCFStreamErrorDomainKey" = 4;
}
"""


def test_ios_http_error_3part():
    rec = one(IOS, IOS_HTTP_ERROR_3PART)
    assert rec.body[0] == "*"                     # the lone-star middle part
    h = rec.structs["http"]
    assert h["status"] == 0
    assert h["error"] == "The request timed out."
    assert h["url"] == "https://tracker.transistorsoft.com/api/locations"
    err = rec.structs["nserror"]
    assert err["desc"] == "The request timed out."
    assert err["domain"] == "kCFErrorDomainCFNetwork"
    assert err["code"] == -1001
    # the NSError dict must NOT masquerade as a config dump
    assert "config_dump" not in rec.structs


def test_ios_nserror_inline():
    text = ("2026-08-10 17:35:23.083 ⚠️ -[TSTrackingService "
            'locationManager:didFailWithError:] Error Domain=kCLErrorDomain Code=0 "(null)"\n')
    err = one(IOS, text).structs["nserror"]
    assert err["domain"] == "kCLErrorDomain"
    assert err["code"] == 0
    assert err["desc"] is None                    # "(null)" → None


def test_ios_nserror_inline_with_userinfo():
    text = ("2026-08-10 17:43:02.401 ⚠️ -[TSTrackingService updateCurrentState]_block_invoke "
            "[motionState] Failed to get motionState location: Error Domain=TSSingleLocationRequest "
            'Code=404 "No location available." UserInfo={NSLocalizedDescription=No location available.}\n')
    err = one(IOS, text).structs["nserror"]
    assert err["domain"] == "TSSingleLocationRequest"
    assert err["code"] == 404
    assert err["desc"] == "No location available."


# ── geofence action/identifier ───────────────────────────────────────────────

def test_ios_geofence_action_banner():
    text = ("2026-07-28 12:37:05.392 \n"
            "╔═══════════════════════════════════════════════════════════\n"
            "║ -[TSGeofenceTransition setTriggerLocation:] 📢 EXIT Geofence: Test\n"
            "╚═══════════════════════════════════════════════════════════\n")
    gf = one(IOS, text).structs["geofence"]
    assert gf == {"action": "EXIT", "identifier": "Test"}


ANDROID_GEOFENCE_EVENT = """\
07-03 23:48:08.533 INFO [GeofencingProcessor handleGeofencingEvent]
╔═════════════════════════════════════════════
║ Geofencing Event: ENTER
╠═════════════════════════════════════════════
╟─ LP-1783136531
╚═════════════════════════════════════════════
"""


def test_android_geofencing_event_banner():
    gf = one(ANDROID, ANDROID_GEOFENCE_EVENT).structs["geofence"]
    assert gf == {"action": "ENTER", "identifier": "LP-1783136531"}


def test_android_geofence_trigger_banner_location():
    text = """\
07-03 23:48:08.535 INFO [GeofencingProcessor logTriggerVsGeofence]
╔═════════════════════════════════════════════
║ Trigger vs Geofence center: LP-1783136531
╠═════════════════════════════════════════════
╟─ 📍 Trigger=Location[gps 45.518228,-73.593216 hAcc=2.13378 et=+10h36m31s850ms alt=0.0 vel=13.888889 bear=300.73547 mock]
╟─ dist=114.367516m radius=150.0m
╟─ minPossibleDist=112.233734m (insideBy=-37.766266m)
╟─ triggerAge=166ms provider=gps
╚═════════════════════════════════════════════
"""
    rec = one(ANDROID, text)
    loc = rec.structs["location"]
    assert loc["lat"] == 45.518228
    assert loc["mock"] is True


# ── A/B comparison rows ──────────────────────────────────────────────────────

IOS_AB = """\
2026-07-28 12:37:05.392 🔵 +[TSLocationHelper pickBestLocationBetween:and:desiredAccuracy:] desiredAccuracy: 10.000000
- A: 📍<+45.51787621,-73.59745019> +/- 16.10m (speed 7.41 mps / course -1.00) @ 2026-07-28, 12:37:05 PM Eastern Daylight Time
- B: 📍<+45.51787621,-73.59745019> +/- 16.10m (speed 7.41 mps / course -1.00) @ 2026-07-28, 12:37:05 PM Eastern Daylight Time
"""


def test_ios_ab_compare():
    rec = one(IOS, IOS_AB)
    rows = rec.structs["ab_compare"]
    assert [r["label"] for r in rows] == ["A", "B"]
    assert rows[0]["lat"] == 45.51787621
    assert rows[0]["acc"] == 16.10
    assert rows[0]["speed"] == 7.41
    assert rows[0]["course"] is None
    # A/B pins must not leak into the record's own location keys
    assert "location" not in rec.structs
    assert "locations" not in rec.structs


ANDROID_AB = """\
07-03 23:48:08.536 INFO [GeofencingProcessor logLocationDiff]
╔═════════════════════════════════════════════
║ Trigger vs last location: LP-1783136531
╠═════════════════════════════════════════════
╟─ A: 45.51822826108341,-73.5932158986477 acc=2.13378 age=167ms provider=gps
╟─ B: 45.518297104076,-73.59233309571617 acc=2.0006514 age=7167ms provider=gps
╟─ distance=69.4m
╟─ Δt(A-B)=6999ms
╟─ requiredSpeed=9.92 m/s (35.7 km/h)
╚═════════════════════════════════════════════
"""


def test_android_ab_compare():
    rows = one(ANDROID, ANDROID_AB).structs["ab_compare"]
    assert len(rows) == 2
    assert rows[0] == {"label": "A", "lat": 45.51822826108341,
                       "lon": -73.5932158986477, "acc": 2.13378,
                       "age_ms": 167, "provider": "gps"}
    assert rows[1]["age_ms"] == 7167


# ── robustness: nothing ever raises ──────────────────────────────────────────

GARBAGE = [
    "Location[",
    "Location[fused ",
    "Location[fused 45.5",
    "LocationFilterResult{",
    "LocationFilterResult{decision=",
    "DetectedActivity [type=",
    "Intent { act=",
    "Intent { act=x cmp=",
    "📍<",
    "📍<+45.5,",
    "1:📍<+45.5,-73.6> +/- ",
    "<CMMotionActivity ",
    "<CMMotionActivity st:",
    "HTTP ERROR: ",
    "HTTP Service (count: ",
    "Response: ",
    "Error Domain= Code=",
    "📢 ENTER Geofence:",
    "Geofencing Event: ",
    "- A: ",
    "╟─ A: junk",
    "decision=",
    "queued_before=",
    " post status=",
    "{",
]


@pytest.mark.parametrize("payload", GARBAGE)
def test_parsers_never_raise_on_garbage(payload):
    for platform, header in ((ANDROID, "07-03 23:36:43.936 INFO [X y] "),
                             (IOS, "2026-07-28 12:10:34.284 🔵 -[X y] ")):
        text = f"{header}{payload}\n"
        recs = records.assemble(platform, text, 2026)
        for r in recs:
            structs.annotate(r)          # must not raise
        # also as a body continuation line
        text = f"{header}\n{payload}\n"
        recs = records.assemble(platform, text, 2026)
        for r in recs:
            structs.annotate(r)


def test_annotate_never_mutates_raw_or_body():
    for text, platform in ((ANDROID_CONFIG_TRUNCATED, ANDROID),
                           (IOS_HTTP_ERROR_3PART, IOS)):
        recs = records.assemble(platform, text, 2026)
        before = [(r.raw, list(r.body)) for r in recs]
        for r in recs:
            structs.annotate(r)
        assert [(r.raw, list(r.body)) for r in recs] == before


# ── integration over the real fixture corpus (skipped when absent) ───────────

ANDROID_FIXTURE = fixture("log62")
IOS_FIXTURE = fixture("bike")


@pytest.mark.skipif(ANDROID_FIXTURE is None, reason="fixture corpus not present")
def test_integration_android_fixture():
    recs = records.assemble(ANDROID, read(ANDROID_FIXTURE), 2026)
    for r in recs:
        structs.annotate(r)
    keys = {k for r in recs for k in r.structs}
    assert {"location", "filter_result", "detected_activity", "intent",
            "config_dump", "http", "geofence", "ab_compare"} <= keys
    locs = [r.structs["location"] for r in recs if "location" in r.structs]
    assert len(locs) > 1000
    assert all("lat" in l and "lon" in l for l in locs)
    cfg = next(r.structs["config_dump"] for r in recs if "config_dump" in r.structs)
    assert cfg["truncated"] is True                  # Android dumps hit the line cap
    assert cfg["data"]["app"]["heartbeatInterval"] == 60
    # no scratch keys leak
    assert not any(k.startswith("_") for k in keys)


@pytest.mark.skipif(IOS_FIXTURE is None, reason="fixture corpus not present")
def test_integration_ios_fixture():
    recs = records.assemble(IOS, read(IOS_FIXTURE), 2026)
    for r in recs:
        structs.annotate(r)
    keys = {k for r in recs for k in r.structs}
    assert {"location", "locations", "filter_result", "motion_activity",
            "config_dump", "http", "geofence", "ab_compare"} <= keys
    batched = [r for r in recs if "locations" in r.structs]
    assert batched and all(
        any("batch_index" in l for l in r.structs["locations"]) or
        len(r.structs["locations"]) > 1
        for r in batched)
    cfg = next(r.structs["config_dump"] for r in recs
               if "config_dump" in r.structs and "authorization" in (r.structs["config_dump"]["data"] or {}))
    assert cfg["data"]["authorization"]["strategy"] == "JWT"
    assert not any(k.startswith("_") for k in keys)
