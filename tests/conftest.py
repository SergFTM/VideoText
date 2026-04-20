"""Shared test fixtures.

DB fixture: uses a separate SQLite file (`prisma/test.db`) that gets
wiped between tests. Prisma's Python client does not support `:memory:` cleanly
with the current schema because multiple connections don't share memory DBs.
"""
import asyncio
import os
import shutil
from pathlib import Path
import pytest
import pytest_asyncio

TEST_DB_PATH = Path(__file__).parent.parent / "prisma" / "test.db"


@pytest_asyncio.fixture
async def db():
    """Fresh DB per test. Uses DATABASE_URL override to point Prisma at test.db."""
    if TEST_DB_PATH.exists():
        TEST_DB_PATH.unlink()
    os.environ["DATABASE_URL"] = f"file:{TEST_DB_PATH}"
    # Push schema to create tables
    import subprocess
    subprocess.run(
        ["prisma", "db", "push", "--skip-generate", "--accept-data-loss"],
        check=True, capture_output=True,
    )
    from prisma import Prisma
    client = Prisma()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
