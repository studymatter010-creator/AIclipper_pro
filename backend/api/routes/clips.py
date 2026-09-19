"""
AIClipper Clip Routes

Endpoints for listing, viewing, deleting, and downloading generated clips.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.api.schemas import (
    AutoEditOptions,
    ClipDetailResponse,
    ClipListResponse,
    ClipResponse,
    ErrorResponse,
)
from backend.database import crud
from backend.database.models import Clip
from backend.utils.config import PROJECT_ROOT
from backend.utils.logging import get_logger

logger = get_logger("api.clips")


def resolve_clip_path(p: str | None) -> Path | None:
    """Resolve a stored (possibly relative) clip/thumb path against the project root.

    ``output_path`` / ``edited_output_path`` are stored relative to the project
    root (e.g. ``outputs\\clip_7_005.mp4``), so constructing ``Path()`` around
    them directly would resolve against the *current working directory*, which
    is fine when uvicorn is launched from the project root but breaks otherwise.
    Resolving against ``PROJECT_ROOT`` makes lookups robust in every case.
    """
    if not p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path

router = APIRouter(tags=["Clips"])


@router.get(
    "/api/clips",
    response_model=ClipListResponse,
    summary="List generated clips",
    description="Return a paginated list of clips, optionally filtered by video ID. "
    "Results are ordered by score descending.",
)
async def list_clips(
    video_id: int | None = Query(None, description="Filter clips by video ID"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(50, ge=1, le=200, description="Page size"),
    db: AsyncSession = Depends(get_db),
) -> ClipListResponse:
    """Return paginated clip listing."""
    count_query = select(func.count(Clip.id))
    if video_id is not None:
        count_query = count_query.where(Clip.video_id == video_id)
    total = await db.scalar(count_query) or 0

    clips = await crud.list_clips(db, video_id=video_id, offset=offset, limit=limit)
    items = [ClipResponse.model_validate(c) for c in clips]

    return ClipListResponse(clips=items, total=total, offset=offset, limit=limit)


@router.get(
    "/api/clips/{clip_id}",
    response_model=ClipDetailResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get clip detail",
    description="Return full clip detail with subtitles, thumbnails, and upload history.",
)
async def get_clip_detail(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
) -> ClipDetailResponse:
    """Retrieve a clip with all related data."""
    clip = await crud.get_clip_with_relations(db, clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} not found.",
        )
    return ClipDetailResponse.model_validate(clip)


@router.delete(
    "/api/clips/{clip_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse}},
    summary="Delete a clip",
    description="Delete a clip record and remove its output file from disk.",
)
async def delete_clip(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a clip and its associated file."""
    clip = await crud.get_clip_with_relations(db, clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} not found.",
        )

    # Remove the output files if they exist (original + edited copies)
    paths = [clip.output_path, clip.edited_output_path]
    paths += [e.output_path for e in clip.edits]
    for p in paths:
        f = resolve_clip_path(p)
        if f and f.exists():
            try:
                f.unlink()
                logger.info(f"Deleted clip file: {f}", extra={"clip_id": clip_id})
            except OSError as exc:
                logger.warning(
                    f"Failed to delete clip file {f}: {exc}",
                    extra={"clip_id": clip_id},
                )

    deleted = await crud.delete_clip(db, clip_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} could not be deleted.",
        )

    logger.info(f"Clip {clip_id} deleted", extra={"clip_id": clip_id})


class BulkDeleteRequest(BaseModel):
    """Body for the bulk clip-delete endpoint."""

    clip_ids: list[int] = Field(..., description="Clip IDs to delete")


