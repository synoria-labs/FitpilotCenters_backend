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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from langchain_core.tools import StructuredTool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import chatbotPendingCrud as pending_crud
from app.crud import membersCrud
from app.crud import reservationsCrud
from app.crud import usersCrud
from app.crud import venuesCrud
from app.crud.memberships import payments as payments_crud
from app.crud.memberships import plans as plans_crud
from app.crud.memberships import enrollment as enrollment_crud
from app.crud.standing_bookings import catalog as class_catalog
from app.models import Venue
from app.models.chatbotModel import (
    ACTION_CREATE_ENROLLMENT,
    ACTION_CREATE_PAYMENT,
    ACTION_CREATE_RESERVATION,
    ACTION_RENEW_SUBSCRIPTION,
    PENDING_STATUS_CONFIRMED,
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


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "?"
    return dt.strftime("%d/%m/%Y %H:%M")


def _to_decimal(value) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


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
        """Lista los planes de membresía disponibles con su precio y duración."""
        plans = await plans_crud.get_membership_plans(db)
        if not plans:
            return "No hay planes de membresía configurados."
        out = []
        for p in plans:
            desc = f" — {p.description}" if p.description else ""
            out.append(
                f"#{p.id} {p.name}: ${p.price:.2f} por {p.duration_value} {p.duration_unit}(s){desc}"
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
            out.append(f"Reserva #{r.id}: {name} — {_fmt_dt(r.session_start)}{seat} ({r.status})")
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

    # --------------------------- propose tools -----------------------------
    async def propose_reservation(session_id: int, seat_id: Optional[int] = None) -> str:
        """Propone reservar una clase para el socio (requiere confirmación). seat_id es opcional."""
        if ctx.member_id is None:
            return _NEEDS_MEMBER
        try:
            seats = await reservationsCrud.get_available_seats(db, int(session_id))
        except ValueError:
            return f"No encontré la clase #{session_id}."
        if seat_id is not None:
            match = next((s for s in seats if s.id == int(seat_id)), None)
            if match is None:
                return f"El asiento {seat_id} no existe en esa clase."
            if not match.is_available:
                return f"El asiento {seat_id} ya está ocupado."
        # Summary for the confirmation message.
        start = datetime.now(timezone.utc)
        sessions = await reservationsCrud.get_available_sessions(
            db, start_date=start, end_date=start + timedelta(days=60)
        )
        info = next((s for s in sessions if s.id == int(session_id)), None)
        when = _fmt_dt(info.start_at) if info else "la fecha programada"
        cls_name = (info.name or info.class_type_name) if info else f"clase #{session_id}"
        seat_txt = f", asiento {seat_id}" if seat_id is not None else ""
        summary = f"Reservar {cls_name} el {when}{seat_txt}"
        await pending_crud.upsert_pending(
            db,
            conversation_id=ctx.conversation_id,
            action_type=ACTION_CREATE_RESERVATION,
            payload={"session_id": int(session_id), "seat_id": int(seat_id) if seat_id is not None else None},
            member_id=ctx.member_id,
            summary=summary,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Pide al cliente que confirme."

    async def propose_payment(amount: float, method: str = "efectivo", subscription_id: Optional[int] = None) -> str:
        """Propone registrar un pago del socio (requiere confirmación)."""
        if ctx.member_id is None:
            return _NEEDS_MEMBER
        dec = _to_decimal(amount)
        if dec is None or dec <= 0:
            return "El monto del pago no es válido."
        summary = f"Registrar pago de ${dec:.2f} ({method})"
        await pending_crud.upsert_pending(
            db,
            conversation_id=ctx.conversation_id,
            action_type=ACTION_CREATE_PAYMENT,
            payload={
                "amount": str(dec),
                "method": method or "efectivo",
                "subscription_id": int(subscription_id) if subscription_id is not None else None,
            },
            member_id=ctx.member_id,
            summary=summary,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Pide al cliente que confirme."

    async def propose_renewal(plan_id: int) -> str:
        """Propone renovar la membresía del socio con un plan (requiere confirmación)."""
        if ctx.member_id is None:
            return _NEEDS_MEMBER
        plan = await plans_crud.get_membership_plan_by_id(db, int(plan_id))
        if plan is None:
            return f"No encontré el plan #{plan_id}."
        if plan.fixed_time_slot:
            return (
                f"El plan {plan.name} tiene horario fijo y requiere asignar un cupo recurrente; "
                "por favor pide al cliente que lo gestione con el staff del gimnasio."
            )
        summary = f"Renovar membresía con el plan {plan.name} (${plan.price:.2f})"
        await pending_crud.upsert_pending(
            db,
            conversation_id=ctx.conversation_id,
            action_type=ACTION_RENEW_SUBSCRIPTION,
            payload={"plan_id": int(plan_id)},
            member_id=ctx.member_id,
            summary=summary,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Pide al cliente que confirme."

    async def propose_enrollment(full_name: str, plan_id: int) -> str:
        """Propone inscribir a un cliente nuevo (no socio) con un plan (requiere confirmación)."""
        name = (full_name or "").strip()
        if not name:
            return "Necesito el nombre completo del cliente para inscribirlo."
        plan = await plans_crud.get_membership_plan_by_id(db, int(plan_id))
        if plan is None:
            return f"No encontré el plan #{plan_id}."
        summary = f"Inscribir a {name} con el plan {plan.name} (${plan.price:.2f})"
        await pending_crud.upsert_pending(
            db,
            conversation_id=ctx.conversation_id,
            action_type=ACTION_CREATE_ENROLLMENT,
            payload={"full_name": name, "plan_id": int(plan_id), "phone_number": ctx.wa_id},
            member_id=ctx.member_id,
            summary=summary,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Pide al cliente que confirme."

    # --------------------------- confirm / cancel --------------------------
    async def confirm_action() -> str:
        """Ejecuta la acción pendiente (reserva/pago/renovación/inscripción) tras la confirmación del cliente."""
        pending = await pending_crud.get_pending(db, ctx.conversation_id)
        if pending is None:
            return "No hay ninguna acción pendiente por confirmar."
        try:
            result = await _execute_pending(db, pending)
        except ValueError as e:
            return f"No se pudo completar: {e}"
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("Chatbot confirm_action failed (type=%s)", pending.action_type)
            return "Ocurrió un error al ejecutar la acción. Intenta de nuevo o contacta al staff."
        await pending_crud.mark_status(db, pending.id, PENDING_STATUS_CONFIRMED, commit=True)
        return result

    async def cancel_action() -> str:
        """Cancela la acción pendiente si el cliente no confirma."""
        canceled = await pending_crud.cancel_pending(db, ctx.conversation_id, commit=True)
        return "Acción cancelada." if canceled else "No había ninguna acción pendiente."

    factories = [
        (get_business_info, "get_business_info", "Información del negocio (nombre, dirección, horarios, teléfono, políticas)."),
        (get_membership_plans, "get_membership_plans", "Planes de membresía disponibles con precio y duración."),
        (list_available_classes, "list_available_classes", "Clases con cupo disponible en los próximos días (incluye el id de cada clase)."),
        (check_class_availability, "check_class_availability", "Asientos disponibles para una clase (session_id)."),
        (get_my_membership, "get_my_membership", "Estado de la membresía del cliente identificado."),
        (list_my_reservations, "list_my_reservations", "Próximas reservas del cliente identificado."),
        (get_weekly_schedule, "get_weekly_schedule", "Horario semanal recurrente de clases (día, hora, clase, sede, instructor)."),
        (get_venues, "get_venues", "Sedes del estudio: nombre, dirección y capacidad."),
        (list_instructors, "list_instructors", "Nombres de los instructores del estudio."),
        (propose_reservation, "propose_reservation", "Propone una reserva de clase (requiere confirmación)."),
        (propose_payment, "propose_payment", "Propone registrar un pago (requiere confirmación)."),
        (propose_renewal, "propose_renewal", "Propone renovar la membresía con un plan (requiere confirmación)."),
        (propose_enrollment, "propose_enrollment", "Propone inscribir a un cliente nuevo (requiere confirmación)."),
        (confirm_action, "confirm_action", "Ejecuta la acción pendiente tras la confirmación del cliente."),
        (cancel_action, "cancel_action", "Cancela la acción pendiente."),
    ]
    return [
        StructuredTool.from_function(coroutine=fn, name=name, description=desc)
        for (fn, name, desc) in factories
    ]


async def _execute_pending(db: AsyncSession, pending) -> str:
    """Run the real CRUD write for a confirmed pending action. Caller marks status."""
    payload = pending.payload or {}
    action = pending.action_type

    if action == ACTION_CREATE_RESERVATION:
        reservation = await reservationsCrud.create_reservation(
            db,
            session_id=int(payload["session_id"]),
            person_id=int(pending.member_id),
            seat_id=payload.get("seat_id"),
            source="whatsapp_bot",
            commit=True,
        )
        return f"¡Listo! Reserva #{reservation.id} confirmada."

    if action == ACTION_CREATE_PAYMENT:
        payment = await payments_crud.create_payment(
            db,
            person_id=int(pending.member_id),
            amount=Decimal(str(payload["amount"])),
            method=payload.get("method") or "efectivo",
            subscription_id=payload.get("subscription_id"),
            comment="WhatsApp bot",
            recorded_by=None,
            commit=True,
        )
        return f"¡Listo! Pago de ${float(payment.amount):.2f} registrado (folio #{payment.id})."

    if action == ACTION_RENEW_SUBSCRIPTION:
        # renew_subscription_with_standing_booking commits internally.
        subscription, payment, plan, _sb_id, _stats = (
            await enrollment_crud.renew_subscription_with_standing_booking(
                db,
                member_id=int(pending.member_id),
                plan_id=int(payload["plan_id"]),
                payment_method="efectivo",
                payment_comment="WhatsApp bot",
                recorded_by=None,
            )
        )
        return (
            f"¡Listo! Membresía renovada con el plan {plan.name}. "
            f"Vence {_fmt_dt(subscription.end_at)}."
        )

    if action == ACTION_CREATE_ENROLLMENT:
        person, subscription, payment, plan = await enrollment_crud.create_member_enrollment(
            db,
            full_name=payload["full_name"],
            phone_number=payload.get("phone_number"),
            plan_id=int(payload["plan_id"]),
            payment_method="efectivo",
            payment_comment="WhatsApp bot",
            recorded_by=None,
        )
        await db.commit()
        return (
            f"¡Bienvenido/a {person.full_name}! Inscripción lista con el plan {plan.name}. "
            f"Vence {_fmt_dt(subscription.end_at)}."
        )

    raise ValueError(f"Tipo de acción desconocido: {action}")
