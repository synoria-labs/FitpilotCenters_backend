"""Membership entitlements. A schedule occurrence is not a seat or a prepaid credit."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select

from app.core.timezones import local_date, local_datetime
from app.models import ClassSession, ClassTemplate, MembershipPlan, MembershipSubscription, Reservation, StandingBooking
from app.models.classModel import StandingBookingException
from .day_passes import is_day_pass
from .utils import _calculate_subscription_end, _calculate_window_end_for_plan

WEEKDAYS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def schedule_key(template):
    return (template.class_type_id, template.venue_id, str(template.start_time_local), template.instructor_id)


def schedule_label(templates):
    days = sorted({(t.weekday - 1) % 7 for t in templates})
    if not days:
        return "Sin horario configurado"
    if days == list(range(5)):
        label = "Lunes a Viernes"
    elif days == list(range(7)):
        label = "Lunes a Domingo"
    else:
        label = ", ".join(WEEKDAYS[d] for d in days)
    return f"{label} a las {str(templates[0].start_time_local)[:5]}"


def occurrences(plan, templates, start_at, end_at):
    """Actual local occurrences within the membership window, including partial weeks/months."""
    first = local_date(start_at)
    last = _calculate_window_end_for_plan(plan, first, local_date(end_at))
    result = []
    day = first
    while day <= last:
        for template in templates:
            if (template.weekday - 1) % 7 == day.weekday():
                instant = local_datetime(day, template.start_time_local)
                if start_at <= instant < end_at:
                    result.append((template.id, day))
        day += timedelta(days=1)
    return result


@dataclass
class Coverage:
    mode: str
    included_classes: int | None
    schedule: str | None = None


async def fixed_coverage(db, plan, templates, start_at, end_at):
    dates = set(occurrences(plan, templates, start_at, end_at))
    if dates:
        canceled = (await db.execute(select(ClassSession).where(
            ClassSession.template_id.in_([t.id for t in templates]),
            ClassSession.start_at >= start_at, ClassSession.start_at < end_at,
            ClassSession.status.in_(("canceled", "displaced")),
        ))).scalars().all()
        dates.difference_update((s.template_id, local_date(s.start_at)) for s in canceled)
    return Coverage("fixed_schedule", len(dates), schedule_label(templates))


async def subscription_coverage(db, subscription):
    plan = await db.get(MembershipPlan, subscription.plan_id)
    if subscription.session_id is not None:
        return Coverage("day_pass", 1)
    if plan.fixed_time_slot:
        templates = (await db.execute(select(ClassTemplate).join(
            StandingBooking, StandingBooking.template_id == ClassTemplate.id,
        ).where(StandingBooking.subscription_id == subscription.id).distinct())).scalars().all()
        return await fixed_coverage(db, plan, templates, subscription.start_at, subscription.end_at)
    if plan.plan_type == "credit_pack":
        return Coverage("credit_pack", plan.class_limit)
    return Coverage("flexible", None)


async def package_facts(db, *, plan_id, template_id=None, member_id=None, subscription=None):
    """Trusted explanation used both by purchase tools and by the final response renderer."""
    from app.crud.standing_bookings.utils import _get_templates_in_same_group
    from .subscriptions import get_member_active_subscription

    plan = await db.get(MembershipPlan, int(plan_id))
    if plan is None or (not plan.is_active and subscription is None) or is_day_pass(plan):
        raise ValueError("Selecciona un paquete o membresía disponible; el pase diario requiere una clase concreta.")
    start = datetime.now(timezone.utc)
    current = await get_member_active_subscription(db, member_id) if member_id and subscription is None else None
    if current and current.end_at > start:
        start = current.end_at
    end = _calculate_subscription_end(plan, start)
    if subscription is not None:
        start, end = subscription.start_at, subscription.end_at
    lines = [f"{plan.name}: ${plan.price:.2f}, vigencia de {plan.duration_value} {plan.duration_unit}(s)."]
    if plan.fixed_time_slot:
        if template_id is None:
            raise ValueError("Selecciona el grupo de horario fijo (template_id) para calcular su cobertura.")
        templates = await _get_templates_in_same_group(db, int(template_id))
        if not templates or not any(t.id == int(template_id) for t in templates):
            raise ValueError("Ese horario ya no está activo; consulta los horarios disponibles.")
        coverage = await fixed_coverage(db, plan, templates, start, end)
        if not coverage.included_classes:
            raise ValueError("El horario elegido no tiene clases dentro de esa vigencia.")
        lines.append(f"Horario fijo: {coverage.schedule}. Incluye todas las clases de ese grupo dentro de la vigencia.")
        qualifier = "Clases incluidas" if subscription else "Clases previstas si se acredita ahora"
        lines.append(f"{qualifier}: {coverage.included_classes}; se calculan por el calendario, no por un límite manual.")
    elif plan.plan_type == "credit_pack":
        lines.append(f"Incluye {plan.class_limit} créditos/clases durante la vigencia, sujetos a disponibilidad.")
    else:
        lines.append("Acceso libre, sin límite de clases ni horario fijo durante la vigencia; cada reserva está sujeta a disponibilidad.")
        lines.append("La membresía no reserva lugares automáticamente.")
    from app.services.chatbot.timefmt import fmt_dt
    if subscription is not None:
        lines.append(f"Vigencia: {fmt_dt(start)} a {fmt_dt(end)} (hora de México).")
    elif current:
        lines.append(f"La renovación comienza al terminar la membresía actual: {fmt_dt(start)}.")
        lines.append("La vigencia y las clases definitivas se verifican al acreditar el pago.")
    else:
        lines.append("La vigencia comienza al acreditarse el pago; no se espera al día de la plantilla seleccionada.")
    return "\n".join(lines)


async def _fixed_covers(db, sub, plan, session):
    last = _calculate_window_end_for_plan(plan, local_date(sub.start_at), local_date(sub.end_at))
    day = local_date(session.start_at)
    if day > last:
        return False
    booking = await db.scalar(select(StandingBooking.id).where(
        StandingBooking.subscription_id == sub.id, StandingBooking.status == "active",
        StandingBooking.template_id == session.template_id,
        StandingBooking.start_date <= day, StandingBooking.end_date >= day,
        ~select(StandingBookingException.id).where(
            StandingBookingException.standing_booking_id == StandingBooking.id,
            StandingBookingException.session_date == day,
        ).exists(),
    ).limit(1))
    if booking:
        return True
    # Staff-approved replacements belong to the original subscription, not a second credit.
    return bool(await db.scalar(select(StandingBookingException.id).join(
        StandingBooking, StandingBooking.id == StandingBookingException.standing_booking_id,
    ).where(StandingBooking.subscription_id == sub.id, StandingBooking.status == "active",
            StandingBookingException.action == "reschedule", StandingBookingException.new_session_id == session.id).limit(1)))


async def reservation_subscription(db, *, person_id, session):
    """Select an entitlement and serialize credit consumption; caller holds the class lock."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(MembershipSubscription, MembershipPlan).join(
        MembershipPlan, MembershipPlan.id == MembershipSubscription.plan_id,
    ).where(MembershipSubscription.person_id == person_id, MembershipSubscription.status == "active",
            MembershipSubscription.start_at <= session.start_at, MembershipSubscription.end_at > session.start_at,
            MembershipSubscription.end_at > now))).all()
    # Prefer already included classes/open access over spending prepaid credits.
    rows.sort(key=lambda row: row[1].plan_type == "credit_pack")
    for sub, plan in rows:
        if sub.session_id is not None:
            if sub.session_id == session.id:
                return sub.id
            continue
        if plan.fixed_time_slot:
            if await _fixed_covers(db, sub, plan, session):
                return sub.id
            continue
        if plan.plan_type == "flexible":
            return sub.id
        if plan.plan_type == "credit_pack":
            # One credit pack cannot be spent twice by simultaneous requests for different classes.
            locked = await db.scalar(select(MembershipSubscription).where(
                MembershipSubscription.id == sub.id, MembershipSubscription.status == "active",
                MembershipSubscription.start_at <= session.start_at,
                MembershipSubscription.end_at > session.start_at, MembershipSubscription.end_at > now,
            ).with_for_update().execution_options(populate_existing=True))
            if locked is None:
                continue
            used = await db.scalar(select(func.count(Reservation.id)).join(ClassSession).where(
                Reservation.person_id == person_id, Reservation.status.in_(("reserved", "checked_in", "no_show")),
                or_(Reservation.subscription_id == sub.id, and_(Reservation.subscription_id.is_(None),
                    ClassSession.start_at >= sub.start_at, ClassSession.start_at < sub.end_at)),
            ))
            if not plan.class_limit or used >= plan.class_limit:
                continue
            starts = (await db.execute(select(ClassSession.start_at).join(Reservation).where(
                Reservation.subscription_id == sub.id,
                Reservation.status.in_(("reserved", "checked_in", "no_show")),
            ))).scalars().all()
            day = local_date(session.start_at)
            week = day - timedelta(days=day.weekday())
            if plan.max_sessions_per_day and sum(local_date(s) == day for s in starts) >= plan.max_sessions_per_day:
                continue
            if plan.max_sessions_per_week and sum(week <= local_date(s) < week + timedelta(days=7) for s in starts) >= plan.max_sessions_per_week:
                continue
            return sub.id
    raise ValueError("La membresía no cubre esa clase: revisa horario, vigencia y créditos disponibles.")
