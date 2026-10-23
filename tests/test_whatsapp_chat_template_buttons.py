"""A template sent from FitPilot shows its buttons in the desktop chat, like on the phone.

Real gap this covers: a send persists the rendered text (``text_content``), the header media
and ``template_id`` — but not the buttons, which only live in the template's ``components``.
The chat read paths never looked them up and the schema had no field for them, so the chat
drew a template bubble without the buttons the member actually saw under it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.crud import whatsappCrud as chat_crud
from app.graphql.schema import schema
from app.models import WhatsAppTemplate

WA_ID = "5218710000077"

COMPONENTS = [
    {"type": "HEADER", "format": "IMAGE"},
    {"type": "BODY", "text": "Hola {{1}}, te extrañamos."},
    {
        "type": "BUTTONS",
        "buttons": [
            {"type": "QUICK_REPLY", "text": "Quiero regresar"},
            {"type": "URL", "text": "Ver planes", "url": "https://example.com/planes/{{1}}"},
            {"type": "PHONE_NUMBER", "text": "Llamar al gym", "phone_number": "+528711234567"},
        ],
    },
]


def test_template_buttons_data_reads_the_top_level_buttons_in_order():
    buttons = chat_crud.template_buttons_data(COMPONENTS)

    assert [(b.type, b.text) for b in buttons] == [
        ("QUICK_REPLY", "Quiero regresar"),
        ("URL", "Ver planes"),
        ("PHONE_NUMBER", "Llamar al gym"),
    ]
    assert buttons[0].url is None
    assert buttons[1].url == "https://example.com/planes/{{1}}"
    assert buttons[2].phone_number == "+528711234567"


def test_template_buttons_data_skips_carousel_card_buttons_and_malformed_entries():
    carousel = [
        {"type": "BODY", "text": "Nuestras clases"},
        {
            "type": "CAROUSEL",
            "cards": [
                {"components": [{"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Card"}]}]}
            ],
        },
    ]
    assert chat_crud.template_buttons_data(carousel) == []
    assert chat_crud.template_buttons_data(None) == []
    assert chat_crud.template_buttons_data(
        [{"type": "BUTTONS", "buttons": ["oops", {"text": "sin tipo"}]}]
    ) == []


async def _conversation(db):
    contact = await chat_crud.upsert_contact(db, wa_id=WA_ID, phone_number=WA_ID)
    conversation = await chat_crud.get_or_open_conversation(db, contact.id)
    await db.flush()
    return contact, conversation


async def _template(db, components=COMPONENTS, name="chat_template_buttons_test"):
    template = WhatsAppTemplate(
        template_name=name,
        template_namespace="ns",
        template_language="es_MX",
        template_status="APPROVED",
        category="MARKETING",
        components=components,
    )
    db.add(template)
    await db.flush()
    return template


async def _send(db, contact, conversation, *, template_id=None, wamid="wamid.chat_tpl_buttons"):
    message = await chat_crud.insert_outbound_message(
        db,
        conversation_id=conversation.id,
        contact_id=contact.id,
        text="Hola Ana, te extrañamos.",
        wa_message_id=wamid,
        message_type="template" if template_id else "text",
        template_id=template_id,
    )
    await db.flush()
    return message


@pytest.mark.asyncio
async def test_thread_and_realtime_reads_carry_the_template_buttons(db):
    contact, conversation = await _conversation(db)
    template = await _template(db)
    sent = await _send(db, contact, conversation, template_id=template.id, wamid="wamid.tpl_1")
    plain = await _send(db, contact, conversation, wamid="wamid.plain_1")

    thread = {m.id: m for m in await chat_crud.get_conversation_messages(db, conversation.id)}
    assert [b.text for b in thread[sent.id].template_buttons] == [
        "Quiero regresar", "Ver planes", "Llamar al gym",
    ]
    assert thread[plain.id].template_buttons == []

    # The realtime subscription loads new messages one by one through this path.
    single = await chat_crud.get_message_by_id(db, sent.id)
    assert [b.type for b in single.template_buttons] == ["QUICK_REPLY", "URL", "PHONE_NUMBER"]


@pytest.mark.asyncio
async def test_conversation_list_preview_carries_the_template_buttons(db):
    contact, conversation = await _conversation(db)
    template = await _template(db)
    await _send(db, contact, conversation, template_id=template.id, wamid="wamid.tpl_list")

    latest = await chat_crud._latest_message_per_conversation(db, [conversation.id])

    assert [b.text for b in latest[conversation.id].template_buttons] == [
        "Quiero regresar", "Ver planes", "Llamar al gym",
    ]


@pytest.mark.asyncio
async def test_a_template_deleted_since_the_send_just_shows_no_buttons(db):
    contact, conversation = await _conversation(db)
    template = await _template(db)
    sent = await _send(db, contact, conversation, template_id=template.id, wamid="wamid.tpl_gone")
    await db.delete(template)
    await db.flush()

    single = await chat_crud.get_message_by_id(db, sent.id)

    assert single.template_id == template.id
    assert single.template_buttons == []


@pytest.mark.asyncio
async def test_graphql_exposes_template_buttons_on_chat_messages(db):
    """The desktop client asks for exactly this selection."""
    contact, conversation = await _conversation(db)
    template = await _template(db)
    await _send(db, contact, conversation, template_id=template.id, wamid="wamid.tpl_gql")
    ctx = SimpleNamespace(
        db=db,
        user=SimpleNamespace(roles=[SimpleNamespace(role=SimpleNamespace(code="admin"), role_id=None)]),
    )

    result = await schema.execute(
        """
        query($id: Int!) {
            conversationMessages(conversationId: $id) {
                messageType
                templateButtons { type text url phoneNumber }
            }
        }
        """,
        variable_values={"id": conversation.id},
        context_value=ctx,
    )

    assert result.errors is None, result.errors
    [message] = result.data["conversationMessages"]
    assert message["messageType"] == "template"
    assert message["templateButtons"] == [
        {"type": "QUICK_REPLY", "text": "Quiero regresar", "url": None, "phoneNumber": None},
        {"type": "URL", "text": "Ver planes", "url": "https://example.com/planes/{{1}}", "phoneNumber": None},
        {"type": "PHONE_NUMBER", "text": "Llamar al gym", "url": None, "phoneNumber": "+528711234567"},
    ]
