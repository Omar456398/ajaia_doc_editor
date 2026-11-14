from collections.abc import AsyncGenerator
import os
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _prepare_asyncpg_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    """
    Normalize standard Postgres / Neon connection URLs (e.g. `postgresql://...?sslmode=require`)
    into an `asyncpg`-compatible SQLAlchemy URL and `connect_args`.
    """
    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    elif url.startswith("postgresql+psycopg2://"):
        url = "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    filtered_query: list[tuple[str, str]] = []
    connect_args: dict[str, Any] = {}

    for k, v in query_pairs:
        k_lower = k.lower()
        if k_lower == "sslmode":
            if v.lower() not in ("disable", "allow"):
                connect_args["ssl"] = "require"
        elif k_lower in ("channel_binding", "options"):
            # asyncpg does not accept libpq's channel_binding keyword argument
            continue
        else:
            filtered_query.append((k, v))

    if "neon.tech" in (parts.hostname or "") and "ssl" not in connect_args:
        connect_args["ssl"] = "require"

    clean_url = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(filtered_query),
            parts.fragment,
        )
    )
    return clean_url, connect_args


ASYNC_DATABASE_URL, ASYNC_CONNECT_ARGS = _prepare_asyncpg_url(settings.DATABASE_URL)
USE_NULL_POOL = bool(os.getenv("VERCEL")) or "neon.tech" in ASYNC_DATABASE_URL

engine_kwargs: dict[str, Any] = {
    "echo": settings.ENVIRONMENT == "development" and not os.getenv("VERCEL"),
    "connect_args": ASYNC_CONNECT_ARGS,
}
if USE_NULL_POOL:
    engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_pre_ping"] = True

engine = create_async_engine(ASYNC_DATABASE_URL, **engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides an async database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            if session.in_transaction():
                await session.rollback()
            raise
        finally:
            await session.close()
