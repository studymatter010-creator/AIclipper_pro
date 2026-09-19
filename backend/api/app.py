"""
AIClipper FastAPI Application Factory

Creates and configures the FastAPI app with middleware, static files,
routers, lifespan events, and OpenAPI metadata.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.database.engine import close_db, init_db
from backend.utils.config import PROJECT_ROOT, get_settings
from backend.utils.logging import get_logger, setup_logging

logger = get_logger("api.app")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan handler.

    * Startup:  initialise logging, ensure directories, create DB tables,
                recover stuck processing jobs.
    * Shutdown: close the database engine cleanly.
    """
    # --- Startup ---
    settings = get_settings()
    setup_logging(settings.log_dir)
    settings.ensure_directories()
    await init_db()

    # Auto-recover videos stuck in "processing" from a previous crash
    try:
        from backend.database.engine import get_session_context
        from backend.database import crud
        from backend.database.models import VideoStatus
        from sqlalchemy import select, update
        from backend.database.models import Video

        async with get_session_context() as session:
            result = await session.execute(
                select(Video).where(Video.status == VideoStatus.PROCESSING)
            )
            stuck_videos = result.scalars().all()
            if stuck_videos:
                for v in stuck_videos:
                    await crud.update_video_status(
                        session, v.id,
                        status=VideoStatus.PENDING,
                        progress=0,
                        step=None,
                        error=None,
                    )
                logger.info(f"Auto-recovered {len(stuck_videos)} stuck video(s) to PENDING")
    except Exception as e:
        logger.warning(f"Auto-recovery check failed: {e}")

    # ── Seed BYOK role routing from persisted user settings ───────────────
    # model_team.chat/chat_json read a small in-memory role_config cache; prime
    # it from the DB so any previously configured API-provider roles are active
    # even before the Settings UI is opened.  Roles default to Local/Ollama.
    try:
        from backend.database.engine import get_session_context as _gsc
        from backend.database import crud as _crud
        from backend.services.llm import role_config
        async with _gsc() as session:
            from backend.database.models import User
            first = (await session.execute(
                select(User).order_by(User.id.asc()).limit(1)
            )).scalars().first()
            if first is not None:
                role_config.load_from_settings(await _crud.get_all_settings(session, first.id))
    except Exception as e:
        logger.warning(f"Could not seed BYOK role config: {e}")

    # ── Transcription engine check ────────────────────────────────────────
    try:
        import faster_whisper  # noqa: F401
        logger.info("Transcription engine: faster-whisper (PRIMARY) ✓")
    except ImportError:
        logger.warning(
            "╔══════════════════════════════════════════════════════════════╗\n"
            "║  WARNING: faster-whisper is NOT installed.                 ║\n"
            "║  Subtitles will use the pywhispercpp fallback engine.      ║\n"
            "║  Install for best quality: pip install faster-whisper      ║\n"
            "╚══════════════════════════════════════════════════════════════╝"
        )

    logger.info(
        f"AIClipper API started — env={settings.app_env}, "
        f"debug={settings.app_debug}, port={settings.app_port}"
    )
    yield
    # --- Shutdown ---
    await close_db()
    logger.info("AIClipper API shut down")


# ---------------------------------------------------------------------------
# App Factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Build and return the fully-configured FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="AIClipper API",
        version="1.0.0",
        description=(
            "AI-powered local video clipping platform for generating "
            "short-form vertical content from long-form videos."
        ),
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # CORS — allow all origins for local development
    # ------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Static file mounts
    # ------------------------------------------------------------------
    _mount_static(app, "/static", PROJECT_ROOT / "frontend", "static")
    _mount_static(app, "/outputs", settings.output_dir, "outputs")
    _mount_static(app, "/thumbnails", settings.thumbnail_dir, "thumbnails")

    # ------------------------------------------------------------------
    # Routers
    # ------------------------------------------------------------------
    from backend.api.routes.clips import router as clips_router
    from backend.api.routes.editor import router as editor_router
    from backend.api.routes.processing import router as processing_router
    from backend.api.routes.providers import router as providers_router
    from backend.api.routes.publishing import router as publishing_router
    from backend.api.routes.settings import router as settings_router
    from backend.api.routes.storage import router as storage_router
    from backend.api.routes.videos import router as videos_router

    app.include_router(videos_router)
    app.include_router(processing_router)
    app.include_router(clips_router)
    app.include_router(editor_router)
    app.include_router(publishing_router)
    app.include_router(settings_router)
    app.include_router(storage_router)
    app.include_router(providers_router)

    # ------------------------------------------------------------------
    # Health-check
    # ------------------------------------------------------------------
    @app.get(
        "/api/health",
        tags=["System"],
        summary="Health check",
        description="Simple health-check endpoint that returns service status.",
    )
    async def health_check() -> dict[str, str]:
        """Return basic health status."""
        return {"status": "healthy", "service": "AIClipper API", "version": "1.0.0"}

    # ------------------------------------------------------------------
    # Serve frontend SPA
    # ------------------------------------------------------------------
    from fastapi.responses import FileResponse

    index_html = PROJECT_ROOT / "frontend" / "index.html"

    @app.get("/", include_in_schema=False)
    async def serve_root():
        """Serve the main SPA page."""
        return FileResponse(str(index_html), media_type="text/html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        """Catch-all route for SPA — serve static files or fall back to index.html."""
        # Try to serve the file from frontend directory
        file_path = PROJECT_ROOT / "frontend" / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        # Fall back to index.html for SPA routing
        return FileResponse(str(index_html), media_type="text/html")

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mount_static(app: FastAPI, path: str, directory: Path, name: str) -> None:
    """Mount a StaticFiles directory, creating it first if needed."""
    directory.mkdir(parents=True, exist_ok=True)
    app.mount(path, StaticFiles(directory=str(directory)), name=name)


# ---------------------------------------------------------------------------
# Singleton app instance (used by uvicorn CLI: ``uvicorn backend.api.app:app``)
# ---------------------------------------------------------------------------

app = create_app()


# ---------------------------------------------------------------------------
# Entry-point (``python -m backend.api.app`` or ``aiclipper`` console script)
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the API server via uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.api.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_debug,
        log_level="info",
    )


if __name__ == "__main__":
    main()
