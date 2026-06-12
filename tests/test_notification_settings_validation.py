from types import SimpleNamespace

import pytest

from app.crud.notificationsCrud import NotificationSettingData
from app.graphql.notifications.mutations import NotificationSettingsMutation
from app.models.notificationModel import EVENT_RENEWAL_CONFIRMATION


def _info():
    return SimpleNamespace(context=SimpleNamespace(db=object()))


def _input(**overrides):
    values = {
        "event_type": EVENT_RENEWAL_CONFIRMATION,
        "enabled": True,
        "template_id": 10,
        "param_mapping": ["member_first_name"],
        "header_media_url": None,
        "header_media_asset_id": None,
        "offsets_days": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_save_notification_setting_rejects_unsynced_template(monkeypatch):
    async def fake_get_template_model(_db, _template_id):
        return SimpleNamespace(
            meta_template_id=None,
            template_status="APPROVED",
            components=[{"type": "BODY", "text": "Hola {{1}}"}],
        )

    monkeypatch.setattr(
        "app.graphql.notifications.mutations.templates_crud.get_template_model",
        fake_get_template_model,
    )

    result = await NotificationSettingsMutation().save_notification_setting(_info(), _input())

    assert result.success is False
    assert "sincronizada" in result.error


@pytest.mark.asyncio
async def test_save_notification_setting_requires_header_media_url(monkeypatch):
    async def fake_get_template_model(_db, _template_id):
        return SimpleNamespace(
            meta_template_id="1608879749764787",
            template_status="APPROVED",
            default_header_media_asset_id=None,
            components=[
                {"type": "HEADER", "format": "IMAGE"},
                {"type": "BODY", "text": "Hola {{1}}"},
            ],
        )

    monkeypatch.setattr(
        "app.graphql.notifications.mutations.templates_crud.get_template_model",
        fake_get_template_model,
    )

    result = await NotificationSettingsMutation().save_notification_setting(_info(), _input())

    assert result.success is False
    assert "media de encabezado" in result.error


@pytest.mark.asyncio
async def test_save_notification_setting_accepts_template_default_header_media(monkeypatch):
    captured = {}

    async def fake_get_template_model(_db, _template_id):
        return SimpleNamespace(
            id=10,
            template_name="renovacion",
            template_namespace="",
            template_language="es_MX",
            template_status="APPROVED",
            category="MARKETING",
            meta_template_id="1608879749764787",
            default_header_media_asset_id=44,
            components=[
                {"type": "HEADER", "format": "IMAGE"},
                {"type": "BODY", "text": "Hola {{1}}"},
            ],
            created_at=None,
            updated_at=None,
        )

    async def fake_get_asset_model(_db, asset_id):
        assert asset_id == 44
        return SimpleNamespace(
            id=44,
            media_kind="image",
            status="active",
            public_url="https://media.example.com/default.png",
        )

    async def fake_upsert_setting(_db, **kwargs):
        captured.update(kwargs)

    async def fake_get_setting(_db, event_type):
        return NotificationSettingData(
            id=1,
            event_type=event_type,
            enabled=True,
            template_id=10,
            param_mapping=["member_first_name"],
            header_media_url=None,
            header_media_asset_id=None,
            offsets_days=[],
            created_at=None,
            updated_at=None,
        )

    async def fake_get_template(_db, _template_id):
        model = await fake_get_template_model(_db, _template_id)
        return SimpleNamespace(
            id=model.id,
            template_name=model.template_name,
            template_namespace=model.template_namespace,
            template_language=model.template_language,
            template_status=model.template_status,
            category=model.category,
            meta_template_id=model.meta_template_id,
            default_header_media_asset_id=model.default_header_media_asset_id,
            components=model.components,
            created_at=None,
            updated_at=None,
        )

    monkeypatch.setattr(
        "app.graphql.notifications.mutations.templates_crud.get_template_model",
        fake_get_template_model,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset_model,
    )
    monkeypatch.setattr(
        "app.graphql.notifications.mutations.crud.upsert_setting",
        fake_upsert_setting,
    )
    monkeypatch.setattr(
        "app.graphql.notifications.mutations.crud.get_setting",
        fake_get_setting,
    )
    monkeypatch.setattr(
        "app.graphql.notifications.mutations.templates_crud.get_template",
        fake_get_template,
    )

    result = await NotificationSettingsMutation().save_notification_setting(_info(), _input())

    assert result.success is True
    assert captured["header_media_asset_id"] is None
    assert captured["header_media_url"] is None
