"""Assemble the special-events GraphQL objects from the CRUD layer.

Shared by queries and mutations, so every mutation answers with the recomputed screen (counts,
blockers, seats) instead of making the client query again to see what it just changed.
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mercadopago_config import mercadopago_config
from app.core.timezones import to_local
from app.crud.special_events import events as events_crud
from app.crud.special_events import orders as orders_crud
from app.crud.special_events import takeover as takeover_crud
from app.crud.special_events.common import local_schedule_label
from app.graphql.special_events.types import (
    AffectedClientType,
    AffectedSessionType,
    LinkedCampaignType,
    RestoreDecisionType,
    RestorePreviewType,
    SpecialEventDetailType,
    SpecialEventDisplacementType,
    SpecialEventOrderType,
    SpecialEventSeatType,
    SpecialEventTakeoverType,
    SpecialEventType,
    TakeoverPreviewType,
)
from app.models import Account, Campaign, ClassSession, People, Venue
from app.models.specialEventModel import (
    FOLLOW_UP_RESOLVED,
    TAKEOVER_ACTIVE,
    SpecialEvent,
    SpecialEventDisplacement,
    SpecialEventTakeover,
)


def payments_status() -> tuple[bool, Optional[str]]:
    """Effective payment configuration, as staff need to read it before publishing."""
    if mercadopago_config.is_configured():
        if mercadopago_config.is_test():
            return True, "Mercado Pago está en modo de prueba: los pagos no son reales."
        return True, None
    return False, (
        "Mercado Pago no está configurado en el servidor: el asistente de WhatsApp no podrá "
        "generar enlaces de pago para este evento."
    )


async def build_event(db: AsyncSession, event: SpecialEvent) -> SpecialEventType:
    avail = await events_crud.availability(db, event)
    offered = await events_crud.offered_seat_ids(db, event)
    labels = await events_crud.seat_labels(db, offered)
    held = set(avail.held_seat_ids)
    sold = set(avail.sold_seat_ids)
    seats = [
        SpecialEventSeatType(
            seat_id=sid,
            label=labels.get(sid, str(sid)),
            status="sold" if sid in sold else "held" if sid in held else "free",
        )
        for sid in offered
    ]
    venue = await db.get(Venue, event.venue_id)
    instructor = await db.get(People, event.instructor_id) if event.instructor_id else None
    blockers = (
        await events_crud.publish_blockers(db, event) if event.status == "draft" else []
    )
    active_takeovers = int(
        (
            await db.execute(
                select(func.count(SpecialEventTakeover.id)).where(
                    SpecialEventTakeover.event_id == event.id,
                    SpecialEventTakeover.status == TAKEOVER_ACTIVE,
                )
            )
        ).scalar()
        or 0
    )
    pending_follow_ups = int(
        (
            await db.execute(
                select(func.count(SpecialEventDisplacement.id)).where(
                    SpecialEventDisplacement.event_id == event.id,
                    SpecialEventDisplacement.follow_up_status != FOLLOW_UP_RESOLVED,
                )
            )
        ).scalar()
        or 0
    )
    campaigns = (
        await db.execute(
            select(Campaign.id, Campaign.name, Campaign.status)
            .where(Campaign.special_event_id == event.id)
            .order_by(Campaign.id)
        )
    ).all()
    ready, warning = payments_status()
    local_start = to_local(event.start_at, event.timezone)
    local_end = to_local(event.end_at, event.timezone) if event.end_at else None
    return SpecialEventType(
        id=event.id,
        name=event.name,
        description=event.description,
        includes=event.includes,
        address=event.address,
        venue_id=event.venue_id,
        venue_name=venue.name if venue else None,
        venue_address=venue.address if venue else None,
        class_type_id=event.class_type_id,
        instructor_id=event.instructor_id,
        instructor_name=instructor.full_name if instructor else None,
        timezone=event.timezone,
        start_at=event.start_at,
        end_at=event.end_at,
        local_date=local_start.date().isoformat(),
        local_start_time=local_start.strftime("%H:%M"),
        local_end_time=local_end.strftime("%H:%M") if local_end else None,
        schedule_label=local_schedule_label(event.start_at, event.end_at, event.timezone),
        price=float(event.price),
        currency=event.currency,
        capacity=event.capacity,
        hold_minutes=event.hold_minutes,
        status=event.status,
        sold=avail.paid,
        held=avail.held,
        available=avail.available,
        sales_open=avail.sales_open,
        sales_closed_reason=avail.sales_closed_reason,
        seats=seats,
        publish_blockers=blockers,
        payments_ready=ready,
        payments_warning=warning,
        active_takeovers=active_takeovers,
        pending_follow_ups=pending_follow_ups,
        linked_campaigns=[LinkedCampaignType(id=c, name=n, status=s) for c, n, s in campaigns],
        cancel_reason=event.cancel_reason,
        published_at=event.published_at,
        canceled_at=event.canceled_at,
        created_at=event.created_at,
    )


def build_order(row: orders_crud.OrderRow) -> SpecialEventOrderType:
    return SpecialEventOrderType(
        id=row.id,
        participant_name=row.participant_name,
        participant_phone=row.participant_phone,
        person_id=row.person_id,
        seat_id=row.seat_id,
        seat_label=row.seat_label,
        status=row.status,
        channel=row.channel,
        amount=float(row.amount),
        currency=row.currency,
        payment_method=row.payment_method,
        paid_at=row.paid_at,
        hold_expires_at=row.hold_expires_at,
        mp_status=row.mp_status,
        mp_payment_id=row.mp_payment_id,
        review_reason=row.review_reason,
        cancel_reason=row.cancel_reason,
        refunded_at=row.refunded_at,
        notes=row.notes,
        created_at=row.created_at,
    )


def build_displacement(row: takeover_crud.DisplacementRow, tz_name: Optional[str] = None) -> SpecialEventDisplacementType:
    return SpecialEventDisplacementType(
        id=row.id,
        event_id=row.event_id,
        event_name=row.event_name,
        session_id=row.session_id,
        session_name=row.session_name,
        session_start_at=row.session_start_at,
        session_label=local_schedule_label(row.session_start_at, row.session_end_at, tz_name),
        session_status=row.session_status,
        person_id=row.person_id,
        person_name=row.person_name,
        phone=row.phone,
        seat_label=row.seat_label,
        reservation_id=row.reservation_id,
        reservation_status=row.reservation_status,
        reservation_source=row.reservation_source,
        plan_name=row.plan_name,
        membership_ends_at=row.membership_ends_at,
        reason=row.reason,
        follow_up_status=row.follow_up_status,
        notified_at=row.notified_at,
        notified_by_name=row.notified_by_name,
        notification_channel=row.notification_channel,
        notification_notes=row.notification_notes,
        compensation_type=row.compensation_type,
        compensation_notes=row.compensation_notes,
        resolved_at=row.resolved_at,
        resolved_by_name=row.resolved_by_name,
        restoration_status=row.restoration_status,
        restoration_note=row.restoration_note,
        restored_at=row.restored_at,
    )


async def _account_names(db: AsyncSession, account_ids) -> dict:
    ids = [a for a in set(account_ids) if a]
    if not ids:
        return {}
    rows = await db.execute(
        select(Account.id, People.full_name)
        .join(People, People.id == Account.person_id)
        .where(Account.id.in_(ids))
    )
    return {aid: name for aid, name in rows.all()}


async def build_detail(db: AsyncSession, event_id: int) -> Optional[SpecialEventDetailType]:
    event = await events_crud.get_event(db, event_id)
    if event is None:
        return None
    orders = await orders_crud.list_orders(db, event.id)
    displacements = await takeover_crud.list_displacements(db, event_id=event.id)
    takeovers = await takeover_crud.takeovers_for_event(db, event.id)
    names = await _account_names(
        db, [t.applied_by for t in takeovers] + [t.restored_by for t in takeovers]
    )
    takeover_types: List[SpecialEventTakeoverType] = []
    for t in takeovers:
        session = await db.get(ClassSession, t.session_id)
        takeover_types.append(
            SpecialEventTakeoverType(
                id=t.id,
                session_id=t.session_id,
                session_name=(session.name if session else None) or "Clase",
                session_label=local_schedule_label(session.start_at, session.end_at, event.timezone)
                if session
                else "",
                status=t.status,
                previous_status=t.previous_status,
                reason=t.reason,
                applied_at=t.applied_at,
                applied_by_name=names.get(t.applied_by),
                restored_at=t.restored_at,
                restored_by_name=names.get(t.restored_by),
            )
        )
    return SpecialEventDetailType(
        event=await build_event(db, event),
        orders=[build_order(o) for o in orders],
        displacements=[build_displacement(d, event.timezone) for d in displacements],
        takeovers=takeover_types,
    )


def build_takeover_preview(preview: takeover_crud.TakeoverPreview, tz_name: str) -> TakeoverPreviewType:
    return TakeoverPreviewType(
        event_id=preview.event_id,
        sessions=[
            AffectedSessionType(
                session_id=s.session_id,
                name=s.name,
                start_at=s.start_at,
                end_at=s.end_at,
                label=local_schedule_label(s.start_at, s.end_at, tz_name),
                status=s.status,
                capacity=s.capacity,
                taken_by_this_event=s.taken_by_this_event,
                clients=[
                    AffectedClientType(
                        person_id=c.person_id,
                        person_name=c.person_name,
                        phone=c.phone,
                        reservation_id=c.reservation_id,
                        reservation_status=c.reservation_status,
                        source=c.source,
                        seat_label=c.seat_label,
                        plan_name=c.plan_name,
                        membership_ends_at=c.membership_ends_at,
                        without_reservation=c.without_reservation,
                    )
                    for c in s.clients
                ],
            )
            for s in preview.sessions
        ],
        pending_session_ids=preview.pending_session_ids,
        affected_clients=preview.affected_clients,
        blockers=list(preview.blockers),
        summary=preview.summary,
    )


def build_restore_preview(preview: takeover_crud.RestorePreview, tz_name: str) -> RestorePreviewType:
    return RestorePreviewType(
        event_id=preview.event_id,
        session_labels=[
            f"{s['name']} ({local_schedule_label(s['start_at'], s['end_at'], tz_name)})"
            for s in preview.sessions
        ],
        decisions=[
            RestoreDecisionType(
                displacement_id=d.displacement_id,
                person_name=d.person_name,
                session_id=d.session_id,
                restore=d.restore,
                reason=d.reason,
            )
            for d in preview.decisions
        ],
        blockers=list(preview.blockers),
        summary=preview.summary,
    )
