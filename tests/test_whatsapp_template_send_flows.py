from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.graphql.whatsapp.template_mutations import WhatsAppTemplateMutation
from app.services.notification_service import dispatch


class _FakeResult:
    def scalars(self):
        return self

    def first(self):
        return None


class _FakeDb:
    def add(self, _obj):
        return None

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, *_args, **_kwargs):
        return _FakeResult()


def _template(*, default_asset_id=77):
    return SimpleNamespace(
        id=10,
        template_name="renovacion",
        template_language="es_MX",
        template_status="APPROVED",
        meta_template_id="meta-10",
        default_header_media_asset_id=default_asset_id,
        components=[
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "Hola"},
        ],
    )


def _asset(asset_id):
    return SimpleNamespace(
        id=asset_id,
        media_kind="image",
        status="active",
        public_url=f"https://media.example.com/{asset_id}.png",
    )


def _message():
    return SimpleNamespace(
        id=1,
        conversation_id=2,
        contact_id=3,
        direction="outbound",
        message_type="template",
        text_content="Hola",
        timestamp=datetime.now(timezone.utc),
        wa_message_id="wamid.test",
    )


@pytest.mark.asyncio
async def test_send_template_test_uses_default_media_when_no_override(monkeypatch):
    captured = {}

    async def fake_get_template_model(_db, template_id):
        assert template_id == 10
        return _template(default_asset_id=77)

    async def fake_get_asset_model(_db, asset_id):
        assert asset_id == 77
        return _asset(asset_id)

    async def fake_send_template(**kwargs):
        captured.update(kwargs)
        return {"wa_message_id": "wamid.test"}

    monkeypatch.setattr(
        "app.graphql.whatsapp.template_mutations.crud.get_template_model",
        fake_get_template_model,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset_model,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.template_mutations.cloud.send_template",
        fake_send_template,
    )

    async def async_upsert_contact(*args, **kwargs):
        return SimpleNamespace(id=3, wa_id="5215555555555")

    async def async_get_or_open_conversation(*args, **kwargs):
        return SimpleNamespace(id=2)

    async def async_insert_outbound_message(*args, **kwargs):
        return _message()

    monkeypatch.setattr(
        "app.graphql.whatsapp.template_mutations.chat_crud.upsert_contact",
        async_upsert_contact,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.template_mutations.chat_crud.get_or_open_conversation",
        async_get_or_open_conversation,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.template_mutations.chat_crud.insert_outbound_message",
        async_insert_outbound_message,
    )

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb()))
    input_data = SimpleNamespace(
        phone="+52 1 555 555 5555",
        template_id=10,
        body_params=[],
        header_media_url=None,
        header_media_id=None,
        header_media_asset_id=None,
        header_text_param=None,
        button_url_param=None,
        location=None,
        carousel_card_overrides=None,
    )

    result = await WhatsAppTemplateMutation().send_template_test(info, input_data)

    assert result.success is True
    assert captured["header_media_url"] == "https://media.example.com/77.png"
    assert captured["header_media_id"] is None


@pytest.mark.asyncio
async def test_notification_dispatch_uses_override_media(monkeypatch):
    captured = {}

    await _run_dispatch(monkeypatch, captured, setting_asset_id=88, default_asset_id=77)

    assert captured["header_media_url"] == "https://media.example.com/88.png"


@pytest.mark.asyncio
async def test_notification_dispatch_uses_default_media_without_override(monkeypatch):
    captured = {}

    await _run_dispatch(monkeypatch, captured, setting_asset_id=None, default_asset_id=77)

    assert captured["header_media_url"] == "https://media.example.com/77.png"


async def _run_dispatch(monkeypatch, captured, *, setting_asset_id, default_asset_id):
    setting = SimpleNamespace(
        enabled=True,
        template_id=10,
        param_mapping=[],
        header_media_asset_id=setting_asset_id,
        header_media_url=None,
    )
    person = SimpleNamespace(
        id=1,
        full_name="Alejandro Martinez",
        phone_number="+52 1 555 555 5555",
        wa_id=None,
    )

    async def fake_get_setting_model(_db, event_type):
        return setting

    async def fake_get_template_model(_db, template_id):
        return _template(default_asset_id=default_asset_id)

    async def fake_get_asset_model(_db, asset_id):
        return _asset(asset_id)

    async def fake_claim_log(*args, **kwargs):
        return SimpleNamespace(id=99)

    async def fake_mark_log(*args, **kwargs):
        return SimpleNamespace(id=99)

    async def fake_upsert_contact(*args, **kwargs):
        return SimpleNamespace(id=3, wa_id="5215555555555")

    async def fake_get_or_open_conversation(*args, **kwargs):
        return SimpleNamespace(id=2)

    async def fake_send_template(**kwargs):
        captured.update(kwargs)
        return {"wa_message_id": "wamid.test"}

    async def fake_insert_outbound_message(*args, **kwargs):
        return _message()

    monkeypatch.setattr(
        "app.services.notification_service.crud.get_setting_model",
        fake_get_setting_model,
    )
    monkeypatch.setattr(
        "app.services.notification_service.templates_crud.get_template_model",
        fake_get_template_model,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_template_send_media.media_crud.get_asset_model",
        fake_get_asset_model,
    )
    monkeypatch.setattr("app.services.notification_service.crud.claim_log", fake_claim_log)
    monkeypatch.setattr("app.services.notification_service.crud.mark_log", fake_mark_log)
    monkeypatch.setattr("app.services.notification_service.chat_crud.upsert_contact", fake_upsert_contact)
    monkeypatch.setattr(
        "app.services.notification_service.chat_crud.get_or_open_conversation",
        fake_get_or_open_conversation,
    )
    monkeypatch.setattr("app.services.notification_service.cloud.send_template", fake_send_template)
    monkeypatch.setattr(
        "app.services.notification_service.chat_crud.insert_outbound_message",
        fake_insert_outbound_message,
    )

    async def fake_is_opted_out(*args, **kwargs):
        return False

    monkeypatch.setattr("app.services.notification_service._is_opted_out", fake_is_opted_out)

    outcome = await dispatch(
        _FakeDb(),
        event_type="renewal_confirmation",
        person=person,
        dedup_key="test-key",
    )

    assert outcome == "sent"
