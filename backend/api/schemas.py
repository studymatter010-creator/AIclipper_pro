"""
AIClipper API Schemas

Pydantic v2 request/response schemas for all API endpoints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Common / Error
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Standard error response returned by all endpoints on failure."""

    detail: str = Field(..., description="Human-readable error message")
    field: str | None = Field(None, description="Field that caused the error, if applicable")

    model_config = {"json_schema_extra": {"examples": [{"detail": "Video not found", "field": None}]}}


# ---------------------------------------------------------------------------
# Video Schemas
# ---------------------------------------------------------------------------

class VideoUploadResponse(BaseModel):
    """Response returned after a successful video upload."""

    id: int = Field(..., description="Unique video ID")
    filename: str = Field(..., description="Original filename")
    filepath: str = Field(..., description="Server-side file path")
    status: str = Field(..., description="Current processing status")
    duration: float | None = Field(None, description="Video duration in seconds")
    width: int | None = Field(None, description="Video width in pixels")
    height: int | None = Field(None, description="Video height in pixels")
    fps: float | None = Field(None, description="Frames per second")
    codec: str | None = Field(None, description="Video codec")
    filesize: int | None = Field(None, description="File size in bytes")
    created_at: datetime = Field(..., description="Upload timestamp")
    deduped: bool = Field(
        default=False,
        description="True when this upload/import matched an existing video and "
        "no new file was written to disk (deduplicated).",
    )

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {
            "examples": [
                {
                    "id": 1,
                    "filename": "interview.mp4",
                    "filepath": "uploads/interview.mp4",
                    "status": "pending",
                    "duration": 1234.5,
                    "width": 1920,
                    "height": 1080,
                    "fps": 30.0,
                    "codec": "h264",
                    "filesize": 104857600,
                    "created_at": "2026-01-15T10:30:00",
                }
            ]
        },
    }


class ImportUrlRequest(BaseModel):
    """Request to import a video from a shareable URL (YouTube/Facebook/etc.)."""

    url: str = Field(..., description="Shareable video URL (YouTube, Facebook, etc.)")
    project_id: int | None = Field(None, description="Optional project ID")
    auto_process: bool = Field(True, description="Automatically start processing after download")
    clip_count: int | None = Field(None, ge=1, le=100, description="Number of clips to generate")
    clip_duration: int | None = Field(None, ge=20, le=180, description="Preferred clip length in seconds")


class ProcessOptions(BaseModel):
    """Optional tuning controls for the clipping pipeline."""

    clip_count: int | None = Field(
        None, ge=1, le=100, description="Number of clips to generate"
    )
    clip_duration: int | None = Field(
        None, ge=20, le=180, description="Preferred clip length in seconds"
    )


class TranscriptSchema(BaseModel):
    """Transcript data associated with a video."""

    id: int
    language: str = Field(..., description="Language code, e.g. 'en'")
    full_text: str | None = Field(None, description="Plain text transcript")
    content_json: Any | None = Field(None, description="Structured transcript segments")
    created_at: datetime

    model_config = {"from_attributes": True}


class SceneSchema(BaseModel):
    """Scene boundary detected in a video."""

    id: int
    scene_number: int
    start_time: float = Field(..., description="Scene start in seconds")
    end_time: float = Field(..., description="Scene end in seconds")
    duration: float
    score: float | None = None

    model_config = {"from_attributes": True}


