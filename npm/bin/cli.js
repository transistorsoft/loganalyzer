#!/usr/bin/env node
"use strict";

// npx launcher for transistorsoft-loganalyzer.
//
// The analyzer is Python. Its audience is React Native / Flutter / Capacitor /
// Cordova developers, who all have npm and mostly do not have Python 3.11+ — a
// stock macOS ships 3.9.6 and Windows ships none. So this shim brings its own
// interpreter by way of uv, and the user never learns any of that happened.
//
// uv has no official npm distribution (the `uv` name on npm is an unrelated
// UTF-8 validation library), so the binary is fetched from Astral's GitHub
// releases, at a pinned version, with its published SHA-256 verified before
// anything is executed. Once cached, later runs spend no network at all.

const { spawnSync, execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Pinned so `npx @transistorsoft/loganalyzer@x` is reproducible: a floating
// Python version would make the npm version a lie about what actually runs.
const PY_PACKAGE = "transistorsoft-loganalyzer";
const PY_VERSION = "0.1.3";
const UV_VERSION = "0.12.5";

const TARGETS = {
  "darwin-arm64": { asset: "uv-aarch64-apple-darwin.tar.gz", bin: "uv" },
  "darwin-x64": { asset: "uv-x86_64-apple-darwin.tar.gz", bin: "uv" },
  "linux-arm64": { asset: "uv-aarch64-unknown-linux-gnu.tar.gz", bin: "uv" },
  "linux-x64": { asset: "uv-x86_64-unknown-linux-gnu.tar.gz", bin: "uv" },
  "win32-arm64": { asset: "uv-aarch64-pc-windows-msvc.zip", bin: "uv.exe" },
  "win32-x64": { asset: "uv-x86_64-pc-windows-msvc.zip", bin: "uv.exe" },
};

const log = (msg) => process.stderr.write(`${msg}\n`);

function cacheDir() {
  // Honour the platform conventions rather than scattering dot-dirs in $HOME.
  const base =
    process.env.LOGANALYZER_CACHE ||
    (process.platform === "win32"
      ? process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local")
      : process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"));
  return path.join(base, "transistorsoft-loganalyzer", `uv-${UV_VERSION}`);
}

/** uv already on PATH — by far the common case for anyone who has used it. */
function uvOnPath() {
  try {
    execFileSync("uv", ["--version"], { stdio: "ignore" });
    return "uv";
  } catch {
    return null;
  }
}

async function fetchBuffer(url) {
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url}`);
  return Buffer.from(await res.arrayBuffer());
}

async function downloadUv(target, dir) {
  const base = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;
  log(`loganalyzer: first run — fetching the Python runtime (~35 MB, cached afterwards)`);

  const [archive, sumText] = await Promise.all([
    fetchBuffer(`${base}/${target.asset}`),
    fetchBuffer(`${base}/${target.asset}.sha256`).then((b) => b.toString("utf8")),
  ]);

  // Verify BEFORE writing anything executable to disk.
  const expected = sumText.trim().split(/\s+/)[0].toLowerCase();
  const actual = createHash("sha256").update(archive).digest("hex");
  if (!/^[0-9a-f]{64}$/.test(expected) || actual !== expected) {
    throw new Error(
      `checksum mismatch for ${target.asset}\n  expected ${expected}\n  actual   ${actual}`
    );
  }

  // Extract into a staging dir, then rename: a killed download must never
  // leave a half-extracted binary that the next run trusts.
  const staging = `${dir}.tmp-${process.pid}`;
  fs.rmSync(staging, { recursive: true, force: true });
  fs.mkdirSync(staging, { recursive: true });
  const archivePath = path.join(staging, target.asset);
  fs.writeFileSync(archivePath, archive);

  // bsdtar handles .tar.gz and .zip alike, and ships as tar.exe on Windows 10+.
  execFileSync("tar", ["-xf", archivePath, "-C", staging], { stdio: "ignore" });
  fs.unlinkSync(archivePath);

  const found = findBinary(staging, target.bin);
  if (!found) throw new Error(`${target.bin} not found inside ${target.asset}`);
  fs.chmodSync(found, 0o755);

  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.dirname(dir), { recursive: true });
  fs.renameSync(staging, dir);
  return findBinary(dir, target.bin);
}

/** uv archives nest the binary under uv-<triple>/; don't assume the layout. */
function findBinary(root, name) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      const hit = findBinary(full, name);
      if (hit) return hit;
    } else if (entry.name === name) {
      return full;
    }
  }
  return null;
}

async function resolveUv() {
  const onPath = uvOnPath();
  if (onPath) return onPath;

  const key = `${process.platform}-${process.arch}`;
  const target = TARGETS[key];
  if (!target) {
    log(
      `loganalyzer: no prebuilt uv for ${key}.\n` +
        `  Install uv yourself and re-run:  https://docs.astral.sh/uv/getting-started/installation/\n` +
        `  Or use the Python package directly:  pipx install ${PY_PACKAGE}`
    );
    process.exit(1);
  }

  const dir = cacheDir();
  const cached = fs.existsSync(dir) && findBinary(dir, target.bin);
  if (cached) return cached;

  try {
    return await downloadUv(target, dir);
  } catch (err) {
    log(
      `loganalyzer: could not obtain uv — ${err.message}\n` +
        `  Locked-down networks often block GitHub release downloads. Either\n` +
        `  install uv manually, or skip this launcher entirely:\n` +
        `    pipx install ${PY_PACKAGE}   (needs Python 3.11+)`
    );
    process.exit(1);
  }
}

async function main() {
  const uv = await resolveUv();
  const args = [
    "tool",
    "run",
    "--from",
    `${PY_PACKAGE}==${PY_VERSION}`,
    "loganalyzer",
    ...process.argv.slice(2),
  ];
  const res = spawnSync(uv, args, { stdio: "inherit" });
  if (res.error) {
    log(`loganalyzer: failed to run uv — ${res.error.message}`);
    process.exit(1);
  }
  // Signals do not map onto exit codes; report them the way a shell would.
  process.exit(res.signal ? 128 : (res.status ?? 1));
}

main();
