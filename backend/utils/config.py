"""
AIClipper Configuration Module

Loads configuration from .env files and YAML config, providing typed access
to all application settings via Pydantic Settings.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Resolve project root – two levels up from this file (backend/utils/config.py)
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent.parent


def _load_yaml_config(path: Path | None = None) -> dict[str, Any]:
    """Load the YAML configuration file and return as a flat dict."""
    if path is None:
        path = PROJECT_ROOT / "configs" / "default.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data


# ---------------------------------------------------------------------------
# Sub-models for nested configuration
# ---------------------------------------------------------------------------

class ScoringWeights(BaseSettings):
    """Clip scoring weight configuration."""
    emotion: float = 0.25
    dialogue: float = 0.20
    scene_change: float = 0.20
    audio: float = 0.20
    face: float = 0.15

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringWeights":
        total = self.emotion + self.dialogue + self.scene_change + self.audio + self.face
        if abs(total - 1.0) > 0.01:
            # Normalize automatically
            self.emotion /= total
            self.dialogue /= total
            self.scene_change /= total
            self.audio /= total
            self.face /= total
        return self


class SubtitleFont(BaseSettings):
    """Subtitle font settings."""
    family: str = "Arial"
    size: int = 24
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    shadow_color: str = "#33000000"
    shadow_offset: int = 2


class SubtitleHighlight(BaseSettings):
    """Active word highlight settings."""
    enabled: bool = True
    color: str = "#FFD700"
    style: str = "color"  # color, background, underline


class SubtitleStyle(BaseSettings):
    """Complete subtitle style configuration."""
    default_format: str = "burned"  # srt, vtt, burned
    font: SubtitleFont = SubtitleFont()
    highlight: SubtitleHighlight = SubtitleHighlight()
    position: str = "bottom"  # top, center, bottom
    max_chars_per_line: int = 42
    max_lines: int = 2


class OutputSettings(BaseSettings):
    """Video output settings."""
    width: int = 1080
    height: int = 1920
    fps: int = 30
    codec: str = "libx264"
    crf: int = 21
    preset: str = "medium"
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    format: str = "mp4"


# ---------------------------------------------------------------------------
# Main Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Main application settings.

    Loads from (in priority order):
    1. Environment variables
    2. .env file
    3. YAML config file (configs/default.yaml)
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "AIClipper"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_debug: bool = True
    secret_key: str = "change-this-to-a-random-secret-key"

    # --- Paths ---
    upload_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "uploads")
    output_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "outputs")
    subtitle_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "subtitles")
    thumbnail_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "thumbnails")
    log_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "logs")
    model_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "models")
    temp_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "temp")
    data_dir: Path = Field(default_factory=lambda: PROJECT_ROOT / "data")

    # --- Database ---
    database_url: str = Field(
        default_factory=lambda: f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'aiclipper.db'}"
    )

    # --- Whisper (PHASE 1: faster-whisper is the PRIMARY ASR engine) ---
    # faster-whisper (CTranslate2) name.  Phase 1 directive: distil-large-v3 OR
    # large-v3-turbo (both distilled/turbo variants, more RAM-frugal than full
    # large-v3).  The generalized RAM sequencer (model_team) evicts other heavy
    # models so these fit on 16GB alongside qwen3:8b.  Keep "small" as the safe
    # fallback if OOM persists.  CTranslate2 CANNOT load ggml-*.bin weights.
    whisper_model: str = "distil-large-v3"
    # whisper.cpp fallback weights (pywhispercpp engine only; smaller to fit RAM).
    whisper_model_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "models" / "ggml-small.bin"
    )
    whisper_threads: int = 4
    whisper_language: str = "auto"

    # --- WhisperX forced alignment (PHASE 1: source-of-truth word timestamps) ---
    # WhisperX refines faster-whisper's word timestamps via a wav2vec2 phoneme
    # aligner, fixing word-audio desync and improving word-level karaoke
    # highlighting — and becomes the sentence-boundary source for Phase 2/4.
    # wav2vec2 base aligner adds ~0.4-1.5GB RAM; the generalized sequencer
    # evicts competing heavy models so it fits. On ANY failure the pipeline
    # safely falls back to faster-whisper's native word timestamps.
    whisperx_align: bool = True
    whisperx_align_model: str = "facebook/wav2vec2-base-960h"
    whisperx_language: str = "en"

    # --- Ollama (the "AI brain") ---
    # qwen3:8b — best fit for 16GB RAM alongside Whisper. Native Chinese,
    # strong instruction-following + structured output for metadata generation.
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    ollama_timeout: int = 120

    # --- Model Team (specialized experts cooperating in sequence) ---
    # Each complex task is delegated to a purpose-picked model, coordinated by
    # backend/services/model_team.py. The heavy brain (ollama_model) is NOT kept
    # resident while a light specialist does fast work — the sequencer unloads
    # LRU models to stay under team_ram_budget_mb so 16GB never OOMs.
    # Specialists share the small qwen2.5:3b (fast, strong zh + JSON output).
    team_classify: str = "qwen2.5:3b"     # content-type / density classifier
    team_hook: str = "qwen2.5:3b"         # snap-hook opening-line extractor
    team_translate: str = "qwen2.5:3b"    # Ollama subtitle-translate FALLBACK (NLLB-200 is now primary)
    team_ram_budget_mb: int = 9000        # keep all resident Ollama models ≤ this

    # --- BYOK (bring-your-own-key): pluggable API providers per role ---
    # Per-role provider routing is persisted in the user settings table and
    # cached by backend/services/llm/role_config.py; these are the *defaults*
    # used before any role is configured (every role defaults to Local/Ollama,
    # so the app works with zero API keys). ``team_ollama_default`` seeds the
    # local model each role would otherwise use.
    team_ollama_default: str = "qwen3:8b"           # default brain via local
    llm_openai_compatible_base: str = ""             # base URL for an OpenAI-compatible endpoint (non-empty enables it)
    llm_anthropic_max_tokens: int = 1024

    # --- NLLB-200 translation (PHASE 1: replaces Ollama translate) ---
    # Purpose-built many-to-many translator via CTranslate2 (CPU).  Deterministic
    # subtitle translation with native Simplified-Chinese output.  Computes on CPU
    # int8 to fit the 16GB budget alongside qwen3:8b, Demucs and WhisperX.
    nllb_model: str = "facebook/nllb-200-distilled-600M"
    nllb_sentencepiece_model: str = ""        # optional override path to the .spm file
    nllb_device: str = "cpu"                  # "cpu" | "cuda"
    nllb_compute: str = "int8"                # "int8" | "fp16" | "float32" (int8 = smallest on CPU)
    nllb_beam: int = 1                        # 1 = greedy (deterministic subtitles); raise for quality
    nllb_repetition_penalty: float = 1.3
    nllb_max_length: int = 256
    nllb_max_batch: int = 16

    # --- Demucs (PHASE 1: Audio Remix stems) ---
    demucs_model: str = "htdemucs"            # 4-stem: vocals/drums/bass/other
    demucs_device: str = "cpu"
    demucs_shifts: int = 1
    demucs_sample_rate: int = 44100           # fallback stem sample rate if unprovided

    # --- bge-small-en (PHASE 1: semantic clip boundaries, Phase 2) ---
    # ~134MB embedding model for sentence similarity used to anchor clip
    # candidates at sentence boundaries in Phase 2.
    bge_model: str = "BAAI/bge-small-en-v1.5"

    # --- Semantic boundaries (PHASE 2) ---
    # Topic-shift cosine threshold for sentence-to-sentence embedding
    # similarity; a seam below this value is treated as a semantic boundary.
    semantic_threshold: float = 0.65
    # Enables semantic-anchored clip candidate windows in addition to the
    # plain sliding sweep (score_clips).
    semantic_boundaries_enabled: bool = True

    # --- MediaPipe ---
    mediapipe_model_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "models" / "blaze_face_short_range.tflite"
    )
    # FaceLandmarker model for facial-expressiveness scoring (Change 5): mouth-open
    # distance + eyebrow-raise drive thumbnail frame selection.  Optional — when
    # this file is absent, expressiveness scoring returns a neutral 0.5.
    mediapipe_landmark_model_path: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "models" / "face_landmarker.task"
    )
    face_sample_every_n_frames: int = 3

    # --- FFmpeg ---
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # --- Clip Settings ---
    # Premium short-form length: 1–1.5 minute clips by default.
    clip_durations: str = "60,75,90"
    max_clips_per_video: int = 10
    min_clip_gap_seconds: int = 10
    face_crop_enabled: bool = False  # Smart vertical face-crop. OFF by default: clips preserve the WHOLE source frame with blurred bars over a 1080x1920 canvas.

    # --- Scoring Weights ---
    scoring_weights: ScoringWeights = ScoringWeights()

    # --- Subtitle Style ---
    subtitle_style: SubtitleStyle = SubtitleStyle()

    # --- Output ---
    output_settings: OutputSettings = OutputSettings()

    # --- Processing Limits ---
    max_video_duration_seconds: int = 14400  # 4 hours
    supported_formats: str = "mp4,mkv,avi,mov"
    max_file_size_mb: int = 4096

    # --- YouTube ---
    youtube_client_secrets_file: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "configs" / "client_secret_youtube.json"
    )
    youtube_token_file: Path = Field(
        default_factory=lambda: PROJECT_ROOT / "data" / "youtube_token.json"
    )

    # --- Facebook ---
    facebook_app_id: str = ""
    facebook_app_secret: str = ""
    facebook_page_id: str = ""
    facebook_access_token: str = ""

    @property
    def clip_durations_list(self) -> list[int]:
        """Parse clip_durations string into list of ints."""
        if isinstance(self.clip_durations, list):
            return self.clip_durations
        return [int(x.strip()) for x in self.clip_durations.split(",") if x.strip()]

    @property
    def supported_formats_list(self) -> list[str]:
        """Parse supported_formats string into list of strings."""
        if isinstance(self.supported_formats, list):
            return self.supported_formats
        return [x.strip().lower() for x in self.supported_formats.split(",") if x.strip()]

    def ensure_directories(self) -> None:
        """Create all required directories if they don't exist."""
        for d in [
            self.upload_dir,
            self.output_dir,
            self.subtitle_dir,
            self.thumbnail_dir,
            self.log_dir,
            self.model_dir,
            self.temp_dir,
            self.data_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_yaml(cls, yaml_path: Path | None = None, **overrides: Any) -> "Settings":
        """Create Settings with YAML defaults, then layer .env and env vars on top."""
        yaml_data = _load_yaml_config(yaml_path)
        # Flatten nested YAML keys into env-style names
        flat: dict[str, Any] = {}
        if "whisper" in yaml_data:
            w = yaml_data["whisper"]
            flat["whisper_model"] = w.get("model", "small")
            flat["whisper_threads"] = w.get("threads", 4)
            flat["whisper_language"] = w.get("language", "auto")
        if "ollama" in yaml_data:
            o = yaml_data["ollama"]
            flat["ollama_host"] = o.get("host", "http://localhost:11434")
            flat["ollama_model"] = o.get("model", "qwen3:8b")
            flat["ollama_timeout"] = o.get("timeout", 120)
        if "clips" in yaml_data:
            c = yaml_data["clips"]
            flat["clip_durations"] = c.get("durations", [15, 30, 60])
            flat["max_clips_per_video"] = c.get("max_per_video", 10)
            flat["min_clip_gap_seconds"] = c.get("min_gap_seconds", 10)
        if "output" in yaml_data:
            o = yaml_data["output"]
            if "resolution" in o:
                flat.setdefault("output_settings", OutputSettings())
        flat.update(overrides)
        return cls(**flat)


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings singleton."""
    settings = Settings()
    settings.ensure_directories()
    return settings
