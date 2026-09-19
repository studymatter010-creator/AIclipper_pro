"""
AIClipper CRUD Operations

Database create, read, update, delete operations for all models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.models import (
    Clip,
    ClipEdit,
    ClipStatus,
    Platform,
    Project,
    ProjectStatus,
    Scene,
    Setting,
    Subtitle,
    SubtitleFormat,
    Thumbnail,
    Transcript,
    Upload,
    UploadStatus,
    User,
    Video,
    VideoStatus,
    VoiceOver,
    VoiceOverStatus,
)


# ===========================================================================
# User CRUD
# ===========================================================================

async def create_user(session: AsyncSession, username: str, email: str | None = None) -> User:
    user = User(username=username, email=email)
    session.add(user)
    await session.flush()
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    result = await session.execute(select(User).where(User.username == username))
    return result.scalars().first()


async def get_or_create_default_user(session: AsyncSession) -> User:
    """Get the default user, creating if needed (single-user mode)."""
    user = await get_user_by_username(session, "admin")
    if user is None:
        user = await create_user(session, "admin", "admin@localhost")
    return user


# ===========================================================================
# Project CRUD
# ===========================================================================

async def create_project(
    session: AsyncSession, user_id: int, name: str, description: str | None = None
) -> Project:
    project = Project(user_id=user_id, name=name, description=description)
    session.add(project)
    await session.flush()
    return project


async def get_project(session: AsyncSession, project_id: int) -> Project | None:
    return await session.get(Project, project_id)


async def list_projects(
    session: AsyncSession,
    user_id: int | None = None,
    status: ProjectStatus | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Sequence[Project]:
    query = select(Project).order_by(Project.created_at.desc())
    if user_id is not None:
        query = query.where(Project.user_id == user_id)
    if status is not None:
        query = query.where(Project.status == status)
    query = query.offset(offset).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


async def update_project(
    session: AsyncSession, project_id: int, **kwargs: Any
) -> Project | None:
    project = await get_project(session, project_id)
    if project:
        for key, value in kwargs.items():
            setattr(project, key, value)
        await session.flush()
    return project


# ===========================================================================
# Video CRUD
# ===========================================================================

async def create_video(
    session: AsyncSession,
    project_id: int,
    filename: str,
    filepath: str,
    **metadata: Any,
) -> Video:
    video = Video(
        project_id=project_id,
        filename=filename,
        filepath=filepath,
        **metadata,
    )
    session.add(video)
    await session.flush()
    return video


async def get_video(session: AsyncSession, video_id: int) -> Video | None:
    return await session.get(Video, video_id)


async def get_video_with_relations(session: AsyncSession, video_id: int) -> Video | None:
    result = await session.execute(
        select(Video)
        .options(
            selectinload(Video.clips).selectinload(Clip.subtitles),
            selectinload(Video.clips).selectinload(Clip.thumbnails),
            selectinload(Video.clips).selectinload(Clip.uploads),
            selectinload(Video.transcripts),
            selectinload(Video.scenes),
        )
        .where(Video.id == video_id)
    )
    return result.scalars().first()


async def list_videos(
    session: AsyncSession,
    project_id: int | None = None,
    status: VideoStatus | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Sequence[Video]:
    query = select(Video).order_by(Video.created_at.desc())
    if project_id is not None:
        query = query.where(Video.project_id == project_id)
    if status is not None:
        query = query.where(Video.status == status)
    query = query.offset(offset).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


_UNSET = object()


async def update_video_status(
    session: AsyncSession,
    video_id: int,
    status: VideoStatus,
    progress: int | None = None,
    step: str | None = None,
    error: str | None = None,
    stage: str | None = None,
    stage_progress: int | None = _UNSET,
) -> None:
    values: dict[str, Any] = {"status": status}
    if progress is not None:
        values["processing_progress"] = progress
    if step is not None:
        values["processing_step"] = step
    if error is not None:
        values["error_message"] = error
    if stage is not None:
        values["current_stage"] = stage
    # stage_progress is written whenever explicitly passed (even NULL, to clear a
    # previously-set value); _UNSET means "leave the DB value untouched".
    if stage_progress is not _UNSET:
        values["stage_progress"] = stage_progress
    await session.execute(update(Video).where(Video.id == video_id).values(**values))


async def update_video(session: AsyncSession, video_id: int, **kwargs: Any) -> Video | None:
    """Update arbitrary fields on a video row (e.g. content_type, content_density)."""
    video = await get_video(session, video_id)
    if video:
        for key, value in kwargs.items():
            setattr(video, key, value)
        await session.flush()
    return video


async def get_videos_by_hash(session: AsyncSession, content_hash: str) -> Sequence[Video]:
    """Return rows whose source file matched ``content_hash`` (upload dedup)."""
    result = await session.execute(
        select(Video).where(Video.content_hash == content_hash)
    )
    return result.scalars().all()


async def get_video_by_source_id(session: AsyncSession, source_video_id: str) -> Video | None:
    """Return the newest row imported from platform source id (URL dedup)."""
    result = await session.execute(
        select(Video)
        .where(Video.source_video_id == source_video_id)
        .order_by(Video.id.desc())
    )
    return result.scalars().first()


async def delete_video(session: AsyncSession, video_id: int) -> bool:
    """Delete a video row and (via ORM cascade) all its derived rows.

    DB-only — callers are responsible for removing files from disk.  Returns
    True if a row was deleted, False if it didn't exist.
    """
    result = await session.execute(delete(Video).where(Video.id == video_id))
    return bool(result.rowcount)


async def get_db_referenced_files(session: AsyncSession) -> set[str]:
    """Return every file-path string referenced by any DB row.

    Used by the orphan-file scan: any file on disk under a managed storage dir
    whose path is NOT in this set (after path resolution) is an orphan.  Paths
    are returned exactly as stored (possibly relative); callers resolve them
    against PROJECT_ROOT before comparing with real disk files.

    Covers: ``videos.filepath``, ``clips.output_path``/``edited_output_path``,
    ``clip_edits.output_path``/``thumbnail_path``, ``subtitles.filepath``,
    ``thumbnails.filepath``, and ``voiceovers.audio_path``/``output_path``.
    """
    referenced: set[str] = set()
    queries = [
        select(Video.filepath),
        select(Clip.output_path),
        select(Clip.edited_output_path),
        select(ClipEdit.output_path),
        select(ClipEdit.thumbnail_path),
        select(Subtitle.filepath),
        select(Thumbnail.filepath),
        select(VoiceOver.audio_path),
        select(VoiceOver.output_path),
    ]
    for stmt in queries:
        try:
            result = await session.execute(stmt)
        except Exception:  # noqa: BLE001 — a missing model/column must not crash the scan
            continue
        for (p,) in result.all():
            if p:  # skip NULL / empty
                referenced.add(p)
    return referenced


# ===========================================================================
# Transcript CRUD
# ===========================================================================

async def create_transcript(
    session: AsyncSession,
    video_id: int,
    language: str,
    content_json: dict | list | None = None,
    word_timestamps_json: dict | list | None = None,
    full_text: str | None = None,
) -> Transcript:
    transcript = Transcript(
        video_id=video_id,
        language=language,
        content_json=content_json,
        word_timestamps_json=word_timestamps_json,
        full_text=full_text,
    )
    session.add(transcript)
    await session.flush()
    return transcript


async def get_transcript_for_video(session: AsyncSession, video_id: int) -> Transcript | None:
    result = await session.execute(
        select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.id.desc())
    )
    return result.scalars().first()


# ===========================================================================
# Scene CRUD
# ===========================================================================

async def create_scenes_batch(
    session: AsyncSession,
    video_id: int,
    scenes_data: list[dict[str, Any]],
) -> list[Scene]:
    scenes = []
    for i, data in enumerate(scenes_data):
        scene = Scene(
            video_id=video_id,
            scene_number=i + 1,
            start_time=data["start"],
            end_time=data["end"],
            duration=data["end"] - data["start"],
            score=data.get("score"),
            metadata_json=data.get("metadata"),
        )
        session.add(scene)
        scenes.append(scene)
    await session.flush()
    return scenes


async def get_scenes_for_video(session: AsyncSession, video_id: int) -> Sequence[Scene]:
    result = await session.execute(
        select(Scene).where(Scene.video_id == video_id).order_by(Scene.start_time)
    )
    return result.scalars().all()


# ===========================================================================
# Clip CRUD
# ===========================================================================

async def create_clip(
    session: AsyncSession,
    video_id: int,
    clip_number: int,
    start_time: float,
    end_time: float,
    total_score: float | None = None,
    score_breakdown: dict | None = None,
) -> Clip:
    clip = Clip(
        video_id=video_id,
        clip_number=clip_number,
        start_time=start_time,
        end_time=end_time,
        duration=end_time - start_time,
        total_score=total_score,
        score_breakdown_json=score_breakdown,
    )
    session.add(clip)
    await session.flush()
    return clip


async def get_clip(session: AsyncSession, clip_id: int) -> Clip | None:
    return await session.get(Clip, clip_id)


async def get_clip_with_relations(session: AsyncSession, clip_id: int) -> Clip | None:
    result = await session.execute(
        select(Clip)
        .options(
            selectinload(Clip.subtitles),
            selectinload(Clip.thumbnails),
            selectinload(Clip.uploads),
            selectinload(Clip.edits),
        )
        .where(Clip.id == clip_id)
    )
    return result.scalars().first()


async def list_clips(
    session: AsyncSession,
    video_id: int | None = None,
    status: ClipStatus | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Sequence[Clip]:
    query = (
        select(Clip)
        .options(selectinload(Clip.edits))
        .order_by(Clip.total_score.desc().nullslast())
    )
    if video_id is not None:
        query = query.where(Clip.video_id == video_id)
    if status is not None:
        query = query.where(Clip.status == status)
    query = query.offset(offset).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


async def update_clip(session: AsyncSession, clip_id: int, **kwargs: Any) -> Clip | None:
    clip = await get_clip(session, clip_id)
    if clip:
        for key, value in kwargs.items():
            setattr(clip, key, value)
        await session.flush()
    return clip


async def delete_clip(session: AsyncSession, clip_id: int) -> bool:
    result = await session.execute(delete(Clip).where(Clip.id == clip_id))
    return result.rowcount > 0


# ===========================================================================
# ClipEdit CRUD (labelled per-language AI edits of a clip)
# ===========================================================================

async def create_clip_edit(
    session: AsyncSession,
    clip_id: int,
    label: str,
    language: str,
    output_path: str,
    thumbnail_path: str | None = None,
) -> ClipEdit:
    edit = ClipEdit(
        clip_id=clip_id,
        label=label,
        language=language,
        output_path=output_path,
        thumbnail_path=thumbnail_path,
    )
    session.add(edit)
    await session.flush()
    return edit


async def list_clip_edits(session: AsyncSession, clip_id: int) -> Sequence[ClipEdit]:
    result = await session.execute(
        select(ClipEdit).where(ClipEdit.clip_id == clip_id).order_by(ClipEdit.id)
    )
    return result.scalars().all()


# ===========================================================================
# Subtitle CRUD
# ===========================================================================

async def create_subtitle(
    session: AsyncSession,
    clip_id: int,
    format: SubtitleFormat,
    filepath: str,
) -> Subtitle:
    subtitle = Subtitle(clip_id=clip_id, format=format, filepath=filepath)
    session.add(subtitle)
    await session.flush()
    return subtitle


async def get_subtitles_for_clip(session: AsyncSession, clip_id: int) -> Sequence[Subtitle]:
    result = await session.execute(
        select(Subtitle).where(Subtitle.clip_id == clip_id)
    )
    return result.scalars().all()


# ===========================================================================
# Thumbnail CRUD
# ===========================================================================

async def create_thumbnail(
    session: AsyncSession,
    clip_id: int,
    filepath: str,
    score: float | None = None,
    format: str = "jpg",
    width: int | None = None,
    height: int | None = None,
    is_selected: bool = False,
) -> Thumbnail:
    thumb = Thumbnail(
        clip_id=clip_id,
        filepath=filepath,
        score=score,
        format=format,
        width=width,
        height=height,
        is_selected=1 if is_selected else 0,
    )
    session.add(thumb)
    await session.flush()
    return thumb


async def get_selected_thumbnail(session: AsyncSession, clip_id: int) -> Thumbnail | None:
    result = await session.execute(
        select(Thumbnail)
        .where(Thumbnail.clip_id == clip_id, Thumbnail.is_selected == 1)
    )
    return result.scalars().first()


# ===========================================================================
# VoiceOver CRUD
# ===========================================================================

async def create_voiceover(
    session: AsyncSession,
    clip_id: int,
    language: str,
    voice: str,
    mode: str = "replace",
    source_text: str | None = None,
) -> VoiceOver:
    vo = VoiceOver(
        clip_id=clip_id,
        language=language,
        voice=voice,
        mode=mode,
        source_text=source_text,
        status=VoiceOverStatus.PENDING,
    )
    session.add(vo)
    await session.flush()
    return vo


async def get_voiceover(session: AsyncSession, voiceover_id: int) -> VoiceOver | None:
    return await session.get(VoiceOver, voiceover_id)


async def get_voiceovers_for_clip(
    session: AsyncSession, clip_id: int
) -> Sequence[VoiceOver]:
    result = await session.execute(
        select(VoiceOver)
        .where(VoiceOver.clip_id == clip_id)
        .order_by(VoiceOver.created_at.desc())
    )
    return result.scalars().all()


async def update_voiceover(
    session: AsyncSession, voiceover_id: int, **kwargs: Any
) -> VoiceOver | None:
    vo = await get_voiceover(session, voiceover_id)
    if vo:
        for key, value in kwargs.items():
            setattr(vo, key, value)
        await session.flush()
    return vo


async def delete_voiceover(session: AsyncSession, voiceover_id: int) -> bool:
    result = await session.execute(delete(VoiceOver).where(VoiceOver.id == voiceover_id))
    return result.rowcount > 0


# ===========================================================================
# Upload CRUD
# ===========================================================================

async def create_upload(
    session: AsyncSession,
    clip_id: int,
    platform: Platform,
    scheduled_at: datetime | None = None,
) -> Upload:
    upload = Upload(
        clip_id=clip_id,
        platform=platform,
        scheduled_at=scheduled_at,
    )
    session.add(upload)
    await session.flush()
    return upload


async def update_upload(session: AsyncSession, upload_id: int, **kwargs: Any) -> Upload | None:
    upload = await session.get(Upload, upload_id)
    if upload:
        for key, value in kwargs.items():
            setattr(upload, key, value)
        await session.flush()
    return upload


async def list_uploads(
    session: AsyncSession,
    clip_id: int | None = None,
    platform: Platform | None = None,
    status: UploadStatus | None = None,
    offset: int = 0,
    limit: int = 50,
) -> Sequence[Upload]:
    query = select(Upload).order_by(Upload.created_at.desc())
    if clip_id is not None:
        query = query.where(Upload.clip_id == clip_id)
    if platform is not None:
        query = query.where(Upload.platform == platform)
    if status is not None:
        query = query.where(Upload.status == status)
    query = query.offset(offset).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()


# ===========================================================================
# Settings CRUD
# ===========================================================================

async def get_setting(session: AsyncSession, user_id: int, key: str) -> Any:
    result = await session.execute(
        select(Setting).where(Setting.user_id == user_id, Setting.key == key)
    )
    setting = result.scalars().first()
    return setting.value_json if setting else None


async def get_setting_any(session: AsyncSession, key: str) -> Any:
    """Read a setting value regardless of which user owns it.

    Used by background pipeline code (e.g. auto-delete-source) that has no
    request/user context.  In this effectively single-user app any owner is
    fine.  Returns ``None`` if no row exists for the key.
    """
    result = await session.execute(
        select(Setting)
        .where(Setting.key == key)
        .order_by(Setting.id.desc())
    )
    setting = result.scalars().first()
    return setting.value_json if setting else None


async def get_intro_setting(session: AsyncSession) -> tuple[bool, float]:
    """Resolve the "Add thumbnail as intro frame" toggle + duration.

    Returns ``(enabled, duration_seconds)`` with defaults ``(False, 0.8)``.
    Handles both a real bool/float and the JSON-string form ("true"/"0.8")
    depending on how the frontend serialized the value.
    """
    on_raw = await get_setting_any(session, "intro_frame")
    dur_raw = await get_setting_any(session, "intro_duration")

    # Default OFF when unset (user must opt in).
    if on_raw is None:
        enabled = False
    elif isinstance(on_raw, bool):
        enabled = on_raw
    else:
        enabled = str(on_raw).strip().lower() in ("1", "true", "yes", "on")

    try:
        duration = float(dur_raw) if dur_raw not in (None, "") else 0.8
    except (TypeError, ValueError):
        duration = 0.8

    return enabled, duration


async def set_setting(session: AsyncSession, user_id: int, key: str, value: Any) -> Setting:
    result = await session.execute(
        select(Setting).where(Setting.user_id == user_id, Setting.key == key)
    )
    setting = result.scalars().first()
    if setting:
        setting.value_json = value
    else:
        setting = Setting(user_id=user_id, key=key, value_json=value)
        session.add(setting)
    await session.flush()
    return setting


async def get_all_settings(session: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await session.execute(
        select(Setting).where(Setting.user_id == user_id)
    )
    settings = result.scalars().all()
    return {s.key: s.value_json for s in settings}


# ===========================================================================
# Dashboard Stats
# ===========================================================================

async def get_dashboard_stats(session: AsyncSession) -> dict[str, Any]:
    """Get aggregate statistics for the dashboard."""
    video_count = await session.scalar(select(func.count(Video.id)))
    clip_count = await session.scalar(select(func.count(Clip.id)))
    completed_clips = await session.scalar(
        select(func.count(Clip.id)).where(Clip.status == ClipStatus.COMPLETED)
    )
    upload_count = await session.scalar(
        select(func.count(Upload.id)).where(Upload.status == UploadStatus.PUBLISHED)
    )
    project_count = await session.scalar(select(func.count(Project.id)))

    return {
        "total_videos": video_count or 0,
        "total_clips": clip_count or 0,
        "completed_clips": completed_clips or 0,
        "published_uploads": upload_count or 0,
        "total_projects": project_count or 0,
    }
