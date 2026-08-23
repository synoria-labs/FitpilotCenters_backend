"""GraphQL types for the marketing campaigns feature.

These power the desktop "Campañas" tab: the campaign list + create wizard
(``CampaignType`` / inputs), the audience preview (``AudiencePreview``), the per-campaign
results dashboard (``CampaignMetrics``) and the recipient drill-down (``CampaignRecipientType``).
``CampaignCatalog`` exposes objectives, audience predicates and the resolvable variable set so
the frontend pickers stay in sync with the backend.
"""
from datetime import datetime
from typing import List, Optional

import strawberry
from strawberry.scalars import JSON

from app.crud.campaignsCrud import CampaignData, CampaignRecipientData
from app.graphql.whatsapp.template_types import WhatsAppTemplate


@strawberry.type
class CampaignType:
    id: int
    name: str
    description: Optional[str]
    objective: str
    status: str
    audience_spec: Optional[JSON]
    template_id: Optional[int]
    param_mapping: Optional[List[str]]
    header_media_url: Optional[str]
    header_media_asset_id: Optional[int]
    header_text_param_key: Optional[str]
    button_url_param_key: Optional[str]
    location_param: Optional[JSON]
    marketing_campaign_id: Optional[int]
    scheduled_at: Optional[datetime]
    send_local_time: bool
    conversion_window_days: int
    conversion_metric: str
    recency_block_days: int
    throttle_per_minute: int
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    template: Optional[WhatsAppTemplate] = None

    @classmethod
    def from_data(
        cls, data: CampaignData, template: Optional[WhatsAppTemplate] = None
    ) -> "CampaignType":
        param_mapping = None
        if data.param_mapping is not None:
            param_mapping = [str(v) for v in data.param_mapping]
        return cls(
            id=data.id,
            name=data.name,
            description=data.description,
            objective=data.objective,
            status=data.status,
            audience_spec=data.audience_spec,
            template_id=data.template_id,
            param_mapping=param_mapping,
            header_media_url=data.header_media_url,
            header_media_asset_id=data.header_media_asset_id,
            header_text_param_key=data.header_text_param_key,
            button_url_param_key=data.button_url_param_key,
            location_param=data.location_param,
            marketing_campaign_id=data.marketing_campaign_id,
            scheduled_at=data.scheduled_at,
            send_local_time=bool(data.send_local_time),
            conversion_window_days=data.conversion_window_days,
            conversion_metric=data.conversion_metric,
            recency_block_days=data.recency_block_days,
            throttle_per_minute=data.throttle_per_minute,
            started_at=data.started_at,
            finished_at=data.finished_at,
            created_at=data.created_at,
            updated_at=data.updated_at,
            template=template,
        )


@strawberry.type
class CampaignRecipientType:
    id: int
    campaign_id: int
    person_id: Optional[int]
    subscription_id: Optional[int]
    phone_e164: Optional[str]
    wa_id: Optional[str]
    # The class this member books most, frozen when the audience was built.
    favorite_class_type_id: Optional[int]
    favorite_class_template_id: Optional[int]
    status: str
    skip_reason: Optional[str]
    wa_message_id: Optional[str]
    sent_at: Optional[datetime]
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]
    replied_at: Optional[datetime]
    error: Optional[str]
    converted: bool
    converted_at: Optional[datetime]
    targeted_at: Optional[datetime]

    @classmethod
    def from_data(cls, data: CampaignRecipientData) -> "CampaignRecipientType":
        return cls(
            id=data.id,
            campaign_id=data.campaign_id,
            person_id=data.person_id,
            subscription_id=data.subscription_id,
            phone_e164=data.phone_e164,
            wa_id=data.wa_id,
            favorite_class_type_id=data.favorite_class_type_id,
            favorite_class_template_id=data.favorite_class_template_id,
            status=data.status,
            skip_reason=data.skip_reason,
            wa_message_id=data.wa_message_id,
            sent_at=data.sent_at,
            delivered_at=data.delivered_at,
            read_at=data.read_at,
            replied_at=data.replied_at,
            error=data.error,
            converted=bool(data.converted),
            converted_at=data.converted_at,
            targeted_at=data.targeted_at,
        )


