"""GraphQL mutations for owner/admin agent configuration."""
from __future__ import annotations

import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import ownerAgentCrud as crud
from app.crud.permissions import MANAGE_OWNER_AGENT
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.owner_agent.types import (
    AddOwnerAgentAuthorizedPhoneInput,
    OwnerAgentAuthorizedPhoneResult,
    OwnerAgentAuthorizedPhoneType,
    OwnerAgentConfigResult,
    OwnerAgentConfigType,
    SaveOwnerAgentConfigInput,
    UpdateOwnerAgentAuthorizedPhoneInput,
)
from app.services.owner_agent.env import owner_agent_env

logger = logging.getLogger(__name__)


@strawberry.type
class OwnerAgentMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def save_owner_agent_config(
        self, info: Info, input: SaveOwnerAgentConfigInput
    ) -> OwnerAgentConfigResult:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return OwnerAgentConfigResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            await crud.upsert_config(
                db,
                enabled=input.enabled,
                require_confirmation=input.require_confirmation,
                model=input.model,
                system_prompt=input.system_prompt,
                history_limit=input.history_limit,
                max_tokens=input.max_tokens,
            )
            data = await crud.get_config(db)
            return OwnerAgentConfigResult(
                success=True,
                config=OwnerAgentConfigType.from_data(
                    data, server_enabled=owner_agent_env.SERVER_ENABLED
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("Error saving owner agent config")
            return OwnerAgentConfigResult(success=False, error=str(exc))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def add_owner_agent_authorized_phone(
        self, info: Info, input: AddOwnerAgentAuthorizedPhoneInput
    ) -> OwnerAgentAuthorizedPhoneResult:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return OwnerAgentAuthorizedPhoneResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            model = await crud.add_authorized_phone(
                db,
                label=input.label,
                phone_number=input.phone_number,
                enabled=input.enabled,
                created_by=getattr(info.context, "account_id", None),
            )
            return OwnerAgentAuthorizedPhoneResult(
                success=True,
                phone=OwnerAgentAuthorizedPhoneType.from_data(
                    crud.OwnerAgentAuthorizedPhoneData.from_model(model)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return OwnerAgentAuthorizedPhoneResult(success=False, error=str(exc))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_owner_agent_authorized_phone(
        self, info: Info, input: UpdateOwnerAgentAuthorizedPhoneInput
    ) -> OwnerAgentAuthorizedPhoneResult:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return OwnerAgentAuthorizedPhoneResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            model = await crud.update_authorized_phone(
                db,
                phone_id=input.phone_id,
                label=input.label,
                phone_number=input.phone_number,
                enabled=input.enabled,
            )
            if model is None:
                return OwnerAgentAuthorizedPhoneResult(
                    success=False, error="Telefono autorizado no encontrado"
                )
            return OwnerAgentAuthorizedPhoneResult(
                success=True,
                phone=OwnerAgentAuthorizedPhoneType.from_data(
                    crud.OwnerAgentAuthorizedPhoneData.from_model(model)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return OwnerAgentAuthorizedPhoneResult(success=False, error=str(exc))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def disable_owner_agent_authorized_phone(
        self, info: Info, phone_id: int
    ) -> OwnerAgentAuthorizedPhoneResult:
        error = await require_capability(info, MANAGE_OWNER_AGENT)
        if error:
            return OwnerAgentAuthorizedPhoneResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            model = await crud.disable_authorized_phone(db, phone_id=phone_id)
            if model is None:
                return OwnerAgentAuthorizedPhoneResult(
                    success=False, error="Telefono autorizado no encontrado"
                )
            return OwnerAgentAuthorizedPhoneResult(
                success=True,
                phone=OwnerAgentAuthorizedPhoneType.from_data(
                    crud.OwnerAgentAuthorizedPhoneData.from_model(model)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            return OwnerAgentAuthorizedPhoneResult(success=False, error=str(exc))
