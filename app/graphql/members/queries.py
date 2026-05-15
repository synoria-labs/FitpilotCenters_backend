from typing import Optional, List

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.membersCrud import get_members_list, get_member_by_id
from app.graphql.members.types import Member
from app.graphql.auth.permissions import IsAuthenticated
from app.core.conversions import coerce_int


@strawberry.type
class MembersQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def members(
        self,
        info: Info,
        limit: Optional[int] = None,
        offset: int = 0,
        search: Optional[str] = None
    ) -> List[Member]:
        """Get comprehensive list of all members with optional filters"""
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
        db: AsyncSession = info.context.db

        member_id = coerce_int(member_id)
        if member_id is None:
            return None

        member_data = await get_member_by_id(db=db, member_id=member_id)

        return Member.from_data(member_data) if member_data else None
