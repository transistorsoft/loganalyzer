# loganalyzer — module contracts (for build agents)

Design authority: `scratchpad/log-triage-design.md` (Design v2, SETTLED). This file pins the
Python interfaces between modules so they can be built in parallel. Do not change these
signatures; extend behind them.

## Already built (do not modify)
- `model.py` — `Record`, `Classification`, `Segment`, `EMOJI_TOKEN`, `normalize_emoji`,
  `ANDROID`/`IOS` constants. Record contract: `raw` is the byte-faithful re-joined record
  text; stages attach annotations (`structs`, `klass`, `segment`) alongside it.
- `sniff.py` — `load_sources(paths) -> list[Source]`; `Source(kind, platform, text, duplicate_of, filename_hint)`.
- `records.py` — `assemble(platform, text, base_year) -> list[Record]`.
- `segments.py` — `split_segments(records, build_map) -> list[Segment]` (sets `rec.segment`).

## Vocabulary YAML schema (harvester output, classify input)
`vocabulary/android.yaml`, `vocabulary/ios.yaml`:
```yaml
meta:
  platform: android
  harvested_refs: {"4.5.0": "a95f31a", ...}   # version -> commit harvested
entries:
  - id: "TSGeofenceManager.setLocation#1"      # stable-ish unique id
    class: TSGeofenceManager
    method: setLocation                         # normalized (no _block_invoke/[λ])
    source: ["geofence/TSGeofenceManager.java:1521"]   # repo-relative, multi-candidate allowed
    level: DEBUG                                # emit level (iOS: from macro)
    icon: "⚠"                                  # normalized base-codepoint icon, "" if none
    patterns:                                   # >=1; regex over Record.raw (MULTILINE+DOTALL)
      - "isMoving: (.*?) \\| stateChanged: (.*?) \\| timerExpired: "
    anchor: "stateChanged: "                    # longest uninterrupted literal fragment
    valid_from: "4.0.0"                         # version range this entry existed
    valid_to: null                              # null = still present at HEAD
    flags: []                                   # conditional-tail | debug-gated | passthrough | legacy | builder-catchall
```
`vocabulary/ios-builds.yaml`: `{ "388": "4.4.2", ... }` build→marketing-version.
`vocabulary/semantics.yaml` (hand-curated, already written by the lead): icon→semantic per
version-range, body-emoji hints, category rules mapping (class/method/semantic) → digest/map
category. Read it, don't regenerate it.

## classify.py
```python
class Vocabulary:
    @classmethod
    def load(cls, vocab_dir: Path) -> "Vocabulary": ...   # reads android/ios/semantics/builds yamls
    @property
    def build_map(self) -> dict[str, str]: ...

class Matcher:
    def __init__(self, vocab: Vocabulary): ...
    def classify(self, rec: Record, version: str | None) -> Classification: ...
        # Sets rec.klass and returns it. Two-tier: pyahocorasick automaton over `anchor`
        # fragments (+ emoji byte anchors for literal-poor sites) -> candidate entries ->
        # full-regex verify against rec.raw. Precedence: literal-length-descending;
        # catch-alls only when no literal hits. Android join: literal candidates, tag as
        # scorer (weight 0 for LoggerFacade$Entry/helper frames), level+icon tiebreak.
        # iOS join: (tag_class, tag_method) bucket first. version filters by
        # valid_from/valid_to when known; None = no filter. On miss: fuzzy longest-fragment
        # attempt -> status "drift" (drift_of=site) or "unknown". Bridge passthrough sites
        # -> status "passthrough" (never pattern-match their body).
```

## structs.py
```python
def annotate(rec: Record) -> None   # fills rec.structs; never mutates raw/body
```
Keys (present only when parsed): `location` (dict: lat, lon, acc, speed, course, alt,
provider, mock, et, age_ms, batch_index), `locations` (list, iOS N: batches),
`filter_result` (decision, reason, raw, effective, anomaly, ...), `detected_activity`,
`motion_activity` (iOS CMMotionActivity), `intent` (act, dat, cmp), `config_dump`
(dict|None, `truncated: bool`, plist-style on iOS, truncated JSON on Android),
`http` (status, count, success, duration_ms, url), `nserror` (code, domain, desc),
`geofence` (action, identifier), `ab_compare` (list). Mini-parsers must tolerate
truncation and never raise.

