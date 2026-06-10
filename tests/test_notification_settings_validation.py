from types import SimpleNamespace

import pytest

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
