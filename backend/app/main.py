from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.router import api_router
from app.core.collab import collab_manager
from app.core.config import ROOT_DIR, settings
from app.core.database import AsyncSessionLocal, Base, engine
from app.core.seed import seed_initial_data
import app.models.entities  # noqa: F401 — ensure ORM models are registered

FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # Ensure database schema, column upgrades, and 20 dummy users exist on startup
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Safe idempotent schema upgrades if tables were created by an earlier revision
            await conn.execute(
                text(
                    "ALTER TABLE user_docs ADD COLUMN IF NOT EXISTS perimeter_start INTEGER NOT NULL DEFAULT 0"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE user_docs ADD COLUMN IF NOT EXISTS perimeter_end INTEGER NOT NULL DEFAULT 64"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE user_docs ADD COLUMN IF NOT EXISTS is_selection_perimeter BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            # Normalize any non-selection 256-char perimeters from previous config to 64-char width
            await conn.execute(
                text(
                    """
                    UPDATE user_docs
                    SET perimeter_start = GREATEST(0, cursor_pos - 32),
                        perimeter_end = GREATEST(0, cursor_pos - 32) + 64
                    WHERE is_selection_perimeter = FALSE AND (perimeter_end - perimeter_start) != 64
                    """
                )
            )
            # Ensure at most one row per (doc_id, user_id) in doc_edit_logs
            await conn.execute(
                text(
                    """
                    DELETE FROM doc_edit_logs a USING doc_edit_logs b
                    WHERE a.id < b.id AND a.doc_id = b.doc_id AND a.user_id = b.user_id
                    """
                )
            )
            await conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_doc_edit_logs_doc_user_idx
                    ON doc_edit_logs (doc_id, user_id)
                    """
                )
            )
        async with AsyncSessionLocal() as db:
            await seed_initial_data(db)
    except Exception as exc:
        print(f"[startup] Database initialization warning: {exc}")

    collab_manager.start_background_monitor()
    try:
        yield
    finally:
        await collab_manager.stop_background_monitor()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        openapi_url=f"{settings.API_PREFIX}/openapi.json",
        docs_url=f"{settings.API_PREFIX}/docs",
        redoc_url=f"{settings.API_PREFIX}/redoc",
        lifespan=lifespan,
    )

    # Mount all backend endpoints under /api (URL-agnostic relative routing)
    app.include_router(api_router, prefix=settings.API_PREFIX)

    # In production / built mode, serve the React static build from the same origin
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        assets_dir = FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="static-assets",
            )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str) -> FileResponse:
            if full_path.startswith(settings.API_PREFIX.lstrip("/")):
                raise HTTPException(status_code=404, detail="API endpoint not found")

            candidate = (FRONTEND_DIST / full_path).resolve()
            if (
                full_path
                and candidate.is_file()
                and FRONTEND_DIST.resolve() in candidate.parents
            ):
                return FileResponse(candidate)

            return FileResponse(FRONTEND_DIST / "index.html")

    return app


app = create_app()
