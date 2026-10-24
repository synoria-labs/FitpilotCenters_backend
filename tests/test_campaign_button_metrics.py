from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import CampaignRecipient, Contact, Conversation, Message, Payment, WhatsAppTemplate, WebhookLog
from app.services import campaign_interactions as clicks, campaign_service as service, whatsapp_outbound
from tests.test_campaign_delivery_recovery import AT, attempt, seed

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_sends(monkeypatch):
    monkeypatch.setattr(whatsapp_outbound.cloud, "send_template", AsyncMock(side_effect=AssertionError("No real sends")))


async def setup(db):
    campaign, recipient = await seed(db)
    template = await db.get(WhatsAppTemplate, campaign.template_id)
    template.components = [{"type": "BUTTONS", "buttons": [
        {"type": "QUICK_REPLY", "text": "Quiero reservar"},
        {"type": "QUICK_REPLY", "text": "Más información"},
        {"type": "URL", "text": "Pagar ahora", "url": "https://example.com/pay"}]}]
    sent = await attempt(db, campaign, recipient)
    contact = Contact(wa_id=recipient.wa_id, phone_number=recipient.phone_e164, name="Ana")
    db.add(contact)
    await db.flush()
    conversation = Conversation(contact_id=contact.id)
    db.add(conversation)
    await db.flush()
    return campaign, recipient, sent, contact, conversation


async def inbound(db, fixture, label="Quiero reservar", *, kind="button", mid="click-1", context=True, hours=1):
    _, _, sent, contact, conversation = fixture
    message = Message(wa_message_id=mid, contact_id=contact.id, conversation_id=conversation.id,
                      direction="inbound", message_type=kind, text_content=label,
                      context_message_id=sent.wa_message_id if context else None,
                      timestamp=(AT + timedelta(hours=hours)).replace(tzinfo=None))
    db.add(message)
    await db.flush()
    return message


async def test_buttons_people_and_written_replies_are_separate_and_share_summary(db):
    fixture = await setup(db)
    campaign, recipient, *_ = fixture
    await inbound(db, fixture)
    await inbound(db, fixture, mid="click-2")
    await inbound(db, fixture, "Más información", mid="click-3")
    await inbound(db, fixture, kind="text", mid="manual-text")
    report = await clicks.analytics(db, campaign.id)
    assert (report["button_clicks"], report["button_clickers"], report["text_replies"], report["text_reply_people"]) == (3, 1, 1, 1)
    reserve, info, url = report["buttons"]
    assert (reserve["clicks"], reserve["unique_people"], info["clicks"]) == (2, 1, 1)
    assert not url["measurable"] and url["clicks"] is None and url["unique_people"] is None
    single = await service.get_metrics(db, campaign.id)
    batch = (await service.get_metrics_batch(db, [campaign.id]))[campaign.id]
    for key in clicks.COUNT_FIELDS:
        assert single[key] == batch[key] == report[key]
    assert recipient.status == "pending"  # Reading metrics never mutates delivery evidence or queues sends.
    whatsapp_outbound.cloud.send_template.assert_not_awaited()


async def test_duplicates_and_provider_redelivery_are_idempotent(db):
    fixture = await setup(db)
    await inbound(db, fixture)
    await inbound(db, fixture)
    assert (await clicks.analytics(db, fixture[0].id))["button_clicks"] == 1
    await inbound(db, fixture, "Más información")  # Conflicting copies of the same provider ID are ambiguous.
    assert (await clicks.analytics(db, fixture[0].id))["button_clicks"] == 0


async def test_previous_attempt_is_retained_after_retry_and_mexican_alias_is_same_phone(db):
    fixture = await setup(db)
    campaign, recipient, old, contact, _ = fixture
    later = await attempt(db, campaign, recipient, 1)
    recipient.wa_message_id = later.wa_message_id
    contact.wa_id = recipient.phone_key  # 52 and 521 normalize to one identity.
    await inbound(db, fixture)
    result = await clicks.analytics(db, campaign.id)
    assert result["button_clicks"] == 1 and result["items"][0]["phone"] == recipient.phone_key


@pytest.mark.parametrize("scenario", ["wrong_sender", "no_context", "wrong_context", "before_send", "outbound"])
async def test_only_exact_inbound_campaign_evidence_is_attributed(db, scenario):
    fixture = await setup(db)
    message = await inbound(db, fixture, context=scenario != "no_context", hours=-1 if scenario == "before_send" else 1)
    if scenario == "wrong_sender":
        fixture[3].wa_id = "5218710009999"
    if scenario == "wrong_context":
        message.context_message_id = "not-a-campaign-message"
    if scenario == "outbound":
        message.direction = "outbound"
    await db.flush()
    assert (await clicks.analytics(db, fixture[0].id))["button_clicks"] == 0


