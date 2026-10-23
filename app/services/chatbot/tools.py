"""LangChain tools for the FitPilot WhatsApp chatbot agent.

Tools are built per turn (``build_tools(ctx)``) so they close over the live DB session and the
conversation's resolved ``member_id``. Member-specific tools act ONLY on ``ctx.member_id`` (the
id resolved from the conversation's wa_id), never an id the model/customer could supply — this
is the security boundary that stops a customer touching another account.

Writes follow a propose-and-confirm flow:

* ``propose_*`` tools validate the request (read-only checks) and store a pending action row,
  then ask the customer to confirm. They never execute the write.
* ``confirm_action`` executes the stored pending action with the real CRUD; ``cancel_action``
  voids it.

Tools never raise into the agent loop — on error they return a short Spanish string the model
can relay or react to.
"""
import logging
import uuid
from dataclasses import dataclass, field
from functools import wraps
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from langchain_core.tools import StructuredTool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mercadopago_config import mercadopago_config
from app.crud import chatbotPendingCrud as pending_crud
from app.crud import membersCrud
from app.crud import reservationsCrud
from app.crud import usersCrud
from app.crud import venuesCrud
from app.crud.memberships import plans as plans_crud
from app.crud.memberships import enrollment as enrollment_crud
from app.crud.memberships.day_passes import is_day_pass, purchase_day_pass
from app.crud.standing_bookings import catalog as class_catalog
from app.crud.special_events import events as special_events_crud
from app.crud.special_events import orders as special_event_orders_crud
from app.crud.special_events.common import (
    SpecialEventError,
    local_schedule_label,
    local_time_label,
    money_label,
)
from app.models import Venue
from app.models.specialEventModel import (
    EVENT_CANCELED,
    EVENT_DRAFT,
    EVENT_PUBLISHED,
    ORDER_AWAITING_PAYMENT,
)
from app.services import mercadopago_service
from app.services import special_event_payments
from app.services.chatbot.timefmt import fmt_dt as _fmt_dt
from app.models.chatbotModel import (
    ACTION_BUY_DAY_PASS,
    ACTION_BUY_PACKAGE,
    PENDING_STATUS_AWAITING_PAYMENT,
    PENDING_STATUS_CONFIRMED,
    PENDING_STATUS_PENDING,
)

logger = logging.getLogger(__name__)

_NEEDS_MEMBER = "Para esta acción necesito identificarte como socio. Aún no encuentro tu membresía con este número."

# ClassTemplate.weekday is encoded 0=Sunday .. 6=Saturday (see classModel.py).
_WEEKDAYS_ES = ["Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"]


@dataclass
class ChatbotContext:
    """Per-turn binding for the tools (one fresh DB session per conversation turn)."""

    db: AsyncSession
    conversation_id: int
    member_id: Optional[int]
    wa_id: Optional[str]
    # Effective switch: the business requires Mercado Pago AND it is configured.
    require_mp_payment: bool = False
    # The business requires Mercado Pago (chatbot_config). When it is required but the gateway
    # is not configured, purchases must be refused — never downgraded to "confirm by text".
    mp_required: bool = False
    user_text: str = ""
    validated_reply: Optional[str] = None
    verification_result: str = "not_called"
    tools_used: list = field(default_factory=list)
    reply_request: Optional[dict] = None


def _to_decimal(value) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


async def _first_available_template_seat(db: AsyncSession, template_id: int) -> Optional[int]:
    """First available seat for a template's next occurrence (None if the class has no seats)."""
    try:
        seats = await class_catalog.get_available_seats_for_template(db, int(template_id))
    except Exception:  # noqa: BLE001
        return None
    return next((s.id for s in seats if s.is_available), None)


async def _first_available_session_seat(db: AsyncSession, session_id: int) -> Optional[int]:
    """First available seat for a specific session (None if the class has no seats)."""
    try:
        seats = await reservationsCrud.get_available_seats(db, int(session_id))
    except Exception:  # noqa: BLE001
        return None
    return next((s.id for s in seats if s.is_available), None)


