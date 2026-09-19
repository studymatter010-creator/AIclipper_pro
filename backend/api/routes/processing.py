"""
AIClipper Processing Routes

Endpoints for starting video processing, polling status, and
receiving real-time progress over WebSocket.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.api.schemas import ErrorResponse, ProcessOptions, ProcessingStatusResponse
from backend.database import crud
from backend.database.models import VideoStatus
from backend.utils.logging import get_logger

logger = get_logger("api.processing")

router = APIRouter(tags=["Processing"])


@router.post(
    "/api/process/{video_id}",
    response_model=ProcessingStatusResponse,
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse, "description": "Video already processing or completed"},
    },
    summary="Start processing a video",
    description="Kick off the AI processing pipeline for a video. "
    "Currently sets the status to PROCESSING; the actual pipeline "
    "integration will be added later.",
)
async def start_processing(
    video_id: int,
    body: ProcessOptions | None = None,
    db: AsyncSession = Depends(get_db),
    clip_count: int | None = None,
    clip_duration: int | None = None,
) -> ProcessingStatusResponse:
    """Start the AI processing pipeline for a video."""
    video = await crud.get_video(db, video_id)
    if video is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video with id {video_id} not found.",
        )

    if video.status == VideoStatus.PROCESSING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video is already being processed.",
        )

    # Resolve clip tuning options (query params override body values).
    if body is not None:
        clip_count = clip_count or body.clip_count
        clip_duration = clip_duration or body.clip_duration

    await crud.update_video_status(
        db,
        video_id=video_id,
        status=VideoStatus.PROCESSING,
        progress=0,
        step="queued",
    )

    logger.info(
        f"Processing started for video {video_id} "
        f"(clip_count={clip_count}, clip_duration={clip_duration})",
        extra={"video_id": video_id},
    )

    # Launch the pipeline as a background task
    asyncio.create_task(_run_pipeline_background(video_id, clip_count, clip_duration))

    return ProcessingStatusResponse(
        video_id=video_id,
        status=VideoStatus.PROCESSING.value,
        progress=0,
        step="queued",
    )


async def _run_pipeline_background(
    video_id: int,
    clip_count: int | None = None,
    clip_duration: int | None = None,
) -> None:
    """Run the pipeline in the background, catching all exceptions."""
    try:
        from backend.services.pipeline import process_video_pipeline
        result = await process_video_pipeline(
            video_id,
            clip_count=clip_count,
            clip_duration=clip_duration,
        )
        logger.info(f"Background pipeline completed for video {video_id}: {result.get('clips_generated', 0)} clips")
    except Exception as e:
        logger.error(f"Background pipeline failed for video {video_id}: {e}", exc_info=True)


@router.get(
    "/api/status/{video_id}",
    response_model=ProcessingStatusResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get processing status",
    description="Return the current processing progress for a video.",
)
async def get_processing_status(
    video_id: int,
    db: AsyncSession = Depends(get_db),
) -> ProcessingStatusResponse:
    """Return the latest processing status from the DB."""
    video = await crud.get_video(db, video_id)
    if video is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Video with id {video_id} not found.",
        )

    return ProcessingStatusResponse(
        video_id=video.id,
        status=video.status.value if isinstance(video.status, VideoStatus) else str(video.status),
        progress=video.processing_progress or 0,
        step=video.processing_step,
        stage=video.current_stage,
        stage_progress=video.stage_progress,
        error_message=video.error_message,
    )


# ---------------------------------------------------------------------------
# WebSocket — Real-time progress
# ---------------------------------------------------------------------------

@router.websocket("/api/ws/progress/{video_id}")
async def ws_progress(
    websocket: WebSocket,
    video_id: int,
) -> None:
    """
    WebSocket endpoint that pushes processing progress for a given video.

    The server polls the database every 2 seconds and pushes a JSON message
    with the current status. The connection closes automatically when
    processing is COMPLETED or FAILED, or if the client disconnects.
    """
    await websocket.accept()
    logger.info(f"WebSocket connected for video {video_id}", extra={"video_id": video_id})

    try:
        while True:
            # Open a fresh session for each poll to see committed changes
            async for db in get_db():
                video = await crud.get_video(db, video_id)

            if video is None:
                await websocket.send_json({"error": f"Video {video_id} not found"})
                break

            current_status = (
                video.status.value if isinstance(video.status, VideoStatus) else str(video.status)
            )

            payload = {
                "video_id": video.id,
                "status": current_status,
                "progress": video.processing_progress or 0,
                "step": video.processing_step,
                "stage": video.current_stage,
                "stage_progress": video.stage_progress,
                "error_message": video.error_message,
            }

            await websocket.send_json(payload)

            # Stop polling when terminal state is reached
            if current_status in ("completed", "failed"):
                break

            await asyncio.sleep(2)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for video {video_id}", extra={"video_id": video_id})
    except Exception as exc:
        logger.error(
            f"WebSocket error for video {video_id}: {exc}",
            extra={"video_id": video_id},
            exc_info=True,
        )
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# Re-export get_db so the WS handler can use it without circular import issues
from backend.api.deps import get_db  # noqa: E402 — already imported at top; kept for clarity
