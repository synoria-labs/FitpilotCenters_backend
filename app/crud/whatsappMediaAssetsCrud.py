"""CRUD helpers for reusable WhatsApp media assets."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WhatsAppMediaAsset


@dataclass
class WhatsAppMediaAssetData:
    id: int
    media_kind: str
    display_name: str
    original_filename: str
    mime_type: str
    file_ext: str
    file_size: int
    sha256: str
    storage_key: str
    public_url: str
    status: str
    sample_header_handle: Optional[str]
    sample_handle_generated_at: Optional[datetime]
    created_by_id: Optional[int]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_validated_at: Optional[datetime]

    @classmethod
    def from_model(cls, m: WhatsAppMediaAsset) -> "WhatsAppMediaAssetData":
        return cls(
            id=m.id,
            media_kind=m.media_kind,
            display_name=m.display_name,
            original_filename=m.original_filename,
            mime_type=m.mime_type,
            file_ext=m.file_ext,
            file_size=m.file_size,
            sha256=m.sha256,
            storage_key=m.storage_key,
            public_url=m.public_url,
            status=m.status,
            sample_header_handle=m.sample_header_handle,
            sample_handle_generated_at=m.sample_handle_generated_at,
            created_by_id=m.created_by_id,
            created_at=m.created_at,
            updated_at=m.updated_at,
            last_validated_at=m.last_validated_at,
        )


async def list_assets(
    db: AsyncSession,
    *,
    kind: Optional[str] = None,
    search: Optional[str] = None,
    status: Optional[str] = "active",
) -> List[WhatsAppMediaAssetData]:
    stmt = select(WhatsAppMediaAsset).order_by(WhatsAppMediaAsset.created_at.desc())
    if kind:
        stmt = stmt.where(WhatsAppMediaAsset.media_kind == kind.lower())
    if status:
        stmt = stmt.where(WhatsAppMediaAsset.status == status.lower())
    if search:
        term = f"%{search}%"
        stmt = stmt.where(
            WhatsAppMediaAsset.display_name.ilike(term)
            | WhatsAppMediaAsset.original_filename.ilike(term)
        )
    rows = (await db.execute(stmt)).scalars().all()
    return [WhatsAppMediaAssetData.from_model(r) for r in rows]


async def get_asset_model(
    db: AsyncSession, asset_id: Optional[int]
) -> Optional[WhatsAppMediaAsset]:
    if not asset_id:
        return None
    return await db.get(WhatsAppMediaAsset, asset_id)


async def get_asset_by_storage_key(
    db: AsyncSession, storage_key: str
) -> Optional[WhatsAppMediaAsset]:
    stmt = select(WhatsAppMediaAsset).where(WhatsAppMediaAsset.storage_key == storage_key)
    return (await db.execute(stmt)).scalars().first()


async def create_asset(
    db: AsyncSession,
    *,
    media_kind: str,
    display_name: str,
    original_filename: str,
    mime_type: str,
    file_ext: str,
    file_size: int,
    sha256: str,
    storage_key: str,
    public_url: str,
    created_by_id: Optional[int] = None,
    commit: bool = True,
) -> WhatsAppMediaAsset:
    now = datetime.utcnow()
    asset = WhatsAppMediaAsset(
        media_kind=media_kind.lower(),
        display_name=display_name,
        original_filename=original_filename,
        mime_type=mime_type,
        file_ext=file_ext,
        file_size=file_size,
        sha256=sha256,
        storage_key=storage_key,
        public_url=public_url,
        status="active",
        created_by_id=created_by_id,
        created_at=now,
        updated_at=now,
    )
    db.add(asset)
    await db.flush()
    if commit:
        await db.commit()
    return asset


async def make_asset_active(
    db: AsyncSession,
    asset: WhatsAppMediaAsset,
    *,
    public_url: Optional[str] = None,
    commit: bool = True,
) -> WhatsAppMediaAsset:
    changed = False
    if (asset.status or "").lower() != "active":
        asset.status = "active"
        changed = True
    if public_url and asset.public_url != public_url:
        asset.public_url = public_url
        changed = True
    if changed:
        asset.updated_at = datetime.utcnow()
        await db.flush()
        if commit:
            await db.commit()
    return asset


async def archive_asset(
    db: AsyncSession, asset_id: int, *, commit: bool = True
) -> Optional[WhatsAppMediaAsset]:
    asset = await get_asset_model(db, asset_id)
    if asset is None:
        return None
    asset.status = "archived"
    asset.updated_at = datetime.utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return asset


async def mark_validated(
    db: AsyncSession, asset: WhatsAppMediaAsset, *, commit: bool = True
) -> WhatsAppMediaAsset:
    asset.last_validated_at = datetime.utcnow()
    asset.updated_at = datetime.utcnow()
    await db.flush()
    if commit:
        await db.commit()
    return asset


async def store_sample_handle(
    db: AsyncSession,
    asset: WhatsAppMediaAsset,
    handle: str,
    *,
    commit: bool = True,
) -> WhatsAppMediaAsset:
    now = datetime.utcnow()
    asset.sample_header_handle = handle
    asset.sample_handle_generated_at = now
    asset.updated_at = now
    await db.flush()
    if commit:
        await db.commit()
    return asset
