"""Vendored Lucide icon geometry for the map renderer — GENERATED.

Regenerate with:  python scripts/gen_icons.py <lucide-static/package/icons>

Only the icons the map actually uses are vendored (a few KB of geometry),
so a published map stays self-contained: no CDN, no webfont, no runtime dep.
Each entry is a list of canvas draw ops on Lucide's native 24x24 grid;
emit/map.py replays them with a stroke, scaled to the marker size.

Icons are from Lucide (https://lucide.dev), used under the ISC License:

    ISC License
    
    Copyright (c) 2026 Lucide Icons and Contributors
    
    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted, provided that the above
    copyright notice and this permission notice appear in all copies.
    
    THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
    WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
    MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
    ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
    WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
    ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
    OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
    
    ---
    
    The following Lucide icons are derived from the Feather project:
    
    airplay, alert-circle, alert-octagon, alert-triangle, aperture, arrow-down-circle, arrow-down-left, arrow-down-right, arrow-down, arrow-left-circle, arrow-left, arrow-right-circle, arrow-right, arrow-up-circle, arrow-up-left, arrow-up-right, arrow-up, at-sign, calendar, cast, check, chevron-down, chevron-left, chevron-right, chevron-up, chevrons-down, chevrons-left, chevrons-right, chevrons-up, circle, clipboard, clock, code, columns, command, compass, corner-down-left, corner-down-right, corner-left-down, corner-left-up, corner-right-down, corner-right-up, corner-up-left, corner-up-right, crosshair, database, divide-circle, divide-square, dollar-sign, download, external-link, feather, frown, hash, headphones, help-circle, info, italic, key, layout, life-buoy, link-2, link, loader, lock, log-in, log-out, maximize, meh, minimize, minimize-2, minus-circle, minus-square, minus, monitor, moon, more-horizontal, more-vertical, move, music, navigation-2, navigation, octagon, pause-circle, percent, plus-circle, plus-square, plus, power, radio, rss, search, server, share, shopping-bag, sidebar, smartphone, smile, square, table-2, tablet, target, terminal, trash-2, trash, triangle, tv, type, upload, x-circle, x-octagon, x-square, x, zoom-in, zoom-out
    
    The MIT License (MIT) (for the icons listed above)
    
    Copyright (c) 2013-present Cole Bemis
    
    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:
    
    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.
    
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""
from __future__ import annotations

LUCIDE_VIEWBOX = 24

ICONS: dict = {"auth-problem":{"lucide":"shield-alert","ops":[["p","M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"],["p","M12 8v4"],["p","M12 16h.01"]]},"background":{"lucide":"moon","ops":[["p","M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401"]]},"config":{"lucide":"settings","ops":[["p","M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"],["c",12.0,12.0,3.0]]},"connectivity":{"lucide":"wifi","ops":[["p","M12 20h.01"],["p","M2 8.82a15 15 0 0 1 20 0"],["p","M5 12.859a10 10 0 0 1 14 0"],["p","M8.5 16.429a5 5 0 0 1 7 0"]]},"connectivity-off":{"lucide":"wifi-off","ops":[["p","M12 20h.01"],["p","M8.5 16.429a5 5 0 0 1 7 0"],["p","M5 12.859a10 10 0 0 1 5.17-2.69"],["p","M19 12.859a10 10 0 0 0-2.007-1.523"],["p","M2 8.82a15 15 0 0 1 4.177-2.643"],["p","M22 8.82a15 15 0 0 0-11.288-3.764"],["p","m2 2 20 20"]]},"error":{"lucide":"octagon-alert","ops":[["p","M12 16h.01"],["p","M12 8v4"],["p","M15.312 2a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586l-4.688-4.688A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2z"]]},"event":{"lucide":"radio-tower","ops":[["p","M4.9 16.1C1 12.2 1 5.8 4.9 1.9"],["p","M7.8 4.7a6.14 6.14 0 0 0-.8 7.5"],["c",12.0,9.0,2.0],["p","M16.2 4.8c2 2 2.26 5.11.8 7.47"],["p","M19.1 1.9a9.96 9.96 0 0 1 0 14.1"],["p","M9.5 18h5"],["p","m8 22 4-11 4 11"]]},"focus-gained":{"lucide":"eye","ops":[["p","M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"],["c",12.0,12.0,3.0]]},"focus-lost":{"lucide":"eye-off","ops":[["p","M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"],["p","M14.084 14.158a3 3 0 0 1-4.242-4.242"],["p","M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"],["p","m2 2 20 20"]]},"foreground":{"lucide":"sun","ops":[["c",12.0,12.0,4.0],["p","M12 2v2"],["p","M12 20v2"],["p","m4.93 4.93 1.41 1.41"],["p","m17.66 17.66 1.41 1.41"],["p","M2 12h2"],["p","M20 12h2"],["p","m6.34 17.66-1.41 1.41"],["p","m19.07 4.93-1.41 1.41"]]},"gap":{"lucide":"hourglass","ops":[["p","M5 22h14"],["p","M5 2h14"],["p","M17 22v-4.172a2 2 0 0 0-.586-1.414L12 12l-4.414 4.414A2 2 0 0 0 7 17.828V22"],["p","M7 2v4.172a2 2 0 0 0 .586 1.414L12 12l4.414-4.414A2 2 0 0 0 17 6.172V2"]]},"geofence":{"lucide":"map-pin-check","ops":[["p","M19.43 12.935c.357-.967.57-1.955.57-2.935a8 8 0 0 0-16 0c0 4.993 5.539 10.193 7.399 11.799a1 1 0 0 0 1.202 0 32.197 32.197 0 0 0 .813-.728"],["c",12.0,10.0,3.0],["p","m16 18 2 2 4-4"]]},"geofence-suppressed":{"lucide":"map-pin-x","ops":[["p","M19.752 11.901A7.78 7.78 0 0 0 20 10a8 8 0 0 0-16 0c0 4.993 5.539 10.193 7.399 11.799a1 1 0 0 0 1.202 0 19 19 0 0 0 .09-.077"],["c",12.0,10.0,3.0],["p","m21.5 15.5-5 5"],["p","m21.5 20.5-5-5"]]},"headless":{"lucide":"monitor-off","ops":[["p","M12 17v4"],["p","M17 17H4a2 2 0 0 1-2-2V5a2 2 0 0 1 1.184-1.826"],["p","m2 2 20 20"],["p","M8 21h8"],["p","M8.656 3H20a2 2 0 0 1 2 2v10a2 2 0 0 1-.293 1.042"]]},"heartbeat":{"lucide":"heart-pulse","ops":[["p","M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"],["p","M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"]]},"http":{"lucide":"cloud-upload","ops":[["p","M12 13v8"],["p","M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"],["p","m8 17 4-4 4 4"]]},"http-error":{"lucide":"cloud-off","ops":[["p","M10.94 5.274A7 7 0 0 1 15.71 10h1.79a4.5 4.5 0 0 1 4.222 6.057"],["p","M18.796 18.81A4.5 4.5 0 0 1 17.5 19H9A7 7 0 0 1 5.79 5.78"],["p","m2 2 20 20"]]},"launch":{"lucide":"power","ops":[["p","M12 2v10"],["p","M18.4 6.6a9 9 0 1 1-12.77.04"]]},"mock":{"lucide":"bug","ops":[["p","M12 20v-9"],["p","M14 7a4 4 0 0 1 4 4v3a6 6 0 0 1-12 0v-3a4 4 0 0 1 4-4z"],["p","M14.12 3.88 16 2"],["p","M21 21a4 4 0 0 0-3.81-4"],["p","M21 5a4 4 0 0 1-3.55 3.97"],["p","M22 13h-4"],["p","M3 21a4 4 0 0 1 3.81-4"],["p","M3 5a4 4 0 0 0 3.55 3.97"],["p","M6 13H2"],["p","m8 2 1.88 1.88"],["p","M9 7.13V6a3 3 0 1 1 6 0v1.13"]]},"motion-foot":{"lucide":"footprints","ops":[["p","M4 16v-2.38C4 11.5 2.97 10.5 3 8c.03-2.72 1.49-6 4.5-6C9.37 2 10 3.8 10 5.5c0 3.11-2 5.66-2 8.68V16a2 2 0 1 1-4 0Z"],["p","M20 20v-2.38c0-2.12 1.03-3.12 1-5.62-.03-2.72-1.49-6-4.5-6C14.63 6 14 7.8 14 9.5c0 3.11 2 5.66 2 8.68V20a2 2 0 1 0 4 0Z"],["p","M16 17h4"],["p","M4 13h4"]]},"motion-vehicle":{"lucide":"car-front","ops":[["p","m21 8-2 2-1.5-3.7A2 2 0 0 0 15.646 5H8.4a2 2 0 0 0-1.903 1.257L5 10 3 8"],["p","M7 14h.01"],["p","M17 14h.01"],["r",3.0,10.0,18.0,8.0,2.0],["p","M5 18v2"],["p","M19 18v2"]]},"persistence":{"lucide":"database","ops":[["e",12.0,5.0,9.0,3.0],["p","M3 5V19A9 3 0 0 0 21 19V5"],["p","M3 12A9 3 0 0 0 21 12"]]},"power-save":{"lucide":"battery-low","ops":[["p","M22 14v-4"],["p","M6 14v-4"],["r",2.0,6.0,16.0,12.0,2.0]]},"provider":{"lucide":"satellite-dish","ops":[["p","M4 10a7.31 7.31 0 0 0 10 10Z"],["p","m9 15 3-3"],["p","M17 13a6 6 0 0 0-6-6"],["p","M21 13A10 10 0 0 0 11 3"]]},"rejection":{"lucide":"ban","ops":[["c",12.0,12.0,10.0],["p","M4.929 4.929 19.07 19.071"]]},"route":{"lucide":"route","ops":[["c",6.0,19.0,3.0],["p","M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"],["c",18.0,5.0,3.0]]},"schedule":{"lucide":"calendar","ops":[["p","M8 2v3"],["p","M16 2v3"],["r",3.0,3.0,18.0,18.0,2.0],["p","M3 9h18"]]},"stationary":{"lucide":"circle-parking","ops":[["c",12.0,12.0,10.0],["p","M9 17V7h4a3 3 0 0 1 0 6H9"]]},"stationary-exit":{"lucide":"circle-arrow-out-up-right","ops":[["p","M22 12A10 10 0 1 1 12 2"],["p","M22 2 12 12"],["p","M16 2h6v6"]]},"stop-timeout":{"lucide":"anchor","ops":[["p","M12 6v16"],["p","m19 13 2-1a9 9 0 0 1-18 0l2 1"],["p","M9 11h6"],["c",12.0,4.0,2.0]]},"terminate":{"lucide":"power-off","ops":[["p","M18.36 6.64A9 9 0 0 1 20.77 15"],["p","M6.16 6.16a9 9 0 1 0 12.68 12.68"],["p","M12 2v4"],["p","m2 2 20 20"]]},"timer":{"lucide":"clock","ops":[["c",12.0,12.0,10.0],["p","M12 6v6l4 2"]]},"warning":{"lucide":"triangle-alert","ops":[["p","m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"],["p","M12 9v4"],["p","M12 17h.01"]]}}
