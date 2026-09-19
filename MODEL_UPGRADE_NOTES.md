# Model Upgrade Evaluation (Task 5) — 2026-09-09

These are the three *optional* upgrades from the regression-cleanup plan. Each was
evaluated against the standing constraints: **16 GB RAM** (qwen3:8b resident),
**don't break working pipelines**, and **Whisper must stay small** (medium OOM'd).

## Verdict at a glance

| Upgrade | Adopted? | Why |
|---|---|---|
| WhisperX forced alignment | ✅ yes (opted-in, OFF by default) | Fixes word-audio desync cheaply |
| NLLB-200 for translation | ❌ no | RAM-heavy, marginal vs qwen2.5:3b |
| large-v3-turbo Whisper | ⚠️ optional toggle, not default | Faster+more accurate but heavier |
| Real-ESRGAN thumbnail | ❌ no | torch dependency for a minor sharpness gain |

---

## 1. WhisperX forced alignment — ADOPTED

Implemented in `backend/services/transcription.py::_try_whisperx_align`.

- Loads **only** the wav2vec2 phoneme aligner (`load_align_model`), NOT a second
  Whisper ASR, so memory overhead is just the aligner (~0.4 GB base, ~1.3 GB large).
- Feeds the existing faster-whisper **segments** to `whisperx.align()` and swaps in
  the aligned word timestamps — the standard fix for word-audio desync in karaoke.
- **OFF by default** (`WHISPERX_ALIGN` env / `whisperx_align` setting) to protect RAM.
- Any failure (missing package, download error, OOM) → safe fallback to faster-whisper's
  native word timestamps. Transcription never breaks because whisperx is absent.

Enable: set `WHISPERX_ALIGN=true` (any non-empty value). `whisperx_align_model`
defaults to `facebook/wav2vec2-base-960h` (RAM-frugal). Install:
`pip install "whisperx" torch`.

---

## 2. NLLB-200 for translation — NOT ADOPTED

- Current zh translation uses the Ollama **translate specialist** (qwen2.5:3b), which is
  good, cached by transcript hash, and routes through the RAM sequencer (<9 GB budget).
- NLLB-200-distilled-600M is ~1.2 GB and NLLB-200 is ~2.3 GB on disk, plus its runtime
  footprint — meaningful pressure on the 16 GB budget already hosting qwen3:8b.
- The existing translation is adequate for on-screen subtitles; swapping engines adds
  install weight and RAM risk for a marginal quality win. **Not worth it here.**

---

## 3. large-v3-turbo Whisper — BENCHMARKABLE, NOT DEFAULT

- faster-whisper already accepts `large-v3-turbo` as a model *name*. It's faster and more
  accurate than `small`, ideal for noisy short-form audio.
- Cost: ~1.6 GB int8 at load — heavy on the 16 GB budget alongside qwen3:8b. Transcription
  brief periods could spike RAM.
- Already try-able with zero code change: set `whisper_model=large-v3-turbo` in `.env`.
  Benchmark beam/vad; if it holds RAM, promote to default. Until then `small` stays default.

---

## 4. Real-ESRGAN thumbnail sharpening — NOT ADOPTED

- Thumbnails are already FFmpeg-sharpened (`unsharp=5:5:0.7`) and graded.
- Real-ESRGAN pulls a torch + model dependency for a subjective sharpness gain on a
  1080×1920 still. Adds install weight and latency for no functional benefit.
- **Tracked as a future option** if the user wants maximal thumbnail pop.

---

## What this means for the running system

The default path is **unchanged and safe**: faster-whisper `small` → (optionally)
WhisperX-aligned words → ASS/drawtext captions → FFmpeg thumbnail. All upgrades are
opt-in toggles, so nothing regresses by default.