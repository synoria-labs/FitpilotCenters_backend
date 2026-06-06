"""Tests for normalized WhatsApp contact resolution and duplicate merging.

Covers the 52/521 Mexican-format equivalence so that sending a template (or text)
reuses an existing contact/conversation instead of creating a duplicate chat.
"""
from __future__ import annotations

from datetime import datetime

from app.crud.whatsappCrud import find_contact_by_number, upsert_contact
from app.models import Contact, Conversation, Message
from scripts.merge_duplicate_whatsapp_contacts import merge_duplicates
from sqlalchemy import func, select

# Same person, two equivalent Mexican formats.
WA_521 = "5218719708890"   # inbound/authoritative form (with the "1")
WA_52 = "528719708890"     # typed-on-send form (without the "1")


async def test_find_contact_by_number_matches_mexican_formats(db):
    created = await upsert_contact(db, wa_id=WA_521, phone_number=WA_521)
    await db.flush()

    # Different format, spaces and "+" — still the same person.
    assert (await find_contact_by_number(db, "+52 871 970 8890")).id == created.id
    assert (await find_contact_by_number(db, WA_52)).id == created.id
    assert await find_contact_by_number(db, "9999999999") is None


async def test_send_reuses_existing_contact_without_downgrading_wa_id(db):
    inbound = await upsert_contact(db, wa_id=WA_521, phone_number=WA_521)
    await db.flush()

    # A send types the number without the "1"; authoritative=False.
    reused = await upsert_contact(db, wa_id=WA_52, phone_number=WA_52, authoritative=False)

    assert reused.id == inbound.id           # no duplicate contact
    assert reused.wa_id == WA_521            # canonical wa_id preserved


async def test_inbound_upsert_canonicalizes_wa_id(db):
    # Contact first created by a send (52 form, non-authoritative).
    sent = await upsert_contact(db, wa_id=WA_52, phone_number=WA_52, authoritative=False)
    await db.flush()

    # Later an inbound message arrives with Meta's authoritative 521 form.
    inbound = await upsert_contact(db, wa_id=WA_521, phone_number=WA_521)

    assert inbound.id == sent.id             # same contact, no duplicate
    assert inbound.wa_id == WA_521           # upgraded to the authoritative form


async def _make_contact_with_message(db, wa_id: str, text: str, ts: datetime) -> Contact:
    contact = Contact(wa_id=wa_id, phone_number=wa_id)
    db.add(contact)
    await db.flush()
    conversation = Conversation(contact_id=contact.id, status="active")
    db.add(conversation)
    await db.flush()
    db.add(
        Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="inbound",
            message_type="text",
            text_content=text,
            timestamp=ts,
        )
    )
    await db.flush()
    return contact


def _last10(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())[-10:]


async def test_merge_collapses_duplicate_contacts(db):
    a = await _make_contact_with_message(db, WA_521, "si", datetime(3009, 1, 2, 12, 0, 0))
    b = await _make_contact_with_message(db, WA_52, "Plantilla enviada", datetime(3009, 1, 1, 12, 0, 0))
    await db.commit()

    summaries = await merge_duplicates(db, dry_run=False)

    # Exactly one group merged; the 521 (authoritative) contact is canonical.
    group = [s for s in summaries if s["key"] == _last10(WA_521)]
    assert len(group) == 1
    assert group[0]["canonical_id"] == a.id
    assert b.id in group[0]["removed_contact_ids"]

    # One contact, one conversation, both messages preserved under the canonical.
    remaining = (
        await db.execute(
            select(Contact).where(
                func.right(func.regexp_replace(Contact.wa_id, r"\D", "", "g"), 10)
                == _last10(WA_521)
            )
        )
    ).scalars().all()
    assert [c.id for c in remaining] == [a.id]

    conv_count = (
        await db.execute(
            select(func.count()).select_from(Conversation).where(Conversation.contact_id == a.id)
        )
    ).scalar_one()
    assert conv_count == 1

    msg_count = (
        await db.execute(
            select(func.count()).select_from(Message).where(Message.contact_id == a.id)
        )
    ).scalar_one()
    assert msg_count == 2


async def test_merge_dry_run_keeps_duplicates(db):
    await _make_contact_with_message(db, WA_521, "si", datetime(3009, 2, 2, 12, 0, 0))
    await _make_contact_with_message(db, WA_52, "Plantilla enviada", datetime(3009, 2, 1, 12, 0, 0))
    await db.commit()

    summaries = await merge_duplicates(db, dry_run=True)
    assert len(summaries) == 1  # reported but not applied

    count = (
        await db.execute(
            select(func.count())
            .select_from(Contact)
            .where(
                func.right(func.regexp_replace(Contact.wa_id, r"\D", "", "g"), 10)
                == _last10(WA_521)
            )
        )
    ).scalar_one()
    assert count == 2  # both duplicates still present
