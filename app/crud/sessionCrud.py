
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sessionModel import Session

logger = logging.getLogger(__name__)


async def create_session(db: AsyncSession, sessionEntry: Session) -> Session:
    """Create a session using a single transaction without post-commit refresh.

    Rationale:
    - Using `refresh()` immediately after `commit()` can trigger an extra
      SELECT that starts a new transaction while the connection may still be
      finalizing the previous operation under high concurrency, which can lead
      to asyncpg's "another operation is in progress" error.
    - We instead `flush()` to persist and populate PKs, then `commit()` and
      return the instance (with expire_on_commit=False in SessionLocal).
    """
    db.add(sessionEntry)
    try:
        # Ensure INSERT is issued and PKs are populated within the same txn
        await db.flush()
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise

    # No refresh needed: SessionLocal is configured with expire_on_commit=False
    # so attributes like `id` remain available.
    return sessionEntry


async def verify_session(db: AsyncSession, session_id: str) -> Session | None:
    """Verifies if a session exists and returns it."""
    res = await db.execute(select(Session).where(Session.session == session_id))
    return res.scalar_one_or_none()


async def update_last_active_at(db: AsyncSession, session_id: str) -> None:
    """Updates the last_active_at timestamp for a session using database function.

    Note: Does NOT commit or flush - changes will be committed when the session closes.
    This function is typically called from build_context() which shares the request session.
    """
    logger.debug("Updating last_active_at for session %s", session_id)
    stmt = update(Session).where(Session.session == session_id).values(last_active_at=func.now())
    await db.execute(stmt)
    # No flush, no commit - just queue the update


async def touch_session(db: AsyncSession, session_id: str) -> None:
    """Updates the last_active_at timestamp for a session with explicit UTC time.

    Note: Does NOT commit or flush - changes will be committed when the session closes.
    """
    timestamp = datetime.now(timezone.utc)
    await db.execute(
        update(Session)
        .where(Session.session == session_id)
        .values(last_active_at=timestamp, updated_at=timestamp)
    )
    # No flush, no commit - just queue the update


async def revoke_session(db: AsyncSession, session_id: str) -> None:
    """Marks the session as revoked.

    Note: Does NOT commit or flush - changes will be committed when the session closes.
    """
    timestamp = datetime.now(timezone.utc)
    await db.execute(
        update(Session)
        .where(Session.session == session_id)
        .values(revoked_at=timestamp, updated_at=timestamp)
    )
    # No flush, no commit - just queue the update


async def revoke_other_sessions(db: AsyncSession, user_id: int, keep_session_id: str) -> int:
    """Revoke every active session for a person except the current one.

    Sessions are keyed by ``user_id`` (= ``People.id``, see auth login) and the
    token identifier is the ``session`` column. Returns the number of sessions
    revoked. Does NOT commit — the caller commits atomically with its own changes.
    """
    timestamp = datetime.now(timezone.utc)
    result = await db.execute(
        update(Session)
        .where(
            Session.user_id == user_id,
            Session.session != keep_session_id,
            Session.revoked_at.is_(None),
            Session.deleted_at.is_(None),
        )
        .values(revoked_at=timestamp, updated_at=timestamp)
    )
    return result.rowcount or 0


async def update_refresh_token(db: AsyncSession, session_id: str, refresh_token: str) -> None:
    """Stores a new refresh token for an existing session.

    Note: Does NOT commit or flush - changes will be committed when the session closes.
    """
    await db.execute(
        update(Session)
        .where(Session.session == session_id)
        .values(refresh_token=refresh_token, updated_at=datetime.utcnow())
    )
    # No flush, no commit - just queue the update
