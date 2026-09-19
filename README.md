# 🎬 AIClipper - AI-Powered Video Clipping Platform

**Transform long-form videos into viral short-form content with AI-powered automation**

AIClipper is a professional desktop application that automatically analyzes, clips, and edits long videos into engaging short-form content optimized for YouTube Shorts, TikTok, and Instagram Reels.

---

## 🌟 Key Features

### Intelligent Video Analysis
- **Smart Scene Detection** — Automatically identifies highlight moments using visual and audio cues
- **AI-Powered Scoring** — Evaluates clips based on emotion, pacing, reactions, and virality potential
- **Face Tracking** — MediaPipe-powered face detection for optimal framing
- **Multi-Language Support** — English and Chinese subtitle generation with translation

### Professional Auto-Editor
- **Dynamic Subtitles** — Word-by-word karaoke-style captions with customizable fonts, colors, and positions
- **Clickbait Thumbnails** — AI-generated thumbnails with optimal frame selection and text overlays
- **Audio Remix** — Demucs-powered stem separation:
  - Mute background music (keep vocals only)
  - Replace music with your own tracks (auto-ducking)
  - Volume control for voice and music independently
  - Built-in copyright-free music fallback
- **Vertical Crop** — Smart 9:16 aspect ratio with face-aware positioning
- **Subtitle Translation** — NLLB-200 neural machine translation

### AI Brain (Local & Cloud)
- **Ollama Integration** — Local AI models (Qwen, DeepSeek) for:
  - Title generation
  - Description writing
  - Hashtag extraction
  - Content classification
- **Multi-Provider Support** — OpenAI, Anthropic, Google Gemini, or custom OpenAI-compatible endpoints
- **Bring Your Own Keys (BYOK)** — Use your own API keys or run 100% locally

### Production-Ready Infrastructure
- **FastAPI Backend** — Modern async Python web framework
- **SQLAlchemy ORM** — Type-safe database operations with Alembic migrations
- **WebSocket Progress** — Real-time pipeline status updates
- **Background Task Queue** — Huey task queue with Redis/in-memory backends
- **Modular Architecture** — Clean separation of concerns, easy to extend

---

## 🎯 Perfect For

- **Content Creators** — Automate your shorts workflow
- **Social Media Agencies** — Scale client content production
- **Marketing Teams** — Repurpose webinars/podcasts into social clips
- **Course Creators** — Turn lectures into promotional shorts
- **Developers** — White-label and resell to your clients

---

## 💻 Tech Stack

| Layer | Technologies |
|-------|-------------|
| **Backend** | FastAPI, Python 3.12+, SQLAlchemy, Alembic |
| **Frontend** | Vanilla JavaScript SPA, HTML5, CSS3 |
| **Database** | SQLite (development), PostgreSQL-ready |
| **Video Processing** | FFmpeg, OpenCV, PySceneDetect |
| **AI & ML** | Ollama, Faster-Whisper, Demucs, NLLB-200, MediaPipe |
| **Task Queue** | Huey (Redis or in-memory) |
| **Deployment** | Docker, Docker Compose |

---

## 📦 What's Included

