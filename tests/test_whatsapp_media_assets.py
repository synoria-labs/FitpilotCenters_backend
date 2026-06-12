from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import whatsapp_media_assets_service as service
from app.services.whatsapp_media_assets_service import (
    MediaAssetError,
    assert_asset_matches_header,
    upload_asset,
    validate_media,
)


def test_validate_media_accepts_supported_image():
    validate_media("image", b"fake-image-bytes", "image/png")


def test_validate_media_rejects_audio_as_image():
    with pytest.raises(MediaAssetError, match="MIME no soportado"):
        validate_media("image", b"fake-audio-bytes", "audio/mpeg")


def test_assert_asset_matches_header_rejects_audio_template_header():
    class Asset:
        media_kind = "audio"
        status = "active"

    with pytest.raises(MediaAssetError, match="requiere image"):
        assert_asset_matches_header(Asset(), "IMAGE")


class UploadFileStub:
    filename = "header.png"
    content_type = "image/png"

    async def read(self):
        return b"same-image-content"


class DbStub:
    def __init__(self):
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_upload_asset_reuses_existing_asset(monkeypatch):
    existing = SimpleNamespace(
        id=10,
        media_kind="image",
        status="active",
        public_url="https://media.example.com/old.png",
    )
    db = DbStub()

    monkeypatch.setattr(
        service.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com"
    )

    async def fake_get_asset_by_storage_key(_db, storage_key):
        assert storage_key.startswith("whatsapp/templates/")
        return existing

    async def fail_put_object(*_args, **_kwargs):
        raise AssertionError("existing uploads should not write to storage again")

    async def fake_make_asset_active(_db, asset, *, public_url, commit=True):
        asset.public_url = public_url
        return asset

    monkeypatch.setattr(
        service.crud, "get_asset_by_storage_key", fake_get_asset_by_storage_key
    )
    monkeypatch.setattr(service.crud, "make_asset_active", fake_make_asset_active)
    monkeypatch.setattr(service, "_put_object", fail_put_object)

    result = await upload_asset(db, file=UploadFileStub(), kind="image")

    assert result is existing
    assert result.public_url.startswith("https://media.example.com/whatsapp/templates/")
    assert db.rolled_back is False


@pytest.mark.asyncio
async def test_upload_asset_restores_archived_duplicate(monkeypatch):
    existing = SimpleNamespace(
        id=11,
        media_kind="image",
        status="archived",
        public_url="https://media.example.com/old.png",
    )

    monkeypatch.setattr(
        service.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com"
    )

    async def fake_get_asset_by_storage_key(_db, _storage_key):
        return existing

    async def fake_make_asset_active(_db, asset, *, public_url, commit=True):
        asset.status = "active"
        asset.public_url = public_url
        return asset

    monkeypatch.setattr(
        service.crud, "get_asset_by_storage_key", fake_get_asset_by_storage_key
    )
    monkeypatch.setattr(service.crud, "make_asset_active", fake_make_asset_active)

    result = await upload_asset(DbStub(), file=UploadFileStub(), kind="image")

    assert result.status == "active"
    assert result.public_url.startswith("https://media.example.com/whatsapp/templates/")


@pytest.mark.asyncio
async def test_upload_asset_rejects_duplicate_with_different_kind(monkeypatch):
    existing = SimpleNamespace(
        id=12,
        media_kind="document",
        status="active",
        public_url="https://media.example.com/file.png",
    )

    monkeypatch.setattr(
        service.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com"
    )

    async def fake_get_asset_by_storage_key(_db, _storage_key):
        return existing

    monkeypatch.setattr(
        service.crud, "get_asset_by_storage_key", fake_get_asset_by_storage_key
    )

    with pytest.raises(MediaAssetError, match="ya existe como document"):
        await upload_asset(DbStub(), file=UploadFileStub(), kind="image")


@pytest.mark.asyncio
async def test_upload_asset_recovers_from_unique_race(monkeypatch):
    existing = SimpleNamespace(
        id=13,
        media_kind="image",
        status="active",
        public_url="https://media.example.com/file.png",
    )
    db = DbStub()
    lookups = 0

    monkeypatch.setattr(
        service.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com"
    )

    async def fake_get_asset_by_storage_key(_db, _storage_key):
        nonlocal lookups
        lookups += 1
        return existing if lookups == 2 else None

    async def fake_put_object(*_args, **_kwargs):
        return None

    async def duplicate_create_asset(*_args, **_kwargs):
        raise IntegrityError("insert", {}, Exception("duplicate storage_key"))

    async def fake_make_asset_active(_db, asset, *, public_url, commit=True):
        asset.public_url = public_url
        return asset

    monkeypatch.setattr(
        service.crud, "get_asset_by_storage_key", fake_get_asset_by_storage_key
    )
    monkeypatch.setattr(service.crud, "create_asset", duplicate_create_asset)
    monkeypatch.setattr(service.crud, "make_asset_active", fake_make_asset_active)
    monkeypatch.setattr(service, "_put_object", fake_put_object)

    result = await upload_asset(db, file=UploadFileStub(), kind="image")

    assert result is existing
    assert db.rolled_back is True
    assert lookups == 2
