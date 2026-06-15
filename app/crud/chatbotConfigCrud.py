"""CRUD for the chatbot configuration (single editable row).

Reads/writes ``app.chatbot_config`` — the system prompt + business info + toggles + model
that the business edits from the desktop frontend. The agent reads it on every inbound turn
so changes apply without a redeploy.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChatbotConfig


@dataclass
class ChatbotConfigData:
    id: Optional[int]
    enabled: bool
    require_confirmation: bool
    require_mp_payment: bool
    model: str
    system_prompt: Optional[str]
    business_name: Optional[str]
    address: Optional[str]
    operating_hours: Optional[str]
    phone: Optional[str]
    policies: Optional[str]
    tone: Optional[str]
    extra_info: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: ChatbotConfig) -> "ChatbotConfigData":
        return cls(
            id=m.id,
            enabled=bool(m.enabled),
            require_confirmation=bool(m.require_confirmation),
            require_mp_payment=bool(m.require_mp_payment),
            model=m.model,
            system_prompt=m.system_prompt,
            business_name=m.business_name,
            address=m.address,
            operating_hours=m.operating_hours,
            phone=m.phone,
            policies=m.policies,
            tone=m.tone,
            extra_info=m.extra_info,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


async def get_config_model(db: AsyncSession) -> Optional[ChatbotConfig]:
    """Return the single config row (lowest id), or None if not seeded yet."""
    stmt = select(ChatbotConfig).order_by(ChatbotConfig.id.asc()).limit(1)
    return (await db.execute(stmt)).scalars().first()


async def get_config(db: AsyncSession) -> Optional[ChatbotConfigData]:
    model = await get_config_model(db)
    return ChatbotConfigData.from_model(model) if model else None


# Fields the save mutation may set. Mirrors the editable columns.
_EDITABLE_FIELDS = (
    "enabled",
    "require_confirmation",
    "require_mp_payment",
    "model",
    "system_prompt",
    "business_name",
    "address",
    "operating_hours",
    "phone",
    "policies",
    "tone",
    "extra_info",
)


async def upsert_config(db: AsyncSession, *, commit: bool = True, **fields) -> ChatbotConfig:
    """Create-or-update the single config row with the provided fields.

    Only keys in ``_EDITABLE_FIELDS`` are applied; ``None`` values are skipped so a
    partial save never clears unrelated columns.
    """
    config = await get_config_model(db)
    now = datetime.now(timezone.utc)
    applied = {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS and v is not None}

    if config is None:
        config = ChatbotConfig(created_at=now, updated_at=now, **applied)
        db.add(config)
    else:
        for key, value in applied.items():
            setattr(config, key, value)
        config.updated_at = now

    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(config)
    return config
