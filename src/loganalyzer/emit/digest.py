"""Stage 5 — digest emitter: digest.md + digest.json + redaction.

Contract (INTERFACES.md `emit/digest.py`):

- ``render_markdown(analysis, redact=True)`` — the scannable, SHAREABLE digest.
- ``render_json(analysis)`` — machine mirror via ``dataclasses.asdict``; NEVER
  redacted (local-only artifact, the /triage-issue skill's input).
- ``Redactor`` — stable per-digest PSEUDONYMIZATION, not deletion: correlation
  must survive. Coordinates -> COORD-A…, geofence identifiers -> GF-1…,
  uuids -> REC-1…, package/bundle ids (incl. Intent cmp=/dat=) -> PKG-1…,
  device models -> DEV-1…, scheme-agnostic URLs -> URL-1 (classification
  verdict retained, e.g. "URL-1 (private-LAN — unreachable from cellular)").
  Geofence identifiers and device models are registered FROM ANALYSIS DATA
  (record structs / header), never guessed by regex.
- ``redact_slice(records, redactor)`` — records rendered verbatim in the
  two-line header/body shape (the authorial convention), redacted.

Hard rules honored here:
- authorization config values + the config url are ALWAYS masked in the
  config output, regardless of the redact flag (defense for the copy-paste
  path). ``analyze.py`` already never carries authorization VALUES.
- Same input string -> same alias within one digest (idempotent redaction).
- The digest is selective, not exhaustive: tables for counters, verbatim
  fenced blocks for quotes, hard caps on every list. Target 200-500 rendered
  lines for a 75k-record log.
"""
from __future__ import annotations

import dataclasses
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..analyze import Analysis, ConfigReport, DedupGroup
from ..model import Record

# ── rendering caps (keep the digest scannable) ───────────────────────────────

MAX_ERROR_GROUPS = 20
MAX_WARN_GROUPS = 12
MAX_QUOTE_LINES = 6          # representative record quote, lines kept
MAX_SEGMENTS = 30
MAX_LANE_INTERVALS = 16      # app-state lane rows (first half + last half)
MAX_GAPS = 40
MAX_CONNECTIVITY = 10
MAX_URLS = 12
MAX_POWER_EVENTS = 10
MAX_HEARTBEAT_SEGMENTS = 20
MAX_AUTH_QUOTES = 8
MAX_AUTH_QUOTE_LINES = 4
MAX_TAIL_LINES = 12
MAX_EVIDENCE_CHARS = 110

_URL_VERDICTS = {
    "private-lan": "private-LAN — unreachable from cellular",
    "cleartext-http": "cleartext HTTP",
    "https-public": "HTTPS public",
    "other": "non-HTTP scheme",
}


# ═════════════════════════════════════════════════════════════════════════════
# Redactor
# ═════════════════════════════════════════════════════════════════════════════

# iOS coordinate form: 📍<+45.01491283,-72.12346833> (sign usually explicit).
_COORD_IOS = re.compile(r"<\s*([+-]?\d{1,3}\.\d+)\s*,\s*([+-]?\d{1,3}\.\d+)\s*>")
# Bare pair form: "45.518862,-73.600546" (Android Location[…] and free text).
# >=3 fractional digits keeps version pairs ("4.5.0") and counters out; range
# validation happens in the callback (|lat|<=90, |lon|<=180).
_COORD_PAIR = re.compile(
    r"(?<![\d.\w])([+-]?\d{1,3}\.\d{3,})\s*,\s*([+-]?\d{1,3}\.\d{3,})(?![\d.])"
)
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Scheme-agnostic URL (catches tslocationmanager:// in Intent dat= too).
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>\])},;|]+")
# Reverse-DNS package/bundle-shaped token: >= 3 dotted segments, first starts
# with a letter. Platform/SDK namespaces are allowlisted below — redacting
# stack-trace frames of the SDK itself would destroy triage value while
# identifying nobody (every customer ships those frames).
_PACKAGE = re.compile(r"(?<![\w.$@\-])(?:[a-zA-Z][\w$]*\.){2,}[a-zA-Z][\w$]*")
_PKG_ALLOWLIST = (
    "android.", "androidx.", "java.", "javax.", "kotlin.", "kotlinx.",
    "dalvik.", "libcore.", "sun.", "com.android.", "com.google.",
    "com.transistorsoft.", "io.flutter.", "org.apache.", "org.json.",
)
_URL_TRAILING = ".,;:'\""


