"""
AIClipper Publishing Routes

Endpoints for publishing clips to social platforms and fetching analytics.

Publishing works in two phases:

1.  ``POST /api/publish`` — creates an ``Upload`` record (``PENDING``) and,
    once the clip is completed, immediately dispatches a background job that
    performs the real platform upload.
2.  ``POST /api/publish/batch/{video_id}`` — creates an ``Upload`` record for
    every completed clip of a video and dispatches background upload jobs.

All platform work runs inside ``asyncio.to_thread`` with its own event loop,
so blocking SDK calls (Google APIs, Graph API) never stall the FastAPI loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.api.schemas import (
    AnalyticsResponse,
    ErrorResponse,
    PublishRequest,
    PublishResponse,
)
from backend.database import crud
from backend.database.models import ClipStatus, Platform, UploadStatus
from backend.utils.logging import get_logger

logger = get_logger("api.publishing")

router = APIRouter(tags=["Publishing"])

# Map of accepted platform strings to Platform enum values
_PLATFORM_MAP = {p.value: p for p in Platform}


# ---------------------------------------------------------------------------
# Background upload helpers
# ---------------------------------------------------------------------------

def _youtube_upload_sync(
    clip_id: int,
    upload_id: int,
    output_path: str,
    title: str,
    description: str,
    tags: list[str],
    privacy: str,
) -> None:
    """
    Upload a single clip to YouTube inside a dedicated thread + event loop.

    This is a *synchronous* wrapper (safe for ``asyncio.to_thread``) that
    drives the async uploader, then records the outcome on the Upload row.
    """
    async def _run() -> None:
        from backend.services.uploaders.youtube import YouTubeUploader
        from backend.database.engine import get_session_context

        uploader = YouTubeUploader()
        try:
            authenticated = await uploader.authenticate({})
            if not authenticated:
                async with get_session_context() as session:
                    await crud.update_upload(
                        session, upload_id,
                        status=UploadStatus.FAILED,
                        error_message="YouTube is not authenticated. Add a "
                                      "client_secret.json and authorize once.",
                    )
                return

            result = await uploader.upload_video(
                Path(output_path), title, description, tags, privacy
            )

            async with get_session_context() as session:
                if result.success:
                    await crud.update_upload(
                        session, upload_id,
                        status=UploadStatus.PUBLISHED,
                        url=result.url,
                        platform_video_id=result.platform_video_id,
                        published_at=result.metadata.get("published_at") if result.metadata else None,
                    )
                else:
                    await crud.update_upload(
                        session, upload_id,
                        status=UploadStatus.FAILED,
                        error_message=result.error,
                    )
        except Exception as exc:  # noqa: BLE001 — surface any failure on the row
            logger.error(f"YouTube upload failed for clip {clip_id}: {exc}")
            try:
                async with get_session_context() as session:
                    await crud.update_upload(
                        session, upload_id,
                        status=UploadStatus.FAILED,
                        error_message=str(exc),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Could not persist upload failure")

    asyncio.run(_run())


def _dispatch_upload(upload_id: int, clip) -> None:
    """Spawn a background job that really uploads ``clip`` on the platform."""
    title = clip.title or f"Clip {clip.clip_number}"
    description = clip.description or ""
    tags = [t.strip().strip("#") for t in (clip.hashtags or "").split() if t.strip()]

    if upload_id is not None and clip.output_path:
        asyncio.create_task(
            asyncio.to_thread(
                _youtube_upload_sync,
                clip.id,
                upload_id,
                str(clip.output_path),
                title,
                description,
                tags,
                "public",
            )
        )
        logger.info(
            f"Dispatched YouTube upload for clip {clip.id} (upload={upload_id})"
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post(
    "/api/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid platform"},
        404: {"model": ErrorResponse, "description": "Clip not found"},
    },
    summary="Publish a clip",
    description="Create a publish / upload job for a clip on the specified "
                "platform and dispatch the background upload.",
)
async def publish_clip(
    body: PublishRequest,
    db: AsyncSession = Depends(get_db),
) -> PublishResponse:
    """Create an Upload record for a clip and kick off the real upload."""
    platform_enum = _PLATFORM_MAP.get(body.platform.lower())
    if platform_enum is None:
        accepted = ", ".join(sorted(_PLATFORM_MAP))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported platform '{body.platform}'. Accepted: {accepted}",
        )

    clip = await crud.get_clip(db, body.clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {body.clip_id} not found.",
        )

    upload = await crud.create_upload(
        db,
        clip_id=body.clip_id,
        platform=platform_enum,
        scheduled_at=body.scheduled_at,
    )

    logger.info(
        f"Upload record created: id={upload.id}, clip={body.clip_id}, platform={body.platform}",
        extra={"clip_id": body.clip_id},
    )

    # Dispatch a real background upload only for completed clips on YouTube.
    # Other platforms can be added by extending ``_dispatch_upload``.
    if (
        platform_enum == Platform.YOUTUBE
        and upload.id is not None
        and clip.status == ClipStatus.COMPLETED
        and clip.output_path
    ):
        _dispatch_upload(upload.id, clip)

    return PublishResponse.model_validate(upload)


@router.post(
    "/api/publish/batch/{video_id}",
    responses={404: {"model": ErrorResponse}},
    summary="Batch publish all clips to YouTube",
    description="Queue every completed clip of a video for YouTube Shorts upload.",
)
async def batch_publish_to_youtube(
    video_id: int,
    privacy: str = Query(default="public"),
    db: AsyncSession = Depends(get_db),
):
    """Create Upload records and dispatch upload jobs for every completed clip."""
    clips = await crud.list_clips(db, video_id=video_id, status=ClipStatus.COMPLETED)
    queued = 0

    for clip in clips:
        if not clip.output_path:
            continue

        upload = await crud.create_upload(
            db,
            clip_id=clip.id,
            platform=Platform.YOUTUBE,
        )
        if upload.id is not None:
            # TODO: thread the requested ``privacy`` through the platform map once
            # multi-platform batch upload is supported.
            _dispatch_upload(upload.id, clip)
            queued += 1

    return {"queued": queued, "message": f"Queued {queued} clip(s) for YouTube upload."}


@router.get(
    "/api/analytics",
    response_model=AnalyticsResponse,
    summary="Dashboard analytics",
    description="Return aggregate statistics for the dashboard: total videos, clips, "
                "completed clips, published uploads, and projects.",
)
async def get_analytics(
    db: AsyncSession = Depends(get_db),
) -> AnalyticsResponse:
    """Return dashboard-level aggregate stats."""
    stats = await crud.get_dashboard_stats(db)
    return AnalyticsResponse(**stats)