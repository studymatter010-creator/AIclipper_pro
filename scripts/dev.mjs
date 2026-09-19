#!/usr/bin/env node
/**
 * AIClipper full-stack dev launcher.
 *
 * `npm run dev` (this script) brings up the ENTIRE application AND every AI
 * service/model it depends on, in one shot:
 *
 *   1. Python virtualenv  -> create if missing, install deps if needed
 *   2. AI model files     -> Whisper (.ggml-small.bin) + MediaPipe (.tflite)
 *   3. Ollama (brain)     -> start it if not running; pull qwen3:8b if absent
 *   4. Runtime folders    -> uploads/, outputs/, subtitles/, thumbnails/, etc.
 *   5. Huey worker        -> background task consumer (YouTube/FB uploads)
 *   6. FastAPI (uvicorn)  -> foreground web server, then opens the browser
 *
 * Ctrl+C stops the server and shuts the worker back down cleanly.
 *
 * Optional subcommands: `worker`, `server`, `models`, `check`.
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

const isWin = process.platform === 'win32';
const VENV_PY = isWin
  ? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
  : path.join(ROOT, '.venv', 'bin', 'python');

const OLLAMA_HOST = process.env.OLLAMA_HOST || 'http://localhost:11434';
const OLLAMA_BASE = OLLAMA_HOST.replace(/\/+$/, '');
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'qwen3:8b';

// ── The model TEAM ─────────────────────────────────────────────────────
// Every specialist the app depends on is pulled at startup so the whole
// team "starts with the app".  The RAM sequencer (backend/services/
// model_team.py) keeps them cooperating in sequence within 16GB.
const TEAM_MODELS = [
  OLLAMA_MODEL,                                  // brain (metadata, scoring)
  process.env.TEAM_CLASSIFY || 'qwen2.5:3b',     // content classifier
  process.env.TEAM_HOOK || 'qwen2.5:3b',         // hook/virality line
  process.env.TEAM_TRANSLATE || 'qwen2.5:3b',    // Chinese subtitle translator
];
// NOTE: flux.1-schnell removed — not a valid Ollama model (pull fails);
// real-frame thumbnails via FFmpeg are used instead.
const TEAM_MODEL_NAMES = [...new Set(TEAM_MODELS)]; // dedupe (3b appears 3x)

const SERVER_HOST = process.env.HOST || '0.0.0.0';
const SERVER_PORT = process.env.PORT || '8000';
const SERVER_URL = `http://localhost:${SERVER_PORT}`;

const REQUIRED_MODELS = [
  'models/ggml-small.bin',
  'models/blaze_face_short_range.tflite',
];

const RUNTIME_DIRS = [
  'uploads', 'outputs', 'subtitles', 'thumbnails', 'logs', 'data', 'models', 'temp',
];

const colors = {
  reset: '\x1b[0m',
  cyan: '\x1b[36m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  red: '\x1b[31m',
  dim: '\x1b[90m',
  bold: '\x1b[1m',
};
const paint = (c, s) => `${colors[c]}${s}${colors.reset}`;

function log(msg) { console.log(`${paint('cyan', '   ▲ ')}${msg}`); }
function step(n, msg) { console.log(`\n${paint('bold', paint('green', `── [${n}] ${msg}`))}`); }
function warn(msg) { console.log(`${paint('yellow', '  [warn]')} ${msg}`); }
function ok(msg) { console.log(`   ${paint('green', '✓')} ${msg}`); }
function fail(msg) { console.log(`   ${paint('red', '✗')} ${msg}`); }

// ---------------------------------------------------------------------------
// 1. Python virtualenv + dependencies
// ---------------------------------------------------------------------------

/** True if an interpreter path is unusable from PowerShell/cmd. */
function rejectPythonPath(exe) {
  // MSYS / Git-Bash styles, or the MS Store alias stub (not a real interpreter).
  return /^\/|[/\\]usr[/\\]|[Mm][Ss][Yy][Ss]64|WindowsApps|Microsoft\\WindowsApps/.test(exe);
}

