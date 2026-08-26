"""GraphQL queries for the marketing campaigns feature."""
import logging
from typing import List, Optional

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.scalars import JSON
from strawberry.types import Info

from app.crud import campaignsCrud as crud
from app.crud import whatsappTemplatesCrud as templates_crud
from app.crud.permissions import SEND_CAMPAIGNS
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.campaigns.types import (
    AudiencePredicateInfo,
    AudiencePreview,
    AudienceSkipBreakdown,
    CampaignCatalog,
    CampaignMetrics,
    CampaignMetricsEntry,
    CampaignObjectiveInfo,
    CampaignRecipientType,
    CampaignType,
    CampaignVariableInfo,
)
from app.graphql.whatsapp.template_types import WhatsAppTemplate
from app.services import segmentation_service
from app.services.campaign_service import (
    AUDIENCE_PREDICATES,
    CAMPAIGN_OBJECTIVES,
    CAMPAIGN_VARIABLES,
    get_metrics,
    get_metrics_batch,
)
from app.services.notification_service import VARIABLES

logger = logging.getLogger(__name__)


async def _load_template(db: AsyncSession, template_id: Optional[int]) -> Optional[WhatsAppTemplate]:
    if not template_id:
        return None
    data = await templates_crud.get_template(db, template_id)
    return WhatsAppTemplate.from_data(data) if data else None


@strawberry.type
class CampaignsQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaigns(
        self,
        info: Info,
        status: Optional[str] = None,
        objective: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[CampaignType]:
        if await require_capability(info, SEND_CAMPAIGNS):
            return []
        db: AsyncSession = info.context.db
        rows = await crud.list_campaigns(
            db, status=status, objective=objective, limit=limit, offset=offset
        )
        return [CampaignType.from_data(r) for r in rows]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaign(self, info: Info, id: int) -> Optional[CampaignType]:
        if await require_capability(info, SEND_CAMPAIGNS):
            return None
        db: AsyncSession = info.context.db
        data = await crud.get_campaign(db, id)
        if data is None:
            return None
        template = await _load_template(db, data.template_id)
        return CampaignType.from_data(data, template)

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaign_recipients(
        self,
        info: Info,
        campaign_id: int,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[CampaignRecipientType]:
        if await require_capability(info, SEND_CAMPAIGNS):
            return []
        db: AsyncSession = info.context.db
        rows = await crud.list_recipients(
            db, campaign_id, status=status, limit=limit, offset=offset
        )
        return [CampaignRecipientType.from_data(r) for r in rows]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaign_metrics(self, info: Info, campaign_id: int) -> CampaignMetrics:
        if await require_capability(info, SEND_CAMPAIGNS):
            return CampaignMetrics()
        db: AsyncSession = info.context.db
        return CampaignMetrics.from_dict(await get_metrics(db, campaign_id))

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def preview_audience(self, info: Info, audience_spec: Optional[JSON] = None) -> AudiencePreview:
        """Count + sample of members matching a spec. Sends nothing; safe to call live."""
        if await require_capability(info, SEND_CAMPAIGNS):
            return AudiencePreview()
        db: AsyncSession = info.context.db
        try:
            result = await segmentation_service.preview_audience(db, audience_spec)
        except segmentation_service.SegmentationError as exc:
            logger.info("preview_audience invalid spec: %s", exc)
            return AudiencePreview()
        skipped = result.get("skipped") or {}
        return AudiencePreview(
            count=result["count"],
            reachable=result.get("reachable", result["count"]),
            skipped=AudienceSkipBreakdown(
                no_phone=skipped.get("no_phone", 0),
                no_consent=skipped.get("no_consent", 0),
                recency_block=skipped.get("recency_block", 0),
            ),
            sample=result["sample"],
        )

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaign_metrics_batch(
        self, info: Info, campaign_ids: List[int]
    ) -> List[CampaignMetricsEntry]:
        """Metrics for a whole page of campaigns in three queries instead of three per row."""
        if await require_capability(info, SEND_CAMPAIGNS):
            return []
        db: AsyncSession = info.context.db
        batch = await get_metrics_batch(db, campaign_ids)
        return [
            CampaignMetricsEntry(campaign_id=cid, metrics=CampaignMetrics.from_dict(data))
            for cid, data in batch.items()
        ]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def campaign_catalog(self, info: Info) -> CampaignCatalog:
        """Objectives, audience predicates and resolvable variables for the wizard."""
        objectives = [
            CampaignObjectiveInfo(
                key=key,
                label=str(meta.get("label", key)),
                variables=list(meta.get("variables", [])),
            )
            for key, meta in CAMPAIGN_OBJECTIVES.items()
        ]
        predicates = [
            AudiencePredicateInfo(
                type=str(p.get("type")),
                label=str(p.get("label", p.get("type"))),
                kind=str(p.get("kind", "")),
                options=list(p["options"]) if p.get("options") else None,
                hint=p.get("hint"),
                levels=list(p["levels"]) if p.get("levels") else None,
                modes=list(p["modes"]) if p.get("modes") else None,
            )
            for p in AUDIENCE_PREDICATES
        ]
        # Member variables plus the campaign-only class-affinity ones, in that order so the
        # picker lists the familiar fields first.
        variables = [
            CampaignVariableInfo(
                key=key,
                label=meta.get("label", key),
                sample=meta.get("sample", ""),
            )
            for key, meta in {**VARIABLES, **CAMPAIGN_VARIABLES}.items()
        ]
        return CampaignCatalog(objectives=objectives, predicates=predicates, variables=variables)
