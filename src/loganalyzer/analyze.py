"""Stage 4 — analysis. Turns assembled/segmented records into an Analysis tree.

The Analysis dataclass tree is the contract consumed by emit/digest.py and
emit/geojson.py: a PLAIN dataclass tree, JSON-serializable via
``dataclasses.asdict`` (all timestamps are ISO-8601 strings, never datetime).

Design rules honored here (design v2):
- gap threshold 15 min; gap + fresh init banner => death; Android gap bounded
  by "Schedule alarm fired!" => scheduler-window; iOS gap w/o banner =>
  suspension; Android remainder => wedge-candidate.
- iOS heartbeat cadence legitimately stretches 60s -> 6-7 min while suspended:
  EXPECTED, never an anomaly.
- Absence caveat: this module (and its notes) never asserts "X didn't happen",
  only "X does not appear in the capture".
- warn/error dedup key = (tag_class, tag_method, normalized message shape):
  digits/uuids/coords stripped. Groups carry ONE representative full raw text.
- structs/klass annotations are used when present but their absence is
  tolerated (structs.py / classify.py are built in parallel): every consumer
  feature-detects and falls back to cheap substring counting on rec.raw.
- Config defaults are unknown in v1: the header emits the parsed keys with a
  "defaults comparison: not yet implemented" marker instead of fake diffs.
"""
from __future__ import annotations

import re
import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .model import ANDROID, IOS, Record, Segment
from .sniff import Source

# Gap threshold (design v2): 15 minutes.
GAP_THRESHOLD_S = 900
# Android platform geofence registration hard limit (Play Services).
MAX_GEOFENCES = 97
# The absence caveat attached verbatim to the Analysis (digest renders it).
ABSENCE_NOTE = (
    "Emissions are debug-gated and conditional: this analysis only reports what "
    "does or does not APPEAR in the capture — it never asserts that an event did "
    "not happen."
)

# ── shared regexes ───────────────────────────────────────────────────────────

_BANNER = re.compile(r"TSLocationManager version:|TSLocationManager \(build ")
_SCHED_ALARM = "Schedule alarm fired"

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
_WS_RE = re.compile(r"\s+")
_BOX_PREFIX = re.compile(r"^[╔╠╚╟╢║═╣─┃\s]+")

_AND_FG = re.compile(r"\[LifecycleManager (?:handleOnResume|handleOnStart)|onWindowFocusChanged: true")
_AND_BG = re.compile(r"\[LifecycleManager (?:handleOnPause|handleOnStop)|onWindowFocusChanged: false")

_CONN_ANDROID = re.compile(r"Connectivity change: connected\? (true|false)")
_CONN_IOS = re.compile(r"Network: (\w+) \| Flags: (\S+)")
_HTTP_STATUS = re.compile(r"Response: (\d+)")
_HTTP_QUEUE_ANDROID = re.compile(r"HTTP Service \(count: (\d+)\)")
_HTTP_QUEUE_IOS = re.compile(r"queued_before=(\d+) synced=(\d+)")
_RETRY = re.compile(r"\bretry=(\d+)")
_URL_RE = re.compile(r"https?://[^\s\"'<>,;)\]]+")

_GEOFENCE_TRANSITION = re.compile(r"[Gg]eofence\s+(ENTER|EXIT|DWELL)")
_MONITORING_N = re.compile(r"monitoring (\d+)/(\d+)")
_FOUND_N = re.compile(r"Found (\d+) / (\d+) within")
_LOC_AVAILABILITY = re.compile(r"Location availability: (true|false)")

_DETECTED_ACTIVITY = re.compile(r"DetectedActivity \[type=(\w+), confidence=(\d+)\]")
_IOS_ACTIVITY = re.compile(r"onMotionActivityChange:\] \| (\w+)/(\d+) \| isMoving: (\d)")
_PACE_ANDROID = re.compile(r"motionchange: (true|false)|Acquired motionchange position, isMoving: (true|false)")
_PACE_IOS = re.compile(r"changePace:\] isMoving: (\d)")

_ENABLED_RE = re.compile(r"[Ee]nabled: (true|false|1|0)")
_ISMOVING_RE = re.compile(r"isMoving: (true|false|1|0)")

_JSON_KEY_LINE = re.compile(r'^\s*"[A-Za-z_]\w*"\s*:', re.MULTILINE)
_PLIST_KEY_LINE = re.compile(r"^\s+[A-Za-z_]\w*\s*=\s*.*;$", re.MULTILINE)
_JSON_TOP_KEY = re.compile(r'^  "([A-Za-z_]\w*)"\s*:', re.MULTILINE)
_PLIST_TOP_KEY = re.compile(r"^    ([A-Za-z_]\w*)\s*=", re.MULTILINE)

# Triage-relevant config keys surfaced in the header (effective values).
_NOTABLE_KEYS = (
    "logLevel", "debug", "url", "autoSync", "autoSyncThreshold", "batchSync",
    "heartbeatInterval", "distanceFilter", "desiredAccuracy", "stopTimeout",
    "stopOnTerminate", "startOnBoot", "foregroundService", "enableHeadless",
    "preventSuspend", "useSignificantChangesOnly", "geofenceModeHighAccuracy",
    "minimumActivityRecognitionConfidence", "schedule", "maxRecordsToPersist",
)


# ── reading an Analysis ──────────────────────────────────────────────────────
# An Analysis is a dataclass tree in-process and a plain dict after a JSON
# round-trip (dataclasses.asdict). Everything downstream — digest, geojson,
# sessions — has to read it either way, so the accessors live here with the
# thing they read rather than being re-invented per consumer.

def iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat(sep=" ", timespec="milliseconds") if ts else None


def parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def field(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access, so a dataclass tree and its JSON round-trip
    read identically."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def gap_list(analysis: Any) -> list:
    """The analyzer's classified silences (>= GAP_THRESHOLD_S), or []."""
    timeline = field(analysis, "timeline")
    return list(field(timeline, "gaps", []) or []) if timeline is not None else []


_iso = iso          # internal alias: this module's own call sites


def _shape(text: str) -> str:
    """Normalized message shape for dedup keys: uuids/digits/coords collapsed."""
    t = _UUID_RE.sub("«id»", text)
    t = _NUM_RE.sub("#", t)
    t = _WS_RE.sub(" ", t).strip()
    return t[:200]


def _content_lines(rec: Record, n: int = 2) -> list[str]:
    """First n human-message lines of a record (box-drawing rows stripped)."""
    out: list[str] = []
    for line in [rec.header_msg, *rec.body]:
        s = _BOX_PREFIX.sub("", line).strip()
        if s and any(c.isalpha() for c in s):
            out.append(s)
            if len(out) == n:
                break
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Dataclass tree
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class SourceInfo:
    path: str                       # input file path as given
    kind: str                       # "text" | "db"
    platform: str                   # "android" | "ios" | "unknown"
    filename_hint: str              # device hint from filename — UNTRUSTED label
    duplicate_of: Optional[str]     # path this input duplicates (byte-identical/prefix), else None
    notes: list[str]                # sniff-stage notes (e.g. unrecognized grammar)


@dataclass
class ConfigReport:
    present: bool                   # True when a config dump was found in the capture
    dump_count: int                 # how many config dumps appear (one per launch is typical)
    keys: list[str]                 # top-level config keys observed in the first dump
    notable: dict[str, Any]         # effective values for triage keys (logLevel/debug/autoSync/…)
    effective_log_level: str        # config logLevel, or inferred from observed record levels
    authorization_present: bool     # an authorization group exists (values NEVER carried here)
    url: Optional[str]              # config upload url (digest masks it; JSON is local-only)
    url_classification: Optional[str]  # private-lan | cleartext-http | https-public | other
    truncated: Optional[bool]       # dump looked cut off (unbalanced braces) — None if unknown
    defaults_note: str              # v1 marker: defaults comparison not yet implemented


@dataclass
class Header:
    sources: list[SourceInfo]       # every input file, with dedupe/untrusted-hint labels
    platform: str                   # platform of the analyzed records
    device: str                     # device model from banner, or "unknown — not present in capture"
    app_id: str                     # application/bundle id from banner, or degraded marker
    sdk_versions: list[str]         # distinct SDK versions seen across segments
    observed_levels: dict[str, int]  # DEBUG/INFO/WARN/ERROR record histogram (empty for iOS text)
    config: ConfigReport            # parsed config dump report
    log_start: Optional[str]        # first record timestamp (ISO, device-local)
    log_end: Optional[str]          # last record timestamp (ISO, device-local)
    duration_s: Optional[float]     # log span in seconds (end - start)
    record_count: int               # total records analyzed


@dataclass
class SegmentReport:
    index: int                      # segment index (== Record.segment)
    version: Optional[str]          # SDK version, None when no banner resolved it
    version_source: str             # "banner" | "build-map" | "unknown"
    start_ts: Optional[str]         # first record ts in segment (ISO)
    end_ts: Optional[str]           # last record ts in segment (ISO)
    duration_s: Optional[float]     # segment span in seconds
    record_count: int               # records in this segment
    launched_headless: bool         # iOS: segment booted in background


@dataclass
class StateInterval:
    state: str                      # foreground | background | headless | unknown
    start_seq: int                  # first record seq of the interval
    end_seq: int                    # last record seq of the interval
    start_ts: Optional[str]         # interval start (ISO)
    end_ts: Optional[str]           # interval end (ISO)


@dataclass
class Gap:
    start_ts: str                   # last record ts before the silence (ISO)
    end_ts: str                     # first record ts after the silence (ISO)
    duration_s: float               # silence length in seconds (>= 900)
    classification: str             # death | scheduler-window | suspension | wedge-candidate
    before_seq: int                 # seq of the record preceding the gap
    after_seq: int                  # seq of the record ending the gap
    app_state: str                  # app-state lane value going INTO the gap
    evidence: str                   # what bounded the gap (banner line / alarm line / "none")


@dataclass
class Timeline:
    segments: list[SegmentReport]   # process lifetimes with per-segment version
    app_state: list[StateInterval]  # foreground/background/headless lane
    gaps: list[Gap]                 # classified silences >= threshold
    gap_threshold_s: int            # the 15-minute rule, exposed for the digest


@dataclass
class DedupGroup:
    tag_class: str                  # tag class of the site ("" when untagged)
    tag_method: str                 # tag method of the site ("" when untagged)
    severity: str                   # "warning" | "error" (union of level and icon)
    shape: str                      # normalized message shape used as the group key
    count: int                      # occurrences in the capture
    first_ts: Optional[str]         # first occurrence (ISO)
    last_ts: Optional[str]          # last occurrence (ISO)
    sites: list[str]                # source file:line candidates from classification (may be [])
    app_states: dict[str, int]      # occurrences per app-state lane value
    representative: str             # FULL raw text of one member record (verbatim)


@dataclass
class PairReport:
    name: str                       # pair family, e.g. "FgsLaunchGate GATE CLOSE/OPEN"
    first_label: str                # the "opening" side label (e.g. GATE CLOSE)
    second_label: str               # the "closing" side label (e.g. GATE OPEN)
    firsts: int                     # opening-side record count
    seconds: int                    # closing-side record count
    paired: int                     # sequentially matched pairs
    unpaired_first: int             # openings never closed (interesting leftovers)
    orphan_second: int              # closings with no prior opening


@dataclass
class ParityReport:
    persisted: int                  # location records persisted to the DB
    posted: int                     # HTTP post attempts for records
    destroyed: int                  # records destroyed (successful post + cleanup)
    autosync: Optional[bool]        # config autoSync value when known
    imbalance: bool                 # persisted >> destroyed while autoSync is on
    imbalance_note: str             # human summary of the imbalance ("" when balanced)
    bg_task_starts: int             # background-task starts
    bg_task_stops: int              # background-task stops (iOS failed-stop lines excluded)
    bg_task_unpaired: int           # starts minus stops (positive = mid-drain truncation hint)


@dataclass
class ConnectivityEvent:
    ts: Optional[str]               # event time (ISO)
    connected: Optional[bool]       # reachable/connected state (None when undecodable)
    detail: str                     # the connectivity line, verbatim (first line)


@dataclass
class UrlReport:
    url: str                        # URL as it appears (digest masks; JSON is local-only)
    classification: str             # private-lan | cleartext-http | https-public | other
    count: int                      # occurrences across the capture


@dataclass
class HttpHealth:
    post_attempts: int              # HTTP POST / schedulePost attempts
    statuses: dict[str, int]        # response-status histogram ("200": n, …)
    retries: int                    # responses carrying retry>0 markers
    flush_attempts: int             # flush invocations observed
    watchdog_arms: int              # flush-watchdog arm events
    watchdog_fires: int             # watchdog fired / force-unlock events
    final_queue_depth: Optional[int]  # last observed queue depth (None = not observable)
    queue_depth_evidence: str       # the line the final depth was read from
    connectivity: list[ConnectivityEvent]  # connectivity-change timeline
    flushes_after_connectivity: int  # flush attempts within 60s of a connected event
    urls: list[UrlReport]           # URL classification (verdict survives redaction)


@dataclass
class AvailabilityEvent:
    ts: Optional[str]               # event time (ISO)
    available: bool                 # location-provider availability state


@dataclass
class GeofenceHealth:
    enters: int                     # geofence ENTER transitions
    exits: int                      # geofence EXIT transitions
    dwells: int                     # geofence DWELL transitions
    spurious: int                   # "Ignoring spurious geofence …" deliveries
    registrations: int              # registration-side events (start-monitoring / add)
    removals: int                   # stop-monitoring / remove events
    stale_cleanups: int             # stale-geofence cleanup events
    registered_max: int             # max simultaneously-registered count observed
    max_geofences: int              # platform registration cap (97) for comparison
    availability_events: list[AvailabilityEvent]  # location-availability timeline
    availability_flaps: int         # availability state transitions
    note: str                       # e.g. "0 transitions despite 8 registered geofences"


@dataclass
class MotionHealth:
    activity_histogram: dict[str, int]  # activity type -> event count
    confidence_median: Optional[float]  # median activity confidence
    below_confidence_threshold: int  # events below the configured minimum confidence
    trigger_armed: int              # motion-trigger timer armed count
    trigger_reset: int              # motion-trigger timer reset count
    stationary_exits: int           # stationary-region exit events
    stoptimeout_engaged: int        # stopTimeout timers started
    stoptimeout_fired: int          # stopTimeout timers that fired
    stoptimeout_cancelled: int      # stopTimeout timers cancelled (movement resumed)
    pace_changes: int               # isMoving state flips observed
    moving_ratio: Optional[float]   # fraction of the observed span spent moving (approx.)


@dataclass
class PowerEvent:
    ts: Optional[str]               # event time (ISO)
    state_hint: str                 # "on" | "off" | "unknown" (from the record's icon)
    detail: str                     # the power-save line, verbatim (first line)


@dataclass
class HeartbeatSegment:
    segment: int                    # segment index
    expected_interval_s: Optional[float]  # config heartbeatInterval (None = config unknown)
    count: int                      # heartbeat records in this segment
    median_interval_s: Optional[float]  # observed median cadence
    max_interval_s: Optional[float]  # observed max cadence
    stretched: bool                 # median > 1.5x expected
    stretch_expected: bool          # iOS suspension stretch (60s -> 6-7 min) — EXPECTED, not anomalous
    note: str                       # cadence commentary ("" when nominal)


@dataclass
class DutyCycle:
    span_hours: Optional[float]     # wall-clock hours from first to last record
    active_hours: Optional[float]   # span minus classified-gap time (approx. active logging)
    fixes_per_hour: Optional[float]  # persisted locations per active hour
    pct_time_moving: Optional[float]  # % of observed span in the moving state (approx.)
    prevent_suspend_count: int      # iOS prevent-suspend timer fires


@dataclass
class PowerHealth:
    power_save_events: list[PowerEvent]  # power-save-mode change timeline
    heartbeat: list[HeartbeatSegment]    # per-segment cadence, expected vs observed
    duty_cycle: DutyCycle           # battery-ticket rollup


@dataclass
class AuthEvent:
    ts: Optional[str]               # event time (ISO)
    tag: str                        # "Class.method" — the tag encodes which gate passed
    raw: str                        # the auth record, verbatim (full raw)


@dataclass
class AuthReport:
    total: int                      # all auth-related records in the capture
    events: list[AuthEvent]         # verbatim quotes (capped; first + last kept)
    truncated: bool                 # True when events were capped
    note: str                       # caveat: unobservable auth states are never asserted healthy


@dataclass
class EndState:
    last_ts: Optional[str]          # final record timestamp (ISO)
    enabled: Optional[bool]         # last observed enabled state (None = does not appear)
    is_moving: Optional[bool]       # last observed pace (None = does not appear)
    last_auth: Optional[str]        # first line of the last auth-related record
    last_connectivity: Optional[bool]  # last observed connectivity state
    queue_depth: Optional[int]      # last observed HTTP queue depth
    abrupt_end: bool                # final record looks truncated mid-emission
    abrupt_end_evidence: str        # why the end looks abrupt ("" when clean)
    tail_record: str                # final record raw (capped at 1000 chars)


@dataclass
class Anomaly:
    kind: str                       # machine key, e.g. "sync-backlog", "wedge-candidate-gap"
    severity: str                   # "high" | "warn" | "info"
    ts: Optional[str]               # anchor time when the anomaly is point-like (ISO)
    detail: str                     # human summary (absence-safe phrasing)


@dataclass
class UnknownSummary:
    classified: bool                # True when classify.py annotations were present
    total: int                      # total records
    matched: int                    # classification status == matched
    drift: int                      # status == drift (probable pattern drift — low interest)
    unknown: int                    # status == unknown (novel — high interest)
    passthrough: int                # bridge passthrough records (app-authored bodies)
    unclassified: int               # records with no classification annotation at all
    unknown_rate: Optional[float]   # unknown / total (None when classification absent)
    regen_warning: bool             # unknown-rate > 5% => vocabulary regeneration advised
    drift_examples: list[str]       # up to 5 first-lines of drift records
    novel_examples: list[str]       # up to 5 first-lines of novel-unknown records


@dataclass
class Analysis:
    header: Header                  # device/SDK/config header (with degraded-behavior markers)
    timeline: Timeline              # segments + app-state lane + classified gaps
    warning_groups: list[DedupGroup]  # deduped WARN groups, most frequent first
    error_groups: list[DedupGroup]  # deduped ERROR groups, most frequent first
    pairs: list[PairReport]         # GATE OPEN/CLOSE and delivery PAUSE/RESUME collapse
    parity: ParityReport            # record-lifecycle parity (persist/post/destroy, bg-tasks)
    http: HttpHealth                # HTTP + connectivity + URL classification
    geofence: GeofenceHealth        # transitions AND registration-side health
    motion: MotionHealth            # activity histogram, triggers, stopTimeout, pace
    power: PowerHealth              # power-save timeline, heartbeat cadence, duty cycle
    auth: AuthReport                # verbatim auth timeline with tags
    end_state: EndState             # state at end of log
    anomalies: list[Anomaly]        # cross-cutting findings, highest severity first
    unknowns: UnknownSummary        # classification coverage (drift vs novel)
    absence_note: str               # the never-assert-absence caveat, verbatim
    excerpt: bool = False           # capture is a hand-pasted excerpt, not a full log:
                                    # its silences are where the author stopped copying,
                                    # so gap-derived wedge findings are suppressed


# ═════════════════════════════════════════════════════════════════════════════
# App-state lane
# ═════════════════════════════════════════════════════════════════════════════

class _Lane:
    """Boundaries [(seq, state)] + bisect lookup for state-at-record queries."""

    def __init__(self, records: list[Record], segments: list[Segment]):
        self.boundaries: list[tuple[int, str]] = []
        if not records:
            return
        seg_by_first = {s.first_seq: s for s in segments}
        state = "unknown"
        self.boundaries.append((records[0].seq, state))

        def push(seq: int, new_state: str) -> None:
            nonlocal state
            if new_state == state:
                return
            if self.boundaries and self.boundaries[-1][0] == seq:
                self.boundaries[-1] = (seq, new_state)
            else:
                self.boundaries.append((seq, new_state))
            state = new_state

        first_seq = records[0].seq
        for rec in records:
            seg = seg_by_first.get(rec.seq)
            if seg is not None and rec.seq != first_seq:
                # Process relaunch: lane resets (headless when the boot says so).
                push(rec.seq, "headless" if seg.launched_headless else "unknown")
            raw = rec.raw
            if rec.platform == ANDROID:
                if "LifecycleManager" in raw or "onWindowFocusChanged" in raw:
                    if _AND_FG.search(raw):
                        push(rec.seq, "foreground")
                    elif _AND_BG.search(raw):
                        push(rec.seq, "background")
                if "💀" in raw and state != "foreground":
                    push(rec.seq, "headless")
                elif "headlessConfirmed=true" in raw and state == "unknown":
                    push(rec.seq, "headless")
            else:
                if "onEnterForeground" in raw or "applicationDidBecomeActive" in raw:
                    push(rec.seq, "foreground")
                elif "onEnterBackground" in raw:
                    push(rec.seq, "background")

        self._seqs = [b[0] for b in self.boundaries]

    def state_at(self, seq: int) -> str:
        if not self.boundaries:
            return "unknown"
        i = bisect_right(self._seqs, seq) - 1
        return self.boundaries[max(i, 0)][1]

    def intervals(self, records: list[Record]) -> list[StateInterval]:
        if not records:
            return []
        by_seq = {r.seq: r for r in records}
        out: list[StateInterval] = []
        for i, (seq, st) in enumerate(self.boundaries):
            end_seq = (self.boundaries[i + 1][0] - 1) if i + 1 < len(self.boundaries) else records[-1].seq
            start_rec = by_seq.get(seq)
            end_rec = by_seq.get(end_seq) or records[-1]
            out.append(StateInterval(
                state=st, start_seq=seq, end_seq=end_seq,
                start_ts=_iso(start_rec.ts if start_rec else None),
                end_ts=_iso(end_rec.ts if end_rec else None),
            ))
        return out


# ═════════════════════════════════════════════════════════════════════════════
# Builders
# ═════════════════════════════════════════════════════════════════════════════

def _classify_url(url: str) -> str:
    m = re.match(r"(https?)://([^/:?#]+)", url)
    if not m:
        return "other"
    scheme, host = m.group(1).lower(), m.group(2).lower()
    private = (
        host in ("localhost",) or host.startswith("127.") or host.startswith("10.")
        or host.startswith("192.168.") or re.match(r"172\.(1[6-9]|2\d|3[01])\.", host)
        or host.endswith(".local") or host.endswith(".lan")
    )
    if private:
        return "private-lan"
    if scheme == "http":
        return "cleartext-http"
    return "https-public"


def _structs_config(rec: Record) -> Optional[dict]:
    """Feature-detect a structs.py config_dump annotation (built in parallel:
    exact shape unknown — accept a dict, optionally carrying 'truncated' and
    either inline config keys or a nested config/data dict)."""
    cd = rec.structs.get("config_dump")
    if not isinstance(cd, dict):
        return None
    for k in ("config", "data", "values", "dump"):
        inner = cd.get(k)
        if isinstance(inner, dict):
            return {"_truncated": bool(cd.get("truncated", False)), **inner}
    rest = {k: v for k, v in cd.items() if k != "truncated"}
    if rest:
        return {"_truncated": bool(cd.get("truncated", False)), **rest}
    return None


def _find_scalar(cfg: Any, key: str) -> Any:
    """Depth-first first-occurrence scalar lookup in a nested config dict."""
    if isinstance(cfg, dict):
        if key in cfg and not isinstance(cfg[key], (dict, list)):
            return cfg[key]
        for v in cfg.values():
            got = _find_scalar(v, key)
            if got is not None:
                return got
    return None


def _parse_config(records: list[Record], platform: str,
                  observed_levels: dict[str, int]) -> ConfigReport:
    # A "dump candidate" is any record with a JSON/plist-shaped body; the real
    # config dump is recognized by a marker key ("distanceFilter") because
    # other structured dumps (location-request dicts, NSError userInfo) share
    # the plist shape but not the config keys.
    marker_recs: list[Record] = []
    generic_recs: list[Record] = []
    for rec in records:
        raw = rec.raw
        if platform == ANDROID:
            is_dump = "{" in raw and '":' in raw and len(_JSON_KEY_LINE.findall(raw)) >= 3
        else:
            is_dump = " = " in raw and ";" in raw and len(_PLIST_KEY_LINE.findall(raw)) >= 3
        if is_dump:
            (marker_recs if "distanceFilter" in raw else generic_recs).append(rec)
    dump_recs = marker_recs or generic_recs

    if not dump_recs:
        lvl = "unknown — no config dump in capture"
        if observed_levels.get("DEBUG"):
            lvl = "verbose/debug (inferred: DEBUG records appear; no config dump in capture)"
        return ConfigReport(
            present=False, dump_count=0, keys=[], notable={},
            effective_log_level=lvl, authorization_present=False,
            url=None, url_classification=None, truncated=None,
            defaults_note="defaults comparison: not yet implemented",
        )

    first = dump_recs[0]
    raw = first.raw
    notable: dict[str, Any] = {}
    keys: list[str] = []
    truncated: Optional[bool] = None
    authorization_present = False
    url: Optional[str] = None

    structs_cfg = _structs_config(first)
    if structs_cfg is not None:
        truncated = bool(structs_cfg.pop("_truncated", False))
        keys = sorted(k for k in structs_cfg.keys())
        authorization_present = "authorization" in structs_cfg
        for k in _NOTABLE_KEYS:
            v = _find_scalar(structs_cfg, k)
            if v is not None:
                notable[k] = v
        u = _find_scalar(structs_cfg, "url")
        url = str(u) if u not in (None, "") else None
    else:
        # Cheap regex fallback over the raw dump text (tolerates truncation).
        if platform == ANDROID:
            keys = sorted(set(_JSON_TOP_KEY.findall(raw)))
            authorization_present = '"authorization"' in raw
            for k in _NOTABLE_KEYS:
                m = re.search(rf'"{k}"\s*:\s*("([^"]*)"|[-\d.]+|true|false|null)', raw)
                if m:
                    val: Any = m.group(2) if m.group(2) is not None else m.group(1)
                    if val in ("true", "false"):
                        val = (val == "true")
                    notable[k] = val
            truncated = raw.count("{") != raw.count("}")
        else:
            keys = sorted(set(_PLIST_TOP_KEY.findall(raw)))
            authorization_present = bool(re.search(r"^\s+authorization\s*=", raw, re.MULTILINE))
            for k in _NOTABLE_KEYS:
                m = re.search(rf'\b{k}\s*=\s*("([^"]*)"|[^;\n]+);', raw)
                if m:
                    notable[k] = m.group(2) if m.group(2) is not None else m.group(1).strip()
            truncated = raw.count("{") != raw.count("}")
        u = notable.get("url")
        url = str(u) if u not in (None, "") else None

    lvl = notable.get("logLevel")
    if lvl is not None:
        effective = f"{lvl} (from config dump)"
    elif observed_levels.get("DEBUG"):
        effective = "verbose/debug (inferred: DEBUG records appear; logLevel key not in dump)"
    elif observed_levels:
        effective = f"unknown — observed levels: {sorted(observed_levels)}"
    else:
        effective = "unknown — iOS text export is level-blind"

    return ConfigReport(
        present=True, dump_count=len(dump_recs), keys=keys, notable=notable,
        effective_log_level=effective, authorization_present=authorization_present,
        url=url, url_classification=_classify_url(url) if url else None,
        truncated=truncated,
        defaults_note="defaults comparison: not yet implemented",
    )


def _build_header(records: list[Record], segments: list[Segment],
                  sources: list[Source], config: ConfigReport,
                  observed_levels: dict[str, int]) -> Header:
    platform = records[0].platform if records else (sources[0].platform if sources else "unknown")
    device = "unknown — not present in capture"
    app_id = "unknown — not present in capture"
    for rec in records:
        if _BANNER.search(rec.raw):
            for line in rec.raw.splitlines():
                if line.startswith("╟─"):
                    s = line.lstrip("╟─ ").strip()
                    if " @ " in s and device.startswith("unknown"):
                        device = s
                    elif "." in s and " " not in s and app_id.startswith("unknown"):
                        app_id = s
            if not device.startswith("unknown") and not app_id.startswith("unknown"):
                break

    versions: list[str] = []
    for seg in segments:
        if seg.version and seg.version not in versions:
            versions.append(seg.version)

    ts_first = next((r.ts for r in records if r.ts), None)
    ts_last = next((r.ts for r in reversed(records) if r.ts), None)
    return Header(
        sources=[SourceInfo(
            path=str(s.path), kind=s.kind, platform=s.platform,
            filename_hint=f"{s.filename_hint} (untrusted filename label)" if s.filename_hint else "",
            duplicate_of=str(s.duplicate_of) if s.duplicate_of else None,
            notes=list(s.notes),
        ) for s in sources],
        platform=platform, device=device, app_id=app_id, sdk_versions=versions,
        observed_levels=observed_levels, config=config,
        log_start=_iso(ts_first), log_end=_iso(ts_last),
        duration_s=(ts_last - ts_first).total_seconds() if ts_first and ts_last else None,
        record_count=len(records),
    )


def _build_timeline(records: list[Record], segments: list[Segment], lane: _Lane) -> Timeline:
    by_seq = {r.seq: r for r in records}
    seg_reports: list[SegmentReport] = []
    for seg in segments:
        a, b = by_seq.get(seg.first_seq), by_seq.get(seg.last_seq)
        dur = (b.ts - a.ts).total_seconds() if a and b and a.ts and b.ts else None
        seg_reports.append(SegmentReport(
            index=seg.index, version=seg.version, version_source=seg.version_source,
            start_ts=_iso(a.ts if a else None), end_ts=_iso(b.ts if b else None),
            duration_s=dur, record_count=seg.last_seq - seg.first_seq + 1,
            launched_headless=seg.launched_headless,
        ))

    gaps: list[Gap] = []
    prev: Optional[Record] = None
    prev_i = -1
    for i, rec in enumerate(records):
        if rec.ts is None:
            continue
        if prev is not None:
            dt = (rec.ts - prev.ts).total_seconds()
            if dt >= GAP_THRESHOLD_S:
                gaps.append(_classify_gap(records, prev, prev_i, rec, i, lane))
        prev, prev_i = rec, i
    return Timeline(segments=seg_reports, app_state=lane.intervals(records),
                    gaps=gaps, gap_threshold_s=GAP_THRESHOLD_S)


def _classify_gap(records: list[Record], before: Record, before_i: int,
                  after: Record, after_i: int, lane: _Lane) -> Gap:
    # Lookahead window after the gap: up to 8 records within 60s.
    look: list[Record] = []
    j = after_i
    while j < len(records) and len(look) < 8:
        r2 = records[j]
        if r2.ts is not None and after.ts is not None and (r2.ts - after.ts).total_seconds() > 60:
            break
        look.append(r2)
        j += 1

    fresh_banner: Optional[Record] = None
    alarm: Optional[Record] = None
    for r2 in look:
        if fresh_banner is None and (_BANNER.search(r2.raw) or r2.segment != before.segment):
            fresh_banner = r2
        if alarm is None and _SCHED_ALARM in r2.raw:
            alarm = r2

    if fresh_banner is not None:
        cls = "death"
        ev_rec = fresh_banner
    elif before.platform == ANDROID and alarm is not None:
        cls = "scheduler-window"
        ev_rec = alarm
    elif before.platform == IOS:
        cls = "suspension"
        ev_rec = None
    else:
        cls = "wedge-candidate"
        ev_rec = None

    if ev_rec is not None:
        content = _content_lines(ev_rec, 1)
        evidence = content[0] if content else ev_rec.raw.splitlines()[0][:160]
    else:
        evidence = "no fresh banner and no schedule alarm appear at the gap boundary"

    return Gap(
        start_ts=_iso(before.ts) or "", end_ts=_iso(after.ts) or "",
        duration_s=round((after.ts - before.ts).total_seconds(), 1),
        classification=cls, before_seq=before.seq, after_seq=after.seq,
        app_state=lane.state_at(before.seq), evidence=evidence,
    )


def _build_dedup_groups(records: list[Record], lane: _Lane) -> tuple[list[DedupGroup], list[DedupGroup]]:
    groups: dict[tuple, dict] = {}
    for rec in records:
        sev = rec.severity
        if sev == "normal":
            continue
        msg = " ".join(_content_lines(rec, 2))
        key = (rec.tag_class or "", rec.tag_method or "", sev, _shape(msg))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "first": rec, "last": rec, "count": 0,
                "states": {}, "sites": [],
            }
        g["count"] += 1
        g["last"] = rec
        st = lane.state_at(rec.seq)
        g["states"][st] = g["states"].get(st, 0) + 1
        if rec.klass is not None:
            for site in rec.klass.sites:
                if site not in g["sites"]:
                    g["sites"].append(site)

    warns: list[DedupGroup] = []
    errors: list[DedupGroup] = []
    for (tc, tm, sev, shape), g in groups.items():
        grp = DedupGroup(
            tag_class=tc, tag_method=tm, severity=sev, shape=shape,
            count=g["count"], first_ts=_iso(g["first"].ts), last_ts=_iso(g["last"].ts),
            sites=g["sites"], app_states=g["states"], representative=g["first"].raw,
        )
        (errors if sev == "error" else warns).append(grp)
    warns.sort(key=lambda g: -g.count)
    errors.sort(key=lambda g: -g.count)
    return warns, errors