/** Run a python one-liner and return { exe, version } or null. */
function probePython(cmdArgs) {
  try {
    const r = spawnSync(cmdArgs[0], cmdArgs.slice(1), {
      cwd: ROOT, shell: true, encoding: 'utf8', windowsHide: true,
    });
    if (r.status !== 0 || !r.stdout) return null;
    const lines = r.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    const exe = lines[0];
    const version = lines[1] || '';
    if (!exe || rejectPythonPath(exe)) return null;
    const vres = spawnSync(exe, ['--version'], { encoding: 'utf8', windowsHide: true });
    if (vres.status !== 0) return null; // path exists but won't run -> skip
    return { exe, version, command: cmdArgs.join(' ') };
  } catch {
    return null;
  }
}

/** Scan the filesystem for real Windows Python installs (official installer paths). */
function scanWindowsPython() {
  const hits = [];
  const roots = new Set();
  if (process.env.LOCALAPPDATA) roots.add(path.join(process.env.LOCALAPPDATA, 'Programs', 'Python'));
  if (process.env.ProgramFiles) { roots.add(process.env.ProgramFiles); }
  if (process.env['ProgramFiles(x86)']) roots.add(process.env['ProgramFiles(x86)']);
  roots.add('C:\\');

  for (const root of roots) {
    if (!root) continue;
    // Try "<root>\Python*\python.exe" and a bare "C:\Python*\python.exe".
    const globs = [
      path.join(root, 'Python', 'Python*', 'python.exe'),
      path.join(root, 'Python3*', 'python.exe'),
      path.join(root, 'Python*', 'python.exe'),
    ];
    for (const g of globs) {
      const dir = path.dirname(g);
      if (!existsSync(dir)) continue;
      let entries;
      try { entries = readdirSync(dir); } catch { continue; }
      for (const e of entries) {
        const p = path.join(dir, e, 'python.exe');
        if (existsSync(p)) hits.push(p);
      }
    }
    // ProgramFiles\PythonXX\python.exe and ProgramFiles\PythonXX
    const direct = path.join(root, 'python.exe');
    if (existsSync(direct)) hits.push(direct);
  }
  return [...new Set(hits)];
}

/**
 * Locate a working *Windows-native* base Python interpreter.  Tries, in order:
 * the `py` launcher, `python`/`python3` on PATH, the `py -0p` registry list,
 * then a filesystem scan of common install dirs.  Deliberately rejects MSYS /
 * Git-Bash bases and the MS Store alias.
 *
 * Returns { cmd, exe, version } (cmd is safe for `python -m venv` with
 * shell:true), or null if nothing usable is found.
 */
/** Collect every distinct usable Windows-native Python interpreter across all sources. */
function collectPythonCandidates() {
  const seen = new Set();
  const addLive = (exe, version) => {
    if (!exe || rejectPythonPath(exe) || seen.has(exe)) return null;
    const vres = spawnSync(exe, ['--version'], { encoding: 'utf8', windowsHide: true });
    if (vres.status !== 0) return null; // path exists but won't run
    seen.add(exe);
    return { exe, version: (version || vres.stdout || '').replace(/[^\d.]/g, ''), command: exe };
  };

  const out = [];

  // 0) Explicit override, e.g. AICLIPPER_PYTHON=C:\Python312\python.exe
  if (process.env.AICLIPPER_PYTHON && !rejectPythonPath(process.env.AICLIPPER_PYTHON)) {
    const c = addLive(process.env.AICLIPPER_PYTHON, '');
    if (c) out.push(c);
  }

  // 1) py launcher + PATH python
  const cmds = isWin ? ['py -3', 'py', 'python', 'python3', 'py -0'] : ['python3', 'python'];
  for (const c of cmds) {
    const p = probePython([...c.split(' '), '-c', 'import sys;print(sys.executable);print(f"{sys.version_info[0]}.{sys.version_info[1]}")']);
    if (p) out.push(p);
  }

  if (!isWin) return out;

  // 2) py -0p (lists every registered interpreter with its path)
  try {
    const r = spawnSync('py', ['-0p'], { cwd: ROOT, shell: true, encoding: 'utf8', windowsHide: true });
    if (r.status === 0 && r.stdout) {
      for (const line of r.stdout.split(/\r?\n/)) {
        const m = line.match(/(\d+(?:\.\d+)*)\s+(\S+[\\/]python\.exe)/i);
        if (m) {
          const c = addLive(m[2].trim(), m[1]);
          if (c) out.push(c);
        }
      }
    }
  } catch { /* ignore */ }

  // 3) Filesystem scan of common install locations
  for (const exe of scanWindowsPython()) {
    const c = addLive(exe, '');
    if (c) out.push(c);
  }

  // Deduplicate by path (same interpreter found by several methods).
  const byPath = new Map();
  for (const c of out) if (!byPath.has(c.exe)) byPath.set(c.exe, c);
  return [...byPath.values()];
}

