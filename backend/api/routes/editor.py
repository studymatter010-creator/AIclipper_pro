"""
AIClipper AI Editor Routes

Endpoints for the premium AI editor: multilingual voice-over dubbing,
voice catalogs, and per-clip render history.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.api.schemas import (
    DubRequest,
    DubResponse,
    ErrorResponse,
    VoiceOverListResponse,
    VoiceOverSchema,
    VoiceLanguage,
    VoiceInfo,
    VoicesResponse,
)
from backend.database import crud
from backend.services import voiceover as voiceover_service
from backend.utils.logging import get_logger

logger = get_logger("api.editor")

router = APIRouter(tags=["AI Editor"])


@router.get(
    "/api/voices",
    response_model=VoicesResponse,
    summary="List AI voices",
    description="Return the supported languages (English, Korean, Hindi, "
                "Japanese) and their selectable neural voices for dubbing.",
)
async def list_voices() -> VoicesResponse:
    """Return the multilingual voice catalog for the AI editor."""
    languages = []
    for lang in voiceover_service.list_voices():
        languages.append(
            VoiceLanguage(
                code=lang["code"],
                name=lang["name"],
                flag=lang["flag"],
                default_voice=lang["default_voice"],
                voices=[VoiceInfo(**v) for v in lang["voices"]],
            )
        )
    return VoicesResponse(languages=languages)


@router.get(
    "/api/team/status",
    summary="Model team status",
    description="Return the role → model roster for the cooperating AI team "
                "(brain, classify, hook, translate) plus the configured RAM "
                "budget.",
)
async def team_status() -> dict:
    """Return the current model-team roster and configuration for the UI."""
    try:
        from backend.services import model_team
        from backend.utils.config import get_settings

        settings = get_settings()
        roster = model_team.roster()
        # BYOK: active provider per role + which keys are configured + session cost.
        providers: dict = {}
        try:
            from backend.services.llm import role_config, usage
            from backend.services.llm.keys import vault
            providers = {
                "roles": role_config.active(),
                "keys_configured": {p: vault.has(p) for p in
                                     ("openai", "anthropic", "gemini", "openai_compatible")},
                "usage": usage.snapshot(),
            }
        except Exception:  # noqa: BLE001 — BYOK must never break the status call
            providers = {}
        ollama_ok = False
        installed: list[str] = []
        try:
            import requests
            base = settings.ollama_host.rstrip("/")
            r = requests.get(f"{base}/api/tags", timeout=4)
            if r.ok:
                ollama_ok = True
                installed = [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:  # noqa: BLE001 — Ollama simply not reachable yet
            pass

        # Check faster-whisper availability (primary transcription engine).
        faster_whisper_ok = False
        try:
            import faster_whisper  # noqa: F401
            faster_whisper_ok = True
        except ImportError:
            pass

        return {
            "roster": roster,
            "ram_budget_mb": settings.team_ram_budget_mb,
            "ollama_ready": ollama_ok,
            "models_installed": installed,
            "faster_whisper_ready": faster_whisper_ok,
            "providers": providers,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"team_status failed: {exc}")
        return {"roster": {}, "ram_budget_mb": None, "ollama_ready": False, "models_installed": []}


@router.post(
    "/api/clips/{clip_id}/dub",
    response_model=DubResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        404: {"model": ErrorResponse, "description": "Clip not found"},
    },
    summary="Generate an AI voice-over",
    description="Translate + synthesize + mux a multilingual neural voice-over "
                "onto a clip. Runs in a background thread.",
)
async def dub_clip(
    clip_id: int,
    body: DubRequest,
    db: AsyncSession = Depends(get_db),
) -> DubResponse:
    """Generate a dubbing for ``clip_id`` in the requested language/voice."""
    if body.clip_id != clip_id:
        # Keep the body's clip_id authoritative but tolerate path/body mismatch.
        pass

    clip = await crud.get_clip(db, body.clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {body.clip_id} not found.",
        )
    if not clip.output_path or not Path(clip.output_path).exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Clip output file has not been generated yet.",
        )

    try:
        # NOTE: dub_clip is an async coroutine, so we await it directly.
        # Passing it through asyncio.to_thread() would return an un-awaited
        # coroutine object and the dubbing would never actually run.
        result = await voiceover_service.dub_clip(
            clip_id=clip_id,
            target_lang=body.language,
            voice=body.voice,
            mode=body.mode,
            mix_volume=body.mix_volume,
            translate_enabled=body.translate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface synthesis/ffmpeg errors
        logger.error(f"Voice-over for clip {clip_id} failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    return DubResponse(
        id=result["id"],
        clip_id=result["clip_id"],
        language=result["language"],
        voice=result["voice"],
        mode=result["mode"],
        translated=result["translated"],
        output_path=result["output_path"],
        audio_path=result["audio_path"],
        status="completed",
        message=(
            "Dubbed successfully." if result["translated"]
            else "Voice-over generated (script was not translated; Ollama "
                 "unreachable)."
        ),
    )


@router.get(
    "/api/clips/{clip_id}/voiceovers",
    response_model=VoiceOverListResponse,
    summary="List clip voice-overs",
    description="Return every AI voice-over render previously generated for a clip.",
)
async def list_clip_voiceovers(
    clip_id: int,
    db: AsyncSession = Depends(get_db),
) -> VoiceOverListResponse:
    """Return the voice-over history for a clip."""
    clip = await crud.get_clip(db, clip_id)
    if clip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Clip with id {clip_id} not found.",
        )
    rows = await crud.get_voiceovers_for_clip(db, clip_id)
    return VoiceOverListResponse(
        voiceovers=[VoiceOverSchema.model_validate(v) for v in rows],
        total=len(rows),
    )