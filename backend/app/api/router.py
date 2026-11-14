from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

api_router = APIRouter()


@api_router.get("/health", tags=["system"])
async def health_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Verify API and PostgreSQL database connectivity."""
    db_status = "disconnected"
    db_error = None
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        db_status = "unavailable"
        db_error = str(exc)

    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "api_prefix": settings.API_PREFIX,
        "database": {
            "status": db_status,
            "error": db_error,
        },
    }
