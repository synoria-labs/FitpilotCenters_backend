"""Resolve event inquiries independently of campaign delivery; never trust ad sale details."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import unicodedata

from sqlalchemy import select

from app.crud.special_events import events as events_crud
from app.crud.special_events.common import local_schedule_label, money_label
from app.models import Message, Venue, People
from app.core.timezones import to_local
from app.services.whatsapp_referrals import normalize_referral

_STOP_WORDS = set("evento eventos clase clases especial especiales fitness love aniversario del para con las los una uno desde informacion informes quiero interesa interesado interesada precio horarios horario spinning pilates yoga zumba".split())
_EVENT = re.compile(r"\b(?:evento[s]?|fiesta|aniversario|anuncio|publicacion|facebook|instagram)\b")
_REGULAR = re.compile(r"\b(?:membresia|mensual|semanal|pase diario|clases regulares|clase regular)\b")


def plain(text):
    return " ".join(re.findall(r"[a-z0-9]+", "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))))


def matching_events(events, text):
    words = set(plain(text).split())
    names = {e.id: {
        word for word in plain(e.name).split() if len(word) >= 4 and not word.isdigit() and word not in _STOP_WORDS
    } for e in events}
    unique = [eid for eid, tokens in names.items() if words & {
        token for token in tokens if sum(token in other for other in names.values()) == 1
    }]
    # A venue/name unique to one event can disambiguate a shared title such as Halloween.
    return narrow_by_date(events, unique or [eid for eid, tokens in names.items() if words & tokens], text)


def narrow_by_date(events, matches, text):
    if len(matches) < 2:
        return matches
    from app.services.chatbot.verification import requested_day
    try:
        day = requested_day(text)
    except ValueError:
        return matches
    narrowed = [e.id for e in events if e.id in matches and to_local(e.start_at, e.timezone).date() == day]
    return narrowed or matches


@dataclass(frozen=True)
class EventDiscovery:
    interested: bool = False
    event_id: int | None = None
    candidates: tuple[int, ...] = ()
    source: str = "none"


def _interpret(events, text, referral=None):
    matches = matching_events(events, text)
    if matches:
        if _REGULAR.search(plain(text)):
            return EventDiscovery(True, candidates=tuple(matches), source="evento o membresía sin aclarar")
        return EventDiscovery(True, matches[0] if len(matches) == 1 else None, tuple(matches), "mensaje del cliente")
    if _REGULAR.search(plain(text)):
        return EventDiscovery(source="regular")
    referral = normalize_referral(referral)
    if referral:
        ad_text = referral.get("headline", "") + " " + referral.get("body", "")
        matches = matching_events(events, ad_text)
        if matches:
            return EventDiscovery(True, matches[0] if len(matches) == 1 else None, tuple(matches), "referencia del anuncio")
        if _REGULAR.search(plain(ad_text)):
            return EventDiscovery(source="regular")
    if referral or _EVENT.search(plain(text)):
        return EventDiscovery(True, candidates=tuple(e.id for e in events), source="consulta sin evento identificado")
    return EventDiscovery()


async def resolve(db, conversation_id, user_text, *, message_id=None):
    now = datetime.now(timezone.utc)
    events = [e for e in await events_crud.list_events(db, statuses=["published"]) if e.start_at > now]
    current = await db.get(Message, message_id) if message_id else None
    if current and (current.conversation_id != conversation_id or current.direction != "inbound"):
        current = None
    result = _interpret(events, user_text, getattr(current, "referral", None))
    if result.interested or result.source == "regular":
        return result
    # Keep a recent direct/ad inquiry across short follow-ups such as the participant's name.
    # A later explicit topic change stops inheriting it. Only inbound messages establish intent.
    query = select(Message).where(
        Message.conversation_id == conversation_id, Message.direction == "inbound",
        Message.timestamp >= (now - timedelta(hours=24)).replace(tzinfo=None),
    )
    if message_id:
        query = query.where(Message.id < message_id)
    rows = (await db.scalars(query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(12))).all()
    recent_text = user_text
    for row in rows:
        prior = _interpret(events, row.text_content or "", getattr(row, "referral", None))
        if prior.source == "regular":
            break
        if prior.interested:
            candidates = narrow_by_date(events, list(prior.candidates), recent_text)
            selected = candidates[0] if len(candidates) == 1 and (prior.event_id or len(prior.candidates) > 1) else prior.event_id
            return EventDiscovery(True, selected, tuple(candidates), "consulta reciente del cliente")
        recent_text += " " + (row.text_content or "")
    return result


async def selection_reply(db, discovery):
    events = [e for e in await events_crud.list_events(db, statuses=["published"])
              if e.start_at > datetime.now(timezone.utc) and e.id in discovery.candidates]
    if not events:
        return "Por ahora no encuentro un evento publicado que coincida con tu consulta. ¿Puedes decirme el nombre del evento o qué viste en la publicación?"
    question = ("¿Te interesa la entrada al evento o una membresía para las clases regulares? Son compras diferentes."
                if discovery.source == "evento o membresía sin aclarar" else
                "Para darte la información del evento correcto, ¿cuál de estas opciones te interesa?")
    lines = [question]
    lines += [f"• {e.name}: {local_schedule_label(e.start_at, e.end_at, e.timezone)}, {money_label(e.price, e.currency)}."
              for e in events]
    return "\n".join(lines)


async def offer_reply(db, event_id):
    event = await events_crud.get_event(db, event_id)
    if event is None or event.status != "published" or event.start_at <= datetime.now(timezone.utc):
        return "Ese evento ya no está disponible para comprar. ¿Quieres consultar los próximos eventos publicados?"
    avail = await events_crud.availability(db, event)
    venue = await db.get(Venue, event.venue_id)
    instructor = await db.get(People, event.instructor_id) if event.instructor_id else None
    lines = [f"🎟️ {event.name}", f"📅 {local_schedule_label(event.start_at, event.end_at, event.timezone)} (hora de México)."]
    address = event.address or (venue.address if venue else None)
    if address:
        lines.append(f"📍 {address}")
    if instructor and instructor.full_name:
        lines.append(f"Instructora: {instructor.full_name}")
    lines.append(f"Precio: {money_label(event.price, event.currency)} por participante.")
    if event.description:
        lines.append(event.description)
    if event.includes:
        lines.append("Incluye:\n" + "\n".join(f"• {line.strip()}" for line in event.includes.splitlines() if line.strip()))
    if not avail.sales_open:
        lines.append(f"En este momento no está disponible para comprar: {avail.sales_closed_reason or 'sin cupo disponible'}.")
        return "\n".join(lines)
    lines.append(f"Lugares disponibles: {avail.available}.")
    if event.payment_link_expires:
        lines.append(f"Al iniciar la compra se aparta el lugar por {event.hold_minutes} minutos mientras realizas el pago en Mercado Pago.")
    else:
        lines.append("El enlace de Mercado Pago no tiene caducidad y no aparta lugar. El cupo se verifica al acreditarse el pago.")
    lines.append("La entrada se confirma cuando el pago queda acreditado. Para comprarla, compárteme el nombre completo del participante.")
    return "\n".join(lines)


async def fallback_reply(db, discovery):
    return await offer_reply(db, discovery.event_id) if discovery.event_id else await selection_reply(db, discovery)
