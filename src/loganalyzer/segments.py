"""Stage 1.5 — banner/launch pre-pass: split records into process lifetimes
and assign a per-segment SDK version. Launch-scoped counters (startId, gate
epochs) reset at these boundaries.
"""
from __future__ import annotations

import re

from .model import ANDROID, IOS, Record, Segment

# Android boot banner: "║ TSLocationManager version: 4.2.1 (4063)"
_ANDROID_VERSION = re.compile(r"TSLocationManager version:\s*([\d.]+)")
# iOS init banner: "║ TSLocationManager (build 388)" — build number only;
# marketing version resolves via vocabulary/ios-builds.yaml.
_IOS_BUILD = re.compile(r"TSLocationManager \(build (\d+)\)")
_IOS_HEADLESS = re.compile(r"Booted in background|didLaunchInBackground=1")


def split_segments(records: list[Record], build_map: dict[str, str] | None = None) -> list[Segment]:
    build_map = build_map or {}
    segments: list[Segment] = []

    def open_segment(first_seq: int) -> Segment:
        seg = Segment(index=len(segments), first_seq=first_seq, last_seq=first_seq, version=None)
        segments.append(seg)
        return seg

    cur: Segment | None = None
    for rec in records:
        banner_version = None
        source = ""
        if rec.platform == ANDROID:
            m = _ANDROID_VERSION.search(rec.raw)
            if m:
                banner_version, source = m.group(1), "banner"
        else:
            m = _IOS_BUILD.search(rec.raw)
            if m:
                build = m.group(1)
                banner_version = build_map.get(build, f"build {build}")
                source = "build-map" if build in build_map else "banner"

        if banner_version is not None:
            # A version banner marks a fresh process launch: open a new segment
            # unless the current one is still version-less and only just began
            # (mid-session captures may open with ordinary records).
            if cur is None or cur.version is not None or rec.seq - cur.first_seq > 3:
                cur = open_segment(rec.seq)
            cur.version, cur.version_source = banner_version, source

        if cur is None:
            cur = open_segment(rec.seq)
            cur.version_source = "unknown"
        cur.last_seq = rec.seq
        rec.segment = cur.index

        if rec.platform == IOS and _IOS_HEADLESS.search(rec.raw):
            cur.launched_headless = True
    return segments
