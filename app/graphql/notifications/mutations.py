"""GraphQL mutations for the automated notification configuration.

``save_notification_setting`` validates the chosen template + variable mapping before
persisting (the number of mapped variables must match the template's body placeholders, and
each variable must be one the event can actually resolve). ``run_notification_sweep`` runs the
renewal/expired sweeps on demand — handy for testing and as a manual "send now" action.
"""
import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import notificationsCrud as crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.notifications.types import (
    NotificationSettingResult,
    NotificationSettingType,
    SaveNotificationSettingInput,
    SweepResult,
)
from app.graphql.whatsapp.template_types import WhatsAppTemplate
from app.services.notification_service import EVENT_TYPES, run_all_sweeps
from app.services.whatsapp_template_components import parse_components, placeholder_count

logger = logging.getLogger(__name__)


@strawberry.type
class NotificationSettingsMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def save_notification_setting(
        self, info: Info, input: SaveNotificationSettingInput
    ) -> NotificationSettingResult:
        db: AsyncSession = info.context.db

        meta = EVENT_TYPES.get(input.event_type)
        if meta is None:
            return NotificationSettingResult(
                success=False, error="Tipo de evento desconocido."
            )

        mapping = [str(v) for v in (input.param_mapping or [])]
        offsets = [int(v) for v in (input.offsets_days or [])]

        tpl = None
        if input.template_id:
            tpl = await templates_crud.get_template_model(db, input.template_id)
            if tpl is None:
                return NotificationSettingResult(
                    success=False, error="Plantilla no encontrada."
                )

        # Can't enable an event without an approved template.
        if input.enabled:
            if tpl is None:
                return NotificationSettingResult(
                    success=False,
                    error="Selecciona una plantilla antes de activar el evento.",
                )
            if (tpl.template_status or "").upper() != "APPROVED":
                return NotificationSettingResult(
                    success=False,
                    error="La plantilla seleccionada no está aprobada por Meta.",
                )

        # Variable mapping must match the template body placeholders exactly.
        if tpl is not None:
            body_text, _, _ = parse_components(tpl.components)
            expected = placeholder_count(body_text)
            if len(mapping) != expected:
                return NotificationSettingResult(
                    success=False,
                    error=(
                        f"La plantilla requiere {expected} variable(s) en el cuerpo; "
                        f"se recibieron {len(mapping)}."
                    ),
                )

        allowed = set(meta.get("variables", []))
        invalid = [key for key in mapping if key not in allowed]
        if invalid:
            return NotificationSettingResult(
                success=False,
                error=f"Variable(s) no válida(s) para este evento: {', '.join(invalid)}.",
            )

        if offsets and not meta.get("supports_offsets", False):
            offsets = []
        if any(o <= 0 for o in offsets):
            return NotificationSettingResult(
                success=False, error="Los días de aviso deben ser mayores a 0."
            )

        try:
            await crud.upsert_setting(
                db,
                event_type=input.event_type,
                enabled=input.enabled,
                template_id=input.template_id,
                param_mapping=mapping,
                offsets_days=sorted(set(offsets)) if offsets else [],
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Error saving notification setting")
            return NotificationSettingResult(success=False, error=str(e))

        data = await crud.get_setting(db, input.event_type)
        template = WhatsAppTemplate.from_data(
            await templates_crud.get_template(db, data.template_id)
        ) if data and data.template_id else None
        return NotificationSettingResult(
            success=True,
            setting=NotificationSettingType.from_data(input.event_type, meta, data, template),
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def run_notification_sweep(self, info: Info) -> SweepResult:
        """Run the renewal + expired sweeps now (idempotent; safe to call repeatedly)."""
        try:
            stats = await run_all_sweeps()
        except Exception as e:  # noqa: BLE001
            logger.exception("Manual notification sweep failed")
            return SweepResult(success=False, error=str(e))

        sent = sum(group.get("sent", 0) for group in stats.values())
        skipped = sum(group.get("skipped", 0) for group in stats.values())
        failed = sum(group.get("failed", 0) for group in stats.values())
        return SweepResult(success=True, sent=sent, skipped=skipped, failed=failed)
