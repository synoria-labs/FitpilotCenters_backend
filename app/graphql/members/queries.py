from typing import Optional, List

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.membersCrud import count_members, get_members_list, get_member_by_id
from app.crud.permissions import VIEW_MEMBERS
from app.graphql.members.types import Member, PaginatedMembers
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.core.conversions import coerce_int


@strawberry.type
class MembersQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def members_page(
        self,
        info: Info,
        limit: int = 100,
        offset: int = 0,
        search: Optional[str] = None
    ) -> PaginatedMembers:
        """Get a paginated list of members with an exact total."""
        if await require_capability(info, VIEW_MEMBERS):
            return PaginatedMembers(items=[], total=0)
        db: AsyncSession = info.context.db
        safe_limit = max(1, min(int(limit or 100), 500))
        safe_offset = max(0, int(offset or 0))

        members_data = await get_members_list(
            db=db,
            limit=safe_limit,
            offset=safe_offset,
            search=search
        )
        total = await count_members(db=db, search=search)

        return PaginatedMembers(
            items=[Member.from_data(member_data) for member_data in members_data],
            total=total,
        )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def members(
        self,
        info: Info,
        limit: Optional[int] = None,
        offset: int = 0,
        search: Optional[str] = None
    ) -> List[Member]:
        """Get comprehensive list of all members with optional filters"""
        if await require_capability(info, VIEW_MEMBERS):
            return []
        db: AsyncSession = info.context.db
        members_data = await get_members_list(
            db=db,
            limit=limit,
            offset=offset,
            search=search
        )

        return [Member.from_data(member_data) for member_data in members_data]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def member(self, info: Info, member_id: int) -> Optional[Member]:
        """Get detailed member information by ID"""
        if await require_capability(info, VIEW_MEMBERS):
            return None
        db: AsyncSession = info.context.db

        member_id = coerce_int(member_id)
        if member_id is None:
            return None

        member_data = await get_member_by_id(db=db, member_id=member_id)

        return Member.from_data(member_data) if member_data else None
