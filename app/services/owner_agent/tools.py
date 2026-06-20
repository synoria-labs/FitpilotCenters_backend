"""LangChain tools for the owner/admin WhatsApp agent."""
from __future__ import annotations

import logging
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import ownerAgentCrud as owner_crud
from app.crud.dashboard_metrics import get_dashboard_metrics
from app.crud.memberships.payment_metrics import get_payment_metrics
from app.models import Conversation, Lead, Message, People
from app.models.classModel import ClassSession, Reservation
from app.models.campaignsModel import Campaign
from app.models.membershipsModel import MembershipSubscription
from app.models.ownerAgentModel import (
    OWNER_PENDING_STATUS_CONFIRMED,
    OWNER_TASK_STATUS_CANCELED,
    OWNER_TASK_STATUS_DONE,
)
from app.models.userModel import PersonRole, Role

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("America/Mexico_City")

ACTION_CREATE_TASK = "create_task"
ACTION_COMPLETE_TASK = "complete_task"
ACTION_CANCEL_TASK = "cancel_task"
ACTION_NOTIFICATION_SWEEP = "notification_sweep"
ACTION_CAMPAIGN_SWEEP = "campaign_sweep"


@dataclass
class OwnerAgentContext:
    db: AsyncSession
    conversation_id: int
    authorized_phone_id: int
    message_id: Optional[int] = None
    require_confirmation: bool = True


def _now_local() -> datetime:
    return datetime.now(_TZ)


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _period_range(period: str = "today") -> tuple[datetime, datetime, str]:
    p = (period or "today").strip().lower()
    now = _now_local()
    if p in {"hoy", "today", "dia"}:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "hoy"
    elif p in {"ayer", "yesterday"}:
        end_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end_local - timedelta(days=1)
        return _to_utc(start), _to_utc(end_local), "ayer"
    elif p in {"semana", "esta semana", "week", "this_week"}:
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        label = "esta semana"
    elif p in {"mes", "este mes", "month", "this_month"}:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "este mes"
    elif p in {"30d", "last_30_days", "ultimos 30 dias"}:
        start = now - timedelta(days=30)
        label = "ultimos 30 dias"
    elif p in {"ano", "año", "year", "this_year"}:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        label = "este ano"
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "hoy"
    return _to_utc(start), _to_utc(now), label


def _money(value: float) -> str:
    return f"${float(value or 0):,.2f}"


def _fmt_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "sin fecha"
    local = value.astimezone(_TZ) if value.tzinfo else value.replace(tzinfo=timezone.utc).astimezone(_TZ)
    return local.strftime("%Y-%m-%d %H:%M")


