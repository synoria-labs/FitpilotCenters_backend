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


async def finalize_reply(ctx, *, kind, text="", session_id=None, reservation_id=None, requested_date=None, order_id=None):
    """Mandatory final tool; only DB-verified facts can produce a class confirmation."""
    ctx.validated_reply = None
    ctx.verification_result = "rejected"
    ctx.reply_request = dict(kind=kind, text=text, session_id=session_id, reservation_id=reservation_id,
                             requested_date=requested_date, order_id=order_id)
    explicit_day = requested_day(ctx.user_text)
    target_day = explicit_day or (date.fromisoformat(requested_date) if requested_date else None)
    if kind == "information" and re.search(r"\b(?:pagar|comprar|reservar|apartar|mi reserva)\b", ctx.user_text, re.I):
        return "La solicitud es de compra/reserva. Usa purchase, clarification o reservation_confirmation, con datos verificados."
    if kind == "reservation_confirmation":
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
        event = await ctx.db.get(SpecialEvent, order.event_id, populate_existing=True)
        if event is None or event.status != "published" or event.end_at <= datetime.now(timezone.utc):
            return "El evento ya no está vigente; no confirmes asistencia."
        ctx.validated_reply = f"Tu entrada #{order.id} para {event.name} está pagada y confirmada para {fmt_dt(event.start_at)} (hora de México)."
    elif kind in ("information", "purchase", "clarification"):
        # A proposed checkout must also refer to the requested day, never yesterday's purchase.
        if kind == "purchase":
            from app.crud.chatbotPendingCrud import get_active_pending
            pending = await get_active_pending(ctx.db, ctx.conversation_id)
            if pending is not None and pending.action_type == "buy_day_pass":
                session = await ctx.db.get(ClassSession, int(pending.payload["session_id"]), populate_existing=True)
                if session is None or session.status != "scheduled" or session.start_at <= datetime.now(timezone.utc):
                    return "La compra pendiente corresponde a una clase que ya no está disponible. Cancélala y ofrece otra."
                mismatch = await requested_session_error(ctx.db, session, ctx.user_text)
                if mismatch:
                    return mismatch
                link = pending.mp_init_point
                ctx.validated_reply = f"Pase diario para {fmt_dt(session.start_at)}. Vence al terminar la clase: {fmt_dt(session.end_at)}.\n{pending.summary or ''}"
                if link:
                    ctx.validated_reply += f"\nPara confirmar tu lugar, realiza el pago aquí: {link}"
            elif pending is not None:
                ctx.validated_reply = pending.summary or "Hay una compra pendiente."
                if pending.mp_init_point:
                    ctx.validated_reply += f"\nPara completar tu compra: {pending.mp_init_point}"
            elif order_id is not None:
                from app.models.specialEventModel import SpecialEventOrder, SpecialEvent
                order = await ctx.db.get(SpecialEventOrder, order_id, populate_existing=True)
                if order is None or order.conversation_id != ctx.conversation_id or order.status != "awaiting_payment":
                    return "No hay una compra de evento pendiente para este cliente. Consulta su estado."
                event = await ctx.db.get(SpecialEvent, order.event_id, populate_existing=True)
                if event is None or event.status != "published" or event.start_at <= datetime.now(timezone.utc):
                    return "El evento ya no está disponible para comprar."
                ctx.validated_reply = f"Compra pendiente de pago: {event.name}, {fmt_dt(event.start_at)}.\n{order.mp_init_point or ''}"
            elif re.search(r"mercadopago\.|mpago\.", text, re.I):
                return "No hay una compra pendiente verificable para enviar ese enlace. Propón primero la compra o indica order_id para el evento."
        if ctx.validated_reply is None:
            if _CONFIRMATION.search(text):
                return "El texto contiene una confirmación. Usa reservation_confirmation con fecha, sesión y reserva verificables."
            ctx.validated_reply = text.strip()
    else:
        return "Tipo de respuesta inválido."
    ctx.verification_result = "accepted"
    logger.info("chatbot_reply_verification conversation=%s kind=%s session=%s reservation=%s result=accepted",
                ctx.conversation_id, kind, session_id, reservation_id)
    return "Respuesta validada. Finaliza el turno sin modificarla."
