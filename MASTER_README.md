# AIClipper Pro 🎬✨ — Master Reference

*A single consolidated document that merges every `.md` file in the project into one
reference: the product README, setup, architecture, deployment and API docs, the model-upgrade
research, the Phase 0–5 build records, the verification guides, and the sellability audit.*

> **Looking for just the quick start?** Jump to §4 [Installation](#4--installation--setup) and §5 [Running the App](#5--running-the-app). For the "what's new" walkthrough, see §9 [Recent Features & How to Verify](#9--recent-features--how-to-verify).

---

## Table of Contents

1. [What Is AIClipper?](#1--what-is-aiclipper)
2. [Features](#2--features)
3. [How It Works — The Workflow](#3--how-it-works--the-workflow)
4. [Installation & Setup](#4--installation--setup)
5. [Running the App](#5--running-the-app)
6. [Architecture](#6--architecture)
7. [The Model Team](#7--the-model-team)
8. [Database Schema](#8--database-schema)
9. [Recent Features & How to Verify](#9--recent-features--how-to-verify)
10. [Configuration](#10--configuration)
11. [API Reference](#11--api-reference)
12. [Deployment](#12--deployment)
13. [Key Design Decisions & Constraints](#13--key-design-decisions--constraints)
14. [Troubleshooting](#14--troubleshooting)

**Appendices (historical / process records preserved from separate `.md` files):**
- [Appendix A — Model Upgrade Research Notes](#appendix-a--model-upgrade-research-notes)
- [Appendix B — Phase 0 Audit](#appendix-b--phase-0-audit)
- [Appendix C — Phase 1 Dependency & Environment Install](#appendix-c--phase-1-dependency--environment-install)
- [Appendix D — Phase 5 Build & Verification Report](#appendix-d--phase-5-build--verification-report)
- [Appendix E — Storage & Deduplication Verification Guide](#appendix-e--storage--deduplication-verification-guide)
- [Appendix F — BYOK AI Providers Verification Guide](#appendix-f--byok-ai-providers-verification-guide)
- [Appendix G — Sellability Cleanup Audit](#appendix-g--sellability-cleanup-audit)
- [Appendix H — Contributing (Issue Templates)](#appendix-h--contributing-issue-templates)

---

# 1 — What Is AIClipper?

An AI-powered, **completely local, CPU-friendly** desktop application that automatically cuts,
crops, and edits long videos into viral, short-form clips (YouTube Shorts, TikTok, Reels).

Everything runs on **your** machine via local models — no cloud AI services required. The core
loop: upload a video (or paste a YouTube link) → the system transcribes, analyzes, scores, and
ranks every possible clip → generates the top clips in vertical format with burned-in subtitles
and thumbnail images → presents them in a visual grid where you can preview, edit subtitles,
translate to Chinese, and download or publish.

> [!IMPORTANT]
> 🇺🇸 **ENGLISH LANGUAGE ONLY:** The clipping algorithms, audio-processing, and AI text
> generators are explicitly configured to process and evaluate **English-language videos only**.

---

# 2 — Features

- **Smart Video Clipping** — Analyzes video for highlights using visual and audio cues.
- **Auto-Editor Pipeline** — One-click generation of fully edited Shorts, including:
  - A 3-second animated intro.
  - Face-aware vertical (9:16) cropping and Shorts-style blurred backgrounds.
  - Cinematic color grading and sharpening.
  - **Dynamic Subtitles** — Large, bouncing, word-by-word karaoke-style subtitles.
  - **Clickbait Thumbnails** — Automatically selects the most active frame and overlays punchy,
    high-contrast title text.
- **Local AI Brain** — Uses Ollama (running entirely locally on your CPU) to generate catchy
  titles, descriptions, and SEO hashtags.
- **No GPU Required** — Heavily optimized FFmpeg pipelines run smoothly on standard consumer
  laptops and CPUs (16 GB RAM recommended).
- **Advanced AI Editor** — Beyond the one-click auto-edit, a dedicated **AI Editor Studio** per
  clip with: configurable **subtitle styles** (Fancy / Normal / Bold Caption presets with live
  preview), per-clip **Chinese translation**, and a **thumbnail editor** (template variants,
  editable headline, scored-frame filmstrip selection).
- **Audio Remix** — Play **your own music** over a clip (`replace_music` mode), with optional
  vocal/music stem separation (Demucs) and auto-ducking.
- **Storage Management & Deduplication** — Uploads and URL imports are deduplicated; a Storage
  panel clears orphan/pending/temp files.
- **Bring-Your-Own-Key (BYOK) AI Providers** — Route any AI role to a remote provider (OpenAI,
  Anthropic, Gemini, OpenAI-compatible) with automatic fallback to local if a key fails.

---

# 3 — How It Works — The Workflow

1. **Upload Your Video** — Upload any long-form English video through the web interface, or
   import it by URL.
2. **Automatic Analysis** — The backend engine analyzes the video, transcribes the audio, and
   evaluates emotional peaks and scene changes.
3. **Smart Division** — It divides the long video into multiple bite-sized, high-retention clips
   (e.g., 15s, 30s, or 60s sections; 60–90s is the default).
4. **One-Click Auto-Edit** — Click **Auto Edit** on any clip to generate the intro, crop to 9:16
   vertical, add popping subtitles, and design a clickbait thumbnail.

### End-to-end data flow (upload → download)

**Phase 1 — Upload & Metadata:** the user uploads a file (or yt-dlp downloads a URL); FFprobe
extracts metadata; a `Video` row is created with status `PENDING`.

**Phase 2 — Background processing** (Huey worker picks up the job):
- **Audio Extraction** — FFmpeg extracts a 16 kHz mono WAV for Whisper.
- **Transcription** — faster-whisper (CTranslate2) with Silero VAD gate, word timestamps, and
  per-segment confidence; falls back to pywhispercpp. Full transcript stored as JSON.
- **Content Classification** — the `classify` specialist reads the first 25 segments and outputs
  JSON content type + density, driving genre-aware weights. Cached by transcript hash.
- **Scene Detection** — FFmpeg scene changes → boundaries with start/end/duration.
- **Audio Analysis** — RMS energy peaks (excitement) and valleys (transitions).
- **Face Tracking** — MediaPipe BlazeFace bounding boxes with timestamps.
- **Heuristic Clip Scoring** — candidate windows (60/75/90s) scored on 5 signals (emotion,
  dialogue, scene_change, audio, face) with genre-adjusted weights.
- **AI Brain Refinement** — top candidates rated on 8 virality criteria (0–10); blended
  `final = heuristic × 0.55 + brain × 0.45`. Cached by transcript hash.
- **Clip Generation** — cuts selected clips, converts to 1080×1920 vertical with blurred
  background (or face-following crop).
- **Subtitles** — segments filtered to the clip window and rebased; generated as SRT/VTT/ASS.
- **Metadata** — title, description, hashtags, keywords, hook sentence, virality reason.
- **Thumbnail** — scored representative frame extracted and stored.

**Phase 3 — AI Editor (one-click post-processing):** `auto_edit_clip` reuses the stored
transcript (sliced + rebased to the clip window), sanitizes segments by confidence, optionally
translates to Chinese, generates an ASS caption file, burns captions into the video
(ASS → drawtext → unchanged fallback), generates a thumbnail, and saves a `ClipEdit` row
(unique per-language files: `edited_clip_{id}_{en|zh}.mp4`). The original clip is **never
overwritten**.

---

# 4 — Installation & Setup

## Prerequisites

- **Python 3.12** (not 3.13+ due to MediaPipe compatibility).
- **FFmpeg** — must be installed and on your system PATH (see the [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) `ffmpeg-release-essentials.zip` build).
- **16 GB RAM minimum** (recommended for Whisper `small`).
- **10 GB disk space** for models and output.
- **Git** — to clone the repository.

*Optional:* **Ollama** (for AI metadata), **YouTube API credentials** (Shorts upload),
**Facebook Developer App** (Reels upload).

## Downloading the project

```bash
git clone https://github.com/studymatter010-creator/AIclipper_pro.git
cd AIclipper_pro
```

## Windows setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m backend.utils.download_models
copy .env.example .env
```

## Linux setup (Ubuntu/Debian) / macOS

```bash
# Ubuntu/Debian system deps
sudo apt update && sudo apt install -y python3.12 python3.12-venv ffmpeg

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m backend.utils.download_models
cp .env.example .env
```

*macOS:* install via Homebrew — `brew install python@3.12 ffmpeg`.

## Setting up the AI Brain (Ollama — optional)

Keep the Ollama app running in the background, then pull a model:

```bash
ollama run qwen2        # or: llama3, gemma2
```

> In the one-command `npm run dev` flow, the team models are pulled automatically
> (qwen3:8b brain + qwen2.5:3b specialists) — see §7.

## YouTube API setup (optional)

1. Google Cloud Console → new project → enable **YouTube Data API v3**.
2. Configure the **OAuth Consent Screen** (add yourself as a test user).
3. Create an **OAuth 2.0 Client ID** (Desktop app type).
4. Download `client_secret.json` → save as `configs/client_secret_youtube.json`.

## Facebook API setup (optional)

1. Meta Developer Portal → new app (Business type) → add **Facebook Login**.
2. Request permissions: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`.
3. Generate a **Page Access Token** with `CREATE_CONTENT` task; add token + page ID to `.env`.

---

# 5 — Running the App

## Recommended: one command (modern)

```bash
npm run dev
```

`scripts/dev.mjs` boots everything: creates a Python venv, installs `requirements.txt`, creates
runtime dirs, starts Ollama, pulls the team models, prints an AI Editor readiness table,
starts the Huey worker, starts uvicorn on port 8000, and auto-recovers any videos stuck in
PROCESSING. Other npm scripts: `npm run worker`, `npm run server`, `npm run models`,
`npm run check`.

## Manual (two terminals)

```bash
# Terminal 1: web server
uvicorn backend.api.app:app --reload --port 8000

# Terminal 2: task queue
python -m backend.workers.consumer
```

Then open **http://localhost:8000** in your browser. Interactive API docs at
**http://localhost:8000/docs**.

---

# 6 — Architecture

AIClipper follows a **modular pipeline architecture** — each processing step is an independent
service that can be replaced without affecting the rest of the system.

```
┌─────────────────────────────────────────────────────────┐
│                     Web Frontend                         │
│            (Vanilla HTML/CSS/JS SPA)                     │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP / WebSocket
┌────────────────────────▼────────────────────────────────┐
│                   FastAPI Backend                         │
│  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌────────────┐  │
│  │ Videos   │ │Processing│ │ Clips   │ │ Publishing │  │
│  │ Router   │ │ Router   │ │ Router  │ │ Router     │  │
│  └──────────┘ └──────────┘ └─────────┘ └────────────┘  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│              Huey Task Queue (SQLite)                    │
└────────────────────────┬────────────────────────────────┘
┌────────────────────────▼────────────────────────────────┐
│                Processing Pipeline                       │
│  Transcription ──► Scene Detection ──► Audio Analysis    │
│       │                   │                  │           │
│       └───────── Face Tracking ─────────────┘           │
│                      │                                   │
│               Clip Scoring Engine                        │
│                      │                                   │
│  Clip Generation ──► Subtitles ──► Metadata ──► Thumbs  │
└────────────────────────┬────────────────────────────────┘
┌────────────────────────▼────────────────────────────────┐
│                   Storage Layer                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐  │
│  │ SQLite   │  │ File     │  │ Upload Registry      │  │
│  │ Database │  │ System   │  │ (YouTube, Facebook)  │  │
│  └──────────┘  └──────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

## Tech stack

| Layer | Technology | Purpose |
|---|---|---|
| Language | Python 3.12 | Backend, all AI/ML pipelines |
| Framework | FastAPI + uvicorn | REST API server (port 8000) |
| Database | SQLite + aiosqlite + SQLAlchemy ORM | Persistent storage for videos, clips, edits, settings |
| Background worker | Huey (with Redis or in-memory) | Async video processing jobs |
| AI models | Ollama (local LLM server) | Brain + specialist roles (see Model Team) |
| Speech recognition | faster-whisper (CTranslate2) + pywhispercpp fallback | Transcribe with VAD, word timestamps, confidence |
| Video processing | FFmpeg + FFprobe | Cutting, re-encoding, vertical conversion, subtitle burn, audio extraction |
| Face detection | MediaPipe BlazeFace | Face-aware cropping |
| Frontend | Vanilla HTML/CSS/JS (no build step) | Single-page app served statically by FastAPI |
| Dev tooling | Node.js (`scripts/dev.mjs`) | One-command boot |
| Config | Pydantic BaseSettings + `.env` + YAML | Hierarchical config |
| Inference cache | JSON file + TTL + transcript-hash keys | Avoids re-running LLM inference |

## Modularity

Each AI service is a standalone, swappable module:

- **Transcription** — swap faster-whisper/pywhispercpp for any STT engine.
- **Scene Detection** — swap for any custom detector.
- **Face Tracking** — swap MediaPipe for YOLO or others.
- **Metadata** — swap Ollama for any LLM (OpenAI, local, etc.).
- **Upload** — add platforms via the `BaseUploader` interface.

---

# 7 — The Model Team

The app runs a "team" of specialized Ollama models that take turns, managed by a **RAM-aware
sequencer** designed for a 16 GB machine:

| Role | Model | RAM | Purpose |
|------|-------|-----|---------|
| **brain** | qwen3:8b | ~5400 MB | Deep semantic analysis — rates each clip on 8 virality criteria (hook moments, emotional peaks, opinion bombs, revelation, conflict, quotable lines, story peaks, practical value). Heavy reasoning; not kept resident during fast specialist work. |
| **classify** | qwen2.5:3b | ~2600 MB | Content classification (8 types + 3 density levels). |
| **hook** | qwen2.5:3b | ~2600 MB | Single viral opening line (max 40 chars) for top clips. |
| **translate** | qwen2.5:3b | ~2600 MB | Translates subtitle segments to Chinese (or other languages) while preserving timing. |
| **Whisper** | faster-whisper (small) | N/A | Speech-to-text with Silero VAD, word timestamps, confidence. Falls back to pywhispercpp. |

**Structured output:** all classify/brain calls use Ollama's native `format: "json"` mode via
`model_team.chat_json()`. Each call gets one automatic retry (1.5 s backoff) before fallback.

**RAM Sequencer:** `acquire(role)` marks a model in-use, then unloads (via `keep_alive=0`) the
least-recently-used models until the estimated resident memory fits the ~9000 MB budget. The
acquired model is never evicted. So the brain (5.4 GB) finishes scoring and is evicted before the
translate specialist (2.6 GB) loads.

---

# 8 — Database Schema

| Model | Table | Purpose |
|-------|-------|---------|
| User | users | User accounts |
| Project | projects | Project containers |
| Video | videos | Uploaded/imported videos + metadata + processing state (incl. `current_stage`, `stage_progress`) |
| Transcript | transcripts | Full transcript (segments/words/plain text) |
| Scene | scenes | Scene boundaries |
| Clip | clips | Generated clips (score, hooks, paths; `output_path` is immutable; absolute `start_time`/`end_time`) |
| Subtitle | subtitles | SRT/VTT/ASS files linked to clips |
| ClipEdit | clip_edits | Per-language edits (`edited_clip_{id}_{en\|zh}.mp4`) |
| Thumbnail | thumbnails | Extracted thumbs with scores |
| Upload | uploads | YouTube/Facebook publish records |
| VoiceOver | voiceovers | edge-tts voiceover tracks |
| Setting | settings | User key-value settings |

**Key design rule:** `clips.output_path` is the original pristine render and is never overwritten
by edits; each edit creates a separate `ClipEdit` row.

---

# 9 — Recent Features & How to Verify

This is a plain-English summary of the latest feature additions and how to see each one working
on your screen — no code knowledge required. *(Full end-to-end verification guides for the
storage/dedup and BYOK features are in Appendices E and F.)*

> ⚠️ **Database note:** the app adds new saved fields (`current_stage`, `stage_progress`)
> automatically at startup — nothing to do on your side, just start the app.

## 9.1 Live progress bars for each processing step

While a video processes, a thin bar now appears under **each** step (Transcription, Scene
Detection, Audio, Faces, Scoring, Clips, Subtitles, Metadata, Thumbnails), in addition to the
overall progress bar at the top.

**How to check:** Upload a video and click **🚀 Process Now**. On the Processing screen, watch
the step list. Running steps with real sub-steps (Clips, Subtitles, Metadata, Thumbnails) show a
teal bar filling to a real %. Steps that can't report a precise number (Scoring, Transcription,
Scene Detection, Audio, Faces) show a pulsing bar. Finished steps show a filled bar with a ✓.
Failed steps turn red with a short reason. The bars move one at a time.

## 9.2 Dashboard stat cards now show real numbers

The four cards at the top of the Dashboard (Total Videos, Clips Generated, Completed, Published)
previously made the page error out; they now show live counts.

**How to check:** Open **Dashboard** — the four number cards show real figures with no red error.

## 9.3 Settings now load your saved choices

The Settings form previously reset to defaults; now it loads your saved values.

**How to check:** Open **Settings**, change anything (e.g. Highlight Color), **Save Settings**,
leave and return — your changes are still on screen.

## 9.4 Re-recorded videos are labelled "Re-uploaded"

Upload the same file twice and the second copy is marked with a gold **Re-uploaded** tag.

## 9.5 Visual polish

Video cards, dashboard quick-action tiles, Publishing account tiles, and Project cards all share
the consistent rounded card styling with hover lift.

## 9.6 Subtitle style picker + live preview (AI Editor)

Before generating subtitles, pick a style (Fancy / Normal / Bold Caption), font size, position,
accent color, font family, and outline — with a **live preview** that updates instantly in the
browser. The last style you used is remembered as your default, and "Generate Subtitle &
Thumbnail" applies your chosen style to the real burn.

## 9.7 Thumbnail editor (AI Editor)

Choose a template variant (full-bleed bold or minimal), edit the headline (prefilled from the
clip's `hook_sentence`), and override the frame by clicking one of 4–6 scored candidate frames in
a filmstrip. A live preview updates as you change things.

## 9.8 Audio Remix — your own music on your clip

Pick a track (via the 🎵 **Your Music** panel), choose *Replace with your music*, and hit
Generate. With Demucs installed you can also *keep your voice* while swapping the music (auto-
ducking).

## 9.9 Storage & deduplication + BYOK AI providers

See the feature summaries in §2 and the full verification guides in **Appendices E and F**.

---

# 10 — Configuration

Settings load in this order (later overrides earlier):

1. **YAML defaults** (`configs/default.yaml`) — base values.
2. **Pydantic model defaults** — hardcoded in `config.py`.
3. **`.env` file** — **HIGHEST PRECEDENCE** for user overrides.
4. **Environment variables** — override `.env` if set.

Key configuration groups:

- **Application:** host (`0.0.0.0`), port (`8000`), debug mode.
- **Paths:** 8 directories (upload, output, subtitle, thumbnail, log, model, temp, data).
- **Database:** SQLite at `data/aiclipper.db` with aiosqlite.
- **Whisper:** engine `faster-whisper` (primary), model `small`, VAD filter enabled, pywhispercpp fallback. `WHISPERX_ALIGN` opt-in toggle for forced alignment.
- **Ollama:** host `localhost:11434`, 120 s timeout.
- **Model Team:** brain `qwen3:8b`, classify/hook/translate `qwen2.5:3b`, RAM budget 9000 MB.
- **Output:** 1080×1920, 30 fps, libx264, CRF 21, AAC 192k.
- **Clips:** durations 60/75/90 s, up to 10 clips, 10 s gap.
- **Scoring:** weights auto-normalized to sum to 1.0, genre-adjusted at runtime.
- **Confidence:** `WHISPER_AVG_LOGPROB_MIN=-1.0`, `WHISPER_NO_SPEECH_PROB_MAX=0.60`.
- **Inference cache:** `data/inference_cache.json`, TTL-based, transcript-hash keyed.

Common `.env` toggles that matter:

```ini
WHISPER_MODEL=small              # set to another faster-whisper model name if needed
WHISPERX_ALIGN=false             # true enables wav2vec2 forced alignment (opt-in)
ROLE_PROVIDER_<role>=ollama      # BYOK routing per role (see Appendix F)
AUTO_DELETE_SOURCE=false         # auto-delete source after full processing
```

---

# 11 — API Reference

Base URL: `http://localhost:8000/api` · Interactive docs: `http://localhost:8000/docs`

## Health
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |

## Videos
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/upload` | Upload video (multipart/form-data) |
| GET | `/api/videos` | List videos (query: offset, limit, project_id) |
| GET | `/api/videos/{id}` | Get video details with clips and transcripts |

## Processing
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/process/{video_id}` | Start AI processing pipeline |
| GET | `/api/status/{video_id}` | Get processing status and progress |
| WS | `/api/ws/progress/{video_id}` | Real-time progress via WebSocket |

## Clips
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/clips` | List clips (query: offset, limit, video_id) |
| GET | `/api/clips/{id}` | Get clip details with subtitles/thumbnails |
| DELETE | `/api/clips/{id}` | Delete a clip |
| GET | `/api/clips/{id}/download` | Download clip file |
| POST | `/api/clips/{id}/auto-edit` | Run the AI Editor on a clip (accepts subtitle/thumbnail style options) |
| POST | `/api/clips/{id}/music` | Upload a music track to swap into a clip (Audio Remix) |
| GET | `/api/clips/{id}/thumbnail-candidates` | Scored candidate frames for the thumbnail-editor filmstrip |

## Publishing
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/publish` | Publish clip to platform |
| GET | `/api/analytics` | Dashboard statistics |

## Projects
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create project |

## Settings, Storage & Providers (BYOK)
| Method | Path | Description |
|--------|------|-------------|
| GET / PUT | `/api/settings` | Read / update settings |
| GET | `/api/storage/usage` | Storage usage report |
| POST | `/api/storage/orphans/cleanup`, `/api/storage/videos/pending-cleanup`, `/api/storage/temp-cleanup` | Destructive cleanups (require `confirm`) |
| GET / PUT | `/api/providers` | List / route AI providers per role |
| POST | `/api/providers/test` | Test a provider connection |
| PUT / DELETE | `/api/providers/key` | Store / remove an API key (OS vault) |

## WebSocket progress format

Connect to `ws://localhost:8000/api/ws/progress/{video_id}`. Messages are JSON:

```json
{ "video_id": 1, "status": "processing", "progress": 45, "step": "Analyzing audio..." }
```

## Errors

```json
{ "detail": "Error description" }
```

HTTP status codes: 400 (bad request), 404 (not found), 409 (conflict), 500 (server error).

---

# 12 — Deployment

## Local development

```bash
pip install -e ".[dev]"
python -m backend.utils.download_models

# Two terminals:
uvicorn backend.api.app:app --reload --port 8000
python -m backend.workers.consumer
```

## Docker

```bash
cd docker
docker-compose up -d

# Post-startup:
docker exec aiclipper-ollama ollama pull qwen2
docker exec aiclipper-app python -m backend.utils.download_models

# Logs:
docker-compose logs -f aiclipper-app
docker-compose logs -f aiclipper-worker

# Stop:
docker-compose down
```

Docker volumes persist uploads, outputs, database, and models across restarts.

## Behind a reverse proxy (nginx)

```nginx
server {
    listen 80;
    server_name aiclipper.local;

    client_max_body_size 4G;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /api/ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## Resource requirements

| Component | CPU | RAM | Disk |
|-----------|-----|-----|------|
| Whisper (small) | 4 cores | 4 GB | 500 MB |
| MediaPipe | 1 core | 500 MB | 50 MB |
| Ollama (7B) | 4 cores | 8 GB | 4 GB |
| FFmpeg | 2 cores | 1 GB | — |
| **Total** | **4+ cores** | **16 GB** | **5 GB** |

## Security notes

- Change `SECRET_KEY` in `.env` for production.
- Set `APP_DEBUG=false`.
- Configure CORS origins in `app.py` for production.
- Store API tokens securely; use HTTPS behind a reverse proxy.

---

# 13 — Key Design Decisions & Constraints

1. **Whisper stays on `small`** — `medium` OOMs on 16 GB alongside qwen3:8b. This is a hard constraint.
2. **faster-whisper is the primary engine** — CTranslate2-based, Silero VAD gate, word timestamps, confidence filtering; pywhispercpp is the fallback. Model must be a *name*, not a `.bin` path.
3. **AI Editor reuses the full-video transcript** — slices + rebases the stored transcript to the clip window (avoids re-transcription hallucinations); falls back to fresh transcription only when no coverage.
4. **ASS subtitle filter is broken on Windows FFmpeg** — the pipeline attempts ASS first, then draws text (reliable), then copies the original unchanged if both fail.
5. **FLUX AI thumbnails removed** — `flux.1-schnell` is not a valid Ollama model; thumbnails come from video frames only.
6. **`.env` has highest precedence.**
7. **Original clips are immutable** — edits create separate `ClipEdit` rows.
8. **All AI degrades gracefully** — every model call wraps in try/except; Ollama down → fallback values / defaults / original video.
9. **Structured JSON output** — classify/brain use Ollama `format: "json"` via `chat_json()`.
10. **Single command startup** — `npm run dev` boots the entire stack.
11. **No Ken Burns / zoom effects** — clips play at native framing.
12. **Scene-preview intros OFF by default** — the frozen-zoom opening was removed.
13. **Huey over Celery** — no external service (Redis/RabbitMQ); native Windows; persistent SQLite storage.
14. **FFmpeg via subprocess** (not ffmpeg-python) — full control, cross-platform, easy debugging.
15. **MediaPipe Tasks API** over the deprecated Solutions API.
16. **Vanilla JS frontend** — no build step, served directly by FastAPI.
17. **Audio re-encode after a remix (`-c:a copy` is never used after a remix)** — the edited clip's looped music froze unless audio is re-encoded (aac) with exact-length remix.

---

# 14 — Troubleshooting

| Issue | Solution |
|-------|----------|
| `MediaPipe not found` | Ensure Python 3.12 (not 3.13+). |
| `FFmpeg not found` | Add FFmpeg to PATH, restart terminal. |
| `Whisper model not found` | Run `python -m backend.utils.download_models`. |
| `Ollama connection refused` | Start Ollama: `ollama serve`. |
| `Port 8000 in use` | Use `--port 8001` or kill the existing process. |
| Subtitles don't burn / garbled | ASS can fail on Windows FFmpeg (libass without fontconfig) — the drawtext fallback handles it. Subtitle timing issues: fresh transcription of the rendered clip is now always tried first for accurate sync. |
| Chinese subtitles show English | Should be fixed by CJK output validation + cover-ge collapsing fixes (see appendices). |
| Clip frozen after adding music | Known fixed: audio is re-encoded after a remix (never `-c:a copy`). |

---

# Appendix A — Model Upgrade Research Notes

**Date:** 2026-09-09

Three *optional* model-stack upgrades were evaluated against the standing constraints:
**16 GB RAM** (qwen3:8b resident), **don't break working pipelines**, and **Whisper stays small**.

| Upgrade | Adopted? | Why |
|---|---|---|
| WhisperX forced alignment | ✅ yes (opt-in, OFF by default) | Fixes word-audio desync cheaply |
| NLLB-200 for translation | ❌ no | RAM-heavy, marginal quality gain vs qwen2.5:3b |
| large-v3-turbo Whisper | ⚠️ optional toggle, not default | Faster + more accurate but heavier |
| Real-ESRGAN thumbnail sharpener | ❌ no | torch dependency for a minor quality gain |

### WhisperX forced alignment — ADOPTED
Loads only the wav2vec2 phoneme aligner (~0.4 GB base); feeds existing segments to
`whisperx.align()`; OFF by default (`WHISPERX_ALIGN=true` to enable); safe fallback.

### NLLB-200 for translation — NOT ADOPTED
NLLB is ~2.3 GB on disk plus runtime footprint; existing Ollama translate is good enough for
on-screen subtitles.

### large-v3-turbo Whisper — BENCHMARKABLE, NOT DEFAULT
Try-able with zero code change: set `whisper_model=large-v3-turbo` in `.env`. Costs ~1.6 GB
int8 at load.

### Real-ESRGAN — NOT ADOPTED
Thumbnails are already sharpened; the dependency isn't worth the marginal gain.

---

# Appendix B — Phase 0 Audit

**Date:** 2026-09-11 · Before build — current code vs every requirement in the Phase 0–5 build
directive.

### Part A — Confirmed bug items (from prior sessions)

| Item | Status |
|------|--------|
| A1. Transcript reuse wired first | ✅ already correct |
| A2. faster-whisper actually installed | ❌ missing (install needed — see Appendix C) |
| A3. pywhispercpp centisecond conversion (÷100 not ÷1000) | ✅ already correct |
| A4. ASS `original_size` receives WxH | ✅ already correct (ASS still fails on fontconfig → drawtext fallback) |
| A5. Drawtext / thumbnail text width fitting | ✅ already correct |
| A6. Thumbnail from the clean original clip | ✅ already correct |
| A7. Chinese captions | 🟡 partially implemented (translate + CJK correct; coverage-merge fixed; e2e re-run needed) |

### Part B — Phase-by-phase readiness

- **Phase 1 — Model stack:** mostly missing (NLLB, faster-whisper, WhisperX, Demucs, bge all need install).
- **Phase 2 — Semantic clip candidates:** missing (no sentence-boundary work yet).
- **Phase 3 — Audio Remix:** missing (no Demucs + stem separation).
- **Phase 4 — WhisperX source-of-truth + ≥90% coverage gate:** missing.
- **Phase 5 — Verification:** not started.

**Conclusion:** the caption pipeline items (A1–A7 core) are in good shape; the bulk of the
directive requires new build work, plus one end-to-end Chinese confirmation.

---

# Appendix C — Phase 1 Dependency & Environment Install

Applies on the Windows machine that runs AIClipper. Run these from the project root **in the
same Python env** that runs the backend:

```bash
# NLLB-200 (CTranslate2 engine)
pip install ctranslate2 sentencepiece

# WhisperX (wav2vec2 forced alignment, pulls torch)
pip install "whisperx[align]" torch torchaudio

# Demucs stem separation + torch
pip install "demucs[htdemucs]" torch torchaudio

# sentence-transformers (bge-small-en for semantic boundaries)
pip install sentence-transformers

# faster-whisper (ASR, already in requirements.txt)
pip install faster-whisper

# Confirm everything resolves:
pip install -r requirements.txt
```

First-run model downloads (~few GB, cached under `~/.cache`):
- `facebook/nllb-200-distilled-600M` — CTranslate2 auto-converts
- `facebook/wav2vec2-base-960h` — WhisperX aligner
- `distil-large-v3` — faster-whisper (or use `small` on 16 GB)
- `BAAI/bge-small-en-v1.5` — sentence-transformers
- `htdemucs` — Demucs

Common `.env` toggles for Phase 1 features:

```ini
WHISPER_MODEL=small          # or distil-large-v3 if RAM allows
WHISPERX_ALIGN=false         # set true to enable forced alignment
NLLB_BEAM=1
NLLB_COMPUTE=int8
```

Feature wiring / fallback map:

| Feature | Requires installed | Rolls back to |
|---|---|---|
| NLLB translate | ctranslate2 + sentencepiece | Ollama translate specialist |
| faster-whisper ASR | faster-whisper | pywhispercpp |
| WhisperX alignment | whisperx + torch | faster-whisper native word timestamps |
| Demucs stems | demucs + torch | gain-only FFmpeg copy |
| bge embeddings | sentence-transformers | empty → heuristic boundaries |

Phase 4 prerequisite: to make the "ASS-success-not-fallback" criterion verifiable, a
fontconfig-enabled FFmpeg is needed. The current one (libass without fontconfig) always falls
back to drawtext, which is fine but means ASS is never the *primary* confirmed path on this
build. Recommended: grab a Gyan.dev or BtbN build shipping `ass` + fontconfig.

---

# Appendix D — Phase 5 Build & Verification Report

**Date:** 2026-09-12 · Runs: Phase 2–5 build + verification harness

### What was verified in that run (logic-level PASS)

A verification harness (`phase5_verify.py`) ran **16/16 checks PASS** covering: EN/ZH
sentence grouping, topic-shift boundary detection, semantic candidate windows, score-parity
(`_score_window` reused), and the ≥90% coverage gate behavior. All changed backend modules
`py_compile` clean; `app.js` + `api.js` pass `node --check`.

### Phase-by-phase status (as of that date)

- **Phase 1 — Model stack:** code written, compiles; runtime deps need install on your machine (Appendix C).
- **Phase 2 — Semantic boundaries:** ✅ built and logic-verified; `clip_scoring.py` refactored so scoring logic is unchanged and semantic windows plug in better anchors.
- **Phase 3 — Audio Remix:** ✅ built, wired, verified-selectable; your-own-music path works even without Demucs (simple replace mode). With Demucs: `mute_music` + `replace` while keeping voice also work.
- **Phase 4 — Source-of-truth + ≥90% coverage gate:** ✅ built; `[COVERAGE GATE] PASS/FLAG` logged; never marks success on thin coverage.
- **Phase 5 — Verification:** ongoing.

**Status:** all pure logic verified in the sandbox; runtime-dependent items (NLLB actually
translating, Demucs separating, WhisperX aligning, a full e2e clip edit with music + subtitles)
require one-time install on your machine + the `npm run dev` app.

---

# Appendix E — Storage & Deduplication Verification Guide

*Supplement to §9.9; the detailed manual checks.*

**What changed:** `videos` gained `source_video_id` (URL dedup) + `content_hash` (upload dedup);
a new `backend/api/routes/storage.py` provides usage, per-video delete, orphan scan/cleanup,
pending-cleanup, and temp-cleanup (all requiring `confirm`); auto-delete of source after full
processing is available via a toggle.

**Restart first:** stop the app and relaunch (or delete `data/aiclipper.db` to start clean; the
schema auto-migrates).

### Dedup checks
1. Upload the same file twice → second returns `"deduped": true`, `uploads/` has one file.
2. Import the same YouTube URL twice → `"deduped": true`, no re-download.
3. Run `python scripts/dedupe_videos.py` for a report; add `--yes` to actually clean up.

### Delete checks
- **Delete source only:** `uploads/<file>` removed; clips/thumbs/subs remain; row stays.
- **Delete everything:** source + all clips/thumbs/subs removed; rows cascade-deleted.
- **Clear pending, temp, orphans:** all require confirmation; each named what will be removed.

### Auto-delete source after full success
Settings → Storage → ON + save → upload and process a video → source gone, clips remain.

---

# Appendix F — BYOK AI Providers Verification Guide

*Supplement to §2; full manual checks.*

All backend files were `py_compile`-verified, frontend files `node --check`-verified in-session.

**What changed:** `backend/services/llm/` — pluggable `LLMProvider` interface (`chat()` +
`chat_json()`); implementations for Ollama, OpenAI, Anthropic, Gemini, OpenAI-compatible;
OS-level API-key vault (`keyring`); per-role routing; session usage/cost; provider REST API
endpoints + frontend Settings panel + sidebar widget.

**Restart first:** every role defaults to Local/Ollama — fully functional with **zero API keys**.

### Zero-key path
1. Don't configure any key → all four roles show **Local (Ollama)**; pipeline works as before.

### Route one role through a provider
1. Store an API key → switch one role (e.g. `brain`) to an OpenAI model → **Test** shows
   ✓ Connected → process a video → sidebar shows provider tag + paid badge; Settings shows
   usage/cost.

### Invalid key → fallback
Set a role to an API provider with a wrong key → pipeline doesn't fail; that role falls back to
Local/Ollama with a logged fallback message.

### Keys are not leaked
Key never returned by any endpoint; masked after save; server logs don't contain it.

### Part-1 model A/B (optional)
Run on your machine with Ollama running:
```
python scripts/benchmark_models.py --repeats 3
```
Benchmarks candidates against current picks; writes `benchmark_results.csv` + report.
**Do not blind-swap** without quality and latency evidence.

---

# Appendix G — Sellability Cleanup Audit

**Date:** 2026-09-13 · Files read on-disk and classified (SAFE vs NEEDS REVIEW).

### Safe to remove (dev/probe artifacts, no shipping value)

| File | What it is |
|---|---|
| `check_db.py` | One-off re-process tool (hardcodes `video_id=1`) |
| `phase5_verify.py` | Standalone verification harness (checks now in unit tests + real runs) |
| `PHASE0_AUDIT.md` | Pre-build planning doc |
| `PHASE1_INSTALL.md` | Stale dependency-install instructions (deps ultimately not adopted) |
| `PHASE5_REPORT.md` | Completed build status report |
| `MODEL_UPGRADE_NOTES.md` | Research/thought-documentation |
| `MASTERCLEAN_PROMPT.md` | Old session master-system-prompt snapshot |
| `VERIFICATION_CHECKLIST.md` | Completed checklist |
| `VERIFICATION_STORAGE.md` | Completed verification record (content now in this README §E) |
| `VERIFICATION_BYOK.md` | Completed verification record (content now in this README §F) |

*All content from these files is preserved in this master README.*

### Needs review (keep)

- `README.md`, `docs/*`, `configs/` — the real user/docs surface (this master README is the
  comprehensive reference).
- `frontend/`, `backend/`, `scripts/`, `tests/` — all shipped app source.
- `requirements.txt`, `package.json` — dependencies.
- Runtime logs (`logs/`) — already covered by `.gitignore`.

### `.gitignore` — already comprehensive, no additions needed

Covers: `__pycache__/`, `*.py[cod]`, `.venv/`, `.env`, `uploads/`, `outputs/`,
`subtitles/`, `thumbnails/`, `logs/`, `temp/`, `data/`, `*.db`, `models/*.bin`, IDE/OS
files, Docker volumes.

### Removal command (run in Command Prompt inside the AIClipper folder)

```bash
git rm check_db.py phase5_verify.py PHASE0_AUDIT.md PHASE1_INSTALL.md PHASE5_REPORT.md MODEL_UPGRADE_NOTES.md MASTERCLEAN_PROMPT.md VERIFICATION_CHECKLIST.md VERIFICATION_STORAGE.md VERIFICATION_BYOK.md
git commit -m "Remove dev/probe artifacts (sellability cleanup)"
```

---

# Appendix H — Contributing (Issue Templates)

### Bug Report

**Bug Description:** a clear description of the bug.
**Steps to Reproduce:** 1… 2… 3…
**Expected Behavior:** what you expected to happen.
**Actual Behavior:** what actually happened.
**Environment:** OS (Windows/macOS/Linux), Python 3.12.x, FFmpeg version, AIClipper version.
**Screenshots / Logs:** relevant log output from `logs/`.

### Feature Request

**Feature Description:** a clear description of the feature you'd like.
**Use Case:** why it would be useful.
**Proposed Solution:** how you'd like it to work.
**Alternatives Considered:** other approaches you considered.
**Additional Context:** screenshots, mockups, or examples from other tools.

---

*This document consolidates every `.md` file in the AIClipper repository as of 2026-09-13:
`README.md`, `docs/setup.md`, `docs/architecture.md`, `docs/api.md`, `docs/deployment.md`,
`MASTERCLEAN_PROMPT.md`, `MODEL_UPGRADE_NOTES.md`, `PHASE0_AUDIT.md`, `PHASE1_INSTALL.md`,
`PHASE5_REPORT.md`, `VERIFICATION_CHECKLIST.md`, `VERIFICATION_STORAGE.md`,
`VERIFICATION_BYOK.md`, `SELLABILITY_AUDIT.md`, and the GitHub issue templates.*