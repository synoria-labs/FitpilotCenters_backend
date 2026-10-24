"""Background orchestration for the WhatsApp chatbot reply.

The webhook must not block on the LLM, so ``schedule_agent_reply`` fires a background task
(mirroring ``whatsapp_media_service.schedule_download``). The task opens its OWN fresh DB
session — never the ingest pipeline's mid-transaction session — loads the conversation history,
resolves the member, runs the agent, and sends + persists the reply.

Outbound replies are persisted via ``insert_outbound_message`` exactly like the
``send_text_message`` mutation, so the bot's messages show up in the desktop Chats tab and the
realtime stream (the DB trigger fans the INSERT out to subscribers).
"""
import asyncio
import logging
from datetime import timezone
from typing import List, Optional, Set

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chatbot_env import chatbot_env
from app.core.mercadopago_config import mercadopago_config
from app.crud import campaignsCrud as campaigns_crud
from app.crud import chatbotConfigCrud
from app.crud import chatbotPendingCrud
from app.crud import membersCrud
from app.crud import whatsappCrud as crud
from app.db.postgresql import async_session_factory
from app.models.chatbotModel import (
    PENDING_STATUS_AWAITING_PAYMENT,
    PENDING_STATUS_PROCESSING,
)
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_outbound as outbound
from app.services.chatbot.agent import run_agent
from app.services.chatbot.business_context import build_business_info
from app.services.chatbot.tools import ChatbotContext, PaymentLink, build_tools
from app.services.chatbot.timefmt import fmt_dt
from app.services.chatbot.verification import reservation_context, finalize_reply
from app.services.chatbot import event_discovery, image_context, turn_queue

logger = logging.getLogger(__name__)

# Keep strong references so background tasks are not garbage-collected.
_background_tasks: Set["asyncio.Task"] = set()

_PAY_BUTTON_TEXT = "Pagar ahora"
_PAY_BUTTON_PROMPT = f"Para completar tu compra, toca el botón «{_PAY_BUTTON_TEXT}» 👇"


async def _send_with_payment_button(
    db: AsyncSession,
    *,
    kind: str,
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
    payment_link: PaymentLink,
) -> "outbound.OutboundResult":
    """Send the reply with its checkout link as a «Pagar ahora» button.

    A reply too long for a button message goes out as text first, followed by the button on its
    own. If the button can't be sent, the link is written into the text as before: the customer
    is never left without a way to pay.
    """
    common = dict(
        kind=kind,
        conversation_id=conversation_id,
        contact_id=contact_id,
        wa_id=to_wa_id,
        message_class=outbound.CLASS_TRANSACTIONAL,
    )
    text = text.strip()
    body = f"{text}\n{_PAY_BUTTON_PROMPT}" if text else _PAY_BUTTON_PROMPT
    fallback_body = f"{text}\n{payment_link.prompt}" if text else payment_link.prompt
    if len(body) > cloud.CTA_BODY_MAX:
        first = await outbound.send_text(db, text=text, **common)
        if not first.ok:
            return first
        body = _PAY_BUTTON_PROMPT
        fallback_body = payment_link.prompt
    result = await outbound.send_cta_url(
        db, body=body, button_text=_PAY_BUTTON_TEXT, url=payment_link.url, **common
    )
    if result.status is not outbound.SendStatus.FAILED:
        return result
    logger.warning("Payment button not sent (%s); sending the link as text", result.reason)
    return await outbound.send_text(db, text=f"{fallback_body} {payment_link.url}", **common)


async def send_and_persist_reply(
    db: AsyncSession,
    *,
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
    kind: str = outbound.KIND_CHATBOT_REPLY,
    payment_link: Optional[PaymentLink] = None,
) -> "outbound.OutboundResult":
    """Send a text reply through the unified outbound gateway and persist it.

    ``kind`` distinguishes a normal chatbot reply from a payment confirmation: the gateway never
    silently drops a payment confirm outside the 24h window (it records it for reconciliation).
    With ``payment_link`` the checkout link goes out as a «Pagar ahora» button.
    Returns the gateway result; existing callers may ignore it.
    """
    if payment_link is not None:
        result = await _send_with_payment_button(
            db,
            kind=kind,
            conversation_id=conversation_id,
            contact_id=contact_id,
            to_wa_id=to_wa_id,
            text=text,
            payment_link=payment_link,
        )
    else:
        result = await outbound.send_text(
            db,
            kind=kind,
            conversation_id=conversation_id,
            contact_id=contact_id,
            wa_id=to_wa_id,
            text=text,
            persist=True,
            message_class=outbound.CLASS_TRANSACTIONAL,
        )
    await db.commit()

    # The bot just answered this conversation, so the customer's inbound messages are
    # handled — mark them read (and send a read receipt) so they don't linger as unread
    # for staff. Best-effort; never let it break the reply flow.
    if result.ok:
        try:
            _conv, latest_wa_id = await crud.mark_conversation_read(db, conversation_id)
            if latest_wa_id:
                await cloud.send_read_receipt(latest_wa_id)
        except Exception:  # noqa: BLE001
            logger.debug("post-reply mark-read failed for %s", conversation_id, exc_info=True)

    return result


