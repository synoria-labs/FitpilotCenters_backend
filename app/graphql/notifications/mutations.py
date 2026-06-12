"""GraphQL mutations for the automated notification configuration.

``save_notification_setting`` validates the chosen template + variable mapping before
persisting (the number of mapped variables must match the template's body placeholders, and
each variable must be one the event can actually resolve). ``run_notification_sweep`` runs the
renewal/expired sweeps on demand — handy for testing and as a manual "send now" action.
"""
import logging
from typing import Optional
from urllib.parse import urlparse

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import notificationsCrud as crud
from app.crud import whatsappMediaAssetsCrud as media_crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.notifications.types import (
    NotificationRetryResult,
    NotificationSettingResult,
    NotificationSettingType,
    SaveNotificationSettingInput,
    SweepResult,
)
from app.graphql.whatsapp.template_types import WhatsAppTemplate
from app.services.notification_service import EVENT_TYPES, retry_failed_log, run_all_sweeps
from app.services import whatsapp_media_assets_service as media_service
from app.services.whatsapp_template_components import (
    parse_components,
    placeholder_count,
    required_header_media_format,
)

logger = logging.getLogger(__name__)


def _clean_header_media_url(value: Optional[str]) -> Optional[str]:
    url = (value or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("La URL de media debe ser una URL pública HTTPS.")
    return url


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
        try:
            header_media_url = _clean_header_media_url(input.header_media_url)
        except ValueError as exc:
            return NotificationSettingResult(success=False, error=str(exc))
        header_media_asset_id = getattr(input, "header_media_asset_id", None)

        tpl = None
        if input.template_id:
            tpl = await templates_crud.get_template_model(db, input.template_id)
            if tpl is None:
                return NotificationSettingResult(
                    success=False, error="Plantilla no encontrada."
                )

        # Can't enable an event without a synchronized, approved template.
        if input.enabled:
            if tpl is None:
                return NotificationSettingResult(
                    success=False,
                    error="Selecciona una plantilla antes de activar el evento.",
                )
            if not tpl.meta_template_id:
                return NotificationSettingResult(
                    success=False,
                    error="La plantilla no está sincronizada con Meta. Sincroniza plantillas primero.",
                )
            if (tpl.template_status or "").upper() != "APPROVED":
                return NotificationSettingResult(
                    success=False,
                    error="La plantilla seleccionada no está aprobada por Meta.",
                )

            media_format = required_header_media_format(tpl.components)
            if media_format:
                asset = None
                if header_media_asset_id:
                    asset = await media_crud.get_asset_model(db, header_media_asset_id)
                    try:
                        media_service.assert_asset_matches_header(asset, media_format)
                    except media_service.MediaAssetError as exc:
                        return NotificationSettingResult(success=False, error=str(exc))
                    header_media_url = asset.public_url
                if not header_media_url:
                    return NotificationSettingResult(
                        success=False,
                        error=(
                            f"La plantilla requiere media de encabezado ({media_format}); "
                            "selecciona un asset o agrega una URL HTTPS."
                        ),
                    )
            elif header_media_asset_id:
                return NotificationSettingResult(
                    success=False,
                    error="La plantilla seleccionada no requiere media de encabezado.",
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
                header_media_url=(
                    header_media_url
                    if tpl is not None and required_header_media_format(tpl.components)
                    else None
                ),
                header_media_asset_id=(
                    header_media_asset_id
                    if tpl is not None and required_header_media_format(tpl.components)
                    else None
                ),
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

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def retry_notification_log(self, info: Info, log_id: int) -> NotificationRetryResult:
        """Retry one failed notification log using the current saved configuration."""
        db: AsyncSession = info.context.db
        try:
            status = await retry_failed_log(db, log_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("Manual notification retry failed")
            return NotificationRetryResult(success=False, status="failed", error=str(e))

        errors = {
            "not_found": "No se encontró el intento original.",
            "not_failed": "Solo se pueden reintentar notificaciones fallidas.",
            "no_person": "No se encontró el socio asociado al intento.",
            "disabled": "El evento está desactivado o sin plantilla configurada.",
            "no_template": "La plantilla actual no está disponible o no está aprobada.",
            "no_phone": "El socio no tiene teléfono válido.",
            "opted_out": "El socio revocó consentimiento de WhatsApp.",
            "duplicate": "El reintento ya fue registrado.",
            "failed": "El reintento falló; revisa el log para ver el error de Meta.",
        }
        if status == "sent":
            return NotificationRetryResult(success=True, status=status)
        return NotificationRetryResult(
            success=False,
            status=status,
            error=errors.get(status, f"No se envió la notificación ({status})."),
        )