def _pair_scan(records: list[Record], first_sub: str, second_sub: str,
               name: str, first_label: str, second_label: str) -> PairReport:
    firsts = seconds = paired = orphan = depth = 0
    for rec in records:
        raw = rec.raw
        if first_sub in raw:
            firsts += 1
            depth += 1
        if second_sub in raw:
            seconds += 1
            if depth > 0:
                depth -= 1
                paired += 1
            else:
                orphan += 1
    return PairReport(name=name, first_label=first_label, second_label=second_label,
                      firsts=firsts, seconds=seconds, paired=paired,
                      unpaired_first=depth, orphan_second=orphan)


def _build_parity(records: list[Record], config: ConfigReport) -> ParityReport:
    persisted = posted = destroyed = starts = stops = 0
    for rec in records:
        raw = rec.raw
        if rec.platform == ANDROID:
            if rec.tag_class == "SQLiteLocationDAO" and rec.tag_method == "persist":
                persisted += 1
            if "HTTP POST:" in raw:
                posted += 1
            if rec.tag_class == "SQLiteLocationDAO" and rec.tag_method == "destroy":
                destroyed += 1
            if "startBackgroundTask:" in raw:
                starts += 1
            if "stopBackgroundTask:" in raw:
                stops += 1
        else:
            if "TSDataStore persist" in raw and "INSERT:" in raw:
                persisted += 1
            if "schedulePost" in raw and "LOCKED:" in raw:
                posted += 1
            if "DESTROY:" in raw:
                destroyed += 1
            if "Created background task:" in raw:
                starts += 1
            if "stopBackgroundTask" in raw and "Failed to find" not in raw:
                stops += 1

    autosync_val = config.notable.get("autoSync")
    autosync: Optional[bool] = None
    if isinstance(autosync_val, bool):
        autosync = autosync_val
    elif isinstance(autosync_val, str):
        autosync = autosync_val.strip().lower() in ("true", "1", "yes")

    imbalance = bool(
        persisted >= 10 and destroyed < persisted * 0.5
        and (autosync is None or autosync)
    )
    note = ""
    if imbalance:
        auto_txt = f"autoSync: {autosync}" if autosync is not None else "autoSync: unknown"
        note = (f"{persisted} records persisted but only {destroyed} destroyed "
                f"({posted} post attempts appear; {auto_txt}) — upload/cleanup "
                f"does not appear to keep up with persistence")
    return ParityReport(
        persisted=persisted, posted=posted, destroyed=destroyed,
        autosync=autosync, imbalance=imbalance, imbalance_note=note,
        bg_task_starts=starts, bg_task_stops=stops, bg_task_unpaired=starts - stops,
    )


