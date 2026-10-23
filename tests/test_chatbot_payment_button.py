"""The chatbot hands over the Mercado Pago link as a «Pagar ahora» button, not as a raw URL.

The link went out pasted at the end of the reply text. Now ``finalize_reply`` leaves it in
``ctx.payment_link`` and the reply service sends a WhatsApp interactive «cta_url» message
(text + one button), persisted with its button so the desktop chat draws it. When the button
can't be sent, the link goes back into the text: the customer must always be able to pay.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.crud import whatsappCrud as chat_crud
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_outbound as outbound
from app.services.chatbot import reply_service
from app.services.chatbot.tools import PaymentLink

WA_ID = "5218710000088"
LINK = PaymentLink("https://www.mercadopago.com.mx/checkout/v1/redirect?pref_id=123", "Para completar tu compra:")


# ----------------------------------------------------------------------------
# WhatsApp payload
# ----------------------------------------------------------------------------
async def test_cta_url_payload_is_an_interactive_message_with_one_url_button(monkeypatch):
    sent = AsyncMock(return_value={"wa_message_id": "wamid.cta"})
    monkeypatch.setattr(cloud, "_post_message", sent)

    result = await cloud.send_cta_url(to=WA_ID, body="Pase diario $50", button_text="Pagar ahora", url=LINK.url)

    assert result == {"wa_message_id": "wamid.cta"}
    payload = sent.await_args.args[0]
    assert payload["type"] == "interactive"
    assert payload["interactive"] == {
        "type": "cta_url",
        "body": {"text": "Pase diario $50"},
        "action": {"name": "cta_url", "parameters": {"display_text": "Pagar ahora", "url": LINK.url}},
    }


@pytest.mark.parametrize(
    "body,button_text,url",
    [
        ("x" * (cloud.CTA_BODY_MAX + 1), "Pagar ahora", LINK.url),
        ("Pase diario", "Pagar con Mercado Pago", LINK.url),  # 22 > 20 characters
        ("Pase diario", "Pagar ahora", "mercadopago.com.mx/checkout"),
    ],
)
async def test_cta_url_rejects_what_meta_would_reject_before_calling_it(monkeypatch, body, button_text, url):
    sent = AsyncMock()
    monkeypatch.setattr(cloud, "_post_message", sent)

    with pytest.raises(cloud.WhatsAppError):
        await cloud.send_cta_url(to=WA_ID, body=body, button_text=button_text, url=url)
    sent.assert_not_awaited()


# ----------------------------------------------------------------------------
# Gateway + persistence: the chat gets the button
# ----------------------------------------------------------------------------
async def test_the_button_is_persisted_with_the_message_for_the_chat(db, monkeypatch):
    monkeypatch.setattr(cloud, "send_cta_url", AsyncMock(return_value={"wa_message_id": "wamid.cta_saved"}))
    contact = await chat_crud.upsert_contact(db, wa_id=WA_ID, phone_number=WA_ID)
    conversation = await chat_crud.get_or_open_conversation(db, contact.id)
    await db.flush()

    result = await outbound.send_cta_url(
        db, kind=outbound.KIND_CHATBOT_REPLY, conversation_id=conversation.id, contact_id=contact.id,
        wa_id=WA_ID, body="Pase diario $50\nPara completar tu compra:", button_text="Pagar ahora", url=LINK.url,
    )

    assert result.ok
    message = await chat_crud.get_message_by_id(db, result.message_id)
    assert message.message_type == "interactive"
    assert message.text_content == "Pase diario $50\nPara completar tu compra:"
    assert [(b.type, b.text, b.url) for b in message.template_buttons] == [("URL", "Pagar ahora", LINK.url)]


# ----------------------------------------------------------------------------
# Reply service: button first, never without a way to pay
# ----------------------------------------------------------------------------
def _result(status=outbound.SendStatus.SENT, reason=None):
    return outbound.OutboundResult(status=status, reason=reason)


async def _reply(db, text):
    return await reply_service.send_and_persist_reply(
        db, conversation_id=987654, contact_id=1, to_wa_id=WA_ID, text=text, payment_link=LINK,
    )


async def test_reply_goes_out_with_the_pagar_ahora_button(db, monkeypatch):
    cta = AsyncMock(return_value=_result())
    text = AsyncMock(return_value=_result())
    monkeypatch.setattr(reply_service.outbound, "send_cta_url", cta)
    monkeypatch.setattr(reply_service.outbound, "send_text", text)

    await _reply(db, "Pase diario para lunes 26/10/2026 20:00.\nPase diario $50\n")

    cta.assert_awaited_once()
    kwargs = cta.await_args.kwargs
    assert kwargs["body"] == (
        "Pase diario para lunes 26/10/2026 20:00.\nPase diario $50\n"
        "Para completar tu compra, toca el botón «Pagar ahora» 👇"
    )
    assert kwargs["button_text"] == "Pagar ahora" and kwargs["url"] == LINK.url
    text.assert_not_awaited()


async def test_a_reply_too_long_for_a_button_goes_first_and_the_button_after(db, monkeypatch):
    cta = AsyncMock(return_value=_result())
    text = AsyncMock(return_value=_result())
    monkeypatch.setattr(reply_service.outbound, "send_cta_url", cta)
    monkeypatch.setattr(reply_service.outbound, "send_text", text)
    long_reply = "Tu paquete incluye todas las clases del horario. " * 30

    await _reply(db, long_reply)

    assert text.await_args.kwargs["text"] == long_reply.strip()
    assert cta.await_args.kwargs["body"] == "Para completar tu compra, toca el botón «Pagar ahora» 👇"


async def test_if_the_button_fails_the_link_is_written_into_the_text(db, monkeypatch):
    cta = AsyncMock(return_value=_result(outbound.SendStatus.FAILED, "(#131009) Parameter value is not valid"))
    text = AsyncMock(return_value=_result())
    monkeypatch.setattr(reply_service.outbound, "send_cta_url", cta)
    monkeypatch.setattr(reply_service.outbound, "send_text", text)

    await _reply(db, "Pase diario $50")

    assert text.await_args.kwargs["text"] == f"Pase diario $50\nPara completar tu compra: {LINK.url}"


async def test_outside_the_window_the_button_is_not_retried_as_text(db, monkeypatch):
    """A deferred send (24 h window closed) would fail as text too: it is reported, not retried."""
    cta = AsyncMock(return_value=_result(outbound.SendStatus.DEFERRED, "outside_window"))
    text = AsyncMock()
    monkeypatch.setattr(reply_service.outbound, "send_cta_url", cta)
    monkeypatch.setattr(reply_service.outbound, "send_text", text)

    result = await _reply(db, "Pase diario $50")

    assert result.status is outbound.SendStatus.DEFERRED
    text.assert_not_awaited()


async def test_replies_without_a_payment_link_are_plain_text(db, monkeypatch):
    cta = AsyncMock()
    text = AsyncMock(return_value=_result())
    monkeypatch.setattr(reply_service.outbound, "send_cta_url", cta)
    monkeypatch.setattr(reply_service.outbound, "send_text", text)

    await reply_service.send_and_persist_reply(
        db, conversation_id=987654, contact_id=1, to_wa_id=WA_ID, text="¡Hola! ¿En qué te ayudo?",
    )

    cta.assert_not_awaited()
    assert text.await_args.kwargs["text"] == "¡Hola! ¿En qué te ayudo?"
