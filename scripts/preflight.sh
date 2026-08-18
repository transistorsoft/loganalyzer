#!/usr/bin/env bash
# Everything worth checking before tagging a release.
#
# The test suite does not catch packaging or resolution failures, and those are
# what have actually shipped broken: a wheel missing its vocabulary would still
# install and still start; `uvx <pkg>` failed because the console script was not
# named after the package; the npm bin has to resolve from a tarball. None of
# that is reachable from `pytest`, and all of it is reachable from here.
#
#   scripts/preflight.sh
#
# Builds into throwaway directories, touches no registry, publishes nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

GREEN=$'\033[0;32m'; RED=$'\033[0;31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
ok()   { echo "  ${GREEN}✓${RESET} $1"; }
fail() { echo "  ${RED}✗${RESET} $1" >&2; exit 1; }
step() { echo; echo "${DIM}── $1${RESET}"; }

# A real capture to prove the thing actually analyzes something. The committed
# fixtures are scrubbed and always present, so this works from a bare clone.
LOG="$ROOT/tests/fixtures/car.log.gz"
[ -f "$LOG" ] || fail "no fixture at $LOG"

step "1. tests"
cd "$ROOT"
uv run pytest -q >/dev/null || fail "test suite"
ok "$(uv run pytest -q 2>&1 | tail -1)"

step "2. version pins agree"
PY_VER="$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2)"
NPM_VER="$(node -p "require('$ROOT/npm/package.json').version" 2>/dev/null || echo "?")"
CLI_VER="$(grep -o 'PY_VERSION = "[^"]*"' npm/bin/cli.js | cut -d'"' -f2)"
[ "$PY_VER" = "$NPM_VER" ] || fail "npm/package.json is $NPM_VER, pyproject is $PY_VER"
[ "$PY_VER" = "$CLI_VER" ] || fail "cli.js pins $CLI_VER, pyproject is $PY_VER"
ok "pyproject / npm / cli.js all $PY_VER"

step "3. wheel builds and carries the vocabulary"
rm -rf "$ROOT/dist"
uv build >/dev/null 2>&1 || fail "uv build"
WHEEL="$(ls "$ROOT"/dist/*.whl)"
VOCAB_N="$(unzip -l "$WHEEL" | grep -c "/vocabulary/.*\.yaml" || true)"
[ "$VOCAB_N" -ge 4 ] || fail "wheel carries only $VOCAB_N vocabulary files — the analyzer would be inert"
ok "$(basename "$WHEEL") — $VOCAB_N vocabulary files"

step "4. wheel installs clean and BOTH console scripts run"
cd "$WORK" && uv venv -q && uv pip install -q "$WHEEL"
for cmd in loganalyzer transistorsoft-loganalyzer; do
  [ -x ".venv/bin/$cmd" ] || fail "$cmd missing — \`uvx <package>\` needs a script named after the package"
  "./.venv/bin/$cmd" --version >/dev/null || fail "$cmd --version"
  "./.venv/bin/$cmd" "$LOG" --out "$WORK/out-$cmd" --no-open >/dev/null 2>&1 || fail "$cmd on a real capture"
done
ok "loganalyzer + transistorsoft-loganalyzer both analyze a capture"

step "5. artifacts land, and the output dir ignores itself"
OUT="$WORK/out-loganalyzer"
for f in digest.md digest.json map.html aliases.local.json; do
  [ -f "$OUT"/*/"$f" ] || fail "$f not written"
done
grep -qx '\*' "$OUT/.gitignore" || fail "output dir did not ignore itself"
ok "digest.md · digest.json · map.html · aliases.local.json + self-ignoring dir"

step "6. npm package resolves its bin"
cd "$ROOT/npm"
TGZ="$WORK/$(npm pack --silent --pack-destination "$WORK")"
cd "$WORK" && npm init -y --silent >/dev/null 2>&1
npm install --silent "$TGZ" >/dev/null 2>&1 || fail "npm install of the tarball"
[ -x node_modules/.bin/loganalyzer ] || fail "npm bin 'loganalyzer' not created"
# Against the wheel just built, not the pinned version — that version does not
# exist on PyPI until this release ships, so pinning would only ever test the
# PREVIOUS one.
LOGANALYZER_FROM="$WHEEL" ./node_modules/.bin/loganalyzer "$LOG" \
  --out "$WORK/npmout" --no-open >/dev/null 2>&1 \
  || fail "npm launcher could not analyze a capture"
ok "npx bin resolves and runs the wheel under test"

step "7. this version is not already published"
# The only irreversible mistake here. A PyPI version can never be reused and an
# npm one only within 72 hours, so "already taken" must surface now rather than
# as a failed release. Skipped without network.
if curl -sfI --max-time 8 https://pypi.org >/dev/null 2>&1; then
  PY_CODE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
      "https://pypi.org/pypi/transistorsoft-loganalyzer/$PY_VER/json")"
  [ "$PY_CODE" = "404" ] || fail "PyPI already has $PY_VER (HTTP $PY_CODE) — bump the version, it cannot be reused"
  NPM_CODE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
      "https://registry.npmjs.org/@transistorsoft%2Floganalyzer/$PY_VER")"
  [ "$NPM_CODE" = "404" ] || fail "npm already has $PY_VER (HTTP $NPM_CODE) — bump the version"
  ok "$PY_VER is unused on both registries"
else
  echo "  ${DIM}- skipped (offline)${RESET}"
fi

echo
echo "${GREEN}preflight passed${RESET} — $PY_VER is ready to tag"
echo "${DIM}  git tag v$PY_VER && git push origin v$PY_VER${RESET}"
