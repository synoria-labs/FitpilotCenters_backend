from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.crud import whatsappTemplatesCrud as crud


class _FakeDb:
    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_upsert_from_meta_preserves_default_header_media_asset_id(monkeypatch):
    template = SimpleNamespace(
        template_name="renovacion",
        template_namespace="old",
        template_language="es_MX",
        template_status="PENDING",
        category="MARKETING",
        components=[{"type": "BODY", "text": "old"}],
        meta_template_id="meta-old",
        default_header_media_asset_id=44,
        updated_at=datetime.now(timezone.utc),
    )

    async def fake_get_by_name_language(_db, name, language):
        assert name == "renovacion"
        assert language == "es_MX"
        return template

    monkeypatch.setattr(crud, "get_by_name_language", fake_get_by_name_language)

    updated = await crud.upsert_from_meta(
        _FakeDb(),
        name="renovacion",
        language="es_MX",
        namespace="new",
        status="APPROVED",
        category="MARKETING",
        components=[{"type": "BODY", "text": "new"}],
        meta_template_id="meta-new",
        commit=False,
    )

    assert updated.default_header_media_asset_id == 44
    assert updated.template_status == "APPROVED"
    assert updated.meta_template_id == "meta-new"
