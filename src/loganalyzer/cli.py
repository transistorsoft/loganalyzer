"""loganalyzer CLI — wires the full pipeline:

  sniff → records → segments → structs → classify → analyze → digest/map/geojson

Usage:
  loganalyzer <files...> [--out DIR] [--map|--open] [--locations] [--no-redact] [--year YYYY]
  loganalyzer <file> --slice "07-04 13:49:29±120s" [--no-redact]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .analyze import analyze
from .classify import Matcher, Vocabulary
from .emit.digest import Redactor, redact_slice, render_json, render_markdown
from .emit.geojson import build_layers
from .sessions import build_sessions
from .emit.map import render_map
from .model import Record
from .records import assemble
from .segments import split_segments
from .sniff import Source, load_sources
from .structs import annotate

VOCAB_DIR = Path(__file__).parent / "vocabulary"

def _package_version() -> str:
    """Installed version, for --version. Reported by anyone filing a bug, so it
    reads from the installed metadata rather than a second hardcoded string."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("transistorsoft-loganalyzer")
    except PackageNotFoundError:
        return "unknown (not installed as a package)"


def _map_subtitle(analysis) -> str:
    """SDK version · device · app id for the map title bar. Many customer logs
    are mid-session exports with no launch banner, so each part is included
    only when the capture actually carried it."""
    h = analysis.header
    bits: list[str] = []
    if h.sdk_versions:
        bits.append("TSLocationManager " + " / ".join(h.sdk_versions))
    for value in (h.device, h.app_id):
        if value and "unknown" not in value.lower():
            bits.append(value)
    return " · ".join(bits)


def _emit_outputs(source: Source, records, analysis, out_dir: Path, *,
                  want_map: bool = True, want_geojson: bool = False,
                  redact: bool = True, harvest_versions: dict | None = None) -> Path:
    """Write digest(+json+aliases), optional geojson and map for one source.
    The single emit path shared by every command."""
    out_dir.mkdir(parents=True, exist_ok=True)
    redactor = Redactor(enabled=redact)
    (out_dir / "digest.md").write_text(
        render_markdown(analysis, redact=redact, redactor=redactor,
                        harvest_versions=harvest_versions))
    (out_dir / "digest.json").write_text(
        json.dumps(render_json(analysis), indent=1, default=str))
    if redactor.mapping():
        (out_dir / "aliases.local.json").write_text(
            json.dumps(redactor.mapping(), indent=1))
    if want_map or want_geojson:
        layers = build_layers(analysis, records)
        if want_geojson:
            (out_dir / "locations.geojson").write_text(json.dumps(layers, default=str))
        if want_map and layers:
            (out_dir / "map.html").write_text(
                render_map(layers, title=source.path.name,
                           subtitle=_map_subtitle(analysis),
                           sessions=build_sessions(analysis, records)))
    return out_dir


def open_maps(maps: list[Path]) -> None:
    """Open rendered maps in the browser. One implementation for every command
    that offers --open, so the two paths cannot drift apart."""
    if not maps:
        return
    import webbrowser
    for mp in maps:
        webbrowser.open(mp.resolve().as_uri())
    print(f"  opened {len(maps)} map tab(s)", file=sys.stderr)


def analyze_files(paths: list[Path], out_root: Path, *,
                  excerpts: set[str] | None = None, want_map: bool = True,
                  redact: bool = True) -> list[Path]:
    """Parse each log into its own folder under out_root; return the map paths.

    Shared by the CLI and by loganalyzer-forge's issue intake. Files whose
    basename is in `excerpts` are analyzed in excerpt mode (gap-derived wedge
    findings suppressed — a pasted fragment's silence is a copy boundary).
    """
    excerpts = excerpts or set()
    vocab = Vocabulary.load(VOCAB_DIR)
    matcher = Matcher(vocab)
    maps: list[Path] = []
    for source in load_sources(paths):
        if source.duplicate_of is not None or source.platform == "unknown" \
                or source.kind == "db":
            continue
        is_excerpt = source.path.name in excerpts
        records, segments, analysis = _pipeline(source, vocab, matcher, None,
                                                excerpt=is_excerpt)
        out_dir = _emit_outputs(source, records, analysis,
                                out_root / source.path.stem.replace(" ", "_"),
                                want_map=want_map, redact=redact,
                                harvest_versions=vocab.harvest_versions)
        mp = out_dir / "map.html"
        if want_map and mp.exists():
            # Only worth a browser tab if there is a route to look at: a
            # one-line excerpt technically maps, but opening a tab for it
            # buries the captures that matter.
            layers = build_layers(analysis, records)
            if layers.get("track", {}).get("features") or \
                    len(layers.get("fixes", {}).get("features", [])) >= 5:
                maps.append(mp)
        n_unk = sum(1 for r in records if r.klass and r.klass.status == "unknown")
        mark = " [excerpt]" if is_excerpt else ""
        print(f"  {source.path.name}{mark}: {len(records)} records, "
              f"{n_unk} unknown ({100 * n_unk / max(len(records), 1):.1f}%) → {out_dir}/",
              file=sys.stderr)
    return maps


def _pipeline(source: Source, vocab: Vocabulary, matcher: Matcher, year: int | None,
              excerpt: bool = False):
    base_year = year or datetime.fromtimestamp(source.path.stat().st_mtime).year
    records = assemble(source.platform, source.text, base_year)
    segments = split_segments(records, vocab.build_map)
    seg_version = {s.index: s.version for s in segments}
    for rec in records:
        annotate(rec)
        matcher.classify(rec, seg_version.get(rec.segment))
    analysis = analyze(records, segments, [source], excerpt=excerpt)
    return records, segments, analysis


