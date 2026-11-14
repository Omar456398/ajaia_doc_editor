import asyncio
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text
from app.core.database import AsyncSessionLocal, engine
from app.core.seed import seed_initial_data


async def reset_and_seed() -> None:
    backend_dir = Path(__file__).resolve().parent
    alembic_bin = backend_dir / ".venv" / "bin" / "alembic"

    print("1. Dropping existing tables...")
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS attachments CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS doc_edit_logs CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS user_docs CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS docs CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS users CASCADE"))
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
    await engine.dispose()

    print("2. Applying Alembic migrations from scratch (upgrade head)...")
    subprocess.run(
        [str(alembic_bin), "upgrade", "head"], cwd=str(backend_dir), check=True
    )

    print("3. Seeding 20 dummy users and starter collaborative document...")
    async with AsyncSessionLocal() as db:
        await seed_initial_data(db)
        if db.in_transaction():
            await db.commit()
    await engine.dispose()

    print("Done! All migrations and seeds re-applied from scratch.")


if __name__ == "__main__":
    asyncio.run(reset_and_seed())
