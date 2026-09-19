"""
AIClipper Database Engine

Async SQLAlchemy engine and session factory for SQLite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.utils.config import get_settings
from backend.utils.logging import get_logger

logger = get_logger("database.engine")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Get or create the SQLAlchemy async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.app_debug,
            pool_pre_ping=True,
            # SQLite-specific: enable WAL mode for better concurrency
            connect_args={"check_same_thread": False},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that yields a database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database sessions (for non-FastAPI usage)."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _ensure_column_if_missing_sync(sync_conn, table: str, column: str, ddl: str) -> None:
    """Add ``column`` to ``table`` if it doesn't already exist (lightweight migration).

    Must be called inside ``conn.run_sync()`` with a *synchronous* connection.
    """
    rows = sync_conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    has_col = any(row[1] == column for row in rows)
    if not has_col:
        sync_conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# The semantic schema for clip_edits (used only if it is missing on an older DB).
_CLIP_EDITS_DDL = (
    "CREATE TABLE IF NOT EXISTS clip_edits ("
    "id INTEGER NOT NULL PRIMARY KEY, "
    "clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE, "
    "label VARCHAR(120) NOT NULL DEFAULT 'Edit', "
    "language VARCHAR(10) NOT NULL DEFAULT 'en', "
    "output_path VARCHAR(1000) NOT NULL, "
    "thumbnail_path VARCHAR(1000), "
    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL"
    ")"
)


def _ensure_table_if_missing_sync(sync_conn, table: str, ddl: str) -> None:
    """Create ``table`` if it doesn't already exist (lightweight migration).

    Must be called inside ``conn.run_sync()`` with a *synchronous* connection.
    """
    rows = sync_conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchall()
    if not rows:
        sync_conn.exec_driver_sql(ddl)


def _backfill_clip_edits_sync(sync_conn) -> None:
    """Create a labelled ClipEdit row for every clip that was edited on an older
    schema (before the ``clip_edits`` table existed), so those edits show up as
    "Edit 1 - English / …" rather than only being reachable via the legacy
    ``clips.edited_output_path`` pointer.

    Safe to run repeatedly: skips clips that already have an edit row, and skips
    clips whose old edit is already represented by a per-language file.
    """
    sync_conn.exec_driver_sql(_CLIP_EDITS_DDL)
    rows = sync_conn.exec_driver_sql(
        "SELECT c.id, c.edited_output_path FROM clips c "
        "WHERE c.edited_output_path IS NOT NULL AND c.edited_output_path <> ''"
    ).fetchall()
    for clip_id, edited_path in rows:
        have = sync_conn.exec_driver_sql(
            "SELECT count(*) FROM clip_edits WHERE clip_id=?", (clip_id,)
        ).fetchone()
        if have[0] > 0:
            continue
        # Derive the subtitle language from the filename suffix if present.
        # e.g. edited_clip_73_zh.mp4 -> zh ; edited_clip_59.mp4 -> en.
        lp = str(edited_path).lower()
        stem = lp.split("/")[-1].split("\\")[-1]
        lang = "zh" if stem.startswith("edited_clip_") and ("_zh." in stem or "_zh.mp4" in stem) else "en"
        label = f"Edit 1 - {'Chinese' if lang == 'zh' else 'English'}"
        sync_conn.exec_driver_sql(
            "INSERT OR IGNORE INTO clip_edits "
            "(clip_id, label, language, output_path, created_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (clip_id, label, lang, edited_path),
        )
        logger.info(f"Backfilled clip_edits for clip {clip_id}: {label}")


async def init_db() -> None:
    """Initialize the database, creating all tables and applying light migrations."""
    from backend.database.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight additive migrations for columns added after v1.0.
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "source_url", "VARCHAR(2000)")
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "content_type", "VARCHAR(50)")
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "content_density", "VARCHAR(20)")
        # Per-stage pipeline progress (added with the stage-progress feature).
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "current_stage", "VARCHAR(50)")
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "stage_progress", "INTEGER")
        # Upload-dedup (storage management): content hash for file uploads,
        # platform source-ID for URL imports.
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "source_video_id", "VARCHAR(200)")
        await conn.run_sync(_ensure_column_if_missing_sync, "videos", "content_hash", "VARCHAR(64)")
        # Idempotent indexes for dedup lookups (survive on pre-existing DBs where
        # the ALTER above adds the columns but create_all already ran).
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_videos_content_hash ON videos(content_hash)"
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_videos_source_video_id ON videos(source_video_id)"
        )
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "hook_sentence", "TEXT")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "virality_reason", "TEXT")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "edited_output_path", "VARCHAR(1000)")
        # Audio Remix (PHASE 1/3): stem-mode + per-track gains persisted per clip.
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "audio_mode", "VARCHAR(20)")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "vocals_gain", "FLOAT")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "music_gain", "FLOAT")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "replacement_track", "VARCHAR(1000)")
        # Actual keyframe-snapped clip bounds (Part A: correct subtitle rebasing).
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "achieved_start_time", "FLOAT")
        await conn.run_sync(_ensure_column_if_missing_sync, "clips", "achieved_end_time", "FLOAT")
        # clip_edits is created by Base.metadata.create_all above; this guard
        # plus backfill lets older databases gain the new multi-edit rows.
        await conn.run_sync(_ensure_table_if_missing_sync, "clip_edits", _CLIP_EDITS_DDL)
        await conn.run_sync(_backfill_clip_edits_sync)


async def close_db() -> None:
    """Close the database engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
