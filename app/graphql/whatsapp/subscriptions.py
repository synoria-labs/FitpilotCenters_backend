"""GraphQL subscriptions for the WhatsApp chat feature.

Driven by the Postgres LISTEN/NOTIFY broadcaster (see app/services/whatsapp_listener.py).

Auth note: over WebSocket the desktop client cannot send the HttpOnly cookie, so it
passes the access token in the graphql-transport-ws ``connection_init`` payload
(``{"authToken": "..."}``). We authorize from ``info.context.user`` (HTTP path) or by
verifying that token (WS path).
"""
import logging
from typing import Optional, AsyncGenerator

import strawberry
from strawberry.types import Info

from app.graphql.whatsapp.types import ChatMessage
from app.services.whatsapp_listener import broadcaster
from app.db.postgresql import async_session_factory
from app.crud.whatsappCrud import get_message_by_id
from app.security.jwt import verify_token

logger = logging.getLogger(__name__)


def _is_authorized(info: Info) -> bool:
    # HTTP context already resolved the user.
    if getattr(info.context, "user", None):
        return True
    # WebSocket: look for a token in the connection_init payload.
    cp = getattr(info.context, "connection_params", None) or {}
    if isinstance(cp, dict):
        token = cp.get("authToken") or cp.get("authtoken")
        if not token:
            auth = cp.get("Authorization") or cp.get("authorization") or ""
            if isinstance(auth, str) and auth.startswith("Bearer "):
                token = auth.split(" ", 1)[1]
        if token and verify_token(token):
            return True
    return False


@strawberry.type
class WhatsAppChatSubscription:
    @strawberry.subscription
    async def message_added(
        self, info: Info, conversation_id: Optional[int] = None
    ) -> AsyncGenerator[ChatMessage, None]:
        """Stream newly inserted messages, optionally filtered by conversation."""
        if not _is_authorized(info):
            raise Exception("Authentication required.")

        queue = broadcaster.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.get("type") != "message":
                    continue
                if conversation_id is not None and event.get("conversation_id") != conversation_id:
                    continue
                msg_id = event.get("id")
                if msg_id is None:
                    continue
                async with async_session_factory() as db:
                    data = await get_message_by_id(db, int(msg_id))
                if data is None:
                    continue
                yield ChatMessage.from_data(data)
        finally:
            broadcaster.unsubscribe(queue)

    @strawberry.subscription
    async def message_updated(
        self, info: Info, conversation_id: Optional[int] = None
    ) -> AsyncGenerator[ChatMessage, None]:
        """Stream messages whose media finished downloading (or failed).

        Emitted by ``notify_media_event`` (application-level pg_notify) so the
        client can swap the "receiving..." placeholder for the real attachment.
        """
        if not _is_authorized(info):
            raise Exception("Authentication required.")

        queue = broadcaster.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.get("type") != "media_updated":
                    continue
                if conversation_id is not None and event.get("conversation_id") != conversation_id:
                    continue
                msg_id = event.get("id")
                if msg_id is None:
                    continue
                async with async_session_factory() as db:
                    data = await get_message_by_id(db, int(msg_id))
                if data is None:
                    continue
                yield ChatMessage.from_data(data)
        finally:
            broadcaster.unsubscribe(queue)