async def _load_history(
    db: AsyncSession, conversation_id: int, exclude_message_id: Optional[int], context_message_id: Optional[str] = None,
) -> List[BaseMessage]:
    """Build LangChain message history from the conversation's stored messages.

    Excludes the current inbound message (it is appended separately by the agent), reactions,
    and empty messages. Inbound -> HumanMessage, outbound -> AIMessage.
    """
    rows, selected_images = await image_context.history_rows(
        db, conversation_id=conversation_id, limit=chatbot_env.HISTORY_LIMIT,
        exclude_message_id=exclude_message_id, context_message_id=context_message_id,
    )
    messages: List[BaseMessage] = []
    for r in rows:
        if exclude_message_id is not None and r.id == exclude_message_id:
            continue
        if r.message_type == "reaction":
            continue
        text = (r.text_content or "").strip()
        if not text and r.message_type == "image":
            text = image_context.IMAGE_PLACEHOLDER
        if not text:
            continue
        stamp = getattr(r, "timestamp", None) or getattr(r, "created_at", None)
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        text = f"[Enviado: {fmt_dt(stamp) if stamp else 'fecha desconocida'}; hora de México] {text}"
        if r.direction == "inbound":
            content = await image_context.image_content(db, r, text, include_image=r.id in selected_images)
            messages.append(HumanMessage(content=content))
        else:
            # Label automated/campaign context so the bot can close a sale the customer is
            # responding to (and not mistake a campaign/notification for its own prior turn).
            if getattr(r, "message_class", None) == "marketing" or r.message_type == "template":
                text = f"[mensaje automático/campaña] {text}"
            elif "pago acreditado" in text.lower():
                text = f"[notificación automática de pago; corresponde a su fecha de envío] {text}"
            messages.append(AIMessage(content=text))
    return messages


async def _build_pending_note(db: AsyncSession, conversation_id: int) -> Optional[str]:
    """Tell the agent there's a pending action so it confirms/reminds-to-pay instead of re-proposing."""
    pending = await chatbotPendingCrud.get_active_pending(db, conversation_id)
    if pending is None:
        return None
    summary = pending.summary or "una compra"
    if pending.status == PENDING_STATUS_PROCESSING:
        return (
            f"⏳ La compra «{summary}» tiene un pago que se está acreditando justo ahora. Pídele al "
            "cliente que espere unos segundos; le confirmas en cuanto se procese. NO propongas una "
            "compra nueva ni generes otro link (interrumpirías la compra en curso)."
        )
    if pending.status == PENDING_STATUS_AWAITING_PAYMENT:
        link = pending.mp_init_point or "(link no disponible)"
        return (
            f"⚠️ Hay una compra pendiente de PAGO: «{summary}». El cliente debe pagar en el link ya "
            f"enviado: {link}. Si reporta un error o envía un comprobante, consulta get_my_purchase_status "
            "antes de indicarle que pague. Reenvía enlaces solo mediante respond_to_customer con kind=purchase, "
            "que verifica que la compra y su clase sigan vigentes. NO la confirmes por texto; "
            "se confirma sola al acreditarse el pago. Si quiere cancelar, usa cancel_action."
        )
    return (
        f"⚠️ Hay una acción PENDIENTE de confirmación: «{summary}». Si el cliente confirma "
        "(sí/ok/dale), llama a confirm_action AHORA; NO vuelvas a proponer ni a pedir más datos. "
        "Si rechaza o cambia de idea, usa cancel_action."
    )


