"""Test fixtures.

Each test gets an `db` AsyncSession bound to a connection-level transaction that
is rolled back at teardown. SQLAlchemy 2.0's `join_transaction_mode="create_savepoint"`
turns any `session.commit()` inside the test (or inside production code under test)
into a SAVEPOINT release, so nothing actually persists to defaultdb.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.postgresql import engine


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