def _letters(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA (COORD-A, COORD-B, … COORD-AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Redactor:
    """Stable per-digest pseudonymization. NOT deletion: the same original
    value always maps to the same alias within one digest, so correlation
    (same coordinate, same record uuid, same geofence) survives redaction.

    ``enabled=False`` makes :meth:`redact` the identity function, but alias
    allocation (:meth:`url_with_verdict`, registrations) still works — the
    config url / authorization masking is unconditional by contract.
    """

    _PREFIXES = {"coord": "COORD", "gf": "GF", "uuid": "REC",
                 "pkg": "PKG", "dev": "DEV", "url": "URL"}

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._map: dict[str, str] = {}                 # alias -> original
        self._rev: dict[tuple[str, str], str] = {}     # (kind, key) -> alias
        self._counts: dict[str, int] = {}
        self._exact: dict[str, tuple[str, str]] = {}   # original -> (kind, key)

    # ── alias bookkeeping ────────────────────────────────────────────────────

    def _next(self, kind: str) -> str:
        n = self._counts.get(kind, 0) + 1
        self._counts[kind] = n
        if kind == "coord":
            return f"COORD-{_letters(n)}"
        return f"{self._PREFIXES[kind]}-{n}"

    def _alias(self, kind: str, key: str, original: str) -> str:
        got = self._rev.get((kind, key))
        if got is None:
            got = self._next(kind)
            self._rev[(kind, key)] = got
            self._map[got] = original
        return got

    def mapping(self) -> dict[str, str]:
        """alias -> original. The caller writes this to a LOCAL file only."""
        return dict(self._map)

    # ── registration (from analysis data — never guessed) ────────────────────

    def register_geofence(self, identifier: str) -> str:
        """Register a geofence identifier known from record structs/analysis."""
        identifier = str(identifier)
        alias = self._alias("gf", identifier, identifier)
        self._exact[identifier] = ("gf", identifier)
        return alias

    def register_device(self, model: str) -> str:
        """Register a device model string (from the launch banner). The model
        part before ' @ ' is aliased so the OS level survives redaction."""
        model = model.split(" @ ")[0].strip() if " @ " in model else model.strip()
        alias = self._alias("dev", model, model)
        self._exact[model] = ("dev", model)
        return alias

    # ── URL aliasing with the classification verdict retained ────────────────

    def alias_url(self, url: str) -> str:
        return self._alias("url", url, url)

    def url_with_verdict(self, url: str, classification: Optional[str]) -> str:
        """'URL-1 (private-LAN — unreachable from cellular)' — the verdict is
        the triage-relevant part and must survive redaction."""
        verdict = _URL_VERDICTS.get(classification or "other", classification or "other")
        return f"{self.alias_url(url)} ({verdict})"

    # ── text redaction ───────────────────────────────────────────────────────

    def redact(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        # 1. Registered exact strings (geofence ids, device models), longest
        #    first so overlapping registrations resolve deterministically.
        for original in sorted(self._exact, key=len, reverse=True):
            if original and original in text:
                alias = self._rev[self._exact[original]]
                if re.fullmatch(r"\w+", original):
                    text = re.sub(rf"(?<!\w){re.escape(original)}(?!\w)", alias, text)
                else:
                    text = text.replace(original, alias)
        # 2. URLs before packages: hostnames are package-shaped.
        text = _URL.sub(self._sub_url, text)
        # 3. Coordinates: iOS <+lat,+lon> first, then bare pairs. Both key on
        #    the sign-normalized pair so the SAME place gets the SAME alias.
        text = _COORD_IOS.sub(self._sub_coord_ios, text)
        text = _COORD_PAIR.sub(self._sub_coord_pair, text)
        # 4. UUIDs, then reverse-DNS package tokens.
        text = _UUID.sub(self._sub_uuid, text)
        text = _PACKAGE.sub(self._sub_pkg, text)
        return text

    def _coord_key(self, lat: str, lon: str) -> str:
        return f"{lat.lstrip('+')},{lon.lstrip('+')}"

    def _sub_coord_ios(self, m: re.Match) -> str:
        lat, lon = m.group(1), m.group(2)
        try:
            if abs(float(lat)) > 90 or abs(float(lon)) > 180:
                return m.group(0)
        except ValueError:
            return m.group(0)
        key = self._coord_key(lat, lon)
        return f"<{self._alias('coord', key, key)}>"

    def _sub_coord_pair(self, m: re.Match) -> str:
        lat, lon = m.group(1), m.group(2)
        try:
            if abs(float(lat)) > 90 or abs(float(lon)) > 180:
                return m.group(0)
        except ValueError:
            return m.group(0)
        key = self._coord_key(lat, lon)
        return self._alias("coord", key, key)

    def _sub_uuid(self, m: re.Match) -> str:
        return self._alias("uuid", m.group(0).lower(), m.group(0))

    def _sub_url(self, m: re.Match) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in _URL_TRAILING:
            tail = url[-1] + tail
            url = url[:-1]
        return self.alias_url(url) + tail

    def _sub_pkg(self, m: re.Match) -> str:
        token = m.group(0)
        if any(token.startswith(p) for p in _PKG_ALLOWLIST):
            return token
        return self._alias("pkg", token, token)


# ═════════════════════════════════════════════════════════════════════════════
# Slice rendering
# ═════════════════════════════════════════════════════════════════════════════

def register_from_records(records: list[Record], redactor: Redactor) -> None:
    """Register geofence identifiers found in record structs (Stage-2 data —
    never guessed by regex) so they redact wherever they appear. The CLI calls
    this once over all records before rendering the digest, so GF-* aliases
    line up between digest.md and every ``--slice``."""
    for rec in records:
        gf = rec.structs.get("geofence")
        if isinstance(gf, dict) and gf.get("identifier"):
            redactor.register_geofence(str(gf["identifier"]))


def redact_slice(records: list[Record], redactor: Redactor) -> str:
    """Render records verbatim in the two-line header/body shape (Record.raw
    preserves it byte-faithfully), one blank line between records, redacted
    through ``redactor``. Geofence identifiers found in record structs are
    registered first (from data, not guessed)."""
    register_from_records(records, redactor)
    blocks = [redactor.redact(rec.raw) for rec in records]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


# ═════════════════════════════════════════════════════════════════════════════
# JSON rendering
# ═════════════════════════════════════════════════════════════════════════════

def render_json(analysis: Analysis) -> dict:
    """Machine mirror of the analysis: dataclasses.asdict with timestamps as
    ISO strings. NEVER redacted — digest.json is a local-only artifact and is
    never pasted into issues (skill rule)."""
    return _jsonify(dataclasses.asdict(analysis))


def _jsonify(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(obj, Path):
        return str(obj)
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


# ═════════════════════════════════════════════════════════════════════════════
# Markdown rendering
# ═════════════════════════════════════════════════════════════════════════════

def render_markdown(analysis: Analysis, redact: bool = True, *,
                    redactor: Optional[Redactor] = None,
                    harvest_versions: Optional[dict] = None) -> str:
    """Render the digest. ``redact`` governs pseudonymization of everything
    EXCEPT the config url + authorization values, which are always masked.

    A shared ``redactor`` may be passed (e.g. pre-loaded with geofence
    identifiers from records, or shared with ``redact_slice`` so aliases line
    up across artifacts); its ``enabled`` flag is set from ``redact``.

    ``harvest_versions`` (platform -> SDK version, from Vocabulary) says which
    release the source line numbers were harvested at, so the digest can state
    what its `file:line` links are accurate FOR. Omitted when unknown, and
    irrelevant when no links are present at all.
    """
    red = redactor if redactor is not None else Redactor(enabled=redact)
    red.enabled = redact

    hdr = analysis.header
    if hdr.device and not hdr.device.startswith("unknown"):
        red.register_device(hdr.device)

    out: list[str] = []
    out += _sec_title(analysis, red)
    out += _sec_header(analysis, red)
    out += _sec_timeline(analysis, red)
    out += _sec_warnings_errors(analysis, red, harvest_versions)
    out += _sec_health(analysis, red)
    out += _sec_end_state(analysis, red)
    out += _sec_anomalies(analysis, red)
    out += _sec_unknowns(analysis, red)
    out += _sec_missing_evidence(analysis)
    out += _sec_footer(red)
    return "\n".join(out) + "\n"


# ── small helpers ────────────────────────────────────────────────────────────

def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _cell(v: Any) -> str:
    return _fmt(v).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return out


def _fence(text: str) -> list[str]:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return [ticks, text, ticks]


def _cap_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n] + [f"… (+{len(lines) - n} more lines)"])


def _dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 90:
        return f"{s:.0f} s"
    if s < 5400:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.1f} h"


def _hist(d: dict[str, int], none_text: str = "none appear") -> str:
    if not d:
        return none_text
    items = sorted(d.items(), key=lambda kv: -kv[1])
    return ", ".join(f"{k}×{v}" for k, v in items)


def _elide(rows: list, cap: int) -> tuple[list, Optional[str]]:
    """Keep first/last halves when over cap; return (rows, elision-note)."""
    if len(rows) <= cap:
        return rows, None
    head, tail = cap // 2, cap - cap // 2
    return rows[:head] + rows[-tail:], f"… {len(rows) - cap} rows elided …"


# ── sections ─────────────────────────────────────────────────────────────────

def _sec_title(a: Analysis, red: Redactor) -> list[str]:
    hdr = a.header
    span = f"{_fmt(hdr.log_start)} → {_fmt(hdr.log_end)}"
    mode = ("redacted — pseudonymized aliases (COORD-*/GF-*/REC-*/PKG-*/DEV-*/URL-*); "
            "mapping file is local-only" if red.enabled
            else "UNREDACTED — local drill-down only, never paste into issues")
    return [
        f"# Log triage digest — {hdr.platform} — {span}",
        "",
        f"> {a.absence_note}",
        f"> Output mode: {mode}.",
        *([
            "",
            "> ⚠️ **EXCERPT** — this capture was pasted into an issue comment, not exported "
            "from the device. It is a fragment the author chose: silences are copy "
            "boundaries (wedge findings are suppressed), counts are not session totals, "
            "and absent evidence means nothing. Ask for the full `.log.gz` from "
            "`emailLog` before drawing timeline conclusions.",
        ] if getattr(a, "excerpt", False) else []),
        "",
    ]


def _sec_header(a: Analysis, red: Redactor) -> list[str]:
    hdr = a.header
    R = red.redact
    out = ["## Header", ""]
    out += [f"- **Platform:** {hdr.platform}",
            f"- **Device:** {R(hdr.device)}",
            f"- **App id:** {R(hdr.app_id)}",
            f"- **SDK version(s):** {', '.join(hdr.sdk_versions) or 'unknown — no version banner in capture'}",
            f"- **Span:** {_fmt(hdr.log_start)} → {_fmt(hdr.log_end)}"
            f" ({_dur(hdr.duration_s)}) · {_fmt(hdr.record_count)} records",
            f"- **Observed levels:** "
            + (_hist(hdr.observed_levels,
                     "none — iOS text export is level-blind (Info/Debug/Verbose/Notice all render 🔵)"))]
    if hdr.sources:
        out += ["", "### Sources", ""]
        rows = []
        for s in hdr.sources:
            notes = "; ".join(s.notes) if s.notes else ""
            if s.duplicate_of:
                notes = (notes + "; " if notes else "") + f"duplicate of {s.duplicate_of}"
            rows.append([s.path, s.kind, s.platform, R(s.filename_hint) or "—", notes or "—"])
        out += _table(["path", "kind", "platform", "filename hint", "notes"], rows)
    out += ["", "### Config", ""]
    out += _config_lines(a.header.config, red)
    out.append("")
    return out


def _config_lines(cfg: ConfigReport, red: Redactor) -> list[str]:
    out: list[str] = []
    if not cfg.present:
        out.append(f"- No config dump appears in the capture — effective logLevel: {cfg.effective_log_level}.")
        out.append(f"- *{cfg.defaults_note}*")
        return out
    R = red.redact
    trunc = " (dump looks TRUNCATED — unbalanced braces)" if cfg.truncated else ""
    out.append(f"- {cfg.dump_count} config dump(s) in capture; {len(cfg.keys)} top-level keys observed{trunc}.")
    out.append(f"- **Effective logLevel:** {cfg.effective_log_level}")
    rows: list[list[Any]] = []
    for k, v in cfg.notable.items():
        if k == "url":
            continue  # rendered on the always-masked row below
        rows.append([k, R(str(v)) if isinstance(v, str) else v])
    # ALWAYS-masked rows, regardless of the redact flag:
    url_cell = (red.url_with_verdict(cfg.url, cfg.url_classification)
                if cfg.url else "not set / does not appear in dump")
    rows.append(["url", f"{url_cell} — always masked"])
    rows.append(["authorization",
                 "present — values always masked" if cfg.authorization_present
                 else "does not appear in dump"])
    out += _table(["key", "effective value"], rows)
    out.append(f"- *{cfg.defaults_note}*")
    return out


def _sec_timeline(a: Analysis, red: Redactor) -> list[str]:
    R = red.redact
    tl = a.timeline
    out = ["## Timeline", ""]

    out.append(f"### Launches / segments ({len(tl.segments)})")
    out.append("")
    seg_rows = [[s.index,
                 (s.version or "unknown") + (f" ({s.version_source})" if s.version_source else ""),
                 s.start_ts, s.end_ts, _dur(s.duration_s), s.record_count,
                 "yes" if s.launched_headless else ""]
                for s in tl.segments]
    seg_rows, note = _elide(seg_rows, MAX_SEGMENTS)
    out += _table(["seg", "version", "start", "end", "duration", "records", "headless"], seg_rows)
    if note:
        out.append(note)

    out += ["", "### App-state lane", ""]
    lane_rows = [[iv.state, iv.start_ts, iv.end_ts, f"seq {iv.start_seq}–{iv.end_seq}"]
                 for iv in tl.app_state]
    if lane_rows:
        lane_rows, note = _elide(lane_rows, MAX_LANE_INTERVALS)
        out += _table(["state", "from", "to", "records"], lane_rows)
        if note:
            out.append(note)
    else:
        out.append("No app-state transitions appear in the capture.")

    out += ["", f"### Gaps ≥ {tl.gap_threshold_s // 60} min ({len(tl.gaps)})", ""]
    if tl.gaps:
        gap_rows = [[g.start_ts, _dur(g.duration_s), g.classification, g.app_state,
                     R(g.evidence)[:MAX_EVIDENCE_CHARS]]
                    for g in tl.gaps]
        gap_rows, note = _elide(gap_rows, MAX_GAPS)
        out += _table(["silence from", "duration", "classification", "app state", "boundary evidence"], gap_rows)
        if note:
            out.append(note)
    else:
        out.append("No silences at or above the threshold appear in the capture.")
    out.append("")
    return out


def _group_lines(g: DedupGroup, red: Redactor) -> list[str]:
    R = red.redact
    tag = f"[{g.tag_class} {g.tag_method}]".strip("[] ") or "(untagged)"
    out = [f"#### {g.count}× {g.severity} — `{R(tag)}`", ""]
    meta = f"- first {_fmt(g.first_ts)} · last {_fmt(g.last_ts)} · app-state: {_hist(g.app_states, 'unknown')}"
    out.append(meta)
    if g.sites:
        links = " · ".join(f"`{s}`" for s in g.sites)
        tie = " *(multi-candidate tie)*" if len(g.sites) > 1 else ""
        out.append(f"- → {links}{tie}")
    out += _fence(_cap_lines(R(g.representative), MAX_QUOTE_LINES))
    out.append("")
    return out


_LINE_REF = re.compile(r"/.*:\d+$")


def _is_line_ref(site: str) -> bool:
    return bool(_LINE_REF.search(site))


def _source_link_note(a: Analysis, harvest_versions: Optional[dict]) -> list[str]:
    """One line saying what a `file:line` link is accurate for.

    A source link is a call SITE. The line number holds at the release it was
    harvested from; between releases the code above it moves, so on an older
    capture the line can land somewhere unrelated — an answer that is confident,
    specific and wrong. Said once here rather than stamped on every link,
    because 83-94% of entries are current and would repeat the same string.

    Silent when there are no links to qualify: a public install carries no
    source map, and a note about links you cannot see is just confusing.
    """
    if not harvest_versions:
        return []
    # Only when there are LINE references to qualify. Without the source map,
    # sites fall back to the vocabulary entry id (VocabEntry.handle) — still a
    # useful symbolic call site, but nothing a line number applies to, so a note
    # about line accuracy would be answering a question nobody asked.
    groups = list(a.error_groups) + list(a.warning_groups)
    if not any(_is_line_ref(site) for g in groups for site in g.sites):
        return []
    # Only this capture's platform: citing the Android harvest on an iOS log is
    # noise, and invites the reader to compare the wrong pair of versions.
    plat = (a.header.platform or "").lower()
    relevant = {k: v for k, v in harvest_versions.items() if k.lower() == plat} \
        or harvest_versions
    at = " / ".join(f"{k} {v}" for k, v in sorted(relevant.items()))
    seen = [v for v in (a.header.sdk_versions or []) if v]
    mine = f" This capture reports {' / '.join(seen)}." if seen else ""
    return [
        f"> Source links are line-accurate as of **{at}**.{mine} A link is a call "
        f"*site*: if the capture is older, read the file at that release and "
        f"locate the call by its literal rather than jumping to the line.",
        "",
    ]


def _sec_warnings_errors(a: Analysis, red: Redactor,
                         harvest_versions: Optional[dict] = None) -> list[str]:
    out = ["## Warnings & Errors", ""]
    e_total = sum(g.count for g in a.error_groups)
    w_total = sum(g.count for g in a.warning_groups)
    out.append(f"Errors: {len(a.error_groups)} groups / {e_total:,} records · "
               f"Warnings: {len(a.warning_groups)} groups / {w_total:,} records")
    out.append("")
    out += _source_link_note(a, harvest_versions)
    if a.error_groups:
        shown = a.error_groups[:MAX_ERROR_GROUPS]
        title = "### Errors" + (f" (top {len(shown)} of {len(a.error_groups)} groups)"
                               if len(a.error_groups) > len(shown) else "")
        out += [title, ""]
        for g in shown:
            out += _group_lines(g, red)
    if a.warning_groups:
        shown = a.warning_groups[:MAX_WARN_GROUPS]
        title = "### Warnings" + (f" (top {len(shown)} of {len(a.warning_groups)} groups)"
                                 if len(a.warning_groups) > len(shown) else "")
        out += [title, ""]
        for g in shown:
            out += _group_lines(g, red)
    if not a.error_groups and not a.warning_groups:
        out += ["No WARN/ERROR-severity records appear in the capture.", ""]
    return out


def _sec_health(a: Analysis, red: Redactor) -> list[str]:
    R = red.redact
    out = ["## Health", ""]

    # ── HTTP ─────────────────────────────────────────────────────────────────
    h = a.http
    out += ["### HTTP", ""]
    out += _table(["metric", "value"], [
        ["post attempts", h.post_attempts],
        ["responses", _hist(h.statuses)],
        ["retry markers (retry>0)", h.retries],
        ["flush attempts", h.flush_attempts],
        ["watchdog armed / fired", f"{h.watchdog_arms} / {h.watchdog_fires}"],
        ["final queue depth", f"{_fmt(h.final_queue_depth)}"
         + (f" (`{R(h.queue_depth_evidence)}`)" if h.queue_depth_evidence else "")],
        ["connectivity events", len(h.connectivity)],
        ["flushes ≤60 s after reconnect", h.flushes_after_connectivity],
    ])
    if h.urls:
        out += ["", "**URLs** (classification verdict survives redaction):", ""]
        url_rows = []
        for u in h.urls[:MAX_URLS]:
            shown = red.alias_url(u.url) if red.enabled else u.url
            url_rows.append([shown, _URL_VERDICTS.get(u.classification, u.classification), u.count])
        out += _table(["url", "classification", "count"], url_rows)
        if len(h.urls) > MAX_URLS:
            out.append(f"… {len(h.urls) - MAX_URLS} more urls elided …")
    if h.connectivity:
        conn_rows = [[c.ts, _fmt(c.connected), R(c.detail)[:MAX_EVIDENCE_CHARS]] for c in h.connectivity]
        conn_rows, note = _elide(conn_rows, MAX_CONNECTIVITY)
        out += ["", "**Connectivity timeline:**", ""]
        out += _table(["ts", "connected", "detail"], conn_rows)
        if note:
            out.append(note)

    # ── record-lifecycle parity + pair collapse ──────────────────────────────
    p = a.parity
    out += ["", "### Record lifecycle & pairs", ""]
    out += _table(["metric", "value"], [
        ["persisted / posted / destroyed", f"{p.persisted:,} / {p.posted:,} / {p.destroyed:,}"],
        ["autoSync", _fmt(p.autosync)],
        ["bg-task starts / stops / unpaired", f"{p.bg_task_starts} / {p.bg_task_stops} / {p.bg_task_unpaired}"],
    ])
    if p.imbalance_note:
        out.append(f"- ⚠️ {p.imbalance_note}")
    if a.pairs:
        out.append("")
        out += _table(["pair", "opens", "closes", "paired", "unpaired open", "orphan close"],
                      [[pr.name, pr.firsts, pr.seconds, pr.paired, pr.unpaired_first, pr.orphan_second]
                       for pr in a.pairs])

    # ── geofence ─────────────────────────────────────────────────────────────
    g = a.geofence
    out += ["", "### Geofence", ""]
    out += _table(["metric", "value"], [
        ["transitions ENTER / EXIT / DWELL", f"{g.enters} / {g.exits} / {g.dwells}"],
        ["spurious deliveries ignored", g.spurious],
        ["registrations / removals / stale cleanups", f"{g.registrations} / {g.removals} / {g.stale_cleanups}"],
        ["max registered (cap)", f"{g.registered_max} (of {g.max_geofences})"],
        ["availability events / flaps", f"{len(g.availability_events)} / {g.availability_flaps}"],
    ])
    if g.note:
        out.append(f"- {g.note}")

    # ── motion ───────────────────────────────────────────────────────────────
    m = a.motion
    out += ["", "### Motion", ""]
    out += _table(["metric", "value"], [
        ["activity histogram", _hist(m.activity_histogram)],
        ["confidence median / below threshold", f"{_fmt(m.confidence_median)} / {m.below_confidence_threshold}"],
        ["motion-trigger armed / reset", f"{m.trigger_armed} / {m.trigger_reset}"],
        ["stationary-region exits", m.stationary_exits],
        ["stopTimeout engaged / fired / cancelled",
         f"{m.stoptimeout_engaged} / {m.stoptimeout_fired} / {m.stoptimeout_cancelled}"],
        ["pace changes", m.pace_changes],
        ["moving ratio (approx.)",
         f"{m.moving_ratio:.1%}" if m.moving_ratio is not None else "—"],
    ])

    # ── auth ─────────────────────────────────────────────────────────────────
    au = a.auth
    out += ["", "### Auth", ""]
    out.append(f"{au.total} auth-related records appear in the capture."
               + (" (list truncated)" if au.truncated else ""))
    out.append(f"- *{au.note}*")
    if au.events:
        events = au.events
        if len(events) > MAX_AUTH_QUOTES:
            half = MAX_AUTH_QUOTES // 2
            events = events[:half] + events[-half:]
            out.append(f"- showing first {half} + last {half} of {len(au.events)} kept events:")
        out.append("")
        quoted = "\n\n".join(
            _cap_lines(R(f"{ev.ts or '—'}  [{ev.tag}]\n{ev.raw}"), MAX_AUTH_QUOTE_LINES)
            for ev in events)
        out += _fence(quoted)

    # ── power ────────────────────────────────────────────────────────────────
    pw = a.power
    out += ["", "### Power", ""]
    if pw.power_save_events:
        rows = [[e.ts, e.state_hint, R(e.detail)[:MAX_EVIDENCE_CHARS]] for e in pw.power_save_events]
        rows, note = _elide(rows, MAX_POWER_EVENTS)
        out += ["**Power-save timeline:**", ""]
        out += _table(["ts", "state", "detail"], rows)
        if note:
            out.append(note)
        out.append("")
    else:
        out.append("No power-save-mode change records appear in the capture.")
        out.append("")
    hb_rows = [[hb.segment, _fmt(hb.expected_interval_s), hb.count,
                _fmt(hb.median_interval_s), _fmt(hb.max_interval_s), hb.note or "—"]
               for hb in pw.heartbeat]
    if hb_rows:
        hb_rows, note = _elide(hb_rows, MAX_HEARTBEAT_SEGMENTS)
        out += ["**Heartbeat cadence (per segment, expected vs observed):**", ""]
        out += _table(["seg", "expected s", "count", "median s", "max s", "note"], hb_rows)
        if note:
            out.append(note)
        out.append("")
    dc = pw.duty_cycle
    out += ["**Duty cycle:**", ""]
    out += _table(["metric", "value"], [
        ["span / active hours", f"{_fmt(dc.span_hours)} / {_fmt(dc.active_hours)}"],
        ["fixes per active hour", _fmt(dc.fixes_per_hour)],
        ["% time moving (approx.)", f"{dc.pct_time_moving}%" if dc.pct_time_moving is not None else "—"],
        ["prevent-suspend fires", dc.prevent_suspend_count],
    ])
    out.append("")
    return out


def _sec_end_state(a: Analysis, red: Redactor) -> list[str]:
    R = red.redact
    es = a.end_state
    out = ["## State at end of log", ""]
    out += _table(["field", "value"], [
        ["last record", es.last_ts],
        ["enabled", _fmt(es.enabled) if es.enabled is not None else "does not appear"],
        ["isMoving", _fmt(es.is_moving) if es.is_moving is not None else "does not appear"],
        ["last auth line", R(es.last_auth) if es.last_auth else "does not appear"],
        ["last connectivity", _fmt(es.last_connectivity) if es.last_connectivity is not None else "does not appear"],
        ["HTTP queue depth", _fmt(es.queue_depth) if es.queue_depth is not None else "not observable"],
        ["abrupt end", ("YES — " + es.abrupt_end_evidence) if es.abrupt_end else "no"],
    ])
    if es.tail_record:
        out += ["", "Final record:", ""]
        out += _fence(_cap_lines(R(es.tail_record), MAX_TAIL_LINES))
    out.append("")
    return out


def _sec_anomalies(a: Analysis, red: Redactor) -> list[str]:
    R = red.redact
    out = ["## Anomalies", ""]
    if not a.anomalies:
        out.append("No anomalies were detected by the automated checks "
                   "(see the absence caveat — this is not a health guarantee).")
    for an in a.anomalies:
        ts = f" (at {an.ts})" if an.ts else ""
        out.append(f"- **{an.severity.upper()}** `{an.kind}` — {R(an.detail)}{ts}")
    out.append("")
    return out


def _sec_unknowns(a: Analysis, red: Redactor) -> list[str]:
    R = red.redact
    u = a.unknowns
    out = ["## Unknown lines", ""]
    if not u.classified:
        out.append("Classification annotations are absent (classify stage not run on "
                   "these records) — drift/novel breakdown unavailable.")
        out.append("")
        return out
    rate = f"{u.unknown_rate:.2%}" if u.unknown_rate is not None else "—"
    out += _table(["metric", "value"], [
        ["records", u.total],
        ["matched", u.matched],
        ["drift (probable pattern drift — low interest)", u.drift],
        ["unknown (novel — high interest)", u.unknown],
        ["bridge passthrough (app-authored)", u.passthrough],
        ["unclassified (no annotation)", u.unclassified],
        ["unknown rate", rate],
    ])
    if u.regen_warning:
        out.append("- ⚠️ unknown rate exceeds 5% — vocabulary regeneration advised.")
    if u.drift_examples:
        out += ["", "Drift examples:", ""]
        out += _fence(R("\n".join(u.drift_examples)))
    if u.novel_examples:
        out += ["", "Novel examples:", ""]
        out += _fence(R("\n".join(u.novel_examples)))
    out.append("")
    return out


def _sec_missing_evidence(a: Analysis) -> list[str]:
    hdr = a.header
    cfg = hdr.config
    asks: list[str] = []
    if hdr.device.startswith("unknown"):
        asks.append("Device identity is absent from the capture — ask for the device "
                    "model and OS version (the filename hint, if any, is untrusted).")
    if not cfg.present:
        asks.append("No config dump appears — the capture likely misses the app-launch "
                    "window. Request a log spanning app launch, or the output of "
                    "`getState()`.")
    elif cfg.truncated:
        asks.append("The config dump looks truncated — request the full config via "
                    "`getState()`.")
    if a.timeline.segments and all(s.version is None for s in a.timeline.segments):
        asks.append("No version banner appears — SDK version is unknown; ask which "
                    "plugin/SDK version is installed.")
    if a.http.final_queue_depth is None and (a.parity.persisted or a.http.post_attempts):
        asks.append("Final HTTP queue depth is not observable in the log — request the "
                    "SQLite database for exact pending-record counts.")
    if a.end_state.abrupt_end:
        asks.append("The log ends abruptly mid-record — request a fresh capture taken "
                    "after the incident window.")
    asks.append("Settings screenshots for states the log cannot observe: Location "
                "permission (precise vs approximate), battery optimization / "
                "whitelist state, notification permission.")
    if hdr.platform == "ios" and not hdr.observed_levels:
        asks.append("iOS text exports are level-blind — if deeper severity resolution "
                    "is needed, this is a permanent constraint of the text export "
                    "(no customer-facing DB export exists).")
    out = ["## Missing evidence — ask the customer", ""]
    out += [f"- {ask}" for ask in asks]
    out.append("")
    return out


def _sec_footer(red: Redactor) -> list[str]:
    n = len(red.mapping())
    if not red.enabled:
        return ["---", "*Unredacted rendering — never paste into public issues.*"]
    return ["---",
            f"*Pseudonymized: {n} alias(es) allocated. The alias→original mapping is "
            f"written to a local file only and must never be shared.*"]
