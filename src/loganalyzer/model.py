"""Core data model — the load-bearing contract shared by every stage.

A Record preserves the original bytes of one logical log entry. Later stages
attach annotations ALONGSIDE the raw text (never replacing it): Stage 3
matches vocabulary patterns against `raw` because harvested patterns span the
Android header/body fold.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

ANDROID = "android"
IOS = "ios"

# Variation selectors that ride along with several icons (⚠️ = U+26A0 U+FE0F,
# and one Android constant embeds a stray U+FE0E). Match icons on base
# codepoints only.
_VARIATION_SELECTORS = "︎️"

_EMOJI_RANGES = (
    "\U0001f000-\U0001faff"  # supplemental symbols, pictographs, transport
    "⌀-⏿"     # misc technical: ⏰ ⏱ ⏳ ⏸ ⏹ ⏺
    "■-◿"     # geometric shapes: ▶ ◀ (starts past box-drawing ╔═║)
    "☀-➿"          # misc symbols, dingbats (⚠ ✅ ⚡)
    "⬀-⯿"
    "‼⁉"           # ‼ ⁉
    "ℹ"                 # ℹ
)
EMOJI_TOKEN = re.compile(rf"[{_EMOJI_RANGES}][{_VARIATION_SELECTORS}]?")


def normalize_emoji(s: str) -> str:
    """Strip variation selectors so ⚠️ and ⚠ compare equal."""
    return s.translate({0xFE0E: None, 0xFE0F: None})


@dataclass
class Record:
    platform: str                       # ANDROID | IOS
    seq: int                            # 0-based record index within the file
    line_no: int                        # 1-based first physical line number
    ts: Optional[datetime]              # resolved absolute timestamp (device-local)
    ts_raw: str                         # timestamp text exactly as it appeared
    level: Optional[str]                # DEBUG/INFO/WARN/ERROR — None for iOS text exports
    icon: Optional[str]                 # normalized level-slot emoji ("⚠", "🔵", "📌🔒", …)
    tag_class: Optional[str]            # e.g. "TSGeofenceManager"
    tag_method_raw: Optional[str]       # method/selector as printed (may carry [λ], _block_invoke)
    tag_method: Optional[str]           # normalized (suffixes stripped)
    header_msg: str                     # message portion of the header line ("" is common)
    body: list[str] = field(default_factory=list)   # continuation lines, verbatim
    raw: str = ""                       # full re-joined text: header line + "\n" + body lines
    # Annotations attached by later stages (never mutate raw/body):
    structs: dict[str, Any] = field(default_factory=dict)     # Stage 2 mini-parser results
    klass: Optional["Classification"] = None                  # Stage 3 result
    segment: int = 0                    # Stage 1.5 process-lifetime index

    @property
    def severity(self) -> str:
        """union(level, icon) — level/icon mismatches are author-confirmed accidental,
        so a record is warning/error if EITHER axis says so."""
        icon = self.icon or ""
        if self.level == "ERROR" or "‼" in icon or "❌" in icon:
            # ❌ is ERROR only on iOS <= the 2026-08 standardization; the
            # classifier refines this per-platform/version. At record level we
            # treat it as error-ish for iOS and let classify.py demote the
            # Android-CANCEL case.
            if self.platform == ANDROID and self.level != "ERROR" and "❌" in icon:
                pass
            else:
                return "error"
        if self.level == "WARN" or "⚠" in icon:
            return "warning"
        return "normal"

    def first_body_line(self) -> str:
        return self.body[0] if self.body else ""


@dataclass
class Classification:
    """Stage 3 output for one record."""
    status: str                         # "matched" | "drift" | "unknown" | "passthrough"
    sites: list[str] = field(default_factory=list)   # ["path/File.java:123", …] multi-candidate
    confidence: str = ""                # "exact" | "ambiguous" | "fuzzy" | ""
    semantic: str = ""                  # vocabulary semantic key (e.g. "http.response")
    family: str = ""                    # "severity" | "state" | "domain" | ""
    category: str = ""                  # digest/map layer bucket (e.g. "lifecycle", "geofence")
    drift_of: str = ""                  # populated when status == "drift"


@dataclass
class Segment:
    """One process lifetime (Stage 1.5)."""
    index: int
    first_seq: int
    last_seq: int
    version: Optional[str]              # SDK version if a banner resolved it
    version_source: str = ""            # "banner" | "build-map" | "unknown"
    launched_headless: bool = False