## analyze.py
```python
@dataclass
class Analysis: ...        # you own this shape; digest/geojson consume it — keep it a
                           # plain dataclass tree, JSON-serializable via dataclasses.asdict
def analyze(records: list[Record], segments: list[Segment], sources: list[Source]) -> Analysis
```
Must produce (per design v2): header info (device/sdk/config-diff incl. effective logLevel,
degraded behavior when banners absent), timeline (segments, app-state lane
foreground/background/headless, classified gaps: death/suspension/scheduler/wedge-candidate
with 15min threshold + schedule-alarm and banner correlation), warnings+errors dedup groups
(count, first/last ts, sites, app-state at occurrence), pair-collapse (GATE OPEN/CLOSE,
PAUSE/RESUME) with unpaired leftovers, record-lifecycle parity (persist/post/destroy,
bg-task start/stop), HTTP health (statuses, retries, watchdog, final queue depth,
connectivity timeline, url classification private-lan/cleartext/public), geofence health
(transitions + registration side: add/remove/stale-cleanup, availability flapping,
registered vs MAX_GEOFENCES=97), motion health (activity histogram, trigger armed/reset,
stopTimeout engaged vs fired, low-confidence ignored), power (power-save timeline,
heartbeat cadence expected-vs-observed per segment, duty-cycle rollup: fixes/hour,
%time moving, prevent-suspend count), auth timeline (verbatim lines with tags),
end-of-log state (enabled, pace, last auth/connectivity, queue depth, abrupt-end flag),
anomalies, unknown lines (drift vs novel). Absence caveat: never assert "didn't happen".

## emit/digest.py
```python
def render_markdown(analysis: Analysis, redact: bool = True) -> str
def render_json(analysis: Analysis) -> dict          # never redacted; local-only artifact
def redact_slice(records: list[Record], redactor: "Redactor") -> str
class Redactor:      # stable per-digest pseudonymization, NOT deletion
    # coords -> COORD-A..., geofence ids -> GF-1..., uuids -> REC-1..., package names &
    # bundle ids (incl. Intent cmp=/dat=) -> PKG-1..., device models -> DEV-1...,
    # urls -> URL-1 with classification verdict retained (private-lan/cleartext/public);
    # authorization config group + url ALWAYS masked even with redact=False in config diffs.
    def mapping(self) -> dict[str, str]   # alias -> original, written to a local file
```
Digest quotes records in the two-line header/body shape (authorial convention).

## emit/geojson.py + emit/map.py
```python
def build_layers(analysis, records) -> dict[str, dict]   # layer name -> GeoJSON FeatureCollection
def render_map(layers: dict[str, dict], title: str) -> str  # self-contained Leaflet HTML
```
Layers: track, fixes, rejections, lifecycle, errors, warnings, geofence, motionchange,
http, gaps, mock. Time-georeferencing: Δt<=120s -> placed marker; else tether to last
known fix with "+N min" badge + dashed link (empirically calibrated: warn/error median
Δt=0.00s; lifecycle 40-50% >120s on quiet devices). Marker glyphs = the log emoji
(☯️ 💀 ⚠️ ‼️ 🚫 📢 …). Popups show the record (two-line shape) + Δt + slice-ready timestamp.
OSM tiles. Marker clustering above ~500 markers/layer. Maps are full-precision, LOCAL-ONLY.

## cli.py (lead builds after modules land)
`loganalyzer <files...> [--out DIR] [--digest] [--map] [--locations] [--slice TS±WIN]
[--redact/--no-redact] [--year YYYY]` and `loganalyzer harvest [--ref TAG] [--platform P]`.