def _build_member_note(member) -> Optional[str]:
    """Personalization line for the system prompt: greet by name + adapt to membership.

    ``member`` is the MemberData from ``membersCrud.get_member_by_id`` (or None). Returns
    None when there's no member/name so the prompt keeps its generic non-member branch.
    """
    if member is None:
        return None
    name = (getattr(member, "full_name", None) or "").strip()
    if not name:
        return None
    m = getattr(member, "active_membership", None)
    status = (getattr(m, "status", None) or "").strip().lower() if m is not None else ""
    days = getattr(m, "remaining_days", None) if m is not None else None
    end_at = getattr(m, "end_date", None) if m is not None else None

    if status == "active":
        detail = f"membresía ACTIVA, {days} días restantes" if days is not None else "membresía ACTIVA"
        if end_at is not None:
            detail = f"membresía ACTIVA hasta {fmt_dt(end_at)} (hora de México)"
        return (
            f"Socio identificado: {name}. {detail}. Salúdalo por su nombre y personaliza tu "
            "respuesta. Esto NO acredita una reserva para la fecha solicitada: verifica la sesión "
            "con las herramientas. Un pase diario anterior no cubre una nueva clase."
        )
    if status == "expired":
        detail = f"VENCIDA hace {abs(days)} días" if days is not None else "VENCIDA"
        if end_at is not None:
            detail = f"VENCIDA desde {fmt_dt(end_at)} (hora de México)"
        return (
            f"Socio identificado: {name}. Su membresía está {detail}. Salúdalo por su nombre y, "
            "con tacto, invítalo a renovar."
        )
    # inactive / desconocido / sin membresía activa
    return f"Socio identificado: {name}. No tiene una membresía activa. Salúdalo por su nombre."


async def _build_campaign_context_note(
    db: AsyncSession,
    context_message_id: Optional[str],
    message_type: str,
    user_text: str,
) -> Optional[str]:
    """Tell the agent which campaign/template this turn is replying to, when known.

    ``context_message_id`` is WhatsApp's ``context.id`` — populated both for a tap on a
    template QUICK_REPLY button (message_type "button"/"interactive") and for a plain text
    message the customer sent as a quoted/swipe-reply. Either way it matches
    ``CampaignRecipient.wa_message_id`` exactly (the id of the outbound template send), the
    same lookup ``campaignsCrud.apply_delivery_status`` already uses for Meta status callbacks.
    Without this, the agent only sees a generic "[mensaje automático/campaña]" tag on the
    OUTBOUND side of the history (see ``_load_history``) and has no idea which campaign or
    button prompted the customer's reply.
    """
    if not context_message_id:
        return None
    recipient = await campaigns_crud.get_recipient_by_wa_message_id(db, context_message_id)
    if recipient is None:
        return None
    campaign, template = await campaigns_crud.get_campaign_and_template(db, recipient.campaign_id)
    if campaign is None:
        return None
    label = template.template_name if template else "plantilla desconocida"
    if message_type in ("button", "interactive"):
        return (
            f"📣 El cliente tocó el botón \"{user_text}\" del mensaje de la campaña "
            f"\"{campaign.name}\" (plantilla {label}). Responde en función de esa intención "
            "específica en vez de una respuesta genérica."
        )
    return (
        f"📣 El cliente respondió citando el mensaje de la campaña \"{campaign.name}\" "
        f"(plantilla {label})."
    )


_EVENT_CAMPAIGN_RECENCY_DAYS = 14