def _build_http(records: list[Record], config: ConfigReport) -> HttpHealth:
    statuses: dict[str, int] = {}
    retries = flush_attempts = watchdog_arms = watchdog_fires = post_attempts = 0
    queue_depth: Optional[int] = None
    queue_evidence = ""
    connectivity: list[ConnectivityEvent] = []
    flush_ts: list[datetime] = []
    url_counts: dict[str, int] = {}

    if config.url:
        url_counts[config.url] = url_counts.get(config.url, 0) + 1

    for rec in records:
        raw = rec.raw
        if "Response: " in raw:
            for st in _HTTP_STATUS.findall(raw):
                statuses[st] = statuses.get(st, 0) + 1
        if "retry=" in raw:
            for r in _RETRY.findall(raw):
                if int(r) > 0:
                    retries += 1
        if (rec.platform == ANDROID and rec.tag_class == "HttpService" and rec.tag_method == "flush") \
                or "beginFlushWithCallback" in raw:
            flush_attempts += 1
            if rec.ts:
                flush_ts.append(rec.ts)
        if "HTTP POST:" in raw or ("schedulePost" in raw and "LOCKED:" in raw):
            post_attempts += 1
        if "armFlushWatchdog" in raw or "watchdog_armed" in raw:
            watchdog_arms += 1
        if re.search(r"[Ww]atchdog.{0,20}(fired|force)|force.?unlock", raw):
            watchdog_fires += 1
        m = _HTTP_QUEUE_ANDROID.search(raw)
        if m:
            queue_depth = int(m.group(1))
            queue_evidence = f"HTTP Service (count: {queue_depth})"
        m = _HTTP_QUEUE_IOS.search(raw)
        if m:
            queue_depth = max(int(m.group(1)) - int(m.group(2)), 0)
            queue_evidence = m.group(0)
        m = _CONN_ANDROID.search(raw)
        if m:
            connectivity.append(ConnectivityEvent(
                ts=_iso(rec.ts), connected=(m.group(1) == "true"),
                detail=_content_lines(rec, 1)[0] if _content_lines(rec, 1) else ""))
        m = _CONN_IOS.search(raw)
        if m:
            iface, flags = m.groups()
            connectivity.append(ConnectivityEvent(
                ts=_iso(rec.ts), connected=("R" in flags.split()[0]),
                detail=f"Network: {iface} | Flags: {flags}"))
        if "http://" in raw or "https://" in raw:
            for u in _URL_RE.findall(raw):
                u = u.rstrip(".;\"'")
                if len(url_counts) < 20 or u in url_counts:
                    url_counts[u] = url_counts.get(u, 0) + 1

    flush_ts.sort()
    flushes_after = 0
    for ev in connectivity:
        if ev.connected and ev.ts:
            t0 = datetime.fromisoformat(ev.ts)
            i = bisect_right(flush_ts, t0)
            while i < len(flush_ts) and (flush_ts[i] - t0).total_seconds() <= 60:
                flushes_after += 1
                i += 1

    urls = [UrlReport(url=u, classification=_classify_url(u), count=c)
            for u, c in sorted(url_counts.items(), key=lambda kv: -kv[1])]
    return HttpHealth(
        post_attempts=post_attempts, statuses=statuses, retries=retries,
        flush_attempts=flush_attempts, watchdog_arms=watchdog_arms,
        watchdog_fires=watchdog_fires, final_queue_depth=queue_depth,
        queue_depth_evidence=queue_evidence, connectivity=connectivity,
        flushes_after_connectivity=flushes_after, urls=urls,
    )


