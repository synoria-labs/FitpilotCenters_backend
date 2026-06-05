"""Tests for enriching WhatsApp conversations with member identity."""
from __future__ import annotations

from datetime import datetime, timezone

from app.crud.whatsappCrud import (
    _MemberCandidate,
    _member_contact_match_rank,
    _phone_match_keys,
    get_conversations,
)
from app.models import (
    Contact,
    Conversation,
    MembershipPlan,
    MembershipSubscription,
    Message,
    People,
    PersonRole,
    Role,
)
from sqlalchemy import select


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


async def _ensure_member_role(db) -> Role:
    existing = (
        await db.execute(select(Role).where(Role.code == "member"))
    ).scalar_one_or_none()
    if existing:
        return existing

    role = Role(code="member", description="Member")
    db.add(role)
    await db.flush()
    return role


async def _make_plan(db, name: str) -> MembershipPlan:
    plan = MembershipPlan(
        name=name,
        price=100,
        duration_value=1,
        duration_unit="month",
    )
    db.add(plan)
    await db.flush()
    return plan


async def _make_member(
    db,
    *,
    role: Role,
    plan: MembershipPlan,
    full_name: str,
    phone_number: str | None = None,
    wa_id: str | None = None,
    status: str = "expired",
) -> People:
    person = People(full_name=full_name, phone_number=phone_number, wa_id=wa_id)
    db.add(person)
    await db.flush()

    db.add(PersonRole(person_id=person.id, role_id=role.id, created_at=_utc(3008, 1, 1)))
    db.add(
        MembershipSubscription(
            person_id=person.id,
            plan_id=plan.id,
            start_at=_utc(3008, 1, 1),
            end_at=_utc(3008, 2, 1),
            status=status,
        )
    )
    await db.flush()
    return person


async def _make_conversation(db, *, wa_id: str, text: str) -> Conversation:
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
            timestamp=datetime(3008, 1, 10, 12, 0, 0),
        )
    )
    await db.flush()
    return conversation


def test_phone_match_keys_support_mexican_whatsapp_formats():
    keys = _phone_match_keys("+52 871 970 8890")

    assert "8719708890" in keys
    assert "528719708890" in keys
    assert "5218719708890" in keys
    assert _member_contact_match_rank(
        Contact(wa_id="5218719708890", phone_number="5218719708890"),
        _MemberCandidate(
            id=1,
            full_name="Socia Local",
            wa_id=None,
            phone_number="8719708890",
            active_membership_rank=0,
            latest_membership_end=None,
        ),
    ) == 2


async def test_get_conversations_enriches_contact_with_member_name(db):
    role = await _ensure_member_role(db)
    plan = await _make_plan(db, "WhatsApp Match Plan 3008")
    await _make_member(
        db,
        role=role,
        plan=plan,
        full_name="Socia WhatsApp 3008",
        phone_number="3006550001",
    )
    matched = await _make_conversation(
        db,
        wa_id="5213006550001",
        text="mensaje con socio",
    )
    unmatched = await _make_conversation(
        db,
        wa_id="5213006550002",
        text="mensaje sin socio",
    )

    conversations = await get_conversations(db, limit=None)
    by_id = {conversation.id: conversation for conversation in conversations}

    assert by_id[matched.id].contact.member_name == "Socia WhatsApp 3008"
    assert by_id[matched.id].contact.member_id is not None
    assert by_id[unmatched.id].contact.member_name is None
    assert by_id[unmatched.id].contact.member_id is None


async def test_get_conversations_searches_by_member_name(db):
    role = await _ensure_member_role(db)
    plan = await _make_plan(db, "WhatsApp Search Plan 3008")
    await _make_member(
        db,
        role=role,
        plan=plan,
        full_name="Nombre Buscable WhatsApp 3008",
        phone_number="3006550003",
    )
    conversation = await _make_conversation(
        db,
        wa_id="5213006550003",
        text="mensaje buscable",
    )

    conversations = await get_conversations(db, limit=None, search="Buscable WhatsApp")
    by_id = {item.id: item for item in conversations}

    assert conversation.id in by_id
    assert by_id[conversation.id].contact.member_name == "Nombre Buscable WhatsApp 3008"