/**
 * Locate the best usable *Windows-native* base Python for this project.
 * Prefers an interpreter that satisfies pyproject.toml's requires-python, and
 * among those picks the highest version.  Falls back to any usable interpreter
 * only if none match (so the version-checker can still report the mismatch).
 */
function findBasePython() {
  const candidates = collectPythonCandidates();
  if (candidates.length === 0) return null;

  const spec = (() => {
    try {
      const m = readFileSync(path.join(ROOT, 'pyproject.toml'), 'utf8').match(/^requires-python\s*=\s*"([^"]+)"/m);
      return m ? m[1] : '';
    } catch { return ''; }
  })();

  const toKey = (c) => (c.version.match(/\d+/g) || []).slice(0, 2).map((n) => String(n).padStart(2, '0')).join('');
  const matching = spec
    ? candidates.filter((c) => checkPythonVersion(c.version).ok)
    : candidates;
  const pool = matching.length > 0 ? matching : candidates;
  pool.sort((a, b) => (toKey(b) || '').localeCompare(toKey(a) || ''));
  return pool[0];
}

/** True if the venv's python binary actually runs (base interpreter resolves). */
function pythonUsable() {
  try {
    const r = spawnSync(VENV_PY, ['--version'], { cwd: ROOT, encoding: 'utf8', windowsHide: true });
    return r.status === 0;
  } catch {
    return false;
  }
}

/** Return the venv's Python major.minor version string (e.g. "3.12"), or '' if unusable. */
function venvVersion() {
  try {
    const r = spawnSync(VENV_PY, ['-c', 'import sys;print(f"{sys.version_info[0]}.{sys.version_info[1]}")'], {
      cwd: ROOT, encoding: 'utf8', windowsHide: true,
    });
    return r.status === 0 ? (r.stdout || '').trim() : '';
  } catch {
    return '';
  }
}

/** Normalize a version string to "major.minor". */
function shortVersion(v) {
  const parts = (v || '').replace(/[^\d.]/g, '').split('.');
  return parts.length >= 2 ? `${parts[0]}.${parts[1]}` : (v || '');
}

/**
 * Parse the project's requires-python from pyproject.toml and return a
 * compatibility check against the given version string.
 * Returns { ok: true } or { ok: false, required: string, reason: string }.
 */
function checkPythonVersion(versionStr) {
  try {
    const toml = readFileSync(path.join(ROOT, 'pyproject.toml'), 'utf8');
    const m = toml.match(/^requires-python\s*=\s*"([^"]+)"/m);
    if (!m) return { ok: true }; // no constraint → skip

    const spec = m[1]; // e.g. ">=3.12,<3.13"
    const parts = spec.split(',').map((s) => s.trim());
    const ver = (versionStr || '').replace(/[^\d.]/g, '');
    const [major, minor] = ver.split('.').map(Number);

    for (const p of parts) {
      const opM = p.match(/^([><=!]+)\s*(\d+\.?\d*)/);
      if (!opM) continue;
      const [, op, req] = opM;
      const [rMaj, rMin] = req.split('.').map(Number);
      const vNum = major * 100 + minor;
      const rNum = (rMaj || 0) * 100 + (rMin || 0);

      let pass = false;
      if (op === '>=' || op === '>=') pass = vNum >= rNum;
      else if (op === '>' || op === '>') pass = vNum > rNum;
      else if (op === '<=' || op === '<=') pass = vNum <= rNum;
      else if (op === '<' || op === '<') pass = vNum < rNum;
      else if (op === '==') pass = vNum === rNum;

      if (!pass) {
        return {
          ok: false,
          required: spec,
          reason: `Found Python ${major}.${minor} but project requires ${spec}`,
        };
      }
    }
    return { ok: true };
  } catch {
    return { ok: true }; // can't parse → don't block
  }
}

