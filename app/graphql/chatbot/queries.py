"""GraphQL query for the chatbot configuration."""
from typing import Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import chatbotConfigCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.chatbot.types import ChatbotConfigType
from app.core.ai_models import AI_MODEL_OPTIONS


@strawberry.type
class AiModelOption:
    id: str
    label: str


@strawberry.type
class ChatbotConfigQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    def ai_model_options(self) -> list[AiModelOption]:
        return [AiModelOption(id=model, label=label) for model, label in AI_MODEL_OPTIONS]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def chatbot_config(self, info: Info) -> Optional[ChatbotConfigType]:
        """Return the single chatbot configuration row (or None if not seeded)."""
        db: AsyncSession = info.context.db
        data = await crud.get_config(db)
        return ChatbotConfigType.from_data(data) if data else None
