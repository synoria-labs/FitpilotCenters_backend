"""CRUD and normalization helpers for the owner/admin WhatsApp agent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    OwnerAgentAuditLog,
    OwnerAgentAuthorizedPhone,
    OwnerAgentConfig,
    OwnerAgentPendingAction,
    OwnerTask,
)
from app.models.ownerAgentModel import (
    OWNER_PENDING_STATUS_CANCELED,
    OWNER_PENDING_STATUS_PENDING,
    OWNER_TASK_STATUS_CANCELED,
    OWNER_TASK_STATUS_DONE,
    OWNER_TASK_STATUS_OPEN,
)


DEFAULT_OWNER_SYSTEM_PROMPT = (
    "Eres el agente administrativo de FitPilot. Respondes por WhatsApp al dueno o "
    "administradores autorizados. Puedes consultar datos reales del negocio usando "
    "herramientas y resumirlos de forma breve y accionable. Nunca inventes metricas, "
    "pagos, horarios, disponibilidad ni datos de socios. Para cualquier accion que "
    "cambie datos, primero presenta un resumen claro y pide confirmacion explicita."
)


@dataclass
class OwnerAgentConfigData:
    id: Optional[int]
    enabled: bool
    require_confirmation: bool
    model: str
    system_prompt: Optional[str]
    history_limit: int
    max_tokens: int
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, model: OwnerAgentConfig) -> "OwnerAgentConfigData":
        return cls(
            id=model.id,
            enabled=bool(model.enabled),
            require_confirmation=bool(model.require_confirmation),
            model=model.model,
            system_prompt=model.system_prompt,
            history_limit=int(model.history_limit or 30),
            max_tokens=int(model.max_tokens or 1024),
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


@dataclass
class OwnerAgentAuthorizedPhoneData:
    id: int
    label: str
    phone_number: str
    normalized_wa_id: str
    enabled: bool
    created_by: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, model: OwnerAgentAuthorizedPhone) -> "OwnerAgentAuthorizedPhoneData":
        return cls(
            id=model.id,
            label=model.label,
            phone_number=model.phone_number,
            normalized_wa_id=model.normalized_wa_id,
            enabled=bool(model.enabled),
            created_by=model.created_by,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


def digits_only(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def phone_match_keys(value: Optional[str]) -> set[str]:
    """Return phone variants used by Meta/Mexico WhatsApp formats.

    Mexico numbers may arrive as local 10 digits, 52 + local, or 521 + local.
    Keeping all variants here lets the configurable allowlist remain stable even
    if Meta sends a different representation than the admin typed.
    """
    digits = digits_only(value)
    if not digits:
        return set()

    keys = {digits}
    if len(digits) >= 10:
        local = digits[-10:]
        keys.update({local, f"52{local}", f"521{local}"})

    if digits.startswith("521") and len(digits) >= 13:
        local = digits[3:]
        keys.update({local, f"52{local}"})
    elif digits.startswith("52") and len(digits) >= 12:
        local = digits[2:]
        keys.update({local, f"521{local}"})

    return {key for key in keys if key}


def normalize_owner_phone(value: Optional[str]) -> str:
    """Canonical WhatsApp id stored for an authorized owner phone."""
    digits = digits_only(value)
    if len(digits) < 10:
        raise ValueError("Telefono invalido: captura al menos 10 digitos.")

    local = digits[-10:]
    if digits.startswith("521") and len(digits) >= 13:
        return f"521{local}"
    if digits.startswith("52") and len(digits) >= 12:
        return f"521{local}"
    if len(digits) == 10:
        return f"521{local}"
    return digits


def _clamp_int(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(parsed, max_value))


async def get_config_model(db: AsyncSession) -> Optional[OwnerAgentConfig]:
    stmt = select(OwnerAgentConfig).order_by(OwnerAgentConfig.id.asc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def get_or_create_config_model(db: AsyncSession) -> OwnerAgentConfig:
    config = await get_config_model(db)
    if config is not None:
        return config
    now = datetime.now(timezone.utc)
    config = OwnerAgentConfig(
        enabled=False,
        require_confirmation=True,
        model="claude-sonnet-4-6",
        system_prompt=DEFAULT_OWNER_SYSTEM_PROMPT,
        history_limit=30,
        max_tokens=1024,
        created_at=now,
        updated_at=now,
    )
    db.add(config)
    await db.flush()
    return config


async def get_config(db: AsyncSession) -> OwnerAgentConfigData:
    model = await get_config_model(db)
    if model is None:
        return OwnerAgentConfigData(
            id=None,
            enabled=False,
            require_confirmation=True,
            model="claude-sonnet-4-6",
            system_prompt=DEFAULT_OWNER_SYSTEM_PROMPT,
            history_limit=30,
            max_tokens=1024,
            created_at=None,
            updated_at=None,
        )
    return OwnerAgentConfigData.from_model(model)


async def upsert_config(db: AsyncSession, *, commit: bool = True, **fields) -> OwnerAgentConfig:
    config = await get_config_model(db)
    if config is None:
        now = datetime.now(timezone.utc)
        config = OwnerAgentConfig(
            enabled=False,
            require_confirmation=True,
            model="claude-sonnet-4-6",
            system_prompt=DEFAULT_OWNER_SYSTEM_PROMPT,
            history_limit=30,
            max_tokens=1024,
            created_at=now,
            updated_at=now,
        )
        db.add(config)
        await db.flush()
    if "enabled" in fields and fields["enabled"] is not None:
        config.enabled = bool(fields["enabled"])
    if "require_confirmation" in fields and fields["require_confirmation"] is not None:
        config.require_confirmation = bool(fields["require_confirmation"])
    if "model" in fields and fields["model"] is not None:
        model = str(fields["model"]).strip()
        if model:
            config.model = model[:80]
    if "system_prompt" in fields and fields["system_prompt"] is not None:
        config.system_prompt = str(fields["system_prompt"]).strip() or None
    if "history_limit" in fields and fields["history_limit"] is not None:
        config.history_limit = _clamp_int(fields["history_limit"], 30, 1, 100)
    if "max_tokens" in fields and fields["max_tokens"] is not None:
        config.max_tokens = _clamp_int(fields["max_tokens"], 1024, 256, 4096)
    config.updated_at = datetime.now(timezone.utc)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(config)
    return config


async def list_authorized_phones(db: AsyncSession) -> list[OwnerAgentAuthorizedPhoneData]:
    stmt = select(OwnerAgentAuthorizedPhone).order_by(
        OwnerAgentAuthorizedPhone.enabled.desc(),
        OwnerAgentAuthorizedPhone.label.asc(),
        OwnerAgentAuthorizedPhone.id.asc(),
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [OwnerAgentAuthorizedPhoneData.from_model(row) for row in rows]


async def get_authorized_phone_model(
    db: AsyncSession, phone_id: int
) -> Optional[OwnerAgentAuthorizedPhone]:
    return await db.get(OwnerAgentAuthorizedPhone, int(phone_id))


async def add_authorized_phone(
    db: AsyncSession,
    *,
    label: str,
    phone_number: str,
    created_by: Optional[int] = None,
    enabled: bool = True,
    commit: bool = True,
) -> OwnerAgentAuthorizedPhone:
    normalized = normalize_owner_phone(phone_number)
    existing = (
        await db.execute(
            select(OwnerAgentAuthorizedPhone).where(
                OwnerAgentAuthorizedPhone.normalized_wa_id == normalized
            )
        )
    ).scalars().first()
    now = datetime.now(timezone.utc)
    clean_label = (label or "").strip() or normalized
    clean_phone = (phone_number or "").strip() or normalized

    if existing is None:
        existing = OwnerAgentAuthorizedPhone(
            label=clean_label[:120],
            phone_number=clean_phone[:40],
            normalized_wa_id=normalized,
            enabled=bool(enabled),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        db.add(existing)
    else:
        existing.label = clean_label[:120]
        existing.phone_number = clean_phone[:40]
        existing.enabled = bool(enabled)
        if existing.created_by is None:
            existing.created_by = created_by
        existing.updated_at = now

    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(existing)
    return existing


async def update_authorized_phone(
    db: AsyncSession,
    *,
    phone_id: int,
    label: Optional[str] = None,
    phone_number: Optional[str] = None,
    enabled: Optional[bool] = None,
    commit: bool = True,
) -> Optional[OwnerAgentAuthorizedPhone]:
    model = await get_authorized_phone_model(db, phone_id)
    if model is None:
        return None
    if label is not None:
        model.label = (label.strip() or model.normalized_wa_id)[:120]
    if phone_number is not None:
        model.phone_number = (phone_number.strip() or model.phone_number)[:40]
        model.normalized_wa_id = normalize_owner_phone(phone_number)
    if enabled is not None:
        model.enabled = bool(enabled)
    model.updated_at = datetime.now(timezone.utc)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(model)
    return model


async def disable_authorized_phone(
    db: AsyncSession, *, phone_id: int, commit: bool = True
) -> Optional[OwnerAgentAuthorizedPhone]:
    return await update_authorized_phone(
        db, phone_id=phone_id, enabled=False, commit=commit
    )


async def resolve_authorized_phone(
    db: AsyncSession, wa_id: Optional[str]
) -> Optional[OwnerAgentAuthorizedPhoneData]:
    keys = phone_match_keys(wa_id)
    if not keys:
        return None
    stmt = (
        select(OwnerAgentAuthorizedPhone)
        .where(OwnerAgentAuthorizedPhone.enabled.is_(True))
        .where(OwnerAgentAuthorizedPhone.normalized_wa_id.in_(keys))
        .limit(1)
    )
    row = (await db.execute(stmt)).scalars().first()
    return OwnerAgentAuthorizedPhoneData.from_model(row) if row else None


async def audit_event(
    db: AsyncSession,
    *,
    conversation_id: Optional[int],
    message_id: Optional[int],
    authorized_phone_id: Optional[int],
    tool_name: Optional[str] = None,
    action_type: Optional[str] = None,
    payload: Optional[dict] = None,
    result_summary: Optional[str] = None,
    status: str = "ok",
    error: Optional[str] = None,
    commit: bool = False,
) -> OwnerAgentAuditLog:
    row = OwnerAgentAuditLog(
        conversation_id=conversation_id,
        message_id=message_id,
        authorized_phone_id=authorized_phone_id,
        tool_name=tool_name,
        action_type=action_type,
        payload=payload or {},
        result_summary=(result_summary or "")[:4000] if result_summary else None,
        status=status[:20],
        error=(error or "")[:4000] if error else None,
    )
    db.add(row)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(row)
    return row


async def get_pending_action(
    db: AsyncSession, conversation_id: int
) -> Optional[OwnerAgentPendingAction]:
    stmt = select(OwnerAgentPendingAction).where(
        OwnerAgentPendingAction.conversation_id == int(conversation_id)
    )
    pending = (await db.execute(stmt)).scalars().first()
    if pending is None or pending.status != OWNER_PENDING_STATUS_PENDING:
        return None
    if pending.expires_at is not None and pending.expires_at < datetime.now(timezone.utc):
        pending.status = "expired"
        pending.updated_at = datetime.now(timezone.utc)
        await db.flush()
        return None
    return pending


async def upsert_pending_action(
    db: AsyncSession,
    *,
    conversation_id: int,
    authorized_phone_id: Optional[int],
    action_type: str,
    payload: dict,
    summary: str,
    ttl_minutes: int = 30,
    commit: bool = True,
) -> OwnerAgentPendingAction:
    pending = (
        await db.execute(
            select(OwnerAgentPendingAction).where(
                OwnerAgentPendingAction.conversation_id == int(conversation_id)
            )
        )
    ).scalars().first()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=max(1, min(int(ttl_minutes or 30), 240)))
    if pending is None:
        pending = OwnerAgentPendingAction(
            conversation_id=int(conversation_id),
            authorized_phone_id=authorized_phone_id,
            action_type=action_type[:50],
            payload=payload or {},
            summary=summary,
            status=OWNER_PENDING_STATUS_PENDING,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        db.add(pending)
    else:
        pending.authorized_phone_id = authorized_phone_id
        pending.action_type = action_type[:50]
        pending.payload = payload or {}
        pending.summary = summary
        pending.status = OWNER_PENDING_STATUS_PENDING
        pending.expires_at = expires_at
        pending.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(pending)
    return pending


async def mark_pending_action(
    db: AsyncSession, pending_id: int, status: str, *, commit: bool = True
) -> bool:
    pending = await db.get(OwnerAgentPendingAction, int(pending_id))
    if pending is None:
        return False
    pending.status = status
    pending.updated_at = datetime.now(timezone.utc)
    await db.flush()
    if commit:
        await db.commit()
    return True


async def cancel_pending_action(
    db: AsyncSession, conversation_id: int, *, commit: bool = True
) -> bool:
    pending = await get_pending_action(db, conversation_id)
    if pending is None:
        return False
    return await mark_pending_action(
        db, pending.id, OWNER_PENDING_STATUS_CANCELED, commit=commit
    )


async def list_owner_tasks(
    db: AsyncSession, *, include_done: bool = False, limit: int = 20
) -> list[OwnerTask]:
    statuses = [OWNER_TASK_STATUS_OPEN]
    if include_done:
        statuses.extend([OWNER_TASK_STATUS_DONE, OWNER_TASK_STATUS_CANCELED])
    stmt = (
        select(OwnerTask)
        .where(OwnerTask.status.in_(statuses))
        .order_by(OwnerTask.status.asc(), OwnerTask.due_at.asc().nullslast(), OwnerTask.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
    )
    return list((await db.execute(stmt)).scalars().all())


async def create_owner_task(
    db: AsyncSession,
    *,
    title: str,
    description: Optional[str] = None,
    due_at: Optional[datetime] = None,
    created_by_phone_id: Optional[int] = None,
    commit: bool = True,
) -> OwnerTask:
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("La tarea necesita titulo.")
    now = datetime.now(timezone.utc)
    task = OwnerTask(
        title=clean_title[:240],
        description=(description or "").strip() or None,
        status=OWNER_TASK_STATUS_OPEN,
        due_at=due_at,
        created_by_phone_id=created_by_phone_id,
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(task)
    return task


async def set_owner_task_status(
    db: AsyncSession,
    *,
    task_id: int,
    status: str,
    commit: bool = True,
) -> Optional[OwnerTask]:
    if status not in {OWNER_TASK_STATUS_OPEN, OWNER_TASK_STATUS_DONE, OWNER_TASK_STATUS_CANCELED}:
        raise ValueError(f"Estado de tarea invalido: {status}")
    task = await db.get(OwnerTask, int(task_id))
    if task is None:
        return None
    task.status = status
    now = datetime.now(timezone.utc)
    task.completed_at = now if status == OWNER_TASK_STATUS_DONE else None
    task.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(task)
    return task