function ensureVenv() {
  step(1, 'Python environment');

  const basePy = findBasePython();
  if (!basePy) {
    fail('No usable Python interpreter found after checking:');
    console.log('   -  ' + (isWin ? 'py -3, py, python, python3' : 'python3, python') + ' on PATH');
    if (isWin) console.log('   -  the "py -0p" launcher registry list');
    console.log('   -  common install folders (Program Files/AppData, C:\\Python*, etc.)');
    console.log('');
    console.log(`   ${paint('yellow', 'Does AIClipper need a Windows Python, or do you launch it from Git Bash?')}`);
    console.log(`   ${paint('yellow', 'Tip: make sure the official python.org installer is on PATH (its "py" launcher makes this work best).')}`);
    console.log(`   ${paint('yellow', 'To point the launcher at a specific interpreter, set the env var AICLIPPER_PYTHON=C:\\path\\to\\python.exe.')}`);
    process.exit(1);
  }
  ok(`Base Python: ${basePy.exe}`);

  // Check that the found Python satisfies pyproject.toml's requires-python constraint.
  const verCheck = checkPythonVersion(basePy.version);
  if (!verCheck.ok) {
    console.log('');
    fail(verCheck.reason);
    console.log('');
    console.log(`   AIClipper requires Python ${verCheck.required}.`);
    console.log('');
    console.log(`   ${paint('yellow', 'Install Python 3.12 from https://www.python.org/downloads/')}`);
    console.log(`   ${paint('yellow', '  → check "Add python.exe to PATH" during install.')}`);
    console.log(`   ${paint('yellow', '  → OR set AICLIPPER_PYTHON=C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe')}`);
    console.log('');
    console.log(`   After installing, delete the .venv and re-run: npm run dev`);
    process.exit(1);
  }

  let venvExists = existsSync(VENV_PY);

  const baseShort = shortVersion(basePy.version);
  const venvShort = venvVersion();
  const venvVersionMismatch = venvExists && pythonUsable() && baseShort && venvShort && baseShort !== venvShort;

  if (venvVersionMismatch) {
    warn(`Existing .venv is on Python ${venvShort} but you're now using ${baseShort}.`);
    warn('Rebuilding .venv on the current base interpreter...');
    rmSync(path.join(ROOT, '.venv'), { recursive: true, force: true });
    venvExists = false;
  }

  if (venvExists && pythonUsable()) {
    ok(`Virtual environment found and usable (Python ${venvShort})`);
  } else {
    if (venvExists) {
      warn('Existing .venv is broken or was created under Git/Bash (MSYS paths are not usable from PowerShell).');
      warn('Recreating it with a Windows-native Python...');
      rmSync(path.join(ROOT, '.venv'), { recursive: true, force: true });
    } else {
      log('Creating virtual environment (.venv)...');
    }
    const r = spawnSync(basePy.command, ['-m', 'venv', path.join(ROOT, '.venv')], {
      cwd: ROOT, shell: true, stdio: 'inherit', windowsHide: true,
    });
    if (r.status !== 0) { fail('Failed to create virtual environment'); process.exit(1); }
    ok('Virtual environment created');
  }

  // Install deps only if FastAPI isn't present in the venv yet.
  const probe = spawnSync(VENV_PY, ['-c', 'import fastapi, pydantic, sqlalchemy'], { cwd: ROOT, windowsHide: true });
  if (probe.status !== 0) {
    log('Installing dependencies (first run)...');
    const r = spawnSync(VENV_PY, ['-m', 'pip', 'install', '-e', '.[dev]', '--quiet'], { cwd: ROOT, stdio: 'inherit', windowsHide: true });
    if (r.status !== 0) { fail('pip install failed'); process.exit(1); }
    ok('Dependencies installed');
  } else {
    ok('Dependencies present');
  }
}

