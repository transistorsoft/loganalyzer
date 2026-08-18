"""Stage 0 — input sniffing: platform, format, compression, dedupe.

Platform is decided by GRAMMAR, never by filename (real-world counterexample:
customer files named background-geolocation-bike.log that are iOS captures).
The filename survives only as an untrusted device-hint label.
"""
from __future__ import annotations

import gzip
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .model import ANDROID, IOS

_ANDROID_HEADER = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} (?:DEBUG|INFO|WARN|ERROR) \[")
_IOS_HEADER = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")


@dataclass
class Source:
    path: Path
    kind: str                 # "text" | "db"
    platform: str             # ANDROID | IOS
    text: str = ""            # decoded content (text sources)
    duplicate_of: Path | None = None      # byte-identical or byte-prefix of another input
    filename_hint: str = ""   # untrusted label derived from the filename
    notes: list[str] = field(default_factory=list)


def _read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".gz" or data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _sniff_db(path: Path) -> Source | None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    except sqlite3.Error:
        return None
    if "logging_event" in tables:
        return Source(path=path, kind="db", platform=ANDROID)
    if "logs" in tables:
        return Source(path=path, kind="db", platform=IOS)
    return None


def sniff_platform(text: str) -> str | None:
    """First grammar hit wins; sample the first 200 non-blank lines."""
    seen = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if _ANDROID_HEADER.match(line):
            return ANDROID
        if _IOS_HEADER.match(line):
            return IOS
        seen += 1
        if seen > 200:
            break
    return None


def load_sources(paths: list[Path]) -> list[Source]:
    sources: list[Source] = []
    raw_cache: dict[Path, bytes] = {}
    for path in paths:
        if path.suffix in (".db", ".sqlite") or path.name.endswith("transistor_log.db"):
            db = _sniff_db(path)
            if db is not None:
                db.filename_hint = path.stem
                sources.append(db)
                continue
        data = _read_bytes(path)
        raw_cache[path] = data
        text = data.decode("utf-8", errors="replace")
        platform = sniff_platform(text)
        if platform is None:
            db = _sniff_db(path)
            if db is not None:
                db.filename_hint = path.stem
                sources.append(db)
                continue
            src = Source(path=path, kind="text", platform="unknown", text=text,
                         filename_hint=path.stem)
            src.notes.append("unrecognized grammar — not an SDK log?")
            sources.append(src)
            continue
        sources.append(Source(path=path, kind="text", platform=platform, text=text,
                              filename_hint=path.stem))

    # Dedupe: byte-identical and byte-prefix (slc.log was an exact prefix of
    # slc-walk.log; car 2.log was byte-identical to car.log).
    texts = [(s, raw_cache.get(s.path)) for s in sources if s.kind == "text"]
    for i, (a, da) in enumerate(texts):
        if da is None or a.duplicate_of:
            continue
        for b, db_ in texts[i + 1:]:
            if db_ is None or b.duplicate_of:
                continue
            if da == db_:
                b.duplicate_of = a.path
            elif len(da) < len(db_) and db_.startswith(da):
                a.duplicate_of = b.path      # a is a prefix of b — keep the longer capture
            elif len(db_) < len(da) and da.startswith(db_):
                b.duplicate_of = a.path
    return sources
