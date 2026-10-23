"""GraphQL mutations for special events.

Every resolver requires ``manage_special_events`` (checked against the database, see
``require_capability``) and records the acting account on what it changes: who created,
published or canceled an event, who applied or undid a takeover, who registered a follow-up.
Each one answers with the recomputed event detail.
"""
import logging
from datetime import time
from typing import Awaitable, Callable, List, Optional

import strawberry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.core.timezones import STUDIO_TIMEZONE
from app.crud.permissions import MANAGE_SPECIAL_EVENTS
from app.crud.special_events import bonus as bonus_crud
from app.crud.special_events import events as events_crud
from app.crud.special_events import orders as orders_crud
from app.crud.special_events import takeover as takeover_crud
from app.crud.special_events.common import SpecialEventError
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.special_events.builders import build_detail, build_displacement
from app.graphql.special_events.types import (
    AddParticipantInput,
    DisplacementFollowUpInput,
    DisplacementResult,
    SpecialEventInput,
    SpecialEventResult,
)
from app.models import Campaign
from app.services import special_event_payments

logger = logging.getLogger(__name__)


def _parse_time(value: Optional[str]) -> Optional[time]:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = time.fromisoformat(str(value).strip())
    except ValueError:
        raise SpecialEventError("Hora no válida; usa el formato HH:MM.") from None
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _integrity_message(exc: IntegrityError) -> str:
    text = str(getattr(exc, "orig", exc))
    if "ex_special_event" in text:
        return (
            "Ya existe otro evento activo en esa sede o con esa instructora en ese horario. "
            "Dos eventos no pueden usar los mismos recursos al mismo tiempo."
        )
    if "uq_special_event_order_seat" in text:
        return "Esa bicicleta ya está apartada o vendida para este evento."
    if "uq_special_event_order_participant" in text:
        return "Ese participante ya tiene un lugar apartado o pagado en este evento."
    if "uq_special_event_takeover_active_session" in text:
        return "Esa clase ya está sustituida por un evento."
    return "La operación choca con otro registro. Actualiza la pantalla e intenta de nuevo."


async def _attempt(
    db: AsyncSession, action: Callable[[], Awaitable[int]], message: str
) -> SpecialEventResult:
    try:
        event_id = await action()
        await db.commit()
    except SpecialEventError as exc:
        await db.rollback()
        return SpecialEventResult(success=False, error=str(exc))
    except IntegrityError as exc:
        await db.rollback()
        return SpecialEventResult(success=False, error=_integrity_message(exc))
    except Exception:  # noqa: BLE001
        await db.rollback()
        logger.exception("Special event mutation failed")
        return SpecialEventResult(
            success=False, error="No se pudo completar la operación. Intenta de nuevo."
        )
    detail = await build_detail(db, event_id) if event_id is not None else None
    return SpecialEventResult(success=True, message=message, detail=detail)


def _actor(info: Info) -> Optional[int]:
    return getattr(info.context, "account_id", None)


async def _require_event(db: AsyncSession, event_id: int):
    event = await events_crud.get_event(db, event_id)
    if event is None:
        raise SpecialEventError("No encontré ese evento.")
    return event


