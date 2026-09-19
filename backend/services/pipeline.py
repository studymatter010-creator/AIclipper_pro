"""
AIClipper Workflow Pipeline Orchestrator

Runs the full automated processing pipeline for a video:
  Ingestion → Transcription → Scene Detection → Audio Analysis
  → Face Tracking → Clip Scoring → Clip Generation → Subtitles
  → Metadata → Thumbnails
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any, Callable

from backend.database import crud
from backend.database.engine import get_session_context
from backend.database.models import ClipStatus, SubtitleFormat, VideoStatus
from backend.utils.config import get_settings
from backend.utils.logging import get_logger, timed

logger = get_logger("processing.pipeline")


async def _update_progress(
    video_id: int,
    progress: int,
    step: str,
    callback: Callable | None = None,
    stage: str | None = None,
    stage_progress: int | None = None,
) -> None:
    """Update DB progress and invoke optional callback.

    ``stage`` is a machine-readable key (e.g. ``"clip_generation"``) for the
    active pipeline stage; ``stage_progress`` is the 0-100 sub-progress within
    that stage, or ``None`` to mark the stage as indeterminate.
    """
    async with get_session_context() as session:
        await crud.update_video_status(
            session, video_id,
            status=VideoStatus.PROCESSING,
            progress=progress,
            step=step,
            stage=stage,
            stage_progress=stage_progress,
        )
    if callback:
        try:
            callback(progress, step)
        except Exception:
            pass
    logger.info(f"Video {video_id}: [{progress}%] {step}")


def _parse_clip_durations(raw: str) -> int:
    """Parse the configured ``clip_durations`` string and return the preferred length."""
    try:
        vals = [int(x) for x in raw.split(",") if x.strip()]
        return vals[0] if vals else 60
    except ValueError:
        return 60


def _default_clip_count(video_duration: float, requested: int | None = None) -> int:
    """
    Pick a sensible number of clips based on video length.

    The user can request a specific count, but a sensible minimum is applied
    depending on how long the video is: long-form (2h+) videos get at least 10
    clips, shorter episodes (~10-30min) get 5-6, and very short videos fewer.
    """
    if requested is not None:
        return max(1, requested)
    if video_duration >= 7200:      # ≥ 2 hours
        return 16
    if video_duration >= 3600:      # 1-2 hours
        return 12
    if video_duration >= 1800:      # 30-60 min
        return 8
    if video_duration >= 600:       # 10-30 min
        return 6
    if video_duration >= 180:       # 3-10 min
        return 4
    return 2


@timed(logger_name="processing")
async def process_video_pipeline(
    video_id: int,
    progress_callback: Callable[[int, str], Any] | None = None,
    clip_count: int | None = None,
    clip_duration: int | None = None,
) -> dict[str, Any]:
    """
    Run the full processing pipeline on a video.

    Args:
        video_id: Database ID of the video to process
        progress_callback: Optional callback(progress_pct, step_name) for real-time updates

    Returns:
        dict with pipeline results summary
    """
    settings = get_settings()
    results: dict[str, Any] = {"video_id": video_id, "steps": {}, "clips_generated": 0}

    # --- Fetch video info ---
    async with get_session_context() as session:
        video = await crud.get_video(session, video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video_path = Path(video.filepath)
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        await crud.update_video_status(
            session, video_id, status=VideoStatus.PROCESSING, progress=0, step="Initializing"
        )

    try:
        # =====================================================================
        # Step 1: Transcription (0-20%)
        # =====================================================================
        await _update_progress(video_id, 2, "Transcribing audio...", progress_callback,
                               stage="transcription", stage_progress=0)
        transcript_data = {}
        try:
            from backend.services.transcription import transcribe_video

            # Live transcription progress (2026-09-12): transcription can take
            # many minutes on CPU with zero intermediate output, which looked like
            # a frozen/"stuck" job stuck at 2%.  faster-whisper yields segments as
            # it goes, so we thread a progress callback (0.0-1.0) from the worker
            # thread back onto the event loop and report stage_progress live while
            # keeping the overall bar moving through its 2-20% window.
            _loop = asyncio.get_running_loop()
            _last_tx_pct = {"pct": -1}

            def _report_transcription_progress(frac: float) -> None:
                try:
                    stage_pct = int(max(0.0, min(1.0, frac)) * 100)
                    if stage_pct == _last_tx_pct["pct"]:
                        return
                    _last_tx_pct["pct"] = stage_pct
                    overall = 2 + int(stage_pct * 0.18)  # 2 -> 20%
                    if _loop.is_closed():
                        return
                    asyncio.run_coroutine_threadsafe(
                        _update_progress(
                            video_id, overall,
                            f"Transcribing audio... {stage_pct}%",
                            progress_callback,
                            stage="transcription", stage_progress=stage_pct,
                        ),
                        _loop,
                    )
                except Exception:
                    pass  # progress reporting must never break transcription

            transcript_data = await asyncio.to_thread(
                transcribe_video,
                video_path,
                language=settings.whisper_language,
                model_name=settings.whisper_model,
                progress_callback=_report_transcription_progress,
            )
            async with get_session_context() as session:
                await crud.create_transcript(
                    session,
                    video_id=video_id,
                    language=transcript_data.get("language", "en"),
                    content_json=transcript_data.get("segments", []),
                    word_timestamps_json=transcript_data.get("words", []),
                    full_text=transcript_data.get("full_text", ""),
                )
            results["steps"]["transcription"] = "success"
        except Exception as e:
            logger.error(f"Transcription failed: {e}\n{traceback.format_exc()}")
            results["steps"]["transcription"] = f"failed: {str(e)}"
            transcript_data = {"segments": [], "words": [], "full_text": ""}

        await _update_progress(video_id, 20, "Transcription complete", progress_callback)

        # =====================================================================
        # Step 1.5: Content Classification
        # =====================================================================
        content_info: dict[str, str] = {"content_type": "other", "density": "medium"}
        try:
            from backend.services.content_classifier import classify_content
            content_info = await asyncio.to_thread(classify_content, transcript_data)
            async with get_session_context() as session:
                await crud.update_video_status(
                    session, video_id,
                    status=VideoStatus.PROCESSING,
                    progress=20,
                    step=f"Classified: {content_info['content_type']} ({content_info['density']})",
                )
            results["steps"]["content_classification"] = (
                f"success ({content_info['content_type']}, {content_info['density']})"
            )
        except Exception as e:
            logger.warning(f"Content classification failed (continuing): {e}")
            results["steps"]["content_classification"] = f"skipped: {str(e)}"

        # =====================================================================
        # Step 2: Scene Detection (20-35%)
        # =====================================================================
        await _update_progress(video_id, 22, "Detecting scenes...", progress_callback,
                               stage="scene_detection", stage_progress=None)
        scenes_data: list[dict] = []
        try:
            from backend.services.scene_detection import detect_scenes
            scenes_data = await asyncio.to_thread(detect_scenes, video_path)
            async with get_session_context() as session:
                await crud.create_scenes_batch(session, video_id, scenes_data)
            results["steps"]["scene_detection"] = f"success ({len(scenes_data)} scenes)"
        except Exception as e:
            logger.error(f"Scene detection failed: {e}\n{traceback.format_exc()}")
            results["steps"]["scene_detection"] = f"failed: {str(e)}"

        await _update_progress(video_id, 35, "Scene detection complete", progress_callback)

        # =====================================================================
        # Step 3: Audio Analysis (35-50%)
        # =====================================================================
        await _update_progress(video_id, 37, "Analyzing audio...", progress_callback,
                               stage="audio_analysis", stage_progress=None)
        audio_segments: list[dict] = []
        try:
            from backend.services.audio_analysis import analyze_audio
            from backend.utils.ffmpeg import extract_audio

            audio_path = settings.temp_dir / f"audio_{video_id}.wav"
            settings.temp_dir.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(extract_audio, video_path, audio_path)
            audio_segments = await asyncio.to_thread(analyze_audio, audio_path)
            results["steps"]["audio_analysis"] = f"success ({len(audio_segments)} segments)"

            # Clean up temp audio
            if audio_path.exists():
                audio_path.unlink()
        except Exception as e:
            logger.error(f"Audio analysis failed: {e}\n{traceback.format_exc()}")
            results["steps"]["audio_analysis"] = f"failed: {str(e)}"

        await _update_progress(video_id, 50, "Audio analysis complete", progress_callback)

        # =====================================================================
        # Step 3.5: Copyright Detection
        # =====================================================================
        await _update_progress(video_id, 51, "Detecting copyright segments...", progress_callback)
        copyright_segments: list[dict] = []
        try:
            from backend.services.copyright_detector import detect_copyright_segments, get_copyright_score_for_range
            copyright_segments = await asyncio.to_thread(detect_copyright_segments, video_path)
            results["steps"]["copyright_detection"] = f"success ({len(copyright_segments)} segments)"
        except Exception as e:
            logger.error(f"Copyright detection failed: {e}\n{traceback.format_exc()}")
            results["steps"]["copyright_detection"] = f"failed: {str(e)}"

        # =====================================================================
        # Step 4: Face Tracking (50-65%)
        # =====================================================================
        await _update_progress(video_id, 52, "Tracking faces...", progress_callback,
                               stage="face_tracking", stage_progress=None)
        face_data: list[dict] = []
        try:
            from backend.services.face_tracking import track_faces
            face_data = await asyncio.to_thread(
                track_faces,
                video_path,
                sample_every_n=settings.face_sample_every_n_frames,
            )
            results["steps"]["face_tracking"] = f"success ({len(face_data)} frames)"
        except Exception as e:
            logger.error(f"Face tracking failed: {e}\n{traceback.format_exc()}")
            results["steps"]["face_tracking"] = f"failed: {str(e)}"

        await _update_progress(video_id, 65, "Face tracking complete", progress_callback)

        # =====================================================================
        # Step 5: Clip Scoring (65-70%)
        # =====================================================================
        await _update_progress(video_id, 67, "Scoring potential clips...", progress_callback,
                               stage="clip_scoring", stage_progress=None)
        scored_clips: list[dict] = []
        try:
            from backend.services.clip_scoring import score_clips

            async with get_session_context() as session:
                video = await crud.get_video(session, video_id)
                video_duration = video.duration or 0

            weights = {
                "emotion": settings.scoring_weights.emotion,
                "dialogue": settings.scoring_weights.dialogue,
                "scene_change": settings.scoring_weights.scene_change,
                "audio": settings.scoring_weights.audio,
                "face": settings.scoring_weights.face,
            }

            # Genre-aware weight adjustment: tune scoring dimensions based on
            # the detected content type (e.g. podcasts reward dialogue/reaction
            # over scene changes).
            try:
                from backend.services.content_classifier import get_genre_weights
                weights = get_genre_weights(content_info.get("content_type"), weights)
            except Exception:
                pass  # fall back to base weights

            # Preferred clip length: user request, else config, else 60-90s.
            if clip_duration:
                preferred = clip_duration
            else:
                preferred = _parse_clip_durations(settings.clip_durations)
            durations = sorted({preferred, preferred + 15, preferred + 30})

            # Number of clips: explicit request, else sensible default for length.
            max_clips = _default_clip_count(
                video_duration, requested=clip_count
            ) if clip_count is not None else _default_clip_count(video_duration)

            scored_clips = await asyncio.to_thread(
                score_clips,
                video_duration=video_duration,
                transcript=transcript_data,
                scenes=scenes_data,
                audio_segments=audio_segments,
                face_data=face_data,
                copyright_segments=copyright_segments,
                clip_durations=durations,
                weights=weights,
                max_clips=max_clips,
                min_gap=float(settings.min_clip_gap_seconds),
                content_type=content_info.get("content_type"),
            )
            results["steps"]["clip_scoring"] = f"success ({len(scored_clips)} clips selected)"
        except Exception as e:
            logger.error(f"Clip scoring failed: {e}\n{traceback.format_exc()}")
            results["steps"]["clip_scoring"] = f"failed: {str(e)}"

        # ---- AI Brain refinement (semantic "hook" ratings) ----------------
        # Blend Ollama's narrative judgement into the heuristic scores so the
        # most attention-grabbing segments surface first. Fully optional: if
        # Ollama is offline the heuristic ordering is preserved.
        try:
            if scored_clips:
                from backend.services.ai_brain import refine_clip_scores
                scored_clips = await asyncio.to_thread(
                    refine_clip_scores,
                    scored_clips,
                    transcript_data,
                    content_type=content_info.get("content_type"),
                    video_duration=video_duration,
                )
                results["steps"]["ai_brain"] = "success"
        except Exception as e:
            logger.warning(f"AI brain refinement failed (continuing): {e}")
            results["steps"]["ai_brain"] = f"skipped: {str(e)}"

        await _update_progress(video_id, 70, "Clip scoring complete", progress_callback)

        if not scored_clips:
            logger.warning(f"No clips scored for video {video_id}. Skipping generation.")
            async with get_session_context() as session:
                await crud.update_video_status(
                    session, video_id,
                    status=VideoStatus.COMPLETED,
                    progress=100,
                    step="Completed (no clips generated)",
                )
            results["clips_generated"] = 0
            return results

        # =====================================================================
        # Step 6: Clip Generation (70-85%)
        # =====================================================================
        await _update_progress(video_id, 72, "Generating clips...", progress_callback,
                               stage="clip_generation", stage_progress=0)
        clip_records: list[Any] = []
        try:
            from backend.services.clip_generator import generate_clip

            settings.output_dir.mkdir(parents=True, exist_ok=True)
            total_clips = len(scored_clips)

            for i, clip_info in enumerate(scored_clips):
                clip_num = i + 1
                pct = 72 + int((i / total_clips) * 13)
                await _update_progress(
                    video_id, pct,
                    f"Generating clip {clip_num}/{total_clips}...",
                    progress_callback,
                    stage="clip_generation",
                    stage_progress=int((clip_num / total_clips) * 100),
                )

                output_filename = f"clip_{video_id}_{clip_num:03d}.mp4"
                output_path = settings.output_dir / output_filename

                # Filter face data for this clip's time range
                clip_face_data = [
                    f for f in face_data
                    if clip_info["start"] <= f.get("time", 0) <= clip_info["end"]
                ]

                try:
                    _clip_result = await asyncio.to_thread(
                        generate_clip,
                        video_path=video_path,
                        output_path=output_path,
                        start_time=clip_info["start"],
                        end_time=clip_info["end"],
                        crop_data=clip_face_data if clip_face_data else None,
                    )
                    # ``generate_clip`` now returns (path, achieved_start).  The
                    # achieved start is the keyframe-snapped REAL first frame, which
                    # may differ from the requested ``start_time`` by a few hundred
                    # ms.  We persist it (and the derived achieved end) on the Clip
                    # row so EVERY later consumer — Stage 7 subtitles AND the AI
                    # Editor — rebases against the truth, not the request.
                    _clip_path, _achieved_start = _clip_result
                    _achieved_end = (_achieved_start
                                     + (float(clip_info["end"]) - float(clip_info["start"])))

                    async with get_session_context() as session:
                        clip_record = await crud.create_clip(
                            session,
                            video_id=video_id,
                            clip_number=clip_num,
                            start_time=clip_info["start"],
                            end_time=clip_info["end"],
                            total_score=clip_info.get("total_score"),
                            score_breakdown=clip_info.get("breakdown"),
                        )
                        await crud.update_clip(
                            session, clip_record.id,
                            output_path=str(output_path),
                            status=ClipStatus.COMPLETED,
                            crop_data_json=clip_face_data,
                            hook_sentence=clip_info.get("hook_sentence"),
                            virality_reason=clip_info.get("virality_reason"),
                            achieved_start_time=_achieved_start,
                            achieved_end_time=_achieved_end,
                        )
                        # Persist content classification onto the video row
                        try:
                            if content_info.get("content_type"):
                                await crud.update_video(
                                    session, video_id,
                                    content_type=content_info.get("content_type"),
                                    content_density=content_info.get("density"),
                                )
                        except Exception:
                            pass
                        clip_records.append({"id": clip_record.id, "path": str(output_path), "info": clip_info})
                except Exception as e:
                    logger.error(f"Failed to generate clip {clip_num}: {e}")

            results["steps"]["clip_generation"] = f"success ({len(clip_records)} clips)"
            results["clips_generated"] = len(clip_records)
        except Exception as e:
            logger.error(f"Clip generation failed: {e}\n{traceback.format_exc()}")
            results["steps"]["clip_generation"] = f"failed: {str(e)}"

        await _update_progress(video_id, 85, "Clip generation complete", progress_callback)

        # =====================================================================
        # Step 7: Subtitle Generation (85-90%)
        # =====================================================================
        await _update_progress(video_id, 86, "Generating subtitles...", progress_callback,
                               stage="subtitles", stage_progress=0)
        try:
            from backend.services.subtitles import generate_srt, generate_vtt, generate_ass_with_highlights

            settings.subtitle_dir.mkdir(parents=True, exist_ok=True)
            segments = transcript_data.get("segments", [])
            # Word-level timestamps drive the karaoke-style ASS highlighting.
            words = (
                transcript_data.get("words")
                or transcript_data.get("word_timestamps")
                or []
            )
            sub_total = len(clip_records) or 1

            for sub_idx, clip_rec in enumerate(clip_records):
                clip_id = clip_rec["id"]
                clip_info = clip_rec["info"]

                await _update_progress(
                    video_id, 86,
                    f"Generating subtitles ({sub_idx + 1}/{sub_total})...",
                    progress_callback,
                    stage="subtitles",
                    stage_progress=int(((sub_idx + 1) / sub_total) * 100),
                )

                # Rebasing window: use the ACTUAL (keyframe-snapped) clip bounds
                # when recorded, else fall back to the requested window. This is
                # the same source-of-truth the AI Editor now uses, so Stage 7
                # captions match the rendered clip's first frame (Part A).
                sub_window_start = sub_window_end = None
                async with get_session_context() as session:
                    _clip_orm = await crud.get_clip(session, clip_id)
                    if _clip_orm is not None:
                        _ach_s = _clip_orm.achieved_start_time
                        _ach_e = _clip_orm.achieved_end_time
                        if _ach_s is not None:
                            sub_window_start = float(_ach_s)
                        if _ach_e is not None:
                            sub_window_end = float(_ach_e)
                if sub_window_start is None:
                    sub_window_start = float(clip_info["start"])
                if sub_window_end is None:
                    sub_window_end = float(clip_info["end"])

                # SRT
                srt_path = settings.subtitle_dir / f"clip_{video_id}_{clip_id}.srt"
                await asyncio.to_thread(generate_srt, segments, srt_path, sub_window_start, sub_window_end)
                async with get_session_context() as session:
                    await crud.create_subtitle(session, clip_id, SubtitleFormat.SRT, str(srt_path))

                # VTT
                vtt_path = settings.subtitle_dir / f"clip_{video_id}_{clip_id}.vtt"
                await asyncio.to_thread(generate_vtt, segments, vtt_path, sub_window_start, sub_window_end)
                async with get_session_context() as session:
                    await crud.create_subtitle(session, clip_id, SubtitleFormat.VTT, str(vtt_path))

                # ASS — word-level karaoke highlighting. Prefer real word
                # timestamps; fall back to whole segments when unavailable.
                ass_source = words if words else segments
                ass_path = settings.subtitle_dir / f"clip_{video_id}_{clip_id}.ass"
                await asyncio.to_thread(
                    generate_ass_with_highlights,
                    ass_source,
                    ass_path,
                    sub_window_start,
                    sub_window_end,
                )
                async with get_session_context() as session:
                    # ASS output is stored under the BURNED subtitle format tag.
                    await crud.create_subtitle(session, clip_id, SubtitleFormat.BURNED, str(ass_path))

            results["steps"]["subtitles"] = "success"
        except Exception as e:
            logger.error(f"Subtitle generation failed: {e}\n{traceback.format_exc()}")
            results["steps"]["subtitles"] = f"failed: {str(e)}"

        await _update_progress(video_id, 90, "Subtitles complete", progress_callback)

        # =====================================================================
        # Step 8: Metadata Generation (90-95%)
        # =====================================================================
        await _update_progress(video_id, 91, "Generating metadata...", progress_callback,
                               stage="metadata", stage_progress=0)
        try:
            from backend.services.metadata_generator import generate_metadata

            meta_total = len(clip_records) or 1
            for meta_idx, clip_rec in enumerate(clip_records):
                clip_id = clip_rec["id"]
                clip_info = clip_rec["info"]

                await _update_progress(
                    video_id, 91,
                    f"Generating metadata ({meta_idx + 1}/{meta_total})...",
                    progress_callback,
                    stage="metadata",
                    stage_progress=int(((meta_idx + 1) / meta_total) * 100),
                )

                # Extract transcript text for this clip's time range
                clip_segments = [
                    s for s in transcript_data.get("segments", [])
                    if s.get("start", s.get("t0", 0)) >= clip_info["start"]
                    and s.get("end", s.get("t1", 0)) <= clip_info["end"]
                ]
                clip_text = " ".join(s.get("text", "") for s in clip_segments).strip()
                if not clip_text:
                    clip_text = transcript_data.get("full_text", "")[:500]

                metadata = await asyncio.to_thread(generate_metadata, clip_text, model=settings.ollama_model)
                async with get_session_context() as session:
                    await crud.update_clip(
                        session, clip_id,
                        title=metadata.get("title", f"Clip {clip_rec['info'].get('start', 0):.0f}s"),
                        description=metadata.get("description", ""),
                        hashtags=metadata.get("hashtags", ""),
                        keywords=metadata.get("keywords", ""),
                    )

            results["steps"]["metadata"] = "success"
        except Exception as e:
            logger.error(f"Metadata generation failed: {e}\n{traceback.format_exc()}")
            results["steps"]["metadata"] = f"failed: {str(e)}"

        await _update_progress(video_id, 95, "Metadata complete", progress_callback)

        # =====================================================================
        # Step 9: Thumbnail Generation (95-100%)
        # =====================================================================
        await _update_progress(video_id, 96, "Generating thumbnails...", progress_callback,
                               stage="thumbnails", stage_progress=0)
        try:
            from backend.services.thumbnail_generator import generate_thumbnails

            settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)

            thumb_total = len(clip_records) or 1
            for thumb_idx, clip_rec in enumerate(clip_records):
                clip_id = clip_rec["id"]

                await _update_progress(
                    video_id, 96,
                    f"Generating thumbnails ({thumb_idx + 1}/{thumb_total})...",
                    progress_callback,
                    stage="thumbnails",
                    stage_progress=int(((thumb_idx + 1) / thumb_total) * 100),
                )
                clip_info = clip_rec["info"]
                clip_thumb_dir = settings.thumbnail_dir / f"clip_{clip_id}"
                clip_thumb_dir.mkdir(parents=True, exist_ok=True)

                thumbs = await asyncio.to_thread(
                    generate_thumbnails,
                    video_path=video_path,
                    output_dir=clip_thumb_dir,
                    clip_start=clip_info["start"],
                    clip_end=clip_info["end"],
                )

                async with get_session_context() as session:
                    for thumb in thumbs:
                        await crud.create_thumbnail(
                            session, clip_id,
                            filepath=thumb["path"],
                            score=thumb.get("score"),
                            format=thumb.get("format", "jpg"),
                            is_selected=thumb.get("is_selected", False),
                        )

            results["steps"]["thumbnails"] = "success"
        except Exception as e:
            logger.error(f"Thumbnail generation failed: {e}\n{traceback.format_exc()}")
            results["steps"]["thumbnails"] = f"failed: {str(e)}"

        # =====================================================================
        # Step 10: Thumbnail-as-intro-frame (LAST, after subtitles + thumbnail)
        # =====================================================================
        # Prepend the clip's SELECTED thumbnail as a short still-intro. Runs only
        # when (a) the user has the "Add thumbnail as intro frame" setting ON
        # (default OFF) and (b) the clip actually has a selected thumbnail. It's
        # applied to the primary clips.output_path (baking the intro into the
        # file every consumer points at). Never fatal: if it fails we keep the
        # pristine clip and log a warning.
        try:
            from backend.utils.ffmpeg import add_thumbnail_intro

            intro_on = False
            intro_dur = 0.8
            async with get_session_context() as session:
                intro_on, intro_dur = await crud.get_intro_setting(session)

            if intro_on and clip_records:
                intro_total = len(clip_records)
                for intro_idx, clip_rec in enumerate(clip_records):
                    clip_id = clip_rec["id"]
                    await _update_progress(
                        video_id, 97,
                        f"Adding thumbnail intro ({intro_idx + 1}/{intro_total})...",
                        progress_callback,
                        stage="thumbnails",
                        stage_progress=int(((intro_idx + 1) / intro_total) * 100),
                    )
                    selected_thumb = None
                    async with get_session_context() as session:
                        _t = await crud.get_selected_thumbnail(session, clip_id)
                        if _t is not None and _t.filepath:
                            selected_thumb = Path(_t.filepath)
                    clip_path = Path(clip_rec["path"])
                    if selected_thumb is not None:
                        await asyncio.to_thread(
                            add_thumbnail_intro,
                            clip_path,
                            selected_thumb,
                            intro_dur,
                        )
                    else:
                        logger.warning(
                            f"[INTRO] Clip {clip_id} has no selected thumbnail; skipping intro"
                        )
                results["steps"]["intro_frame"] = "success"
            else:
                results["steps"]["intro_frame"] = "skipped" if not clip_records else "disabled"
        except Exception as e:
            logger.warning(f"[INTRO] Intro-frame step failed (non-fatal): {e}")
            results["steps"]["intro_frame"] = f"failed: {str(e)}"

        # =====================================================================
        # Complete
        # =====================================================================
        await _update_progress(video_id, 100, "Processing complete", progress_callback)
        async with get_session_context() as session:
            await crud.update_video_status(
                session, video_id,
                status=VideoStatus.COMPLETED,
                progress=100,
                step="Completed",
            )
            # ── Optional auto-delete of the (large) source file after success ─
            # Only runs when (a) the user turned on "auto_delete_source" and
            # (b) at least one clip was actually generated.  Never fires on
            # partial/failed runs (that path is handled by the except block).
            # Keeps every derived artifact — clips/thumbnails/subtitles/edits
            # reference their own files, not the source, so removing just the
            # source upload saves the biggest chunk of disk.
            try:
                want_auto_delete = bool(
                    await crud.get_setting_any(session, "auto_delete_source")
                )
            except Exception:  # noqa: BLE001 — never let settings break completion
                want_auto_delete = False
            if want_auto_delete and results["clips_generated"] > 0:
                try:
                    if video_path.exists():
                        video_path.unlink(missing_ok=True)
                        logger.info(
                            f"VIDEO_AUTODELETE on video {video_id}: source removed "
                            f"after {results['clips_generated']} clips generated "
                            f"({video_path})"
                        )
                    else:
                        logger.info(
                            f"VIDEO_AUTODELETE video {video_id}: source already missing "
                            f"({video_path})"
                        )
                except OSError as exc:
                    logger.warning(
                        f"Auto-delete source for video {video_id} failed: {exc}"
                    )

        logger.info(
            f"Pipeline complete for video {video_id}: "
            f"{results['clips_generated']} clips generated, "
            f"steps: {results['steps']}"
        )
        return results

    except Exception as e:
        logger.error(f"Pipeline failed for video {video_id}: {e}\n{traceback.format_exc()}")
        async with get_session_context() as session:
            await crud.update_video_status(
                session, video_id,
                status=VideoStatus.FAILED,
                progress=-1,
                step="Pipeline failed",
                error=str(e),
            )
        raise
