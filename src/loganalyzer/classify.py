"""Stage 3 — classification: join log records back to their emitting source sites.

Two-tier matcher (kills the O(patterns x records) naive cost):
  tier 1: pyahocorasick automaton over per-pattern literal anchors — emoji
          byte-sequences (💾, 📢, 🛃 …) anchor literal-poor sites — producing
          candidate entries;
  tier 2: full compiled-regex verify (MULTILINE|DOTALL) against Record.raw only
          for those candidates.

Precedence: literal-length-descending; per-site catch-alls (builder/no-literal
sites) fire only when no literal pattern verified.

Join keys:
  iOS     — (tag_class, tag_method) bucket first (records.py already strips
            _block_invoke); class-alone fallback because harvested selectors
            can differ in colons/argument counts; automaton candidates cover
            tag-less records (banners, 📍 payload lines).
  Android — composite scoring: literal candidates generate, tag agreement is a
            bonus scorer (weight 0 when the tag is a helper frame such as
            LoggerFacade$Entry — on many captures EVERY line carries it),
            level+icon agreement tie-breaks.

Version filtering via valid_from/valid_to (inclusive, semver-ish); "build N"
versions resolve through vocabulary/ios-builds.yaml, unresolvable builds mean
no filter.

Statuses: "matched" | "drift" (fuzzy longest-fragment attempt — record shares
>= 60% of an entry's literal tokens) | "unknown" | "passthrough" (bridge
pass-through sites are app-authored: classify by tag, never pattern-match the
body).

Semantics layer (vocabulary/semantics.yaml, hand-curated): category rules are
first-match-wins over "<tag_class>.<tag_method>" (a matched entry's own
class.method substitutes when the record tag is missing or a helper frame),
then body-emoji hints; icon_semantics (per-platform, version-ranged) fill
semantic/family when the vocabulary entry carries none.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import ahocorasick
import yaml

from .model import (
    ANDROID,
    IOS,
    EMOJI_TOKEN,
    Classification,
    Record,
    normalize_emoji,
)

log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────

_MIN_ANCHOR_LEN = 4          # shorter literals only qualify as anchors when emoji
_DRIFT_SHARE = 0.60          # token-share threshold for "probable drift of <site>"
_MIN_DRIFT_TOKENS = 3        # entries with fewer literal tokens never claim drift
_MAX_CATCHALL_TIE = 3        # more tied catch-alls than this = too unspecific

# Helper frames: the log tag names the logging plumbing, not the emitting site.
_HELPER_TAGS = {"LoggerFacade$Entry", "TSLog", "LoggerFacade", "Logger"}

# EMOJI_TOKEN (model.py, frozen) misses the U+23xx clock block; these still
# count as emoji for anchor qualification.
_EXTRA_EMOJI = "⏰⏱⏳⏸⏹⏺▶"

_WORD_TOKEN = re.compile(r"[A-Za-z0-9_]{3,}")
_BUILD_VERSION = re.compile(r"^\s*build\s+(\d+)\s*$")
_VER_NUM = re.compile(r"\d+")


def _is_helper_tag(tag_class: str | None) -> bool:
    if not tag_class:
        return False
    return (
        tag_class in _HELPER_TAGS
        or tag_class.startswith("LoggerFacade")
        or tag_class.endswith("$Entry")
    )


def _contains_emoji(s: str) -> bool:
    return bool(EMOJI_TOKEN.search(s)) or any(ch in _EXTRA_EMOJI for ch in s)


# ── Version handling (semver-ish) ────────────────────────────────────────────

def _ver_tuple(v: Any) -> tuple[int, ...] | None:
    """'4.2.1' -> (4, 2, 1). Tolerates '4.2.1 (4063)', 'v4.2'. None/garbage -> None."""
    if v is None:
        return None
    s = str(v).split("(")[0]
    nums = _VER_NUM.findall(s)
    if not nums:
        return None
    return tuple(int(n) for n in nums[:4])


def _in_range(vt: tuple[int, ...] | None,
              from_t: tuple[int, ...] | None,
              to_t: tuple[int, ...] | None) -> bool:
    """Inclusive on both ends; unknown version = no filter."""
    if vt is None:
        return True
    if from_t is not None and _cmp(vt, from_t) < 0:
        return False
    if to_t is not None and _cmp(vt, to_t) > 0:
        return False
    return True


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    """Compare padding the shorter with zeros so (4,5) == (4,5,0)."""
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


# ── Literal-fragment extraction ──────────────────────────────────────────────

_CLASS_ESCAPES = frozenset("dDsSwWbBAZz")
_QUANTIFIER_CHARS = frozenset("*+?{")


def _literal_fragments(pattern: str) -> list[tuple[str, bool]]:
    """Extract literal fragments from a regex source string.

    Returns (fragment, safe) pairs. A fragment is *safe* when it is guaranteed
    to appear in every string the pattern matches — i.e. it sits outside any
    alternation branch, optional/quantified group, or lookaround. Only safe
    fragments may serve as tier-1 anchors; unsafe ones still feed drift tokens.
    """
    frags: list[tuple[str, tuple[int, ...]]] = []
    alt_scopes: set[int] = set()
    scope_stack: list[int] = [0]
    next_scope = 1
    buf: list[str] = []

    def flush() -> None:
        if buf:
            frags.append(("".join(buf), tuple(scope_stack)))
            buf.clear()

    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            nxt = pattern[i + 1]
            if nxt in _CLASS_ESCAPES or nxt.isdigit():
                flush()                       # class/backref escape: breaks the run
            else:
                lit = {"n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v"}.get(nxt, nxt)
                if i + 2 < n and pattern[i + 2] in _QUANTIFIER_CHARS:
                    flush()                   # quantified escaped literal: not fixed
                else:
                    buf.append(lit)
            i += 2
            continue
        if c == "(":
            flush()
            scope = next_scope
            next_scope += 1
            scope_stack.append(scope)
            i += 1
            if i < n and pattern[i] == "?":
                if i + 1 < n and pattern[i + 1] == ":":
                    i += 2                    # (?:…) — ordinary group
                elif pattern.startswith("?P<", i):
                    end = pattern.find(">", i)
                    i = end + 1 if end != -1 else i + 1
                else:
                    alt_scopes.add(scope)     # lookaround/flags/conditional: unsafe
                    i += 1
            continue
        if c == ")":
            flush()
            closed = scope_stack.pop() if len(scope_stack) > 1 else 0
            i += 1
            if i < n and pattern[i] in _QUANTIFIER_CHARS:
                alt_scopes.add(closed)        # (…)? / (…)* / (…){m,n}: optional
            continue
        if c == "|":
            flush()
            alt_scopes.add(scope_stack[-1])
            i += 1
            continue
        if c == "[":
            flush()
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                if pattern[i] == "\\":
                    i += 1
                i += 1
            i += 1
            continue
        if c in _QUANTIFIER_CHARS:
            if buf:
                buf.pop()                     # quantifier binds the previous char
            flush()
            if c == "{":
                end = pattern.find("}", i)
                i = end + 1 if end != -1 else i + 1
            else:
                i += 1
            continue
        if c in ".^$":
            flush()
            i += 1
            continue
        buf.append(c)
        i += 1
    flush()
    return [(f, not any(s in alt_scopes for s in scopes)) for f, scopes in frags]


# ── Vocabulary ───────────────────────────────────────────────────────────────

_METHOD_NOISE = re.compile(r"(_block_invoke(_?\d+)?|\[λ\]|\$\d+)$")


def _normalize_method(method: str) -> str:
    return _METHOD_NOISE.sub("", method or "")


@dataclass
class VocabEntry:
    id: str
    platform: str
    klass: str
    method: str
    source: list[str] = field(default_factory=list)
    level: str = ""
    icon: str = ""
    patterns: list[str] = field(default_factory=list)
    anchor: str = ""
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    flags: list[str] = field(default_factory=list)
    semantic: str = ""
    family: str = ""

    @property
    def site(self) -> str:
        """The display handle for this entry: its id, else its first source."""
        return self.id or (self.source[0] if self.source else f"{self.klass}.{self.method}")


def _entry_from_yaml(raw: dict, platform: str, index: int) -> VocabEntry:
    klass = str(raw.get("class") or "")
    method = _normalize_method(str(raw.get("method") or ""))
    source = raw.get("source") or []
    if isinstance(source, str):
        source = [source]
    patterns = raw.get("patterns") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    flags = raw.get("flags") or []
    if isinstance(flags, str):
        flags = [flags]
    return VocabEntry(
        id=str(raw.get("id") or f"{klass}.{method}#{index}"),
        platform=str(raw.get("platform") or platform),
        klass=klass,
        method=method,
        source=[str(s) for s in source],
        level=str(raw.get("level") or "").upper(),
        icon=normalize_emoji(str(raw.get("icon") or "")).strip(),
        patterns=[str(p) for p in patterns],
        anchor=str(raw.get("anchor") or ""),
        valid_from=None if raw.get("valid_from") is None else str(raw.get("valid_from")),
        valid_to=None if raw.get("valid_to") is None else str(raw.get("valid_to")),
        flags=[str(f) for f in flags],
        semantic=str(raw.get("semantic") or ""),
        family=str(raw.get("family") or ""),
    )


_DEFAULT_SEMANTICS: dict[str, Any] = {
    "icon_semantics": {},
    "body_emoji_hints": {},
    "categories": [],
}


class Vocabulary:
    """Loaded vocabulary: harvested entries + build map + hand-curated semantics."""

    def __init__(self,
                 entries: list[VocabEntry] | None = None,
                 semantics: dict[str, Any] | None = None,
                 build_map: dict[str, str] | None = None):
        self.entries: list[VocabEntry] = entries or []
        self.semantics: dict[str, Any] = semantics or dict(_DEFAULT_SEMANTICS)
        self._build_map: dict[str, str] = {str(k): str(v) for k, v in (build_map or {}).items()}

    @property
    def build_map(self) -> dict[str, str]:
        return self._build_map

    @classmethod
    def load(cls, vocab_dir: Path) -> "Vocabulary":
        """Read android/ios/semantics/builds yamls from vocab_dir.

        Absent files are skipped gracefully with a warning — the classifier
        degrades (records go unknown / lose semantics) but never fails.
        """
        vocab_dir = Path(vocab_dir)
        entries: list[VocabEntry] = []
        for filename, platform in (("android.yaml", ANDROID), ("ios.yaml", IOS)):
            path = vocab_dir / filename
            data = _load_yaml(path, f"{platform} vocabulary")
            if data is None:
                continue
            if not isinstance(data, dict):
                log.warning("%s: expected a mapping — ignoring", path)
                continue
            raw_entries = data.get("entries") or []
            if not isinstance(raw_entries, list):
                log.warning("%s: 'entries' is not a list — ignoring", path)
                continue
            meta_platform = str((data.get("meta") or {}).get("platform") or platform)
            for i, raw in enumerate(raw_entries):
                if not isinstance(raw, dict):
                    log.warning("%s: entry %d is not a mapping — skipped", path, i)
                    continue
                entries.append(_entry_from_yaml(raw, meta_platform, i))

        builds = _load_yaml(vocab_dir / "ios-builds.yaml", "iOS build map") or {}
        if not isinstance(builds, dict):
            log.warning("%s: expected a mapping — ignoring", vocab_dir / "ios-builds.yaml")
            builds = {}

        semantics = _load_yaml(vocab_dir / "semantics.yaml", "semantics layer")
        if semantics is None:
            semantics = dict(_DEFAULT_SEMANTICS)

        vocab = cls(entries=entries, semantics=semantics, build_map=builds)
        vocab.apply_sources(sources_dir())
        return vocab

    def apply_sources(self, sources_dir: Path | None) -> int:
        """Overlay `<platform>.sources.yaml` — entry id -> ["path/file.java:1521"].

        The mapping from a log line back to the SDK source line that emitted it
        is only meaningful with that source checked out, so it is NOT shipped
        with the public vocabulary; it lives beside the private SDK sources and
        is merged in when present. Without it, findings simply carry no source
        link. -> number of entries enriched.
        """
        if not sources_dir:
            return 0
        by_id = {e.id: e for e in self.entries if e.id}
        hits = 0
        for filename, platform in (("android.sources.yaml", ANDROID),
                                   ("ios.sources.yaml", IOS)):
            path = Path(sources_dir) / filename
            if not path.exists():
                continue
            data = _load_yaml(path, f"{platform} source sidecar") or {}
            # sidecar = {meta: harvest provenance, sources: {id: [file:line]}}
            for key, refs in (data.get("sources") or {}).items():
                entry = by_id.get(str(key))
                if entry is None:
                    continue
                entry.source = [str(r) for r in (refs if isinstance(refs, list) else [refs])]
                hits += 1
        return hits


def sources_dir() -> Path | None:
    """Where to look for the private source sidecars, or None.

    Set LOGANALYZER_SOURCES to a directory holding `<platform>.sources.yaml`,
    mapping vocabulary entry ids to the SDK source line that emits them. That
    index is only meaningful with the SDK checked out, so it is not shipped:
    public installs never set this and findings simply carry no source link.
    """
    raw = os.environ.get("LOGANALYZER_SOURCES")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def _load_yaml(path: Path, what: str) -> Any:
    if not path.exists():
        log.warning("vocabulary file missing: %s — %s unavailable", path, what)
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        log.warning("vocabulary file unreadable: %s (%s) — %s unavailable", path, exc, what)
        return None


# ── Matcher internals ────────────────────────────────────────────────────────

@dataclass
class _Prepared:
    entry: VocabEntry
    regexes: list[re.Pattern]
    anchors: set[str]                 # normalized automaton keys
    anchor_len: int                   # longest safe literal (precedence rank)
    tokens: frozenset[str]            # literal word tokens (drift fuzzy match)
    from_t: tuple[int, ...] | None
    to_t: tuple[int, ...] | None
    catchall: bool
    passthrough: bool


@dataclass
class _CategoryRule:
    class_re: re.Pattern | None
    body_hint: str | None
    semantic_re: re.Pattern | None
    category: str


@dataclass
class _IconRule:
    icon: str
    semantic: str
    family: str
    from_t: tuple[int, ...] | None
    to_t: tuple[int, ...] | None


class Matcher:
    def __init__(self, vocab: Vocabulary):
        self.vocab = vocab
        self._sites: list[_Prepared] = [self._prepare(e) for e in vocab.entries]
        self._version_cache: dict[str | None, tuple[int, ...] | None] = {}

        # tier-1 automatons, one per platform, over normalized anchors
        keys: dict[str, dict[str, set[int]]] = {ANDROID: {}, IOS: {}}
        # join buckets
        self._bucket: dict[tuple[str, str, str], list[int]] = {}      # (platform, class, method)
        self._class_bucket: dict[tuple[str, str], list[int]] = {}     # (platform, class)
        self._pass_bucket: dict[tuple[str, str, str], list[int]] = {}
        self._pass_class_bucket: dict[tuple[str, str], list[int]] = {}

        for idx, site in enumerate(self._sites):
            e = site.entry
            if site.passthrough:
                self._pass_bucket.setdefault((e.platform, e.klass, e.method), []).append(idx)
                self._pass_class_bucket.setdefault((e.platform, e.klass), []).append(idx)
                continue
            self._bucket.setdefault((e.platform, e.klass, e.method), []).append(idx)
            self._class_bucket.setdefault((e.platform, e.klass), []).append(idx)
            if not site.catchall:
                platform_keys = keys.setdefault(e.platform, {})
                for anchor in site.anchors:
                    platform_keys.setdefault(anchor, set()).add(idx)

        self._android_catchalls: list[int] = [
            i for i, s in enumerate(self._sites)
            if s.catchall and not s.passthrough and s.entry.platform == ANDROID
        ]

        self._automatons: dict[str, ahocorasick.Automaton | None] = {}
        for platform, kmap in keys.items():
            if not kmap:
                self._automatons[platform] = None
                continue
            automaton = ahocorasick.Automaton()
            for key, idxs in kmap.items():
                automaton.add_word(key, tuple(idxs))
            automaton.make_automaton()
            self._automatons[platform] = automaton

        self._compile_semantics()

    # ── vocabulary preparation ───────────────────────────────────────────────

    def _prepare(self, e: VocabEntry) -> _Prepared:
        regexes: list[re.Pattern] = []
        for p in e.patterns:
            try:
                regexes.append(re.compile(p, re.MULTILINE | re.DOTALL))
            except re.error as exc:
                log.warning("entry %s: pattern does not compile (%s): %r", e.site, exc, p)

        anchors: set[str] = set()
        anchor_len = 0
        token_text: list[str] = [e.anchor]
        for p in e.patterns:
            frags = _literal_fragments(p)
            token_text.extend(f for f, _safe in frags)
            best = ""
            for f, safe in frags:
                if not safe:
                    continue
                anchor_len = max(anchor_len, len(f.strip()))
                if len(f) > len(best):
                    best = f
            best = best.strip("\n")           # anchors must not span record joins oddly
            if _qualifies_as_anchor(best):
                anchors.add(normalize_emoji(best))
        if _qualifies_as_anchor(e.anchor):
            anchors.add(normalize_emoji(e.anchor))
            anchor_len = max(anchor_len, len(e.anchor.strip()))

        tokens = frozenset(_WORD_TOKEN.findall(" ".join(t for t in token_text if t)))
        passthrough = "passthrough" in e.flags
        catchall = (not passthrough) and ("builder-catchall" in e.flags or not anchors)
        return _Prepared(
            entry=e, regexes=regexes, anchors=anchors, anchor_len=anchor_len,
            tokens=tokens, from_t=_ver_tuple(e.valid_from), to_t=_ver_tuple(e.valid_to),
            catchall=catchall, passthrough=passthrough,
        )

    def _compile_semantics(self) -> None:
        sem = self.vocab.semantics or {}
        self._cat_rules: list[_CategoryRule] = []
        for raw in sem.get("categories") or []:
            if not isinstance(raw, dict) or "category" not in raw:
                continue
            class_re = semantic_re = None
            try:
                if raw.get("match_class"):
                    class_re = re.compile(str(raw["match_class"]))
                if raw.get("match_semantic"):
                    semantic_re = re.compile(str(raw["match_semantic"]))
            except re.error as exc:
                log.warning("semantics category rule %r: bad regex (%s) — skipped", raw, exc)
                continue
            self._cat_rules.append(_CategoryRule(
                class_re=class_re,
                body_hint=str(raw["match_body_hint"]) if raw.get("match_body_hint") else None,
                semantic_re=semantic_re,
                category=str(raw["category"]),
            ))
        self._any_hint_rules = any(r.body_hint for r in self._cat_rules)

        self._hints: dict[str, str] = {
            normalize_emoji(str(k)): str(v)
            for k, v in (sem.get("body_emoji_hints") or {}).items()
        }

        self._icon_rules: dict[str, list[_IconRule]] = {}
        icon_sem = sem.get("icon_semantics") or {}
        for platform, rules in icon_sem.items():
            prepared = []
            for raw in rules or []:
                if not isinstance(raw, dict) or "icon" not in raw:
                    continue
                prepared.append(_IconRule(
                    icon=normalize_emoji(str(raw["icon"])).strip(),
                    semantic=str(raw.get("semantic") or ""),
                    family=str(raw.get("family") or ""),
                    from_t=_ver_tuple(raw.get("valid_from")),
                    to_t=_ver_tuple(raw.get("valid_to")),
                ))
            self._icon_rules[str(platform)] = prepared

    # ── public API ───────────────────────────────────────────────────────────

    def classify(self, rec: Record, version: str | None = None) -> Classification:
        """Classify one record. Sets rec.klass and returns it."""
        vt = self._resolve_version(version)
        klass, entry = self._match(rec, vt)
        self._apply_semantics(rec, klass, entry, vt)
        rec.klass = klass
        return klass

    # ── version resolution ───────────────────────────────────────────────────

    def _resolve_version(self, version: str | None) -> tuple[int, ...] | None:
        if version in self._version_cache:
            return self._version_cache[version]
        vt: tuple[int, ...] | None
        if version is None:
            vt = None
        else:
            m = _BUILD_VERSION.match(version)
            if m:
                marketing = self.vocab.build_map.get(m.group(1))
                vt = _ver_tuple(marketing) if marketing else None   # unresolvable build: no filter
            else:
                vt = _ver_tuple(version)
        self._version_cache[version] = vt
        return vt

    def _valid(self, idx: int, vt: tuple[int, ...] | None) -> bool:
        s = self._sites[idx]
        return _in_range(vt, s.from_t, s.to_t)

    # ── matching ─────────────────────────────────────────────────────────────

    def _match(self, rec: Record, vt: tuple[int, ...] | None
               ) -> tuple[Classification, VocabEntry | None]:
        hit = self._passthrough(rec, vt)
        if hit is not None:
            return hit

        candidates = self._tier1(rec, vt)
        if rec.platform == IOS:
            return self._match_ios(rec, vt, candidates)
        return self._match_android(rec, vt, candidates)

    def _passthrough(self, rec: Record, vt: tuple[int, ...] | None
                     ) -> tuple[Classification, VocabEntry | None] | None:
        """Bridge pass-through sites: app-authored bodies — never pattern-matched."""
        idxs = (self._pass_bucket.get((rec.platform, rec.tag_class or "", rec.tag_method or ""))
                or self._pass_class_bucket.get((rec.platform, rec.tag_class or "")))
        if idxs:
            valid = [i for i in idxs if self._valid(i, vt)]
            if valid:
                e = self._sites[valid[0]].entry
                return (Classification(
                    status="passthrough", sites=list(e.source), confidence="exact",
                    semantic=e.semantic, family=e.family,
                ), e)
        # Built-in fallback so pass-through short-circuits even without a vocabulary.
        if rec.platform == IOS and rec.tag_class == "TSLocationManager" \
                and (rec.tag_method or "").startswith("log"):
            return (Classification(status="passthrough", confidence="exact"), None)
        if rec.platform == ANDROID and rec.tag_class == "Logger":
            return (Classification(status="passthrough", confidence="exact"), None)
        return None

    def _tier1(self, rec: Record, vt: tuple[int, ...] | None) -> list[int]:
        automaton = self._automatons.get(rec.platform)
        if automaton is None:
            return []
        haystack = normalize_emoji(rec.raw)
        seen: set[int] = set()
        for _end, idxs in automaton.iter(haystack):
            seen.update(idxs)
        return [i for i in seen if self._valid(i, vt)]

    def _match_ios(self, rec: Record, vt: tuple[int, ...] | None, candidates: list[int]
                   ) -> tuple[Classification, VocabEntry | None]:
        bucket: list[int] = []
        if rec.tag_class:
            bucket = [i for i in self._bucket.get(
                (IOS, rec.tag_class, rec.tag_method or ""), []) if self._valid(i, vt)]
            if not bucket:
                # Harvested selectors may differ in colons/arg counts: class alone.
                bucket = [i for i in self._class_bucket.get(
                    (IOS, rec.tag_class), []) if self._valid(i, vt)]

        for pool in (bucket, candidates):
            verified = self._verify(rec, [i for i in pool if not self._sites[i].catchall])
            if verified:
                return self._pick(rec, verified)

        catchalls = self._verify(rec, [i for i in bucket if self._sites[i].catchall])
        if catchalls and len(catchalls) <= _MAX_CATCHALL_TIE:
            return self._pick(rec, catchalls)

        return self._drift_or_unknown(rec, bucket + candidates)

    def _match_android(self, rec: Record, vt: tuple[int, ...] | None, candidates: list[int]
                       ) -> tuple[Classification, VocabEntry | None]:
        verified = self._verify(rec, [i for i in candidates if not self._sites[i].catchall])
        if verified:
            return self._pick(rec, verified)

        # Catch-alls fire only when no literal pattern hit, scoped by
        # (class, level, icon). Helper-frame tags cannot join by class, so they
        # must agree on BOTH level and icon instead.
        helper = _is_helper_tag(rec.tag_class)
        if rec.tag_class and not helper:
            pool = [i for i in self._class_bucket.get((ANDROID, rec.tag_class), [])
                    if self._sites[i].catchall and self._valid(i, vt)
                    and self._agrees(rec, i, need_both=False)]
        else:
            pool = [i for i in self._android_catchalls
                    if self._valid(i, vt) and self._agrees(rec, i, need_both=True)]
        verified = self._verify(rec, pool)
        if verified:
            klass, entry = self._pick(rec, verified)
            if len(klass.sites) <= _MAX_CATCHALL_TIE * 2 and \
                    self._tied_count(rec, verified) <= _MAX_CATCHALL_TIE:
                return klass, entry

        bucket = []
        if rec.tag_class and not helper:
            bucket = [i for i in self._class_bucket.get((ANDROID, rec.tag_class), [])
                      if self._valid(i, vt)]
        return self._drift_or_unknown(rec, bucket + candidates)

    def _agrees(self, rec: Record, idx: int, need_both: bool) -> bool:
        e = self._sites[idx].entry
        level_ok = bool(e.level and rec.level and e.level == rec.level)
        icon_ok = bool(e.icon and rec.icon and e.icon == rec.icon)
        if need_both:
            return level_ok and icon_ok
        return level_ok or icon_ok or (not e.level and not e.icon)

    def _verify(self, rec: Record, pool: list[int]) -> list[int]:
        """Tier 2: full-regex verify candidates against rec.raw."""
        out = []
        for i in pool:
            for rx in self._sites[i].regexes:
                if rx.search(rec.raw):
                    out.append(i)
                    break
        return out

    # ── selection / scoring ──────────────────────────────────────────────────

    def _score(self, rec: Record, idx: int) -> tuple[int, int, int]:
        """(literal length, tag agreement, level+icon agreement) — in that order."""
        s = self._sites[idx]
        e = s.entry
        tag_w = 0
        if rec.tag_class and not _is_helper_tag(rec.tag_class) and e.klass == rec.tag_class:
            tag_w = 2 if e.method and e.method == (rec.tag_method or "") else 1
        li = 0
        if e.level and rec.level and e.level == rec.level:
            li += 1
        if e.icon and rec.icon and e.icon == rec.icon:
            li += 1
        return (s.anchor_len, tag_w, li)

    def _tied_count(self, rec: Record, verified: list[int]) -> int:
        best = max(self._score(rec, i) for i in verified)
        return sum(1 for i in verified if self._score(rec, i) == best)

    def _pick(self, rec: Record, verified: list[int]
              ) -> tuple[Classification, VocabEntry | None]:
        ranked = sorted(verified, key=lambda i: self._score(rec, i), reverse=True)
        best = self._score(rec, ranked[0])
        group = [i for i in ranked if self._score(rec, i) == best]

        sites: list[str] = []
        for i in group:
            for src in self._sites[i].entry.source:
                if src not in sites:
                    sites.append(src)
        entries = [self._sites[i].entry for i in group]
        primary = entries[0]
        semantics = {e.semantic for e in entries}
        families = {e.family for e in entries}
        klass = Classification(
            status="matched",
            sites=sites or [primary.site],
            confidence="exact" if len(group) == 1 else "ambiguous",
            semantic=primary.semantic if len(semantics) == 1 else "",
            family=primary.family if len(families) == 1 else "",
        )
        return klass, primary

    # ── drift / unknown ──────────────────────────────────────────────────────

    def _drift_or_unknown(self, rec: Record, pool: list[int]
                          ) -> tuple[Classification, VocabEntry | None]:
        if pool:
            raw_tokens = frozenset(_WORD_TOKEN.findall(rec.raw))
            best_share = 0.0
            best_idx: int | None = None
            for i in set(pool):
                s = self._sites[i]
                if len(s.tokens) < _MIN_DRIFT_TOKENS:
                    continue
                share = len(s.tokens & raw_tokens) / len(s.tokens)
                if share >= _DRIFT_SHARE and share > best_share:
                    best_share, best_idx = share, i
            if best_idx is not None:
                e = self._sites[best_idx].entry
                return (Classification(
                    status="drift", sites=list(e.source), confidence="fuzzy",
                    semantic=e.semantic, family=e.family, drift_of=e.site,
                ), e)
        return Classification(status="unknown"), None

    # ── semantics layer ──────────────────────────────────────────────────────

    def _apply_semantics(self, rec: Record, klass: Classification,
                         entry: VocabEntry | None, vt: tuple[int, ...] | None) -> None:
        # Category: first-match-wins over "<tag_class>.<tag_method>", then body
        # hints. A matched entry's own class.method substitutes when the record
        # tag is missing or a helper frame (LoggerFacade$Entry would otherwise
        # swallow every Android record into one bucket).
        if entry is not None and (not rec.tag_class or _is_helper_tag(rec.tag_class)):
            key = f"{entry.klass}.{entry.method}"
        else:
            key = f"{rec.tag_class or ''}.{rec.tag_method or ''}"

        hints: set[str] | None = None
        if not klass.category:
            for rule in self._cat_rules:
                if rule.class_re is not None:
                    if rule.class_re.match(key):
                        klass.category = rule.category
                        break
                elif rule.body_hint is not None:
                    if hints is None:
                        hints = self._body_hints(rec)
                    if rule.body_hint in hints:
                        klass.category = rule.category
                        break
                elif rule.semantic_re is not None:
                    if klass.semantic and rule.semantic_re.match(klass.semantic):
                        klass.category = rule.category
                        break

        # Icon semantics fill semantic/family when the vocabulary entry had none.
        if (not klass.semantic or not klass.family) and rec.icon:
            rule = self._icon_lookup(rec.platform, rec.icon, vt)
            if rule is not None:
                if not klass.semantic:
                    klass.semantic = rule.semantic
                if not klass.family:
                    klass.family = rule.family

    def _body_hints(self, rec: Record) -> set[str]:
        """Substring scan (VS-normalized) — EMOJI_TOKEN misses U+23xx clocks."""
        haystack = normalize_emoji(rec.raw)
        return {hint for emoji, hint in self._hints.items() if emoji in haystack}

    def _icon_lookup(self, platform: str, icon: str,
                     vt: tuple[int, ...] | None) -> _IconRule | None:
        rules = self._icon_rules.get(platform)
        if not rules:
            return None
        # exact (possibly multi-emoji, e.g. 📌🔒) first, then the first emoji token
        for candidate in self._icon_candidates(icon):
            for rule in rules:
                if rule.icon == candidate and _in_range(vt, rule.from_t, rule.to_t):
                    return rule
        return None

    @staticmethod
    def _icon_candidates(icon: str) -> list[str]:
        out = [icon]
        m = EMOJI_TOKEN.match(icon)
        if m and normalize_emoji(m.group(0)) != icon:
            out.append(normalize_emoji(m.group(0)))
        return out


def _qualifies_as_anchor(fragment: str) -> bool:
    if not fragment:
        return False
    if len(fragment.strip()) >= _MIN_ANCHOR_LEN:
        return True
    return _contains_emoji(fragment)