_SLICE_RE = re.compile(r"^(.*?)(?:±|\+/-)(\d+)([sm]?)$")


def _run_slice(records: list[Record], spec: str, redactor: Redactor) -> str:
    m = _SLICE_RE.match(spec.strip())
    if not m:
        raise SystemExit(f"--slice expects '<timestamp>±<N>[s|m]', got: {spec!r}")
    ts_text, n, unit = m.group(1).strip(), int(m.group(2)), m.group(3) or "s"
    window = timedelta(seconds=n * (60 if unit == "m" else 1))

    center = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S.%f", "%m-%d %H:%M:%S"):
        try:
            center = datetime.strptime(ts_text, fmt)
            break
        except ValueError:
            continue
    if center is not None and center.year == 1900:
        # no-year Android form: borrow the year from the records themselves
        for rec in records:
            if rec.ts:
                center = center.replace(year=rec.ts.year)
                break

    if center is not None:
        hits = [r for r in records if r.ts and abs(r.ts - center) <= window]
    else:
        # fall back to prefix match on the raw timestamp text
        hits = [r for r in records if r.ts_raw.startswith(ts_text)]
        if hits and hits[0].ts:
            center = hits[0].ts
            hits = [r for r in records if r.ts and abs(r.ts - center) <= window]
    if not hits:
        raise SystemExit(f"no records within {spec!r}")
    return redact_slice(hits, redactor)


def _interactive() -> bool:
    """Is there a person to show a browser tab to? stderr, because that is where
    the progress output goes — stdout may be piped while a human still watches."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _cmd_parse(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files]
    for p in paths:
        if not p.exists():
            raise SystemExit(f"no such file: {p}")
    sources = load_sources(paths)

    vocab = Vocabulary.load(VOCAB_DIR)
    matcher = Matcher(vocab)

    out_root = Path(args.out)
    if args.open_browser is None:
        args.open_browser = _interactive()
    want_map = args.map or args.open_browser      # --open always implies a map
    any_output = False
    maps: list[Path] = []
    for source in sources:
        label = source.path.stem.replace(" ", "_")
        if source.duplicate_of is not None:
            print(f"skip {source.path.name}: duplicate of {source.duplicate_of.name}", file=sys.stderr)
            continue
        if source.platform == "unknown":
            print(f"skip {source.path.name}: not recognized as an SDK log", file=sys.stderr)
            continue
        if source.kind == "db":
            print(f"skip {source.path.name}: DB input not wired into the CLI yet", file=sys.stderr)
            continue

        records, segments, analysis = _pipeline(source, vocab, matcher, args.year)

        if args.slice:
            redactor = Redactor(enabled=not args.no_redact)
            print(_run_slice(records, args.slice, redactor))
            any_output = True
            continue

        # One emit path for both commands (analyze_files) so outputs can never
        # drift apart — a duplicated call site here previously dropped the map
        # subtitle from `loganalyzer <file> --map`.
        out_dir = _emit_outputs(source, records, analysis, out_root / label,
                                want_map=want_map, want_geojson=args.locations,
                                redact=not args.no_redact,
                                harvest_versions=vocab.harvest_versions)
        # Unlike `issue --open`, which sifts whatever a thread happened to
        # contain, these files were named explicitly: open every map that got
        # written rather than second-guessing which are worth a tab.
        if want_map and (out_dir / "map.html").exists():
            maps.append(out_dir / "map.html")
        n_unknown = sum(1 for r in records if r.klass and r.klass.status == "unknown")
        print(f"{source.path.name}: {len(records)} records, {len(segments)} segment(s), "
              f"{n_unknown} unknown ({100 * n_unknown / max(len(records), 1):.1f}%) → {out_dir}/",
              file=sys.stderr)
        # A map nobody looks at is half the point. Name it, and name the flag
        # that opens it — that is the moment someone wants to know.
        if want_map and not args.open_browser and (out_dir / "map.html").exists():
            print(f"  map: {out_dir / 'map.html'}", file=sys.stderr)
        any_output = True

    if args.open_browser:
        open_maps(maps)
    return 0 if any_output else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(prog="loganalyzer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version",
                        version=f"loganalyzer {_package_version()}")
    parser.add_argument("files", nargs="*", help="log files (.log/.log.gz) or transistor_log.db")
    parser.add_argument("--out", default="loganalyzer-out", help="output directory root")
    # Mapping is the point of the tool, so it is the default — and it always
    # was in analyze_files(); only this flag disagreed. --no-map is kept for the
    # cases that genuinely do not want it: a pipeline reading digest.json, or
    # deliberately not creating a full-precision artifact at all.
    parser.add_argument("--map", action=argparse.BooleanOptionalAction, default=True,
                        help="write map.html (default: on; LOCAL-ONLY, full precision)")
    # On when a human is watching, off when nothing can look at it. A CLI that
    # spawns browser tabs inside CI or a batch script is obnoxious; one that
    # renders a map and then tells you how to open it is a half-step. The TTY
    # check separates those two cases instead of picking a side.
    parser.add_argument("--open", dest="open_browser",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="open each map in a browser (default: on when "
                             "attached to a terminal)")
    parser.add_argument("--locations", action="store_true", help="emit locations.geojson (LOCAL-ONLY)")
    parser.add_argument("--slice", help="print records around '<ts>±<N>[s|m]' instead of writing outputs")
    parser.add_argument("--no-redact", action="store_true",
                        help="disable pseudonymization (local drill-down only — never paste)")
    parser.add_argument("--year", type=int, help="base year for Android's no-year timestamps")

    args = parser.parse_args(argv)
    if not args.files:
        parser.print_help()
        return 1
    return _cmd_parse(args)


if __name__ == "__main__":
    raise SystemExit(main())