async def test_exact_campaign_beats_recent_campaign_and_ambiguous_legacy_mapping_is_excluded(db):
    fixture = await setup(db)
    other, recipient = await seed(db)
    await inbound(db, fixture)
    reports = await clicks.reports(db, [fixture[0].id, other.id])
    assert reports[fixture[0].id]["button_clicks"] == 1 and reports[other.id]["button_clicks"] == 0
    # A second attempt cannot share a wamid (unique constraint); conflicting legacy aliases can.
    fixture[2].wa_message_id = None
    fixture[1].wa_message_id = recipient.wa_message_id = "test-wamid-" + str(fixture[1].id) + "-0"
    await db.flush()
    assert (await clicks.analytics(db, fixture[0].id))["button_clicks"] == 0


async def test_renamed_button_retains_actual_historical_caption_and_zero_for_new_caption(db):
    fixture = await setup(db)
    await inbound(db, fixture, "Reservar (anterior)")
    report = await clicks.analytics(db, fixture[0].id)
    assert report["buttons"][0]["clicks"] == 0
    assert report["buttons"][-1]["historical"] and report["buttons"][-1]["clicks"] == 1


@pytest.mark.parametrize("subtype,raw", [("button_reply", True), ("list_reply", True), ("button_reply", False)])
async def test_interactive_requires_raw_button_callback_not_list_selection(db, subtype, raw):
    fixture = await setup(db)
    message = await inbound(db, fixture, kind="interactive")
    if raw:
        value = {"id": message.wa_message_id, "from": fixture[3].wa_id, "type": "interactive",
                 "context": {"id": fixture[2].wa_message_id},
                 "interactive": {"type": subtype, subtype: {"id": "reserve", "title": "Quiero reservar"}}}
        payload = {"entry": [{"changes": [{"value": {"messages": [value]}}]}]}
        db.add_all([WebhookLog(event_type="whatsapp_business_account", payload=payload) for _ in range(2)])
        await db.flush()
    report = await clicks.analytics(db, fixture[0].id)
    assert report["button_clicks"] == int(raw and subtype == "button_reply")


@pytest.mark.parametrize("status,hours,expected", [("COMPLETED", 2, 1), ("COMPLETED", -1, 0),
                                                ("PENDING", 2, 0), ("REFUNDED", 2, 0)])
async def test_only_verified_subsequent_campaign_conversion_counts(db, status, hours, expected):
    fixture = await setup(db)
    campaign, recipient, *_ = fixture
    await inbound(db, fixture)
    await inbound(db, fixture, "Más información", mid="other-click")
    paid_at = AT + timedelta(hours=hours)
    payment = Payment(person_id=recipient.person_id, amount=500, method="cash", status=status, paid_at=paid_at)
    db.add(payment)
    await db.flush()
    recipient.converted, recipient.converted_at, recipient.conversion_payment_id = True, paid_at, payment.id
    await db.flush()
    report = await clicks.analytics(db, campaign.id)
    assert report["converted_clickers"] == expected
    assert [b["converted_people"] for b in report["buttons"][:2]] == [expected, expected]
    assert all(e["converted"] == bool(expected) for e in report["items"])


async def test_query_permission_pagination_and_missing_campaign(db, monkeypatch):
    from app.graphql.campaigns import queries
    fixture = await setup(db)
    for index in range(3):
        await inbound(db, fixture, mid=f"event-{index}", hours=index + 1)
    await inbound(db, fixture, "Más información", mid="other")
    info = SimpleNamespace(context=SimpleNamespace(db=db))
    check = AsyncMock(return_value="denied")
    monkeypatch.setattr(queries, "require_capability", check)
    query = queries.CampaignsQuery()
    assert (await query.campaign_button_analytics(info, fixture[0].id)).error == "denied"
    check.return_value = None
    result = await query.campaign_button_analytics(info, fixture[0].id, clicks.key_for("Quiero reservar"), limit=1, offset=1)
    assert result.button_clicks == 4 and result.total == 3 and len(result.items) == 1 and result.offset == 1
    assert result.items[0].wa_message_id == "event-1"
    assert (await query.campaign_button_analytics(info, -1)).error == "Campaña no encontrada."
    check.assert_awaited_with(info, queries.SEND_CAMPAIGNS)


async def test_template_variant_buttons_and_duplicate_people_are_unique_by_phone(db):
    from app.models import CampaignVariant
    fixture = await setup(db)
    c, r, _, contact, _ = fixture
    t = WhatsAppTemplate(template_name="variant", template_namespace="test", template_language="es_MX",
                         template_status="APPROVED", components=[{"type": "BUTTONS", "buttons": [
                             {"type": "QUICK_REPLY", "text": "Otra opción"}]}])
    db.add(t)
    await db.flush()
    db.add(CampaignVariant(campaign_id=c.id, variant_code="B", template_id=t.id))
    duplicate = CampaignRecipient(campaign_id=c.id, person_id=r.person_id, dedup_key="duplicate",
                                  wa_id=r.wa_id, phone_key=r.phone_key, status="skipped")
    db.add(duplicate)
    await db.flush()
    duplicate.wa_message_id = fixture[2].wa_message_id
    await inbound(db, fixture)
    await db.flush()
    report = await clicks.analytics(db, c.id)
    assert report["button_clickers"] == 1 and len(report["buttons"]) == 4
    assert report["items"][0]["recipient_id"] == min(r.id, duplicate.id)