def _build_geofence(records: list[Record]) -> GeofenceHealth:
    enters = exits = dwells = spurious = registrations = removals = stale = 0
    registered_max = 0
    avail: list[AvailabilityEvent] = []

    for rec in records:
        raw = rec.raw
        gf = rec.structs.get("geofence")
        if isinstance(gf, dict) and gf.get("action"):
            action = str(gf["action"]).upper()
            if "spurious" in raw or "Ignoring spurious" in raw:
                spurious += 1
            elif action == "ENTER":
                enters += 1
            elif action == "EXIT":
                exits += 1
            elif action == "DWELL":
                dwells += 1
        else:
            if "Ignoring spurious geofence" in raw:
                spurious += 1
            elif "eofence" in raw:
                for action in _GEOFENCE_TRANSITION.findall(raw):
                    if action == "ENTER":
                        enters += 1
                    elif action == "EXIT":
                        exits += 1
                    else:
                        dwells += 1
            if rec.platform == IOS and rec.tag_class == "TSGeofenceManager":
                tm = rec.tag_method or ""
                if tm.startswith("locationManager:didEnterRegion"):
                    enters += 1
                elif tm.startswith("locationManager:didExitRegion"):
                    exits += 1

        if "Start monitoring geofences" in raw or "startMonitoringForRegion" in raw:
            registrations += 1
        if "Stop monitoring geofences" in raw or "stopMonitoringForRegion" in raw:
            removals += 1
        if "eofence" in raw and "stale" in raw.lower():
            stale += 1
        m = _MONITORING_N.search(raw)
        if m:
            registered_max = max(registered_max, int(m.group(2)))
        m = _FOUND_N.search(raw)
        if m:
            registered_max = max(registered_max, int(m.group(2)))
        m = _LOC_AVAILABILITY.search(raw)
        if m:
            avail.append(AvailabilityEvent(ts=_iso(rec.ts), available=(m.group(1) == "true")))

    flaps = sum(1 for a, b in zip(avail, avail[1:]) if a.available != b.available)
    transitions = enters + exits + dwells
    if transitions == 0 and spurious == 0 and registered_max > 0:
        note = (f"0 geofence transitions appear despite {registered_max} registered "
                f"geofences — delivery-side silence")
    elif transitions == 0 and registered_max == 0:
        note = "no geofence registrations and no transitions appear in the capture"
    else:
        note = ""
    return GeofenceHealth(
        enters=enters, exits=exits, dwells=dwells, spurious=spurious,
        registrations=registrations, removals=removals, stale_cleanups=stale,
        registered_max=registered_max, max_geofences=MAX_GEOFENCES,
        availability_events=avail, availability_flaps=flaps, note=note,
    )