```
AIClipper/
├── backend/               # FastAPI application
│   ├── api/              # REST API routes
│   ├── database/         # SQLAlchemy models & migrations
│   ├── services/         # Core business logic
│   │   ├── audio_remix.py        # Demucs stem separation
│   │   ├── auto_editor.py        # AI editing pipeline
│   │   ├── clip_scoring.py       # Virality scoring
│   │   ├── transcription.py      # Faster-Whisper ASR
│   │   ├── thumbnail/            # Thumbnail generation
│   │   └── llm/                  # Multi-provider AI
│   └── utils/            # Shared utilities
├── frontend/             # Web UI
│   ├── index.html       # Single-page application
│   ├── css/             # Styles
│   └── js/              # Application logic
├── configs/             # Configuration files
├── docs/                # Documentation
├── tests/               # Test suite
├── docker/              # Docker configuration
├── requirements.txt     # Python dependencies
└── README.md           # This file
```

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.12+** ([Download](https://www.python.org/downloads/))
- **FFmpeg** (must be in PATH) ([Download](https://ffmpeg.org/download.html))
- **Ollama** (for local AI) ([Download](https://ollama.com/))
- **Git** ([Download](https://git-scm.com/downloads))

### Installation

1. **Clone the repository**
   ```bash
   git clone <your-repo-url>
   cd AIClipper
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   
   # Windows
   .venv\Scripts\activate
   
   # macOS/Linux
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Setup Ollama models** (for local AI)
   ```bash
   ollama pull qwen2.5:3b
   ```

5. **Configure environment** (optional)
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

6. **Run the application**
   ```bash
   # Development
   python -m uvicorn backend.api.app:app --reload --host 0.0.0.0 --port 8000
   
   # Or use the provided scripts
   # Windows
   start.bat
   
   # macOS/Linux
   ./start.sh
   ```

7. **Open in browser**
   ```
   http://localhost:8000
   ```

---

## 📖 Documentation

- **[Setup Guide](docs/setup.md)** — Detailed installation instructions
- **[Architecture](docs/architecture.md)** — System design and data flow
- **[API Reference](docs/api.md)** — REST API endpoints
- **[Deployment](docs/deployment.md)** — Production deployment guide

---

## 🔧 Configuration

All configuration is managed via environment variables. Copy `.env.example` to `.env` and customize:

```bash
# Database
DATABASE_URL=sqlite:///./data/aiclipper.db

# AI Providers
OLLAMA_BASE_URL=http://localhost:11434
OPENAI_API_KEY=your-key-here  # Optional

# FFmpeg
FFMPEG_PATH=/usr/bin/ffmpeg

# Video Processing
OUTPUT_WIDTH=1080
OUTPUT_HEIGHT=1920
OUTPUT_FPS=30
OUTPUT_CODEC=libx264
OUTPUT_CRF=23

# Security
SECRET_KEY=change-this-to-a-random-secret-key

# Demucs (Audio Remix)
DEMUCS_MODEL=htdemucs
DEMUCS_DEVICE=cpu  # or 'cuda' for GPU

# Whisper (Transcription)
WHISPER_MODEL=small  # tiny, base, small, medium, large
```

---

## 🎨 Features in Detail

### Audio Remix (Demucs Stem Separation)
The audio remix feature uses Demucs 4.0 to separate audio into vocals, drums, bass, and other stems:

- **Mute Music** — Extract vocals only, remove all background music
- **Replace Music** — Swap background music with your own track (auto-loops and ducks under speech)
- **Volume Control** — Independent gain control for vocals and music
- **Copyright-Free Fallback** — Built-in ambient track when no replacement is provided

### AI Editor Pipeline
One-click automated editing includes:

1. **Transcription** — Faster-Whisper with VAD for accurate word timestamps
2. **Audio Remixing** — Apply music replacement/removal
3. **Caption Burn** — Animated word-by-word subtitles with FFmpeg drawtext
4. **Thumbnail Generation** — Multi-candidate scoring (sharpness, brightness, face detection)
5. **Metadata Generation** — AI-powered titles, descriptions, hashtags

### Subtitle Styles
Three built-in presets:
- **Fancy** — Karaoke glow effect with word highlighting
- **Normal** — Clean, readable captions
- **Bold** — Large, heavy impact text

Custom fonts, colors, sizes, and positions supported.

---

## 🧪 Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=backend --cov-report=html

# Run specific test file
pytest tests/test_services/test_clip_scoring.py
```

---

## 🐳 Docker Deployment

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

---

## 📊 System Requirements

### Minimum
- **CPU:** 4 cores (Intel i5 / AMD Ryzen 5)
- **RAM:** 8 GB
- **Storage:** 10 GB free space
- **OS:** Windows 10+, macOS 11+, Ubuntu 20.04+

### Recommended
- **CPU:** 8+ cores (Intel i7 / AMD Ryzen 7)
- **RAM:** 16 GB (32 GB for large models)
- **GPU:** NVIDIA GPU with CUDA support (optional, for Demucs/Whisper acceleration)
- **Storage:** 50 GB SSD

---

## 🔒 Security

- **Environment Variables** — All secrets externalized to `.env`
- **OS Keyring** — API keys stored securely via `keyring` library
- **Input Validation** — Pydantic models validate all API inputs
- **SQL Injection Protection** — SQLAlchemy ORM with parameterized queries
- **CORS Configuration** — Configurable allowed origins

---

## 🤝 Support & Customization

This is a complete, production-ready codebase. For:
- Custom feature development
- White-label branding
- Enterprise deployment assistance
- Training and onboarding

Contact the seller for professional services.

---

## 📜 License

Copyright (c) 2026 AIClipper

Licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

Built with:
- [FFmpeg](https://ffmpeg.org/) — Video processing
- [Faster-Whisper](https://github.com/guillaumekln/faster-whisper) — Speech recognition
- [Demucs](https://github.com/facebookresearch/demucs) — Audio source separation
- [Ollama](https://ollama.com/) — Local LLM inference
- [FastAPI](https://fastapi.tiangolo.com/) — Modern Python web framework
- [MediaPipe](https://google.github.io/mediapipe/) — Face detection

---

## 📞 Questions?

For pre-sale questions, technical support, or customization inquiries, contact the seller.

**Happy clipping! 🎬✨**