async def _audit(
    ctx: OwnerAgentContext,
    *,
    tool_name: str,
    payload: Optional[dict],
    result: Optional[str] = None,
    status: str = "ok",
    error: Optional[str] = None,
) -> None:
    try:
        await owner_crud.audit_event(
            ctx.db,
            conversation_id=ctx.conversation_id,
            message_id=ctx.message_id,
            authorized_phone_id=ctx.authorized_phone_id,
            tool_name=tool_name,
            payload=payload or {},
            result_summary=result,
            status=status,
            error=error,
            commit=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug("owner agent audit failed", exc_info=True)


def _wrap_tool(ctx: OwnerAgentContext, name: str, fn):
    @wraps(fn)
    async def _inner(*args, **kwargs):
        payload = {"args": list(args), "kwargs": kwargs}
        try:
            result = await fn(*args, **kwargs)
            await _audit(ctx, tool_name=name, payload=payload, result=str(result)[:4000])
            return result
        except Exception as exc:  # noqa: BLE001
            await ctx.db.rollback()
            await _audit(
                ctx,
                tool_name=name,
                payload=payload,
                status="error",
                error=str(exc),
            )
            logger.exception("owner tool %s failed", name)
            return f"No pude ejecutar {name}: {exc}"

    return _inner


def build_tools(ctx: OwnerAgentContext) -> list:
    from langchain_core.tools import StructuredTool

    db = ctx.db

    async def get_business_report(period: str = "today") -> str:
        """KPIs generales del negocio para un periodo: socios, ingresos, reservas y ocupacion."""
        start, end, label = _period_range(period)
        metrics = await get_dashboard_metrics(db=db, start_date=start, end_date=end)
        lines = [
            f"Reporte {label} ({_fmt_dt(start)} a {_fmt_dt(end)}):",
            f"- Ingresos: {_money(metrics.period_revenue)}",
            f"- Reservas: {metrics.period_reservations}",
            f"- Socios activos: {metrics.active_members}",
            f"- Nuevos socios: {metrics.new_members}",
            f"- Ocupacion promedio: {metrics.avg_occupancy:.1f}%",
        ]
        if metrics.top_membership_sales_period:
            top = metrics.top_membership_sales_period
            lines.append(f"- Plan mas vendido: {top.plan_name} ({top.count}, {_money(top.total)})")
        return "\n".join(lines)

    async def get_payments_report(period: str = "today") -> str:
        """Reporte financiero: pagos, total, metodos, pendientes, huerfanos y duplicados sospechosos."""
        start, end, label = _period_range(period)
        metrics = await get_payment_metrics(db, start_date=start, end_date=end)
        methods = ", ".join(
            f"{b.method}: {b.count} / {_money(b.total)}" for b in metrics.by_method[:5]
        ) or "sin pagos"
        return "\n".join(
            [
                f"Finanzas {label}:",
                f"- Total: {_money(metrics.total_amount)} en {metrics.total_count} pago(s)",
                f"- Completado: {_money(metrics.completed_amount)}",
                f"- Ticket promedio: {_money(metrics.avg_amount)}",
                f"- Pendientes: {metrics.pending_count} / {_money(metrics.pending_amount)}",
                f"- Fallidos: {metrics.failed_count}; reembolsados: {metrics.refunded_count}",
                f"- Huerfanos: {metrics.orphan_count}; duplicados sospechosos: {metrics.duplicate_suspect_count}",
                f"- Por metodo: {methods}",
            ]
        )

    async def get_members_report(days_ahead: int = 14) -> str:
        """Socios activos, vencidos y proximos vencimientos."""
        now = datetime.now(timezone.utc)
        ahead = now + timedelta(days=max(1, min(int(days_ahead or 14), 60)))
        member_role = (
            select(PersonRole.person_id)
            .join(Role, Role.id == PersonRole.role_id)
            .where(Role.code == "member")
        )
        active_count = int(
            (
                await db.execute(
                    select(func.count(MembershipSubscription.id)).where(
                        MembershipSubscription.status == "active",
                        MembershipSubscription.start_at <= now,
                        MembershipSubscription.end_at > now,
                    )
                )
            ).scalar_one()
            or 0
        )
        expired_count = int(
            (
                await db.execute(
                    select(func.count(MembershipSubscription.id)).where(
                        MembershipSubscription.status == "active",
                        MembershipSubscription.end_at <= now,
                    )
                )
            ).scalar_one()
            or 0
        )
        expiring = (
            await db.execute(
                select(People.full_name, MembershipSubscription.end_at)
                .join(MembershipSubscription, MembershipSubscription.person_id == People.id)
                .where(People.id.in_(member_role))
                .where(People.deleted_at.is_(None))
                .where(MembershipSubscription.status == "active")
                .where(MembershipSubscription.end_at > now)
                .where(MembershipSubscription.end_at <= ahead)
                .order_by(MembershipSubscription.end_at.asc())
                .limit(10)
            )
        ).all()
        lines = [
            "Socios:",
            f"- Suscripciones activas: {active_count}",
            f"- Suscripciones vencidas sin cerrar: {expired_count}",
            f"- Vencen en {days_ahead} dias: {len(expiring)}",
        ]
        for name, end_at in expiring:
            lines.append(f"  - {name or 'Sin nombre'}: {_fmt_dt(end_at)}")
        return "\n".join(lines)

    async def get_classes_report(period: str = "today") -> str:
        """Reporte de clases, reservas y ocupacion por periodo."""
        start, end, label = _period_range(period)
        sessions = int(
            (
                await db.execute(
                    select(func.count(ClassSession.id)).where(
                        ClassSession.start_at >= start,
                        ClassSession.start_at <= end,
                        ClassSession.status.in_(("scheduled", "completed")),
                    )
                )
            ).scalar_one()
            or 0
        )
        reservations = int(
            (
                await db.execute(
                    select(func.count(Reservation.id))
                    .join(ClassSession, ClassSession.id == Reservation.session_id)
                    .where(
                        ClassSession.start_at >= start,
                        ClassSession.start_at <= end,
                        Reservation.status.in_(("reserved", "checked_in")),
                    )
                )
            ).scalar_one()
            or 0
        )
        metrics = await get_dashboard_metrics(db=db, start_date=start, end_date=end)
        top = metrics.occupancy_by_class[:5]
        lines = [
            f"Clases {label}:",
            f"- Sesiones: {sessions}",
            f"- Reservas/check-ins: {reservations}",
            f"- Ocupacion promedio: {metrics.avg_occupancy:.1f}%",
        ]
        for b in top:
            lines.append(f"  - {b.class_name}: {b.reserved}/{b.capacity} ({b.occupancy_pct:.1f}%)")
        return "\n".join(lines)

    async def get_leads_report(period: str = "this_month") -> str:
        """Resumen de leads por estado y conversiones."""
        start, end, label = _period_range(period)
        total = int(
            (
                await db.execute(
                    select(func.count(Lead.id)).where(Lead.created_at >= start, Lead.created_at <= end)
                )
            ).scalar_one()
            or 0
        )
        rows = (
            await db.execute(
                select(Lead.status, func.count(Lead.id))
                .where(Lead.created_at >= start, Lead.created_at <= end)
                .group_by(Lead.status)
                .order_by(func.count(Lead.id).desc())
            )
        ).all()
        converted = int(
            (
                await db.execute(
                    select(func.count(Lead.id)).where(
                        Lead.converted_at >= start, Lead.converted_at <= end
                    )
                )
            ).scalar_one()
            or 0
        )
        by_status = ", ".join(f"{status}: {count}" for status, count in rows) or "sin leads"
        return f"Leads {label}: total {total}; convertidos {converted}; por estado: {by_status}."

    async def get_campaigns_report() -> str:
        """Estado resumido de campanas de marketing."""
        rows = (
            await db.execute(
                select(Campaign.status, func.count(Campaign.id))
                .group_by(Campaign.status)
                .order_by(func.count(Campaign.id).desc())
            )
        ).all()
        total = sum(int(count or 0) for _status, count in rows)
        by_status = ", ".join(f"{status}: {count}" for status, count in rows) or "sin campanas"
        return f"Campanas: {total} total; {by_status}."

    async def get_whatsapp_report() -> str:
        """Conversaciones y mensajes de WhatsApp pendientes/no leidos."""
        unread = int(
            (
                await db.execute(
                    select(func.count(Message.id)).where(
                        Message.direction == "inbound",
                        Message.is_read.is_(False),
                    )
                )
            ).scalar_one()
            or 0
        )
        active_conversations = int(
            (
                await db.execute(
                    select(func.count(Conversation.id)).where(Conversation.status == "active")
                )
            ).scalar_one()
            or 0
        )
        latest = (
            await db.execute(
                select(Message.text_content, Message.timestamp)
                .where(Message.direction == "inbound")
                .where(Message.message_type == "text")
                .order_by(Message.timestamp.desc(), Message.id.desc())
                .limit(5)
            )
        ).all()
        lines = [
            "WhatsApp:",
            f"- Conversaciones activas: {active_conversations}",
            f"- Mensajes entrantes no leidos: {unread}",
        ]
        for text, ts in latest:
            snippet = (text or "").strip().replace("\n", " ")[:80]
            lines.append(f"  - {_fmt_dt(ts)}: {snippet}")
        return "\n".join(lines)

    async def list_tasks(include_done: bool = False) -> str:
        """Lista tareas administrativas del agente."""
        tasks = await owner_crud.list_owner_tasks(db, include_done=include_done)
        if not tasks:
            return "No hay tareas pendientes."
        lines = ["Tareas:"]
        for task in tasks:
            due = f", vence {_fmt_dt(task.due_at)}" if task.due_at else ""
            lines.append(f"- #{task.id} [{task.status}] {task.title}{due}")
        return "\n".join(lines)

    async def _proposal(action_type: str, payload: dict, summary: str) -> str:
        await owner_crud.upsert_pending_action(
            db,
            conversation_id=ctx.conversation_id,
            authorized_phone_id=ctx.authorized_phone_id,
            action_type=action_type,
            payload=payload,
            summary=summary,
            commit=True,
        )
        return f"Propuesta lista: {summary}. Responde 'si' para confirmar o 'no' para cancelar."

    async def propose_create_task(title: str, description: Optional[str] = None) -> str:
        """Propone crear una tarea administrativa."""
        clean_title = (title or "").strip()
        if not clean_title:
            return "Necesito el titulo de la tarea."
        return await _proposal(
            ACTION_CREATE_TASK,
            {"title": clean_title, "description": (description or "").strip() or None},
            f"Crear tarea: {clean_title}",
        )

    async def propose_complete_task(task_id: int) -> str:
        """Propone marcar una tarea como completada."""
        return await _proposal(
            ACTION_COMPLETE_TASK,
            {"task_id": int(task_id)},
            f"Marcar tarea #{int(task_id)} como completada",
        )

    async def propose_cancel_task(task_id: int) -> str:
        """Propone cancelar una tarea."""
        return await _proposal(
            ACTION_CANCEL_TASK,
            {"task_id": int(task_id)},
            f"Cancelar tarea #{int(task_id)}",
        )

    async def propose_notification_sweep() -> str:
        """Propone ejecutar el barrido de notificaciones de renovacion/vencimiento."""
        return await _proposal(
            ACTION_NOTIFICATION_SWEEP,
            {},
            "Ejecutar barrido de notificaciones automaticas",
        )

    async def propose_campaign_sweep() -> str:
        """Propone ejecutar el barrido de campanas programadas."""
        return await _proposal(
            ACTION_CAMPAIGN_SWEEP,
            {},
            "Ejecutar barrido de campanas programadas",
        )

    async def confirm_action() -> str:
        """Ejecuta la accion administrativa pendiente tras una confirmacion explicita."""
        pending = await owner_crud.get_pending_action(db, ctx.conversation_id)
        if pending is None:
            return "No hay accion pendiente por confirmar."
        pending_id = pending.id
        action_type = pending.action_type
        payload = dict(pending.payload or {})
        try:
            result = await _execute_pending(action_type, payload)
        except Exception as exc:  # noqa: BLE001
            await db.rollback()
            await _audit(
                ctx,
                tool_name="confirm_action",
                action_type=action_type,
                payload=payload,
                status="error",
                error=str(exc),
            )
            return f"No pude ejecutar la accion: {exc}"
        await owner_crud.mark_pending_action(
            db, pending_id, OWNER_PENDING_STATUS_CONFIRMED, commit=True
        )
        await _audit(
            ctx,
            tool_name="confirm_action",
            action_type=action_type,
            payload=payload,
            result=result,
        )
        return result

    async def cancel_action() -> str:
        """Cancela la accion administrativa pendiente."""
        canceled = await owner_crud.cancel_pending_action(db, ctx.conversation_id, commit=True)
        return "Accion cancelada." if canceled else "No habia accion pendiente."

    async def _execute_pending(action_type: str, payload: dict) -> str:
        if action_type == ACTION_CREATE_TASK:
            task = await owner_crud.create_owner_task(
                db,
                title=payload.get("title") or "",
                description=payload.get("description"),
                created_by_phone_id=ctx.authorized_phone_id,
                commit=True,
            )
            return f"Tarea #{task.id} creada: {task.title}"
        if action_type == ACTION_COMPLETE_TASK:
            task = await owner_crud.set_owner_task_status(
                db, task_id=int(payload["task_id"]), status=OWNER_TASK_STATUS_DONE, commit=True
            )
            if task is None:
                return f"No encontre la tarea #{payload['task_id']}."
            return f"Tarea #{task.id} completada: {task.title}"
        if action_type == ACTION_CANCEL_TASK:
            task = await owner_crud.set_owner_task_status(
                db,
                task_id=int(payload["task_id"]),
                status=OWNER_TASK_STATUS_CANCELED,
                commit=True,
            )
            if task is None:
                return f"No encontre la tarea #{payload['task_id']}."
            return f"Tarea #{task.id} cancelada: {task.title}"
        if action_type == ACTION_NOTIFICATION_SWEEP:
            from app.services.notification_service import run_all_sweeps

            stats = await run_all_sweeps()
            return f"Barrido de notificaciones ejecutado: {stats}"
        if action_type == ACTION_CAMPAIGN_SWEEP:
            from app.services.campaign_service import run_campaign_sweep

            stats = await run_campaign_sweep()
            return f"Barrido de campanas ejecutado: {stats}"
        raise ValueError(f"Tipo de accion desconocido: {action_type}")

    factories = [
        (get_business_report, "get_business_report", "KPIs generales del negocio para today/week/month/last_30_days/year."),
        (get_payments_report, "get_payments_report", "Reporte financiero por periodo."),
        (get_members_report, "get_members_report", "Socios activos, vencidos y proximos vencimientos."),
        (get_classes_report, "get_classes_report", "Clases, reservas y ocupacion por periodo."),
        (get_leads_report, "get_leads_report", "Leads y conversiones por periodo."),
        (get_campaigns_report, "get_campaigns_report", "Estado resumido de campanas."),
        (get_whatsapp_report, "get_whatsapp_report", "Conversaciones y mensajes WhatsApp pendientes."),
        (list_tasks, "list_tasks", "Lista tareas administrativas."),
        (propose_create_task, "propose_create_task", "Propone crear una tarea. Args: title, description opcional."),
        (propose_complete_task, "propose_complete_task", "Propone completar una tarea por id."),
        (propose_cancel_task, "propose_cancel_task", "Propone cancelar una tarea por id."),
        (propose_notification_sweep, "propose_notification_sweep", "Propone ejecutar barrido de notificaciones."),
        (propose_campaign_sweep, "propose_campaign_sweep", "Propone ejecutar barrido de campanas."),
        (confirm_action, "confirm_action", "Ejecuta la accion pendiente tras confirmacion explicita."),
        (cancel_action, "cancel_action", "Cancela la accion pendiente."),
    ]

    return [
        StructuredTool.from_function(
            coroutine=_wrap_tool(ctx, name, fn),
            name=name,
            description=desc,
        )
        for fn, name, desc in factories
    ]