@strawberry.type
class CampaignMetrics:
    targeted: int = 0
    pending: int = 0
    sent: int = 0
    delivered: int = 0
    read: int = 0
    replied: int = 0
    failed: int = 0
    skipped: int = 0
    opted_out: int = 0
    converted: int = 0
    delivery_rate: float = 0.0
    read_rate: float = 0.0
    reply_rate: float = 0.0
    conversion_rate: float = 0.0
    revenue_recovered: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "CampaignMetrics":
        return cls(**{k: d.get(k, getattr(cls, k, 0)) for k in cls.__annotations__})


@strawberry.type
class CampaignMetricsEntry:
    """Metrics for one campaign, returned as part of a whole-page batch."""

    campaign_id: int
    metrics: CampaignMetrics


@strawberry.type
class AudienceSkipBreakdown:
    """Why members matching the segment will NOT receive the campaign."""

    no_phone: int = 0
    no_consent: int = 0
    recency_block: int = 0


@strawberry.type
class AudiencePreview:
    count: int = 0
    # How many will actually be messaged once consent / reachability / fatigue rules apply.
    # ``count`` alone overstates the audience, sometimes by a lot.
    reachable: int = 0
    skipped: AudienceSkipBreakdown = strawberry.field(default_factory=AudienceSkipBreakdown)
    sample: List[str] = strawberry.field(default_factory=list)


@strawberry.type
class CampaignVariableInfo:
    key: str
    label: str
    sample: str


@strawberry.type
class CampaignObjectiveInfo:
    key: str
    label: str
    variables: List[str]


@strawberry.type
class AudiencePredicateInfo:
    type: str
    label: str
    kind: str
    options: Optional[List[str]] = None
    hint: Optional[str] = None
    # Only the class-affinity predicate uses these: the granularities and matching modes
    # the picker may offer, so the frontend never hard-codes them.
    levels: Optional[List[str]] = None
    modes: Optional[List[str]] = None


@strawberry.type
class CampaignCatalog:
    objectives: List[CampaignObjectiveInfo]
    predicates: List[AudiencePredicateInfo]
    variables: List[CampaignVariableInfo]


# ---------------------------------------------------------------------------
# Inputs + result wrappers
# ---------------------------------------------------------------------------
@strawberry.input
class CreateCampaignInput:
    name: str
    objective: str
    description: Optional[str] = None
    audience_spec: Optional[JSON] = None
    template_id: Optional[int] = None
    param_mapping: Optional[List[str]] = None
    header_media_url: Optional[str] = None
    header_media_asset_id: Optional[int] = None
    header_text_param_key: Optional[str] = None
    button_url_param_key: Optional[str] = None
    location_param: Optional[JSON] = None
    marketing_campaign_id: Optional[int] = None
    conversion_window_days: int = 14
    conversion_metric: str = "payment"
    recency_block_days: int = 30
    throttle_per_minute: int = 60


@strawberry.input
class UpdateCampaignInput:
    name: Optional[str] = None
    objective: Optional[str] = None
    description: Optional[str] = None
    audience_spec: Optional[JSON] = None
    template_id: Optional[int] = None
    param_mapping: Optional[List[str]] = None
    header_media_url: Optional[str] = None
    header_media_asset_id: Optional[int] = None
    header_text_param_key: Optional[str] = None
    button_url_param_key: Optional[str] = None
    location_param: Optional[JSON] = None
    marketing_campaign_id: Optional[int] = None
    conversion_window_days: Optional[int] = None
    conversion_metric: Optional[str] = None
    recency_block_days: Optional[int] = None
    throttle_per_minute: Optional[int] = None


@strawberry.type
class CampaignResult:
    success: bool = False
    campaign: Optional[CampaignType] = None
    error: Optional[str] = None


@strawberry.type
class CampaignMutationResult:
    success: bool = False
    error: Optional[str] = None


@strawberry.type
class CampaignRunResult:
    success: bool = False
    paused: bool = False
    # Recipients parked for later (quiet hours / mid-purchase). Not a failure, but the
    # operator needs to know the blast did not all go out just now.
    deferred: bool = False
    dry_run: bool = False
    targeted: int = 0
    pending: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    rendered_preview: Optional[str] = None
    error: Optional[str] = None
