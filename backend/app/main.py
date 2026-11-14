from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import ROOT_DIR, settings

FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        openapi_url=f"{settings.API_PREFIX}/openapi.json",
        docs_url=f"{settings.API_PREFIX}/docs",
        redoc_url=f"{settings.API_PREFIX}/redoc",
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
