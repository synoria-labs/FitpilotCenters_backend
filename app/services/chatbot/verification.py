"""Server-owned confirmations: old chat text is never evidence of a reservation."""
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone, date

from sqlalchemy import select

from app.core.timezones import to_local, local_datetime
from app.models import ClassSession, Reservation
from app.services.chatbot.timefmt import fmt_dt

logger = logging.getLogger(__name__)

# Defense in depth for an incorrectly classified free-text response. Confirmations themselves
# are always rendered below, never copied from the model.
_CONFIRMATION = re.compile(
    r"(?:\b(?:tu|su|la)\s+(?:reserva|lugar|bici|clase|asiento|entrada)\b.{0,100}"
    r"\b(?:confirmad[oa]|activ[oa]|reservad[oa]|apartad[oa]|pagad[oa])|"
    r"\b(?:ya\s+)?tienes\s+(?:tu\s+)?(?:lugar|reserva|bici|asiento)|"
    r"\b(?:te\s+(?:reserve|apart[eé])|reserva\s*#|pago\s+acreditado)|"
    r"\b(?:todo\s+listo|te\s+esperamos|nos\s+vemos\s+en\s+clase)|"
    r"\b(?:estas|estás)\s+list[oa]\s+para\s+(?:pedalear|asistir))", re.I | re.S,
)


def requested_day(text, now=None):
    now = to_local(now or datetime.now(timezone.utc))
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    if "pasado manana" in plain:
        return now.date() + timedelta(days=2)
    if re.search(r"(?<!por la )(?<!en la )\bmanana\b", plain):
        return now.date() + timedelta(days=1)
    if re.search(r"\bhoy\b", plain):
        return now.date()
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", plain)
    if match:
        return date.fromisoformat(match[1])
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", plain)
    if match:
        return date(int(match[3] or now.year), int(match[2]), int(match[1]))
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")
    match = re.search(r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(months) + r")(?:\s+(?:de\s+)?(\d{4}))?\b", plain)
    if match:
        return date(int(match[3] or now.year), months.index(match[2]) + 1, int(match[1]))
    for weekday, name in enumerate(("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")):
        if re.search(rf"\b{name}\b", plain):
            days = (weekday - now.weekday()) % 7
            return now.date() + timedelta(days=days or (7 if "proximo" in plain else 0))
    return None


def requested_hours(text):
    match = re.search(r"(?:\b(?:a\s+)?las?\s+|^)(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", text.lower())
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if hour > 23 or minute > 59:
        return set()
    suffix = (match[3] or "").replace(".", "")
    if suffix:
        return {(hour % 12 + (12 if suffix == "pm" else 0), minute)}
    return {(hour, minute), (hour + 12, minute)} if 1 <= hour <= 11 else {(hour, minute)}


async def requested_session_error(db, session, user_text, fallback_date=None):
    local_start = to_local(session.start_at)
    target_day = requested_day(user_text) or fallback_date
    hours = requested_hours(user_text)
    if (target_day and local_start.date() != target_day) or (hours is not None and (local_start.hour, local_start.minute) not in hours):
        return "La fecha u hora de esa clase no coincide con lo solicitado. Busca la clase correcta."
    if hours and len(hours) > 1:
        from datetime import time
        start = local_datetime(target_day or local_start.date(), time.min)
        alternatives = (await db.execute(select(ClassSession).where(
            ClassSession.status == "scheduled", ClassSession.end_at > datetime.now(timezone.utc),
            ClassSession.start_at >= start, ClassSession.start_at < start + timedelta(days=1),
        ))).scalars().all()
        if sum((to_local(s.start_at).hour, to_local(s.start_at).minute) in hours for s in alternatives) > 1:
            return "Hay más de una clase posible para ese horario. Aclara hora y sede antes de confirmar."
    return None


