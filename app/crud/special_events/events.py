"""Special events: creation, edition, publication, availability and cancellation.

Functions here never commit: the caller owns the transaction, so an advisory lock taken with
``lock_special_event`` covers the whole check-and-write and is released at the caller's commit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.timezones import STUDIO_TIMEZONE, local_datetime, to_local, zone
from app.crud.locks import lock_special_event
from app.models import ClassSession, ClassType, People, Seat, Venue
from app.models.specialEventModel import (
    EVENT_CANCELED,
    EVENT_COMPLETED,
    EVENT_DRAFT,
    EVENT_PUBLISHED,
    ORDER_ACTIVE_STATUSES,
    ORDER_AWAITING_PAYMENT,
    ORDER_CANCELED,
    ORDER_EXPIRED,
    ORDER_PAID,
    ORDER_REFUND_PENDING,
    TAKEOVER_ACTIVE,
    SpecialEvent,
    SpecialEventOrder,
    SpecialEventSeat,
    SpecialEventTakeover,
)

from .common import SpecialEventError, local_schedule_label, utcnow

_UNSET = object()


# ---------------------------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------------------------
async def get_event(db: AsyncSession, event_id: int) -> Optional[SpecialEvent]:
    stmt = (
        select(SpecialEvent)
        .options(selectinload(SpecialEvent.seats))
        .where(SpecialEvent.id == int(event_id))
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalars().first()


async def list_events(
    db: AsyncSession, *, include_past: bool = False, statuses: Optional[Sequence[str]] = None
) -> List[SpecialEvent]:
    stmt = select(SpecialEvent).options(selectinload(SpecialEvent.seats))
    if not include_past:
        # Keep the last week visible so staff can close out an event after it happened.
        stmt = stmt.where(SpecialEvent.start_at >= utcnow() - timedelta(days=7))
    if statuses:
        stmt = stmt.where(SpecialEvent.status.in_(list(statuses)))
    stmt = stmt.order_by(SpecialEvent.start_at, SpecialEvent.id)
    return list((await db.execute(stmt)).scalars().all())


async def seat_labels(db: AsyncSession, seat_ids: Sequence[int]) -> Dict[int, str]:
    if not seat_ids:
        return {}
    rows = await db.execute(select(Seat.id, Seat.label).where(Seat.id.in_(list(seat_ids))))
    return {sid: label for sid, label in rows.all()}


async def offered_seat_ids(db: AsyncSession, event: SpecialEvent) -> List[int]:
    """The event's bikes in label order ("Bicicleta 2" before "Bicicleta 10")."""
    ids = [s.seat_id for s in event.seats]
    if not ids:
        return []
    rows = await db.execute(
        select(Seat.id).where(Seat.id.in_(ids)).order_by(func.length(Seat.label), Seat.label)
    )
    return [sid for (sid,) in rows.all()]


# ---------------------------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------------------------
@dataclass
class EventAvailability:
    capacity: Optional[int]
    offered_seats: int
    paid: int
    held: int
    available: Optional[int]
    free_seat_ids: List[int] = field(default_factory=list)
    held_seat_ids: List[int] = field(default_factory=list)
    sold_seat_ids: List[int] = field(default_factory=list)
    sales_open: bool = False
    sales_closed_reason: Optional[str] = None


async def availability(
    db: AsyncSession, event: SpecialEvent, *, now: Optional[datetime] = None
) -> EventAvailability:
    """Seats and spots left, counting paid tickets AND live holds (the retention policy).

    A hold whose ``hold_expires_at`` passed no longer counts even before the sweep flips it to
    ``expired``, so availability is never understated by a lagging job.
    """
    now = now or utcnow()
    rows = (
        await db.execute(
            select(
                SpecialEventOrder.status,
                SpecialEventOrder.seat_id,
                SpecialEventOrder.hold_expires_at,
            ).where(
                SpecialEventOrder.event_id == event.id,
                SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
            )
        )
    ).all()
    paid = 0
    held = 0
    sold_seats: List[int] = []
    held_seats: List[int] = []
    for status, seat_id, expires in rows:
        if status == ORDER_PAID:
            paid += 1
            if seat_id is not None:
                sold_seats.append(seat_id)
        elif expires is None or expires > now:
            held += 1
            if seat_id is not None:
                held_seats.append(seat_id)

    offered = await offered_seat_ids(db, event)
    taken = set(sold_seats) | set(held_seats)
    free = [sid for sid in offered if sid not in taken]

    available: Optional[int] = None
    if event.capacity is not None:
        available = max(0, int(event.capacity) - paid - held)
        if offered:
            available = min(available, len(free))

    result = EventAvailability(
        capacity=event.capacity,
        offered_seats=len(offered),
        paid=paid,
        held=held,
        available=available,
        free_seat_ids=free,
        held_seat_ids=held_seats,
        sold_seat_ids=sold_seats,
    )
    result.sales_open, result.sales_closed_reason = _sales_state(event, available, now)
    return result


