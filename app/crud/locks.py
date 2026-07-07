"""Postgres advisory-lock helpers shared by the booking flows.

Mirrors the per-contact lock in ``app/services/whatsapp_outbound.py``: a
transaction-scoped ``pg_advisory_xact_lock`` (auto-released at commit/rollback,
so nothing leaks on pooled asyncpg connections) keyed by a namespaced blake2b
hash, so keys cannot collide with other advisory-lock users.
"""
import hashlib

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession


def _lock_key(namespace: str, object_id: int) -> int:
    """Stable signed 64-bit advisory-lock key (process-independent)."""
    h = hashlib.blake2b(f"{namespace}:{object_id}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big", signed=True)


async def lock_class_session(db: AsyncSession, session_id: int) -> None:
    """Serialize capacity/seat checks + reservation inserts for one class session.

    The capacity check (count reservations, compare to ``capacity``) has no
    backing constraint in the DB, so two concurrent bookings that both count
    before either inserts can oversell the class (TOCTOU). Callers MUST take
    this lock BEFORE counting; it is held until the surrounding transaction
    commits or rolls back.
    """
    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _lock_key("class_session", session_id)},
    )


# Fixed key: one coarse lock serializes ALL multi-session batch runs against
# each other (materialization + reschedule share it).
_MATERIALIZE_BATCH_KEY = _lock_key("materialize_batch", 0)


async def lock_materialization_batch(db: AsyncSession) -> None:
    """Serialize whole multi-session batch runs (materialize / reschedule).

    A batch acquires MANY per-session ``lock_class_session`` locks, held until
    the single terminal commit, in a data-dependent order. Two concurrent
    batches could therefore grab the same pair of session locks in opposite
    order and ABBA-deadlock (Postgres kills one; the batch's per-item
    ``except: continue`` then runs on the aborted transaction and the final
    commit rolls back — silently losing every reservation it created).

    Taking this coarse lock FIRST (before any per-session lock) makes batches
    mutually exclusive, so no two batches ever hold session locks at the same
    time. A single manual booking still only holds ONE session lock and never
    takes this one, so it can never form a cycle with a batch.
    """
    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _MATERIALIZE_BATCH_KEY},
    )