@router.post(
    "/api/clips/bulk-delete",
    summary="Delete multiple clips",
    description="Delete several clips (records + their output files) in one "
                "request. Missing IDs are reported, not fatal.",
)
async def bulk_delete_clips(
    body: BulkDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete multiple clips and their associated files at once."""
    if not body.clip_ids:
        return {"deleted": 0, "not_found": [], "failed": []}

    deleted: list[int] = []
    missing: list[int] = []
    failed: list[int] = []

    for clip_id in body.clip_ids:
        clip = await crud.get_clip_with_relations(db, clip_id)
        if clip is None:
            missing.append(clip_id)
            continue

        # Remove the output files if they exist (original + edited copies)
        paths = [clip.output_path, clip.edited_output_path]
        paths += [e.output_path for e in clip.edits]
        for p in paths:
            f = resolve_clip_path(p)
            if f and f.exists():
                try:
                    f.unlink()
                except OSError as exc:
                    logger.warning(
                        f"Failed to delete clip file {f}: {exc}",
                        extra={"clip_id": clip_id},
                    )

        ok = await crud.delete_clip(db, clip_id)
        if ok:
            deleted.append(clip_id)
        else:
            failed.append(clip_id)

    logger.info(f"Bulk-deleted {len(deleted)} clips: {deleted}")
    return {
        "deleted": len(deleted),
        "deleted_ids": deleted,
        "not_found": missing,
        "failed": failed,
    }


@router.get(
    "/api/clips/{clip_id}/download",
    responses={
        404: {"model": ErrorResponse},
        200: {"content": {"video/mp4": {}}, "description": "Clip video file"},
    },
    summary="Download a clip",
    description="Serve the clip output file as a downloadable attachment.",
)
async def download_clip(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Serve the clip file for download."""
    clip = await crud.get_clip(db, clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} not found.",
        )

    # Prefer the edited version when it exists; fall back to the original.
    serve_path = clip.edited_output_path if clip.edited_output_path else clip.output_path
    output_file = resolve_clip_path(serve_path)
    if not output_file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clip output file has not been generated yet.",
        )
    if not output_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Clip output file not found on disk.",
        )

    download_name = f"clip_{clip.clip_number}_{clip.video_id}.mp4"

    return FileResponse(
        path=str(output_file),
        media_type="video/mp4",
        filename=download_name,
    )


@router.get(
    "/api/clips/{clip_id}/thumbnail",
    responses={
        404: {"model": ErrorResponse},
        200: {"content": {"image/jpeg": {}}, "description": "Clip thumbnail"},
    },
    summary="Download a clip thumbnail",
    description="Serve the clip's thumbnail image as a downloadable attachment.",
)
async def download_thumbnail(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """Serve the clip's thumbnail for download."""
    clip = await crud.get_clip_with_relations(db, clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} not found.",
        )

    # Find the thumbnail: prefer the latest edit's thumbnail, then the first
    # thumbnail in the thumbnails relationship, then the edited_output_path
    # derived thumbnail.
    thumb_path: str | None = None
    if clip.edits:
        latest_edit = clip.edits[-1]
        thumb_path = latest_edit.thumbnail_path
    if not thumb_path and clip.thumbnails:
        thumb_path = clip.thumbnails[0].filepath
    # Derive from edited_output_path as a last resort (e.g. edited_clip_73_en.mp4 → edited_thumb_73_en.jpg)
    if not thumb_path and clip.edited_output_path:
        edited_stem = Path(clip.edited_output_path).stem
        # Try to find the matching thumbnail on disk
        thumb_dir = resolve_clip_path(str(Path(clip.edited_output_path).parent.parent / "thumbnails"))
        if thumb_dir and thumb_dir.is_dir():
            for ext in (".jpg", ".jpeg", ".png"):
                candidate = thumb_dir / f"{edited_stem.replace('edited_clip', 'edited_thumb')}{ext}"
                if candidate.exists():
                    thumb_path = str(candidate)
                    break

    if not thumb_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No thumbnail found for this clip.",
        )

    thumb_file = resolve_clip_path(thumb_path)
    if not thumb_file or not thumb_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Thumbnail file not found on disk.",
        )

    download_name = f"thumbnail_{clip.clip_number}_{clip.video_id}.jpg"

    return FileResponse(
        path=str(thumb_file),
        media_type="image/jpeg",
        filename=download_name,
    )


