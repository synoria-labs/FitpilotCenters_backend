"""GraphQL queries for the automated notification configuration."""
from typing import List

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import notificationsCrud as crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.notifications.types import (
    NotificationEventCatalog,
    NotificationSettingType,
    NotificationVariable,
)
from app.graphql.whatsapp.template_types import WhatsAppTemplate
from app.services.notification_service import EVENT_TYPES, VARIABLES


@strawberry.type
class NotificationSettingsQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def notification_settings(self, info: Info) -> List[NotificationSettingType]:
        """Return the config for every event type (with safe defaults if not yet saved)."""
        db: AsyncSession = info.context.db
        existing = {s.event_type: s for s in await crud.list_settings(db)}

        results: List[NotificationSettingType] = []
        for event_type, meta in EVENT_TYPES.items():
            data = existing.get(event_type)
            template = None
            if data is not None and data.template_id:
                t = await templates_crud.get_template(db, data.template_id)
                template = WhatsAppTemplate.from_data(t) if t else None
            results.append(
                NotificationSettingType.from_data(event_type, meta, data, template)
            )
        return results

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def notification_catalog(self, info: Info) -> List[NotificationEventCatalog]:
        """Expose each event and the variables it can resolve, for the placeholder pickers."""
        catalog: List[NotificationEventCatalog] = []
        for event_type, meta in EVENT_TYPES.items():
            variables = [
                NotificationVariable(
                    key=key,
                    label=VARIABLES.get(key, {}).get("label", key),
                    sample=VARIABLES.get(key, {}).get("sample", ""),
                )
                for key in meta.get("variables", [])
            ]
            catalog.append(
                NotificationEventCatalog(
                    event_type=event_type,
                    label=str(meta.get("label", event_type)),
                    supports_offsets=bool(meta.get("supports_offsets", False)),
                    variables=variables,
                )
            )
        return catalog
