"""Shared test fixtures.

DB fixture: uses a separate SQLite file (`prisma/test.db`) that gets
wiped between tests. Prisma's Python client does not support `:memory:` cleanly
with the current schema because multiple connections don't share memory DBs.
"""
import os
import subprocess
import sys
from pathlib import Path
import pytest_asyncio

TEST_DB_PATH = Path(__file__).parent.parent / "prisma" / "test.db"


@pytest_asyncio.fixture
async def db():
    """Fresh DB per test. Uses DATABASE_URL override to point Prisma at test.db."""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    _orig_db_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"file:{TEST_DB_PATH}"
    # Invoke prisma via the active Python interpreter's -m entry point so we
    # don't depend on `prisma` being on PATH (it's only in .venv/Scripts/).
    subprocess.run(
        [sys.executable, "-m", "prisma", "db", "push",
         "--skip-generate", "--accept-data-loss"],
        check=True,
    )
    from prisma import Prisma
    # Pass datasource URL directly and skip dotenv so the client uses test.db,
    # not the production DB defined in .env.
    client = Prisma(
        datasource={"url": f"file:{TEST_DB_PATH}"},
        use_dotenv=False,
    )
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
        # Restore DATABASE_URL so subsequent tests see the production DB
        if _orig_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _orig_db_url
