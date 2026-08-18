"""Stage 2 — embedded-struct mini-parsers.

`annotate(rec)` inspects `Record.raw` and fills `rec.structs` with any
structured payloads it recognizes. Contract (INTERFACES.md): keys present only
when parsed; every parser tolerates truncation and NEVER raises (each parser
is individually wrapped). `raw`/`body` are never mutated.

Keys produced:
  location          dict: lat, lon, acc, speed, course, alt, provider, mock,
                    et (boot-relative elapsed, ms), age_ms, batch_index,
                    time (epoch ms, Android), time_text (iOS locale prose),
                    vacc/sacc/bacc (Android extras)
  locations         list[dict] — set when a record carries >1 location payload
                    or an iOS `N:` batch-indexed pin (A/B comparison rows are
                    routed to ab_compare instead)
  filter_result     dict: decision, reason, raw, effective, anomaly, … —
                    Android `LocationFilterResult{…}` or iOS filter-metrics
                    `key=value` lines
  detected_activity dict: type, confidence
  motion_activity   dict: st, walk, run, auto, cyc, conf, start, test
                    (iOS CMMotionActivity dump; field names as logged)
  intent            dict: act, dat, cmp   (Android Intent { … })
  config_dump       dict: {"data": dict | None, "truncated": bool} —
                    plist-style ObjC dumps on iOS, truncated JSON on Android
                    (complete top-level keys only; the whole block is NEVER
                    json.loads'd)
  http              dict: status, count, success, queued_before, synced,
                    pages, duration_ms, retry, busy, flush, url, error,
                    post_uuid  (merged across the record's HTTP lines)
  nserror           dict: code, domain, desc
  geofence          dict: action, identifier
  ab_compare        list[dict]: label ("A"/"B") + location fields

Performance: every parser is gated on a cheap substring check before any
regex runs — annotate() executes on ~75k records for a big capture.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .model import Record

# ── shared helpers ───────────────────────────────────────────────────────────

_DUR_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+)s)?(?:(\d+)ms)?$")


def _duration_ms(s: str) -> int | None:
    """'+10h25m6s54ms' / '1d23h40m16s428ms' → total milliseconds."""
    m = _DUR_RE.fullmatch(s.lstrip("+"))
    if not m or not any(m.groups()):
        return None
    d, h, mi, sec, ms = (int(g) if g else 0 for g in m.groups())
    return (((d * 24 + h) * 3600) + mi * 60 + sec) * 1000 + ms


def _num(s: str) -> Any:
    """Best-effort numeric conversion; returns the string unchanged on failure."""
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


# ── Android Location[…] ──────────────────────────────────────────────────────

_A_LOC_KV = re.compile(r"(\w+)=(-?\d+(?:\.\d+)?(?:[Ee]-?\d+)?)")
_A_LOC_ET = re.compile(r"et=(\+?\S+)")
_A_LOC_TIME = re.compile(r", time: (\d+)")
_A_KEYMAP = {"hAcc": "acc", "vel": "speed", "bear": "course",
             "vAcc": "vacc", "sAcc": "sacc", "bAcc": "bacc"}


def _parse_android_location_inner(inner: str) -> dict | None:
    parts = inner.split(None, 2)
    if len(parts) < 2:
        return None
    ll = parts[1].split(",")
    if len(ll) != 2:
        return None
    try:
        lat, lon = float(ll[0]), float(ll[1])
    except ValueError:
        return None
    loc: dict[str, Any] = {"provider": parts[0], "lat": lat, "lon": lon}
    rest = parts[2] if len(parts) > 2 else ""
    for k, v in _A_LOC_KV.findall(rest):
        loc[_A_KEYMAP.get(k, k)] = float(v) if ("." in v or "e" in v.lower()) else int(v)
    m = _A_LOC_ET.search(rest)
    if m:
        ms = _duration_ms(m.group(1))
        if ms is not None:
            loc["et"] = ms
    # trailing ' mock]' flag — token check keeps Bundle[{…}] innards out
    loc["mock"] = "mock" in rest.split()
    return loc


def _p_android_location(rec: Record, raw: str) -> None:
    locs: list[dict] = []
    idx = 0
    n = len(raw)
    while True:
        start = raw.find("Location[", idx)
        if start < 0:
            break
        # boundary: reject e.g. a preceding identifier char (LocationFilterResult
        # has no '[', but stay defensive)
        if start > 0 and (raw[start - 1].isalnum() or raw[start - 1] == "_"):
            idx = start + 9
            continue
        i = start + len("Location[")
        inner_start = i
        depth = 1
        while i < n and depth:
            c = raw[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
            i += 1
        inner = raw[inner_start: i - 1] if depth == 0 else raw[inner_start:]
        loc = _parse_android_location_inner(inner)
        if loc is not None:
            if depth == 0:
                m = _A_LOC_TIME.match(raw[i:])
                if m:
                    loc["time"] = int(m.group(1))
            locs.append(loc)
        idx = i
    _commit_locations(rec, locs)


# ── iOS 📍<+lat,+lon> pins ───────────────────────────────────────────────────

_IOS_PIN = re.compile(
    r"(?:(\d+):)?📍<([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)>"
    r"(?:\s*\+/-\s*([\d.]+)m)?"
    r"(?:\s*\(speed\s+(-?[\d.]+)\s+mps\s*/\s*course\s+(-?[\d.]+)\))?"
    r"(?:\s*@\s*([^|\n]+?))?"
    r"(?:\s*\|\s*age:\s*(\d+)\s*ms)?\s*$"
)


def _pin_to_loc(m: re.Match) -> dict:
    loc: dict[str, Any] = {"lat": float(m.group(2)), "lon": float(m.group(3))}
    if m.group(1):
        loc["batch_index"] = int(m.group(1))
    if m.group(4):
        loc["acc"] = float(m.group(4))
    if m.group(5) is not None:
        v = float(m.group(5))
        loc["speed"] = None if v == -1.0 else v      # -1.00 = unknown sentinel
    if m.group(6) is not None:
        v = float(m.group(6))
        loc["course"] = None if v == -1.0 else v     # -1.00 = unknown sentinel
    if m.group(7):
        loc["time_text"] = m.group(7).strip()
    if m.group(8):
        loc["age_ms"] = int(m.group(8))
    return loc


def _p_ios_pins(rec: Record, raw: str) -> None:
    locs: list[dict] = []
    for line in raw.splitlines():
        if "📍<" not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- A:", "- B:", "╟─ A:", "╟─ B:")):
            continue  # comparison rows belong to ab_compare
        m = _IOS_PIN.search(line)
        if m:
            locs.append(_pin_to_loc(m))
    _commit_locations(rec, locs)


def _commit_locations(rec: Record, locs: list[dict]) -> None:
    if not locs:
        return
    rec.structs["location"] = locs[0]
    if len(locs) > 1 or any("batch_index" in x for x in locs):
        rec.structs["locations"] = locs


# ── filter results ───────────────────────────────────────────────────────────

_A_FILTER_KEYMAP = {"acc(cur)": "acc_cur", "acc(prev)": "acc_prev"}
_UNIT_M = re.compile(r"^(-?\d+(?:\.\d+)?)m$")


def _p_android_filter(rec: Record, raw: str) -> None:
    start = raw.find("LocationFilterResult{")
    inner_start = start + len("LocationFilterResult{")
    end = raw.find("}", inner_start)
    inner = raw[inner_start:end] if end >= 0 else raw[inner_start:]
    out: dict[str, Any] = {}
    for pair in inner.split(", "):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = _A_FILTER_KEYMAP.get(k.strip(), k.strip())
        v = v.strip()
        if v.startswith("'") and v.endswith("'") and len(v) >= 2:
            out[k] = v[1:-1]
        elif v in ("true", "false"):
            out[k] = v == "true"
        else:
            out[k] = _num(v)
    if out:
        rec.structs["filter_result"] = out


def _p_ios_filter_metrics(rec: Record, raw: str) -> None:
    """`decision=Accepted reason=OK raw=264.6m effective=245.9m …` k=v lines."""
    if "filter_result" in rec.structs:
        return
    for line in raw.splitlines():
        pos = line.find("decision=")
        if pos < 0:
            continue
        out: dict[str, Any] = {}
        for k, v in re.findall(r"(\w+)=(\S+)", line[pos:]):
            um = _UNIT_M.match(v)
            out[k] = float(um.group(1)) if um else _num(v)
        if out:
            rec.structs["filter_result"] = out
        return


# ── DetectedActivity / CMMotionActivity ──────────────────────────────────────

_DETECTED = re.compile(r"DetectedActivity ?\[type=(\w+), confidence=(\d+)\]")


def _p_detected_activity(rec: Record, raw: str) -> None:
    m = _DETECTED.search(raw)
    if m:
        rec.structs["detected_activity"] = {"type": m.group(1),
                                            "confidence": int(m.group(2))}


_CMMA_FIELDS = re.compile(r"\b(st|walk|run|auto|cyc|conf):(\d+)")


def _p_motion_activity(rec: Record, raw: str) -> None:
    start = raw.find("<CMMotionActivity")
    end = raw.find(">", start)
    inner = raw[start:end] if end >= 0 else raw[start:]
    out: dict[str, Any] = {k: int(v) for k, v in _CMMA_FIELDS.findall(inner)}
    if not out:
        return
    sm = re.search(r"start:(.*?)(?= test:|$)", inner)
    if sm:
        out["start"] = sm.group(1).strip()
    tm = re.search(r"test:(\S*)", inner)
    if tm:
        out["test"] = tm.group(1)
    rec.structs["motion_activity"] = out


# ── Android Intent { act= dat= cmp= } ────────────────────────────────────────

_INTENT_FIELD = re.compile(r"\b(act|dat|cmp)=([^\s}]+)")


def _p_intent(rec: Record, raw: str) -> None:
    start = raw.find("Intent { ")
    brace = raw.find("{", start)
    i = brace + 1
    depth = 1
    n = len(raw)
    while i < n and depth:          # nested braces / Bundle[{…}] tolerated
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    inner = raw[brace + 1: i - 1] if depth == 0 else raw[brace + 1:]
    out = {k: v for k, v in _INTENT_FIELD.findall(inner)}
    if out:
        rec.structs["intent"] = out


# ── config dumps ─────────────────────────────────────────────────────────────

_JSON_TOPKEY = re.compile(r'^  "((?:[^"\\]|\\.)*)": (.*)$')
_JSON_TOPCLOSE = re.compile(r"^  [\]}],?$")


def _parse_android_config_json(lines: list[str]) -> tuple[dict, bool]:
    """Extract complete top-level keys from a (usually truncated) JSON dump.
    NEVER json.loads the whole block — only per-key fragments."""
    data: dict[str, Any] = {}
    truncated = True            # flips False only on a col-0 closing brace
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line in ("}", "},"):
            truncated = False
            break
        m = _JSON_TOPKEY.match(line)
        if not m:
            break               # unexpected shape ⇒ truncation/garbage
        key, rest = m.group(1), m.group(2)
        frag = rest.rstrip(",")
        try:
            data[key] = json.loads(frag)
            i += 1
            continue
        except ValueError:
            pass
        if rest in ("{", "["):  # nested block — accumulate to its indent-2 close
            j = i + 1
            while j < n and not _JSON_TOPCLOSE.match(lines[j]):
                j += 1
            if j >= n:
                break           # nested block ran off the end
            frag = "\n".join([rest, *lines[i + 1: j], lines[j].rstrip(",")])
            try:
                data[key] = json.loads(frag)
            except ValueError:
                break
            i = j + 1
        else:
            break               # incomplete scalar (e.g. `"heartbeatEnabled":`)
    return data, truncated


# ObjC-description (plist-style) parser: key = value; arrays ( ); NSSet {( )}

def _plist_scalar(s: str) -> Any:
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        v = s[1:-1].replace('\\"', '"')
        if v == "<null>":
            return None
        return _num(v)          # the describer quotes NSNumbers like "-1"
    if s == "<null>":
        return None
    return _num(s)


def _plist_dict(lines: list[str], i: int) -> tuple[dict, int, bool]:
    d: dict[str, Any] = {}
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        i += 1
        if line.startswith("}"):        # } / }; / }, …
            return d, i, False
        if not line or "=" not in line:
            continue                    # tolerate stray rows
        key_s, val_s = line.split("=", 1)
        key = key_s.strip()
        if len(key) >= 2 and key.startswith('"') and key.endswith('"'):
            key = key[1:-1].replace('\\"', '"')
        val = val_s.strip()
        if val.endswith(";"):
            val = val[:-1].rstrip()
        if val == "{":
            sub, i, trunc = _plist_dict(lines, i)
            d[key] = sub
            if trunc:
                return d, i, True
        elif val == "(":
            arr, i, trunc = _plist_array(lines, i, close=")")
            d[key] = arr
            if trunc:
                return d, i, True
        elif val == "{(":
            arr, i, trunc = _plist_array(lines, i, close=")}")
            d[key] = arr                # NSSet → list
            if trunc:
                return d, i, True
        else:
            d[key] = _plist_scalar(val)
    return d, i, True                   # ran out of lines before the close


def _plist_array(lines: list[str], i: int, close: str) -> tuple[list, int, bool]:
    arr: list[Any] = []
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        i += 1
        if line.startswith(close):
            return arr, i, False
        if not line:
            continue
        if line == "{":
            sub, i, trunc = _plist_dict(lines, i)
            arr.append(sub)
            if trunc:
                return arr, i, True
        else:
            arr.append(_plist_scalar(line.rstrip(",").rstrip(";").strip()))
    return arr, i, True


def _dump_block(rec: Record) -> list[str] | None:
    """Body lines following a col-0 '{' opener (or the whole body for iOS
    bare-'{' records where the opener sits in the header slot)."""
    if rec.header_msg.strip() == "{":
        return rec.body
    for i, line in enumerate(rec.body):
        if line == "{":
            return rec.body[i + 1:]
    return None


def _p_config_dump(rec: Record, raw: str) -> None:
    if "HTTP ERROR:" in raw:
        return                          # that dict is the NSError payload
    block = _dump_block(rec)
    if block is None:
        return
    if rec.platform == "android":
        if not any(_JSON_TOPKEY.match(ln) for ln in block[:2]):
            return                      # not a JSON config dump
        data, truncated = _parse_android_config_json(block)
    else:
        data, _, truncated = _plist_dict(block, 0)
    if data:
        rec.structs["config_dump"] = {"data": data, "truncated": truncated}


# ── HTTP ─────────────────────────────────────────────────────────────────────

_HTTP_COUNT = re.compile(r"HTTP Service \(count: (\d+)\)")
_HTTP_RESPONSE = re.compile(r"Response: (\d+)\s*$", re.MULTILINE)
_HTTP_POST_UUID = re.compile(r"HTTP POST: ([0-9a-fA-F-]{8,})")
_HTTP_POST_STATUS = re.compile(
    r"flush=([0-9A-Fa-f-]+) post status=(\d+)(?: retry=(\d+))?(?: busy=(\d+))?")
_HTTP_FINISH = re.compile(
    r"success=(\d+) queued_before=(\d+) synced=(\d+) pages=(\d+) duration_ms=(\d+)")
_HTTP_ERROR = re.compile(r"HTTP ERROR: (\d+)\*?\s*(.*)$", re.MULTILINE)
_NSERROR_INLINE = re.compile(r'Error Domain=(\S+) Code=(-?\d+)(?: \\?"(.*?)\\?")?')
_NSERROR_USERINFO_DESC = re.compile(r"UserInfo=\{NSLocalizedDescription=([^}]*)\}")


def _http(rec: Record) -> dict:
    return rec.structs.setdefault("http", {})


def _p_http_count(rec: Record, raw: str) -> None:
    m = _HTTP_COUNT.search(raw)
    if m:
        _http(rec)["count"] = int(m.group(1))


def _p_http_response(rec: Record, raw: str) -> None:
    m = _HTTP_RESPONSE.search(raw)
    if m:
        _http(rec)["status"] = int(m.group(1))


def _p_http_post_uuid(rec: Record, raw: str) -> None:
    m = _HTTP_POST_UUID.search(raw)
    if m:
        _http(rec)["post_uuid"] = m.group(1)


def _p_http_post_status(rec: Record, raw: str) -> None:
    m = _HTTP_POST_STATUS.search(raw)
    if not m:
        return
    h = _http(rec)
    h["flush"] = m.group(1)
    h["status"] = int(m.group(2))
    if m.group(3) is not None:
        h["retry"] = int(m.group(3))
    if m.group(4) is not None:
        h["busy"] = int(m.group(4))


def _p_http_finish(rec: Record, raw: str) -> None:
    m = _HTTP_FINISH.search(raw)
    if not m:
        return
    h = _http(rec)
    h["success"] = bool(int(m.group(1)))
    h["queued_before"] = int(m.group(2))
    h["synced"] = int(m.group(3))
    h["pages"] = int(m.group(4))
    h["duration_ms"] = int(m.group(5))


def _p_http_error(rec: Record, raw: str) -> None:
    """The 3-part HTTP-error record: ⚠ status-0 header + lone '*' + NSError dict."""
    m = _HTTP_ERROR.search(raw)
    if m:
        h = _http(rec)
        h["status"] = int(m.group(1))
        if m.group(2):
            h["error"] = m.group(2).strip()
    block = _dump_block(rec)
    if block is None:
        return
    d, _, _ = _plist_dict(block, 0)
    if not d:
        return
    err: dict[str, Any] = {}
    desc = d.get("NSLocalizedDescription")
    if desc is not None:
        err["desc"] = desc
    underlying = d.get("NSUnderlyingError")
    if isinstance(underlying, str):
        um = _NSERROR_INLINE.search(underlying)
        if um:
            err["domain"] = um.group(1)
            err["code"] = int(um.group(2))
    url = d.get("NSErrorFailingURLKey") or d.get("NSErrorFailingURLStringKey")
    if url:
        _http(rec)["url"] = url
    if err:
        rec.structs["nserror"] = err


def _p_nserror_inline(rec: Record, raw: str) -> None:
    if "nserror" in rec.structs:
        return                          # the dict form already filled it
    m = _NSERROR_INLINE.search(raw)
    if not m:
        return
    desc = m.group(3)
    if desc in ("(null)", ""):
        desc = None
    um = _NSERROR_USERINFO_DESC.search(raw)
    if um:
        desc = um.group(1).strip()
    rec.structs["nserror"] = {"domain": m.group(1), "code": int(m.group(2)),
                              "desc": desc}


# ── geofence action/identifier ───────────────────────────────────────────────

_GEOFENCE_ACTION = re.compile(r"📢\s*(\w+) Geofence: (.+?)\s*$", re.MULTILINE)
_GEOFENCE_EVENT = re.compile(r"Geofencing Event: (\w+)")
_BANNER_ROW = re.compile(r"^╟─\s*(.+?)\s*$")


def _p_geofence(rec: Record, raw: str) -> None:
    m = _GEOFENCE_ACTION.search(raw)
    if m:
        rec.structs["geofence"] = {"action": m.group(1), "identifier": m.group(2)}
        return
    m = _GEOFENCE_EVENT.search(raw)
    if not m:
        return
    gf: dict[str, Any] = {"action": m.group(1)}
    # identifier = first plain ╟─ row after the banner title (no k=v, no 📍)
    for line in raw[m.end():].splitlines():
        rm = _BANNER_ROW.match(line)
        if not rm:
            continue
        candidate = rm.group(1)
        if "=" in candidate or candidate.startswith(("📍", "A:", "B:")):
            continue
        gf["identifier"] = candidate
        break
    rec.structs["geofence"] = gf


# ── A/B comparison rows ──────────────────────────────────────────────────────

_AB_ROW = re.compile(r"^(?:- |╟─\s*)([AB]): (.*)$")
_AB_ANDROID = re.compile(
    r"^(-?[\d.]+),(-?[\d.]+) acc=([\d.]+) age=(\d+)ms provider=(\S+)")


def _p_ab_compare(rec: Record, raw: str) -> None:
    rows: list[dict] = []
    for line in raw.splitlines():
        m = _AB_ROW.match(line)
        if not m:
            continue
        label, payload = m.group(1), m.group(2)
        entry: dict[str, Any] = {"label": label}
        pm = _IOS_PIN.search(payload)
        if pm:
            entry.update(_pin_to_loc(pm))
        else:
            am = _AB_ANDROID.match(payload)
            if am:
                entry.update(lat=float(am.group(1)), lon=float(am.group(2)),
                             acc=float(am.group(3)), age_ms=int(am.group(4)),
                             provider=am.group(5))
            else:
                entry["raw"] = payload
        rows.append(entry)
    if rows:
        rec.structs["ab_compare"] = rows


# ── dispatcher ───────────────────────────────────────────────────────────────

def _try(fn, rec: Record, raw: str) -> None:
    try:
        fn(rec, raw)
    except Exception:
        pass                            # mini-parsers NEVER raise


def annotate(rec: Record) -> None:
    """Fill rec.structs from rec.raw. Never mutates raw/body; never raises."""
    raw = rec.raw
    # locations
    if "Location[" in raw:
        _try(_p_android_location, rec, raw)
    if "📍<" in raw:
        _try(_p_ios_pins, rec, raw)
    # filter results
    if "LocationFilterResult{" in raw:
        _try(_p_android_filter, rec, raw)
    elif "decision=" in raw:
        _try(_p_ios_filter_metrics, rec, raw)
    # activity
    if "DetectedActivity" in raw:
        _try(_p_detected_activity, rec, raw)
    if "<CMMotionActivity" in raw:
        _try(_p_motion_activity, rec, raw)
    # intent
    if "Intent { " in raw:
        _try(_p_intent, rec, raw)
    # http (order: the 3-part error first so nserror wins the dict)
    if "HTTP ERROR:" in raw:
        _try(_p_http_error, rec, raw)
    if "HTTP Service (count:" in raw:
        _try(_p_http_count, rec, raw)
    if "Response: " in raw:
        _try(_p_http_response, rec, raw)
    if "HTTP POST: " in raw:
        _try(_p_http_post_uuid, rec, raw)
    if " post status=" in raw:
        _try(_p_http_post_status, rec, raw)
    if "queued_before=" in raw:
        _try(_p_http_finish, rec, raw)
    # nserror (inline form)
    if "Error Domain=" in raw:
        _try(_p_nserror_inline, rec, raw)
    # geofence
    if ("📢" in raw and "Geofence: " in raw) or "Geofencing Event: " in raw:
        _try(_p_geofence, rec, raw)
    # A/B comparison rows
    if "- A: " in raw or "- B: " in raw or "╟─ A: " in raw:
        _try(_p_ab_compare, rec, raw)
    # config / plist dumps (needs body-line access, cheap gate on col-0 '{')
    if "\n{" in raw or rec.header_msg.strip() == "{":
        _try(_p_config_dump, rec, raw)