def _build_motion(records: list[Record], config: ConfigReport) -> MotionHealth:
    hist: dict[str, int] = {}
    confs: list[int] = []
    armed = reset = stat_exits = engaged = fired = cancelled = 0
    pace_events: list[tuple[datetime, bool]] = []

    min_conf = 75
    mc = config.notable.get("minimumActivityRecognitionConfidence")
    try:
        if mc is not None:
            min_conf = int(str(mc))
    except ValueError:
        pass

    for rec in records:
        raw = rec.raw
        da = rec.structs.get("detected_activity")
        if isinstance(da, dict) and da.get("type"):
            hist[str(da["type"])] = hist.get(str(da["type"]), 0) + 1
            try:
                confs.append(int(da.get("confidence", 0)))
            except (TypeError, ValueError):
                pass
        elif "DetectedActivity" in raw:
            for typ, conf in _DETECTED_ACTIVITY.findall(raw):
                hist[typ] = hist.get(typ, 0) + 1
                confs.append(int(conf))
        if "onMotionActivityChange" in raw:
            m = _IOS_ACTIVITY.search(raw)
            if m:
                typ, conf, _mv = m.groups()
                hist[typ] = hist.get(typ, 0) + 1
                confs.append(int(conf))
        if "startMotionTriggerTimer" in raw or "Motion-trigger timer engaged" in raw:
            armed += 1
        if "resetMotionTriggerTimer" in raw:
            reset += 1
        if "Exit stationary region" in raw or "exit stationary" in raw.lower():
            stat_exits += 1
        if "[stopTimeout] Starting timer" in raw or "Scheduled OneShot: STOP_TIMEOUT" in raw:
            engaged += 1
        if "stopTimeout fired" in raw or "OneShot event fired: STOP_TIMEOUT" in raw:
            fired += 1
        if "Cancel OneShot: STOP_TIMEOUT" in raw or ("stop] ⏰ [stopTimeout]" in raw):
            cancelled += 1
        if rec.ts is not None:
            if rec.platform == ANDROID and ("motionchange:" in raw or "isMoving:" in raw):
                m = _PACE_ANDROID.search(raw)
                if m:
                    val = m.group(1) or m.group(2)
                    pace_events.append((rec.ts, val == "true"))
            elif rec.platform == IOS and "changePace:" in raw:
                m = _PACE_IOS.search(raw)
                if m:
                    pace_events.append((rec.ts, m.group(1) == "1"))

    pace_changes = 0
    moving_ratio: Optional[float] = None
    if pace_events:
        last_state = pace_events[0][1]
        for _, st in pace_events[1:]:
            if st != last_state:
                pace_changes += 1
                last_state = st
        ts_last = next((r.ts for r in reversed(records) if r.ts), None)
        if ts_last is not None:
            total = (ts_last - pace_events[0][0]).total_seconds()
            if total > 0:
                moving = 0.0
                for i, (t, st) in enumerate(pace_events):
                    t_next = pace_events[i + 1][0] if i + 1 < len(pace_events) else ts_last
                    if st:
                        moving += (t_next - t).total_seconds()
                moving_ratio = round(moving / total, 4)

    below = sum(1 for c in confs if c < min_conf)
    return MotionHealth(
        activity_histogram=hist,
        confidence_median=(statistics.median(confs) if confs else None),
        below_confidence_threshold=below,
        trigger_armed=armed, trigger_reset=reset, stationary_exits=stat_exits,
        stoptimeout_engaged=engaged, stoptimeout_fired=fired,
        stoptimeout_cancelled=cancelled, pace_changes=pace_changes,
        moving_ratio=moving_ratio,
    )


