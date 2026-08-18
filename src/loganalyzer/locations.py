"""Which coordinates in a log are POSITIONS THE DEVICE REPORTED.

A log is full of latitude/longitude pairs, and most of them are not fixes. A
geofence trigger point, a fence-center comparison, a stationary anchor, an A:/B:
pair — those are evidence *about a decision*, quoted back by the SDK. Treating
them as location updates invents GPS points (29% of the "fixes" in one fixture
were these) and bends any route drawn through them.

This module answers that one question and extracts the fixes. It sits below
both the analysis and the emitters: `sessions.py` needs fixes to find tracking
sessions, `emit/geojson.py` needs them for the track and the fixes layer, and
neither should own the definition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .model import Record


@dataclass
class Fix:
    """One position the device actually reported."""
    t: Optional[datetime]
    lon: float
    lat: float
    rec: Record
    loc: dict


# Coordinates a record merely REFERS to — never a location update.
REFERENCE_COORDS = re.compile(
    r"Trigger vs |Trigger=|Stationary=|Ignoring \w+ geofence|Deferring geofence"
    r"|^\s*╟?─?\s*[AB]:\s*[-\d]", re.M)

# Diagnostic banners that carry the SPATIAL evidence for a trigger: the trigger
# fix itself plus its distance to the fence. The coordinates in them are
# references, which is why the pattern lives here — but they are also the most
# map-worthy geofence records in a log (a spurious trigger is visible as a
# marker sitting hundreds of metres outside its own fence), so emit/geojson.py
# imports it for routing too.
GF_DIAGNOSTIC = re.compile(
    r"Trigger vs (?:Geofence center|last location)"
    r"|Ignoring (?:spurious|duplicate|deferred|stationary) geofence"
    r"|Deferring geofence transition"
    r"|Synthesizing missed-ENTER"
    r"|Updated geofence state"
    r"|Normalizing stale (?:PENDING_EXIT|PENDING_ENTER)")


def has_reference_coords(rec: Record) -> bool:
    """True when this record's coordinates describe something the SDK was
    reasoning about, not somewhere the device reported being."""
    return bool(REFERENCE_COORDS.search(rec.raw) or GF_DIAGNOSTIC.search(rec.raw))


def collect_fixes(records: list[Record]) -> list[Fix]:
    """Every device-reported position, in record order."""
    fixes: list[Fix] = []
    for rec in records:
        if has_reference_coords(rec):
            continue
        locs = rec.structs.get("locations")
        if not locs:
            one = rec.structs.get("location")
            locs = [one] if isinstance(one, dict) else []
        for loc in locs:
            if not isinstance(loc, dict):
                continue
            lat, lon = loc.get("lat"), loc.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            fixes.append(Fix(t=rec.ts, lon=float(lon), lat=float(lat),
                             rec=rec, loc=loc))
    return fixes


def timed_fixes(records: list[Record]) -> list[Fix]:
    """Fixes that carry a timestamp, chronologically. Anything reasoning about
    *when* the device was somewhere needs this, not collect_fixes()."""
    return sorted((f for f in collect_fixes(records) if f.t is not None),
                  key=lambda f: f.t)                    # type: ignore[arg-type,return-value]