@router.post("/api/clips/{clip_id}/auto-edit")
async def auto_edit_clip_endpoint(
    clip_id: int,
    options: AutoEditOptions | None = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Auto-edit a clip with a branded intro, animated word captions, color
    grading, thumbnail, and AI metadata. Accepts optional style overrides.
    """
    import asyncio

    # Verify clip exists
    clip = await crud.get_clip(db, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not clip.output_path or not Path(clip.output_path).exists():
        raise HTTPException(400, "Clip video file not found")

    # Run auto-edit in background thread
    from backend.services.auto_editor import auto_edit_clip
    opts = options.model_dump() if options else None
    result = await asyncio.to_thread(auto_edit_clip, clip_id, opts)

    return {"status": "success", "result": result}


@router.get("/api/clips/{clip_id}/thumbnail-candidates")
async def thumbnail_candidates_endpoint(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return 4-6 scored candidate frames for the thumbnail editor filmstrip.

    Runs the SAME deterministic candidate extraction + scoring the thumbnail
    engine uses (even sampling across the clip), so clicking a frame in the
    editor selects the exact same frame on render via ``thumbnail_style.frame_index``.
    Candidate frames travel as base64 data URIs so the filmstrip needs no second
    serving path.
    """
    import asyncio
    import base64

    clip = await crud.get_clip(db, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not clip.output_path or not Path(clip.output_path).exists():
        raise HTTPException(400, "Clip video file not found")

    def _compute() -> list[dict]:
        try:
            from backend.services.thumbnail.candidates import extract_candidates
            from backend.services.thumbnail.scoring import score_candidate
            from backend.utils.ffmpeg import get_video_duration
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Thumbnail candidate modules unavailable ({exc})")
            return []
        frames = extract_candidates(Path(clip.output_path))
        if not frames:
            return []
        duration = float(get_video_duration(Path(clip.output_path)) or 0.0)
        n = len(frames)
        step = duration / (n + 1) if duration > 0 else 0.0
        scored: list[dict] = []
        for idx, fp in enumerate(frames):
            comp, breakdown, _bbox = score_candidate(fp)
            try:
                data = base64.b64encode(fp.read_bytes()).decode("ascii")
            except Exception:  # noqa: BLE001
                data = ""
            scored.append(
                {
                    "index": idx,
                    "time": round(step * (idx + 1), 2),
                    "score": comp,
                    "breakdown": breakdown or {},
                    "data_uri": f"data:image/jpeg;base64,{data}" if data else "",
                    "best": False,
                }
            )
        scored.sort(key=lambda s: s["score"], reverse=True)
        if scored:
            scored[0]["best"] = True  # highest-scoring == the engine's auto pick
        return scored

    candidates_list = await asyncio.to_thread(_compute)
    return {
        "status": "success",
        "clip_id": clip_id,
        "candidates": candidates_list,
    }


@router.post("/api/clips/{clip_id}/music")
async def upload_clip_music(
    clip_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload YOUR OWN music track to use with this clip (replacement track for
    ``replace_music`` mode). Saves a copy beside the clip and records its path
    on the clip row so a later auto-edit can swap it in with one click.
    """
    import aiofiles

    clip = await crud.get_clip(db, clip_id)
    if not clip:
        raise HTTPException(404, "Clip not found")
    if not clip.output_path:
        raise HTTPException(400, "Clip has no output file")

    clip_dir = Path(clip.output_path).parent
    clip_dir.mkdir(parents=True, exist_ok=True)

    # Preserve the original extension so FFmpeg can sniff the format.
    original_name = Path(file.filename or "music.mp3").name
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "mp3"
    if ext not in ("mp3", "wav", "m4a", "aac", "flac", "ogg", "wma", "opus"):
        ext = "mp3"
    dest = clip_dir / f"music_{clip_id}.{ext}"

    content = await file.read()
    if not content:
        raise HTTPException(400, "Uploaded music file is empty")
    async with aiofiles.open(str(dest), "wb") as f:
        await f.write(content)

    await crud.update_clip(db, clip_id, replacement_track=str(dest))
    logger.info(f"Uploaded user music for clip {clip_id} -> {dest}")
    return {
        "status": "success",
        "replacement_track": str(dest),
        "message": "Your music is saved — pick 'Replace with your music' and hit Generate.",
    }