def _build_power(records: list[Record], segments: list[Segment],
                 config: ConfigReport, parity: ParityReport,
                 gaps: list[Gap], motion: MotionHealth) -> PowerHealth:
    events: list[PowerEvent] = []
    prevent_suspend = 0
    hb_by_seg: dict[int, list[datetime]] = {}

    for rec in records:
        raw = rec.raw
        if "PowerSaveMode" in raw or "PowerSaveChangeReceiver" in rec.raw:
            icon = rec.icon or ""
            hint = "on" if ("🟢" in icon or "🎾" in icon) else ("off" if "🔴" in icon else "unknown")
            first = _content_lines(rec, 1)
            events.append(PowerEvent(ts=_iso(rec.ts), state_hint=hint,
                                     detail=first[0] if first else ""))
        if "onPreventSuspendTimer" in raw:
            prevent_suspend += 1
        if "onHeartbeat" in raw and rec.tag_class in ("HeartbeatService", "TSHeartbeatService"):
            if rec.ts is not None:
                hb_by_seg.setdefault(rec.segment, []).append(rec.ts)

    expected: Optional[float] = None
    hb_cfg = config.notable.get("heartbeatInterval")
    try:
        if hb_cfg is not None:
            expected = float(str(hb_cfg))
    except ValueError:
        pass

    platform = records[0].platform if records else ""
    hb_reports: list[HeartbeatSegment] = []
    for seg in segments:
        ts_list = hb_by_seg.get(seg.index, [])
        intervals = [(b - a).total_seconds() for a, b in zip(ts_list, ts_list[1:])]
        med = round(statistics.median(intervals), 1) if intervals else None
        mx = round(max(intervals), 1) if intervals else None
        stretched = bool(expected and med is not None and med > expected * 1.5)
        stretch_expected = bool(platform == IOS and stretched and mx is not None and mx <= 450)
        note = ""
        if not ts_list:
            note = "heartbeat records do not appear in this segment"
        elif stretch_expected:
            note = ("cadence stretched beyond config — EXPECTED on iOS while the app "
                    "is suspended (60s can legitimately stretch to 6-7 min)")
        elif stretched:
            note = f"median cadence {med}s exceeds 1.5x the configured {expected}s"
        hb_reports.append(HeartbeatSegment(
            segment=seg.index, expected_interval_s=expected, count=len(ts_list),
            median_interval_s=med, max_interval_s=mx,
            stretched=stretched, stretch_expected=stretch_expected, note=note,
        ))

    ts_first = next((r.ts for r in records if r.ts), None)
    ts_last = next((r.ts for r in reversed(records) if r.ts), None)
    span_h = active_h = fixes_h = None
    if ts_first and ts_last and ts_last > ts_first:
        span_h = round((ts_last - ts_first).total_seconds() / 3600, 2)
        gap_s = sum(g.duration_s for g in gaps)
        active_h = round(max(span_h - gap_s / 3600, 0.01), 2)
        fixes_h = round(parity.persisted / active_h, 1) if active_h else None
    pct_moving = round(motion.moving_ratio * 100, 1) if motion.moving_ratio is not None else None

    return PowerHealth(
        power_save_events=events, heartbeat=hb_reports,
        duty_cycle=DutyCycle(span_hours=span_h, active_hours=active_h,
                             fixes_per_hour=fixes_h, pct_time_moving=pct_moving,
                             prevent_suspend_count=prevent_suspend),
    )


_AUTH_TAG = re.compile(r"Authorization|Permission|Licens|licens|TransistorAuthorizationToken")


def _build_auth(records: list[Record]) -> AuthReport:
    events: list[AuthEvent] = []
    for rec in records:
        tag = f"{rec.tag_class or ''}.{rec.tag_method or ''}".strip(".")
        is_auth = bool(
            (rec.icon and "📌" in rec.icon and "🔒" in rec.icon)
            or (rec.tag_class and _AUTH_TAG.search(rec.tag_class))
            or "license" in rec.raw.lower()
        )
        if is_auth:
            events.append(AuthEvent(ts=_iso(rec.ts), tag=tag, raw=rec.raw))
    total = len(events)
    truncated = total > 40
    if truncated:
        events = events[:25] + events[-15:]
    return AuthReport(
        total=total, events=events, truncated=truncated,
        note=("auth lines are quoted verbatim with tags (the tag encodes which gate "
              "passed); states the log cannot observe are never asserted healthy"),
    )


def _build_end_state(records: list[Record], http: HttpHealth, auth: AuthReport) -> EndState:
    last_ts = next((r.ts for r in reversed(records) if r.ts), None)
    enabled: Optional[bool] = None
    moving: Optional[bool] = None
    for rec in reversed(records):
        raw = rec.raw
        if enabled is None:
            m = _ENABLED_RE.search(raw)
            if m:
                enabled = m.group(1) in ("true", "1")
        if moving is None:
            m = _ISMOVING_RE.search(raw)
            if m:
                moving = m.group(1) in ("true", "1")
        if enabled is not None and moving is not None:
            break

    last_auth = None
    if auth.events:
        first_line = auth.events[-1].raw.splitlines()[0]
        last_auth = first_line[:200]
    last_conn = None
    if http.connectivity:
        last_conn = http.connectivity[-1].connected

    abrupt = False
    evidence = ""
    if records:
        tail = records[-1]
        opens, closes = tail.raw.count("{"), tail.raw.count("}")
        if opens > closes and opens - closes > 1:
            abrupt = True
            evidence = "final record's structured dump is missing closing braces (cut mid-emission)"
        elif tail.raw.count("╔") > tail.raw.count("╚") and tail.raw.rstrip().endswith("═"):
            abrupt = True
            evidence = "final record's banner box is unterminated"

    return EndState(
        last_ts=_iso(last_ts), enabled=enabled, is_moving=moving,
        last_auth=last_auth, last_connectivity=last_conn,
        queue_depth=http.final_queue_depth, abrupt_end=abrupt,
        abrupt_end_evidence=evidence,
        tail_record=(records[-1].raw[:1000] if records else ""),
    )