async def describe_special_event(db: AsyncSession, event) -> str:
    """Structured facts about an event, straight from the database (never from campaign text)."""
    from app.models import People

    avail = await special_events_crud.availability(db, event)
    address = event.address
    if not address:
        venue = await db.get(Venue, event.venue_id)
        address = venue.address if venue else None
    instructor = await db.get(People, event.instructor_id) if event.instructor_id else None
    lines = [
        f"Evento #{event.id}: «{event.name}» (evento especial, distinto de las clases regulares).",
        f"Cuándo: {local_schedule_label(event.start_at, event.end_at, event.timezone)} (hora de México).",
    ]
    if address:
        lines.append(f"Dónde: {address}")
    if instructor and instructor.full_name:
        lines.append(f"Instructora: {instructor.full_name}")
    if event.description:
        lines.append(f"Descripción: {event.description}")
    if event.includes:
        items = [i.strip() for i in event.includes.splitlines() if i.strip()]
        lines.append("Incluye: " + "; ".join(items))
    lines.append(f"Precio: {money_label(event.price, event.currency)} por participante.")
    if avail.sales_open:
        lines.append(f"Lugares disponibles: {avail.available}.")
    else:
        lines.append(f"No está a la venta ahora: {avail.sales_closed_reason}")
    lines.append(
        f"Al iniciar la compra el lugar se aparta {event.hold_minutes} minutos mientras se paga "
        "con el link de Mercado Pago (tarjeta o saldo de Mercado Pago). Comprar el evento no usa "
        "ni modifica la membresía; quien tenga clase regular a esa hora también puede comprarlo."
    )
    return "\n".join(lines)


def _order_status_es(order, event) -> str:
    """Customer-facing status of an event order. Only 'paid' is a confirmed ticket."""
    status = order.status
    if status == "paid":
        return "PAGADA y confirmada (verificado con Mercado Pago)."
    if status == ORDER_AWAITING_PAYMENT:
        expires = (
            local_time_label(order.hold_expires_at, event.timezone) if order.hold_expires_at else "?"
        )
        link = order.mp_init_point or "(link no disponible)"
        return f"pendiente de pago, apartada hasta las {expires}. Link: {link}"
    return {
        "expired": "vencida sin pago (el lugar se liberó).",
        "canceled": "cancelada.",
        "refund_pending": "en proceso de reembolso.",
        "refunded": "reembolsada.",
        "needs_review": "en revisión por el equipo (un asesor la contactará).",
    }.get(status, status)


