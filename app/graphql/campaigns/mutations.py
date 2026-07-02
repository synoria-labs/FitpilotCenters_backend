"""GraphQL mutations for the marketing campaigns feature.

Validation mirrors ``save_notification_setting``: a chosen template must be synced + APPROVED
and the variable mapping must match its body placeholders and use only variables the objective
can resolve. Sends are fired in the background (``asyncio.create_task``) after the request
commits, exactly like the notification dispatch, so a large blast never blocks the API.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import campaignsCrud as crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.crud.permissions import SEND_CAMPAIGNS
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.campaigns.types import (
    CampaignMutationResult,
    CampaignResult,
    CampaignRunResult,
    CampaignType,
    CreateCampaignInput,
    UpdateCampaignInput,
)
from app.graphql.whatsapp.template_types import WhatsAppTemplate
from app.models.campaignsModel import (
    STATUS_CANCELED,
    STATUS_COMPLETED,
    STATUS_DRAFT,
    STATUS_PAUSED,
    STATUS_SCHEDULED,
    STATUS_SENDING,
)
from app.services import campaign_service
from app.services.campaign_service import CAMPAIGN_OBJECTIVES, allowed_variables_for
from app.services.whatsapp_template_components import parse_components, placeholder_count

logger = logging.getLogger(__name__)


async def _validate_template_mapping(
    db: AsyncSession, objective: str, template_id: Optional[int], mapping: list
) -> Optional[str]:
    if not template_id:
        if mapping:
            return "No se pueden mapear variables sin una plantilla seleccionada."
        return None
    tpl = await templates_crud.get_template_model(db, template_id)
    if tpl is None:
        return "Plantilla no encontrada."
    body_text, _, _ = parse_components(tpl.components)
    expected = placeholder_count(body_text)
    if len(mapping) != expected:
        return (
            f"La plantilla requiere {expected} variable(s) en el cuerpo; "
            f"se recibieron {len(mapping)}."
        )
    allowed = allowed_variables_for(objective)
    invalid = [key for key in mapping if key not in allowed]
    if invalid:
        return f"Variable(s) no válida(s) para este objetivo: {', '.join(invalid)}."
    return None


async def _require_sendable(db: AsyncSession, campaign) -> Optional[str]:
    if not campaign.template_id:
        return "Selecciona una plantilla antes de enviar o programar."
    tpl = await templates_crud.get_template_model(db, campaign.template_id)
    if tpl is None:
        return "Plantilla no encontrada."
    if not tpl.meta_template_id:
        return "La plantilla no está sincronizada con Meta. Sincroniza plantillas primero."
    if (tpl.template_status or "").upper() != "APPROVED":
        return "La plantilla seleccionada no está aprobada por Meta."
    if not campaign.audience_spec:
        return "Define una audiencia antes de enviar."
    return None


async def _campaign_result(db: AsyncSession, campaign_id: int) -> CampaignResult:
    data = await crud.get_campaign(db, campaign_id)
    if data is None:
        return CampaignResult(success=False, error="Campaña no encontrada.")
    template = None
    if data.template_id:
        t = await templates_crud.get_template(db, data.template_id)
        template = WhatsAppTemplate.from_data(t) if t else None
    return CampaignResult(success=True, campaign=CampaignType.from_data(data, template))


@strawberry.type
class CampaignsMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_campaign(self, info: Info, input: CreateCampaignInput) -> CampaignResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignResult(success=False, error=error)
        if input.objective not in CAMPAIGN_OBJECTIVES:
            return CampaignResult(success=False, error="Objetivo de campaña desconocido.")
        if not (input.name or "").strip():
            return CampaignResult(success=False, error="La campaña necesita un nombre.")

        mapping = [str(v) for v in (input.param_mapping or [])]
        err = await _validate_template_mapping(db, input.objective, input.template_id, mapping)
        if err:
            return CampaignResult(success=False, error=err)

        try:
            campaign = await crud.create_campaign(
                db,
                created_by=getattr(info.context, "account_id", None),
                name=input.name.strip(),
                description=input.description,
                objective=input.objective,
                audience_spec=input.audience_spec,
                template_id=input.template_id,
                param_mapping=mapping,
                header_media_url=input.header_media_url,
                header_media_asset_id=input.header_media_asset_id,
                marketing_campaign_id=input.marketing_campaign_id,
                conversion_window_days=input.conversion_window_days,
                conversion_metric=input.conversion_metric,
                recency_block_days=input.recency_block_days,
                throttle_per_minute=input.throttle_per_minute,
            )
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("create_campaign failed")
            return CampaignResult(success=False, error=str(exc))
        return await _campaign_result(db, campaign.id)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_campaign(
        self, info: Info, id: int, input: UpdateCampaignInput
    ) -> CampaignResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignResult(success=False, error="Campaña no encontrada.")
        if campaign.status != STATUS_DRAFT:
            return CampaignResult(
                success=False, error="Solo se pueden editar campañas en borrador."
            )

        objective = input.objective or campaign.objective
        if objective not in CAMPAIGN_OBJECTIVES:
            return CampaignResult(success=False, error="Objetivo de campaña desconocido.")

        fields = {}
        for key in (
            "name", "description", "objective", "audience_spec", "template_id",
            "header_media_url", "header_media_asset_id", "marketing_campaign_id",
            "conversion_window_days", "conversion_metric", "recency_block_days",
            "throttle_per_minute",
        ):
            value = getattr(input, key)
            if value is not None:
                fields[key] = value
        if input.param_mapping is not None:
            fields["param_mapping"] = [str(v) for v in input.param_mapping]

        template_id = fields.get("template_id", campaign.template_id)
        mapping = fields.get(
            "param_mapping",
            [str(v) for v in (campaign.param_mapping or [])],
        )
        err = await _validate_template_mapping(db, objective, template_id, mapping)
        if err:
            return CampaignResult(success=False, error=err)

        try:
            await crud.update_campaign(db, campaign, **fields)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("update_campaign failed")
            return CampaignResult(success=False, error=str(exc))
        return await _campaign_result(db, id)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def delete_campaign(self, info: Info, id: int) -> CampaignMutationResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignMutationResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignMutationResult(success=False, error="Campaña no encontrada.")
        if campaign.status not in (STATUS_DRAFT, STATUS_CANCELED):
            return CampaignMutationResult(
                success=False, error="Solo se pueden eliminar campañas en borrador o canceladas."
            )
        await crud.delete_campaign(db, campaign)
        return CampaignMutationResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def build_campaign_audience(self, info: Info, id: int) -> CampaignRunResult:
        """Materialize campaign_recipients for preview before sending."""
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignRunResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignRunResult(success=False, error="Campaña no encontrada.")
        try:
            stats = await campaign_service.build_campaign_audience(db, id)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            logger.exception("build_campaign_audience failed")
            return CampaignRunResult(success=False, error=str(exc))
        return CampaignRunResult(
            success=True,
            targeted=stats.get("targeted", 0),
            pending=stats.get("pending", 0),
            skipped=stats.get("skipped", 0),
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def schedule_campaign(
        self, info: Info, id: int, scheduled_at: datetime, send_local_time: bool = False
    ) -> CampaignResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignResult(success=False, error="Campaña no encontrada.")
        if campaign.status not in (STATUS_DRAFT, STATUS_SCHEDULED):
            return CampaignResult(
                success=False, error="Solo se pueden programar campañas en borrador."
            )
        err = await _require_sendable(db, campaign)
        if err:
            return CampaignResult(success=False, error=err)
        await crud.set_campaign_status(
            db, campaign, status=STATUS_SCHEDULED,
            scheduled_at=scheduled_at, send_local_time=send_local_time,
        )
        return await _campaign_result(db, id)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def trigger_campaign(
        self, info: Info, id: int, dry_run: bool = False
    ) -> CampaignRunResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignRunResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignRunResult(success=False, error="Campaña no encontrada.")

        err = await _require_sendable(db, campaign)
        if err:
            return CampaignRunResult(success=False, error=err)

        if dry_run:
            result = await campaign_service.run_campaign(id, dry_run=True)
            if not result.get("ok"):
                return CampaignRunResult(success=False, error=result.get("error"))
            return CampaignRunResult(
                success=True,
                dry_run=True,
                pending=result.get("pending", 0),
                skipped=result.get("skipped", 0),
                rendered_preview=result.get("rendered_preview"),
            )

        if campaign.status in (STATUS_SENDING,):
            return CampaignRunResult(success=False, error="La campaña ya se está enviando.")

        # Fire-after-request: dispatch in the background so the API returns immediately.
        asyncio.create_task(campaign_service.trigger_in_background(id))
        return CampaignRunResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def pause_campaign(self, info: Info, id: int) -> CampaignResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignResult(success=False, error="Campaña no encontrada.")
        if campaign.status not in (STATUS_SENDING, STATUS_SCHEDULED):
            return CampaignResult(
                success=False, error="Solo se pueden pausar campañas activas o programadas."
            )
        await crud.set_campaign_status(db, campaign, status=STATUS_PAUSED)
        return await _campaign_result(db, id)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def resume_campaign(self, info: Info, id: int) -> CampaignRunResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignRunResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignRunResult(success=False, error="Campaña no encontrada.")
        if campaign.status != STATUS_PAUSED:
            return CampaignRunResult(success=False, error="La campaña no está pausada.")
        await crud.set_campaign_status(db, campaign, status=STATUS_SENDING)
        asyncio.create_task(campaign_service.trigger_in_background(id))
        return CampaignRunResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def cancel_campaign(self, info: Info, id: int) -> CampaignResult:
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignResult(success=False, error="Campaña no encontrada.")
        if campaign.status == STATUS_COMPLETED:
            return CampaignResult(success=False, error="La campaña ya finalizó.")
        await crud.set_campaign_status(db, campaign, status=STATUS_CANCELED)
        return await _campaign_result(db, id)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def retry_campaign_failures(self, info: Info, id: int) -> CampaignRunResult:
        """Re-dispatch failed recipients (run_campaign only picks pending/failed)."""
        db: AsyncSession = info.context.db
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignRunResult(success=False, error=error)
        campaign = await crud.get_campaign_model(db, id)
        if campaign is None:
            return CampaignRunResult(success=False, error="Campaña no encontrada.")
        if campaign.status == STATUS_SENDING:
            return CampaignRunResult(success=False, error="La campaña ya se está enviando.")
        asyncio.create_task(campaign_service.trigger_in_background(id))
        return CampaignRunResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def run_campaign_sweep(self, info: Info) -> CampaignRunResult:
        """Run scheduled-campaign + conversion sweeps now (testing / manual trigger)."""
        error = await require_capability(info, SEND_CAMPAIGNS)
        if error:
            return CampaignRunResult(success=False, error=error)
        try:
            send_stats = await campaign_service.run_campaign_sweep()
            await campaign_service.run_conversion_sweep()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Manual campaign sweep failed")
            return CampaignRunResult(success=False, error=str(exc))
        return CampaignRunResult(
            success=True,
            sent=send_stats.get("sent", 0),
            failed=send_stats.get("failed", 0),
        )
