"""Loader for vocabulary/map-rules.yaml — how a log event is presented on the map.

Presentation rules used to be hardcoded across emit/geojson.py and emit/map.py.
They are data, they change often, and several are platform-specific, so they
live in YAML: editing that file is the supported way to change how an event
looks, with no Python change.

Rules are ordered and scoped to a layer; platform-specific entries are checked
before shared ones so a platform can override. Regexes are compiled once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

RULES_FILE = Path(__file__).parent / "vocabulary" / "map-rules.yaml"


@dataclass
class Rule:
    layer: Optional[str]
    pattern: re.Pattern
    icon: Optional[str] = None
    tint: Optional[str] = None
    stationary_region: bool = False
    platform: Optional[str] = None
    note: str = ""

    def applies(self, platform: str, layer: str) -> bool:
        if self.platform and self.platform != platform:
            return False
        return self.layer is None or self.layer == layer


@dataclass
class Suppression:
    reason: str
    pattern: Optional[re.Pattern] = None
    classes: tuple[str, ...] = ()
    unless: Optional[re.Pattern] = None
    platform: Optional[str] = None
    when: Optional[str] = None      # capture-level condition, e.g. mock_locations

    def matches(self, platform: str, raw: str, tag_class: str,
                context: Optional[dict] = None) -> bool:
        if self.platform and self.platform != platform:
            return False
        # `when` gates on a property of the CAPTURE, not the record: some noise
        # is only noise in context (stop-detection warnings are expected once
        # mock locations are being injected).
        if self.when and not (context or {}).get(self.when):
            return False
        if self.classes:
            if tag_class not in self.classes:
                return False
            # class-scoped: suppressed UNLESS it looks like a real problem
            return not (self.unless and self.unless.search(raw))
        return bool(self.pattern and self.pattern.search(raw))


@dataclass
class MapRules:
    layers: dict[str, dict] = field(default_factory=dict)
    events: dict[str, dict] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    suppress: list[Suppression] = field(default_factory=list)
    tints: dict[str, str] = field(default_factory=dict)
    geofence_actions: dict[str, str] = field(default_factory=dict)
    navigator: dict = field(default_factory=dict)

    # ── per-layer lookups ────────────────────────────────────────────────────
    # `layers` is the single definition of everything per-layer, key order
    # included. Consumers ask here rather than keeping their own tables.

    def layer_order(self) -> list[str]:
        """Layer names in declared order — panel order and build order."""
        return list(self.layers)

    def layer_icon(self, layer: str) -> Optional[str]:
        return (self.layers.get(layer) or {}).get("icon")

    def layer_clock(self, layer: str, default: int = 12) -> int:
        return (self.layers.get(layer) or {}).get("clock", default)

    def layer_glyph(self, layer: str, default: str = "🔵") -> str:
        return (self.layers.get(layer) or {}).get("glyph", default)

    def layer_display(self) -> dict[str, dict]:
        """layer -> {label, kind, glyph} for the renderer's layer control."""
        return {name: {"label": cfg.get("label", name),
                       "kind": cfg.get("kind", "marker"),
                       "glyph": cfg.get("glyph", "🔵")}
                for name, cfg in self.layers.items() if isinstance(cfg, dict)}

    def offset_clocks(self) -> dict[str, int]:
        return {name: cfg["clock"] for name, cfg in self.layers.items()
                if isinstance(cfg, dict) and "clock" in cfg}

    def layer_icons(self) -> dict[str, str]:
        return {name: cfg["icon"] for name, cfg in self.layers.items()
                if isinstance(cfg, dict) and cfg.get("icon")}

    def event_icon(self, event: Optional[str]) -> Optional[str]:
        return (self.events.get(event or "") or {}).get("icon")

    def bulk_hide_above(self) -> dict[str, int]:
        return {name: cfg["bulk_hide_above"] for name, cfg in self.layers.items()
                if isinstance(cfg, dict) and "bulk_hide_above" in cfg}

    def match(self, platform: str, layer: str, raw: str) -> Optional[Rule]:
        """First matching rule for this platform+layer (platform-specific first)."""
        for want_platform in (True, False):
            for rule in self.rules:
                if bool(rule.platform) != want_platform:
                    continue
                if rule.applies(platform, layer) and rule.pattern.search(raw):
                    return rule
        return None

    def suppressed(self, platform: str, raw: str, tag_class: str = "",
                   context: Optional[dict] = None) -> Optional[str]:
        """-> the reason this record is not mapped, or None."""
        for s in self.suppress:
            if s.matches(platform, raw, tag_class, context):
                return s.reason
        return None


def _compile(pat: Any) -> re.Pattern:
    return re.compile(str(pat))


@lru_cache(maxsize=4)
def load_rules(path: Path | None = None) -> MapRules:
    """Parse map-rules.yaml. Cached — the file is read once per process."""
    src = Path(path or RULES_FILE)
    if not src.exists():                      # degrade rather than crash
        return MapRules()
    doc = yaml.safe_load(src.read_text()) or {}
    rules = [
        Rule(layer=r.get("layer"), pattern=_compile(r["match"]), icon=r.get("icon"),
             tint=r.get("tint"), stationary_region=bool(r.get("stationary_region")),
             platform=r.get("platform"), note=r.get("note", ""))
        for r in doc.get("rules", []) if r.get("match")
    ]
    suppress = [
        Suppression(
            reason=s.get("reason", ""),
            pattern=_compile(s["match"]) if s.get("match") else None,
            classes=tuple(s.get("classes", ())),
            unless=_compile(s["unless_match"]) if s.get("unless_match") else None,
            platform=s.get("platform"),
            when=s.get("when"),
        )
        for s in doc.get("suppress", [])
    ]
    return MapRules(
        layers=doc.get("layers", {}) or {},
        events=doc.get("events", {}) or {},
        rules=rules,
        suppress=suppress,
        tints=doc.get("tints", {}) or {},
        geofence_actions=doc.get("geofence_actions", {}) or {},
        navigator=doc.get("navigator", {}) or {},
    )
