"""
AIClipper Huey Task Definitions

Background task queue using Huey with SQLite backend.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from huey import SqliteHuey

from backend.utils.config import PROJECT_ROOT
from backend.utils.logging import get_logger

logger = get_logger("workers")

# --- Huey instance (SQLite-backed, no Redis needed) ---
_db_path = PROJECT_ROOT / "data"
_db_path.mkdir(parents=True, exist_ok=True)

huey = SqliteHuey(
    filename=str(_db_path / "task_queue.db"),
    immediate=False,  # Set True for synchronous testing
)


def _run_async(coro):
    """Helper to run async code from sync Huey tasks."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ===========================================================================
# Task: Process Video Pipeline
# ===========================================================================

@huey.task(retries=1, retry_delay=10)
def process_video_task(video_id: int) -> dict:
    """
    Process a video through the full AI pipeline.

    This is the main task that runs:
    Transcription → Scene Detection → Audio Analysis → Face Tracking
    → Clip Scoring → Clip Generation → Subtitles → Metadata → Thumbnails
    """
    logger.info(f"Starting pipeline task for video {video_id}")

    try:
        from backend.services.pipeline import process_video_pipeline
        result = _run_async(process_video_pipeline(video_id))
        logger.info(f"Pipeline task completed for video {video_id}: {result.get('clips_generated', 0)} clips")
        return result
    except Exception as e:
        logger.error(f"Pipeline task failed for video {video_id}: {e}")
        # Update video status to failed
        try:
            from backend.database import crud
            from backend.database.engine import get_session_context
            from backend.database.models import VideoStatus

            _run_async(_update_video_failed(video_id, str(e)))
        except Exception:
            pass
        raise


async def _update_video_failed(video_id: int, error: str) -> None:
    """Mark a video as failed in the database."""
    from backend.database import crud
    from backend.database.engine import get_session_context
    from backend.database.models import VideoStatus

    async with get_session_context() as session:
        await crud.update_video_status(
            session, video_id,
            status=VideoStatus.FAILED,
            progress=-1,
            step="Task failed",
            error=error,
        )


# ===========================================================================
# Task: Upload Clip
# ===========================================================================

@huey.task(retries=2, retry_delay=30)
def upload_clip_task(clip_id: int, platform: str) -> dict:
    """
    Upload a clip to a social media platform.

    Args:
        clip_id: Database ID of the clip to upload
        platform: Target platform (youtube, facebook)
    """
    logger.info(f"Starting upload task: clip {clip_id} to {platform}")

    try:
        result = _run_async(_do_upload(clip_id, platform))
        return result
    except Exception as e:
        logger.error(f"Upload task failed: clip {clip_id} to {platform}: {e}")
        raise


async def _do_upload(clip_id: int, platform: str) -> dict:
    """Perform the actual upload."""
    from backend.database import crud
    from backend.database.engine import get_session_context
    from backend.database.models import UploadStatus
    from backend.services.uploaders.registry import UploaderRegistry

    registry = UploaderRegistry.get_instance()
    uploader = registry.get(platform)

    async with get_session_context() as session:
        clip = await crud.get_clip_with_relations(session, clip_id)
        if not clip:
            raise ValueError(f"Clip {clip_id} not found")

        if not clip.output_path or not Path(clip.output_path).exists():
            raise FileNotFoundError(f"Clip file not found: {clip.output_path}")

        # Authenticate
        from backend.utils.config import get_settings
        settings = get_settings()

        creds = {}
        if platform == "youtube":
            creds = {
                "client_secrets_file": str(settings.youtube_client_secrets_file),
                "token_file": str(settings.youtube_token_file),
            }
        elif platform == "facebook":
            creds = {
                "access_token": settings.facebook_access_token,
                "page_id": settings.facebook_page_id,
            }

        if not await uploader.authenticate(creds):
            raise RuntimeError(f"Authentication failed for {platform}")

        # Upload
        result = await uploader.upload_video(
            video_path=Path(clip.output_path),
            title=clip.title or f"Clip {clip_id}",
            description=clip.description or "",
            tags=(clip.hashtags or "").split() if clip.hashtags else [],
        )

        # Update upload record
        uploads = clip.uploads
        upload_record = next(
            (u for u in uploads if u.platform.value == platform and u.status.value == "pending"),
            None,
        )
        if upload_record:
            await crud.update_upload(
                session, upload_record.id,
                status=UploadStatus.PUBLISHED if result.success else UploadStatus.FAILED,
                platform_video_id=result.platform_video_id,
                url=result.url,
                error_message=result.error,
                metadata_json=result.metadata,
            )

        return {
            "success": result.success,
            "platform": platform,
            "clip_id": clip_id,
            "url": result.url,
            "error": result.error,
        }


# ===========================================================================
# Task: Regenerate Metadata
# ===========================================================================

@huey.task()
def regenerate_metadata_task(clip_id: int, model: str | None = None) -> dict:
    """Regenerate AI metadata (title, description, hashtags) for a clip.
    Default model = settings.ollama_model (qwen3:8b)."""
    logger.info(f"Regenerating metadata for clip {clip_id}")
    return _run_async(_do_regenerate_metadata(clip_id, model))


async def _do_regenerate_metadata(clip_id: int, model: str | None) -> dict:
    from backend.database import crud
    from backend.database.engine import get_session_context
    from backend.services.metadata_generator import generate_metadata

    async with get_session_context() as session:
        clip = await crud.get_clip_with_relations(session, clip_id)
        if not clip:
            raise ValueError(f"Clip {clip_id} not found")

        video = await crud.get_video(session, clip.video_id)
        transcript = await crud.get_transcript_for_video(session, clip.video_id)

        clip_text = ""
        if transcript and transcript.content_json:
            segments = transcript.content_json
            clip_text = " ".join(
                s.get("text", "") for s in segments
                if s.get("start", s.get("t0", 0)) >= clip.start_time
                and s.get("end", s.get("t1", 0)) <= clip.end_time
            ).strip()

        if not clip_text and transcript:
            clip_text = (transcript.full_text or "")[:500]

        metadata = generate_metadata(clip_text, model=model)
        await crud.update_clip(
            session, clip_id,
            title=metadata.get("title", clip.title),
            description=metadata.get("description", clip.description),
            hashtags=metadata.get("hashtags", clip.hashtags),
            keywords=metadata.get("keywords", clip.keywords),
        )

    return metadata
