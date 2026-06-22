from typing import Optional
from fastapi import Depends
import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.usersCrud import list_people, get_person_by_id, list_users, list_roles
from app.crud.permissions import MANAGE_USERS
from app.db.postgresql import get_db
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.users.types import Person, AppUser, RoleType
from app.core.conversions import coerce_int


@strawberry.type
class UserQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def people(self, info: Info, role_code: str = None) -> list[Person]:
        """Get list of all people, optionally filtered by role"""
        db = info.context.db

        people = await list_people(db=db, role_code=role_code)
        return [Person.from_model(person) for person in people]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def app_users(self, info: Info, include_inactive: bool = True) -> list[AppUser]:
        """List login accounts (staff users). Requires the manage_users capability."""
        db = info.context.db

        error = await require_capability(info, MANAGE_USERS)
        if error:
            return []

        accounts = await list_users(db=db, include_inactive=include_inactive)
        return [AppUser.from_account(account) for account in accounts]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def roles(self, info: Info) -> list[RoleType]:
        """Available roles in the system (to assign to users)."""
        db = info.context.db

        roles = await list_roles(db=db)
        return [RoleType.from_model(role) for role in roles]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def person(self, info: Info, person_id: int) -> Optional[Person]:
        """Get specific person by ID"""
        db = info.context.db

        person_id = coerce_int(person_id)
        if person_id is None:
            return None

        person = await get_person_by_id(db=db, person_id=person_id)
        return Person.from_model(person) if person else None
