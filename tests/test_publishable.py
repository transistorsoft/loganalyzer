"""Guards on what this package is allowed to contain.

Everything here is a *publishing* assertion, not a behaviour one. The vocabulary
is generated from a private SDK checkout and the fixtures are derived from real
captures, so both are one careless regeneration away from carrying something
that must not ship. These tests fail loudly at that moment instead of quietly at
the next release.

They deliberately do not import anything from the private tooling — this package
must be able to police itself from a bare public clone.
"""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
VOCAB = ROOT / "src" / "loganalyzer" / "vocabulary"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Built at runtime so this file does not itself contain the strings it forbids.
LOCAL_PATH = "/" + "Users" + "/"
PRIVATE_REPO = "bg" + "-forge"

# Harvest bookkeeping. It quotes git stderr, which embeds the operator's
# absolute paths, and says nothing about what a log line means.
PROVENANCE_KEYS = {"self_check", "unharvestable", "merge", "notes", "site_counts"}

TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".md", ".cfg", ".txt"}


def _vocabularies():
    for name in ("android.yaml", "ios.yaml"):
        path = VOCAB / name
        if path.exists():
            yield name, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_vocabulary_carries_no_source_refs():
    """The entry-id -> SDK file:line index is private.

    It is a map of ~1,300 call sites inside TSLocationManager, it is useless
    without that source checked out, and it is merged back in at runtime from a
    sidecar the private tooling holds. If a harvest lands here unsplit, this is
    where it stops.
    """
    for name, doc in _vocabularies():
        with_source = [e.get("id") for e in (doc.get("entries") or []) if e.get("source")]
        assert not with_source, (
            f"{name}: {len(with_source)} entries still carry `source:` — this "
            f"vocabulary was harvested but not split. Run "
            f"`python -m loganalyzer_forge.sources` before committing. "
            f"First few: {with_source[:3]}")


def test_vocabulary_carries_no_harvest_provenance():
    for name, doc in _vocabularies():
        leaked = PROVENANCE_KEYS & set(doc.get("meta") or {})
        assert not leaked, f"{name}: harvest provenance not split out: {sorted(leaked)}"


def test_no_shipped_file_leaks_a_local_path():
    """Nothing shipped should name a developer's filesystem or the private
    monorepo. Catches provenance shapes the key list above does not know about."""
    offenders = []
    for path in ROOT.rglob("*"):
        if path.suffix not in TEXT_SUFFIXES or not path.is_file():
            continue
        if path == Path(__file__) or ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in (LOCAL_PATH, PRIVATE_REPO):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)} contains {needle!r}")
    assert not offenders, "private paths in shipped files:\n  " + "\n  ".join(offenders)


@pytest.mark.skipif(not FIXTURES.exists(), reason="no committed fixtures")
def test_fixtures_are_scrubbed():
    """Committed captures are real logs with their identifiers replaced and
    their coordinates moved. Guards against dropping in a raw capture."""
    for path in sorted(FIXTURES.glob("*.log.gz")):
        text = gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
        for needle in ("com.transistorsoft.", "tracker.transistorsoft.com", LOCAL_PATH):
            assert needle not in text, f"{path.name} is not scrubbed: contains {needle!r}"


@pytest.mark.skipif(not FIXTURES.exists(), reason="no committed fixtures")
def test_fixtures_still_parse():
    """A scrubbed fixture is worthless if the scrubbing broke the grammar."""
    from loganalyzer.records import assemble
    from loganalyzer.sniff import load_sources

    for path in sorted(FIXTURES.glob("*.log.gz")):
        source = load_sources([path])[0]
        assert source.platform in ("android", "ios"), f"{path.name}: platform lost"
        records = assemble(source.platform, source.text, 2026)
        assert len(records) > 100, f"{path.name}: only {len(records)} records"


# ── the npm launcher's pins must track this package ──────────────────────────

NPM = ROOT / "npm"
PY_PACKAGE_HINT = "transistorsoft-loganalyzer"


@pytest.mark.skipif(not NPM.exists(), reason="npm launcher not present")
def test_npm_launcher_pins_this_version():
    """`npx @transistorsoft/loganalyzer@X` must actually run X.

    The launcher pins the PyPI version it invokes. If that pin drifts from this
    package's version, the npm version becomes a lie about what runs — and the
    two live in the same repo, so nothing but a check keeps them together.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    pkg = json.loads((NPM / "package.json").read_text(encoding="utf-8"))
    assert pkg["version"] == version, (
        f"npm package.json is {pkg['version']}, pyproject is {version}")

    cli = (NPM / "bin" / "cli.js").read_text(encoding="utf-8")
    pinned = re.search(r'PY_VERSION = "([^"]+)"', cli).group(1)
    assert pinned == version, (
        f"cli.js pins {PY_PACKAGE_HINT}=={pinned}, pyproject is {version}")

    pkg_name = re.search(r'PY_PACKAGE = "([^"]+)"', cli).group(1)
    assert pkg_name == re.search(r'^name = "([^"]+)"', pyproject, re.M).group(1)



@pytest.mark.skipif(not NPM.exists(), reason="npm launcher not present")
def test_npm_launcher_verifies_what_it_downloads():
    """It fetches and executes a binary, so the checksum check is not optional."""
    cli = (NPM / "bin" / "cli.js").read_text(encoding="utf-8")
    assert "createHash" in cli and "sha256" in cli
    assert "checksum mismatch" in cli
    # pinned uv, not "latest" — a moving target defeats the pin above
    assert re.search(r'UV_VERSION = "\d+\.\d+\.\d+"', cli)