// ---------------------------------------------------------------------------
// 2. AI model files
// ---------------------------------------------------------------------------
function ensureModels() {
  step(2, 'AI model files (Whisper + MediaPipe)');
  const missing = REQUIRED_MODELS.filter((m) => !existsSync(path.join(ROOT, m)));
  if (missing.length === 0) {
    ok(`All models present (${REQUIRED_MODELS.length})`);
    return;
  }
  warn(`Missing: ${missing.join(', ')}`);
  log('Downloading required AI models...');
  const r = spawnSync(VENV_PY, ['-m', 'backend.utils.download_models'], { cwd: ROOT, stdio: 'inherit' });
  if (r.status !== 0) { warn('Model download had issues — some AI features may degrade.'); return; }
  ok('Models download complete');
}

// ---------------------------------------------------------------------------
// 3. Ollama (the "AI brain" — my content-classifier + ai_brain depend on it)
// ---------------------------------------------------------------------------
async function ollamaReachable() {
  try {
    const res = await fetch(`${OLLAMA_BASE}/api/tags`, { signal: AbortSignal.timeout(4000) });
    return res.ok;
  } catch {
    return false;
  }
}

async function ollamaHasModel(model) {
  try {
    const res = await fetch(`${OLLAMA_BASE}/api/tags`, { signal: AbortSignal.timeout(4000) });
    if (!res.ok) return false;
    const data = await res.json();
    return (data.models || []).some((m) => m.name === model || m.name.startsWith(`${model}:`));
  } catch {
    return false;
  }
}

/** Locate the Ollama executable (CLI `ollama` or the desktop `ollama app`).
 *  Searches PATH first (the installer usually adds it), then the standard
 *  Windows install folders — so we find it even when it is not on the PATH
 *  of the shell that launched `npm run dev`. Returns an absolute path or null.
 */
function findOllamaExe() {
  const candidates = [];
  if (isWin) {
    for (const c of ['ollama', 'ollama.exe', 'ollama app.exe']) {
      try {
        const r = spawnSync('where', [c], { cwd: ROOT, encoding: 'utf8', windowsHide: true });
        if (r.status === 0 && r.stdout) {
          const first = r.stdout.split(/\r?\n/).map((s) => s.trim()).find(Boolean);
          if (first && existsSync(first)) candidates.push(first);
        }
      } catch { /* ignore */ }
    }
    const LOCALAPPDATA = process.env.LOCALAPPDATA || '';
    const folders = [
      path.join(LOCALAPPDATA, 'Programs', 'Ollama'),
      path.join(LOCALAPPDATA, 'Ollama'),
      path.join(LOCALAPPDATA, 'Programs', 'Ollama', 'bin'),
      path.join(LOCALAPPDATA, 'Ollama', 'bin'),
      'C:\\Program Files\\Ollama',
      'C:\\Program Files (x86)\\Ollama',
    ];
    for (const f of folders) {
      if (!f) continue;
      for (const name of ['ollama.exe', 'ollama app.exe']) {
        const p = path.join(f, name);
        if (existsSync(p)) candidates.push(p);
      }
    }
  } else {
    const r = spawnSync('which', ['ollama'], { cwd: ROOT, encoding: 'utf8' });
    if (r.status === 0 && r.stdout) candidates.push(r.stdout.trim());
  }
  return [...new Set(candidates)].find((p) => p && existsSync(p)) || null;
}

