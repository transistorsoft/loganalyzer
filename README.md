# loganalyzer

Triage toolkit for [Background Geolocation](https://github.com/transistorsoft/react-native-background-geolocation)
SDK logs. Turns an iOS or Android capture into a **digest** — what happened, what went
wrong — and an **interactive map**: the route, the events on it, and a time navigator for
captures that span days.

If you have a tracking problem and a log, this tells you what the SDK was doing, and gives
you something you can paste into an issue.

```bash
uvx transistorsoft-loganalyzer background-geolocation.log.gz --open
```

No Python setup required — see below.

---

## Install

The tool is Python, but you don't need to manage Python. [uv](https://docs.astral.sh/uv/)
brings its own:

```bash
# one-off, nothing installed
uvx transistorsoft-loganalyzer <file> --open

# equivalent, and the form to use if you pin a version
uvx --from transistorsoft-loganalyzer loganalyzer <file> --open

# or install it, which puts `loganalyzer` on your PATH
uv tool install transistorsoft-loganalyzer
loganalyzer <file> --open
```

The package installs two identical commands: `loganalyzer` (what you will
normally type) and `transistorsoft-loganalyzer` (so the `uvx <package>`
shorthand resolves).

If you already run Python 3.11+, `pipx install transistorsoft-loganalyzer` works too.

## Getting a log

Call `emailLog()` in your app — the SDK writes a `.log.gz` and hands it to the share sheet.
Both `.log` and `.log.gz` are accepted, as are several at once.

```dart
BackgroundGeolocation.emailLog("you@example.com");
```

---

## Commands

### Analyze

```bash
loganalyzer <files...> [--out DIR] [--open] [--no-map] [--locations] [--year YYYY]
```

A map is written **and opened** by default — analyzing a tracking log and not
looking at it is rarely what you want. Opening is skipped automatically when
output is piped or there is no terminal, so scripts and CI stay quiet.
`--no-open` writes without viewing; `--no-map` skips the map entirely.

Platform is grammar-sniffed, not guessed from the filename. Duplicates and unrecognized
files are skipped with a note.

| flag | effect |
|---|---|
| `--out DIR` | output root (default `loganalyzer-out/`); one subfolder per input |
| `--map` / `--no-map` | write `map.html` — **on by default** |
| `--open` / `--no-open` | open the map — **on by default in a terminal**, off when piped or in CI |
| `--locations` | also write `locations.geojson` |
| `--year YYYY` | base year for Android's year-less timestamps (inferred otherwise) |
| `--no-redact` | disable pseudonymization — local drill-down only |

### Drill into a moment

```bash
loganalyzer <file> --slice "07-04 13:49:29±120s"
```

Prints the raw records around a timestamp instead of writing outputs. Accepts `s` or `m`
windows. Redacted by default, so slice output is safe to quote into an issue — and every map
popup shows a copy-ready `--slice` string for the record it describes.

---

## Output, and what is safe to share

| file | contents | shareable? |
|---|---|---|
| `digest.md` | the triage summary | ✅ **pseudonymized** — the artifact to quote |
| `digest.json` | same analysis, machine-readable | ❌ full precision |
| `aliases.local.json` | alias → real value mapping | ❌ never leaves the machine |
| `map.html` | interactive map | ❌ full-precision coordinates |
| `locations.geojson` | raw layer geometry | ❌ full-precision coordinates |

Output lands in `./loganalyzer-out/` unless you pass `--out`. A freshly created
output directory gets its own `.gitignore` containing `*`, so running this inside
a repository cannot commit somebody's movements by accident. An output directory
that already existed is never modified.

Redaction is **pseudonymizing, not deleting**: coordinates become `COORD-A`, fences `GF-1`,
packages `PKG-1`, devices `DEV-1`, URLs `URL-1`. The same real value always gets the same
alias, so the digest still reads as a coherent story — "the device left `GF-1` at `COORD-A`"
— while carrying nothing identifying.

**`digest.md` and `--slice` output are the only artifacts meant to be pasted into a public
issue.** The map is a local instrument: it plots exactly where the device went.

If a log contains an auth token the SDK failed to redact, say *"token present in log"* — do
not paste it.

---

## The map

```bash
loganalyzer <file> --open
```

One self-contained HTML file: no CDN, no sibling assets, no build step. OpenStreetMap tiles
are its only network dependency, so it works offline apart from the basemap and can be
archived alongside a ticket.

- **Track** — the route, with an optional *color by speed* mode
- **Fixes** — chevrons pointing in the direction of travel (dot when course is unknown)
- **Layers** — launch, lifecycle, errors, warnings, geofence, motion, HTTP, rejections,
  gaps, mock; high-volume layers start hidden
- **Time navigator** — the strip along the bottom: an activity histogram over the whole
  capture with a window you can drag, stretch or zoom. Everything above filters to it, and
  the track is genuinely clipped, not just hidden.
- **Sessions** — the ruler under the histogram. A capture is split into tracking sessions at
  silences in the location stream; click one to jump to it, or step with `‹ ›`. Each reports
  its distance and what ended it (`death`, `scheduler-window`, `suspension`,
  `wedge-candidate`).
- **Popups** — the record in its authored two-line shape, plus a copy-ready `--slice`

Markers use vendored [Lucide](https://lucide.dev) icons, tinted semantically: green =
tracking resumes / geofence ENTER, red = tracking parks / EXIT / failure, amber = DWELL /
app foreground.

---

## Customising the map

`src/loganalyzer/vocabulary/map-rules.yaml` decides which icon an event gets, which colour,
which bearing from its anchor, and what is not worth mapping. Editing it changes the map; no
Python change needed.

`layers:` is the single definition of every per-layer fact, and its key order is the layer
order:

```yaml
layers:
  geofence: { label: Geofence, kind: marker, glyph: "📢", icon: geofence, clock: 10 }
```

`rules:` are ordered (first match wins), scoped to a layer, and may be scoped to a platform
with `platform: android|ios`. `suppress:` drops records from the map entirely, and each entry
states **why** — so a later reader can judge whether it still holds.

---

## How it works

```
sniff → records → segments → structs → classify → analyze → digest / map / geojson
```

`classify` joins each log line back to the SDK call site that emitted it, using a vocabulary
harvested from the SDK sources across every release. That is how the tool recognizes lines it
has never seen in a sample, and how it knows which SDK version a message belongs to.

```
src/loganalyzer/
  sniff.py      platform detection, gz/dup handling
  records.py    raw text → Records (folds continuation lines)
  segments.py   split at app-launch banners
  structs.py    extract locations, config, geofences, filter results
  classify.py   match records against the harvested vocabulary
  analyze.py    the findings: gaps, HTTP health, motion, power, anomalies
  locations.py  which coordinates are real fixes vs merely referenced
  sessions.py   tracking sessions (runs of fixes, split at 20-min silences)
  emit/         digest, geojson, map, navigator, icons
  vocabulary/   the harvested tables + map presentation rules
```

`INTERFACES.md` pins the contracts between these modules.

## Tests

```bash
uv run pytest -q          # the suite
scripts/preflight.sh      # everything the suite cannot reach
```

`preflight.sh` is what to run before tagging. The suite does not exercise
packaging or resolution, and that is where the failures have actually been: a
wheel missing its vocabulary still installs and still starts, `uvx <package>`
needs a console script named after the package, and the npm launcher has to
resolve its bin from a tarball. It builds the wheel, installs it into a
throwaway venv, runs both console scripts on a real capture, packs and installs
the npm launcher against that same wheel, checks the three version pins agree,
and refuses a version either registry already has. It publishes nothing.

The suite runs against real captures committed under `tests/fixtures/` — real cadence, real
gaps, real geofence chatter. Their coordinates have been moved by a single rigid transform,
so every distance, speed, bearing and session boundary is exactly preserved while the route
points somewhere nobody has been. A handful of tests are gated on captures that cannot be
published and skip cleanly.

## License

MIT — see [LICENSE](LICENSE).