@strawberry.type
class SpecialEventsMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_special_event(self, info: Info, input: SpecialEventInput) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            missing = [
                label
                for label, value in (
                    ("nombre", input.name),
                    ("sede", input.venue_id),
                    ("fecha", input.local_date),
                    ("hora de inicio", input.start_time),
                    ("precio", input.price),
                )
                if value in (None, "")
            ]
            if missing:
                raise SpecialEventError("Faltan datos: " + ", ".join(missing) + ".")
            event = await events_crud.create_event(
                db,
                actor_account_id=_actor(info),
                name=input.name,
                venue_id=input.venue_id,
                local_day=input.local_date,
                start_time=_parse_time(input.start_time),
                end_time=_parse_time(input.end_time),
                price=input.price,
                description=input.description,
                includes=input.includes,
                address=input.address,
                class_type_id=input.class_type_id,
                instructor_id=input.instructor_id,
                timezone_name=input.timezone or STUDIO_TIMEZONE,
                capacity=input.capacity,
                seat_ids=input.seat_ids,
                hold_minutes=input.hold_minutes or 30,
            )
            return event.id

        return await _attempt(db, action, "Evento creado como borrador.")

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_special_event(
        self, info: Info, id: int, input: SpecialEventInput
    ) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            event = await _require_event(db, id)
            changes = {}
            for field in (
                "name", "description", "includes", "address", "class_type_id",
                "instructor_id", "venue_id", "price", "capacity", "seat_ids", "hold_minutes",
            ):
                value = getattr(input, field)
                if value is not None:
                    changes[field] = value
            if input.timezone is not None:
                changes["timezone_name"] = input.timezone
            if input.local_date is not None:
                changes["local_day"] = input.local_date
            if input.start_time is not None:
                changes["start_time"] = _parse_time(input.start_time)
            if input.end_time is not None:
                changes["end_time"] = _parse_time(input.end_time)
            await events_crud.update_event(db, event, actor_account_id=_actor(info), **changes)
            return event.id

        return await _attempt(db, action, "Cambios guardados.")

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def publish_special_event(self, info: Info, id: int) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            event = await _require_event(db, id)
            await events_crud.publish_event(db, event, actor_account_id=_actor(info))
            return event.id

        return await _attempt(
            db, action, "Evento publicado: el asistente de WhatsApp ya puede venderlo."
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def unpublish_special_event(self, info: Info, id: int) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            event = await _require_event(db, id)
            await events_crud.unpublish_event(db, event, actor_account_id=_actor(info))
            return event.id

        return await _attempt(db, action, "El evento volvió a borrador; ya no se vende.")

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def cancel_special_event(self, info: Info, id: int, reason: str) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        summary_holder = {}

        async def action() -> int:
            event = await _require_event(db, id)
            summary_holder["summary"] = await events_crud.cancel_event(
                db, event, actor_account_id=_actor(info), reason=reason
            )
            return event.id

        result = await _attempt(db, action, "Evento cancelado.")
        summary = summary_holder.get("summary")
        if result.success and summary is not None:
            await special_event_payments.expire_payment_links(summary.preference_ids)
            parts = ["Evento cancelado."]
            if summary.to_refund_order_ids:
                parts.append(
                    f"{len(summary.to_refund_order_ids)} entrada(s) pagada(s) quedaron como "
                    "«por reembolsar»: reembólsalas desde la lista de participantes."
                )
            if summary.active_takeovers:
                parts.append(
                    "Las clases regulares sustituidas siguen desplazadas: usa «Restituir horario "
                    "regular» para revisarlas y reactivarlas."
                )
            parts.append("No se envió ningún mensaje a los clientes.")
            result.message = " ".join(parts)
        return result

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def apply_special_event_takeover(
        self,
        info: Info,
        event_id: int,
        session_ids: List[int],
        reason: Optional[str] = None,
    ) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        holder = {}

        async def action() -> int:
            event = await _require_event(db, event_id)
            holder["result"] = await takeover_crud.apply_takeover(
                db,
                event,
                actor_account_id=_actor(info),
                session_ids=session_ids,
                reason=reason,
            )
            return event.id

        result = await _attempt(db, action, "Sustitución aplicada.")
        if result.success and holder.get("result"):
            r = holder["result"]
            result.message = (
                f"Sustitución aplicada: {r.sessions_taken} clase(s) y {r.clients_displaced} "
                "cliente(s) desplazados. Están en «Clientes desplazados» para darles seguimiento."
            )
        return result

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def restore_regular_schedule(self, info: Info, event_id: int) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        holder = {}

        async def action() -> int:
            event = await _require_event(db, event_id)
            holder["result"] = await takeover_crud.apply_restore(
                db, event, actor_account_id=_actor(info)
            )
            return event.id

        result = await _attempt(db, action, "Horario regular restituido.")
        if result.success and holder.get("result"):
            r = holder["result"]
            result.message = (
                f"Horario restituido: {r.sessions_restored} clase(s); {r.reservations_restored} "
                f"reserva(s) reactivadas y {r.reservations_kept} siguen desplazadas para revisión."
            )
        return result

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_displacement_follow_up(
        self, info: Info, input: DisplacementFollowUpInput
    ) -> DisplacementResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return DisplacementResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            displacement = await takeover_crud.update_follow_up(
                db,
                input.displacement_id,
                actor_account_id=_actor(info),
                notified=input.notified,
                notification_channel=input.notification_channel,
                notification_notes=input.notification_notes,
                compensation_type=input.compensation_type,
                compensation_notes=input.compensation_notes,
                resolved=input.resolved,
            )
            event_id = displacement.event_id
            await db.commit()
        except SpecialEventError as exc:
            await db.rollback()
            return DisplacementResult(success=False, error=str(exc))
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("update_displacement_follow_up failed")
            return DisplacementResult(success=False, error="No se pudo guardar el seguimiento.")
        rows = await takeover_crud.list_displacements(db, event_id=event_id)
        row = next((r for r in rows if r.id == input.displacement_id), None)
        return DisplacementResult(
            success=True, displacement=build_displacement(row) if row else None
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def apply_displacement_bonus_class(
        self, info: Info, displacement_id: int, resolved: bool = True
    ) -> DisplacementResult:
        """The default compensation: extend the client's membership to one more class after it
        ends and book that class (see crud.special_events.bonus)."""
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return DisplacementResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            displacement = await bonus_crud.apply_bonus_class(
                db, displacement_id, actor_account_id=_actor(info), resolve=resolved
            )
            event_id = displacement.event_id
            await db.commit()
        except SpecialEventError as exc:
            await db.rollback()
            return DisplacementResult(success=False, error=str(exc))
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("apply_displacement_bonus_class failed")
            return DisplacementResult(success=False, error="No se pudo dar la clase extra.")
        rows = await takeover_crud.list_displacements(db, event_id=event_id)
        row = next((r for r in rows if r.id == displacement_id), None)
        return DisplacementResult(
            success=True, displacement=build_displacement(row) if row else None
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def apply_special_event_bonus_classes(self, info: Info, event_id: int) -> SpecialEventResult:
        """The extra class for every displaced client of the event still without compensation."""
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        holder: dict = {}

        async def action() -> int:
            event = await _require_event(db, event_id)
            holder["result"] = await bonus_crud.apply_bonus_classes(
                db, event, actor_account_id=_actor(info)
            )
            return event.id

        result = await _attempt(db, action, "Clases extra aplicadas.")
        if result.success and holder.get("result"):
            r = holder["result"]
            lines = [f"Clase extra para {len(r.applied)} cliente(s)."]
            if r.skipped:
                lines.append("Sin clase extra:")
                lines.extend(f"• {name}: {reason}" for name, reason in r.skipped)
            result.message = "\n".join(lines)
        return result

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def add_special_event_participant(
        self, info: Info, input: AddParticipantInput
    ) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            order = await orders_crud.add_manual_participant(
                db,
                input.event_id,
                actor_account_id=_actor(info),
                participant_name=input.participant_name,
                participant_phone=input.participant_phone,
                payment_method=input.payment_method,
                amount=input.amount,
                seat_id=input.seat_id,
                notes=input.notes,
            )
            return order.event_id

        return await _attempt(db, action, "Participante registrado.")

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def cancel_special_event_order(
        self, info: Info, order_id: int, reason: Optional[str] = None
    ) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        holder = {}

        async def action() -> int:
            order = await orders_crud.cancel_order(
                db, order_id, actor_account_id=_actor(info), reason=reason or ""
            )
            holder["preference"] = order.mp_preference_id
            return order.event_id

        result = await _attempt(db, action, "Compra cancelada; el lugar quedó libre.")
        if result.success and holder.get("preference"):
            await special_event_payments.expire_payment_links([holder["preference"]])
        return result

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def refund_special_event_order(
        self, info: Info, order_id: int, reason: Optional[str] = None
    ) -> SpecialEventResult:
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db
        try:
            order = await special_event_payments.refund_order(
                db, order_id, actor_account_id=_actor(info), reason=reason or ""
            )
        except SpecialEventError as exc:
            await db.rollback()
            return SpecialEventResult(success=False, error=str(exc))
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("refund_special_event_order failed")
            return SpecialEventResult(success=False, error="No se pudo procesar el reembolso.")
        message = (
            "Reembolso registrado."
            if order.status == "refunded"
            else "El reembolso automático falló; la compra quedó «en revisión» para hacerlo manualmente."
        )
        return SpecialEventResult(
            success=order.status == "refunded",
            message=message,
            error=None if order.status == "refunded" else message,
            detail=await build_detail(db, order.event_id),
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def link_campaign_to_special_event(
        self, info: Info, campaign_id: int, event_id: Optional[int] = None
    ) -> SpecialEventResult:
        """Tell the chatbot which event a campaign promotes ("Quiero reservar" -> this event)."""
        error = await require_capability(info, MANAGE_SPECIAL_EVENTS)
        if error:
            return SpecialEventResult(success=False, error=error)
        db: AsyncSession = info.context.db

        async def action() -> int:
            campaign = await db.get(Campaign, campaign_id)
            if campaign is None:
                raise SpecialEventError("No encontré esa campaña.")
            previous = campaign.special_event_id
            if event_id is not None:
                await _require_event(db, event_id)
            campaign.special_event_id = event_id
            return event_id if event_id is not None else previous

        result = await _attempt(
            db, action, "Campaña vinculada." if event_id is not None else "Campaña desvinculada."
        )
        return result
