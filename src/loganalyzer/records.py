"""Stage 1 — record assembly. Turns raw text into Records, preserving bytes.

Grammar facts baked in here were established empirically against the fixture
corpus (see fixtures/) and the design doc:

Android text export (`TSLogReader.hydrate`):
  header  = "MM-DD HH:MM:SS.mmm LEVEL [Class method] msg"   (no year, local tz)
  - method may nest one bracket level: [AbstractService stopSelfDelayed[λ]]
  - header frequently ends with a trailing space and an empty message; the
    payload then arrives on following lines, indented, usually starting with an
    icon (the deliberate leading-\n two-line convention)
  - everything that doesn't match the header folds into the previous record:
    indented bodies, box rows (╔║╠╟╚), raw truncated config JSON, and untagged
    exception stack-trace lines
  - midnight rollover must be inferred statefully (Dec→Jan bumps the year)

iOS text export (TSNativeLogger getLog):
  record  = "YYYY-MM-DD HH:MM:SS.mmm <slot> ..." + continuations, terminated by
            exactly one blank line
  - the slot after the timestamp is polymorphic: one emoji, two (📌 🔒), a bare
    "{" (ObjC dict dump), a 📍<+lat,+lon> payload, or nothing at all
  - selector tags are macro-injected: -[Class sel:], +[Class sel:], and
    block-mangled __NN-[Class sel:]_block_invoke(_N)
  - banner ║ rows repeat the selector; blank-line termination is authoritative,
    banner ╔/╚ pairing is NOT
"""
from __future__ import annotations

import re
from datetime import datetime

from .model import ANDROID, IOS, EMOJI_TOKEN, Record, normalize_emoji

# ── Android ──────────────────────────────────────────────────────────────────

ANDROID_HEADER = re.compile(
    r"^(\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}\.\d{3}) (DEBUG|INFO|WARN|ERROR) "
    r"\[(\S+) ((?:[^\[\]]|\[[^\]]*\])*)\] ?(.*)$"
)

_METHOD_SUFFIXES = re.compile(r"(\[λ\]|\$\d+)$")

# Double-tagged timing lines: "[LoggerFacade$Entry log] ⏱️ [TSConfig hydrateTime] 123ms"
_PSEUDO_TAG = re.compile(r"^[⏱⏰]️?\s*\[(\S+) ([^\]]+)\]\s*(.*)$")


def _android_normalize_method(method: str) -> str:
    return _METHOD_SUFFIXES.sub("", method)


def assemble_android(text: str, base_year: int) -> list[Record]:
    records: list[Record] = []
    cur: Record | None = None
    year = base_year
    prev_month = None

    for line_no, line in enumerate(text.splitlines(), start=1):
        m = ANDROID_HEADER.match(line)
        if not m:
            if cur is not None:
                cur.body.append(line)
            continue
        if cur is not None:
            cur.raw = "\n".join([cur.raw, *cur.body]) if cur.body else cur.raw
            records.append(cur)

        md, hms, level, klass, method, msg = m.groups()
        month = int(md[:2])
        if prev_month == 12 and month == 1:
            year += 1
        prev_month = month
        try:
            ts = datetime.strptime(f"{year}-{md} {hms}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            ts = None

        tag_class, tag_method = klass, method
        header_msg = msg
        # Re-tag pseudo-tagged timing lines by their inner (semantic) pair.
        pm = _PSEUDO_TAG.match(msg)
        if pm:
            tag_class, tag_method, header_msg = pm.group(1), pm.group(2), pm.group(3)

        cur = Record(
            platform=ANDROID, seq=len(records), line_no=line_no,
            ts=ts, ts_raw=f"{md} {hms}", level=level,
            icon=None,      # filled below from body line 1
            tag_class=tag_class, tag_method_raw=tag_method,
            tag_method=_android_normalize_method(tag_method),
            header_msg=header_msg, raw=line,
        )
    if cur is not None:
        cur.raw = "\n".join([cur.raw, *cur.body]) if cur.body else cur.raw
        records.append(cur)

    for rec in records:
        _android_fill_icon(rec)
    return records


def _android_fill_icon(rec: Record) -> None:
    """Android's semantic icon is the first emoji token at the start of body
    line 1 (helpers emit '\\n  <icon> msg'); header-inline icons also count."""
    for candidate in (rec.first_body_line().lstrip(), rec.header_msg):
        if not candidate:
            continue
        m = EMOJI_TOKEN.match(candidate)
        if m:
            rec.icon = normalize_emoji(m.group(0))
            return


# ── iOS ──────────────────────────────────────────────────────────────────────

IOS_HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}\.\d{3})(?: (.*))?$")

IOS_SELECTOR = re.compile(
    r"^(?:__\d+)?([+-])\[(\w+) ([^\]]+)\](_block_invoke(?:_?\d+)?)?"
)
_IOS_C_FUNC = re.compile(r"^(\w+) ")


def _ios_normalize_selector(sel: str) -> str:
    return sel  # suffixes are captured outside the brackets and simply dropped


def assemble_ios(text: str) -> list[Record]:
    records: list[Record] = []
    cur: Record | None = None
    lines = text.splitlines()

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            # trim the single trailing blank separator, keep interior blanks
            while cur.body and cur.body[-1] == "":
                cur.body.pop()
            cur.raw = "\n".join([cur.raw, *cur.body]) if cur.body else cur.raw
            records.append(cur)
            cur = None

    for line_no, line in enumerate(lines, start=1):
        m = IOS_HEADER.match(line)
        if not m:
            if cur is not None:
                cur.body.append(line)
            continue
        flush()
        date_s, hms, rest = m.group(1), m.group(2), m.group(3) or ""
        try:
            ts = datetime.strptime(f"{date_s} {hms}", "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            ts = None

        icon, tag_class, tag_method_raw, msg = _ios_parse_slot(rest)
        cur = Record(
            platform=IOS, seq=len(records), line_no=line_no,
            ts=ts, ts_raw=f"{date_s} {hms}", level=None, icon=icon,
            tag_class=tag_class, tag_method_raw=tag_method_raw,
            tag_method=tag_method_raw, header_msg=msg, raw=line,
        )
    flush()
    return records


def _ios_parse_slot(rest: str) -> tuple[str | None, str | None, str | None, str]:
    """Parse the polymorphic slot after the timestamp: up to two emoji tokens,
    then an optional selector, then the message. Bare '{', 📍 payloads, and
    empty slots (banner lead-ins) all leave tag fields None."""
    icons: list[str] = []
    s = rest
    for _ in range(2):
        s2 = s.lstrip()
        m = EMOJI_TOKEN.match(s2)
        if m and not s2.startswith("📍<"):
            icons.append(normalize_emoji(m.group(0)))
            s = s2[m.end():]
        else:
            s = s2
            break
    # Must re-strip: a two-emoji slot ("📌 🔒 -[TSLocationAuthorization …]")
    # exhausts the loop without the else-branch that would have stripped, so the
    # selector regex would face a leading space and fail — leaving tag_class None
    # on every iOS authorization record.
    s = s.lstrip()
    sel = IOS_SELECTOR.match(s)
    if sel:
        sign, klass, method, _block = sel.groups()
        msg = s[sel.end():].lstrip()
        return ("".join(icons) or None), klass, method, msg
    return ("".join(icons) or None), None, None, s


def assemble(platform: str, text: str, base_year: int) -> list[Record]:
    if platform == ANDROID:
        return assemble_android(text, base_year)
    if platform == IOS:
        return assemble_ios(text)
    raise ValueError(f"unknown platform: {platform}")
