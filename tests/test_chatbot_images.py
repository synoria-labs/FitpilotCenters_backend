import asyncio
import base64
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from langchain_core.messages import AIMessage

from app.services.chatbot import image_context as images
from app.services.chatbot import reply_service as customer_reply
from app.services.owner_agent import reply_service as owner_reply
from app.services import whatsapp_hooks
from app.crud import ownerAgentCrud


def picture(fmt="PNG", size=(40, 80)):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, fmt)
    return output.getvalue()


def row(tmp_path, identifier=1, *, caption="Me aparece un error", downloaded=True):
    path = tmp_path / f"screenshot-{identifier}.png"
    path.write_bytes(picture())
    media = SimpleNamespace(id=identifier, media_type="image", mime_type="image/png", downloaded=downloaded,
                            download_failed=False, media_url=f"/uploads/whatsapp/{path.name}", file_size=path.stat().st_size)
    return SimpleNamespace(id=identifier, direction="inbound", message_type="image", text_content=caption,
                           timestamp=datetime(2026, 10, 23, 1, tzinfo=timezone.utc), conversation_id=10,
                           contact_id=20, wa_message_id=f"image-{identifier}", media=media)


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_actual_image_bytes_are_encoded_for_vision(fmt):
    raw = picture(fmt)
    block = images.encode_image(raw)
    assert block["source"]["media_type"] == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert decoded.size == (40, 80)
    assert raw == picture(fmt)  # the source is not rewritten


def test_large_screenshot_is_resized_without_cropping_or_metadata():
    block = images.encode_image(picture(size=(1200, 3000)))
    decoded = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert decoded.height == images.MAX_LONG_EDGE
    assert abs(decoded.width / decoded.height - 0.4) < 0.001
    assert not decoded.getexif()