class SubtitleSchema(BaseModel):
    """Subtitle file linked to a clip."""

    id: int
    format: str
    filepath: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ThumbnailSchema(BaseModel):
    """Thumbnail image for a clip."""

    id: int
    filepath: str
    score: float | None = None
    format: str
    width: int | None = None
    height: int | None = None
    is_selected: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class UploadSchema(BaseModel):
    """Platform upload record."""

    id: int
    platform: str
    status: str
    platform_video_id: str | None = None
    url: str | None = None
    scheduled_at: datetime | None = None
    published_at: datetime | None = None
    error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ClipSummary(BaseModel):
    """Compact clip representation used in video detail views."""

    id: int
    clip_number: int
    start_time: float
    end_time: float
    duration: float
    total_score: float | None = None
    title: str | None = None
    status: str
    output_path: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class VideoDetail(BaseModel):
    """Detailed video representation including related clips and transcripts."""

    id: int
    project_id: int
    filename: str
    filepath: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    audio_codec: str | None = None
    bitrate: int | None = None
    filesize: int | None = None
    format_name: str | None = None
    status: str
    processing_progress: int = 0
    processing_step: str | None = None
    current_stage: str | None = None
    stage_progress: int | None = None
    content_type: str | None = None
    content_density: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    clips: list[ClipSummary] = Field(default_factory=list)
    transcripts: list[TranscriptSchema] = Field(default_factory=list)
    scenes: list[SceneSchema] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class VideoListItem(BaseModel):
    """Compact video representation for list views."""

    id: int
    project_id: int
    filename: str
    duration: float | None = None
    status: str
    processing_progress: int = 0
    current_stage: str | None = None
    stage_progress: int | None = None
    created_at: datetime
    # Media metadata — populated at ingest; surfaced here so list views can show
    # resolution / file size / bitrate instead of placeholder "— · ?x?".
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: int | None = None
    filesize: int | None = None

    model_config = {"from_attributes": True}


class VideoListResponse(BaseModel):
    """Paginated list of videos."""

    videos: list[VideoListItem]
    total: int = Field(..., description="Total number of videos matching the query")
    offset: int = Field(0, description="Current offset")
    limit: int = Field(50, description="Page size")


# ---------------------------------------------------------------------------
# Processing Schemas
# ---------------------------------------------------------------------------

class ProcessingStatusResponse(BaseModel):
    """Current processing status for a video."""

    video_id: int = Field(..., description="Video ID being processed")
    status: str = Field(..., description="Processing status: pending, processing, completed, failed")
    progress: int = Field(0, description="Processing progress 0-100", ge=0, le=100)
    step: str | None = Field(None, description="Current processing step name")
    stage: str | None = Field(None, description="Machine-readable active stage key (e.g. 'transcription')")
    stage_progress: int | None = Field(None, description="0-100 progress within the current stage; null = indeterminate")
    error_message: str | None = Field(None, description="Error message if status is 'failed'")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "video_id": 1,
                    "status": "processing",
                    "progress": 45,
                    "step": "transcription",
                    "error_message": None,
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Clip Schemas
# ---------------------------------------------------------------------------

