"""Single-class purchases. The caller owns the transaction, including the payment."""
from dataclasses import dataclass
from datetime import datetime, timezone
import os

from sqlalchemy import select, update, text as sa_text

from app.models import ClassSession, MembershipPlan, MembershipSubscription, Payment, People, Reservation
from app.crud.locks import lock_class_session, _lock_key
from .payments import create_payment
from .utils import _resolve_payment_amount


def is_day_pass(plan) -> bool:
    return plan.duration_unit == "day" and plan.duration_value == 1


def uses_class_expiry(plan, session_id) -> bool:
    """The opt-out is only for the documented, temporary old-client rollout window."""
    required = os.getenv("DAY_PASS_SESSION_REQUIRED", "true").strip().lower() not in {"false", "0", "no"}
    return is_day_pass(plan) and (session_id is not None or required)


def effective_status(subscription, now=None) -> str:
    """Elapsed time can expire an active subscription, never revive a canceled/pending one."""
    now = now or datetime.now(timezone.utc)
    if subscription.status == "active" and subscription.end_at <= now:
        return "expired"
    return subscription.status


@dataclass
class DayPassPurchase:
    person: People
    subscription: MembershipSubscription
    payment: Payment
    plan: MembershipPlan
    reservation: Reservation


async def purchase_day_pass(
    db, *, plan_id, session_id, person_id=None, full_name=None, email=None,
    phone_number=None, seat_id=None, payment_method="cash", payment_amount=None,
    payment_status="COMPLETED", payment_comment=None, payment_provider=None,
    provider_payment_id=None, external_reference=None, recorded_by=None,
) -> DayPassPurchase:
    from app.crud.membersCrud import create_member
    from app.crud.reservationsCrud import create_reservation, get_available_seats

    if session_id is None:
        raise ValueError("Selecciona una clase concreta (sessionId) para comprar el pase diario.")
    plan = await db.get(MembershipPlan, int(plan_id))
    if plan is None or not is_day_pass(plan) or not plan.is_active:
        raise ValueError("El plan no es un pase diario disponible.")
    if payment_status != "COMPLETED":
        raise ValueError("El pase diario se activa únicamente al acreditar el pago.")
    payment_key = provider_payment_id or external_reference
    if payment_key:
        await db.execute(sa_text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key("day_pass_payment", payment_key)})
        previous = (await db.execute(select(Payment).where(
            Payment.provider_payment_id == str(provider_payment_id) if provider_payment_id
            else Payment.external_reference == external_reference
        ))).scalars().first()
        if previous is not None:
            if previous.status != "COMPLETED":
                raise ValueError("El pago original no está completado; requiere conciliación.")
            subscription = await db.get(MembershipSubscription, previous.subscription_id)
            if (subscription is None or subscription.plan_id != plan.id or subscription.session_id != int(session_id)
                    or (person_id is not None and previous.person_id != int(person_id))):
                raise ValueError("El pago ya está vinculado a otra compra.")
            reservation = (await db.execute(select(Reservation).where(
                Reservation.person_id == previous.person_id, Reservation.session_id == subscription.session_id
            ).order_by(Reservation.id))).scalars().first()
            if reservation is None:
                raise ValueError("El pago requiere conciliación: falta la reserva original.")
            return DayPassPurchase(await db.get(People, previous.person_id), subscription, previous, plan, reservation)
    await lock_class_session(db, int(session_id))
    session = await db.get(ClassSession, int(session_id), populate_existing=True, with_for_update=True)
    now = datetime.now(timezone.utc)
    if session is None or session.status != "scheduled" or session.start_at <= now or session.end_at <= session.start_at:
        raise ValueError("La clase seleccionada ya no está disponible para comprar un pase diario.")
    if person_id is None:
        if not (full_name or "").strip():
            raise ValueError("El pase diario requiere el nombre completo del participante.")
        person = await create_member(db=db, full_name=full_name, email=email, phone_number=phone_number, commit=False)
    else:
        person = await db.get(People, int(person_id))
        if person is None:
            raise ValueError("Socio no encontrado.")
    seats = await get_available_seats(db, session.id)
    if seat_id is not None and not any(seat.id == seat_id and seat.is_available for seat in seats):
        raise ValueError("El lugar seleccionado no está disponible en esta clase.")
    if seat_id is None:
        if seats:
            seat_id = next((seat.id for seat in seats if seat.is_available), None)
            if seat_id is None:
                raise ValueError("La clase ya no tiene lugares disponibles.")
    subscription = MembershipSubscription(
        person_id=person.id, plan_id=plan.id, session_id=session.id,
        start_at=now, end_at=session.end_at, status="active", created_by=recorded_by,
    )
    db.add(subscription)
    await db.flush()
    payment = await create_payment(
        db=db, person_id=person.id, subscription_id=subscription.id,
        amount=_resolve_payment_amount(plan, payment_amount), method=payment_method,
        status=payment_status, comment=payment_comment, provider=payment_provider,
        provider_payment_id=provider_payment_id, external_reference=external_reference,
        recorded_by=recorded_by, commit=False,
    )
    reservation = await create_reservation(
        db, session_id=session.id, person_id=person.id, seat_id=seat_id, commit=False,
    )
    subscription.plan = plan
    return DayPassPurchase(person, subscription, payment, plan, reservation)


async def expire_day_passes(db, now=None) -> int:
    """Idempotent reconciliation; legacy subscriptions (session_id NULL) are never modified."""
    result = await db.execute(
        update(MembershipSubscription).where(
            MembershipSubscription.session_id.isnot(None),
            MembershipSubscription.status == "active",
            MembershipSubscription.end_at <= (now or datetime.now(timezone.utc)),
        ).values(status="expired", updated_at=now or datetime.now(timezone.utc))
    )
    return result.rowcount
