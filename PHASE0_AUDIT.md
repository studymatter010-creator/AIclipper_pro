# AIClipper — PHASE 0 AUDIT (Audit Before Building)

**Date:** 2026-09-11
**Scope:** Current codebase vs. every requirement in the PHASE 0–5 build directive.
**Method:** On-disk inspection of the live code (not memory) + a sandbox `pip` check of the Python runtime.

Every item is labeled **already correct / partially implemented / missing / broken-and-needs-fixing**, with the specific `file:function` and current verified status.

---

## Part A — The 7 confirmed bug items (from prior sessions)

### A1. Transcript reuse wired as the primary path, before fresh transcription
**Status: ALREADY CORRECT**
- `backend/services/auto_editor.py:auto_edit_clip` (L1622–1652): transcript reuse via `get_clip_transcript(clip, transcript)` runs FIRST. Only when it returns nothing usable does the code fall back to fresh `transcribe_video(clip_path)` (L1656-1670).
- Verified on disk in the latest batch: the fallback is correctly `if not _fresh_segs and not _fresh_words`.

### A2. faster-whisper actually installed
**Status: MISSING (install needed — Phase 1)**
- Sandbox `pip show`: `faster-whisper` → **Package(s) not found**.
- Same for `whisperx`, `demucs`, `ctranslate2`, `mediapipe`, `sentence-transformers` — all absent.
- Code already *expects* faster-whisper as the primary ASR engine and warns loudly at startup when it's missing (`backend/app.py` lifespan, boxed warning) and exposes `team_status() → faster_whisper_ready`.
- ⚠️ Note: the sandbox is a *separate* Python env from the user's Windows runtime. The on-disk boxed warning in a prior startup log confirms it is absent on the user's machine too. Phase 1 must actually install it in the runtime that runs the app.

### A3. pywhispercpp centisecond conversion (÷100, not ÷1000)
**Status: ALREADY CORRECT**
- `backend/services/transcription.py:_transcribe_with_pywhispercpp` L334–353: `start_s = seg.t0 / 100.0`, `end_s = seg.t1 / 100.0`; word interpolation uses `seg.t0 * 10` / `seg.t1 * 10`. Comment documents centisecond→second.
- Sanity check (L355–383) fails loudly if last-segment-end / audio-duration ratio <0.2 (compressed) or >5.0 (expanded) — catches regression automatically.