function tryStartOllama() {
  const exe = findOllamaExe();
  if (!exe) {
    warn('Ollama executable not found.');
    warn('Install it from https://ollama.com/download (or add it to your PATH),');
    warn('or start the Ollama desktop app manually — AI features will be degraded until it runs.');
    return false;
  }
  // Launch it detached in the background so it keeps running after this
  // script exits. The desktop app ("ollama app.exe") starts the server itself;
  // the CLI (`ollama serve`) runs the headless server.
  const isGui = isWin && /app\.exe$/i.test(exe);
  log(`Starting Ollama: ${exe}${isGui ? ' (desktop app)' : ' (serve)'}`);
  const p = spawn(exe, isGui ? [] : ['serve'], {
    cwd: ROOT, detached: true, stdio: 'ignore', windowsHide: true,
  });
  p.on('error', (e) => warn(`Ollama failed to launch: ${e.message}`));
  p.unref();
  return true;
}

async function ensureOllama() {
  step(3, 'Ollama brain (llm backend for content-classifier + AI-Brain)');

  if (await ollamaReachable()) {
    ok(`Ollama already running at ${OLLAMA_BASE}`);
  } else {
    warn('Ollama is not running.');
    if (!tryStartOllama()) {
      warn('Without Ollama, the content-classifier and AI-Brain will gracefully fall back to heuristics.');
      return;
    }
    // Wait for it to come up (max ~60s — first launch of the desktop app can
    // take a while; a fresh `serve` is usually ready in a few seconds).
    log('Waiting for Ollama to come up...');
    let up = false;
    for (let i = 0; i < 120; i++) {
      if (await ollamaReachable()) { up = true; break; }
      await sleep(500);
    }
    if (up) {
      ok('Ollama started');
    } else {
      warn('Ollama did not become reachable in time — starting anyway (AI features will degrade).');
      warn(`Check that Ollama is serving at ${OLLAMA_BASE}.`);
      return;
    }
  }

  // Pull every member of the model team (brain + specialists + thumbnail model).
  const first = TEAM_MODEL_NAMES[0];
  if (await ollamaHasModel(first)) {
    ok(`Model "${first}" present`);
  } else {
    log(`Pulling model ${first} (may take a while on first run)...`);
    const pull = spawn(OLLAMA_BASE.startsWith('http://localhost') ? 'ollama' : process.execPath, ['pull', first], { cwd: ROOT, stdio: 'inherit' });
    const code = await new Promise((res) => pull.on('close', res));
    if (code === 0) ok(`Model "${first}" ready`);
    else warn(`Could not pull "${first}" — AI features will degrade gracefully.`);
  }

  // Ensure each remaining specialist is present (only pulling what's missing).
  for (const model of TEAM_MODEL_NAMES.slice(1)) {
    if (await ollamaHasModel(model)) { continue; }
    log(`Pulling team model ${model} (first run may take a while)...`);
    const pull = spawn(OLLAMA_BASE.startsWith('http://localhost') ? 'ollama' : process.execPath, ['pull', model], { cwd: ROOT, stdio: 'inherit' });
    const code = await new Promise((res) => pull.on('close', res));
    if (code === 0) ok(`Team model "${model}" ready`);
    else warn(`Could not pull "${model}" — that feature will degrade gracefully.`);
  }
  ok(`Model team ready (${TEAM_MODEL_NAMES.join(', ')})`);
}

// ---------------------------------------------------------------------------
// 4. Runtime folders
// ---------------------------------------------------------------------------
function ensureDirs() {
  step(4, 'Runtime folders');
  for (const d of RUNTIME_DIRS) mkdirSync(path.join(ROOT, d), { recursive: true });
  ok(`Ensured ${RUNTIME_DIRS.length} runtime folders`);
}

// ---------------------------------------------------------------------------
// 4b. AI-Editor readiness — verifies every model/dependency/font the one-click
//      AI edit needs BEFORE the server starts, and reports a clear ✓/✗ table.
//      The AI editor (backend/services/auto_editor.py) depends on:
//        - FFmpeg + ffprobe (grading, captions, intro, concat)
//        - the `drawtext` caption filter (guaranteed-caption fallback)
//        - the `ass` subtitle filter (professional karaoke; optional)
//        - Whisper + MediaPipe models (transcription + face tracking)
//        - Windows fonts for burned captions + thumbnails (incl. CJK YaHei)
// ---------------------------------------------------------------------------

