#!/usr/bin/env python3
"""Vendor the Lucide icons the map uses into src/loganalyzer/emit/icons.py.

Only the icons in ICON_SET are vendored — a few KB of geometry, no runtime
dependency, no CDN (maps must stay self-contained). Lucide icons are
stroke-based and multi-element, so each SVG is converted to a small list of
canvas draw ops the map's renderer replays on a 24x24 grid.

Usage:
    python scripts/gen_icons.py <path-to-lucide-static/package/icons>

Lucide is ISC-licensed; the license text is copied into the generated module.
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# marker concept -> lucide icon name
ICON_SET = {
    "launch": "power",
    "terminate": "power-off",
    "headless": "monitor-off",
    "geofence": "map-pin-check",
    "geofence-suppressed": "map-pin-x",
    "stationary": "circle-parking",
    # The two opposite ends of a stop/start cycle, drawn as opposites:
    # breaking OUT of the stationary fence vs settling INTO stationary.
    "stationary-exit": "circle-arrow-out-up-right",
    "stop-timeout": "anchor",
    "http": "cloud-upload",
    "http-error": "cloud-off",
    "connectivity": "wifi",
    "connectivity-off": "wifi-off",
    "provider": "satellite-dish",
    "heartbeat": "heart-pulse",
    "motion-vehicle": "car-front",
    "motion-foot": "footprints",
    "rejection": "ban",
    "warning": "triangle-alert",
    "error": "octagon-alert",
    "gap": "hourglass",
    "mock": "bug",
    "config": "settings",
    # app-state: awake vs asleep reads instantly, unlike a gear for everything
    "foreground": "sun",
    "background": "moon",
    "focus-gained": "eye",
    "focus-lost": "eye-off",
    "auth-problem": "shield-alert",
    "persistence": "database",
    "power-save": "battery-low",
    "schedule": "calendar",
    "timer": "clock",
    "event": "radio-tower",
    "route": "route",
}

_NUM = re.compile(r"-?\d*\.?\d+(?:e-?\d+)?")


def _floats(s: str) -> list[float]:
    return [float(x) for x in _NUM.findall(s or "")]


def convert(svg_path: Path) -> list[list]:
    """SVG -> [[op, ...args], ...] on Lucide's native 24x24 grid.

    Ops: ["p", "<svg path d>"] | ["c", cx, cy, r] | ["l", x1, y1, x2, y2]
         ["r", x, y, w, h, rx] | ["e", cx, cy, rx, ry] | ["y", "x,y x,y …"]
    """
    root = ET.parse(svg_path).getroot()
    ops: list[list] = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        a = el.attrib
        if tag == "path" and a.get("d"):
            ops.append(["p", a["d"]])
        elif tag == "circle":
            ops.append(["c", float(a["cx"]), float(a["cy"]), float(a["r"])])
        elif tag == "line":
            ops.append(["l", float(a["x1"]), float(a["y1"]),
                        float(a["x2"]), float(a["y2"])])
        elif tag == "rect":
            ops.append(["r", float(a["x"]), float(a["y"]), float(a["width"]),
                        float(a["height"]), float(a.get("rx", 0) or 0)])
        elif tag == "ellipse":
            ops.append(["e", float(a["cx"]), float(a["cy"]),
                        float(a["rx"]), float(a["ry"])])
        elif tag in ("polyline", "polygon"):
            ops.append(["y", a.get("points", "").strip(), tag == "polygon"])
    if not ops:
        raise SystemExit(f"no drawable elements in {svg_path}")
    return ops


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit(__doc__)
    icon_dir = Path(argv[0])
    license_file = icon_dir.parent / "LICENSE"
    icons = {}
    for concept, name in sorted(ICON_SET.items()):
        svg = icon_dir / f"{name}.svg"
        if not svg.exists():
            raise SystemExit(f"missing lucide icon: {name}.svg")
        icons[concept] = {"lucide": name, "ops": convert(svg)}

    out = Path(__file__).resolve().parents[1] / "src/loganalyzer/emit/icons.py"
    body = json.dumps(icons, separators=(",", ":"), sort_keys=True)
    lic = (license_file.read_text().strip() if license_file.exists()
           else "ISC License — Lucide Icons and Contributors")
    out.write_text(
        '"""Vendored Lucide icon geometry for the map renderer — GENERATED.\n\n'
        "Regenerate with:  python scripts/gen_icons.py <lucide-static/package/icons>\n\n"
        "Only the icons the map actually uses are vendored (a few KB of geometry),\n"
        "so a published map stays self-contained: no CDN, no webfont, no runtime dep.\n"
        "Each entry is a list of canvas draw ops on Lucide's native 24x24 grid;\n"
        "emit/map.py replays them with a stroke, scaled to the marker size.\n\n"
        "Icons are from Lucide (https://lucide.dev), used under the ISC License:\n\n"
        + "\n".join("    " + ln for ln in lic.splitlines())
        + '\n"""\nfrom __future__ import annotations\n\n'
        f"LUCIDE_VIEWBOX = 24\n\nICONS: dict = {body}\n"
    )
    print(f"wrote {out} — {len(icons)} icons, {out.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
