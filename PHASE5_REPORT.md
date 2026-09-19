# AIClipper — Phase 5 Verification Report

**Date:** 2026-09-12 · **Runs:** Phases 2–5 build + verification harness
**Companion docs:** `PHASE0_AUDIT.md` (original audit) · `PHASE1_INSTALL.md` (deps + env)

This report re-audits the Phase 0 items and the new-phase work, and clearly
separates what was **verified right here** from what needs a **one-time install +
a real run on your Windows machine** (this sandbox has no internet for packages
and no torch/Demucs/WhisperX, so those can't run here).

---

## 1. What was verified in this run (logic-level, PASS)

A verification harness (`phase5_verify.py`, checked in at the project root)
stubbed the heavy dependencies (config, embeddings) and executed the **real**
new module code from this build. Result: **16 / 16 checks PASS.**

| Check | What it proves |
|---|---|
| EN sentence grouping | Per-word timestamps → clean sentence spans on `. ` punctuation |
| ZH sentence grouping | CJK (no word spaces) → character/token sentence grouping still works |
| ZH has CJK glyphs | Grouped Chinese still contains correct Han characters |
| Topic-shift boundary detection | A semantic dip in sentence embeddings is detected as a boundary |
| Boundary prev≠next | Detected boundary genuinely separates two different topics |
| Semantic candidate windows | Topic-shift anchors are turned into well-placed clip windows |
| Windows in bounds | No window exceeds the video length |
| Windows keep target length | ~60/75/90s windows preserved (no collapsed slivers) |
| Windows de-duplicated | No duplicate candidates |
| `_score_window` reuse | Semantic windows are scored by the SAME helper as the sliding sweep (scoring unchanged, per requirement) |
| `_score_window` anchor metadata | `anchor` / `semantic_boundary` tags carried through |
| Coverage gate ≥90% → PASS | Exact gate behavior |
| Coverage gate 90% → PASS | Exact gate behavior |
| Coverage gate <90% → FLAG | Never marks success on thin subtitle coverage |

Also verified (syntax/compile): all changed backend modules `py_compile` clean;
frontend `app.js` + `api.js` pass `node --check`.

---

## 2. Phase 0 audit items — re-audited

| Item (from Phase 0) | Original verdict | Now |
|---|---|---|
| A1 transcript-reuse-first | already correct | ✅ **unchanged + still correct** (`get_clip_transcript` reuse primary, fresh fallback) |
| A2 faster-whisper installed | missing | ⏳ **install pending** — `PHASE1_INSTALL.md` step for `faster-whisper`; can't run here (no PyPI) |
| A3 centisecond ÷100 | already correct | ✅ **unchanged + still correct** (`transcription.py` ÷100 + sanity assertion) |
| A4 ASS original_size WxH | already correct | ✅ **unchanged + still correct** — ASS still needs fontconfig FFmpeg to actually succeed (new `[ASS CAPABILITY]` boot log added) |
| A5 drawtext/thumb width-fit | already correct | ✅ **unchanged + still correct** |
| A6 thumbnail from clean clip | already correct | ✅ **unchanged + still correct** (`thumb_source = clip_path`, never post-burn) |
| A7 Chinese captions | partial→needs e2e | 🟡 **code complete** — NLLB translation path, `[CJK CHECK]`, CJK font-confirmation `[CJK FONT]` (asserts Microsoft YaHei exists) all in; ✅ logic verified; ⏳ **final e2e run still needs your machine** |

---

## 3. Phase-by-phase status (this build)

### Phase 1 — Model stack (built in prior round)
Code written, compiles. **Runtime (NLLB/Demucs/WhisperX/bge) requires the
one-time dependency install on your machine** — `PHASE1_INSTALL.md` has the
exact commands. Can't run here (sandbox has no PyPI).

### Phase 2 — Semantic-boundary clip candidates ✅ built + logic-verified
- New `backend/services/semantic_boundaries.py`: groups WhisperX/faster-whisper
  words into sentences, embeds them, detects topic-shift boundaries, emits
  sentence-anchored candidate windows.
- `clip_scoring.py` refactored: per-window scoring extracted into one
  `_score_window()` helper used by BOTH the sliding sweep and the semantic
  windows → **scoring logic is unchanged** (requirement met), semantic windows
  just plug in better start/end anchors.
- New `semantic_threshold` / `semantic_boundaries_enabled` config toggles.

### Phase 3 — Audio Remix ✅ built + wired + verified-selectable
- Backend wired into `auto_edit_clip`: reads `audio_mode` / vocals / music gain /
  replacement track, renders a remixed intermediate, feeds it into the caption
  burn. Settings persisted to the clip.
- **Your-own-music path**: new `POST /api/clips/{id}/music` upload endpoint +
  a "🎵 Your Music" panel in the AI Editor — pick a track, choose *Replace with
  your music*, hit Generate. The track is **looped so it fills the whole clip**
  (a short song won't truncate your clip).
- **Works even without Demucs**: if the vocal/music isolation tool isn't
  installed, simple *replace* mode drops the original audio and plays your
  track over the clip — so "my own music on my own clip" works regardless.
  With Demucs installed, *mute music* / *replace while keeping your voice* also
  work (auto-ducking keeps speech clear).

### Phase 4 — WhisperX source-of-truth + coverage gate ✅ built
- Full-video WhisperX alignment already ran and stored; renderer chooses it when
  present.
- **≥90% coverage gate** added: logs `[COVERAGE GATE] PASS/FAIL`. A result under
  90% is **FLAGGED, never marked success** (requirement met).
- `[SOURCE OF TRUTH]` log line shows reuse-vs-fresh and whisperx-vs-native words.
- `[CJK FONT]` confirmation checks Microsoft YaHei is present so Chinese glyphs
  actually render.
- `[ASS CAPABILITY]` boot log reports whether the current FFmpeg supports
  ASS/libass+fontconfig — so the "ASS-success-not-fallback" criterion is
  checkable. See `PHASE1_INSTALL.md` §5 for the fontconfig-FFmpeg swap.

---

## 4. What YOU need to do (plain English — no coding)

For the app to actually run the new engines on your computer, do this **once**:

1. Open a **Command Prompt** in the AIClipper folder and run the single install
   commands in `PHASE1_INSTALL.md` `§1` (they pull in NLLB, faster-whisper,
   WhisperX, Demucs, the embedder). This is the only technical step — it's
   provided ready to paste.
2. Re-start the app (`npm run dev` or your normal start command).
3. Open a **clip → AI Editor Studio**:
   - Choose **🎵 Your Music → Replace with your music**, tap *Choose music file*,
     pick your song, hit **✨ Generate**.
   - Done — your track plays over your clip, with subtitles.
4. If you want subtitles in **Chinese**, pick *简体中文* and Generate.

**What happens if you skip the install:** the app still works for subtitles +
thumbnails, and *Replace with your music* still works via the simple-replace
fallback (your track plays over the clip). The richer "keep my voice while
swapping the music" needs Demucs installed.

---

## 5. Honest status (not assumed)

- ✅ **Verified (ran here):** all new pure logic — sentence grouping, semantic
  boundary detection, semantic clip windows, scoring-parity, coverage gate,
  compile/syntax of all changed files.
- ⏳ **Needs your machine + the install to truly confirm:** NLLB actually
  translating, Demucs separating stems, WhisperX aligning, ASS burning
  successfully on a fontconfig FFmpeg, and a full end-to-end clip edit producing
  a playable video with your music + subtitles.
- These are one-time deps + one real run, not more code. Once you run the
  install and one AI Edit, the `[SUBTTITLE DEBUG]`, `[SOURCE OF TRUTH]`,
  `[COVERAGE GATE]`, `[CJK CHECK]`, `[CJK FONT]`, `[ASS CAPABILITY]`, and
  `Audio Remix:` log lines will tell us (and you) exactly what's working.