def _build_unknowns(records: list[Record]) -> UnknownSummary:
    total = len(records)
    counts = {"matched": 0, "drift": 0, "unknown": 0, "passthrough": 0}
    unclassified = 0
    drift_ex: list[str] = []
    novel_ex: list[str] = []
    for rec in records:
        k = rec.klass
        if k is None:
            unclassified += 1
            continue
        counts[k.status] = counts.get(k.status, 0) + 1
        first = rec.raw.splitlines()[0][:160]
        if k.status == "drift" and len(drift_ex) < 5:
            drift_ex.append(f"{first}  (probable drift of {k.drift_of})")
        elif k.status == "unknown" and len(novel_ex) < 5:
            novel_ex.append(first)
    classified = unclassified < total
    rate = (counts["unknown"] / total) if (classified and total) else None
    return UnknownSummary(
        classified=classified, total=total, matched=counts["matched"],
        drift=counts["drift"], unknown=counts["unknown"],
        passthrough=counts["passthrough"], unclassified=unclassified,
        unknown_rate=(round(rate, 4) if rate is not None else None),
        regen_warning=bool(rate is not None and rate > 0.05),
        drift_examples=drift_ex, novel_examples=novel_ex,
    )


def _build_anomalies(records: list[Record], timeline: Timeline, parity: ParityReport,
                     http: HttpHealth, power: PowerHealth,
                     unknowns: UnknownSummary, pairs: list[PairReport],
                     end_state: EndState) -> list[Anomaly]:
    out: list[Anomaly] = []
    for gap in timeline.gaps:
        if gap.classification == "wedge-candidate":
            out.append(Anomaly(
                kind="wedge-candidate-gap", severity="high", ts=gap.start_ts,
                detail=(f"{round(gap.duration_s / 60, 1)} min of silence starting "
                        f"{gap.start_ts} (app state: {gap.app_state}) with no fresh "
                        f"banner and no schedule alarm at the boundary"),
            ))
    if parity.imbalance:
        out.append(Anomaly(kind="sync-backlog", severity="high", ts=None,
                           detail=parity.imbalance_note))
    if http.watchdog_fires:
        out.append(Anomaly(
            kind="http-watchdog", severity="warn", ts=None,
            detail=f"{http.watchdog_fires} flush-watchdog fire/force-unlock events appear"))
    for p in pairs:
        if p.unpaired_first or p.orphan_second:
            out.append(Anomaly(
                kind="unpaired-" + p.name.split()[0].lower(), severity="warn", ts=None,
                detail=(f"{p.name}: {p.unpaired_first} unmatched {p.first_label} and "
                        f"{p.orphan_second} orphan {p.second_label} remain after collapse"),
            ))
    if parity.bg_task_unpaired > 0:
        out.append(Anomaly(
            kind="bg-task-unpaired", severity="info", ts=None,
            detail=(f"{parity.bg_task_unpaired} background-task starts have no matching "
                    f"stop in the capture (possible mid-drain truncation)")))
    mock = sum(1 for r in records if re.search(r"\bmock[\]\s}]", r.raw) or "🐞" in r.raw)
    if mock:
        out.append(Anomaly(kind="mock-locations", severity="info", ts=None,
                           detail=f"{mock} records carry mock-location markers"))
    if unknowns.regen_warning:
        out.append(Anomaly(
            kind="vocabulary-drift", severity="warn", ts=None,
            detail=(f"unknown-record rate {unknowns.unknown_rate:.1%} exceeds 5% — "
                    f"vocabulary regeneration advised")))
    platform = records[0].platform if records else ""
    for hb in power.heartbeat:
        seg = timeline.segments[hb.segment] if hb.segment < len(timeline.segments) else None
        if (hb.expected_interval_s and hb.count == 0 and seg and seg.duration_s
                and seg.duration_s >= 1800):
            out.append(Anomaly(
                kind="heartbeat-absent", severity="info", ts=seg.start_ts,
                detail=(f"heartbeat records do not appear in segment {hb.segment} "
                        f"({round(seg.duration_s / 60)} min) although heartbeatInterval="
                        f"{hb.expected_interval_s:g}s is configured — the SDK may have "
                        f"been disabled for part of this window")))
        elif hb.stretched and not hb.stretch_expected and platform == ANDROID:
            out.append(Anomaly(
                kind="heartbeat-cadence", severity="warn", ts=seg.start_ts if seg else None,
                detail=(f"segment {hb.segment}: heartbeat median cadence "
                        f"{hb.median_interval_s}s vs configured {hb.expected_interval_s:g}s")))
    if end_state.abrupt_end:
        out.append(Anomaly(kind="abrupt-end", severity="info", ts=end_state.last_ts,
                           detail=end_state.abrupt_end_evidence))
    sev_rank = {"high": 0, "warn": 1, "info": 2}
    out.sort(key=lambda a: sev_rank.get(a.severity, 3))
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def analyze(records: list[Record], segments: list[Segment],
            sources: list[Source], excerpt: bool = False) -> Analysis:
    """Stage 4: full analysis over assembled+segmented records.

    Tolerates missing Stage-2/3 annotations (rec.structs / rec.klass): every
    consumer feature-detects and falls back to substring counting on rec.raw.

    ``excerpt=True`` marks a hand-pasted fragment (e.g. a fenced block from a
    GitHub comment) rather than a full capture. The timeline is still built,
    but wedge-candidate anomalies are suppressed: a silence in an excerpt marks
    where the author stopped copying, not where the SDK stalled.
    """
    observed_levels: dict[str, int] = {}
    for rec in records:
        if rec.level:
            observed_levels[rec.level] = observed_levels.get(rec.level, 0) + 1

    platform = records[0].platform if records else (sources[0].platform if sources else "unknown")
    lane = _Lane(records, segments)
    config = _parse_config(records, platform, observed_levels)
    header = _build_header(records, segments, sources, config, observed_levels)
    timeline = _build_timeline(records, segments, lane)
    warning_groups, error_groups = _build_dedup_groups(records, lane)
    pairs = [
        _pair_scan(records, "🛃 GATE CLOSE", "🛃 GATE OPEN",
                   "FgsLaunchGate GATE CLOSE/OPEN", "GATE CLOSE", "GATE OPEN"),
        _pair_scan(records, "EVENT_DELIVERY_PAUSE", "EVENT_DELIVERY_RESUME",
                   "EventManager delivery PAUSE/RESUME",
                   "EVENT_DELIVERY_PAUSE", "EVENT_DELIVERY_RESUME"),
    ]
    parity = _build_parity(records, config)
    http = _build_http(records, config)
    geofence = _build_geofence(records)
    motion = _build_motion(records, config)
    power = _build_power(records, segments, config, parity, timeline.gaps, motion)
    auth = _build_auth(records)
    end_state = _build_end_state(records, http, auth)
    unknowns = _build_unknowns(records)
    anomalies = _build_anomalies(records, timeline, parity, http, power,
                                 unknowns, pairs, end_state)
    if excerpt:
        # An excerpt's silences are copy boundaries, not SDK stalls.
        anomalies = [a for a in anomalies if a.kind != "wedge-candidate-gap"]
    return Analysis(
        header=header, timeline=timeline,
        warning_groups=warning_groups, error_groups=error_groups,
        pairs=pairs, parity=parity, http=http, geofence=geofence,
        motion=motion, power=power, auth=auth, end_state=end_state,
        anomalies=anomalies, unknowns=unknowns, absence_note=ABSENCE_NOTE,
        excerpt=excerpt,
    )
