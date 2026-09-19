"""
AIClipper Video Routes

Endpoints for uploading, listing, and retrieving video details.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_config, get_current_user, get_db
from backend.api.schemas import (
    ErrorResponse,
    ImportUrlRequest,
    VideoDetail,
    VideoListItem,
    VideoListResponse,
    VideoUploadResponse,
)
from backend.database import crud
from backend.database.models import User, Video
from backend.utils.config import Settings
from backend.utils.logging import get_logger
from backend.utils.validators import ValidationError, validate_video_file

logger = get_logger("api.videos")

router = APIRouter(tags=["Videos"])


def _sha256_of_file(path: Path) -> str:
    """Compute the SHA-256 of a file's bytes (runs once per upload/import)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


async def _existing_for_source(
    db: AsyncSession,
    *,
    content_hash: str | None = None,
    source_video_id: str | None = None,
):
    """Return a usable existing video row for dedup, or None.

    A match only counts if its stored source file still exists on disk —
    otherwise (e.g. the file was manually deleted) treat it as a fresh import
    rather than pointing at a path that no longer resolves.
    """
    candidates = []
    if content_hash:
        candidates.extend(await crud.get_videos_by_hash(db, content_hash))
    if source_video_id:
        hit = await crud.get_video_by_source_id(db, source_video_id)
        if hit:
            candidates.append(hit)
    for v in candidates:
        try:
            if v.filepath and Path(v.filepath).exists():
                return v
        except OSError:
            continue
    return None


@router.post(
    "/api/upload",
    response_model=VideoUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Validation error"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Upload a video file",
    description="Upload a video file for processing. The file is validated for format, size, "
    "and integrity before being saved.",
)
async def upload_video(
    file: UploadFile,
    project_id: int | None = Query(None, description="Optional Project ID"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    config: Settings = Depends(get_config),
) -> VideoUploadResponse:
    """Accept a video upload, validate it, save to disk, and create a DB record."""
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No filename provided.",
        )

    # Generate a unique filename to prevent collisions
    original_name = file.filename
    ext = Path(original_name).suffix.lower()
    safe_name = f"{uuid.uuid4().hex}{ext}"
    save_path = config.upload_dir / safe_name

    logger.info(f"Receiving upload: {original_name} -> {safe_name}")

    # ── 1. Stream the bytes to disk ───────────────────────────────────────
    try:
        async with aiofiles.open(save_path, "wb") as out_file:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                await out_file.write(chunk)
    except OSError as exc:
        logger.error(f"Failed to save upload to {save_path}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save uploaded file.",
        )

    # ── 2. Validate + hash the saved file ─────────────────────────────────
    try:
        metadata = validate_video_file(save_path, original_name)
    except ValidationError as exc:
        # Clean up the invalid file
        save_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        )
    content_hash = _sha256_of_file(save_path)

    # ── 3. Dedup: same bytes already uploaded? ────────────────────────────
    # If an existing Video row stores the same content (and its file still
    # exists on disk), do NOT keep a second copy — drop the one we just wrote
    # and point the caller at the existing record instead.
    existing = await _existing_for_source(db, content_hash=content_hash)
    if existing is not None:
        save_path.unlink(missing_ok=True)  # don't store the duplicate
        logger.info(
            f"Upload duplicate detected (hash {content_hash[:12]}...) — "
            f"reusing existing video id={existing.id}, no second file saved"
        )
        return VideoUploadResponse.model_validate(
            existing, update={"deduped": True}
        )

    # Ensure a default project exists for the user if project_id not provided
    if project_id is None:
        projects = await crud.list_projects(db, user_id=user.id, limit=1)
        if projects:
            project_id = projects[0].id
        else:
            project = await crud.create_project(db, user_id=user.id, name="Default Project")
            project_id = project.id

    # Create the video record with its content hash for future dedup checks
    video = await crud.create_video(
        db,
        project_id=project_id,
        filename=original_name,
        filepath=str(save_path),
        content_hash=content_hash,
        **metadata,
    )

    logger.info(
        f"Video uploaded: id={video.id}, file={original_name}, size={metadata.get('filesize')}",
        extra={"video_id": video.id},
    )

    return VideoUploadResponse.model_validate(video)