def build_tools(ctx: ChatbotContext) -> List[StructuredTool]:
    db = ctx.db

    # ----------------------------- read tools ------------------------------
    async def get_business_info() -> str:
        """Devuelve la información del negocio: nombre, dirección, horarios, teléfono y políticas."""
        from app.crud import chatbotConfigCrud
        config = await chatbotConfigCrud.get_config(db)
        lines: List[str] = []
        if config:
            if config.business_name:
                lines.append(f"Negocio: {config.business_name}")
            address = config.address
            if not address:
                venue = (await db.execute(select(Venue).order_by(Venue.id).limit(1))).scalars().first()
                address = venue.address if venue else None
            if address:
                lines.append(f"Dirección: {address}")
            if config.operating_hours:
                lines.append(f"Horarios: {config.operating_hours}")
            if config.phone:
                lines.append(f"Teléfono: {config.phone}")
            if config.policies:
                lines.append(f"Políticas: {config.policies}")
            if config.extra_info:
                lines.append(config.extra_info)
        return "\n".join(lines) if lines else "No hay información de negocio configurada todavía."

    async def get_membership_plans() -> str:
        """Lista los planes disponibles con precio, duración y TIPO (paquete con horario fijo / pase diario / membresía)."""
        plans = await plans_crud.get_membership_plans(db)
        if not plans:
            return "No hay planes de membresía configurados."
        out = []
        for p in plans:
            if is_day_pass(p):
                tipo = "pase diario"
            elif p.fixed_time_slot:
                tipo = "paquete con horario fijo"
            else:
                tipo = "membresía"
            desc = f" — {p.description}" if p.description else ""
            out.append(
                f"#{p.id} {p.name} ({tipo}): ${p.price:.2f} por {p.duration_value} {p.duration_unit}(s){desc}"
            )
        return "\n".join(out)

    async def list_available_classes(days_ahead: int = 7) -> str:
        """Lista las clases con cupo disponible en los próximos ``days_ahead`` días (id, nombre, fecha, lugares libres)."""
        days_ahead = max(1, min(int(days_ahead or 7), 30))
        start = datetime.now(timezone.utc)
        end = start + timedelta(days=days_ahead)
        sessions = await reservationsCrud.get_available_sessions(db, start_date=start, end_date=end)
        if not sessions:
            return "No hay clases programadas en ese rango."
        out = []
        for s in sessions:
            name = s.name or s.class_type_name or "Clase"
            venue = f" en {s.venue_name}" if s.venue_name else ""
            out.append(
                f"#{s.id} {name}{venue} — {_fmt_dt(s.start_at)} — {s.available_spots} lugar(es) disponible(s)"
            )
        return "\n".join(out)

    async def check_class_availability(session_id: int) -> str:
        """Muestra los asientos/lugares disponibles para una clase (session_id)."""
        try:
            seats = await reservationsCrud.get_available_seats(db, int(session_id))
        except ValueError:
            return f"No encontré la clase #{session_id}."
        available = [s for s in seats if s.is_available]
        if not seats:
            return f"La clase #{session_id} no tiene asientos definidos (reserva sin asiento específico)."
        if not available:
            return f"La clase #{session_id} está llena."
        labels = ", ".join(f"{s.label} (id {s.id})" for s in available)
        return f"Asientos disponibles en la clase #{session_id}: {labels}"

    async def get_my_membership() -> str:
        """Devuelve el estado de la membresía del cliente identificado (plan, vencimiento, días restantes)."""
        if ctx.member_id is None:
            return "No encuentro una membresía asociada a este número. ¿Te gustaría inscribirte?"
        member = await membersCrud.get_member_by_id(db, ctx.member_id)
        if member is None:
            return "No encuentro tus datos de socio."
        m = member.active_membership
        if m is None:
            return f"{member.full_name}: no tienes una membresía activa en este momento."
        return (
            f"{member.full_name}: plan {m.plan_name or '?'}, estado {m.status}, "
            f"vence {_fmt_dt(m.end_date)} ({m.remaining_days if m.remaining_days is not None else '?'} días restantes)."
        )

    async def list_my_reservations() -> str:
        """Lista las próximas reservas del cliente identificado."""
        if ctx.member_id is None:
            return _NEEDS_MEMBER
        reservations = await reservationsCrud.get_person_reservations(
            db, ctx.member_id, include_past=False, include_canceled=False, limit=20
        )
        if not reservations:
            return "No tienes reservas próximas."
        out = []
        for r in reservations:
            name = r.session_name or "Clase"
            seat = f" asiento {r.seat_label}" if r.seat_label else ""
            status = (
                "clase reemplazada por un evento especial; el equipo le dará seguimiento"
                if r.status == "displaced"
                else r.status
            )
            out.append(f"Reserva #{r.id} (session_id={r.session_id}): {name} — {_fmt_dt(r.session_start)}{seat} ({status})")
        return "\n".join(out)

    async def get_weekly_schedule() -> str:
        """Horario semanal recurrente de clases (día, hora, tipo de clase, sede e instructor)."""
        templates = await class_catalog.get_class_templates(db, active_only=True)
        if not templates:
            return "Aún no hay un horario de clases configurado."
        by_day: dict = {}
        for t in templates:
            by_day.setdefault(t.weekday, []).append(t)
        lines: List[str] = []
        for wd in sorted(by_day.keys()):
            day = _WEEKDAYS_ES[wd] if 0 <= wd < 7 else f"Día {wd}"
            lines.append(f"*{day}*")
            for t in sorted(by_day[wd], key=lambda x: x.start_time_local or ""):
                hhmm = (t.start_time_local or "")[:5]
                cls = t.class_type_name or t.name or "Clase"
                venue = f" en {t.venue_name}" if t.venue_name else ""
                instr = f" (con {t.instructor_name})" if t.instructor_name else ""
                lines.append(f"  {hhmm} — {cls}{venue}{instr}")
        return "\n".join(lines)

    async def get_venues() -> str:
        """Sedes del estudio: nombre, dirección y capacidad."""
        venues = await venuesCrud.list_venues(db)
        if not venues:
            return "No hay sedes configuradas."
        out = []
        for v in venues:
            addr = f" — {v.address}" if v.address else ""
            desc = f" ({v.description})" if v.description else ""
            out.append(f"{v.name}{addr} — capacidad {v.capacity}{desc}")
        return "\n".join(out)

    async def list_instructors() -> str:
        """Nombres de los instructores del estudio."""
        people = await usersCrud.list_people(db, role_code="instructor")
        names = sorted({p.full_name for p in people if p.full_name})
        if not names:
            return "No hay instructores registrados."
        return "Instructores: " + ", ".join(names)

    # --------------------------- propose (purchase) tools ------------------
    async def _finalize_proposal(
        *, action_type: str, payload: dict, summary: str, amount, description: str, payer_name=None
    ) -> str:
        """Store the pending action. If MercadoPago is required (and amount>0), create a payment
        link (status awaiting_payment) and return it; otherwise leave it pending for a 'Sí'."""
        if ctx.mp_required and not ctx.require_mp_payment and amount and float(amount) > 0:
            # Required but not configured: say so. Falling back to a text confirmation would
            # record a COMPLETED payment nobody collected.
            logger.error("Chatbot purchase refused: Mercado Pago required but not configured")
            return ("No puedo generar el link de pago en este momento: el cobro en línea no está "
                    "disponible. Dile al cliente que un asesor lo contactará para completar la "
                    "compra; NO la confirmes ni la registres por texto.")
        if ctx.require_mp_payment and amount and float(amount) > 0:
            external_reference = f"chatbot-{uuid.uuid4().hex}"
            try:
                mp = await mercadopago_service.create_preference(
                    amount=amount,
                    description=description,
                    external_reference=external_reference,
                    payer_name=payer_name,
                )
            except Exception:  # noqa: BLE001
                logger.exception("MercadoPago preference failed")
                return ("No pude generar el link de pago en este momento. "
                        "Intenta más tarde o contacta al staff.")
            await pending_crud.upsert_pending(
                db,
                conversation_id=ctx.conversation_id,
                action_type=action_type,
                payload=payload,
                member_id=ctx.member_id,
                summary=summary,
                status=PENDING_STATUS_AWAITING_PAYMENT,
                external_reference=external_reference,
                mp_preference_id=mp.get("preference_id"),
                mp_init_point=mp.get("init_point"),
                ttl_minutes=120,
                commit=True,
            )
            return (f"{summary}.\nPara confirmar, realiza el pago aquí: {mp['init_point']}\n"
                    "En cuanto se acredite el pago te confirmo automáticamente. (No confirmes por texto.)")
        await pending_crud.upsert_pending(
            db,
            conversation_id=ctx.conversation_id,
            action_type=action_type,
            payload=payload,
            member_id=ctx.member_id,
            summary=summary,
            status=PENDING_STATUS_PENDING,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Pide al cliente que confirme respondiendo *Sí*."

    async def propose_membership(plan_id: int, template_id: int, full_name: Optional[str] = None) -> str:
        """Propone comprar un PAQUETE: un plan con horario fijo que reserva automáticamente el
        horario elegido para todo el periodo. Para socio existente es renovación; para cliente nuevo
        es inscripción (requiere full_name). Requiere plan_id (plan de horario fijo) y template_id
        (un horario de get_weekly_schedule)."""
        plan = await plans_crud.get_membership_plan_by_id(db, int(plan_id))
        if plan is None:
            return f"No encontré el plan #{plan_id}."
        if not plan.fixed_time_slot:
            return (f"El plan {plan.name} no es de horario fijo. Para asistir un solo día usa el "
                    "pase diario (propose_day_pass).")
        templates = await class_catalog.get_class_templates(db, active_only=True)
        tinfo = next((t for t in templates if t.id == int(template_id)), None)
        if tinfo is None:
            return ("No encontré ese horario. Muéstrale los horarios con get_weekly_schedule y pide "
                    "que elija uno.")
        name = (full_name or "").strip()
        if ctx.member_id is None and not name:
            return "Necesito el nombre completo del cliente para inscribirlo. Pídeselo."
        seat = await _first_available_template_seat(db, int(template_id))
        # If the class uses seats and none is free for the next occurrence, warn.
        try:
            tseats = await class_catalog.get_available_seats_for_template(db, int(template_id))
        except Exception:  # noqa: BLE001
            tseats = []
        if tseats and seat is None:
            day0 = _WEEKDAYS_ES[tinfo.weekday] if 0 <= tinfo.weekday < 7 else ""
            return (f"El horario {day0} {(tinfo.start_time_local or '')[:5]} está lleno por ahora. "
                    "Ofrécele otro horario.")
        day = _WEEKDAYS_ES[tinfo.weekday] if 0 <= tinfo.weekday < 7 else ""
        hhmm = (tinfo.start_time_local or "")[:5]
        cls = tinfo.class_type_name or tinfo.name or "clase"
        action_word = "Renovar" if ctx.member_id is not None else f"Inscribir a {name}"
        summary = (f"{action_word} con el plan {plan.name} (${plan.price:.2f}), horario {day} {hhmm} "
                   f"de {cls} — se reservan todas las clases del periodo")
        payload = {
            "plan_id": int(plan_id),
            "template_id": int(template_id),
            "full_name": name or None,
            "phone_number": ctx.wa_id,
            "amount": str(plan.price),
        }
        return await _finalize_proposal(
            action_type=ACTION_BUY_PACKAGE, payload=payload, summary=summary,
            amount=plan.price, description=f"{plan.name} - FitPilot", payer_name=name or None,
        )

    async def propose_day_pass(plan_id: int, session_id: int, full_name: Optional[str] = None) -> str:
        """Propone un PASE DIARIO (1 día): el cliente paga un plan diario y reserva UNA clase
        específica. Requiere plan_id (plan con duración en días) y session_id (de list_available_classes).
        Para cliente nuevo requiere full_name."""
        plan = await plans_crud.get_membership_plan_by_id(db, int(plan_id))
        if plan is None:
            return f"No encontré el plan #{plan_id}."
        if not is_day_pass(plan):
            return (f"El plan {plan.name} no es un pase diario. Para asistir varios días con horario "
                    "fijo usa propose_membership.")
        try:
            seats = await reservationsCrud.get_available_seats(db, int(session_id))
        except ValueError:
            return ("No encontré esa clase. Muéstrale las clases con list_available_classes y pide "
                    "que elija una.")
        start = datetime.now(timezone.utc)
        sessions = await reservationsCrud.get_available_sessions(
            db, start_date=start, end_date=start + timedelta(days=60)
        )
        sinfo = next((s for s in sessions if s.id == int(session_id)), None)
        if sinfo is None:
            return "Esa clase no está disponible para reservar. Ofrécele otra."
        from app.services.chatbot.verification import requested_session_error
        mismatch = await requested_session_error(db, sinfo, ctx.user_text)
        if mismatch:
            return mismatch
        if (sinfo.available_spots is not None and sinfo.available_spots <= 0) or (
            seats and not any(s.is_available for s in seats)
        ):
            return f"La clase {_fmt_dt(sinfo.start_at)} ya no tiene cupo. Ofrécele otra."
        name = (full_name or "").strip()
        if ctx.member_id is None and not name:
            return "Necesito el nombre completo del cliente para el pase diario. Pídeselo."
        when = _fmt_dt(sinfo.start_at)
        cls = sinfo.name or sinfo.class_type_name or "clase"
        action_word = "Comprar" if ctx.member_id is not None else f"Inscribir a {name} con"
        summary = (f"{action_word} pase diario {plan.name} (${plan.price:.2f}) y reservar {cls} "
                   f"el {when}")
        payload = {
            "plan_id": int(plan_id),
            "session_id": int(session_id),
            "full_name": name or None,
            "phone_number": ctx.wa_id,
            "amount": str(plan.price),
        }
        return await _finalize_proposal(
            action_type=ACTION_BUY_DAY_PASS, payload=payload, summary=summary,
            amount=plan.price, description=f"Pase diario {plan.name} - FitPilot", payer_name=name or None,
        )

    # --------------------------- confirm / cancel --------------------------
    async def confirm_action() -> str:
        """Ejecuta la acción pendiente (reserva/pago/renovación/inscripción) tras la confirmación del cliente."""
        pending = await pending_crud.get_pending(db, ctx.conversation_id)
        if pending is None:
            return "No hay ninguna acción pendiente por confirmar."
        # Capture before any rollback: rolled-back ORM attributes lazy-load (sync IO -> MissingGreenlet).
        pending_id = pending.id
        action_type = pending.action_type
        try:
            result = await _execute_pending(db, pending)
        except ValueError as e:
            await db.rollback()
            return f"No se pudo completar: {e}"
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("Chatbot confirm_action failed (type=%s)", action_type)
            return "Ocurrió un error al ejecutar la acción. Intenta de nuevo o contacta al staff."
        await pending_crud.mark_status(db, pending_id, PENDING_STATUS_CONFIRMED, commit=True)
        return result

    async def cancel_action() -> str:
        """Cancela la acción pendiente si el cliente no confirma."""
        canceled = await pending_crud.cancel_pending(db, ctx.conversation_id, commit=True)
        return "Acción cancelada." if canceled else "No había ninguna acción pendiente."

    # --------------------------- special events ----------------------------
    async def list_special_events() -> str:
        """Eventos especiales a la venta (fechas únicas con precio propio). No son clases regulares."""
        events = await special_events_crud.list_events(db, statuses=[EVENT_PUBLISHED])
        now = datetime.now(timezone.utc)
        lines: List[str] = []
        for event in events:
            if event.start_at <= now:
                continue
            avail = await special_events_crud.availability(db, event, now=now)
            spots = (
                f"{avail.available} lugar(es) disponible(s)"
                if avail.sales_open
                else (avail.sales_closed_reason or "sin lugares")
            )
            lines.append(
                f"#{event.id} «{event.name}» — "
                f"{local_schedule_label(event.start_at, event.end_at, event.timezone)} — "
                f"{money_label(event.price, event.currency)} por participante — {spots}"
            )
        return "\n".join(lines) if lines else "No hay eventos especiales a la venta en este momento."

    async def get_special_event(event_id: int) -> str:
        """Detalle verificado de un evento especial: fecha, horario, lugar, instructora, qué incluye,
        precio, lugares disponibles y cómo se aparta el lugar."""
        event = await special_events_crud.get_event(db, int(event_id))
        if event is None or event.status == EVENT_DRAFT:
            return "No encontré ese evento. Usa list_special_events."
        if event.status == EVENT_CANCELED:
            return f"El evento «{event.name}» fue cancelado; ya no está a la venta."
        return await describe_special_event(db, event)

    async def start_event_purchase(
        event_id: int,
        participant_name: Optional[str] = None,
        participant_phone: Optional[str] = None,
    ) -> str:
        """Inicia la compra de UNA entrada a un evento especial: aparta un lugar y genera el link de
        pago de Mercado Pago. Requiere el nombre completo del participante (si es el socio
        identificado comprando para sí, puede omitirse). El teléfono por defecto es el del cliente."""
        name = (participant_name or "").strip()
        if not name and ctx.member_id is not None:
            member = await membersCrud.get_member_by_id(db, ctx.member_id)
            name = (getattr(member, "full_name", None) or "").strip()
        if not name:
            return "Necesito el nombre completo del participante. Pídeselo antes de generar el pago."
        phone = (participant_phone or "").strip() or ctx.wa_id
        result = await special_event_payments.start_whatsapp_purchase(
            db,
            event_id=int(event_id),
            participant_name=name,
            participant_phone=phone,
            conversation_id=ctx.conversation_id,
            wa_id=ctx.wa_id,
        )
        if not result.ok:
            return f"No se pudo apartar el lugar: {result.message}"
        if result.already_paid:
            return f"{result.message} No generes otro pago."
        event = await special_events_crud.get_event(db, int(event_id))
        expires = (
            local_time_label(result.hold_expires_at, event.timezone)
            if result.hold_expires_at
            else "?"
        )
        return (
            f"Lugar APARTADO (todavía no pagado) para {name} en «{event.name}» "
            f"({local_schedule_label(event.start_at, event.end_at, event.timezone)}), "
            f"{money_label(event.price, event.currency)}. El apartado vence a las {expires} "
            f"(hora de México). Link de pago de Mercado Pago: {result.init_point}\n"
            "Envíale este link tal cual. La entrada NO está confirmada hasta que Mercado Pago "
            "acredite el pago; la confirmación le llegará sola por WhatsApp. No la confirmes tú."
        )

    async def check_event_order_status() -> str:
        """Estado VERIFICADO de las compras de eventos especiales de este cliente (pagada,
        pendiente de pago, vencida, reembolsada...). Úsalo antes de afirmar que algo está pagado."""
        orders = await special_event_orders_crud.live_orders_for_conversation(
            db, conversation_id=ctx.conversation_id, wa_id=ctx.wa_id
        )
        if not orders:
            return "Este cliente no tiene compras de eventos especiales."
        lines: List[str] = []
        for order in orders:
            event = await special_events_crud.get_event(db, order.event_id)
            lines.append(
                f"Compra #{order.id} — {order.participant_name} — «{event.name}»: "
                f"{_order_status_es(order, event)}"
            )
        return "\n".join(lines)

    async def cancel_event_order(order_id: Optional[int] = None) -> str:
        """Cancela una compra de evento especial que AÚN NO se ha pagado (libera el lugar)."""
        orders = await special_event_orders_crud.live_orders_for_conversation(
            db, conversation_id=ctx.conversation_id, wa_id=ctx.wa_id
        )
        targets = [
            o for o in orders
            if o.status == ORDER_AWAITING_PAYMENT and (order_id is None or o.id == int(order_id))
        ]
        if not targets:
            return "No hay compras de eventos pendientes de pago que cancelar."
        preferences: List[str] = []
        for order in targets:
            try:
                await special_event_orders_crud.cancel_order(
                    db, order.id, actor_account_id=None, reason="Cancelado por el cliente"
                )
                if order.mp_preference_id:
                    preferences.append(order.mp_preference_id)
            except SpecialEventError as exc:
                await db.rollback()
                return f"No se pudo cancelar: {exc}"
        await db.commit()
        await special_event_payments.expire_payment_links(preferences)
        return f"Listo, cancelé {len(targets)} compra(s) pendiente(s) de pago; el lugar quedó libre."

    async def respond_to_customer(
        kind: str, text: str = "", session_id: Optional[int] = None,
        reservation_id: Optional[int] = None, requested_date: Optional[str] = None,
        order_id: Optional[int] = None,
    ) -> str:
        """Finaliza TODA respuesta. kind=information|purchase|clarification|reservation_confirmation|event_confirmation.

        Para confirmar una reserva: session_id, reservation_id y requested_date YYYY-MM-DD son
        obligatorios. El servidor verifica y redacta la confirmación; text solo se usa para otros tipos.
        Si rechaza, corrige la compra o pide aclaración y vuelve a llamar esta herramienta.
        Para confirmar entradas pagadas de eventos usa event_confirmation con order_id.
        Para enviar el link de un evento usa purchase con order_id.
        """
        from app.services.chatbot.verification import finalize_reply
        try:
            if ctx.member_id is None and ctx.wa_id:
                ctx.member_id = await membersCrud.get_member_id_by_wa_id(db, ctx.wa_id)
            return await finalize_reply(
                ctx, kind=kind, text=text, session_id=session_id,
                reservation_id=reservation_id, requested_date=requested_date,
                order_id=order_id,
            )
        except (ValueError, TypeError):
            ctx.validated_reply = None
            ctx.reply_request = None
            return "Fecha o identificadores inválidos. Aclara los datos antes de responder."

    factories = [
        (respond_to_customer, "respond_to_customer", respond_to_customer.__doc__),
        (get_business_info, "get_business_info", "Información del negocio (nombre, dirección, horarios, teléfono, políticas)."),
        (get_membership_plans, "get_membership_plans", "Planes de membresía disponibles con precio y duración."),
        (list_available_classes, "list_available_classes", "Clases con cupo disponible en los próximos días (incluye el id de cada clase)."),
        (check_class_availability, "check_class_availability", "Asientos disponibles para una clase (session_id)."),
        (get_my_membership, "get_my_membership", "Estado de la membresía del cliente identificado."),
        (list_my_reservations, "list_my_reservations", "Próximas reservas del cliente identificado."),
        (get_weekly_schedule, "get_weekly_schedule", "Horario semanal recurrente de clases (día, hora, clase, sede, instructor). Da el id de cada horario (template) para propose_membership."),
        (get_venues, "get_venues", "Sedes del estudio: nombre, dirección y capacidad."),
        (list_instructors, "list_instructors", "Nombres de los instructores del estudio."),
        (propose_membership, "propose_membership", "Propone comprar un PAQUETE (plan de horario fijo): inscribe/renueva y auto-reserva el horario elegido todo el periodo. Args: plan_id, template_id, full_name (si es cliente nuevo)."),
        (propose_day_pass, "propose_day_pass", "Propone un PASE DIARIO (1 día): paga un plan diario y reserva una clase específica. Args: plan_id, session_id, full_name (si es cliente nuevo)."),
        (confirm_action, "confirm_action", "Ejecuta la compra pendiente tras la confirmación del cliente (solo cuando el pago no es por MercadoPago)."),
        (cancel_action, "cancel_action", "Cancela la acción pendiente."),
        (list_special_events, "list_special_events", "Eventos especiales a la venta (fecha, horario, precio y lugares). No son clases regulares ni membresías."),
        (get_special_event, "get_special_event", "Detalle verificado de un evento especial (qué incluye, lugar, instructora, precio, disponibilidad). Arg: event_id."),
        (start_event_purchase, "start_event_purchase", "Aparta un lugar en un evento especial y genera el link de pago de Mercado Pago. Args: event_id, participant_name (nombre completo del participante), participant_phone (opcional)."),
        (check_event_order_status, "check_event_order_status", "Estado verificado de las compras de eventos del cliente. Úsalo antes de decir que una entrada está pagada."),
        (cancel_event_order, "cancel_event_order", "Cancela una compra de evento aún no pagada. Arg opcional: order_id."),
    ]
    def audited(fn, name):
        @wraps(fn)
        async def invoke(*args, **kwargs):
            ctx.tools_used.append(name)
            result = await fn(*args, **kwargs)
            logger.info("chatbot_tool conversation=%s tool=%s verification=%s", ctx.conversation_id, name, ctx.verification_result)
            return result
        return invoke

    return [
        StructuredTool.from_function(coroutine=audited(fn, name), name=name, description=desc)
        for (fn, name, desc) in factories
    ]


def _materialized_count(stats) -> str:
    created = stats.get("created_reservations") if isinstance(stats, dict) else None
    return f" Reservé {created} clase(s) de tu horario." if created else ""


def _assigned_seat_note(stats) -> str:
    """Tell the customer which bike was assigned (it may differ from the one first offered if it
    got taken before the payment acreditó and we auto-reassigned a free one)."""
    label = stats.get("assigned_seat_label") if isinstance(stats, dict) else None
    return f" Tu lugar asignado: {label}." if label else ""


async def _execute_pending(
    db: AsyncSession,
    pending,
    *,
    payment_method: str = "efectivo",
    payment_provider: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    external_reference: Optional[str] = None,
) -> str:
    """Run the real CRUD for a confirmed/paid purchase. Caller marks the status.

    For a MercadoPago confirmation the caller passes ``payment_method="mercadopago"`` + the
    provider id + external_reference so the Payment row records the gateway transaction.
    """
    payload = pending.payload or {}
    action = pending.action_type
    amount = payload.get("amount")
    amount_dec = Decimal(str(amount)) if amount is not None else None
    full_name = (payload.get("full_name") or "Cliente").strip() or "Cliente"
    phone_number = payload.get("phone_number")

    if action == ACTION_BUY_PACKAGE:
        plan_id = int(payload["plan_id"])
        template_id = int(payload["template_id"])
        seat_id = await _first_available_template_seat(db, template_id)
        if pending.member_id is not None:
            # renew_subscription_with_standing_booking commits internally.
            subscription, _payment, plan, _sb, stats = (
                await enrollment_crud.renew_subscription_with_standing_booking(
                    db,
                    member_id=int(pending.member_id),
                    plan_id=plan_id,
                    template_id=template_id,
                    seat_id=seat_id,
                    payment_method=payment_method,
                    payment_amount=amount_dec,
                    payment_comment="WhatsApp bot",
                    payment_provider=payment_provider,
                    provider_payment_id=provider_payment_id,
                    external_reference=external_reference,
                    recorded_by=None,
                )
            )
            return (f"¡Listo! Membresía {plan.name} activada. Vence {_fmt_dt(subscription.end_at)}."
                    f"{_materialized_count(stats)}{_assigned_seat_note(stats)}")
        # New customer -> enrollment with standing booking (commits internally).
        person, subscription, _payment, plan, _sb, stats = (
            await enrollment_crud.create_member_enrollment_with_standing_booking(
                db,
                full_name=full_name,
                plan_id=plan_id,
                template_id=template_id,
                seat_id=seat_id,
                phone_number=phone_number,
                payment_method=payment_method,
                payment_amount=amount_dec,
                payment_comment="WhatsApp bot",
                payment_provider=payment_provider,
                provider_payment_id=provider_payment_id,
                external_reference=external_reference,
                recorded_by=None,
            )
        )
        return (f"¡Bienvenido/a {person.full_name}! Inscripción lista con {plan.name}. "
                f"Vence {_fmt_dt(subscription.end_at)}.{_materialized_count(stats)}"
                f"{_assigned_seat_note(stats)}")

    if action == ACTION_BUY_DAY_PASS:
        purchase = await purchase_day_pass(
            db, plan_id=int(payload["plan_id"]), session_id=int(payload["session_id"]),
            person_id=pending.member_id, full_name=full_name, phone_number=phone_number,
            payment_method=payment_method, payment_amount=amount_dec,
            payment_comment="WhatsApp bot pase diario", payment_provider=payment_provider,
            provider_payment_id=provider_payment_id, external_reference=external_reference,
        )
        await db.commit()
        from app.models import ClassSession
        session = await db.get(ClassSession, purchase.subscription.session_id, populate_existing=True)
        if (session.status != "scheduled" or session.end_at <= datetime.now(timezone.utc)
                or purchase.reservation.status not in ("reserved", "checked_in")):
            return (
                f"El pago ya está registrado para la clase del {_fmt_dt(session.start_at)}. "
                "La reserva requiere revisión; un asesor verificará su estado."
            )
        return (
            f"Pase diario {purchase.plan.name} activado. Reserva #{purchase.reservation.id} "
            f"confirmada para {_fmt_dt(session.start_at)}. "
            f"El pase vence al terminar esa clase: {_fmt_dt(session.end_at)}."
        )

    raise ValueError(f"Tipo de acción desconocido: {action}")
