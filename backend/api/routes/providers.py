"""
AIClipper BYOK Providers Routes

Per-role LLM provider selection + API key management + connection testing +
session cost readout.  API keys are stored in the OS credential vault and never
returned, logged, or echoed by any endpoint here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_current_user, get_db
from backend.database import crud
from backend.database.models import User
from backend.utils.logging import get_logger

logger = get_logger("api.providers")

router = APIRouter(tags=["AI Providers"])


# ── Schemas ─────────────────────────────────────────────────────────────

class RoleConfigRequest(BaseModel):
    role: str = Field(..., description="brain | classify | hook | translate")
    provider: str = Field(..., description="local | openai | anthropic | gemini | openai_compatible")
    model: str | None = Field(None, description="Provider model id (optional; defaults are used if blank)")


class TestRequest(BaseModel):
    provider: str = Field(..., description="Which provider to test")
    model: str | None = Field(None, description="Optional model id")


class KeyRequest(BaseModel):
    provider: str = Field(..., description="Provider to store a key for")
    key: str = Field(..., description="The API key to store in the OS vault")


# ── GET /api/providers ──────────────────────────────────────────────────

@router.get("/api/providers", summary="Provider status, keys and usage")
async def providers_status(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    from backend.services.llm import providers_meta, role_config, usage
    from backend.services.llm.keys import vault

    # Make the in-memory routing cache reflect what's persisted in settings.
    settings = await crud.get_all_settings(db, user.id)
    role_config.load_from_settings(settings)

    options = {
        p: {
            "label": providers_meta.provider_label(p),
            "default_model": providers_meta.provider_default_model(p),
            "tier": providers_meta.provider_cost_tier(p),
        }
        for p in providers_meta.DEFAULT_MODELS
    }
    return {
        "roles": role_config.active(),
        "options": options,
        "keys_configured": {p: vault.has(p) for p in
                             ("openai", "anthropic", "gemini", "openai_compatible")},
        "usage": usage.snapshot(),
    }


# ── PUT /api/providers/role ─────────────────────────────────────────────

@router.put("/api/providers/role", summary="Set a role's provider + model")
async def set_role(
    body: RoleConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    from backend.services.llm import providers_meta, role_config
    if body.role not in role_config.ROLES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"role must be one of {', '.join(role_config.ROLES)}")
    if body.provider not in providers_meta.DEFAULT_MODELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"provider must be one of {', '.join(providers_meta.DEFAULT_MODELS)}")

    provider, model = role_config.set_role(body.role, body.provider, body.model)
    # Persist for next startup.
    await crud.set_setting(db, user.id, role_config.provider_key(body.role), provider)
    await crud.set_setting(db, user.id, role_config.model_key(body.role), model)

    logger.info(f"Role '{body.role}' set to provider '{provider}' model '{model}' (user {user.id})")
    return {"role": body.role, "provider": provider, "model": model}


# ── POST /api/providers/test ────────────────────────────────────────────

@router.post("/api/providers/test", summary="Test a provider connection")
async def test_provider(body: TestRequest) -> dict:
    from backend.services.llm import providers_meta
    from backend.services.llm.providers import build_provider
    if body.provider not in providers_meta.DEFAULT_MODELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="invalid provider")
    try:
        prov = build_provider(body.provider, body.model)
        ok, message = prov.test()
        return {"ok": ok, "message": message, "provider": body.provider}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Provider test failed for '{body.provider}': {exc}")
        return {"ok": False, "message": str(exc), "provider": body.provider}


# ── PUT /api/providers/key ──────────────────────────────────────────────

@router.put("/api/providers/key", summary="Store an API key in the OS vault")
async def store_key(body: KeyRequest) -> dict:
    from backend.services.llm.keys import vault
    if body.provider not in ("openai", "anthropic", "gemini", "openai_compatible"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="invalid provider")
    # Never log or return the key.
    vault.set(body.provider, body.key)
    logger.info(f"API key stored for provider '{body.provider}' (user vault)")
    return {"provider": body.provider, "configured": vault.has(body.provider)}


# ── DELETE /api/providers/key/{provider} ────────────────────────────────

@router.delete("/api/providers/key/{provider}", summary="Remove a stored API key")
async def delete_key(provider: str) -> dict:
    from backend.services.llm.keys import vault
    vault.delete(provider)
    logger.info(f"API key removed for provider '{provider}'")
    return {"provider": provider, "configured": False}


# ── GET /api/providers/usage ────────────────────────────────────────────

@router.get("/api/providers/usage", summary="Session token/cost estimate")
async def usage_readout() -> dict:
    from backend.services.llm.usage import snapshot
    return snapshot()