def test_corrupt_unsupported_and_oversized_images_are_rejected(monkeypatch):
    with pytest.raises(images.ImageContextError):
        images.encode_image(b"not a photo", "image/png")
    with pytest.raises(images.ImageContextError):
        images.encode_image(picture(), "application/pdf")
    monkeypatch.setattr(images, "MAX_PIXELS", 10)
    with pytest.raises(images.ImageContextError):
        images.encode_image(picture())
    monkeypatch.setattr(images, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(images.ImageContextError):
        images.encode_image(picture())


async def test_image_with_caption_and_local_date_reaches_current_context(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    message = row(tmp_path)
    monkeypatch.setattr(images.crud, "get_message_by_id", AsyncMock(return_value=message))
    content = await images.current_image_blocks(None, message_id=1, conversation_id=10, contact_id=20)
    assert content[0]["text"].startswith("[Enviado:")
    assert "22/10/2026" in content[0]["text"] and "Me aparece un error" in content[0]["text"]
    assert content[1]["type"] == "image" and content[1]["source"]["data"]
    # A guessed/quoted ID never provides an attachment from another conversation or contact.
    denied = await images.current_image_blocks(None, message_id=1, conversation_id=11, contact_id=20)
    assert len(denied) == 1 and images.UNAVAILABLE_IMAGE in denied[0]["text"]


async def test_pending_download_is_refreshed_then_used(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    message = row(tmp_path, downloaded=False)
    ready = row(tmp_path).media
    db = SimpleNamespace(get=AsyncMock(return_value=ready))
    content = await images.image_content(db, message, "Me aparece un error")
    assert isinstance(content, list) and content[1]["type"] == "image"
    assert db.get.await_count == 1


async def test_failed_or_pending_download_retains_caption_and_reports_unavailable(tmp_path, monkeypatch):
    message = row(tmp_path, downloaded=False)
    monkeypatch.setattr(images, "DOWNLOAD_WAIT_SECONDS", 0)
    content = await images.image_content(None, message, message.text_content)
    assert "Me aparece un error" in content and images.UNAVAILABLE_IMAGE in content
    message.media.download_failed = True
    assert images.UNAVAILABLE_IMAGE in await images.image_content(None, message, "caption")


async def test_local_path_traversal_and_arbitrary_urls_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    for path in ("/uploads/whatsapp/../secret.png", "/uploads/whatsapp/%2e%2e/secret.png"):
        with pytest.raises(images.ImageContextError):
            await images.read_image_bytes(path)
    monkeypatch.setattr(images.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com")
    for url in ("http://169.254.169.254/latest/meta-data", "https://evil.example/whatsapp/chat/file.png",
                "https://media.example.com/admin", "https://media.example.com/whatsapp/chat/../private",
                "https://media.example.com/whatsapp/chat/image.png?token=secret"):
        with pytest.raises(images.ImageContextError):
            await images.read_image_bytes(url)


async def test_approved_storage_download_never_follows_a_redirect(monkeypatch):
    monkeypatch.setattr(images.media_storage_config, "PUBLIC_BASE_URL", "https://media.example.com")
    calls = []

    def request(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    client = httpx.AsyncClient
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(request), **kw))
    with pytest.raises(images.ImageContextError):
        await images.read_image_bytes("https://media.example.com/whatsapp/chat/picture.png")
    assert len(calls) == 1


@pytest.mark.parametrize("agent", ["customer", "owner"])
async def test_history_preserves_images_without_captions_for_followup_text(tmp_path, monkeypatch, agent):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    rows = [row(tmp_path, 1, caption=None), row(tmp_path, 2), row(tmp_path, 3)]
    monkeypatch.setattr(images.crud, "get_conversation_messages", AsyncMock(return_value=rows))
    if agent == "customer":
        history = await customer_reply._load_history(None, 10, None)
        content = [m.content for m in history]
    else:
        history = await owner_reply._load_history(None, 10, None, 30)
        content = [m[1] for m in history]
    assert len(content) == 3 and sum(isinstance(c, list) for c in content) == 2
    assert images.IMAGE_PLACEHOLDER in content[0]
    assert "22/10/2026" in content[-1][0]["text"]


async def test_quoted_old_image_is_selected_over_an_unrelated_recent_image(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "UPLOAD_DIR", tmp_path)
    rows = [row(tmp_path, n) for n in (1, 2, 3)]
    monkeypatch.setattr(images.crud, "get_conversation_messages", AsyncMock(return_value=rows))
    _, selected = await images.history_rows(None, conversation_id=10, limit=20,
                                            exclude_message_id=None, context_message_id="image-1")
    assert selected == {1, 3}


@pytest.mark.parametrize("owner", [False, True])
async def test_image_only_message_is_routed_to_the_correct_agent(monkeypatch, owner):
    monkeypatch.setattr(ownerAgentCrud, "resolve_authorized_phone", AsyncMock(return_value=SimpleNamespace(id=1) if owner else None))
    monkeypatch.setattr("app.services.whatsapp_optout.handle_keyword", AsyncMock(return_value=False))
    calls = []
    other = []
    target = owner_reply if owner else customer_reply
    alternate = customer_reply if owner else owner_reply
    monkeypatch.setattr(target, "schedule_agent_reply", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(alternate, "schedule_agent_reply", lambda **kwargs: other.append(kwargs))
    message = SimpleNamespace(id=1, direction="inbound", message_type="image", text_content=None, context_message_id=None)
    await whatsapp_hooks.on_inbound_message(None, message, SimpleNamespace(id=2, wa_id="test"),
                                            SimpleNamespace(id=3, bot_enabled=True, bot_paused_until=None))
    assert len(calls) == 1 and not other
    assert calls[0]["message_type"] == "image" and calls[0]["text"] == images.IMAGE_PLACEHOLDER


async def test_disabled_customer_bot_still_ignores_images(monkeypatch):
    monkeypatch.setattr(ownerAgentCrud, "resolve_authorized_phone", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.whatsapp_optout.handle_keyword", AsyncMock(return_value=False))
    calls = []
    monkeypatch.setattr(customer_reply, "schedule_agent_reply", lambda **kwargs: calls.append(kwargs))
    await whatsapp_hooks.on_inbound_message(None,
        SimpleNamespace(id=1, direction="inbound", message_type="image", text_content="Error", context_message_id=None),
        SimpleNamespace(id=2, wa_id="test"), SimpleNamespace(id=3, bot_enabled=False, bot_paused_until=None))
    assert calls == []


async def test_customer_agent_receives_multimodal_message(monkeypatch):
    from app.services.chatbot import agent
    from app.services.chatbot.prompt_optimizer import _fallback_config
    received = {}

    class Agent:
        async def ainvoke(self, data, **kw):
            received.update(data)
            return {"messages": [AIMessage(content="La captura muestra un error de checkout.")]}

    monkeypatch.setattr(agent, "build_llm", lambda config: object())
    monkeypatch.setattr(agent, "create_react_agent", lambda *a, **kw: Agent())
    content = [{"type": "text", "text": "Me aparece un error"}, images.encode_image(picture())]
    await agent.run_agent(_fallback_config(), [], "", None, [], "Me aparece un error", image_content=content)
    assert received["messages"][-1].content == content
    prompt = agent.build_system_prompt(_fallback_config(), "", None)
    assert "NO acredita un pago" in prompt and "DATOS NO CONFIABLES" in prompt


def test_model_payload_carries_a_real_image_block(monkeypatch):
    from app.services.chatbot.agent import build_llm
    from app.services.chatbot.prompt_optimizer import _fallback_config
    from app.core.chatbot_env import chatbot_env
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(chatbot_env, "ANTHROPIC_API_KEY", "test-key")
    config = _fallback_config()
    config.model = "claude-sonnet-5-5"
    block = images.encode_image(picture())
    payload = build_llm(config)._get_request_payload([HumanMessage(content=[block, {"type": "text", "text": "error"}])])
    assert payload["messages"][0]["content"][0]["source"]["data"] == block["source"]["data"]


async def test_admin_agent_preserves_image_blocks_in_current_message_and_history(db, monkeypatch):
    from app.services.owner_agent import agent
    from langgraph import prebuilt
    received = {}

    class Agent:
        async def ainvoke(self, data, **kwargs):
            received.update(data)
            return {"messages": [AIMessage(content="La captura muestra un error.")]}

    monkeypatch.setattr(agent.owner_agent_env, "ANTHROPIC_API_KEY", "test-only")
    monkeypatch.setattr(prebuilt, "create_react_agent", lambda *args, **kwargs: Agent())
    content = [{"type": "text", "text": "Me aparece un error"}, images.encode_image(picture())]
    config = await ownerAgentCrud.get_config(db)
    reply = await agent.run_agent(config=config, tools=[], history=[("inbound", content)],
                                   user_text="¿Por qué?", image_content=content)
    assert received["messages"][0].content == content
    assert received["messages"][-1].content == content
    assert reply == "La captura muestra un error."