async def reservation_context(db, member_id):
    if member_id is None:
        return "No hay un socio identificado; no se puede confirmar una reserva existente."
    rows = (await db.execute(
        select(Reservation, ClassSession).join(ClassSession, ClassSession.id == Reservation.session_id)
        .where(Reservation.person_id == member_id, Reservation.status.in_(("reserved", "checked_in")),
               ClassSession.status == "scheduled", ClassSession.end_at > datetime.now(timezone.utc))
        .order_by(ClassSession.start_at).limit(30)
    )).all()
    lines = ["Reservas vigentes verificadas AHORA (una membresía activa no es una reserva):"]
    lines.extend(f"Reserva #{r.id}, session_id={s.id}: {fmt_dt(s.start_at)} a {fmt_dt(s.end_at)}." for r, s in rows)
    if not rows:
        lines.append("NINGUNA. No afirmes que ya tiene lugar; inicia una compra si la solicita.")
    return "\n".join(lines)


async def finalize_reply(ctx, *, kind, text="", session_id=None, reservation_id=None, requested_date=None, order_id=None,
                         plan_id=None, template_id=None, event_id=None, membership_order_id=None):
    """Mandatory final tool; only DB-verified facts can produce a class confirmation.

    A checkout link is never written into the reply: it goes in ``ctx.payment_link`` and the
    reply service sends it as a «Pagar ahora» button."""
    from app.services.chatbot.tools import PaymentLink

    ctx.validated_reply = None
    ctx.payment_link = None
    ctx.verification_result = "rejected"
    ctx.reply_request = dict(kind=kind, text=text, session_id=session_id, reservation_id=reservation_id,
                             requested_date=requested_date, order_id=order_id, plan_id=plan_id, template_id=template_id,
                             event_id=event_id)
    ctx.reply_request["membership_order_id"] = membership_order_id
    from .intents import acknowledgment, payment_question, reservation_question
    simple = acknowledgment(ctx.user_text)
    if simple or kind == "acknowledgment":
        if not simple:
            return "El cliente tiene una consulta concreta. Responde esa consulta antes de cerrar."
        ctx.validated_reply, ctx.verification_result = simple, "accepted"
        return "Respuesta breve validada."
    if payment_question(ctx.user_text) or kind == "payment_options":
        from .sales import payment_answer
        ctx.validated_reply = await payment_answer(ctx)
        ctx.verification_result = "accepted"
        return "Métodos de pago verificados."
    if kind == "reservation_confirmation" and not reservation_question(ctx.user_text):
        return "El cliente no consultó una reserva. Responde su pregunta o pide aclaración sin confirmar asistencia."
    if ctx.sales_v2_enabled and kind in ("purchase", "membership_information") and order_id is None:
        from .sales import active_order, summary, facts_for
        from app.models.chatbotSalesModel import MembershipPurchaseOrder
        order = await ctx.db.get(MembershipPurchaseOrder, membership_order_id or ctx.order_id) if (membership_order_id or ctx.order_id) else await active_order(ctx.db, ctx.conversation_id)
        if order is not None:
            if order.conversation_id != ctx.conversation_id:
                return "Esa compra no pertenece a esta conversación."
            if order.status == "confirmed":
                if kind != "purchase":
                    ctx.validated_reply = summary(order.facts)
                else:
                    ctx.validated_reply = "Tu pago y registro ya están confirmados.\n" + summary(order.facts)
            elif order.payment_id:
                ctx.validated_reply = "Recibimos tu pago. ¿Qué nueva fecha disponible prefieres para comenzar? No necesitas pagar de nuevo."
            elif order.status == "draft":
                ctx.validated_reply = summary(order.facts) + "\n¿Confirmas estos datos para preparar tu enlace de pago?"
            elif order.status == "awaiting_payment" and order.mp_init_point:
                if order.first_class_at and order.first_class_at <= datetime.now(timezone.utc):
                    return "La primera clase de esa fecha ya comenzó. Revisa el estado del pago antes de preparar otra fecha."
                ctx.validated_reply = summary(order.facts) if kind == "membership_information" else "Tu enlace está listo. Puedes pagar con tarjeta o transferencia. El cupo se confirma al acreditarse el pago."
                if kind == "purchase":
                    ctx.payment_link = PaymentLink(order.mp_init_point, "Completa el pago aquí:")
            else:
                return "Esa compra requiere revisión antes de enviar un enlace o confirmar reservas."
        elif plan_id is not None:
            from app.models import MembershipPlan
            plan = await ctx.db.get(MembershipPlan, plan_id)
            if not plan or not plan.is_active:
                return "Ese plan no está disponible."
            ctx.validated_reply = f"{plan.name}: ${plan.price:.2f} MXN. ¿Qué día quieres comenzar para revisar el horario y las clases incluidas?"
        else:
            return "Primero identifica el plan, horario y fecha elegida."
        ctx.verification_result = "accepted"
        return "Respuesta comercial verificada."
    explicit_day = requested_day(ctx.user_text)
    target_day = explicit_day or (date.fromisoformat(requested_date) if requested_date else None)
    from app.crud.chatbotPendingCrud import get_active_pending
    from app.crud.memberships.coverage import package_facts
    pending = await get_active_pending(ctx.db, ctx.conversation_id) if order_id is None and kind in (
        "information", "clarification", "membership_information", "purchase",
    ) else None
    package_pending = pending is not None and pending.action_type == "buy_package"
    coverage_question = re.search(r"\b(?:membres[ií]a|mensual|semanal|mi mes|empieza|vigencia|cubre|incluye|clases|horario)\b",
                                  ctx.user_text, re.I)
    if kind == "membership_information" or (package_pending and coverage_question and kind in ("information", "clarification")):
        subscription = None
        if package_pending:
            plan_id = pending.payload["plan_id"]
            template_id = pending.payload.get("template_id")
        elif ctx.member_id:
            from app.crud.memberships.subscriptions import get_member_active_subscription
            from app.models import StandingBooking
            current = await get_member_active_subscription(ctx.db, ctx.member_id)
            if current and (plan_id is None or plan_id == current.plan_id):
                subscription = current
                plan_id = current.plan_id
                template_id = await ctx.db.scalar(select(StandingBooking.template_id).where(
                    StandingBooking.subscription_id == current.id,
                ).order_by(StandingBooking.id).limit(1))
        if plan_id is None:
            return "Consulta el plan y su grupo de horario antes de explicar la cobertura."
        try:
            ctx.validated_reply = await package_facts(
                ctx.db, plan_id=plan_id, template_id=template_id, member_id=ctx.member_id,
                subscription=subscription,
            )
        except ValueError as exc:
            return str(exc)
        ctx.verification_result = "accepted"
        logger.info("chatbot_membership_verification conversation=%s plan=%s template=%s result=accepted",
                    ctx.conversation_id, plan_id, template_id)
        return "Cobertura verificada. Finaliza el turno sin modificarla."
    discovery = ctx.event_discovery
    if (kind in ("information", "clarification") and discovery and discovery.interested
            and discovery.source != "consulta reciente del cliente"
            and not re.search(r"\b(?:cancel\w*|pag\w*|comprobante|reembolso|devoluci[oó]n|error)\b", ctx.user_text, re.I)):
        from app.services.chatbot.event_discovery import fallback_reply
        ctx.validated_reply = await fallback_reply(ctx.db, discovery)
        ctx.verification_result = "accepted"
        return "Información del evento verificada por el servidor."
    if kind == "event_information":
        from app.services.chatbot.event_discovery import offer_reply
        discovery = ctx.event_discovery
        if event_id is None or (discovery and discovery.interested and discovery.event_id != event_id):
            return "Identifica el evento con el cliente antes de ofrecer una opción concreta. No elijas entre eventos ambiguos."
        ctx.validated_reply = await offer_reply(ctx.db, event_id)
    elif kind == "reservation_confirmation":
        if ctx.member_id is None or session_id is None or reservation_id is None or target_day is None:
            return "Falta identificar cliente, fecha, sesión y reserva. Consulta las herramientas; no confirmes aún."
        row = (await ctx.db.execute(
            select(Reservation, ClassSession).join(ClassSession, ClassSession.id == Reservation.session_id)
            .where(Reservation.id == reservation_id, Reservation.person_id == ctx.member_id,
                   Reservation.session_id == session_id, Reservation.status.in_(("reserved", "checked_in")),
                   ClassSession.status == "scheduled", ClassSession.end_at > datetime.now(timezone.utc))
            .execution_options(populate_existing=True)
        )).first()
        if row is None:
            return "Esa reserva no confirma la clase solicitada. Busca la clase correcta y propón su compra."
        reservation, session = row
        if reservation.subscription_id is not None:
            from app.models import MembershipSubscription
            subscription = await ctx.db.get(MembershipSubscription, reservation.subscription_id, populate_existing=True)
            if (subscription is None or subscription.status != "active" or subscription.person_id != ctx.member_id
                    or not subscription.start_at <= session.start_at < subscription.end_at):
                return "La reserva ya no tiene una membresía válida para esa clase. El equipo debe revisar su cobertura."
        mismatch = await requested_session_error(ctx.db, session, ctx.user_text, target_day)
        if mismatch:
            return mismatch
        ctx.validated_reply = (
            f"Tu reserva #{reservation.id} está confirmada para {fmt_dt(session.start_at)} "
            f"(hora de México). La clase termina el {fmt_dt(session.end_at)}."
        )
    elif kind == "event_confirmation":
        from app.models.specialEventModel import SpecialEventOrder, SpecialEvent
        order = await ctx.db.get(SpecialEventOrder, order_id, populate_existing=True) if order_id else None
        if order is None or order.conversation_id != ctx.conversation_id or order.status != "paid":
            return "La entrada del cliente no está pagada. Consulta check_event_order_status."
        if discovery and discovery.interested and discovery.event_id != order.event_id:
            return "La entrada pagada corresponde a otro evento; aclara cuál quiere consultar antes de confirmarla."
        event = await ctx.db.get(SpecialEvent, order.event_id, populate_existing=True)
        if event is None or event.status != "published" or event.end_at <= datetime.now(timezone.utc):
            return "El evento ya no está vigente; no confirmes asistencia."
        ctx.validated_reply = f"Tu entrada #{order.id} para {event.name} está pagada y confirmada para {fmt_dt(event.start_at)} (hora de México)."
    elif kind in ("information", "purchase", "clarification"):
        # A proposed checkout must also refer to the requested day, never yesterday's purchase.
        if kind == "purchase":
            if discovery and discovery.interested and order_id is None:
                return "El cliente consulta un evento especial. Usa purchase con el order_id de ese evento; no reutilices una compra de clase o membresía."
            from app.services.chatbot.purchase_status import PROCESSING_STATUSES
            if (pending is not None and ctx.checked_payment_reference
                    and pending.external_reference == ctx.checked_payment_reference
                    and ctx.provider_payment_status in PROCESSING_STATUSES):
                return "No envíes otro enlace: el proveedor/pago registrado indica un pago aprobado, en proceso o que requiere revisión. Informa el estado y verifica la reserva por separado."
            if pending is not None and pending.action_type == "buy_day_pass":
                session = await ctx.db.get(ClassSession, int(pending.payload["session_id"]), populate_existing=True)
                if session is None or session.status != "scheduled" or session.start_at <= datetime.now(timezone.utc):
                    return "La compra pendiente corresponde a una clase que ya no está disponible. Cancélala y ofrece otra."
                mismatch = await requested_session_error(ctx.db, session, ctx.user_text)
                if mismatch:
                    return mismatch
                ctx.validated_reply = f"Pase diario para {fmt_dt(session.start_at)}. Vence al terminar la clase: {fmt_dt(session.end_at)}.\n{pending.summary or ''}"
                if pending.mp_init_point:
                    ctx.payment_link = PaymentLink(pending.mp_init_point, "Para confirmar tu lugar, realiza el pago aquí:")
            elif package_pending:
                try:
                    ctx.validated_reply = await package_facts(
                        ctx.db, plan_id=pending.payload["plan_id"], template_id=pending.payload.get("template_id"),
                        member_id=ctx.member_id,
                    )
                except ValueError as exc:
                    return str(exc)
                if pending.mp_init_point:
                    ctx.payment_link = PaymentLink(pending.mp_init_point, "Para completar tu compra:")
            elif pending is not None:
                ctx.validated_reply = pending.summary or "Hay una compra pendiente."
                if pending.mp_init_point:
                    ctx.payment_link = PaymentLink(pending.mp_init_point, "Para completar tu compra:")
            elif order_id is not None:
                from app.models.specialEventModel import SpecialEventOrder, SpecialEvent
                from app.crud.special_events.events import get_event, availability
                order = await ctx.db.get(SpecialEventOrder, order_id, populate_existing=True)
                if order is None or order.conversation_id != ctx.conversation_id or order.status != "awaiting_payment":
                    return "No hay una compra de evento pendiente para este cliente. Consulta su estado."
                if discovery and discovery.interested and discovery.event_id != order.event_id:
                    return "Esa compra no corresponde al evento identificado; aclara el evento antes de enviar el enlace."
                event = await get_event(ctx.db, order.event_id)
                if event is None or event.status != "published" or event.start_at <= datetime.now(timezone.utc):
                    return "El evento ya no está disponible para comprar."
                if not order.mp_init_point:
                    return "El enlace aún no está disponible; consulta la compra antes de enviarlo."
                if not order.payment_link_expires:
                    avail = await availability(ctx.db, event)
                    if not avail.sales_open:
                        return "Ya no hay cupo disponible. No envíes ese enlace ni prometas un lugar."
                    policy = ("Enlace sin caducidad. No aparta lugar ni bicicleta. El cupo se verifica al acreditarse "
                              "el pago; si ya no hay lugar, el administrador revisará el pago y gestionará el "
                              "reembolso manual en Mercado Pago.")
                else:
                    if order.hold_expires_at and order.hold_expires_at <= datetime.now(timezone.utc):
                        return "El apartado y el enlace ya vencieron. Consulta disponibilidad antes de iniciar otra compra."
                    policy = f"El apartado y el enlace vencen el {fmt_dt(order.hold_expires_at)}." if order.hold_expires_at else ""
                ctx.validated_reply = f"Compra pendiente de pago: {event.name}, {fmt_dt(event.start_at)}.\n{policy}".strip()
                ctx.payment_link = PaymentLink(order.mp_init_point, "Para completar tu compra:")
            elif re.search(r"mercadopago\.|mpago\.", text, re.I):
                return "No hay una compra pendiente verificable para enviar ese enlace. Propón primero la compra o indica order_id para el evento."
        if ctx.validated_reply is None:
            if kind != "purchase" and re.search(r"(?:mercadopago\.|mpago\.)", text, re.I):
                return "Los enlaces de pago se envían con purchase y una compra vigente verificada; no reutilices enlaces de capturas o del historial."
            membership_topic = re.search(r"\b(?:membres[ií]a|mensual|semanal|paquete|plan|mi mes)\b",
                                         ctx.user_text + " " + text, re.I)
            if membership_topic and re.search(r"\b(?:cubre|incluye|incluidas|abarca|empieza|comienza)\b.{0,90}"
                                              r"\b(?:clases|membres[ií]a|mes|mi[eé]rcoles|pago)\b", text, re.I | re.S):
                return "La cobertura de membresía requiere membership_information con plan_id y grupo; no la deduzcas de un solo día."
            if _CONFIRMATION.search(text):
                return "El texto contiene una confirmación. Usa reservation_confirmation con fecha, sesión y reserva verificables."
            ctx.validated_reply = text.strip()
    else:
        return "Tipo de respuesta inválido."
    ctx.verification_result = "accepted"
    logger.info("chatbot_reply_verification conversation=%s kind=%s session=%s reservation=%s result=accepted",
                ctx.conversation_id, kind, session_id, reservation_id)
    return "Respuesta validada. Finaliza el turno sin modificarla."
