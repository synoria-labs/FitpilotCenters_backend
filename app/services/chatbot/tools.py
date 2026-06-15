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
from dataclasses import dataclass
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
from app.crud.memberships import payments as payments_crud
from app.crud.memberships import plans as plans_crud
from app.crud.memberships import enrollment as enrollment_crud
from app.crud.memberships import subscriptions as subscriptions_crud
from app.crud.standing_bookings import catalog as class_catalog
from app.models import Venue
from app.services import mercadopago_service
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
    require_mp_payment: bool = False


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
            if p.duration_unit == "day":
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

    # --------------------------- propose (purchase) tools ------------------
    async def _finalize_proposal(
        *, action_type: str, payload: dict, summary: str, amount, description: str, payer_name=None
    ) -> str:
        """Store the pending action. If MercadoPago is required (and amount>0), create a payment
        link (status awaiting_payment) and return it; otherwise leave it pending for a 'Sí'."""
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
        if plan.duration_unit != "day":
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

    factories = [
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
    ]
    return [
        StructuredTool.from_function(coroutine=fn, name=name, description=desc)
        for (fn, name, desc) in factories
    ]


def _materialized_count(stats) -> str:
    created = stats.get("created_reservations") if isinstance(stats, dict) else None
    return f" Reservé {created} clase(s) de tu horario." if created else ""


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
                    f"{_materialized_count(stats)}")
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
                f"Vence {_fmt_dt(subscription.end_at)}.{_materialized_count(stats)}")

    if action == ACTION_BUY_DAY_PASS:
        plan_id = int(payload["plan_id"])
        session_id = int(payload["session_id"])
        seat_id = await _first_available_session_seat(db, session_id)
        if pending.member_id is not None:
            subscription = await subscriptions_crud.create_membership_subscription(
                db, person_id=int(pending.member_id), plan_id=plan_id, commit=False
            )
            await payments_crud.create_payment(
                db,
                person_id=int(pending.member_id),
                amount=amount_dec if amount_dec is not None else Decimal("0"),
                method=payment_method,
                subscription_id=subscription.id,
                provider=payment_provider,
                provider_payment_id=provider_payment_id,
                external_reference=external_reference,
                comment="WhatsApp bot pase diario",
                recorded_by=None,
                commit=False,
            )
            reservation = await reservationsCrud.create_reservation(
                db, session_id=session_id, person_id=int(pending.member_id),
                seat_id=seat_id, source="manual", commit=False,
            )
            await db.commit()
            return f"¡Listo! Pase diario activado y reserva #{reservation.id} confirmada."
        # New customer day pass -> 1-day enrollment + single reservation.
        person, _subscription, _payment, plan = await enrollment_crud.create_member_enrollment(
            db,
            full_name=full_name,
            phone_number=phone_number,
            plan_id=plan_id,
            payment_method=payment_method,
            payment_amount=amount_dec,
            payment_comment="WhatsApp bot pase diario",
            payment_provider=payment_provider,
            provider_payment_id=provider_payment_id,
            external_reference=external_reference,
            recorded_by=None,
        )
        reservation = await reservationsCrud.create_reservation(
            db, session_id=session_id, person_id=person.id,
            seat_id=seat_id, source="manual", commit=False,
        )
        await db.commit()
        return (f"¡Bienvenido/a {person.full_name}! Pase diario {plan.name} activado y "
                f"reserva #{reservation.id} confirmada.")

    raise ValueError(f"Tipo de acción desconocido: {action}")
