"""
AIClipper Database Models

SQLAlchemy ORM models for all application tables.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class VideoStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ClipStatus(str, enum.Enum):
    PENDING = "pending"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    SCHEDULED = "scheduled"
    FAILED = "failed"


class SubtitleFormat(str, enum.Enum):
    SRT = "srt"
    VTT = "vtt"
    BURNED = "burned"


class VoiceOverStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class Platform(str, enum.Enum):
    YOUTUBE = "youtube"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"


class ProjectStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    projects = relationship("Project", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("Setting", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username='{self.username}')>"


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(ProjectStatus), default=ProjectStatus.ACTIVE, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="projects")
    videos = relationship("Video", back_populates="project", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Project(id={self.id}, name='{self.name}')>"


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(500), nullable=False)
    filepath = Column(String(1000), nullable=False)
    duration = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    fps = Column(Float, nullable=True)
    codec = Column(String(50), nullable=True)
    audio_codec = Column(String(50), nullable=True)
    bitrate = Column(Integer, nullable=True)
    filesize = Column(Integer, nullable=True)
    format_name = Column(String(50), nullable=True)
    status = Column(Enum(VideoStatus), default=VideoStatus.PENDING, nullable=False, index=True)
    processing_progress = Column(Integer, default=0)  # 0-100 percentage
    processing_step = Column(String(100), nullable=True)  # Current step name
    current_stage = Column(String(50), nullable=True)  # Machine-readable active stage key (e.g. "transcription")
    stage_progress = Column(Integer, nullable=True)    # 0-100 progress within the current stage; NULL=indeterminate
    source_url = Column(String(2000), nullable=True)  # Original shareable URL (YouTube/FB)
    source_video_id = Column(String(200), nullable=True, index=True)  # Platform source ID (yt-dlp id) extracted from URL, for dedup
    content_hash = Column(String(64), nullable=True)  # SHA-256 of the source file bytes, for upload dedup
    content_type = Column(String(50), nullable=True)   # podcast, interview, tutorial, vlog, etc.
    content_density = Column(String(20), nullable=True) # low, medium, high
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    project = relationship("Project", back_populates="videos")
    transcripts = relationship("Transcript", back_populates="video", cascade="all, delete-orphan")
    scenes = relationship("Scene", back_populates="video", cascade="all, delete-orphan")
    clips = relationship("Clip", back_populates="video", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Video(id={self.id}, filename='{self.filename}', status={self.status})>"


class Transcript(Base):
    __tablename__ = "transcripts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    language = Column(String(10), nullable=False, default="en")
    content_json = Column(JSON, nullable=True)          # Full transcript segments
    word_timestamps_json = Column(JSON, nullable=True)  # Word-level timestamps
    full_text = Column(Text, nullable=True)              # Plain text transcript
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    video = relationship("Video", back_populates="transcripts")

    def __repr__(self) -> str:
        return f"<Transcript(id={self.id}, video_id={self.video_id}, lang='{self.language}')>"


class Scene(Base):
    __tablename__ = "scenes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    scene_number = Column(Integer, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)
    score = Column(Float, nullable=True)
    metadata_json = Column(JSON, nullable=True)  # Additional scene metadata
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    video = relationship("Video", back_populates="scenes")

    def __repr__(self) -> str:
        return f"<Scene(id={self.id}, {self.start_time:.1f}s-{self.end_time:.1f}s)>"


class Clip(Base):
    __tablename__ = "clips"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True)
    clip_number = Column(Integer, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)
    # ACTUAL start/end the rendered clip lands on in the source video. The
    # pipeline cuts with ``-c copy`` (input seeking), so FFmpeg snaps to the
    # LAST KEYFRAME at or before ``start_time``, making the real clip start a
    # little EARLIER than requested.  We rebase subtitles against THIS value,
    # never the requested one, or captions land early by the keyframe offset.
    achieved_start_time = Column(Float, nullable=True)
    achieved_end_time = Column(Float, nullable=True)
    total_score = Column(Float, nullable=True)
    score_breakdown_json = Column(JSON, nullable=True)   # Per-factor scores
    output_path = Column(String(1000), nullable=True)     # ORIGINAL clip render (never overwritten by edits)
    edited_output_path = Column(String(1000), nullable=True)  # Edits are saved as a SEPARATE copy here
    status = Column(Enum(ClipStatus), default=ClipStatus.PENDING, nullable=False, index=True)
    title = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    hashtags = Column(Text, nullable=True)
    keywords = Column(Text, nullable=True)
    hook_sentence = Column(Text, nullable=True)           # Best opening line to stop scrolling
    virality_reason = Column(Text, nullable=True)         # Why this clip should go viral
    crop_data_json = Column(JSON, nullable=True)         # Face tracking crop coordinates
    # ── Audio Remix (PHASE 1/3) ─────────────────────────────────────────
    audio_mode = Column(String(20), nullable=True)       # keep_original | mute_music | replace_music
    vocals_gain = Column(Float, default=1.0, nullable=True)
    music_gain = Column(Float, default=1.0, nullable=True)
    replacement_track = Column(String(1000), nullable=True)  # local royalty-free track path
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    video = relationship("Video", back_populates="clips")
    subtitles = relationship("Subtitle", back_populates="clip", cascade="all, delete-orphan")
    thumbnails = relationship("Thumbnail", back_populates="clip", cascade="all, delete-orphan")
    uploads = relationship("Upload", back_populates="clip", cascade="all, delete-orphan")
    voiceovers = relationship("VoiceOver", back_populates="clip", cascade="all, delete-orphan")
    edits = relationship(
        "ClipEdit",
        back_populates="clip",
        cascade="all, delete-orphan",
        order_by="ClipEdit.id",
    )

    def __repr__(self) -> str:
        return f"<Clip(id={self.id}, {self.start_time:.1f}s-{self.end_time:.1f}s, score={self.total_score})>"


class Subtitle(Base):
    __tablename__ = "subtitles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clip_id = Column(Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True)
    format = Column(Enum(SubtitleFormat), nullable=False)
    filepath = Column(String(1000), nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    clip = relationship("Clip", back_populates="subtitles")

    def __repr__(self) -> str:
        return f"<Subtitle(id={self.id}, format={self.format})>"


class ClipEdit(Base):
    """A single labelled AI-edit of a clip (e.g. "Edit 1 - English").

    Each edit is a SEPARATE file from the original (clips.output_path is the
    pristine render and is NEVER overwritten). Multiple edits (one per subtitle
    language) can coexist for the same clip, each with its own burned-in
    subtitles, thumbnail and a user-facing label like "Edit 1 - English".
    """

    __tablename__ = "clip_edits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clip_id = Column(Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(120), nullable=False, default="Edit")  # "Edit 1 - English", "Edit 2 - 中文"
    language = Column(String(10), nullable=False, default="en")  # 'en' or 'zh'
    output_path = Column(String(1000), nullable=False)           # relative: outputs\edited_clip_7_en.mp4
    thumbnail_path = Column(String(1000), nullable=True)         # relative: thumbnails\edited_thumb_7_en.jpg
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    clip = relationship("Clip", back_populates="edits")

    def __repr__(self) -> str:
        return f"<ClipEdit(id={self.id}, clip_id={self.clip_id}, label='{self.label}')>"


class Thumbnail(Base):
    __tablename__ = "thumbnails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clip_id = Column(Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True)
    filepath = Column(String(1000), nullable=False)
    score = Column(Float, nullable=True)
    format = Column(String(10), nullable=False, default="jpg")  # png, jpg
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    is_selected = Column(Integer, default=0)  # Boolean (SQLite doesn't have bool)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    # Relationships
    clip = relationship("Clip", back_populates="thumbnails")

    def __repr__(self) -> str:
        return f"<Thumbnail(id={self.id}, score={self.score}, selected={self.is_selected})>"


class Upload(Base):
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clip_id = Column(Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True)
    platform = Column(Enum(Platform), nullable=False)
    status = Column(Enum(UploadStatus), default=UploadStatus.PENDING, nullable=False, index=True)
    platform_video_id = Column(String(200), nullable=True)   # Platform-specific ID
    url = Column(String(1000), nullable=True)                  # Published URL
    scheduled_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    metadata_json = Column(JSON, nullable=True)                # Platform-specific metadata
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    clip = relationship("Clip", back_populates="uploads")

    def __repr__(self) -> str:
        return f"<Upload(id={self.id}, platform={self.platform}, status={self.status})>"


class VoiceOver(Base):
    """
    A multilingual AI voice-over (dubbed) render of a clip.

    Records the translation + synthesised speech + final muxed video produced
    by :mod:`backend.services.voiceover`.
    """

    __tablename__ = "voiceovers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    clip_id = Column(Integer, ForeignKey("clips.id", ondelete="CASCADE"), nullable=False, index=True)
    language = Column(String(10), nullable=False)                # en | ko | hi | ja | zh
    voice = Column(String(100), nullable=False)                  # edge-tts voice tag
    mode = Column(String(20), nullable=False, default="replace")  # replace | mix
    status = Column(Enum(VoiceOverStatus), default=VoiceOverStatus.PENDING, nullable=False, index=True)
    source_text = Column(Text, nullable=True)              # Original clip transcript text
    translated_text = Column(Text, nullable=True)           # Translated audio script
    translated = Column(Integer, default=0)                 # 1 if translation succeeded
    audio_path = Column(String(1000), nullable=True)         # Synthesised speech file
    output_path = Column(String(1000), nullable=True)        # Final dubbed video
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    clip = relationship("Clip", back_populates="voiceovers")

    def __repr__(self) -> str:
        return f"<VoiceOver(id={self.id}, clip={self.clip_id}, lang='{self.language}')>"


class Setting(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(200), nullable=False)
    value_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="settings")

    # Unique constraint on (user_id, key)
    __table_args__ = (
        UniqueConstraint('user_id', 'key', name='uq_user_setting_key'),
        {"sqlite_autoincrement": True},
    )

    def __repr__(self) -> str:
        return f"<Setting(id={self.id}, key='{self.key}')>"
