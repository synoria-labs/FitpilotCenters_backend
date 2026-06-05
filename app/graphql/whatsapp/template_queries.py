"""GraphQL queries for WhatsApp message-template management."""
from typing import List, Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import whatsappTemplatesCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.whatsapp.template_types import WhatsAppTemplate


@strawberry.type
class WhatsAppTemplateQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def whatsapp_templates(
        self, info: Info, search: Optional[str] = None
    ) -> List[WhatsAppTemplate]:
        """List the locally-mirrored Meta message templates."""
        db: AsyncSession = info.context.db
        data = await crud.list_templates(db, search=search)
        return [WhatsAppTemplate.from_data(d) for d in data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def whatsapp_template(
        self, info: Info, id: int
    ) -> Optional[WhatsAppTemplate]:
        db: AsyncSession = info.context.db
        d = await crud.get_template(db, id)
        return WhatsAppTemplate.from_data(d) if d else None
