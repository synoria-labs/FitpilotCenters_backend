from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.graphql.whatsapp.mutations import WhatsAppChatMutation
from app.services import whatsapp_cloud_service as cloud
from app.services.whatsapp_media_assets_service import validate_media


class _FakeDb:
    async def commit(self):
        return None

    async def rollback(self):
        return None


class _UploadStub:
    def __init__(self, filename: str, content_type: str, raw: bytes = b"voice-bytes"):
        self.filename = filename
        self.content_type = content_type
        self._raw = raw

    async def read(self) -> bytes:
        return self._raw


class _Response:
    status_code = 200
    text = '{"messages":[{"id":"wamid.voice"}]}'

    def json(self):
        return {"messages": [{"id": "wamid.voice"}]}


@pytest.mark.asyncio
async def test_cloud_send_media_sets_audio_voice_payload(monkeypatch):
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr(cloud.whatsapp_config.__class__, "PHONE_NUMBER_ID", "phone-123")
    monkeypatch.setattr(cloud.whatsapp_config.__class__, "ACCESS_TOKEN", "token-123")
    monkeypatch.setattr(cloud.httpx, "AsyncClient", FakeAsyncClient)

    result = await cloud.send_media(
        to="5215555555555",
        media_type="audio",
        media_id="media-123",
        voice=True,
    )

    assert result == {"wa_message_id": "wamid.voice"}
    assert captured["json"]["type"] == "audio"
    assert captured["json"]["audio"] == {"id": "media-123", "voice": True}


def test_validate_media_accepts_ogg_opus_voice_mime():
    validate_media("audio", b"voice-bytes", "audio/ogg; codecs=opus")


@pytest.mark.asyncio
async def test_send_media_message_sends_voice_note_as_ogg_opus(monkeypatch):
    captured: dict = {}

    async def fake_get_conversation(_db, _conversation_id):
        return SimpleNamespace(id=2, contact=SimpleNamespace(id=3, wa_id="5215555555555"))

    async def fake_upload_media(raw, mime_type, filename):
        captured["upload"] = {
            "raw": raw,
            "mime_type": mime_type,
            "filename": filename,
        }
        return "media-voice-1"

    async def fake_send_media(*_args, **kwargs):
        captured["send"] = kwargs
        return SimpleNamespace(ok=True, wa_message_id="wamid.voice")

    async def fake_store_media_bytes(raw, **kwargs):
        captured["store"] = {"raw": raw, **kwargs}
        return "/uploads/whatsapp/media-voice-1.ogg"

    async def fake_insert_outbound_message(*_args, **kwargs):
        captured["message"] = kwargs
        return SimpleNamespace(id=10)

    async def fake_insert_outbound_media(*_args, **kwargs):
        captured["media"] = kwargs
        return SimpleNamespace(id=11)

    async def fake_get_message_by_id(_db, _message_id):
        return SimpleNamespace(
            id=10,
            conversation_id=2,
            contact_id=3,
            direction="outbound",
            message_type="audio",
            text_content=None,
            timestamp=datetime.now(timezone.utc),
            wa_message_id="wamid.voice",
            context_message_id=None,
            media_url="/uploads/whatsapp/media-voice-1.ogg",
            media=SimpleNamespace(
                id=11,
                media_type="audio",
                mime_type="audio/ogg; codecs=opus",
                filename="note.ogg",
                caption=None,
                file_size=11,
                media_url="/uploads/whatsapp/media-voice-1.ogg",
                downloaded=True,
                download_failed=False,
            ),
        )

    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.get_conversation",
        fake_get_conversation,
    )
    monkeypatch.setattr("app.graphql.whatsapp.mutations.cloud.upload_media", fake_upload_media)
    monkeypatch.setattr("app.graphql.whatsapp.mutations.outbound.send_media", fake_send_media)
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.media_service.store_media_bytes",
        fake_store_media_bytes,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.insert_outbound_message",
        fake_insert_outbound_message,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.insert_outbound_media",
        fake_insert_outbound_media,
    )
    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.get_message_by_id",
        fake_get_message_by_id,
    )

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb()))
    input_data = SimpleNamespace(
        conversation_id=2,
        wa_id=None,
        caption="caption ignored",
        voice_note=True,
    )

    result = await WhatsAppChatMutation().send_media_message(
        info,
        input_data,
        _UploadStub("note.ogg", "audio/ogg"),
    )

    assert result.success is True
    assert result.message.message_type == "audio"
    assert captured["upload"]["mime_type"] == "audio/ogg; codecs=opus"
    assert captured["send"]["voice"] is True
    assert captured["send"]["caption"] is None
    assert captured["message"]["text"] is None
    assert captured["media"]["mime_type"] == "audio/ogg; codecs=opus"
    assert captured["media"]["caption"] is None


@pytest.mark.asyncio
async def test_send_media_message_rejects_non_ogg_voice_note(monkeypatch):
    async def fake_get_conversation(_db, _conversation_id):
        return SimpleNamespace(id=2, contact=SimpleNamespace(id=3, wa_id="5215555555555"))

    async def fail_upload_media(*_args, **_kwargs):
        raise AssertionError("invalid voice notes must not upload to Meta")

    monkeypatch.setattr(
        "app.graphql.whatsapp.mutations.crud.get_conversation",
        fake_get_conversation,
    )
    monkeypatch.setattr("app.graphql.whatsapp.mutations.cloud.upload_media", fail_upload_media)

    info = SimpleNamespace(context=SimpleNamespace(db=_FakeDb()))
    input_data = SimpleNamespace(
        conversation_id=2,
        wa_id=None,
        caption=None,
        voice_note=True,
    )

    result = await WhatsAppChatMutation().send_media_message(
        info,
        input_data,
        _UploadStub("note.mp3", "audio/mpeg"),
    )

    assert result.success is False
    assert "OGG/Opus" in result.error
