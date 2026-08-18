# @transistorsoft/loganalyzer

Triage [Background Geolocation](https://github.com/transistorsoft/react-native-background-geolocation)
SDK logs. Turns an iOS or Android capture into a **digest** — what happened, what went
wrong — and an **interactive map**.

```bash
npx @transistorsoft/loganalyzer background-geolocation.log.gz
```

Renders the map and opens it.

No Python, no toolchain, nothing to install. If you have `npx`, you have this.

## Getting a log

Call `emailLog()` in your app — the SDK writes a `.log.gz` and hands it to the share sheet.
Both `.log` and `.log.gz` work, and you can pass several at once.

```dart
BackgroundGeolocation.emailLog("you@example.com");
```

## What you get

One folder per log:

| file | contents | safe to share? |
|---|---|---|
| `digest.md` | the triage summary | ✅ **pseudonymized** — the one to paste into an issue |
| `digest.json` | same analysis, machine-readable | ❌ full precision |
| `map.html` | interactive map | ❌ full-precision coordinates |

Coordinates in `digest.md` become `COORD-A`, geofences `GF-1`, and so on — the same real
value always gets the same alias, so it still reads as a coherent story while identifying
nothing. **The map is a local instrument: it plots exactly where the device went.**

## Flags

```
--no-open       write the map without opening it (opens by default in a terminal)
--no-map        skip the map entirely (it is written by default)
--locations     write locations.geojson
--out DIR       output root (default: ./loganalyzer-out)
--slice "<ts>±<N>[s|m]"   print the raw records around a moment instead
--no-redact     disable pseudonymization (local drill-down only)
--year YYYY     base year for Android's year-less timestamps
```

## How it works

This package is a launcher. The analyzer itself is Python, published to PyPI as
[`transistorsoft-loganalyzer`](https://pypi.org/project/transistorsoft-loganalyzer/), and
this shim brings its own interpreter via [uv](https://docs.astral.sh/uv/) so you never
have to think about Python versions.

First run fetches uv (~35 MB, checksum-verified against Astral's published SHA-256) plus a
CPython, into a cache directory. Everything after that is local and instant. If you already
have `uv` on your PATH, nothing is downloaded at all.

Prefer to skip the launcher? The Python package is the same tool:

```bash
uvx transistorsoft-loganalyzer <file> --open
pipx install transistorsoft-loganalyzer      # needs Python 3.11+
```

## License

MIT. Source and issues: https://github.com/transistorsoft/loganalyzer
