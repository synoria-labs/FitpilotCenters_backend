"""GraphQL query for the chatbot configuration."""
from typing import Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import chatbotConfigCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.chatbot.types import ChatbotConfigType


@strawberry.type
class ChatbotConfigQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def chatbot_config(self, info: Info) -> Optional[ChatbotConfigType]:
        """Return the single chatbot configuration row (or None if not seeded)."""
        db: AsyncSession = info.context.db
        data = await crud.get_config(db)
        return ChatbotConfigType.from_data(data) if data else None
