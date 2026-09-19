"""
AIClipper Storage Routes

Storage-usage reporting and the deletion / cleanup surface for reclaiming
disk space.  Every destructive action in this module removes real files from
disk (not just database rows) and, for the bulk/orphan/temp sweepers, insists
the caller pass ``confirm=True`` so a client dialog has to deliberately opt in.

Category semantics (mirrors the on-disk layout):
  * source uploads  -> settings.upload_dir
  * clips / outputs -> settings.output_dir
  * thumbnails      -> settings.thumbnail_dir
  * subtitles       -> settings.subtitle_dir
  * temp            -> settings.temp_dir
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.api.deps import get_db
from backend.database import crud
from backend.database.models import Clip, ClipEdit, Video, VideoStatus
from backend.utils.config import PROJECT_ROOT, get_settings
from backend.utils.logging import get_logger

logger = get_logger("api.storage")

router = APIRouter(tags=["Storage"])


# ── Helpers ────────────────────────────────────────────────────────────


def _dir_size(d: Path) -> int:
    if not d.exists() or not d.is_dir():
        return 0
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file())


def _resolve(p: str | None) -> Path | None:
    """Resolve a stored (possibly relative) path against the project root."""
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _unlink_quiet(path: Path) -> bool:
    if path and path.exists():
        try:
            path.unlink()
            return True
        except OSError as exc:
            logger.warning(f"Failed to delete {path}: {exc}")
    return False


def _ensure_confirmed(confirm: bool) -> None:
    """Reject a destructive bulk/cleanup action unless explicitly confirmed."""
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Confirmation required: pass confirm=true to run this "
            "destructive action.",
        )


async def _load_video_for_delete(db: AsyncSession, video_id: int) -> Video | None:
    """Load a video with every derived artifact needed for a full delete."""
    result = await db.execute(
        select(Video)
        .options(
            selectinload(Video.clips).selectinload(Clip.subtitles),
            selectinload(Video.clips).selectinload(Clip.thumbnails),
            selectinload(Video.clips).selectinload(Clip.edits),
        )
        .where(Video.id == video_id)
    )
    return result.scalars().first()


def _all_derived_files(video: Video) -> list[Path]:
    """Return every source+derived file path owned by a video."""
    files = [_resolve(video.filepath)]
    for clip in video.clips or []:
        files.append(_resolve(clip.output_path))
        files.append(_resolve(clip.edited_output_path))
        for t in clip.thumbnails or []:
            files.append(_resolve(t.filepath))
        for s in clip.subtitles or []:
            files.append(_resolve(s.filepath))
        for e in clip.edits or []:
            files.append(_resolve(e.output_path))
            files.append(_resolve(e.thumbnail_path))
    return [f for f in files if f is not None]


# ── Schemas ────────────────────────────────────────────────────────────


class StorageUsage(BaseModel):
    source_uploads: int = Field(..., description="Bytes in the uploads/source dir")
    clips_outputs: int = Field(..., description="Bytes in the outputs/clips dir")
    thumbnails: int = Field(..., description="Bytes in the thumbnails dir")
    subtitles: int = Field(..., description="Bytes in the subtitles dir")
    temp: int = Field(..., description="Bytes in the temp dir")
    total: int = Field(..., description="Total bytes summed across categories")


class ConfirmRequest(BaseModel):
    confirm: bool = Field(default=False, description="Must be true to run")


class VideoDeleteRequest(BaseModel):
    mode: str = Field(..., description="'source' or 'everything'")
    confirm: bool = Field(default=False, description="Must be true to run")


class DeleteResult(BaseModel):
    message: str
    removed_files: int = Field(0, description="Files unlinked from disk")
    kept_clips: int = Field(0, description="Clips kept (source-only delete)")
    deleted_video: bool = Field(False)


class OrphanEntry(BaseModel):
    path: str
    size: int


class OrphanReport(BaseModel):
    orphans: list[OrphanEntry]
    total_size: int = Field(0)


class CleanupResult(BaseModel):
    message: str
    freed_bytes: int = Field(0)
    removed_items: int = Field(0)


# ── Storage usage ──────────────────────────────────────────────────────


@router.get(
    "/api/storage/usage",
    response_model=StorageUsage,
    summary="Report storage usage by category",
    description="Return the total bytes used per storage category and overall.",
)
async def storage_usage() -> StorageUsage:
    s = get_settings()
    source = _dir_size(s.upload_dir)
    clips = _dir_size(s.output_dir)
    thumbs = _dir_size(s.thumbnail_dir)
    subs = _dir_size(s.subtitle_dir)
    temp = _dir_size(s.temp_dir)
    return StorageUsage(
        source_uploads=source,
        clips_outputs=clips,
        thumbnails=thumbs,
        subtitles=subs,
        temp=temp,
        total=source + clips + thumbs + subs + temp,
    )


# ── Per-video delete (source-only vs everything) ───────────────────────


@router.post(
    "/api/videos/{video_id}/delete",
    response_model=DeleteResult,
    summary="Delete a video (source only, or everything)",
    description="mode='source' removes just the large source file and keeps all "
    "clips/thumbnails/subtitles; mode='everything' removes the source AND every "
    "derived file AND the database rows. Requires confirm=true.",
)
async def delete_video(
    video_id: int,
    body: VideoDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> DeleteResult:
    _ensure_confirmed(body.confirm)
    if body.mode not in ("source", "everything"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="mode must be 'source' or 'everything'.",
        )

    video = await _load_video_for_delete(db, video_id)
    if video is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video with id {video_id} not found.",
        )

    if body.mode == "source":
        # Remove only the large source file.  Derived clips/thumbnails/subtitles
        # (the valuable, small output) and the video row are all kept.
        removed = 0
        src = _resolve(video.filepath)
        if _unlink_quiet(src):
            removed += 1
        kept = len(video.clips or [])
        logger.info(
            f"Deleted source file only for video {video_id}: {src} "
            f"(kept {kept} clips)"
        )
        return DeleteResult(
            message=f"Deleted source file only; kept {kept} clip(s).",
            removed_files=removed,
            kept_clips=kept,
            deleted_video=False,
        )

    # mode == "everything"
    files = _all_derived_files(video)
    removed = 0
    for f in files:
        if _unlink_quiet(f):
            removed += 1
    deleted = await crud.delete_video(db, video_id)  # cascade deletes child rows
    logger.info(
        f"Deleted video {video_id} and everything derived from it "
        f"({removed} files, video row deleted={deleted})"
    )
    return DeleteResult(
        message=f"Deleted video and {removed} derived file(s). This cannot be undone.",
        removed_files=removed,
        deleted_video=deleted,
    )


# ── Orphaned-file scan + cleanup ───────────────────────────────────────


def _orphan_paths(db_files: set[Path]) -> list[Path]:
    """Files on disk with no corresponding DB row (crash/bug leftovers).

    Scans the source/output/thumbnail/subtitle dirs (not temp, which is
    intentionally transient and handled separately).  ``db_files`` must be the
    set of *resolved* absolute paths already known to the database.
    """
    s = get_settings()
    trackers = [s.upload_dir, s.output_dir, s.thumbnail_dir, s.subtitle_dir]
    orphans: list[Path] = []
    for base in trackers:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f not in db_files and not f.name.endswith((".part", ".ytdl")):
                orphans.append(f)
    return sorted(set(orphans), key=lambda p: str(p))


async def _db_file_set(db: AsyncSession) -> set[Path]:
    """Resolve every DB-referenced path to an absolute Path for orphan checks."""
    raw = await crud.get_db_referenced_files(db)
    out: set[Path] = set()
    for p in raw:
        r = _resolve(p)
        if r is not None:
            out.add(r)
    return out


@router.get(
    "/api/storage/orphans",
    response_model=OrphanReport,
    summary="Scan for orphaned files",
    description="Return files present on disk with no matching database row.",
)
async def list_orphans(db: AsyncSession = Depends(get_db)) -> OrphanReport:
    db_files = await _db_file_set(db)
    items, total = [], 0
    for p in _orphan_paths(db_files):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        items.append(OrphanEntry(path=str(p), size=size))
        total += size
    return OrphanReport(orphans=items, total_size=total)


@router.post(
    "/api/storage/orphans/cleanup",
    response_model=CleanupResult,
    summary="Delete orphaned files",
    description="Delete every orphaned file reported by /api/storage/orphans. "
    "Requires confirm=true.",
)
async def cleanup_orphans(
    body: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> CleanupResult:
    _ensure_confirmed(body.confirm)
    paths = _orphan_paths(await _db_file_set(db))
    freed, removed = 0, 0
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if _unlink_quiet(p):
            freed += size
            removed += 1
    logger.info(f"Orphan cleanup removed {removed} files ({freed} bytes)")
    return CleanupResult(
        message=f"Removed {removed} orphaned file(s).",
        freed_bytes=freed,
        removed_items=removed,
    )


# ── Bulk cleanup: pending videos + temp files ─────────────────────────


@router.post(
    "/api/storage/videos/pending-cleanup",
    response_model=CleanupResult,
    summary="Delete all pending (unprocessed) videos",
    description="Permanently delete every video that was uploaded but never "
    "processed, and everything derived from it. Requires confirm=true.",
)
async def cleanup_pending_videos(
    body: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> CleanupResult:
    _ensure_confirmed(body.confirm)
    result = await db.execute(
        select(Video).where(Video.status.in_([VideoStatus.PENDING, VideoStatus.FAILED]))
    )
    pending = result.scalars().all()
    freed, removed, videos_removed = 0, 0, 0
    for video in pending:
        loaded = await _load_video_for_delete(db, video.id)
        files = _all_derived_files(loaded) if loaded else []
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                size = 0
            if _unlink_quiet(f):
                freed += size
                removed += 1
        if await crud.delete_video(db, video.id):
            videos_removed += 1
    await db.flush()
    logger.info(
        f"Pending-video cleanup: removed {videos_removed} videos, "
        f"{removed} files ({freed} bytes)"
    )
    return CleanupResult(
        message=f"Deleted {videos_removed} pending video(s) and {removed} file(s).",
        freed_bytes=freed,
        removed_items=videos_removed,
    )


@router.post(
    "/api/storage/temp-cleanup",
    response_model=CleanupResult,
    summary="Clear temporary files",
    description="Delete everything in the temp working directory. Requires "
    "confirm=true.",
)
async def clear_temp(body: ConfirmRequest) -> CleanupResult:
    _ensure_confirmed(body.confirm)
    temp_dir = get_settings().temp_dir
    freed, removed = 0, 0
    if temp_dir.exists():
        for f in temp_dir.rglob("*"):
            if f.is_file():
                try:
                    size = f.stat().st_size
                    f.unlink()
                    freed += size
                    removed += 1
                except OSError:
                    pass
    logger.info(f"Temp cleanup removed {removed} files ({freed} bytes)")
    return CleanupResult(
        message=f"Cleared {removed} temp file(s).",
        freed_bytes=freed,
        removed_items=removed,
    )


@router.get(
    "/api/storage/video-count-pending",
    summary="Count pending videos",
    description="Return how many videos are pending/unprocessed (for the UI).",
)
async def count_pending(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    result = await db.execute(
        select(Video).where(Video.status.in_([VideoStatus.PENDING, VideoStatus.FAILED]))
    )
    return {"pending": len(result.scalars().all())}
