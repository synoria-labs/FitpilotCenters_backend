"""Postgres LISTEN/NOTIFY bridge for WhatsApp realtime events.

A dedicated asyncpg connection LISTENs on the ``whatsapp_events`` channel (fed by the
``trg_notify_whatsapp_message`` trigger on ``app.messages``) and fans each event out to
in-process subscribers (the GraphQL subscription resolvers). Using LISTEN/NOTIFY keeps a
single fan-out path for both inbound (webhook) and outbound (mutation) inserts, and works
across multiple uvicorn workers (each worker has its own listener).
"""
import asyncio
import json
import logging
import os
from typing import Dict, Set

import asyncpg

logger = logging.getLogger("whatsapp.listener")

CHANNEL = "whatsapp_events"


class Broadcaster:
    """Minimal in-process pub/sub with bounded per-subscriber queues."""

    def __init__(self, maxsize: int = 1000):
        self._subscribers: Set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: Dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Drop the oldest event to make room (slow consumer protection).
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass


def _plain_dsn() -> str:
    """asyncpg needs a plain DSN (strip the SQLAlchemy '+asyncpg' driver suffix)."""
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://appuser:secret123@localhost:5432/defaultdb",
    )
    return url.replace("+asyncpg", "")


class WhatsAppListener:
    def __init__(self, broadcaster: Broadcaster):
        self.broadcaster = broadcaster
        self._conn: asyncpg.Connection = None
        self._task: asyncio.Task = None
        self._stop = False

    async def start(self) -> None:
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._conn and not self._conn.is_closed():
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _on_notify(self, connection, pid, channel, payload) -> None:
        try:
            event = json.loads(payload)
        except Exception:  # noqa: BLE001
            event = {"raw": payload}
        self.broadcaster.publish(event)

    async def _run(self) -> None:
        backoff = 1
        dsn = _plain_dsn()
        while not self._stop:
            try:
                self._conn = await asyncpg.connect(dsn)
                await self._conn.add_listener(CHANNEL, self._on_notify)
                logger.info("WhatsApp listener connected; LISTEN %s", CHANNEL)
                backoff = 1
                while not self._stop:
                    await asyncio.sleep(5)
                    if self._conn.is_closed():
                        raise ConnectionError("listener connection closed")
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                logger.warning("WhatsApp listener error: %s; reconnecting in %ss", e, backoff)
                if self._conn and not self._conn.is_closed():
                    try:
                        await self._conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


# Global singletons used by the subscription resolver and the app lifespan.
broadcaster = Broadcaster()
listener = WhatsAppListener(broadcaster)
