"""Test fixtures.

Each test gets an `db` AsyncSession bound to a connection-level transaction that
is rolled back at teardown. SQLAlchemy 2.0's `join_transaction_mode="create_savepoint"`
turns any `session.commit()` inside the test (or inside production code under test)
into a SAVEPOINT release, so nothing actually persists to defaultdb.
"""
from __future__ import annotations

import asyncio
import sys

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

# Windows defaults to the Proactor event loop, which psycopg3 refuses to run async on.
# Local development uses psycopg (asyncpg has no Python 3.14 build yet); production and CI
# run asyncpg on 3.12, where this policy is simply unused. Set before the engine is imported
# so the pool never binds to a loop the driver rejects.
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.db.postgresql import engine  # noqa: E402 - must follow the policy above


@pytest_asyncio.fixture
async def db():
    async with engine.connect() as conn:
        outer_tx = await conn.begin()
        SessionMaker = async_sessionmaker(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with SessionMaker() as session:
            try:
                yield session
            finally:
                await session.close()
        await outer_tx.rollback()
