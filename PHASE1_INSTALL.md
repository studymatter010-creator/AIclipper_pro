# AIClipper — PHASE 1 Install & Environment

**Applies on the Windows machine that runs AIClipper** (the sandbox this was
built in has no PyPI access). Run these from the `AIClipper` project root in the
**same Python env that runs the backend** (`uvicorn`).

## 1. Install the Phase 1 model-stack dependencies

```bash
# NLLB-200 translator (CTranslate2 engine)
pip install --break-system-packages ctranslate2 sentencepiece

# WhisperX forced alignment (wav2vec2) — pulls torch
pip install --break-system-packages "whisperx[align]" torch torchaudio

# Demucs stem separation (Audio Remix) + its torch backend
pip install --break-system-packages "demucs[htdemucs]" torch torchaudio

# bge-small-en sentence embedder (Phase 2 semantic boundaries)
pip install --break-system-packages sentence-transformers

# faster-whisper (ASR primary) — already in requirements.txt, ensure present
pip install --break-system-packages faster-whisper

# Confirm everything resolves in one import:
pip install --break-system-packages -r requirements.txt
```

> On Windows, `--break-system-packages` may be unnecessary; drop it if your env
> is a plain venv. If torch via pip is slow on your machine, install the CPU
> wheel bundle for your Python version first, then `pip install whisperx demucs
> sentence-transformers`.

## 2. First-run model downloads (cached under `~/.cache`)
These download on first use (a few GB total), then load locally:
- `facebook/nllb-200-distilled-600M` → CTranslate2 auto-converts in `~/.cache/ctranslate2`
- `facebook/wav2vec2-base-960h` → WhisperX aligner
- `distil-large-v3` → faster-whisper (CTranslate2)
- `BAAI/bge-small-en-v1.5` → sentence-transformers
- `htdemucs` → Demucs

**NLLB sentencepiece model:** the CTranslate2 conversion does NOT always carry
the `.spm` file. If translation logs show "NLLB-200 unavailable", download
`sentencepiece.bpe.model` from the HF repo and set:
```
.ini
NLLB_SENTENCEPIECE_MODEL=C:\path\to\sentencepiece.bpe.model
```

## 3. .env toggles that matter
```ini
# ASR — distil-large-v3 is the Phase 1 default; drop to "small" if 16GB OOMs
WHISPER_MODEL=distil-large-v3
# WhisperX on (source-of-truth word timestamps / Phase 4)
WHISPERX_ALIGN=true
# NLLB batching / determinism
NLLB_BEAM=1
NLLB_COMPUTE=int8
# Audio Remix mode default (keep_original | mute_music | replace_music)
# set per-clip at edit time via the API; config here is the fallback.
```

## 4. Feature wiring map (what depends on what)
| Feature | Requires installed | Rolls back to |
|---|---|---|
| NLLB translate | ctranslate2 + sentencepiece | Ollama translate specialist (model_team) |
| faster-whisper ASR | faster-whisper | pywhispercpp / whisper.cpp |
| WhisperX align | whisperx + torch | faster-whisper native word timestamps |
| Demucs stems | demucs + torch | gain-only FFmpeg copy |
| bge embeddings | sentence-transformers | (empty result → heuristic boundaries) |

## 5. Phase 4/5 prerequisite — fontconfig-enabled FFmpeg (decision #3)

ASS burn **cannot succeed** on the current FFmpeg (`libass` built without
fontconfig), so the caption path always falls back to drawtext. To make the
"ASS-success-not-fallback" criterion verifiable we need an FFmpeg with the `ass`
filter compiled against libass *with fontconfig*.

- **Recommended (Windows):** grab a Gyan.dev or BtbN build that ships an
  `ass`/libass+fontconfig, point `FFMPEG_PATH` at its `ffmpeg.exe`, and confirm
  `ffmpeg -filters | findstr ass` lists `--enable-libass --enable-fontconfig`.
- Keep the existing FFmpeg as fallback: the drawtext path stays the reliable
  renderer; this swap only lets ASSERT succeed when the new binary is present.
- A helper boot check (`_smoke_test_drawtext` already exists) can be extended to
  log `ass_supported=true/false` at startup.

## Verification (Phase 5 will re-run this)
1. `[SUBTITLE DEBUG]` logs show `engine=...` (nllb / whispher-or reused).
2. `[CJK CHECK] has_cjk=True` for a zh edit.
3. `NLLB translated N/N segments ... (engine=nllb)` in the log.
4. `Demucs separated stems ... /stems/{vocals,music}.wav`.
5. `Audio Remix: <mode> -> <output>`.
6. Coverage ≥90% after WhisperX source-of-truth (Phase 4) — flags, not "success".