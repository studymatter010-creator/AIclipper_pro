# AIClipper — Master System Prompt

## What This App Does

AIClipper is a fully local, AI-powered video clipping platform that takes long-form videos (YouTube links or uploaded files) and automatically produces viral-ready YouTube Shorts (60–90 seconds, 1080×1920 vertical). It does this without any cloud AI services — everything runs on the user's machine via local models. The core loop is: upload a video → the system transcribes, analyzes, scores, and ranks every possible clip → generates the top clips in vertical format with burned-in subtitles and thumbnail images → presents them in a visual grid where the user can preview, edit subtitles, translate to Chinese, download, and publish to YouTube.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        npm run dev                              │
│  (scripts/dev.mjs — boots everything with one command)          │
├──────────────┬──────────────────┬───────────────────────────────┤
│  Python venv │  Ollama server   │  Huey background worker      │
│  (uvicorn)   │  (port 11434)   │  (processes videos async)    │
│  port 8000   │                  │                               │
├──────────────┴──────────────────┴───────────────────────────────┤
│                    FastAPI Backend                               │
│  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐           │
│  │ /videos  │ │ /clips   │ │ /editor │ │ /settings│           │
│  └──────────┘ └──────────┘ └─────────┘ └──────────┘           │
│  ┌──────────────────────────────────────────────────┐          │
│  │           SQLite (data/aiclipper.db)              │          │
│  └──────────────────────────────────────────────────┘          │
├─────────────────────────────────────────────────────────────────┤
│                    Service Layer                                │
│  transcription.py → content_classifier.py → ai_brain.py        │
│  → clip_generator.py → subtitles.py → auto_editor.py           │
│  All coordinated by model_team.py (RAM-aware Ollama sequencer) │
│  Inference cache: backend/utils/cache.py (JSON, TTL, hash-keyed)│
├─────────────────────────────────────────────────────────────────┤
│                    External Tools                               │
│  FFmpeg/FFprobe  •  faster-whisper (CTranslate2)  •  Ollama   │
├─────────────────────────────────────────────────────────────────┤
│               Vanilla JS Frontend (served as static)            │
│  app.js • components.js • api.js • styles.css • index.html     │
└─────────────────────────────────────────────────────────────────┘
```

---

## All Technologies Used

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Language | Python 3.12 | Backend, all AI/ML pipelines |
| Framework | FastAPI + uvicorn | REST API server (port 8000) |
| Database | SQLite + aiosqlite + SQLAlchemy ORM | Persistent storage for videos, clips, edits, settings |
| Background worker | Huey (with Redis or in-memory) | Async video processing jobs |
| AI models | Ollama (local LLM server) | Runs qwen3:8b (brain) and qwen2.5:3b (classify/hook/translate) |
| Speech recognition | faster-whisper (CTranslate2, primary) + pywhispercpp (fallback) | Transcribes audio with VAD, word timestamps, per-segment confidence |
| Video processing | FFmpeg + FFprobe | Cutting, re-encoding, vertical conversion, subtitle burning, audio extraction |
| Face detection | MediaPipe BlazeFace | Identifies faces in frames for face-aware cropping |
| Frontend | Vanilla JavaScript (no framework) | Single-page app served as static files |
| Dev tooling | Node.js (scripts/dev.mjs) | One-command full-stack boot: venv, models, Ollama, worker, server |
| Config | Pydantic BaseSettings + .env + YAML | Hierarchical config with .env having highest precedence |
| Inference cache | JSON file + TTL + transcript-hash keys | Caches classify/brain outputs to avoid re-running LLM inference |

---

## The Model TEAM — How Local AI Cooperates

The app runs a "team" of specialized Ollama models that take turns, managed by a RAM-aware sequencer (`model_team.py`) designed for a 16GB machine:

| Role | Model | RAM | Purpose |
|------|-------|-----|---------|
| **brain** | qwen3:8b | ~5400 MB | Deep semantic analysis — rates each candidate clip for viral potential on 8 criteria (hook moments, emotional peaks, opinion bombs, revelation moments, conflict, quotable lines, story peaks, practical value). Heavy reasoning, NOT kept resident during fast specialist work. |
| **classify** | qwen2.5:3b | ~2600 MB | Content classification — reads first 25 transcript segments and outputs JSON classifying the video into one of 8 content types (podcast, interview, tutorial, lecture, vlog, commentary, debate, other) and 3 density levels (low/medium/high). |
| **hook** | qwen2.5:3b | ~2600 MB | Hook generation — produces a single viral opening line (max 40 chars, no quotes/hashtags) for the top 3 clips. |
| **translate** | qwen2.5:3b | ~2600 MB | Translation — translates subtitle segments to Chinese (or other languages) while preserving timing. |
| **Whisper** | faster-whisper (CTranslate2) | N/A | Speech-to-text with Silero VAD gating, native word-level timestamps, per-segment avg_logprob/no_speech_prob confidence. Model name: `small`. Falls back to pywhispercpp if faster-whisper unavailable. |

**Structured output:** All classify and brain calls use Ollama's native `format: "json"` mode via `model_team.chat_json()`, constraining the model to emit valid JSON. Each call gets a single automatic retry with 1.5s backoff before falling back to heuristics.

**RAM Sequencer logic:** The `acquire(role)` function marks a model as in-use, then iterates all loaded models and unloads (via Ollama `keep_alive=0`) the least-recently-used ones until estimated resident memory fits within the 9000 MB budget. The model being acquired is never evicted. This means: when the brain (5.4 GB) finishes scoring, it gets evicted to make room for the translate specialist (2.6 GB) to do subtitle translation. Models load on-demand and unload automatically.

---

## Complete Data Flow — Upload to Download

### Phase 1: Upload & Metadata Extraction
1. User uploads a video file (or pastes a YouTube URL — yt-dlp downloads it).
2. FFprobe extracts metadata: duration, resolution, fps, codec, audio codec, bitrate, filesize.
3. A `Video` record is created in SQLite with status `PENDING`.

### Phase 2: Background Processing (Huey worker picks up the job)
Status changes to `PROCESSING`. The pipeline runs sequentially:

**Step 1 — Audio Extraction**
- FFmpeg extracts a 16kHz mono WAV audio file from the video (`ffmpeg.extract_audio`).
- Optimized for Whisper's expected input format.

**Step 2 — Transcription (faster-whisper primary, pywhispercpp fallback)**
- `transcribe_video()` first attempts `faster-whisper` (CTranslate2) with `vad_filter=True` (Silero VAD silence-gating), `word_timestamps=True`, `condition_on_previous_text=False`. Returns per-segment `avg_logprob` and `no_speech_prob` for downstream confidence filtering.
- If faster-whisper is unavailable, falls back to `pywhispercpp` (confidence fields set to `None`).
- Full transcript stored as JSON in `transcripts` table, including word-level timestamps and engine identifier.

**Step 3 — Content Classification (qwen2.5:3b via classify role)**
- First 25 transcript segments (capped at 3000 chars) sent to the classify specialist via `model_team.chat_json()` with `format="json"` and a single automatic retry.
- Returns JSON: `{content_type: "podcast", density: "high"}`.
- This determines genre-aware scoring weights (e.g., podcasts emphasize emotion and dialogue; tutorials emphasize dialogue and face presence).
- **Inference cache:** Results are cached keyed on transcript hash (`classify:{hash}`). Subsequent calls for the same video skip LLM inference entirely.

**Step 4 — Scene Detection**
- FFmpeg-based scene change detection identifies visual transitions.
- Each scene gets a start_time, end_time, and duration.

**Step 5 — Audio Analysis**
- RMS energy analysis identifies moments of high/low audio intensity.
- Peaks suggest exciting moments; valleys suggest transitions.

**Step 6 — Face Tracking (MediaPipe BlazeFace)**
- Samples frames (every 3rd frame by default) and detects faces.
- Returns bounding boxes with timestamps for face-aware cropping.
- Stored as crop_data_json on the Video record.

**Step 7 — Heuristic Clip Scoring**
- Candidate clips are generated from overlapping time windows (60, 75, or 90 seconds, configurable).
- Each candidate scored on 5 signals with genre-adjusted weights:
  - `emotion` (audio energy peaks)
  - `dialogue` (transcript density within the window)
  - `scene_change` (visual transitions = interest)
  - `audio` (overall audio quality/energy)
  - `face` (face presence and size)
- Base weights: emotion=0.25, dialogue=0.20, scene_change=0.20, audio=0.20, face=0.15 (auto-normalized to sum to 1.0).

**Step 8 — AI Brain Refinement (qwen3:8b)**
- Top candidates sent to the brain model via `model_team.chat_json()` with `format="json"` and a single automatic retry.
- Brain rates each on 8 virality criteria (0–10 scale).
- Brain score blended with heuristic: `final = heuristic × 0.55 + brain × 0.45`.
- For long videos (>30 min), transcript is chunked into 20-minute overlapping segments to fit context.
- Top 3 clips get refined hook sentences from the hook specialist.
- **Inference cache:** Brain ratings cached keyed on transcript hash + content type (`brain:{hash}:{type}:{chunked|full}`). Re-processing the same video skips 8B inference.

**Step 9 — Clip Generation (FFmpeg)**
- Each selected clip cut from the source video.
- Two-step process: (1) raw cut with stream copy (fast), (2) format conversion to 1080×1920 vertical.
- Vertical conversion uses "blurred background" layout: source scaled up + blurred as background, original scaled down centered on top.
- If face tracking data available and face_crop_enabled, uses face-following crop instead.
- Output: `clip_{video_id}_{number}.mp4` in the outputs directory.

**Step 10 — Subtitle Generation**
- Transcript segments filtered to the clip's time window, rebased to start at 0.
- Multiple formats generated: SRT (plain text), VTT (web), ASS (professional with karaoke word highlighting).
- ASS format includes: word-by-word smooth fill animation, pop-in bounce effect, accent color highlight, CJK auto-detection (switches to Microsoft YaHei font).
- Subtitles stored in `subtitles` table linked to the clip.

**Step 11 — Metadata Generation**
- Title, description, hashtags, keywords generated from transcript and brain analysis.
- Hook sentence and virality reason stored for each clip.

**Step 12 — Thumbnail Extraction**
- FFmpeg extracts a representative frame from each clip.
- Thumbnail scored and stored in `thumbnails` table.

### Phase 3: AI Editor (One-Click Post-Processing)
When the user clicks "✨ AI Edit" on a clip, `auto_edit_clip` in `auto_editor.py` runs:

1. **Reuse stored transcript** — `get_clip_transcript(clip, transcript)` slices the full-video transcript to the clip's time window `[clip.start_time, clip.end_time]` and rebases timestamps to 0-based clip-relative. This is the SAME source Path A uses during clip generation (captured with full-video context, so accuracy is higher). Log line: `"Reusing stored transcript for clip {id}... (NO Whisper re-transcription)"`.
   - **Fallback:** If no stored transcript covers the clip's time range, falls back to fresh `transcribe_video(clip_path, language="auto")`.

2. **Confidence-based sanitization** — `_sanitize_segments()` filters out:
   - Whisper hallucinations ("Thank you for watching", "subscribe", filler words)
   - Segments shorter than 0.3 seconds
   - Timestamps outside the clip duration
   - Consecutive duplicate text
   - Segments with end ≤ start
   - **NEW:** Segments with `no_speech_prob > 0.60` (configurable via `WHISPER_NO_SPEECH_PROB_MAX`)
   - **NEW:** Segments with `avg_logprob < -1.0` (configurable via `WHISPER_AVG_LOGPROB_MIN`)

3. **Optional Chinese translation** — If language is "zh", translates each rebased segment via the translate specialist. Translation is sanitized again after completion.

4. **Generate ASS subtitle file** — Uses `generate_ass_professional()` with word-level karaoke highlighting.

5. **Burn captions into video** (FFmpeg) — Attempts ASS filter first. If that fails (known issue on Windows FFmpeg), falls back to drawtext filter (reliable on all platforms). If both fail, copies the original video unchanged.

6. **Generate thumbnail** — Extracts a frame and saves as JPEG.

7. **Save to database** — Creates a `ClipEdit` row with:
   - Unique per-language files: `edited_clip_{id}_{en|zh}.mp4` + `edited_thumb_{id}_{en|zh}.jpg`
   - Label: "Edit 1 - English" or "Edit 2 - 中文"
   - The ORIGINAL `clips.output_path` is NEVER overwritten.

8. **Return** edit metadata to the client for immediate UI update.

---

## Database Schema (11 Models)

| Model | Table | Purpose |
|-------|-------|---------|
| User | users | User accounts (username, email) |
| Project | projects | Project containers (name, status) |
| Video | videos | Uploaded/imported videos with full metadata + processing state |
| Transcript | transcripts | Full transcript (segments JSON, word timestamps JSON, plain text) |
| Scene | scenes | Detected scene boundaries (start, end, score) |
| Clip | clips | Generated clips with scores, hook sentences, output paths. Original path is IMMUTABLE. Has start_time/end_time (absolute offsets into source video). |
| Subtitle | subtitles | SRT/VTT/ASS files linked to clips |
| ClipEdit | clip_edits | Per-language edits (label, language, output_path, thumbnail_path). Keeps original intact. |
| Thumbnail | thumbnails | Extracted thumbnail images with scores |
| Upload | uploads | YouTube/Facebook publish records |
| VoiceOver | voiceovers | Generated voiceover tracks (edge-tts) |
| Setting | settings | User key-value settings |

**Key design rule:** `clips.output_path` is the ORIGINAL pristine render and is never overwritten by edits. Each edit creates a separate ClipEdit row with its own file paths.

---

## Configuration Hierarchy

Settings load in this order (later overrides earlier):
1. **YAML defaults** (`configs/default.yaml`) — base values
2. **Pydantic model defaults** — hardcoded in `config.py`
3. **`.env` file** — **HIGHEST PRECEDENCE** for user overrides
4. **Environment variables** — override .env if set

Key configuration groups:
- **Application:** host (0.0.0.0), port (8000), debug mode
- **Paths:** 8 directories (upload, output, subtitle, thumbnail, log, model, temp, data)
- **Database:** SQLite at `data/aiclipper.db` with aiosqlite
- **Whisper:** Engine `faster-whisper` (primary), model `small`, 4 threads, auto language detection. Silero VAD filter enabled. Falls back to pywhispercpp.
- **Ollama:** Host localhost:11434, 120s timeout
- **Model Team:** brain=qwen3:8b, classify/hook/translate=qwen2.5:3b, RAM budget 9000 MB. All calls use structured JSON output with 1 automatic retry.
- **Output:** 1080×1920, 30fps, libx264, CRF 21, AAC 192k
- **Clips:** Durations 60/75/90s, max 10 clips, 10s gap between clips
- **Scoring weights:** Auto-normalized to sum to 1.0, genre-adjusted at runtime
- **Confidence thresholds:** `WHISPER_AVG_LOGPROB_MIN=-1.0`, `WHISPER_NO_SPEECH_PROB_MAX=0.60`
- **Inference cache:** `data/inference_cache.json`, TTL-based, keyed on transcript hash

---

## Frontend Architecture

Vanilla JavaScript single-page app with no build step. Served as static files by FastAPI.

- **`index.html`** — Main layout: sidebar (settings + AI Models widget) + main content area (dashboard, videos, clips, publishing tabs)
- **`app.js`** — SPA controller: tab routing, view rendering, API calls, modal management, AI model status polling (5s interval)
- **`components.js`** — Reusable HTML generators: clip cards (VIDEO-FIRST with `<video controls>` and thumbnail poster), version switchers, edit tags, score badges
- **`api.js`** — HTTP client wrapper: all `/api/*` calls, thumbnail URL helper
- **`styles.css`** — Full styling with CSS variables for theming

**Clip card design:** Each clip renders as a playable `<video>` with the thumbnail as poster. Shows an "✨ Edited" badge if edits exist, plus labeled chips for each version (Original / Edit 1 - English / Edit 2 - 中文). Clicking opens a modal with a version switcher that plays the selected version.

**AI Models widget:** Sits in the sidebar footer. Opens by default. Polls `/api/team/status` every 5 seconds. Shows Ollama connection status (✓ ready / ⏳ starting) and per-model installation status (✓ installed / ⏳ pulling).

---

## Inference Cache (`backend/utils/cache.py`)

A JSON-backed, thread-locked, TTL-based cache stored at `data/inference_cache.json`.

- **`transcript_hash(*parts)`** — SHA-256 hash of concatenated transcript text. Deterministic across runs.
- **`get(key)`** — Returns cached value or `None` (if expired or missing).
- **`set(key, value, ttl)`** — Stores value with 24-hour default TTL.
- **`invalidate(prefix)`** — Removes all entries matching a prefix (for manual cache busting).

**Used by:**
- `content_classifier.classify_content` → cache key: `classify:{transcript_hash}`
- `ai_brain.refine_clip_scores` → cache key: `brain:{transcript_hash}:{content_type}:{chunked|full}`

When a video is re-processed (e.g. during dev/testing), the cache returns previously-computed classify/brain results instantly instead of re-running 3B/8B inference.

---

## Development Workflow

**One command boots everything:**
```bash
npm run dev
```

This runs `scripts/dev.mjs` which:
1. Activates Python 3.12 virtual environment
2. Installs/updates Python dependencies from requirements.txt
3. Creates runtime directories (outputs, thumbnails, temp, data)
4. Starts Ollama server (auto-detects install location on Windows)
5. Pulls team models (qwen3:8b, qwen2.5:3b) if not present
6. Prints AI Editor readiness table (✓/✗ per component)
7. Starts Huey background worker
8. Starts uvicorn FastAPI server on port 8000
9. Auto-recovers any videos stuck in PROCESSING state

**Individual commands:**
- `npm run worker` — just the background worker
- `npm run server` — just the API server
- `npm run models` — model management/pull
- `npm run check` — environment validation

---

## Key Design Decisions & Constraints

1. **Whisper stays on `small`** — The `medium` model OOMs on 16GB alongside qwen3:8b. This is a hard constraint.

2. **faster-whisper is the primary engine** — CTranslate2-based with Silero VAD gating (`vad_filter=True`), native word timestamps, per-segment `avg_logprob`/`no_speech_prob` for confidence filtering. pywhispercpp is the fallback. Model identifier must be a NAME (`"small"`), not a ggml `.bin` path.

3. **AI Editor reuses the full-video transcript** — `get_clip_transcript()` slices + rebases the stored transcript to the clip's time window, using the same source Path A uses during clip generation. This avoids Whisper hallucinations caused by re-transcribing isolated clips with no context. Falls back to fresh Whisper transcription only when no stored transcript covers the clip's range.

4. **ASS subtitle filter is broken on Windows FFmpeg** — The pipeline attempts ASS first, then falls back to drawtext (reliable on all platforms), then copies the original if both fail.

5. **FLUX AI thumbnails removed** — `flux.1-schnell` is not a valid Ollama model. Thumbnails are extracted from video frames only.

6. **`.env` has highest precedence** — User overrides in `.env` always win over YAML defaults and code defaults.

7. **Original clips are immutable** — `clips.output_path` is never overwritten. All edits create separate ClipEdit rows with unique filenames.

8. **All AI degrades gracefully** — Every model_team call is wrapped in try/except. If Ollama is down or a model fails, the pipeline continues with fallback values (empty strings, default scores, original video unchanged). Ollama calls get a single automatic retry with 1.5s backoff before fallback.

9. **Structured JSON output** — All classify and brain calls use Ollama's `format: "json"` via `model_team.chat_json()`, ensuring valid JSON without regex/fence-stripping.

10. **Single command startup** — `npm run dev` must bootstrap the ENTIRE stack. No separate manual steps.

11. **No Ken Burns / zoom effects** — The user explicitly does not want any zoom or pan effects on edited clips. Clips play at their native framing.

12. **Scene-preview intros OFF by default** — The frozen-zoom opening was deemed undesirable and is disabled.

13. **No schema migrations needed** — `clip.start_time` / `clip.end_time` already exist on the Clip model (absolute offsets into source video). The transcript-reuse feature uses these directly.

---

## Files Reference

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app creation, router mounting |
| `backend/api/routes/videos.py` | Video upload, import, status endpoints |
| `backend/api/routes/clips.py` | Clip CRUD, thumbnail download, bulk delete |
| `backend/api/routes/editor.py` | AI edit endpoint (triggers `auto_edit_clip`) |
| `backend/api/routes/settings.py` | User settings CRUD |
| `backend/services/transcription.py` | `transcribe_video()` — faster-whisper (primary) + pywhispercpp (fallback) |
| `backend/services/model_team.py` | RAM sequencer, `acquire()`, `chat()`, `chat_json()`, role resolution |
| `backend/services/content_classifier.py` | `classify_content()` — genre detection via classify specialist + cache |
| `backend/services/ai_brain.py` | `refine_clip_scores()` — 8-criteria viral rating via brain + cache |
| `backend/services/clip_generator.py` | Candidate generation, FFmpeg cutting, vertical conversion |
| `backend/services/auto_editor.py` | `auto_edit_clip()` — transcript reuse, sanitize, translate, burn, thumbnail |
| `backend/services/subtitles.py` | ASS/SRT/VTT generation, `burn_captions()` with ASS→drawtext→copy fallback |
| `backend/services/audio_analyzer.py` | RMS energy analysis |
| `backend/services/scene_detector.py` | FFmpeg scene detection |
| `backend/services/face_tracker.py` | MediaPipe BlazeFace face detection |
| `backend/services/thumbnail_generator.py` | Frame extraction and scoring |
| `backend/utils/config.py` | Pydantic BaseSettings, `.env` loading, YAML defaults |
| `backend/utils/cache.py` | Inference cache: `transcript_hash()`, `get()`, `set()`, `invalidate()` |
| `backend/utils/logging.py` | Structured logging |
| `backend/database/models.py` | SQLAlchemy ORM models (11 tables) |
| `backend/database/migrations/` | Alembic migrations (source of truth is `init_db()`) |
| `scripts/dev.mjs` | One-command full-stack boot |
| `frontend/js/app.js` | SPA controller |
| `frontend/js/components.js` | HTML generators (clip cards, version switchers) |
| `frontend/js/api.js` | HTTP client |
| `frontend/css/styles.css` | Styling |
| `requirements.txt` | Python dependencies (includes `faster-whisper>=1.1.0`) |
| `tests/test_services/test_auto_editor.py` | Unit tests for `_rebase_transcript_segments`, `get_clip_transcript`, `_sanitize_segments` |
