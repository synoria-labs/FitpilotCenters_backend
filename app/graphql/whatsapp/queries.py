"""GraphQL queries for the WhatsApp chat feature."""
from typing import Optional, List

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.whatsappCrud import (
    get_conversations,
    get_conversation_data,
    get_conversation_messages,
)
from app.graphql.whatsapp.types import ChatConversation, ChatMessage
from app.graphql.auth.permissions import IsAuthenticated


@strawberry.type
class WhatsAppChatQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def conversations(
        self,
        info: Info,
        limit: Optional[int] = 50,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> List[ChatConversation]:
        """List WhatsApp conversations ordered by most recent activity."""
        db: AsyncSession = info.context.db
        data = await get_conversations(db=db, limit=limit, offset=offset, search=search)
        return [ChatConversation.from_data(d) for d in data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def conversation(
        self,
        info: Info,
        id: int,
    ) -> Optional[ChatConversation]:
        """Fetch a single conversation enriched like the list (for incremental inserts)."""
        db: AsyncSession = info.context.db
        data = await get_conversation_data(db=db, conversation_id=id)
        return ChatConversation.from_data(data) if data else None

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def conversation_messages(
        self,
        info: Info,
        conversation_id: int,
        limit: int = 50,
        offset: int = 0,
    ) -> List[ChatMessage]:
        """Messages of a conversation in chronological order."""
        db: AsyncSession = info.context.db
        data = await get_conversation_messages(
            db=db, conversation_id=conversation_id, limit=limit, offset=offset
        )
        return [ChatMessage.from_data(d) for d in data]