def _button_intent(text: str) -> Optional[str]:
    """Map a campaign quick-reply label to an intent ("Quiero reservar" / "Más información")."""
    import unicodedata

    t = unicodedata.normalize("NFKD", (text or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    if "reserv" in t or "comprar" in t or "apartar" in t:
        return "reserve"
    if "inform" in t or t in ("info", "mas info", "detalles"):
        return "info"
    return None


async def _event_from_campaign_message(db: AsyncSession, context_message_id: Optional[str]):
    """The special event linked to the campaign message the customer replied to, if any."""
    if not context_message_id:
        return None
    recipient = await campaigns_crud.get_recipient_by_wa_message_id(db, context_message_id)
    if recipient is None:
        return None
    campaign, _template = await campaigns_crud.get_campaign_and_template(db, recipient.campaign_id)
    event_id = getattr(campaign, "special_event_id", None) if campaign else None
    if not event_id:
        return None
    from app.crud.special_events import events as special_events_crud

    return await special_events_crud.get_event(db, event_id)


async def _event_from_recent_campaign(db: AsyncSession, wa_id: Optional[str]):
    """The event promoted by the latest event campaign sent to this number (last 14 days).

    Keeps the event in context on the turns after the button tap (e.g. when the customer just
    answers with the participant's name), without depending on the campaign's wording.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, or_, select

    from app.crud.special_events import events as special_events_crud
    from app.crud.special_events.common import participant_key
    from app.models import Campaign, CampaignRecipient

    key = participant_key(wa_id)
    if not key:
        return None
    since = datetime.now(timezone.utc) - timedelta(days=_EVENT_CAMPAIGN_RECENCY_DAYS)
    wa_digits = func.right(
        func.regexp_replace(func.coalesce(CampaignRecipient.wa_id, ""), r"\D", "", "g"), 10
    )
    phone_digits = func.right(
        func.regexp_replace(func.coalesce(CampaignRecipient.phone_e164, ""), r"\D", "", "g"), 10
    )
    row = (
        await db.execute(
            select(Campaign.special_event_id)
            .join(CampaignRecipient, CampaignRecipient.campaign_id == Campaign.id)
            .where(
                Campaign.special_event_id.is_not(None),
                CampaignRecipient.sent_at.is_not(None),
                CampaignRecipient.sent_at >= since,
                or_(wa_digits == key, phone_digits == key),
            )
            .order_by(CampaignRecipient.sent_at.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return await special_events_crud.get_event(db, row[0])


async def _build_special_event_note(
    db: AsyncSession,
    conversation_id: int,
    wa_id: Optional[str],
    context_message_id: Optional[str],
    message_type: str,
    user_text: str,
    *, discovery=None,
) -> Optional[str]:
    """Structured special-event context for this turn: the event behind a campaign button, what
    the button means, and the customer's verified orders. All facts come from the database."""
    from datetime import datetime, timezone

    from app.crud.special_events import events as special_events_crud
    from app.crud.special_events import orders as special_event_orders_crud
    from app.crud.special_events.common import local_schedule_label, money_label
    from app.services.chatbot.tools import _order_status_es

    parts: List[str] = []
    discovery = discovery or await event_discovery.resolve(db, conversation_id, user_text)
    tapped_event = await _event_from_campaign_message(db, context_message_id)
    if discovery.interested:
        event = await special_events_crud.get_event(db, discovery.event_id) if discovery.event_id else None
        if event is None:
            parts.append(await event_discovery.selection_reply(db, discovery))
            parts.append("El cliente consulta un evento sin identificarlo de forma única. Pide elegir entre estas opciones; no lo conviertas en una clase regular ni elijas por tu cuenta.")
    elif discovery.source == "regular":
        event = None
    else:
        event = tapped_event or await _event_from_recent_campaign(db, wa_id)
    if event is not None and event.status != "draft":
        now = datetime.now(timezone.utc)
        if event.status == "published" and event.start_at > now:
            avail = await special_events_crud.availability(db, event, now=now)
            spots = (
                f"{avail.available} lugar(es) disponible(s)"
                if avail.sales_open
                else avail.sales_closed_reason or "sin lugares"
            )
            origin = discovery.source if discovery.interested else ("la campaña a la que respondió" if tapped_event else "una campaña reciente")
            parts.append(
                f"🎟️ Evento especial en contexto (de {origin}): #{event.id} «{event.name}» — "
                f"{local_schedule_label(event.start_at, event.end_at, event.timezone)} — "
                f"{money_label(event.price, event.currency)} por participante — {spots}. "
                f"Datos verificados del sistema; detalle con get_special_event({event.id})."
            )
            if discovery.interested:
                parts.append(f"No necesita haber recibido una plantilla. Para explicar este evento usa respond_to_customer(kind=event_information, event_id={event.id}). Si quiere comprar, solicita solo los datos faltantes e inicia start_event_purchase. No pidas fecha/horario de una clase regular.")
            if not event.payment_link_expires:
                parts.append("Este evento usa enlaces sin caducidad y SIN apartado de cupo; no prometas un lugar antes del pago verificado.")
            intent = (
                _button_intent(user_text)
                if tapped_event is not None and message_type in ("button", "interactive")
                else None
            )
            if intent == "reserve":
                parts.append(
                    f"El cliente tocó «{user_text}»: quiere comprar ESTE evento. Pide solo lo que "
                    "falte (el nombre completo del participante), confirma evento, fecha, horario y "
                    f"precio, y llama a start_event_purchase(event_id={event.id}). No ofrezcas "
                    "planes ni pase diario para esto."
                )
            elif intent == "info":
                parts.append(
                    f"El cliente tocó «{user_text}»: explícale el evento con "
                    f"get_special_event({event.id}) (qué incluye, fecha, horario, lugar, precio y "
                    "lugares) y ofrécele reservar su lugar."
                )
        elif tapped_event is not None:
            state = "fue cancelado" if event.status == "canceled" else "ya no está a la venta"
            parts.append(
                f"El cliente respondió a la campaña del evento «{event.name}», que {state}. "
                "Díselo con amabilidad y no intentes venderlo."
            )

    orders = await special_event_orders_crud.live_orders_for_conversation(
        db, conversation_id=conversation_id, wa_id=wa_id
    )
    for order in orders[:5]:
        if order.status not in ("awaiting_payment", "paid", "needs_review", "refund_pending"):
            continue
        order_event = await special_events_crud.get_event(db, order.event_id)
        parts.append(
            f"Compra de evento #{order.id} ({order.participant_name}, «{order_event.name}»): "
            f"{_order_status_es(order, order_event)}"
        )
    if any(o.status == "awaiting_payment" for o in orders):
        parts.append(
            "Si pregunta por su pago pendiente, consulta el estado y usa respond_to_customer(kind=purchase, order_id=...) "
            "para reenviar el mismo enlace. En enlaces sin caducidad verifica cupo antes de reenviarlo; no generes otro."
        )
    return "\n".join(parts) if parts else None


async def _run_agent_reply(
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    message_id: int,
    text: str,
    message_type: str = "text",
    context_message_id: Optional[str] = None,
) -> None:
    """Run the agent for one inbound message and reply. Uses its own DB session."""
    if not chatbot_env.is_configured() or not chatbot_env.ENABLED:
        return
    text = (text or "").strip()
    if not text and message_type == "image":
        text = image_context.IMAGE_PLACEHOLDER
    if not text:
        return
    try:
        async with async_session_factory() as db:
            config = await chatbotConfigCrud.get_config(db)
            if config is None or not config.enabled:
                return

            member_id = await membersCrud.get_member_id_by_wa_id(db, contact_wa_id)
            member = (
                await membersCrud.get_member_by_id(db, member_id)
                if member_id is not None
                else None
            )
            business_info = await build_business_info(db, config)
            current_image = None
            if message_type == "image":
                current_image = await image_context.current_image_blocks(
                    db, message_id=message_id, conversation_id=conversation_id, contact_id=contact_id,
                )
            history = await _load_history(db, conversation_id, exclude_message_id=message_id,
                                          context_message_id=context_message_id)
            mp_required = bool(config.require_mp_payment)
            require_mp = mp_required and mercadopago_config.is_configured()
            pending_note = await _build_pending_note(db, conversation_id)
            member_note = _build_member_note(member)
            member_note = (member_note or "") + "\n" + await reservation_context(db, member_id)
            campaign_note = await _build_campaign_context_note(
                db, context_message_id, message_type, text
            )
            discovery = event_discovery.EventDiscovery()
            try:
                discovery = await event_discovery.resolve(db, conversation_id, text, message_id=message_id)
                if discovery.interested:
                    logger.info("chatbot_event_discovery message=%s conversation=%s source=%s event=%s candidates=%s",
                                message_id, conversation_id, discovery.source, discovery.event_id, discovery.candidates)
                event_note = await _build_special_event_note(
                    db, conversation_id, contact_wa_id, context_message_id, message_type, text, discovery=discovery,
                )
            except Exception:  # noqa: BLE001 - extra context must never block the reply
                await db.rollback()
                logger.exception("special event note failed for conversation %s", conversation_id)
                event_note = None

            ctx = ChatbotContext(
                db=db,
                conversation_id=conversation_id,
                member_id=member_id,
                wa_id=contact_wa_id,
                require_mp_payment=require_mp,
                mp_required=mp_required,
                user_text=text,
                event_discovery=discovery,
            )
            tools = build_tools(ctx)
            if message_type == "image" and text == image_context.IMAGE_PLACEHOLDER:
                tools = image_context.read_only_tools(tools)
            turn_queue.mark_started("customer", conversation_id)
            visual_turn = current_image is not None or any(isinstance(m.content, list) for m in history)
            try:
                reply = await run_agent(
                    config, tools, business_info, member_id, history, text,
                    pending_note=pending_note, member_note=member_note,
                    campaign_note=campaign_note, event_note=event_note,
                    image_content=current_image,
                )
            except Exception as exc:  # noqa: BLE001 - provide a truthful response for unreadable images
                if not visual_turn and not discovery.interested:
                    raise
                logger.info("chatbot_model message=%s result=failed reason=%s event_context=%s", message_id, type(exc).__name__, discovery.interested)
                await db.rollback()
                ctx.reply_request = None
                ctx.payment_link = None
                ctx.validated_reply = image_context.IMAGE_FAILURE_REPLY if visual_turn else None
            # Never send unverified model output, even when it omitted the final tool.
            fallback = (
                "Necesito verificar la fecha y el horario antes de confirmar una reserva. "
                "¿Para qué fecha y hora deseas comprar tu clase?"
            )
            if visual_turn:
                fallback = "No pude interpretar la imagen con seguridad. ¿Puedes reenviarla o escribir el mensaje de error que aparece?"
            reply = ctx.validated_reply or fallback

            # The agent took time to run; re-check the bot wasn't disabled/paused meanwhile (STOP,
            # human takeover, or robot button off) so it doesn't talk over a human or reply right
            # after the customer asked to stop.
            from datetime import datetime

            conv = await crud.get_conversation(db, conversation_id)
            if conv is not None and (
                conv.bot_enabled is False
                or (conv.bot_paused_until is not None and conv.bot_paused_until > datetime.utcnow())
            ):
                logger.info(
                    "Chatbot reply suppressed: bot disabled/paused for conversation %s",
                    conversation_id,
                )
                return

            if ctx.reply_request:
                await finalize_reply(ctx, **ctx.reply_request)
                fallback = "No pude verificar esa reserva. Revisemos la fecha y el horario antes de confirmarla."
            if discovery.interested and not visual_turn and not ctx.validated_reply:
                fallback = await event_discovery.fallback_reply(db, discovery)
                ctx.verification_result = "event_fallback"
            reply = ctx.validated_reply or fallback

            logger.info("chatbot_turn conversation=%s tools=%s verification=%s", conversation_id, ctx.tools_used, ctx.verification_result)

            await send_and_persist_reply(
                db,
                conversation_id=conversation_id,
                contact_id=contact_id,
                to_wa_id=contact_wa_id,
                text=reply,
                payment_link=ctx.payment_link,
            )
    except cloud.WhatsAppError as e:
        # e.g. 131047 outside the 24h window — log, don't crash the loop.
        logger.warning("Chatbot reply send failed (%s): %s", e.code, e.message)
    except Exception:  # noqa: BLE001
        logger.exception("Chatbot agent reply failed for conversation %s", conversation_id)


def schedule_agent_reply(
    conversation_id: int,
    contact_id: int,
    contact_wa_id: str,
    message_id: int,
    text: str,
    message_type: str = "text",
    context_message_id: Optional[str] = None,
) -> None:
    """Fire-and-forget the agent reply on the running event loop (non-blocking webhook)."""
    try:
        task = turn_queue.schedule_turn(
            "customer", conversation_id, lambda: _run_agent_reply(
                conversation_id, contact_id, contact_wa_id, message_id, text,
                message_type=message_type, context_message_id=context_message_id,
            ),
        )
    except RuntimeError:
        # No running loop (e.g. called from a sync context outside the app) — skip.
        logger.warning("schedule_agent_reply called without a running event loop; skipped")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _run_text_send(
    conversation_id: int, contact_id: int, to_wa_id: str, text: str, kind: str
) -> None:
    """Background a single transactional text send in its own session (e.g. opt-out confirmation)."""
    try:
        async with async_session_factory() as db:
            await send_and_persist_reply(
                db,
                conversation_id=conversation_id,
                contact_id=contact_id,
                to_wa_id=to_wa_id,
                text=text,
                kind=kind,
            )
    except Exception:  # noqa: BLE001
        logger.exception("background text send failed for conversation %s", conversation_id)


def schedule_text_send(
    conversation_id: int,
    contact_id: int,
    to_wa_id: str,
    text: str,
    kind: str = outbound.KIND_CHATBOT_REPLY,
) -> None:
    """Fire-and-forget a transactional text send so the webhook/ingest path stays non-blocking."""
    try:
        task = asyncio.create_task(
            _run_text_send(conversation_id, contact_id, to_wa_id, text, kind)
        )
    except RuntimeError:
        logger.warning("schedule_text_send called without a running event loop; skipped")
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
