"""Calendar-aware quotes and durable, idempotent application of approved payments."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import re
import uuid

from sqlalchemy import func, select, text as sql_text

from app.core.timezones import local_date, local_datetime, to_local
from app.crud.locks import _lock_key, lock_class_session
from app.crud.memberships.calendar import period, last_day
from app.crud.memberships.coverage import occurrences, schedule_label
from app.crud.memberships.day_passes import is_day_pass
from app.models import ClassSession, MembershipPlan, MembershipSubscription, Payment, Reservation, Seat, StandingBooking
from app.models.chatbotSalesModel import MembershipPurchaseOrder as Order
from app.services import mercadopago_service as mp

OPEN_STATES = ("draft", "awaiting_payment", "paid_awaiting_start", "needs_review")


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def summary(facts, *, receipt=False, bike=None):
    start, end = stamp(facts["start_at"]), stamp(facts["end_at"])
    lines = [f"{facts['plan_name']}: ${Decimal(facts['amount']):.2f} MXN."]
    if facts.get("schedule"):
        lines.append(f"Horario: {facts['schedule']}.")
    lines.append(f"Inicio: {local_date(start):%d/%m/%Y}. Último día cubierto: {last_day(end):%d/%m/%Y}.")
    count = facts.get("included_classes")
    if count is not None:
        lines.append(f"Incluye {count} clase{'s' if count != 1 else ''} en ese periodo.")
    else:
        lines.append("Acceso sin límite de clases durante la vigencia, sujeto a disponibilidad.")
    if receipt and bike:
        lines.append(f"Tu lugar: {bike}.")
    return "\n".join(lines)


async def active_order(db, conversation_id):
    return await db.scalar(select(Order).where(Order.conversation_id == conversation_id,
        Order.status.in_(OPEN_STATES)).order_by(Order.id.desc()).limit(1))


async def payment_answer(ctx):
    from .intents import in_person_choice, payment_options, plain
    answer = payment_options(ctx.offer_in_person_registration)
    order = await active_order(ctx.db, ctx.conversation_id) if ctx.sales_v2_enabled else None
    if order and order.payment_id:
        return "Ya recibimos tu pago. Revisemos el inicio de tu membresía; no necesitas pagar nuevamente."
    if in_person_choice(ctx.user_text) and ctx.offer_in_person_registration:
        if order:
            return await cancel_unpaid(ctx.db, order)
        return "Puedes registrarte y pagar en recepción con un miembro de nuestro equipo. El registro y la disponibilidad se revisan al llegar."
    if order and order.mp_init_point and "transferencia" in plain(ctx.user_text):
        statuses = await mp.search_payments(order.external_reference)
        if any(p.get("status") in ("approved", "pending", "in_process", "authorized", "in_mediation") for p in statuses):
            return "Mercado Pago indica un pago recibido o en proceso. Te avisaremos cuando se acredite; no realices otro pago."
        if order.first_class_at and order.first_class_at <= now():
            return answer + " La fecha del enlace anterior ya pasó; revisemos el pago y una nueva fecha antes de reutilizarlo."
        from .tools import PaymentLink
        ctx.payment_link = PaymentLink(order.mp_init_point, "Puedes realizar la transferencia desde este mismo enlace:")
    return answer


async def facts_for(db, *, plan_id, start_date, template_id=None, session_id=None, member_id=None,
                    at=None, allow_started=False):
    at = at or now()
    plan = await db.get(MembershipPlan, int(plan_id))
    if not plan or not plan.is_active:
        raise ValueError("Ese plan no está disponible. Revisemos otra opción.")
    start, end = period(plan, start_date)
    facts = dict(plan_id=plan.id, plan_name=plan.name, amount=str(plan.price), start_at=start.isoformat(),
                 end_at=end.isoformat(), plan_type=plan.plan_type, fixed_time_slot=plan.fixed_time_slot,
                 duration_unit=plan.duration_unit, duration_value=plan.duration_value,
                 included_classes=None, schedule=None, first_class_at=None, available_places=None)
    if is_day_pass(plan):
        session = await db.get(ClassSession, int(session_id)) if session_id else None
        if not session or session.status != "scheduled" or local_date(session.start_at) != start_date:
            raise ValueError("El pase diario requiere una clase disponible de la fecha elegida.")
        from app.crud.reservationsCrud import get_available_seats
        seats = await get_available_seats(db, session.id)
        used = await db.scalar(select(func.count()).select_from(Reservation).where(
            Reservation.session_id == session.id, Reservation.status.in_(("reserved", "checked_in"))))
        available = sum(s.is_available for s in seats) if seats else max(0, session.capacity - used)
        facts.update(start_at=session.start_at.isoformat(), end_at=session.end_at.isoformat(), included_classes=1,
                     schedule=to_local(session.start_at).strftime("%d/%m/%Y a las %H:%M"),
                     first_class_at=session.start_at.isoformat(), available_places=available)
    elif plan.fixed_time_slot:
        from app.crud.standing_bookings.utils import _get_templates_in_same_group
        templates = await _get_templates_in_same_group(db, int(template_id)) if template_id else []
        if not templates:
            raise ValueError("¿Qué horario prefieres para tus clases?")
        dates = occurrences(plan, templates, start, end)
        sessions = (await db.scalars(select(ClassSession).where(ClassSession.template_id.in_([t.id for t in templates]),
            ClassSession.start_at >= start, ClassSession.start_at < end))).all()
        canceled = {(s.template_id, local_date(s.start_at)) for s in sessions if s.status != "scheduled"}
        dates = [(tid, day) for tid, day in dates if (tid, day) not in canceled]
        if not dates or min(day for _, day in dates) != start_date:
            raise ValueError("Ese día no hay una clase de ese horario. ¿Qué otra fecha disponible prefieres para comenzar?")
        times = {t.id: t.start_time_local for t in templates}
        first = min(local_datetime(day, times[tid]).astimezone(timezone.utc) for tid, day in dates)
        seats = (await db.scalars(select(Seat).where(Seat.venue_id == templates[0].venue_id, Seat.is_active.is_(True)))).all()
        occupied = set((await db.scalars(select(Reservation.seat_id).join(ClassSession).where(
            ClassSession.template_id.in_([t.id for t in templates]), ClassSession.start_at >= start,
            ClassSession.start_at < end, Reservation.status.in_(("reserved", "checked_in")),
            Reservation.person_id != (member_id or -1)))).all())
        recurring = (await db.scalars(select(StandingBooking).where(
            StandingBooking.template_id.in_([t.id for t in templates]), StandingBooking.status == "active",
            StandingBooking.start_date <= last_day(end), StandingBooking.end_date >= start_date,
            StandingBooking.person_id != (member_id or -1)))).all()
        occupied.update(sb.seat_id for sb in recurring)
        if seats:
            available = sum(s.id not in occupied for s in seats)
        else:
            counts = [sum(r.session_id == s.id for r in (await db.scalars(select(Reservation).where(
                Reservation.session_id == s.id, Reservation.status.in_(("reserved", "checked_in"))))).all()) for s in sessions]
            available = min([t.capacity for t in templates] + [s.capacity - count for s, count in zip(sessions, counts)])
            available = min(available, min(t.capacity for t in templates) - len({sb.person_id for sb in recurring}))
        facts.update(schedule=schedule_label(templates), included_classes=len(dates),
                     first_class_at=first.isoformat(), available_places=max(0, available))
    elif plan.plan_type == "credit_pack":
        facts["included_classes"] = plan.class_limit
    if facts["first_class_at"] and not allow_started and stamp(facts["first_class_at"]) <= at:
        raise ValueError("La primera clase de esa fecha ya comenzó. ¿Qué otra fecha prefieres para iniciar?")
    if facts["available_places"] is not None and facts["available_places"] <= 0:
        raise ValueError("Ese horario no tiene cupo para todo el periodo. Revisemos otra fecha u horario.")
    if start_date < local_date(at) and not allow_started:
        raise ValueError("Necesito una fecha de inicio actual o futura.")
    if member_id and not is_day_pass(plan):
        overlap = await db.scalar(select(MembershipSubscription.id).where(
            MembershipSubscription.person_id == member_id, MembershipSubscription.plan_id == plan_id,
            MembershipSubscription.status == "active", MembershipSubscription.start_at < end,
            MembershipSubscription.end_at > start).limit(1))
        if overlap:
            raise ValueError("Ya tienes una membresía de este plan que cubre parte de esas fechas. Revisemos su vigencia antes de cobrar otra.")
    return facts


async def propose(ctx, *, plan_id, start_date=None, template_id=None, session_id=None, full_name=None):
    from app.services.chatbot.verification import requested_day
    if not start_date:
        return "¿Qué día quieres comenzar? Confirma la fecha completa antes de preparar el pago."
    day = date.fromisoformat(start_date)
    evidence = requested_day(ctx.user_text) or ctx.start_date_evidence
    existing = await active_order(ctx.db, ctx.conversation_id)
    if evidence != day and not (existing and existing.start_date == day):
        return "Pide al cliente que confirme la fecha de inicio; no la deduzcas del pago ni del día de una plantilla."
    if ctx.member_id is None and not (full_name or "").strip():
        return "¿Cuál es tu nombre completo para preparar el registro?"
    payload = dict(plan_id=int(plan_id), template_id=template_id, session_id=session_id,
                   full_name=full_name, phone_number=ctx.wa_id)
    if existing:
        if existing.payment_id:
            return "Ya recibimos un pago. Usa confirm_membership_start para resolver la fecha sin cobrar de nuevo."
        if existing.start_date == day and all(existing.payload.get(k) == v for k, v in payload.items() if v is not None):
            return summary(existing.facts) + ("\nYa existe un enlace vigente; no generes otro." if existing.mp_init_point else "\n¿Confirmas estos datos?")
        return "Ya hay una propuesta o compra en curso. Aclara el cambio y cancela la anterior antes de preparar otra."
    facts = await facts_for(ctx.db, plan_id=plan_id, start_date=day, template_id=template_id,
                            session_id=session_id, member_id=ctx.member_id)
    order = Order(conversation_id=ctx.conversation_id, member_id=ctx.member_id,
                  action_type="buy_day_pass" if session_id else "buy_package", payload=payload,
                  facts=facts, start_date=day, first_class_at=stamp(facts["first_class_at"]) if facts["first_class_at"] else None,
                  amount=Decimal(facts["amount"]), status="draft", external_reference=f"membership-{uuid.uuid4().hex}")
    ctx.db.add(order)
    await ctx.db.commit()
    ctx.order_id = order.id
    return summary(facts) + "\n¿Confirmas la fecha y el horario para preparar tu enlace de pago?"


async def checkout(ctx):
    order = await active_order(ctx.db, ctx.conversation_id)
    if not order:
        return "Primero confirma el plan, horario y fecha de inicio."
    if order.payment_id:
        return "Ya recibimos el pago; falta resolver el inicio. No solicites otro cobro."
    if order.mp_init_point:
        ctx.order_id = order.id
        return "Ya existe un enlace para esa compra. Reutilízalo con kind=purchase."
    from app.services.chatbot.intents import plain
    if plain(ctx.user_text) not in {"si", "si claro", "si por favor", "confirmo", "confirmado", "perfecto", "adelante", "dale", "ok", "de acuerdo"}:
        return "Necesito la confirmación del cliente sobre el resumen antes de generar el enlace."
    # Lock serializes two confirmations; provider request itself is idempotent by order key.
    order = await ctx.db.scalar(select(Order).where(Order.id == order.id).with_for_update())
    if order.mp_init_point:
        return "El enlace de esa compra ya está preparado."
    fresh = await facts_for(ctx.db, **{k: order.payload.get(k) for k in ("plan_id", "template_id", "session_id")},
                            start_date=order.start_date, member_id=order.member_id)
    if any(fresh[k] != order.facts[k] for k in ("amount", "start_at", "end_at", "schedule", "included_classes")):
        return "La disponibilidad o las condiciones cambiaron. Revisa y confirma un nuevo resumen."
    result = await mp.create_preference(amount=order.amount, description=order.facts["plan_name"],
        external_reference=order.external_reference, payer_name=order.payload.get("full_name"), idempotency_key=order.external_reference)
    order.mp_preference_id, order.mp_init_point = result["preference_id"], result["init_point"]
    order.status, order.confirmation_message_id, order.updated_at = "awaiting_payment", ctx.message_id, now()
    await ctx.db.commit()
    ctx.order_id = order.id
    return "Enlace listo. Usa respond_to_customer(kind=purchase) para enviarlo. El cupo se confirma al acreditar el pago."


async def apply_order(db, order, *, allow_reconfirmed=False):
    """Caller holds order lock; nested savepoint makes all attendance writes atomic."""
    if order.status == "confirmed":
        return "Tu pago y membresía ya están registrados."
    if not order.payment_id or not order.approved_at:
        raise ValueError("No hay un pago acreditado verificable para esta orden.")
    if not allow_reconfirmed and (order.first_class_at and order.approved_at >= order.first_class_at):
        order.status = "paid_awaiting_start"
        return "Recibimos tu pago. La primera clase elegida ya comenzó. ¿Qué nueva fecha disponible prefieres para iniciar? No necesitas pagar de nuevo."
    payload = order.payload
    fresh = await facts_for(db, **{k: payload.get(k) for k in ("plan_id", "template_id", "session_id")},
                            start_date=order.start_date, member_id=order.member_id, allow_started=not allow_reconfirmed)
    if any(fresh[k] != order.facts[k] for k in ("start_at", "end_at", "schedule", "included_classes", "plan_type", "fixed_time_slot")):
        raise ValueError("Las condiciones del horario cambiaron y requieren revisión del equipo.")
    await db.execute(sql_text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key("membership_person", payload.get("phone_number") or order.member_id)})
    from app.crud import membersCrud
    member_id = order.member_id or await membersCrud.get_member_id_by_wa_id(db, payload.get("phone_number"))
    payment = await db.get(Payment, order.payment_id)
    if payment.status != "COMPLETED" or payment.subscription_id is not None:
        raise ValueError("El pago ya está aplicado o requiere revisión.")
    if order.action_type == "buy_day_pass":
        from app.crud.memberships.day_passes import purchase_day_pass
        purchase = await purchase_day_pass(db, plan_id=payload["plan_id"], session_id=payload["session_id"],
            person_id=member_id, full_name=payload.get("full_name"), phone_number=payload.get("phone_number"),
            existing_payment=payment, payment_amount=order.amount)
        subscription, person_id, bike = purchase.subscription, purchase.person.id, None
    else:
        if member_id is None:
            person = await membersCrud.create_member(db, full_name=payload["full_name"], phone_number=payload.get("phone_number"), commit=False)
            member_id = person.id
        # Recheck under the per-member lock, including a member enrolled by another conversation.
        await facts_for(db, plan_id=payload["plan_id"], template_id=payload.get("template_id"),
                        start_date=order.start_date, member_id=member_id, allow_started=not allow_reconfirmed)
        subscription = MembershipSubscription(person_id=member_id, plan_id=payload["plan_id"],
            start_at=stamp(order.facts["start_at"]), end_at=stamp(order.facts["end_at"]), status="active", period_rule_version=2)
        db.add(subscription)
        await db.flush()
        plan = await db.get(MembershipPlan, payload["plan_id"])
        subscription.plan = plan
        bike = None
        if plan.fixed_time_slot:
            sessions = (await db.scalars(select(ClassSession).where(ClassSession.template_id == payload["template_id"],
                ClassSession.start_at >= subscription.start_at, ClassSession.start_at < subscription.end_at).order_by(ClassSession.id))).all()
            for session in sessions:
                await lock_class_session(db, session.id)
            from app.crud.memberships.standing_bookings import _handle_fixed_timeslot_effects, _assert_materialization_success
            _, stats = await _handle_fixed_timeslot_effects(db, subscription, plan, payload["template_id"], commit_sessions=False)
            _assert_materialization_success(stats)
            bike = stats.get("assigned_seat_label")
        person_id = member_id
    payment.person_id, payment.subscription_id = person_id, subscription.id
    order.member_id, order.subscription_id, order.status, order.updated_at = person_id, subscription.id, "confirmed", now()
    return "¡Pago acreditado! Tu registro quedó listo.\n" + summary(order.facts, receipt=True, bike=bike)


async def approved_payment(db, payment_id, payment):
    """Returns None only for references owned by the legacy/event handlers."""
    order = await db.scalar(select(Order).where(Order.external_reference == payment.get("external_reference")).with_for_update())
    if not order:
        return None
    if order.status == "confirmed" or order.payment_id:
        return {"conversation_id": order.conversation_id, "text": None}
    raw = payment.get("date_approved")
    approved_at = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    if (payment.get("status") != "approved" or Decimal(str(payment.get("transaction_amount", "-1"))) != order.amount
            or payment.get("currency_id") != "MXN"):
        order.status, order.review_reason = "needs_review", "El importe, moneda o estado no coincide con la orden."
        await db.commit()
        return {"conversation_id": order.conversation_id, "text": "El pago requiere revisión del equipo. No realices otro pago."}
    existing = await db.scalar(select(Payment).where(Payment.provider_payment_id == str(payment_id)))
    if existing:
        order.status, order.review_reason = "needs_review", "Ese pago ya se registró para otra operación."
        await db.commit()
        return {"conversation_id": order.conversation_id, "text": None}
    proof = Payment(person_id=order.member_id, amount=order.amount, method="mercadopago", provider="mercadopago",
        provider_payment_id=str(payment_id), external_reference=order.external_reference, status="COMPLETED",
        paid_at=approved_at or now(), comment="WhatsApp bot; pago recibido, aplicación por orden")
    db.add(proof)
    await db.flush()
    order.payment_id, order.approved_at = proof.id, approved_at
    old_status = order.status
    order.status = "paid_awaiting_start"
    await db.commit()  # Financial proof survives an allocation failure or a process restart.
    order = await db.scalar(select(Order).where(Order.id == order.id).with_for_update())
    try:
        async with db.begin_nested():
            if approved_at is None or old_status == "canceled":
                raise ValueError("El pago no tiene fecha de aprobación verificable o pertenece a una orden cancelada.")
            reply = await apply_order(db, order)
    except ValueError as exc:
        order.status, order.review_reason = "needs_review", str(exc)
        reply = "Recibimos tu pago, pero no pudimos aplicar la reserva. Nuestro equipo debe revisar el caso. No necesitas pagar nuevamente."
    order.updated_at = now()
    await db.commit()
    return {"conversation_id": order.conversation_id, "text": reply}


async def confirm_start(db, order_id, start_date, *, conversation_id=None, session_id=None):
    order = await db.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if not order or (conversation_id is not None and order.conversation_id != conversation_id):
        raise ValueError("No encontré esa compra en esta conversación.")
    if order.status != "paid_awaiting_start" or not order.payment_id:
        raise ValueError("Esta orden no tiene un pago pendiente de confirmar inicio.")
    payload = dict(order.payload)
    if session_id:
        payload["session_id"] = session_id
    facts = await facts_for(db, plan_id=payload["plan_id"], template_id=payload.get("template_id"),
                            session_id=payload.get("session_id"), start_date=start_date, member_id=order.member_id)
    async with db.begin_nested():
        order.payload, order.facts, order.start_date = payload, facts, start_date
        order.first_class_at = stamp(facts["first_class_at"]) if facts["first_class_at"] else None
        reply = await apply_order(db, order, allow_reconfirmed=True)
    await db.commit()
    return reply


async def cancel_unpaid(db, order):
    if order.payment_id:
        return "Ya hay un pago recibido. El equipo debe resolverlo; no realices otro cobro."
    payments = await mp.search_payments(order.external_reference) if order.mp_init_point else []
    if any(p.get("status") in ("approved", "pending", "in_process", "authorized", "in_mediation") for p in payments):
        return "Hay un pago aprobado o en proceso. Verifica su estado antes de pagar por otro medio."
    if order.mp_preference_id:
        await mp.expire_preference(order.mp_preference_id)
    order.status, order.updated_at = "canceled", now()
    await db.commit()
    return "La compra en línea quedó cancelada. Puedes hacer tu registro y pago en recepción con nuestro equipo."
