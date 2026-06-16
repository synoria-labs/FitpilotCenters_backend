"""Marketing campaign models for FitPilot.

A *campaign* is a user-initiated WhatsApp broadcast that sits beside the event-driven
notification system (``notification_settings``/``notification_log``). It reuses the same
send-and-persist plumbing, the same ``dedup_key`` idempotency primitive, the same template /
consent / media infrastructure and the same APScheduler — but campaigns are many, parametric
and audience-targeted, where notifications are a singleton per business event.

Three tables power Phase 1 (recapture-first, members only, WhatsApp only):

* ``campaigns`` — the campaign definition: objective, declarative audience filter, the Meta
  template + variable mapping to send, schedule, conversion window and throttle.
* ``campaign_variants`` — A/B variants. First-class in the schema from day one (so adding A/B
  later is not a painful migration) but the MVP auto-creates a single variant ``A`` per
  campaign and hides the picker in the UI.
* ``campaign_recipients`` — the frozen audience snapshot **and** the per-recipient ledger:
  one row per (campaign, person). Each row is simultaneously the idempotency claim
  (``dedup_key``), the delivery tracking record (denormalized sent/delivered/read timestamps),
  and the conversion attribution record. This intentionally replaces a separate send-log.

Like the other FitPilot-native tables these use TIMESTAMPTZ and BigInteger ids assigned by the
database sequence (never set ``id`` manually).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, JSON,
    String, Text, TIMESTAMP, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.postgresql import Base


# Canonical campaign objectives and statuses. Keep in sync with CAMPAIGN_OBJECTIVES in
# app/services/campaign_service.py (single source of truth for behaviour/labels).
OBJECTIVE_WIN_BACK = "win_back"
OBJECTIVE_RENEWAL_PUSH = "renewal_push"
OBJECTIVE_ENGAGEMENT = "engagement"
OBJECTIVE_BROADCAST = "broadcast"
CAMPAIGN_OBJECTIVE_VALUES = (
    OBJECTIVE_WIN_BACK, OBJECTIVE_RENEWAL_PUSH, OBJECTIVE_ENGAGEMENT, OBJECTIVE_BROADCAST,
)

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_SENDING = "sending"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELED = "canceled"
CAMPAIGN_STATUS_VALUES = (
    STATUS_DRAFT, STATUS_SCHEDULED, STATUS_SENDING, STATUS_PAUSED, STATUS_COMPLETED, STATUS_CANCELED,
)

# Recipient lifecycle. ``sending`` is the transient compare-and-set claim state.
RECIPIENT_STATUS_VALUES = (
    "pending", "sending", "sent", "delivered", "read", "replied",
    "failed", "skipped", "opted_out",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(Base):
    """A user-defined WhatsApp broadcast campaign (editable from the desktop frontend)."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    objective: Mapped[str] = mapped_column(String(30), nullable=False, default=OBJECTIVE_WIN_BACK)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=STATUS_DRAFT)

    # Declarative audience filter (predicate AST) compiled by segmentation_service.
    audience_spec: Mapped[Optional[dict]] = mapped_column(JSON)

    # Content: an approved Meta template + its variable mapping + optional header media.
    template_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("whatsapp_templates.id", ondelete="SET NULL")
    )
    param_mapping: Mapped[Optional[list]] = mapped_column(JSON)
    header_media_url: Mapped[Optional[str]] = mapped_column(String(1000))
    header_media_asset_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("whatsapp_media_assets.id", ondelete="SET NULL")
    )

    # Optional link to an ad-attribution campaign (leadsModel.MarketingCampaign) for ROAS
    # roll-up. The campaign config is NOT stored there — that table has no template/recipient.
    marketing_campaign_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("marketing_campaigns.id", ondelete="SET NULL")
    )

    # Scheduling. ``scheduled_at`` NULL => send immediately when triggered.
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    send_local_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Tracking config.
    conversion_window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    conversion_metric: Mapped[str] = mapped_column(String(20), nullable=False, default="payment")
    recency_block_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    throttle_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    started_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    created_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("accounts.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "objective IN ('win_back','renewal_push','engagement','broadcast')",
            name="ck_campaign_objective",
        ),
        CheckConstraint(
            "status IN ('draft','scheduled','sending','paused','completed','canceled')",
            name="ck_campaign_status",
        ),
        Index("idx_campaigns_status_scheduled", "status", "scheduled_at"),
        Index("idx_campaigns_objective_created", "objective", "created_at"),
    )


class CampaignVariant(Base):
    """An A/B variant of a campaign (MVP: a single auto-created variant 'A')."""

    __tablename__ = "campaign_variants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    variant_code: Mapped[str] = mapped_column(String(8), nullable=False, default="A")
    # Per-variant overrides (NULL => fall back to the campaign defaults).
    template_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("whatsapp_templates.id", ondelete="SET NULL")
    )
    param_mapping: Mapped[Optional[list]] = mapped_column(JSON)
    header_media_url: Mapped[Optional[str]] = mapped_column(String(1000))
    header_media_asset_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("whatsapp_media_assets.id", ondelete="SET NULL")
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("uq_campaign_variant_code", "campaign_id", "variant_code", unique=True),
    )


class CampaignRecipient(Base):
    """Frozen audience snapshot + per-recipient send/track/convert ledger.

    One row per (campaign, person). The ``dedup_key`` unique index makes audience builds and
    re-sends idempotent; the denormalized status/timestamps make per-campaign dashboards a
    single-table aggregation.
    """

    __tablename__ = "campaign_recipients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("campaign_variants.id", ondelete="SET NULL")
    )
    person_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("people.id", ondelete="CASCADE")
    )
    # Acquisition audiences (Phase 2). Recapture MVP only sets person_id.
    lead_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("leads.id", ondelete="SET NULL")
    )
    # Snapshot of the subscription that made this person eligible (conversion baseline).
    subscription_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    # Denormalized at snapshot time so the list survives later person edits.
    phone_e164: Mapped[Optional[str]] = mapped_column(String(32))
    wa_id: Mapped[Optional[str]] = mapped_column(String(100))

    dedup_key: Mapped[str] = mapped_column(String(140), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    skip_reason: Mapped[Optional[str]] = mapped_column(String(40))

    # Links to the persisted chat message + Meta delivery tracking (no FK: messages is an
    # externally-managed table, mirror notification_log which also stores ids loosely).
    wa_message_id: Mapped[Optional[str]] = mapped_column(String(120))
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    sent_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    delivered_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    read_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    replied_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)

    converted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    converted_at: Mapped[Optional[datetime]] = mapped_column(TIMESTAMP(timezone=True))
    conversion_payment_id: Mapped[Optional[int]] = mapped_column(BigInteger)

    targeted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','sending','sent','delivered','read','replied',"
            "'failed','skipped','opted_out')",
            name="ck_campaign_recipient_status",
        ),
        CheckConstraint(
            "person_id IS NOT NULL OR lead_id IS NOT NULL OR phone_e164 IS NOT NULL",
            name="ck_campaign_recipient_target",
        ),
        Index("uq_campaign_recipient_dedup", "dedup_key", unique=True),
        Index("idx_campaign_recipient_campaign_status", "campaign_id", "status"),
        Index("idx_campaign_recipient_person_targeted", "person_id", "targeted_at"),
        Index("idx_campaign_recipient_wa_message", "wa_message_id"),
    )