// Extra model files beyond the core two (kept in REQUIRED_MODELS above).
const AE_MODELS = [
  ['models/ggml-small.bin', 'Whisper small multilingual (EN+ZH transcription)'],
  ['models/blaze_face_short_range.tflite', 'MediaPipe (face tracking)'],
];

// Windows fonts burned onto the edited video / thumbnails by the AI editor.
// msyh.ttc is Microsoft YaHei — required for Chinese (Bilibili) captions.
const AE_FONTS = isWin ? [
  ['C:\\Windows\\Fonts\\arial.ttf', 'Caption font (Arial)'],
  ['C:\\Windows\\Fonts\\arialbd.ttf', 'Thumbnail bold font (Arial Bold)'],
  ['C:\\Windows\\Fonts\\seguiemj.ttf', 'Thumbnail emoji font (Segoe UI Emoji)'],
  ['C:\\Windows\\Fonts\\msyh.ttc', 'Chinese captions (Microsoft YaHei)'],
] : [];

function ffmpegResult(cmd, args) {
  try {
    const r = spawnSync(cmd, args, { encoding: 'utf8', windowsHide: true });
    return r;
  } catch {
    return { status: -1, stdout: '' };
  }
}

function ensureAEEditorReady() {
  step(7, 'AI Editor readiness');
  const rows = [];

  // FFmpeg + ffprobe executables.
  const hasFF = ffmpegResult('ffmpeg', ['-hide_banner', '-version']).status === 0;
  rows.push({ ok: hasFF, label: 'FFmpeg (encode/grading/captions)',
              fix: 'Install FFmpeg & add it to PATH: https://ffmpeg.org/download.html' });
  rows.push({ ok: ffmpegResult('ffprobe', ['-version']).status === 0, label: 'ffprobe (probe)',
              fix: 'ships with FFmpeg — install together' });

  // Caption render filters.
  const filters = hasFF ? (ffmpegResult('ffmpeg', ['-hide_banner', '-filters']).stdout || '') : '';
  const hasFilter = (name) => filters.includes(` ${name} `);
  rows.push({ ok: hasFilter('drawtext'), label: 'Caption filter: drawtext (guaranteed)',
              fix: 'build FFmpeg with --enable-libfreetype' });
  rows.push({ ok: hasFilter('ass'), label: 'Caption filter: ass (professional, optional)',
              fix: 'build FFmpeg with --enable-libass' });

  // Required encoders (video + audio output).
  const encoders = hasFF ? (ffmpegResult('ffmpeg', ['-hide_banner', '-encoders']).stdout || '') : '';
  const hasEncoder = (name) => encoders.includes(` ${name} `);
  rows.push({ ok: hasEncoder('libx264'), label: 'Encoder: libx264 (video output)',
              fix: 'build FFmpeg with --enable-libx264' });
  rows.push({ ok: hasEncoder('aac'), label: 'Encoder: aac (audio output)',
              fix: 'build FFmpeg with --enable-aac / native aac' });

  // Model files.
  for (const [rel, label] of AE_MODELS) {
    rows.push({ ok: existsSync(path.join(ROOT, rel)), label: `${label} (${rel})`,
                fix: 'run: npm run models (or re-run npm run dev)' });
  }

  // Fonts (Windows only).
  for (const [abs, label] of AE_FONTS) {
    rows.push({ ok: existsSync(abs), label, fix: 'reinstall Microsoft fonts (they ship with Windows)' });
  }

  const width = Math.max(...rows.map((r) => r.label.length)) + 2;
  for (const r of rows) {
    const status = r.ok ? 'ready' : paint('red', `MISSING — ${r.fix}`);
    console.log(`   ${r.ok ? paint('green', '✓') : paint('red', '✗')} ${r.label.padEnd(width)} ${status}`);
  }
  const missing = rows.filter((r) => !r.ok);
  if (missing.length) {
    warn(`${missing.length} AI-editor prerequisite(s) missing (✗ above). The app will still start, ` +
         'but the AI Editor will degrade or fail on those features.');
    if (!hasFF) {
      warn('FFmpeg is REQUIRED for the AI Editor. Without it no edited video can be produced.');
    }
  } else {
    ok('All AI-editor prerequisites are ready ✓');
  }
}