@router.get(
    "/api/videos",
    response_model=VideoListResponse,
    summary="List uploaded videos",
    description="Return a paginated list of uploaded videos, most recent first.",
)
async def list_videos(
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    project_id: int | None = Query(None, description="Filter by project ID"),
    db: AsyncSession = Depends(get_db),
) -> VideoListResponse:
    """Return paginated video listing."""
    # Count total
    count_query = select(func.count(Video.id))
    if project_id is not None:
        count_query = count_query.where(Video.project_id == project_id)
    total = await db.scalar(count_query) or 0

    videos = await crud.list_videos(db, project_id=project_id, offset=offset, limit=limit)
    items = [VideoListItem.model_validate(v) for v in videos]

    return VideoListResponse(videos=items, total=total, offset=offset, limit=limit)


@router.get(
    "/api/videos/{video_id}",
    response_model=VideoDetail,
    responses={404: {"model": ErrorResponse}},
    summary="Get video detail",
    description="Return full video detail including clips, transcripts, and scenes.",
)
async def get_video_detail(
    video_id: int,
    db: AsyncSession = Depends(get_db),
) -> VideoDetail:
    """Retrieve a video with all related data."""
    video = await crud.get_video_with_relations(db, video_id)
    if video is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video with id {video_id} not found.",
        )
    return VideoDetail.model_validate(video)


@router.post(
    "/api/import-url",
    response_model=VideoUploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid URL or download failed"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
    },
    summary="Import video from a URL",
    description="Download a video from a shareable YouTube/Facebook URL and create "
    "a video record, optionally starting AI processing automatically.",
)
async def import_video_from_url(
    body: ImportUrlRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    config: Settings = Depends(get_config),
) -> VideoUploadResponse:
    """Download a video from ``body.url`` and register it as a video."""
    from backend.services.downloader import (
        DownloadError,
        detect_platform,
        download_video,
        extract_source_id,
    )

    if not body.url.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL is required.",
        )

    platform = detect_platform(body.url)
    if platform == "other":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only YouTube and Facebook video URLs are supported.",
        )

    # ── 1. Dedup: same source already imported? ───────────────────────────
    # Resolve the platform source-ID from the URL BEFORE any download and reuse
    # the existing stored file if we've already pulled this source.  This saves
    # bandwidth AND avoids a duplicate copy on disk.  Only counts if the stored
    # file still exists (otherwise it's a fresh import / re-download).
    source_video_id = extract_source_id(body.url)
    existing = None
    if source_video_id:
        existing = await _existing_for_source(db, source_video_id=source_video_id)
    if existing is not None:
        logger.info(
            f"URL import duplicate detected (source_id={source_video_id}) — "
            f"reusing existing video id={existing.id}, no re-download"
        )
        return VideoUploadResponse.model_validate(
            existing, update={"deduped": True}
        )

    # ── 2. Download via yt-dlp ───────────────────────────────────────────
    try:
        result = await asyncio.to_thread(download_video, body.url.strip())
    except DownloadError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    save_path = Path(result["file_path"])

    # ── 2. Validate the downloaded file ──────────────────────────────────
    try:
        metadata = validate_video_file(save_path, result["filename"])
    except ValidationError as exc:
        save_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        )

    # ── 3. Ensure a project exists ───────────────────────────────────────
    project_id = body.project_id
    if project_id is None:
        projects = await crud.list_projects(db, user_id=user.id, limit=1)
        if projects:
            project_id = projects[0].id
        else:
            project = await crud.create_project(db, user_id=user.id, name="Default Project")
            project_id = project.id

    # ── 4. Create the video record ───────────────────────────────────────
    display_name = result.get("title") or result["filename"]
    video = await crud.create_video(
        db,
        project_id=project_id,
        filename=display_name,
        filepath=str(save_path),
        source_url=body.url.strip(),
        source_video_id=(result.get("source_video_id") or source_video_id),
        content_hash=_sha256_of_file(save_path),
        **metadata,
    )

    logger.info(
        f"Video imported from URL: id={video.id}, platform={platform}, "
        f"title='{display_name[:60]}'",
        extra={"video_id": video.id},
    )

    # ── 5. Auto-start processing if requested ────────────────────────────
    if body.auto_process and video.status.value not in ("processing", "completed"):
        from backend.api.routes.processing import start_processing

        try:
            await start_processing(
                video_id=video.id, db=db,
                clip_count=body.clip_count, clip_duration=body.clip_duration,
            )
        except Exception as exc:  # noqa: BLE001 — processing is best-effort
            logger.warning(f"Auto-processing for imported video failed: {exc}")

    return VideoUploadResponse.model_validate(video)
