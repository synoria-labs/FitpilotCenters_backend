"""GraphQL queries for owner/admin agent configuration."""
from __future__ import annotations

from typing import List, Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import ownerAgentCrud as crud
from app.crud.permissions import MANAGE_OWNER_AGENT
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.owner_agent.types import (
    OwnerAgentAuthorizedPhoneType,
    OwnerAgentConfigType,
)
from app.services.owner_agent.env import owner_agent_env


@strawberry.type
class OwnerAgentQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def owner_agent_config(self, info: Info) -> Optional[OwnerAgentConfigType]:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return None
        db: AsyncSession = info.context.db
        data = await crud.get_config(db)
        return OwnerAgentConfigType.from_data(
            data, server_enabled=owner_agent_env.SERVER_ENABLED
        )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def owner_agent_authorized_phones(
        self, info: Info
    ) -> List[OwnerAgentAuthorizedPhoneType]:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return []
        db: AsyncSession = info.context.db
        return [
            OwnerAgentAuthorizedPhoneType.from_data(row)
            for row in await crud.list_authorized_phones(db)
        ]
