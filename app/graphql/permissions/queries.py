from typing import List

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.permissions import (
    ALL_CAPABILITIES,
    get_capabilities_for_person,
    list_role_capabilities,
)
from app.graphql.auth.permissions import IsAuthenticated, require_admin
from app.graphql.permissions.types import RoleCapabilities


@strawberry.type
class PermissionsQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def role_capabilities(self, info: Info) -> List[RoleCapabilities]:
        """Matrix of roles and their granted capabilities (admin only)."""
        error = await require_admin(info)
        if error:
            return []

        db: AsyncSession = info.context.db
        rows = await list_role_capabilities(db)
        return [
            RoleCapabilities(
                role_code=row["role_code"],
                role_description=row["role_description"],
                capabilities=row["capabilities"],
                locked=row["locked"],
            )
            for row in rows
        ]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def all_capabilities(self, info: Info) -> List[str]:
        """The catalog of capabilities the system knows about."""
        return list(ALL_CAPABILITIES)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def my_capabilities(self, info: Info) -> List[str]:
        """Effective capabilities for the current user (fresh from DB)."""
        db: AsyncSession = info.context.db
        user = getattr(info.context, "user", None)
        caps = await get_capabilities_for_person(db, user)
        return sorted(caps)
