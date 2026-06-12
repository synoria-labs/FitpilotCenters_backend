from types import SimpleNamespace

import pytest

from app.services.whatsapp_media_assets_service import MediaAssetError
from app.services.whatsapp_template_send_media import resolve_template_send_header_media


def _template(*, header_format="IMAGE", default_asset_id=None):
    components = [{"type": "BODY", "text": "Hola"}]
    if header_format:
        components.insert(0, {"type": "HEADER", "format": header_format})
    return SimpleNamespace(
        components=components,
        default_header_media_asset_id=default_asset_id,
    )


def _asset(asset_id, kind="image", status="active", url=None):
    return SimpleNamespace(
        id=asset_id,
        media_kind=kind,
        status=status,
        public_url=url or f"https://media.example.com/{asset_id}",
    )


@pytest.mark.asyncio
async def test_resolve_prefers_header_media_id_over_assets(monkeypatch):
    async def fail_get_asset(_db, _asset_id):
        raise AssertionError("asset lookup should not run when a media id is provided")

    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fail_get_asset,
    )

    result = await resolve_template_send_header_media(
        object(),
        template=_template(default_asset_id=1),
        override_media_asset_id=2,
        legacy_header_media_url="https://legacy.example.com/header.png",
        header_media_id="wamedia123",
    )

    assert result.media_format == "IMAGE"
    assert result.media_id == "wamedia123"
    assert result.media_url is None
    assert result.source == "id"


@pytest.mark.asyncio
async def test_resolve_uses_override_asset(monkeypatch):
    async def fake_get_asset(_db, asset_id):
        return _asset(asset_id, kind="image")

    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset,
    )

    result = await resolve_template_send_header_media(
        object(),
        template=_template(default_asset_id=1),
        override_media_asset_id=2,
    )

    assert result.media_url == "https://media.example.com/2"
    assert result.source == "override_asset"


@pytest.mark.asyncio
async def test_resolve_uses_template_default_asset(monkeypatch):
    async def fake_get_asset(_db, asset_id):
        return _asset(asset_id, kind="video")

    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset,
    )

    result = await resolve_template_send_header_media(
        object(),
        template=_template(header_format="VIDEO", default_asset_id=7),
    )

    assert result.media_format == "VIDEO"
    assert result.media_url == "https://media.example.com/7"
    assert result.source == "template_default_asset"


@pytest.mark.asyncio
async def test_resolve_uses_legacy_url_when_no_assets():
    result = await resolve_template_send_header_media(
        object(),
        template=_template(default_asset_id=None),
        legacy_header_media_url="https://cdn.example.com/header.png",
    )

    assert result.media_url == "https://cdn.example.com/header.png"
    assert result.source == "legacy_url"


@pytest.mark.asyncio
async def test_resolve_rejects_wrong_override_kind(monkeypatch):
    async def fake_get_asset(_db, asset_id):
        return _asset(asset_id, kind="audio")

    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset,
    )

    with pytest.raises(MediaAssetError, match="requiere image"):
        await resolve_template_send_header_media(
            object(),
            template=_template(header_format="IMAGE"),
            override_media_asset_id=3,
        )


@pytest.mark.asyncio
async def test_resolve_rejects_inactive_default_asset(monkeypatch):
    async def fake_get_asset(_db, asset_id):
        return _asset(asset_id, kind="image", status="archived")

    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset,
    )

    with pytest.raises(MediaAssetError, match="no esta activo"):
        await resolve_template_send_header_media(
            object(),
            template=_template(default_asset_id=4),
        )


@pytest.mark.asyncio
async def test_resolve_fails_when_required_media_has_no_source():
    with pytest.raises(MediaAssetError, match="requiere media de encabezado"):
        await resolve_template_send_header_media(
            object(),
            template=_template(default_asset_id=None),
        )


@pytest.mark.asyncio
async def test_resolve_rejects_media_for_text_only_template():
    with pytest.raises(MediaAssetError, match="no requiere media"):
        await resolve_template_send_header_media(
            object(),
            template=_template(header_format=None),
            legacy_header_media_url="https://cdn.example.com/header.png",
        )