class ClipEditOut(BaseModel):
    """A labelled per-language AI edit of a clip."""

    id: int
    clip_id: int
    label: str
    language: str
    output_path: str | None = None
    thumbnail_path: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ClipResponse(BaseModel):
    """Clip in a list view."""

    id: int
    video_id: int
    clip_number: int
    start_time: float
    end_time: float
    duration: float
    achieved_start_time: float | None = None
    achieved_end_time: float | None = None
    total_score: float | None = None
    title: str | None = None
    description: str | None = None
    hook_sentence: str | None = None
    virality_reason: str | None = None
    status: str
    output_path: str | None = None
    edited_output_path: str | None = None
    audio_mode: str | None = "keep_original"
    vocals_gain: float | None = 1.0
    music_gain: float | None = 1.0
    replacement_track: str | None = None
    created_at: datetime
    edits: list[ClipEditOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class ClipListResponse(BaseModel):
    """Paginated list of clips."""

    clips: list[ClipResponse]
    total: int = Field(..., description="Total number of clips matching the query")
    offset: int = 0
    limit: int = 50


class ClipDetailResponse(BaseModel):
    """Full clip detail with subtitles, thumbnails, and upload history."""
    id: int
    video_id: int
    clip_number: int
    start_time: float
    end_time: float
    duration: float
    achieved_start_time: float | None = None
    achieved_end_time: float | None = None
    total_score: float | None = None
    score_breakdown_json: dict[str, Any] | None = None
    title: str | None = None
    description: str | None = None
    hashtags: str | None = None
    keywords: str | None = None
    hook_sentence: str | None = None
    virality_reason: str | None = None
    status: str
    output_path: str | None = None
    edited_output_path: str | None = None
    audio_mode: str | None = "keep_original"
    vocals_gain: float | None = 1.0
    music_gain: float | None = 1.0
    replacement_track: str | None = None

    crop_data_json: list[Any] | None = None

    created_at: datetime
    updated_at: datetime | None = None
    subtitles: list[SubtitleSchema] = Field(default_factory=list)
    thumbnails: list[ThumbnailSchema] = Field(default_factory=list)
    uploads: list[UploadSchema] = Field(default_factory=list)
    edits: list[ClipEditOut] = Field(default_factory=list)
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# AI Auto-Edit Schemas
# ---------------------------------------------------------------------------

class AutoEditOptions(BaseModel):
    """Optional overrides for the minimal AI auto-edit pipeline.

    The editor is intentionally stripped down (2026-09-09 request): it only
    burns subtitles and generates a thumbnail. No intro, color grading, glow,
    or AI metadata.
    """

    with_captions: bool = Field(
        True,
        description="Burn subtitles across the entire clip (timed to speech)",
    )
    subtitle_language: str = Field(
        "en",
        description="Subtitle language: 'en' keeps the original audio language, "
        "'zh' translates all on-screen subtitles to Simplified Chinese",
    )
    # ── Audio Remix (Phase 3) ─────────────────────────────────────────────
    audio_mode: str = Field(
        "keep_original",
        description="Audio mix: 'keep_original' (default) leaves the clip's own "
        "audio untouched; 'mute_music' removes the background music while keeping "
        "speech; 'replace_music' swaps in your own music track",
    )
    vocals_gain: float = Field(
        1.0,
        description="Loudness multiplier applied to the speech/vocals (1.0 = no change)",
    )
    music_gain: float = Field(
        1.0,
        description="Loudness multiplier applied to the background music (1.0 = no change)",
    )
    replacement_track: str | None = Field(
        None,
        description="Absolute path to YOUR OWN music file (song/royalty-free) used "
        "when audio_mode='replace_music'. Must exist locally.",
    )
    # ── Subtitle STYLE (Part A) ───────────────────────────────────────────
    # Applied to the real caption burn (drawtext path) so the live preview in
    # the editor matches the rendered output.
    subtitle_style: str = Field(
        "fancy",
        description="Subtitle style preset: 'fancy' (karaoke glow), 'normal' "
        "(plain clean), 'bold' (big heavy captions)",
    )
    caption_font: str | None = Field(
        None,
        description="Caption font family name (e.g. 'Arial'). Mapped to a Windows "
        "font path on the backend; must be in the confirmed-available list.",
    )
    caption_color: str | None = Field(
        None,
        description="Caption/accent text colour as '#RRGGBB'.",
    )
    caption_highlight: str | None = Field(
        None,
        description="Highlight/accent colour for karaoke effects as '#RRGGBB'.",
    )
    caption_size_scale: float = Field(
        1.0,
        description="Font-size multiplier for captions (0.6–1.8). Applied on top "
        "of the auto-fit size so long lines still stay on-canvas.",
    )
    caption_position: str = Field(
        "bottom",
        description="Vertical caption position: 'bottom' | 'top' | 'center'.",
    )
    caption_outline: bool = Field(
        True,
        description="Draw a strong outline/drop shadow behind caption text.",
    )
    # ── Thumbnail STYLE (Part B) ──────────────────────────────────────────
    thumbnail_style: dict | None = Field(
        None,
        description="Thumbnail override flags forwarded to the thumbnail engine. "
        "Keys: 'template' ('full_bleed'|'minimal'), 'headline' (override text), "
        "'frame_index' (override the auto-picked frame), plus compositor design "
        "tokens like 'cta_show'/'cta_fill'.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "with_captions": True,
                    "subtitle_language": "en",
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# AI Voice-Over (Dubbing) Schemas
# ---------------------------------------------------------------------------

class VoiceInfo(BaseModel):
    """A single neural voice offered for a language."""

    id: str = Field(..., description="edge-tts voice tag, e.g. 'en-US-JennyNeural'")
    label: str = Field(..., description="Human-readable display label")
    gender: str = Field(..., description="'male' or 'female'")


class VoiceLanguage(BaseModel):
    """A supported language with its selectable voices."""

    code: str = Field(..., description="ISO code: en | ko | hi | ja | zh")
    name: str = Field(..., description="Human-readable language name")
    flag: str = Field(..., description="Emoji flag for the UI")
    default_voice: str = Field(..., description="Recommended default voice tag")
    voices: list[VoiceInfo] = Field(default_factory=list)


class VoicesResponse(BaseModel):
    """Catalog of supported languages and neural voices."""

    languages: list[VoiceLanguage] = Field(default_factory=list)


class DubRequest(BaseModel):
    """Request to generate a multilingual AI voice-over for a clip."""

    clip_id: int = Field(..., description="ID of the clip to dub")
    language: str = Field(
        ...,
        description="Target language: 'en' | 'ko' | 'hi' | 'ja' | 'zh'",
    )
    voice: str | None = Field(
        None,
        description="edge-tts voice tag; defaults to the language default",
    )
    mode: str = Field(
        "replace",
        description="'replace' swaps audio, 'mix' layers voice-over on top",
    )
    mix_volume: float = Field(
        1.0,
        ge=0.0,
        le=3.0,
        description="Loudness of the voice-over when mode='mix'",
    )
    translate: bool = Field(
        True,
        description="Attempt to translate the script to the target language",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "clip_id": 3,
                    "language": "ko",
                    "voice": "ko-KR-SunHiNeural",
                    "mode": "replace",
                    "mix_volume": 1.0,
                    "translate": True,
                }
            ]
        }
    }


