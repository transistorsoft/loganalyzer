"""Tracking sessions — the capture subdivided into distinct outings.

A capture routinely spans days. A tracking session is one contiguous run of
location fixes, and the silences between them are where the interesting
failures live.

This is a FINDING about the capture, not a rendering concern, which is why it
sits beside `analyze.py` rather than inside an emitter: the map draws sessions
on its time navigator, and the digest can table them, from the same computation.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any, Optional

from .analyze import field, gap_list, iso, parse_iso
from .locations import Fix, timed_fixes
from .model import Record

# What ends a tracking session: this much silence in the LOCATION stream.
#
# Deliberately not the analyzer's record-level GAP_THRESHOLD_S. An idle device
# still logs — heartbeats, scheduler alarms, connectivity — so splitting on
# record silence turns a quiet night into dozens of 10-record "sessions" (82 of
# them in one 4-day Pixel 6 capture, most with no fix at all). A session is a
# stretch of TRACKING, so it is the fixes that define it.
#
# Kept >= GAP_THRESHOLD_S on purpose: that makes every session boundary also a
# track-segment boundary (emit/geojson splits the track at 900 s), so a session
# is always a whole number of track segments and the map's clipping stays exact.
SESSION_GAP_S = 20 * 60

# The launch that starts a session and the stopTimeout/park tail that ends one
# bracket the fixes rather than sitting between them. Without this padding,
# selecting a session hides the very records that explain how it began and how
# it ended.
SESSION_PAD_S = 300


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    dlon, dlat = radians(lon2 - lon1), radians(lat2 - lat1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return 2 * 6371000.0 * asin(min(1.0, sqrt(a)))


def _runs(fixes: list[Fix]) -> list[list[Fix]]:
    """Fixes grouped into runs, split wherever the location stream went quiet
    for SESSION_GAP_S. A run of one fix is not an outing — an hourly heartbeat
    reporting a single position is not tracking — so it is dropped."""
    runs: list[list[Fix]] = [[fixes[0]]]
    for prev, fx in zip(fixes, fixes[1:]):
        if (fx.t - prev.t).total_seconds() >= SESSION_GAP_S:   # type: ignore[operator]
            runs.append([])
        runs[-1].append(fx)
    return [r for r in runs if len(r) >= 2]


def _silences(analysis: Any) -> list[tuple[datetime, datetime, str, float, str]]:
    """The analyzer's classified gaps, chronologically."""
    out = []
    for g in gap_list(analysis):
        s, e = parse_iso(field(g, "start_ts")), parse_iso(field(g, "end_ts"))
        if s and e:
            out.append((s, e, str(field(g, "classification", "gap")),
                        float(field(g, "duration_s", 0.0) or 0.0),
                        str(field(g, "app_state", "") or "")))
    return sorted(out, key=lambda x: x[0])


def build_sessions(analysis: Any, records: list[Record]) -> list[dict]:
    """-> [{i, start, end, duration_s, records, fixes, distance_m,
            ended_by, gap_s, app_state}], one per tracking session.

    Each session reports what ENDED it, taken from the analyzer's classified
    gap in the silence that follows (death / scheduler-window / suspension /
    wedge-candidate). On a stuck-tracking ticket the session ending in a
    `wedge-candidate` is the one to look at, and that verdict belongs on the
    session rather than buried in a separate gap list.
    """
    timed = [r for r in records if r.ts is not None]
    fixes = timed_fixes(records)
    if not timed or len(fixes) < 2:
        return []
    runs = _runs(fixes)
    if not runs:
        return []

    classified = _silences(analysis)
    pad = timedelta(seconds=SESSION_PAD_S)
    first_ts, last_ts = timed[0].ts, timed[-1].ts

    out: list[dict] = []
    for k, run in enumerate(runs):
        a, b = run[0].t, run[-1].t
        start, end = a - pad, b + pad                     # type: ignore[operator]
        # Never let padding reach into a neighbour: split the silence evenly.
        if k > 0:
            start = max(start, runs[k - 1][-1].t + (a - runs[k - 1][-1].t) / 2)
        if k + 1 < len(runs):
            end = min(end, b + (runs[k + 1][0].t - b) / 2)
        start = max(start, first_ts)                      # type: ignore[arg-type]
        end = min(end, last_ts)                           # type: ignore[arg-type]

        limit = runs[k + 1][0].t if k + 1 < len(runs) else None
        ended_by, gap_s, app_state = _closed_by(classified, b, limit)

        out.append({
            "i": len(out) + 1,
            "start": iso(start),
            "end": iso(end),
            "duration_s": round((end - start).total_seconds(), 1),
            "records": sum(1 for r in timed if start <= r.ts <= end),  # type: ignore[operator]
            "fixes": len(run),
            "distance_m": round(sum(_haversine_m(p.lon, p.lat, q.lon, q.lat)
                                    for p, q in zip(run, run[1:])), 1),
            "ended_by": ended_by,
            "gap_s": gap_s,
            "app_state": app_state,
        })
    return out


def _closed_by(classified: list, after: datetime, before: Optional[datetime]):
    """The longest classified silence between this session's last fix and the
    next session's first -> (classification, duration_s, app_state)."""
    best = 0.0
    found: tuple[Optional[str], Optional[float], Optional[str]] = (None, None, None)
    for s, _e, cls, dur, state in classified:
        if s < after or (before is not None and s >= before):
            continue
        if dur > best:
            best, found = dur, (cls, round(dur, 1), state or None)
    return found
