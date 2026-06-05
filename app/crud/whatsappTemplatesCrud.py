"""CRUD for the local mirror of Meta WhatsApp message templates.

These helpers only touch the ``app.whatsapp_templates`` table. The actual create/edit/delete
against Meta (and approval status) is orchestrated by the GraphQL mutations, which call the
Business Management API client and then persist the result here. Primary keys are assigned by
the database; callers commit (``commit=True`` by default for convenience).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Optional

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WhatsAppTemplate

logger = logging.getLogger(__name__)


@dataclass
class WhatsAppTemplateData:
    id: int
    template_name: str
    template_namespace: str
    template_language: str
    template_status: str
    category: Optional[str]
    meta_template_id: Optional[str]
    components: Optional[list]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: WhatsAppTemplate) -> "WhatsAppTemplateData":
        return cls(
            id=m.id,
            template_name=m.template_name,
            template_namespace=m.template_namespace,
            template_language=m.template_language,
            template_status=m.template_status,
            category=m.category,
            meta_template_id=m.meta_template_id,
            components=m.components,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def list_templates(
    db: AsyncSession, search: Optional[str] = None
) -> List[WhatsAppTemplateData]:
    stmt = select(WhatsAppTemplate).order_by(WhatsAppTemplate.template_name)
    if search:
        stmt = stmt.where(WhatsAppTemplate.template_name.ilike(f"%{search}%"))
    rows = (await db.execute(stmt)).scalars().all()
    return [WhatsAppTemplateData.from_model(r) for r in rows]


async def get_template(db: AsyncSession, template_id: int) -> Optional[WhatsAppTemplateData]:
    model = await get_template_model(db, template_id)
    return WhatsAppTemplateData.from_model(model) if model else None


async def get_template_model(
    db: AsyncSession, template_id: int
) -> Optional[WhatsAppTemplate]:
    stmt = select(WhatsAppTemplate).where(WhatsAppTemplate.id == template_id)
    return (await db.execute(stmt)).scalars().first()


async def get_by_name_language(
    db: AsyncSession, name: str, language: str
) -> Optional[WhatsAppTemplate]:
    stmt = select(WhatsAppTemplate).where(
        WhatsAppTemplate.template_name == name,
        WhatsAppTemplate.template_language == language,
    )
    return (await db.execute(stmt)).scalars().first()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
async def create_local(
    db: AsyncSession,
    *,
    name: str,
    namespace: str,
    language: str,
    status: str,
    category: Optional[str],
    components: Optional[list],
    meta_template_id: Optional[str],
    commit: bool = True,
) -> WhatsAppTemplate:
    now = datetime.utcnow()
    tpl = WhatsAppTemplate(
        template_name=name,
        template_namespace=namespace,
        template_language=language,
        template_status=status,
        category=category,
        meta_template_id=meta_template_id,
        components=components,
        created_at=now,
        updated_at=now,
    )
    db.add(tpl)
    await db.flush()
    if commit:
        await db.commit()
    return tpl


async def update_local(
    db: AsyncSession,
    template_id: int,
    *,
    status: Optional[str] = None,
    category: Optional[str] = None,
    components: Optional[list] = None,
    meta_template_id: Optional[str] = None,
    commit: bool = True,
) -> Optional[WhatsAppTemplate]:
    tpl = await get_template_model(db, template_id)
    if tpl is None:
        return None
    if status is not None:
        tpl.template_status = status
    if category is not None:
        tpl.category = category
    if components is not None:
        tpl.components = components
    if meta_template_id is not None:
        tpl.meta_template_id = meta_template_id
    tpl.updated_at = datetime.utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return tpl


async def delete_local(
    db: AsyncSession, template_id: int, commit: bool = True
) -> bool:
    tpl = await get_template_model(db, template_id)
    if tpl is None:
        return False
    await db.delete(tpl)
    await db.flush()
    if commit:
        await db.commit()
    return True


async def upsert_from_meta(
    db: AsyncSession,
    *,
    name: str,
    language: str,
    namespace: str,
    status: str,
    category: Optional[str],
    components: Optional[list],
    meta_template_id: Optional[str],
    commit: bool = True,
) -> WhatsAppTemplate:
    """Insert or update a local row to match what Meta reports (used by sync)."""
    tpl = await get_by_name_language(db, name, language)
    now = datetime.utcnow()
    if tpl is None:
        tpl = WhatsAppTemplate(
            template_name=name,
            template_namespace=namespace,
            template_language=language,
            template_status=status,
            category=category,
            meta_template_id=meta_template_id,
            components=components,
            created_at=now,
            updated_at=now,
        )
        db.add(tpl)
    else:
        tpl.template_namespace = namespace or tpl.template_namespace
        tpl.template_status = status
        tpl.category = category
        tpl.components = components
        if meta_template_id:
            tpl.meta_template_id = meta_template_id
        tpl.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
    return tpl