class VoiceOverSchema(BaseModel):
    """A persisted AI voice-over render."""

    id: int
    clip_id: int
    language: str
    voice: str
    mode: str
    status: str
    source_text: str | None = None
    translated_text: str | None = None
    translated: int = 0
    audio_path: str | None = None
    output_path: str | None = None
    error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class VoiceOverListResponse(BaseModel):
    """List of voice-overs for a clip."""

    voiceovers: list[VoiceOverSchema] = Field(default_factory=list)
    total: int = 0


class DubResponse(BaseModel):
    """Response after generating a voice-over."""

    id: int
    clip_id: int
    language: str
    voice: str
    mode: str
    translated: bool = False
    output_path: str | None = None
    audio_path: str | None = None
    status: str = "completed"
    message: str = "Voice-over generated successfully."


# ---------------------------------------------------------------------------
# Publishing Schemas
# ---------------------------------------------------------------------------

class PublishRequest(BaseModel):
    """Request to publish a clip to a platform."""

    clip_id: int = Field(..., description="ID of the clip to publish")
    platform: str = Field(
        ...,
        description="Target platform: youtube, facebook, tiktok, instagram",
    )
    scheduled_at: datetime | None = Field(
        None,
        description="Optional scheduled publish time (ISO 8601)",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"clip_id": 1, "platform": "youtube", "scheduled_at": None}]
        }
    }


class PublishResponse(BaseModel):
    """Response after creating a publish job."""

    id: int = Field(..., description="Upload record ID")
    clip_id: int
    platform: str
    status: str
    scheduled_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Analytics Schemas
# ---------------------------------------------------------------------------

class AnalyticsResponse(BaseModel):
    """Dashboard aggregate statistics."""

    total_videos: int = Field(0, description="Total uploaded videos")
    total_clips: int = Field(0, description="Total generated clips")
    completed_clips: int = Field(0, description="Clips in completed status")
    published_uploads: int = Field(0, description="Successfully published uploads")
    total_projects: int = Field(0, description="Total projects")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "total_videos": 12,
                    "total_clips": 48,
                    "completed_clips": 42,
                    "published_uploads": 15,
                    "total_projects": 3,
                }
            ]
        }
    }


# ---------------------------------------------------------------------------
# Project Schemas
# ---------------------------------------------------------------------------

class ProjectCreate(BaseModel):
    """Request to create a new project."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Project name",
    )
    description: str | None = Field(None, max_length=2000, description="Optional description")

    model_config = {
        "json_schema_extra": {"examples": [{"name": "My Podcast Clips", "description": "Weekly podcast highlights"}]}
    }


class ProjectResponse(BaseModel):
    """Single project."""

    id: int
    user_id: int
    name: str
    description: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    """Paginated list of projects."""

    projects: list[ProjectResponse]
    total: int = Field(..., description="Total number of projects")
    offset: int = 0
    limit: int = 50


# ---------------------------------------------------------------------------
# Settings Schemas
# ---------------------------------------------------------------------------

class SettingsResponse(BaseModel):
    """All user settings as key-value pairs."""

    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value mapping of user settings",
    )


class SettingsUpdateRequest(BaseModel):
    """Request to update one or more settings."""

    settings: dict[str, Any] = Field(
        ...,
        description="Key-value pairs to create or update",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "settings": {
                        "theme": "dark",
                        "default_clip_duration": 30,
                        "auto_generate_subtitles": True,
                    }
                }
            ]
        }
    }