def _sales_state(event: SpecialEvent, available: Optional[int], now: datetime):
    if event.status == EVENT_CANCELED:
        return False, "El evento fue cancelado."
    if event.status == EVENT_COMPLETED:
        return False, "El evento ya terminó."
    if event.status != EVENT_PUBLISHED:
        return False, "El evento todavía no está publicado."
    if event.start_at <= now:
        return False, "El evento ya comenzó."
    if event.capacity is None or event.end_at is None:
        return False, "Al evento le faltan datos (duración o cupo)."
    if not available:
        return False, "Ya no hay lugares disponibles."
    return True, None


async def expire_stale_holds(
    db: AsyncSession, *, event_id: Optional[int] = None, now: Optional[datetime] = None
) -> int:
    """Flip holds whose time ran out to ``expired`` (frees their seat). Caller holds the lock."""
    now = now or utcnow()
    stmt = (
        update(SpecialEventOrder)
        .where(
            SpecialEventOrder.status == ORDER_AWAITING_PAYMENT,
            SpecialEventOrder.hold_expires_at.is_not(None),
            SpecialEventOrder.hold_expires_at <= now,
        )
        .values(status=ORDER_EXPIRED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if event_id is not None:
        stmt = stmt.where(SpecialEventOrder.event_id == int(event_id))
    result = await db.execute(stmt)
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------------------------
def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise SpecialEventError("El precio no es válido.") from None
    if amount < 0:
        raise SpecialEventError("El precio no puede ser negativo.")
    return amount


def _check_timezone(tz_name: str) -> str:
    try:
        zone(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SpecialEventError(f"Zona horaria desconocida: {tz_name}.") from None
    return tz_name


def _resolve_period(
    tz_name: str, day: date, start: time, end: Optional[time]
) -> tuple[datetime, Optional[datetime]]:
    start_at = local_datetime(day, start, tz_name)
    end_at = None
    if end is not None:
        end_at = local_datetime(day, end, tz_name)
        if end_at <= start_at:
            raise SpecialEventError("La hora de fin debe ser posterior a la hora de inicio.")
    return start_at, end_at


async def _check_references(
    db: AsyncSession,
    venue_id: int,
    class_type_id: Optional[int],
    instructor_id: Optional[int],
) -> Venue:
    venue = await db.get(Venue, int(venue_id))
    if venue is None:
        raise SpecialEventError("La sede no existe.")
    if class_type_id is not None and await db.get(ClassType, int(class_type_id)) is None:
        raise SpecialEventError("El tipo de clase no existe.")
    if instructor_id is not None:
        person = await db.get(People, int(instructor_id))
        if person is None or person.deleted_at is not None:
            raise SpecialEventError("La instructora no existe.")
    return venue


async def _check_seats(db: AsyncSession, venue_id: int, seat_ids: Optional[Sequence[int]]) -> List[int]:
    """Validate the bikes offered. ``None`` means every active bike of the venue."""
    if seat_ids is None:
        rows = await db.execute(
            select(Seat.id)
            .where(Seat.venue_id == int(venue_id), Seat.is_active.is_(True))
            .order_by(func.length(Seat.label), Seat.label)
        )
        return [sid for (sid,) in rows.all()]
    wanted = sorted({int(s) for s in seat_ids})
    if not wanted:
        return []
    rows = await db.execute(
        select(Seat.id).where(
            Seat.id.in_(wanted), Seat.venue_id == int(venue_id), Seat.is_active.is_(True)
        )
    )
    found = {sid for (sid,) in rows.all()}
    if found != set(wanted):
        raise SpecialEventError("Algunas bicicletas no pertenecen a la sede o están inactivas.")
    return wanted


async def overlapping_events(
    db: AsyncSession,
    *,
    venue_id: int,
    instructor_id: Optional[int],
    start_at: datetime,
    end_at: Optional[datetime],
    exclude_id: Optional[int] = None,
) -> List[SpecialEvent]:
    """Live events on the same venue or instructor whose period overlaps (mirrors the EXCLUDE)."""
    if end_at is None:
        return []
    resources = [SpecialEvent.venue_id == int(venue_id)]
    if instructor_id is not None:
        resources.append(SpecialEvent.instructor_id == int(instructor_id))
    stmt = select(SpecialEvent).where(
        SpecialEvent.status != EVENT_CANCELED,
        SpecialEvent.end_at.is_not(None),
        SpecialEvent.start_at < end_at,
        SpecialEvent.end_at > start_at,
        or_(*resources),
    )
    if exclude_id is not None:
        stmt = stmt.where(SpecialEvent.id != int(exclude_id))
    return list((await db.execute(stmt)).scalars().all())


async def overlapping_regular_sessions(
    db: AsyncSession, event: SpecialEvent, *, statuses: Sequence[str] = ("scheduled",)
) -> List[ClassSession]:
    """Regular classes at the event's venue whose time overlaps the event."""
    if event.end_at is None:
        return []
    stmt = (
        select(ClassSession)
        .where(
            ClassSession.venue_id == event.venue_id,
            ClassSession.status.in_(list(statuses)),
            ClassSession.start_at < event.end_at,
            ClassSession.end_at > event.start_at,
        )
        .order_by(ClassSession.start_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def has_active_takeovers(db: AsyncSession, event_id: int) -> bool:
    row = await db.execute(
        select(SpecialEventTakeover.id)
        .where(
            SpecialEventTakeover.event_id == int(event_id),
            SpecialEventTakeover.status == TAKEOVER_ACTIVE,
        )
        .limit(1)
    )
    return row.first() is not None


async def count_active_orders(db: AsyncSession, event_id: int, *, now: Optional[datetime] = None) -> int:
    """Paid tickets plus live holds."""
    now = now or utcnow()
    count = await db.execute(
        select(func.count(SpecialEventOrder.id)).where(
            SpecialEventOrder.event_id == int(event_id),
            or_(
                SpecialEventOrder.status == ORDER_PAID,
                and_(
                    SpecialEventOrder.status == ORDER_AWAITING_PAYMENT,
                    or_(
                        SpecialEventOrder.hold_expires_at.is_(None),
                        SpecialEventOrder.hold_expires_at > now,
                    ),
                ),
            ),
        )
    )
    return int(count.scalar() or 0)


# ---------------------------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------------------------
async def create_event(
    db: AsyncSession,
    *,
    actor_account_id: Optional[int],
    name: str,
    venue_id: int,
    local_day: date,
    start_time: time,
    price,
    end_time: Optional[time] = None,
    description: Optional[str] = None,
    includes: Optional[str] = None,
    address: Optional[str] = None,
    class_type_id: Optional[int] = None,
    instructor_id: Optional[int] = None,
    timezone_name: str = STUDIO_TIMEZONE,
    capacity: Optional[int] = None,
    seat_ids: Optional[Sequence[int]] = None,
    hold_minutes: int = 30,
    currency: str = "MXN",
) -> SpecialEvent:
    """Create a draft event. A draft is invisible to the chatbot until published."""
    name = (name or "").strip()
    if not name:
        raise SpecialEventError("El evento necesita un nombre.")
    amount = _money(price)
    tz_name = _check_timezone((timezone_name or STUDIO_TIMEZONE).strip())
    start_at, end_at = _resolve_period(tz_name, local_day, start_time, end_time)
    if start_at <= utcnow():
        raise SpecialEventError("La fecha y hora de inicio ya pasaron.")
    if capacity is not None and int(capacity) <= 0:
        raise SpecialEventError("El cupo debe ser mayor a cero.")
    if not 5 <= int(hold_minutes) <= 240:
        raise SpecialEventError("El tiempo de apartado debe estar entre 5 y 240 minutos.")

    await _check_references(db, venue_id, class_type_id, instructor_id)
    seats = await _check_seats(db, venue_id, seat_ids)
    if capacity is not None and seats and int(capacity) > len(seats):
        raise SpecialEventError(
            f"El cupo ({capacity}) es mayor que las bicicletas asignadas ({len(seats)})."
        )
    clashes = await overlapping_events(
        db, venue_id=venue_id, instructor_id=instructor_id, start_at=start_at, end_at=end_at
    )
    if clashes:
        raise SpecialEventError(_clash_message(clashes[0]))

    event = SpecialEvent(
        name=name,
        description=_clean_text(description),
        includes=_clean_text(includes),
        address=_clean_text(address),
        venue_id=int(venue_id),
        class_type_id=int(class_type_id) if class_type_id is not None else None,
        instructor_id=int(instructor_id) if instructor_id is not None else None,
        timezone=tz_name,
        start_at=start_at,
        end_at=end_at,
        price=amount,
        currency=(currency or "MXN").upper()[:3],
        capacity=int(capacity) if capacity is not None else None,
        status=EVENT_DRAFT,
        hold_minutes=int(hold_minutes),
        created_by=actor_account_id,
        updated_by=actor_account_id,
    )
    event.seats = [SpecialEventSeat(seat_id=sid) for sid in seats]
    db.add(event)
    await db.flush()
    return event


async def update_event(
    db: AsyncSession,
    event: SpecialEvent,
    *,
    actor_account_id: Optional[int],
    name=_UNSET,
    description=_UNSET,
    includes=_UNSET,
    address=_UNSET,
    class_type_id=_UNSET,
    instructor_id=_UNSET,
    venue_id=_UNSET,
    timezone_name=_UNSET,
    local_day=_UNSET,
    start_time=_UNSET,
    end_time=_UNSET,
    price=_UNSET,
    capacity=_UNSET,
    seat_ids=_UNSET,
    hold_minutes=_UNSET,
) -> SpecialEvent:
    """Edit an event. Changing *when* or *where* is refused once it has consequences.

    With classes already taken over, or seats held/sold, moving the event would silently break
    those commitments — staff must restore the schedule or refund first, deliberately.
    """
    if event.status in (EVENT_CANCELED, EVENT_COMPLETED):
        raise SpecialEventError("No se puede modificar un evento cancelado o terminado.")
    await lock_special_event(db, event.id)
    now = utcnow()

    if name is not _UNSET:
        cleaned = (name or "").strip()
        if not cleaned:
            raise SpecialEventError("El evento necesita un nombre.")
        event.name = cleaned
    if description is not _UNSET:
        event.description = _clean_text(description)
    if includes is not _UNSET:
        event.includes = _clean_text(includes)
    if address is not _UNSET:
        event.address = _clean_text(address)
    if price is not _UNSET:
        event.price = _money(price)
    if hold_minutes is not _UNSET:
        if not 5 <= int(hold_minutes) <= 240:
            raise SpecialEventError("El tiempo de apartado debe estar entre 5 y 240 minutos.")
        event.hold_minutes = int(hold_minutes)

    new_venue = event.venue_id if venue_id is _UNSET else int(venue_id)
    new_instructor = event.instructor_id if instructor_id is _UNSET else (
        int(instructor_id) if instructor_id is not None else None
    )
    new_class_type = event.class_type_id if class_type_id is _UNSET else (
        int(class_type_id) if class_type_id is not None else None
    )
    await _check_references(db, new_venue, new_class_type, new_instructor)

    tz_name = event.timezone if timezone_name is _UNSET else _check_timezone(
        (timezone_name or STUDIO_TIMEZONE).strip()
    )
    current_local = to_local(event.start_at, event.timezone)
    day = current_local.date() if local_day is _UNSET else local_day
    start = current_local.time().replace(second=0, microsecond=0) if start_time is _UNSET else start_time
    if end_time is _UNSET:
        end = (
            to_local(event.end_at, event.timezone).time().replace(second=0, microsecond=0)
            if event.end_at is not None
            else None
        )
    else:
        end = end_time
    new_start, new_end = _resolve_period(tz_name, day, start, end)

    moved = (
        new_start != event.start_at
        or new_venue != event.venue_id
        or tz_name != event.timezone
        or (event.end_at is not None and new_end != event.end_at)
    )
    if moved:
        if await has_active_takeovers(db, event.id):
            raise SpecialEventError(
                "El evento ya sustituyó clases regulares. Restituye el horario regular antes de "
                "cambiar fecha, hora o sede."
            )
        if await count_active_orders(db, event.id, now=now):
            raise SpecialEventError(
                "El evento ya tiene lugares apartados o vendidos. No se puede cambiar fecha, hora "
                "o sede; cancela o reembolsa esas compras primero."
            )
    elif event.end_at is None and new_end is not None and await has_active_takeovers(db, event.id):
        raise SpecialEventError("Restituye el horario regular antes de definir la duración.")
    if new_start <= now and new_start != event.start_at:
        raise SpecialEventError("La fecha y hora de inicio ya pasaron.")

    clashes = await overlapping_events(
        db,
        venue_id=new_venue,
        instructor_id=new_instructor,
        start_at=new_start,
        end_at=new_end,
        exclude_id=event.id,
    )
    if clashes:
        raise SpecialEventError(_clash_message(clashes[0]))
    if moved and event.status == EVENT_PUBLISHED and new_end is not None:
        probe = SpecialEvent(venue_id=new_venue, start_at=new_start, end_at=new_end)
        if await overlapping_regular_sessions(db, probe):
            raise SpecialEventError(
                "El nuevo horario se empalma con clases regulares sin sustituir. Regresa el "
                "evento a borrador, revisa la sustitución y vuelve a publicarlo."
            )

    event.venue_id = new_venue
    event.instructor_id = new_instructor
    event.class_type_id = new_class_type
    event.timezone = tz_name
    event.start_at = new_start
    event.end_at = new_end

    # Seats and capacity: never below what is already held or sold.
    current_seats = [s.seat_id for s in event.seats]
    if seat_ids is not _UNSET or venue_id is not _UNSET:
        wanted = await _check_seats(
            db, new_venue, None if seat_ids is _UNSET or seat_ids is None else seat_ids
        )
        removed = set(current_seats) - set(wanted)
        if removed:
            busy = await db.execute(
                select(SpecialEventOrder.seat_id).where(
                    SpecialEventOrder.event_id == event.id,
                    SpecialEventOrder.seat_id.in_(list(removed)),
                    SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
                )
            )
            if busy.first() is not None:
                raise SpecialEventError(
                    "No puedes quitar bicicletas que ya están apartadas o vendidas."
                )
        event.seats = [s for s in event.seats if s.seat_id in set(wanted)] + [
            SpecialEventSeat(seat_id=sid) for sid in wanted if sid not in set(current_seats)
        ]
    if capacity is not _UNSET:
        if capacity is not None and int(capacity) <= 0:
            raise SpecialEventError("El cupo debe ser mayor a cero.")
        if capacity is not None:
            taken = await count_active_orders(db, event.id, now=now)
            if int(capacity) < taken:
                raise SpecialEventError(
                    f"Ya hay {taken} lugar(es) apartados o vendidos; el cupo no puede ser menor."
                )
        event.capacity = int(capacity) if capacity is not None else None
    seat_count = len(event.seats)
    if event.capacity is not None and seat_count and event.capacity > seat_count:
        raise SpecialEventError(
            f"El cupo ({event.capacity}) es mayor que las bicicletas asignadas ({seat_count})."
        )

    event.updated_by = actor_account_id
    event.updated_at = now
    await db.flush()
    return event


async def publish_blockers(db: AsyncSession, event: SpecialEvent) -> List[str]:
    """Everything that still prevents publishing, in plain language (empty = ready)."""
    blockers: List[str] = []
    now = utcnow()
    if event.status in (EVENT_CANCELED, EVENT_COMPLETED):
        return ["El evento está cancelado o terminado."]
    if event.end_at is None:
        blockers.append("Falta la hora de fin (la duración) del evento.")
    if not event.capacity:
        blockers.append("Falta el cupo del evento.")
    if event.price is None or Decimal(event.price) <= 0:
        blockers.append("El precio debe ser mayor a cero para cobrarlo por Mercado Pago.")
    if event.start_at <= now:
        blockers.append("La fecha del evento ya pasó.")
    seat_count = len(event.seats)
    if event.capacity and seat_count and event.capacity > seat_count:
        blockers.append(
            f"El cupo ({event.capacity}) es mayor que las bicicletas asignadas ({seat_count})."
        )
    if event.end_at is not None:
        pending = await overlapping_regular_sessions(db, event)
        if pending:
            labels = ", ".join(
                f"{s.name or 'Clase'} ({local_schedule_label(s.start_at, s.end_at, event.timezone)})"
                for s in pending
            )
            blockers.append(
                "En ese horario hay clases regulares sin sustituir: "
                f"{labels}. Revisa la vista previa y aplica la sustitución, o cambia el horario."
            )
        clashes = await overlapping_events(
            db,
            venue_id=event.venue_id,
            instructor_id=event.instructor_id,
            start_at=event.start_at,
            end_at=event.end_at,
            exclude_id=event.id,
        )
        if clashes:
            blockers.append(_clash_message(clashes[0]))
    return blockers


async def publish_event(db: AsyncSession, event: SpecialEvent, *, actor_account_id: Optional[int]) -> SpecialEvent:
    await lock_special_event(db, event.id)
    if event.status == EVENT_PUBLISHED:
        return event
    blockers = await publish_blockers(db, event)
    if blockers:
        raise SpecialEventError(" ".join(blockers))
    now = utcnow()
    event.status = EVENT_PUBLISHED
    event.published_at = now
    event.published_by = actor_account_id
    event.updated_by = actor_account_id
    event.updated_at = now
    await db.flush()
    return event


async def unpublish_event(db: AsyncSession, event: SpecialEvent, *, actor_account_id: Optional[int]) -> SpecialEvent:
    """Back to draft (stops the chatbot from selling). Only while nothing is held or sold."""
    await lock_special_event(db, event.id)
    if event.status != EVENT_PUBLISHED:
        raise SpecialEventError("Solo un evento publicado puede volver a borrador.")
    await expire_stale_holds(db, event_id=event.id)
    if await count_active_orders(db, event.id):
        raise SpecialEventError(
            "Hay lugares apartados o vendidos. No se puede regresar a borrador; si el evento no "
            "se realizará, cancélalo."
        )
    event.status = EVENT_DRAFT
    event.updated_by = actor_account_id
    event.updated_at = utcnow()
    await db.flush()
    return event


@dataclass
class CancelSummary:
    holds_canceled: int = 0
    to_refund_order_ids: List[int] = field(default_factory=list)
    preference_ids: List[str] = field(default_factory=list)
    active_takeovers: int = 0


async def cancel_event(
    db: AsyncSession, event: SpecialEvent, *, actor_account_id: Optional[int], reason: str
) -> CancelSummary:
    """Cancel the event. Nothing is refunded or restored implicitly.

    Unpaid holds are voided (their payment links are expired by the caller after commit); paid
    tickets move to ``refund_pending`` so staff refund them explicitly; displaced classes stay
    displaced until staff run the previewed restoration — some of those clients may already
    have been compensated, and reactivating them blindly would double-book or confuse them.
    """
    reason = (reason or "").strip()
    if not reason:
        raise SpecialEventError("Indica el motivo de la cancelación.")
    await lock_special_event(db, event.id)
    if event.status == EVENT_CANCELED:
        raise SpecialEventError("El evento ya está cancelado.")
    if event.status == EVENT_COMPLETED:
        raise SpecialEventError("El evento ya terminó; no se puede cancelar.")
    now = utcnow()
    summary = CancelSummary()
    orders = (
        await db.execute(
            select(SpecialEventOrder)
            .where(
                SpecialEventOrder.event_id == event.id,
                SpecialEventOrder.status.in_(ORDER_ACTIVE_STATUSES),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    for order in orders:
        if order.status == ORDER_AWAITING_PAYMENT:
            order.status = ORDER_CANCELED
            order.canceled_at = now
            order.cancel_reason = "Evento cancelado"
            summary.holds_canceled += 1
            if order.mp_preference_id:
                summary.preference_ids.append(order.mp_preference_id)
        else:
            order.status = ORDER_REFUND_PENDING
            order.review_reason = f"Evento cancelado: {reason}"
            summary.to_refund_order_ids.append(order.id)
        order.updated_by = actor_account_id
        order.updated_at = now

    event.status = EVENT_CANCELED
    event.cancel_reason = reason
    event.canceled_at = now
    event.canceled_by = actor_account_id
    event.updated_by = actor_account_id
    event.updated_at = now
    summary.active_takeovers = int(
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
    await db.flush()
    return summary


async def complete_past_events(db: AsyncSession, *, now: Optional[datetime] = None) -> int:
    """Published events whose end passed become ``completed`` (sales closed for good)."""
    now = now or utcnow()
    result = await db.execute(
        update(SpecialEvent)
        .where(
            SpecialEvent.status == EVENT_PUBLISHED,
            SpecialEvent.end_at.is_not(None),
            SpecialEvent.end_at < now,
        )
        .values(status=EVENT_COMPLETED, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


def _clean_text(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clash_message(other: SpecialEvent) -> str:
    when = local_schedule_label(other.start_at, other.end_at, other.timezone)
    return (
        f"Ya existe el evento «{other.name}» en esa sede o con esa instructora ({when}). "
        "Dos eventos activos no pueden usar los mismos recursos al mismo tiempo."
    )