### A4. ASS `original_size` receives WIDTHxHEIGHT, not a file path
**Status: ALREADY CORRECT**
- `backend/services/auto_editor.py:_attempt_ass_burn` L979–980: `sub_str` single-quoted ASS temp path; `capt_vf = f"ass={sub_str}:original_size={output_width}x{output_height}"`. The value is `WxH`, not the temp path.
- `_classify_ass_failure` L951–956 checks `original_size`/`unable to parse` BEFORE the fontconfig check (avoids the false positive from FFmpeg's always-present fontconfig mention).
- ⚠️ Caveat for Phase 4: on this Windows FFmpeg build, **ASS still fails** (`libass built without fontconfig`). This is a known, expected limitation with a working drawtext fallback — Phase 4's "ASS-success-not-fallback" criterion cannot be met on this build and should instead verify the *drawtext* fallback succeeds and renders visibly (or add a fontconfig-enabled FFmpeg path).

### A5. Drawtext / thumbnail text width measurement
**Status: ALREADY CORRECT**
- `backend/services/auto_editor.py:_thumbnail_lines` (L1224) truncates headline to a character budget; `generate_thumbnail` (L1317+) then shrinks/wraps as a final guarantee.
- `_build_drawtext_caption_vf` caps at 80 cues; balanced headline wrap in `_wrap_text` (SEVENTH fix) picks the split minimizing `abs(width_line1 - width_line2)`.
- Prior Sessions: `TestTextWidthFitting` unit tests exist and pass (25/25).

### A6. Thumbnail extracted from the CLEAN original clip, not the post-burn edit
**Status: ALREADY CORRECT**
- `backend/services/auto_editor.py:auto_edit_clip` Step 5 (L1890): `thumb_source = clip_path if clip_path.exists() else final_path`, with the comment explaining why extracting from the post-subtitle-burn frame bakes subtitle glyphs into the thumb. Verified on disk.

### A7. Chinese captions — audited SEPARATELY from English
**Status: PARTIALLY IMPLEMENTED → still needs end-to-end confirmation**

- **Translate step:** ALREADY CORRECT — `backend/services/subtitles.py:translate_segments_to_language` now validates CJK output (`_has_cjk`); returns original segments (fail-fast) if a `zh`/`ja` translation contains zero CJK chars. `[CJK CHECK]` diagnostic in `auto_edit_clip` prints orig/translated sample + `has_cjk`. Verified working in prior batch.
- **UTF-8 / Windows subprocess handling:** ALREADY CORRECT (as far as the caption burn path): ASS temp path is single-quoted (L979) to survive the Windows drive-colon; `_drawtext_escape` escapes colons as `C\:/...`. This was the earlier root cause of a garbled thumbnail and is fixed.
- **CJK font switch:** ALREADY CORRECT — `_cjk_font_for` → `C:/Windows/Fonts/msyh.ttc`; `is_cjk=is_chinese` passed to `generate_ass_professional`; `use_segments_for_cjk=True` forced for Chinese.
- **Coverage collapse (the actual "Chinese shows English / nothing" bug):** FIXED in the NINTH batch — `_sanitize_segments` short-drop threshold 0.3s→**0.08s** + new `_merge_adjacent_segments()` (gap≤0.2s, line≤3.0s, ≤120 chars). Result: 75 fragmented reused-transcript cues → 17 readable lines / 55% coverage (was 2 segs / 7%). **Shared by English and Chinese.**
- **⚠️ Remaining gap:** this merge fix was validated in-bash against real DB data but **has NOT yet been re-run end-to-end through the app**. The user must re-run a Chinese edit and confirm `[CJK CHECK] has_cjk=True` + coverage ≥ meaningful %.

---

## Part B — Phase-by-phase readiness

### PHASE 1 — Model stack replacement
| Requirement | Status | Where |
|---|---|---|
| Keep qwen3:8b (brain) | ALREADY CORRECT | `model_team.py` `resolve()` |
| Keep qwen2.5:3b (classify/hook) | ALREADY CORRECT | `model_team.py` `resolve()` |
| Keep MediaPipe | **MISSING** (dep not installed) | — |
| Replace translate ro→ NLLB-200-distilled-600M via CTranslate2 | **MISSING** | current translate = Ollama qwen2.5:3b (`subtitles.py:_translate_with_ollama`) |
| ASR → faster-whisper distil-large-v3 / large-v3-turbo | **MISSING** (pkg not installed; code falls back to pywhispercpp) | `transcription.py` |
| WhisperX wav2vec2 forced alignment | **MISSING** (pkg not installed) | — |
| Demucs htdemucs | **MISSING** (pkg not installed) | — |
| bge-small-en-v1.5 | **MISSING** (pkg not installed) | — |
| Generalize RAM sequencer to non-Ollama heavy processes | **MISSING** — `acquire()/chat()` are Ollama-HTTP-specific | `model_team.py` |

### PHASE 2 — Semantic-boundary clip candidates
| Requirement | Status | Where |
|---|---|---|
| Keep existing frame-level passage analysis | PARTIALLY IMPLEMENTED (exists + scoring; needs the new semantic stage) | `clipper.py` passage analysis |
| WhisperX segments + bge-small-en sentence embeddings → candidate anchors | **MISSING** | — |
| Snap to sentence boundary + silence gap | **MISSING** | — |
| Scoring logic UNCHANGED | ALREADY CORRECT (do not touch passage-urgency weighted-sum) | `clipper.py` scoring |

### PHASE 3 — Audio Remix (Demucs)
| Requirement | Status | Where |
|---|---|---|
| Demucs stems (cache per clip) | **MISSING** (pkg not installed) | — |
| Modes keep_original / mute_music / replace_music | **MISSING** | — |
| Local royalty-free library only (no copyrighted tracks) | **MISSING** | — |
| Auto-ducking (sidechaincompress) | **MISSING** | — |
| Persist vocals_gain / music_gain / replacement_track | **MISSING** (DB schema has no such columns) | `db/models.py` |

### PHASE 4 — WhisperX subtitles as source-of-truth
| Requirement | Status | Where |
|---|---|---|
| WhisperX once on full source → single JSON transcript (seg+words) written to DB | **MISSING** | `db/models.py`, `transcription.py` |
| ≥90% coverage gate (flag, never "success") | **MISSING** (merge fix gives ~55%; no gate) | `auto_editor.py` |
| Renderer auto-selects WhisperX if present | **MISSING** | `auto_editor.py` caption sourcing |
| Chinese/translated: reuse, coverage, debug logging, CJK font-confirmation | PARTIALLY IMPLEMENTED (reuse+[CJK CHECK]+merge exist; ≥90% gate + font-confirmation log missing) | `auto_editor.py` |

### PHASE 5 — Verification
- **NOT STARTED** — required to actually run (logs: reuse/alignment/Demucs/mode/ASS-vs-fallback/coverage) and re-report the audit with every broken item fixed-and-verified.

---

## Summary verdict table

| Item | Status |
|---|---|
| A1 reuse-first | already correct |
| A2 faster-whisper installed | **missing** |
| A3 centisecond ÷100 | already correct |
| A4 ASS original_size WxH | already correct (ASS still fails on fontconfig → drawtext fallback) |
| A5 drawtext/thumb width | already correct |
| A6 thumb from clean clip | already correct |
| A7 Chinese captions | partially implemented (translate+CJK correct; merge-coverage fixed; needs end-to-end re-run) |
| P1 model stack | **mostly missing** |
| P2 semantic clips | **missing** |
| P3 Audio Remix | **missing** |
| P4 WhisperX source-of-truth | **missing** |
| P5 verification | **not started** |

**Conclusion:** The caption-pipeline correctness items (A1, A3, A4, A5, A6, A7 core) are in good shape. The bulk of the directive — P1 (install + swap the heavy models), P2 (semantic boundaries), P3 (Audio Remix), P4 (WhisperX-as-truth + coverage gate), and P5 (verification) — is **missing and requires new build work**, plus one end-to-end confirmation of the Chinese path.

This audit is the Phase 0 deliverable. Phase 1 does not begin until this is confirmed.