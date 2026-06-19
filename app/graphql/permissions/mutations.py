import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud.permissions import grant_role_capability, revoke_role_capability
from app.graphql.auth.permissions import IsAuthenticated, require_admin
from app.graphql.permissions.types import CapabilityMutationResponse


@strawberry.type
class PermissionsMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def grant_role_capability(
        self, info: Info, role_code: str, capability: str
    ) -> CapabilityMutationResponse:
        """Grant a capability to a role (admin only)."""
        error = await require_admin(info)
        if error:
            return CapabilityMutationResponse(success=False, message=error)

        db: AsyncSession = info.context.db
        try:
            await grant_role_capability(db, role_code, capability)
            return CapabilityMutationResponse(success=True, message="Permiso concedido")
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return CapabilityMutationResponse(success=False, message=str(exc))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def revoke_role_capability(
        self, info: Info, role_code: str, capability: str
    ) -> CapabilityMutationResponse:
        """Revoke a capability from a role (admin only)."""
        error = await require_admin(info)
        if error:
            return CapabilityMutationResponse(success=False, message=error)

        db: AsyncSession = info.context.db
        try:
            await revoke_role_capability(db, role_code, capability)
            return CapabilityMutationResponse(success=True, message="Permiso revocado")
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return CapabilityMutationResponse(success=False, message=str(exc))
