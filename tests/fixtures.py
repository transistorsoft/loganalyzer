"""Where the test corpus comes from.

Two tiers, checked in order:

1. `tests/fixtures/` — committed, publishable captures. These are real logs
   whose coordinates have been moved by one rigid transform (see
   loganalyzer-forge/make_fixture.py): every distance, speed, bearing, gap and
   session boundary is preserved exactly, but the route is somewhere nobody has
   been. This is what a public clone runs against.

2. A private corpus — `$LOGANALYZER_FIXTURES`, else `../../tmp` relative to the
   repo, which is where it lands when this repo is vendored into Transistor's
   monorepo. Holds customer captures that can never be published, so these
   tests skip anywhere else.

`fixture("car")` returns the best available path, or None so the test skips.
Names with no committed counterpart (customer-supplied logs) resolve only from
the private corpus and simply skip elsewhere.
"""
from __future__ import annotations

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_COMMITTED = _HERE / "fixtures"

# short name -> (committed filename or None, private-corpus filename)
_NAMES: dict[str, tuple[str | None, str]] = {
    "car":      ("car.log.gz",      "background-geolocation-car.log"),
    "bike":     ("bike.log.gz",     "background-geolocation-bike.log"),
    "slc":      ("slc.log.gz",      "background-geolocation-slc.log"),
    "slc-walk": ("slc-walk.log.gz", "background-geolocation-slc-walk.log"),
    # Customer-supplied — never published. Private corpus only.
    "log62":    (None,              "background-geolocation (62).log"),
    "big-ios":  (None,              "background-geolocation-ios.log.gz"),
}


def private_corpus() -> Path:
    raw = os.environ.get("LOGANALYZER_FIXTURES")
    if raw:
        return Path(raw).expanduser()
    return (_HERE.parent / ".." / ".." / "tmp").resolve()


def fixture(name: str) -> Path | None:
    """Best available path for a named capture, or None if absent."""
    committed, private = _NAMES[name]
    if committed:
        path = _COMMITTED / committed
        if path.exists():
            return path
    path = private_corpus() / private
    return path if path.exists() else None


def have(name: str) -> bool:
    return fixture(name) is not None


def read(name_or_path) -> str:
    """Fixture text, transparently un-gzipping. Committed fixtures are stored
    gzipped (a 1.5 MB capture is 68 KB), so tests must never assume plain text
    — `.read_text()` on a .gz silently yields mojibake and an empty parse."""
    import gzip
    path = fixture(name_or_path) if isinstance(name_or_path, str) else Path(name_or_path)
    if path is None:
        raise FileNotFoundError(name_or_path)
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
    return path.read_text(encoding="utf-8", errors="replace")