// ---------------------------------------------------------------------------
// Helpers to spawn python inside the venv, rooted at ROOT
// ---------------------------------------------------------------------------
function spawnPython(args, opts = {}) {
  return spawn(VENV_PY, args, { cwd: ROOT, shell: false, ...opts });
}

function openBrowser(url) {
  const start = isWin ? (process.platform === 'win32' ? 'start' : '') : '';
  if (isWin) {
    import('node:child_process').then(({ spawn }) => {
      const p = spawn('cmd', ['/c', `start "" "${url}"`], { detached: true, stdio: 'ignore', cwd: ROOT });
      p.on('error', () => {});
      p.unref();
    });
  } else if (process.env.DISPLAY) {
    const opener = process.platform === 'darwin' ? 'open' : 'xdg-open';
    const p = spawn(opener, [url], { detached: true, stdio: 'ignore', cwd: ROOT });
    p.on('error', () => {});
    p.unref();
  }
}

// ---------------------------------------------------------------------------
// Core processes
// ---------------------------------------------------------------------------
function runWorkerForeground() {
  step(5, 'Huey worker');
  const worker = spawnPython(['-m', 'backend.workers.consumer'], { stdio: 'inherit' });
  worker.on('exit', (c) => process.exit(c ?? 0));
}

async function runServerForeground() {
  step(5, 'Huey worker (background)');
  const worker = spawnPython(['-m', 'backend.workers.consumer'], { stdio: 'inherit' });
  worker.on('error', (e) => warn(`Worker failed to start: ${e.message}`));

  const shutdown = (sig) => {
    console.log(`\n${paint('yellow', `Received ${sig} — shutting down...`)}`);
    if (worker) { try { worker.kill('SIGTERM'); } catch {} }
    if (isWin) { try { spawnSync('taskkill', ['/pid', String(worker.pid), '/T', '/F'], { stdio: 'ignore' }); } catch {} }
    setTimeout(() => process.exit(0), 400);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  step(6, `FastAPI server (http://${SERVER_HOST}:${SERVER_PORT})`);
  const server = spawnPython(
    ['-m', 'uvicorn', 'backend.api.app:app', '--host', SERVER_HOST, '--port', SERVER_PORT, '--reload'],
    { stdio: 'inherit' }
  );
  server.on('error', (e) => fail(`Uvicorn failed to start: ${e.message}`));

  // Give it a few seconds, then open the browser once and only.
  let opened = false;
  const tryOpen = setInterval(() => {
    if (opened) { clearInterval(tryOpen); return; }
    fetch(SERVER_URL, { signal: AbortSignal.timeout(2500) })
      .then(() => { opened = true; clearInterval(tryOpen); log(`Browser -> ${SERVER_URL}`); openBrowser(SERVER_URL); })
      .catch(() => {});
  }, 2000);

  const code = await new Promise((res) => server.on('close', res));
  if (worker) { try { worker.kill('SIGTERM'); } catch {} }
  process.exit(code ?? 1);
}

// ---------------------------------------------------------------------------
// Entry
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);
const sub = args[0] || '';

async function main() {
  console.log(`\n${paint('bold', 'AIClipper — AI Video Clipper')}   ${paint('dim', 'one-command full-stack boot')}\n`);
  try {
    switch (sub) {
      case 'worker': return runWorkerForeground();
      case 'server':
        ensureVenv();
        ensureModels();
        await ensureOllama();
        ensureDirs();
        ensureAEEditorReady();
        return await runServerForeground();
      case 'models': ensureVenv(); ensureModels(); return;
      case 'check':
        ensureVenv(); ensureModels(); await ensureOllama(); ensureAEEditorReady(); return;
      default:
        ensureVenv();
        ensureModels();
        await ensureOllama();
        ensureDirs();
        ensureAEEditorReady();
        await runServerForeground();
    }
  } catch (e) {
    console.error(e);
    process.exit(1);
  }
}

main();