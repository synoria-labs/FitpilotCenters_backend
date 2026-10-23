from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace
import asyncio

import pytest
from sqlalchemy import delete, func, select

from app.crud import campaignsCrud as crud
from app.models import Campaign, CampaignRecipient, People, WhatsAppTemplate
from app.models.campaignsModel import CampaignSendAttempt as Attempt
from app.models.whatsappModel import WebhookLog
from app.services import campaign_delivery as d, campaign_service as svc, whatsapp_outbound as outbound

pytestmark = pytest.mark.asyncio
AT = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_real_sends(monkeypatch):
    monkeypatch.setattr(outbound.cloud, "send_template", AsyncMock(side_effect=AssertionError("Real send forbidden")))
    from app.core.outbound_config import outbound_config
    monkeypatch.setattr(outbound_config, "QUIET_HOURS_START", 0)
    monkeypatch.setattr(outbound_config, "QUIET_HOURS_END", 0)


async def seed(db, *, policy="respect", auto=True, number="5218712345678", status="pending"):
    t = WhatsAppTemplate(template_name="test_campaign", template_namespace="test", template_language="es_MX",
                         template_status="APPROVED", category="MARKETING",
                         components=[{"type": "BODY", "text": "Hola"}])
    p = People(full_name="Test member", phone_number=number)
    db.add_all([t, p])
    await db.flush()
    c = Campaign(name="Test", objective="broadcast", status="scheduled", template_id=t.id,
                 daily_cap_policy=policy, auto_retry_meta_limits=auto)
    db.add(c)
    await db.flush()
    r = CampaignRecipient(campaign_id=c.id, person_id=p.id, dedup_key=f"test:{c.id}:{p.id}",
                          wa_id=number, phone_e164=number, phone_key=d.phone_key(number), status=status)
    db.add(r)
    await db.flush()
    return c, r


async def attempt(db, c, r, index=0):
    a = Attempt(campaign_id=c.id, recipient_id=r.id, phone_key=r.phone_key, state="accepted",
                started_at=AT + timedelta(hours=index * 24), accepted_at=AT + timedelta(hours=index * 24),
                wa_message_id=f"test-wamid-{r.id}-{index}")
    db.add(a)
    await db.flush()
    return a


@pytest.mark.parametrize("value", ["8712345678", "+52 871 234 5678", "5218712345678"])
async def test_mexican_identity_is_one_destination(value):
    assert d.phone_key(value) == "528712345678"
    assert d.phone_key("+1 415 555 0123") == "14155550123"


async def test_three_meta_failures_have_exactly_two_retries(db):
    c, r = await seed(db)
    a1 = await attempt(db, c, r)
    await d.failure(db, c, r, a1, 131049, "Limit", AT)
    assert r.status == "pending" and r.send_after == AT + timedelta(hours=24)
    a2 = await attempt(db, c, r, 1)
    await d.failure(db, c, r, a2, 131049, "Limit", AT + timedelta(hours=24))
    assert r.send_after == AT + timedelta(hours=72)
    a3 = await attempt(db, c, r, 3)
    await d.failure(db, c, r, a3, 131049, "Limit", AT + timedelta(hours=72))
    assert r.status == "failed" and r.send_after is None


@pytest.mark.parametrize("code", [130472, 131026, 100, 131048])
async def test_review_errors_are_not_automatically_claimed(db, code):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    await d.failure(db, c, r, a, code, "Original rejection", AT)
    assert r.status == "failed"
    assert code != 131048 or c.status == "paused"
    assert await crud.claim_recipient_batch(db, c.id, 20) == []
    assert str(code) in r.error


@pytest.mark.parametrize("auto,canceled", [(False, False), (True, True)])
async def test_existing_campaigns_and_canceled_campaigns_do_not_opt_into_retries(db, auto, canceled):
    c, r = await seed(db, auto=auto)
    if canceled:
        c.status = "canceled"
    a = await attempt(db, c, r)
    await d.failure(db, c, r, a, 131049, "Limit", AT)
    assert r.status == "failed" and r.send_after is None


async def test_delivery_wins_over_duplicates_and_late_errors(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    for status in ("read", "delivered", "sent", "read", "failed"):
        await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status=status, timestamp=AT,
                             errors=[{"code": 131026, "title": "Old failure"}] if status == "failed" else None)
    assert a.state == "read"
    assert r.status == "read" and r.send_after is None
    m = await d.metrics(db, c.id)
    assert (m["accepted"], m["sent"], m["delivered"], m["read"]) == (1, 1, 1, 1)


async def test_old_failed_callback_does_not_reset_new_attempt(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    newer = await attempt(db, c, r, 1)
    r.status = "sent"
    await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status="failed", timestamp=AT,
                         errors=[{"code": 131049}])
    assert r.status == "sent" and newer.state == "accepted"


async def test_duplicate_failed_webhook_preserves_retry_instant(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    for at in (AT, AT + timedelta(hours=2)):
        await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status="failed", timestamp=at,
                             errors=[{"code": 131049, "title": "Marketing limit"}])
    assert r.send_after == AT + timedelta(hours=24)
    assert await db.scalar(select(func.count(Attempt.id))) == 1


async def test_naive_webhook_timestamp_is_utc_not_server_local_time(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status="failed",
                         timestamp=AT.replace(tzinfo=None), errors=[{"code": 131049}])
    await db.refresh(a)
    await db.refresh(r)
    assert a.failed_at == AT and r.send_after == AT + timedelta(hours=24)


async def test_legacy_callbacks_preserve_error_without_enabling_retry(db):
    c, r = await seed(db, auto=False)
    r.wa_message_id, r.sent_at = f"legacy-{r.id}", AT
    await db.flush()
    await d.apply_status(db, wa_message_id=r.wa_message_id, meta_status="failed", timestamp=AT,
                         errors=[{"code": 131049, "title": "Original Meta error"}])
    history = await d.attempts_for(db, c.id)
    assert len(history) == 1 and history[0].error_code == 131049
    assert r.status == "failed" and r.send_after is None
    assert "Original Meta error" in r.error


async def test_list_and_detail_metrics_use_the_same_evidence(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status="read", timestamp=AT)
    single = await svc.get_metrics(db, c.id)
    batch = (await svc.get_metrics_batch(db, [c.id]))[c.id]
    for field in ("accepted", "sent", "delivered", "read", "rejected", "unique_phones",
                  "delivery_rate", "read_rate", "error_counts"):
        assert single[field] == batch[field]


async def test_halloween_evidence_survives_recipient_reclassification(db):
    c, r = await seed(db)
    db.add_all([Attempt(campaign_id=c.id, recipient_id=r.id, phone_key=r.phone_key, state="delivered",
                        started_at=AT, accepted_at=AT, sent_at=AT, delivered_at=AT,
                        wa_message_id=f"halloween-delivery-{r.id}-{i}") for i in range(241)])
    db.add_all([Attempt(campaign_id=c.id, recipient_id=r.id, phone_key=r.phone_key, state="failed",
                        started_at=AT, accepted_at=AT, failed_at=AT, error_code=131048,
                        error_detail="Original Meta spam limit",
                        wa_message_id=f"halloween-failure-{r.id}-{i}") for i in range(72)])
    r.status, r.skip_reason = "skipped", "daily_cap"
    await db.flush()
    m = await d.metrics(db, c.id)
    assert m["delivered"] == 241 and m["rejected"] == 72 and m["error_counts"]["131048"] == 72


async def test_preview_and_recovery_deduplicate_without_mutating_members(db):
    c, r = await seed(db, policy="allow_additional", status="skipped")
    p2 = People(full_name="Second member sharing phone", phone_number="528712345678")
    db.add(p2)
    await db.flush()
    r2 = CampaignRecipient(campaign_id=c.id, person_id=p2.id, wa_id=p2.phone_number,
                           dedup_key=f"alias:{c.id}:{p2.id}", status="skipped")
    db.add(r2)
    await db.flush()
    preview = await d.recovery(db, c.id)
    assert preview["eligible"] == 1 and preview["duplicates"] == 1
    assert r.status == "skipped" and r2.status == "skipped"
    result = await d.recovery(db, c.id, execute=True)
    assert result["eligible"] == 1 and r.status == "pending" and r2.status == "skipped"
    assert p2.phone_number == "528712345678"
    assert await d.check_recipient(db, c, r2) == "duplicate_phone"


async def test_recovery_never_resends_delivered_aliases(db):
    c, r = await seed(db, policy="allow_additional", status="skipped")
    a = await attempt(db, c, r)
    a.delivered_at, a.state = AT, "delivered"
    await db.flush()
    p = await d.recovery(db, c.id, execute=True)
    assert p["eligible"] == 0 and p["delivered"] == 1
    assert r.status == "skipped"


async def test_uncertain_and_unacknowledged_requests_require_review(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    for state in ("started", "uncertain", "accepted", "sent"):
        a.state = state
        await db.flush()
        assert (await d.recovery(db, c.id))["review"] == 1
        assert await d.check_recipient(db, c, r) in ("uncertain_previous_send", "awaiting_delivery")


@pytest.mark.parametrize("policy,expected", [("respect", "skipped"), ("defer", "pending"), ("allow_additional", "sent")])
async def test_actual_dispatch_obeys_daily_policy(db, monkeypatch, policy, expected):
    from app.crud import whatsappCrud
    c, r = await seed(db, policy=policy)
    c.status = "sending"
    cloud = AsyncMock(return_value={"wa_message_id": f"new-{r.id}"})
    monkeypatch.setattr(outbound.cloud, "send_template", cloud)
    monkeypatch.setattr(whatsappCrud, "count_marketing_sends_today", AsyncMock(return_value=1))
    await svc._send_to_recipient(db, c, None, r)
    assert r.status == expected
    assert cloud.await_count == int(policy == "allow_additional")
    if policy == "defer":
        assert r.send_after > d.now()


async def test_real_daily_ledger_applies_across_campaigns_without_counting_own_intent(db, monkeypatch):
    c, first = await seed(db)
    c.status = "sending"
    cloud = AsyncMock(side_effect=[{"wa_message_id": f"ledger-first-{first.id}"},
                                   {"wa_message_id": f"ledger-extra-{first.id}"}])
    monkeypatch.setattr(outbound.cloud, "send_template", cloud)
    await svc._send_to_recipient(db, c, None, first)
    assert first.status == "sent"  # The durable intent does not block its own first request.
    for policy, expected in (("respect", "skipped"), ("defer", "pending"), ("allow_additional", "sent")):
        campaign = Campaign(name="Next campaign", objective="broadcast", template_id=c.template_id,
                            status="sending", daily_cap_policy=policy)
        db.add(campaign)
        await db.flush()
        r = CampaignRecipient(campaign_id=campaign.id, person_id=first.person_id,
                              wa_id=first.wa_id, phone_e164=first.phone_e164,
                              dedup_key=f"ledger:{campaign.id}:{first.person_id}", status="pending")
        db.add(r)
        await db.flush()
        await svc._send_to_recipient(db, campaign, None, r)
        assert r.status == expected
    assert cloud.await_count == 2


async def test_lost_provider_response_is_never_blindly_retried(db, monkeypatch):
    c, r = await seed(db, policy="allow_additional")
    c.status = "sending"
    monkeypatch.setattr(outbound.cloud, "send_template", AsyncMock(side_effect=TimeoutError("lost response")))
    await svc._send_to_recipient(db, c, None, r)
    assert r.status == "needs_review"
    a = await db.scalar(select(Attempt).where(Attempt.recipient_id == r.id))
    assert a.state == "uncertain"
    assert await crud.claim_recipient_batch(db, c.id, 20) == []


async def test_callback_before_message_persistence_is_recovered(db):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    db.add(WebhookLog(event_type="whatsapp_business_account", created_at=AT.replace(tzinfo=None),
                      payload={"entry": [{"changes": [{"value": {"statuses": [
                          {"id": a.wa_message_id, "status": "delivered", "timestamp": str(int(AT.timestamp()))}
                      ]}}]}]}))
    await db.flush()
    await d.replay_receipts(db, a)
    assert a.delivered_at == AT


async def test_reconciliation_dry_run_and_exact_history(db, monkeypatch):
    from app.crud import whatsappCrud
    c, r = await seed(db)
    contact = await whatsappCrud.upsert_contact(db, wa_id=r.wa_id)
    conv = await whatsappCrud.get_or_open_conversation(db, contact.id)
    msg = await whatsappCrud.insert_outbound_message(db, conversation_id=conv.id, contact_id=contact.id,
                                                    text="Test", wa_message_id=f"historical-{r.id}")
    r.message_id, r.wa_message_id, r.sent_at = msg.id, msg.wa_message_id, AT
    await whatsappCrud.insert_message_status(db, wa_message_id=msg.wa_message_id, status="delivered",
                                             timestamp=AT.replace(tzinfo=None))
    assert (await d.reconcile(db, c.id))["matched"] == 1
    assert not (await d.attempts_for(db, c.id))
    await d.reconcile(db, c.id, apply=True)
    assert (await d.metrics(db, c.id))["delivered"] == 1
    assert (await d.reconcile(db, c.id, apply=True))["matched"] == 0
    assert r.status == "delivered"  # Repair uses proof, without enqueuing a send.


async def test_phone_lock_serializes_two_independent_workers():
    from app.db.postgresql import async_session_factory
    started, released, order = asyncio.Event(), asyncio.Event(), []

    async def first():
        async with async_session_factory() as db:
            await d.phone_lock(db, 999999, "528712345678")
            order.append("first")
            started.set()
            await released.wait()
            await db.commit()

    async def second():
        await started.wait()
        async with async_session_factory() as db:
            released.set()
            await d.phone_lock(db, 999999, "528712345678")
            order.append("second")
            await db.commit()

    await asyncio.wait_for(asyncio.gather(first(), second()), 5)
    assert order == ["first", "second"]


async def test_policy_update_only_saves_and_recovery_requires_permission(db, monkeypatch):
    import app.graphql.campaigns.mutations as mutations
    import app.graphql.campaigns.queries as queries
    from app.graphql.campaigns.types import UpdateCampaignInput
    c, r = await seed(db, auto=False)
    info = SimpleNamespace(context=SimpleNamespace(db=db, account_id=None))
    monkeypatch.setattr(mutations, "require_capability", AsyncMock(return_value=None))
    result = await mutations.CampaignsMutation().update_campaign(
        info, c.id, UpdateCampaignInput(daily_cap_policy="defer", auto_retry_meta_limits=True))
    assert result.success and c.daily_cap_policy == "defer" and c.auto_retry_meta_limits
    assert r.status == "pending" and r.send_after is None
    assert not await d.attempts_for(db, c.id)
    c.status = "sending"
    result = await mutations.CampaignsMutation().update_campaign(
        info, c.id, UpdateCampaignInput(daily_cap_policy="allow_additional"))
    assert not result.success
    monkeypatch.setattr(mutations, "require_capability", AsyncMock(return_value="denied"))
    monkeypatch.setattr(queries, "require_capability", AsyncMock(return_value="denied"))
    assert not (await mutations.CampaignsMutation().recover_campaign_recipients(info, c.id)).success
    assert (await queries.CampaignsQuery().preview_campaign_recovery(info, c.id)).error == "denied"


async def test_no_retry_or_provider_send_after_event_closes(db, monkeypatch):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    monkeypatch.setattr(d, "closed_event", AsyncMock(return_value=True))
    await d.failure(db, c, r, a, 131049, "Limit", AT)
    assert r.status == "failed" and r.send_after is None
    with pytest.raises(ValueError, match="evento"):
        await d.recovery(db, c.id, execute=True)
    await svc._send_to_recipient(db, c, None, r)
    outbound.cloud.send_template.assert_not_awaited()


async def test_retry_that_would_miss_event_deadline_is_not_scheduled(db, monkeypatch):
    c, r = await seed(db)
    a = await attempt(db, c, r)
    monkeypatch.setattr(d, "beyond_event", AsyncMock(return_value=True))
    await d.failure(db, c, r, a, 131049, "Limit", AT)
    assert r.status == "failed" and r.send_after is None


async def test_revoked_consent_stops_recovery_and_dispatch(db):
    from app.models import CommunicationOptIn
    c, r = await seed(db, policy="allow_additional")
    db.add(CommunicationOptIn(person_id=r.person_id, channel="whatsapp", granted_at=AT - timedelta(days=1),
                             revoked_at=AT))
    await db.flush()
    assert (await d.recovery(db, c.id))["blocked"] == 1
    await svc._send_to_recipient(db, c, None, r)
    assert r.status == "skipped" and r.skip_reason == "no_consent"
    outbound.cloud.send_template.assert_not_awaited()


async def test_exact_button_reply_links_to_old_attempt_with_normalized_identity(db):
    c, r = await seed(db)
    old = await attempt(db, c, r)
    await attempt(db, c, r, 1)
    assert await crud.apply_inbound_reply(db, wa_id="528712345678", timestamp=AT,
                                          context_message_id=old.wa_message_id)
    assert r.status == "replied"
    assert (await d.metrics(db, c.id))["read"] == 0  # A reply does not invent a read receipt.


async def test_real_concurrent_alias_workers_send_only_once(monkeypatch):
    from app.db.postgresql import async_session_factory
    from app.models import Contact, Conversation, Message, Media, MessageStatus
    cloud = AsyncMock(return_value={"wa_message_id": "concurrent-campaign-proof"})
    monkeypatch.setattr(outbound.cloud, "send_template", cloud)
    cid = pid = tid = None
    people_ids = []
    try:
        async with async_session_factory() as db:
            c, first = await seed(db, policy="allow_additional", number="5218719994321")
            c.status = "sending"
            p = People(full_name="Alias", phone_number="528719994321")
            db.add(p)
            await db.flush()
            second = CampaignRecipient(campaign_id=c.id, person_id=p.id, wa_id=p.phone_number,
                                       phone_key=d.phone_key(p.phone_number),
                                       dedup_key=f"concurrent:{c.id}:{p.id}", status="pending")
            db.add(second)
            await db.commit()
            cid, tid = c.id, c.template_id
            people_ids = [first.person_id, p.id]
            rids = [first.id, second.id]

        async def worker(rid):
            async with async_session_factory() as db:
                c = await db.get(Campaign, cid)
                r = await db.get(CampaignRecipient, rid)
                return await svc._send_to_recipient(db, c, None, r)

        outcomes = await asyncio.wait_for(asyncio.gather(*(worker(rid) for rid in rids)), 10)
        assert cloud.await_count == 1 and outcomes.count("sent") == 1
        async with async_session_factory() as db:
            assert len(await d.attempts_for(db, cid)) == 1
    finally:
        if cid is not None:
            async with async_session_factory() as db:
                rids = list((await db.scalars(select(CampaignRecipient.id).where(CampaignRecipient.campaign_id == cid))).all())
                mids = list((await db.scalars(select(Attempt.message_id).where(Attempt.campaign_id == cid))).all())
                contact_ids = list((await db.scalars(select(Message.contact_id).where(Message.id.in_(mids)))).all())
                await db.execute(delete(Attempt).where(Attempt.recipient_id.in_(rids)))
                await db.execute(delete(CampaignRecipient).where(CampaignRecipient.campaign_id == cid))
                await db.execute(delete(MessageStatus).where(MessageStatus.message_id.in_(mids)))
                await db.execute(delete(Media).where(Media.message_id.in_(mids)))
                await db.execute(delete(Message).where(Message.id.in_(mids)))
                await db.execute(delete(Conversation).where(Conversation.contact_id.in_(contact_ids)))
                await db.execute(delete(Contact).where(Contact.id.in_(contact_ids)))
                await db.execute(delete(Campaign).where(Campaign.id == cid))
                await db.execute(delete(People).where(People.id.in_(people_ids)))
                await db.execute(delete(WhatsAppTemplate).where(WhatsAppTemplate.id == tid))
                await db.commit()

# Recovery by cause: manual authorization remains separate from automatic retries.
@pytest.mark.parametrize('code,cause', [
    (131049, 'meta_limit'), (1, 'meta_temporary'), (2, 'meta_temporary'), (4, 'meta_temporary'),
    (80007, 'meta_temporary'), (130429, 'meta_temporary'), (131000, 'meta_temporary'),
    (131016, 'meta_temporary'), (131056, 'meta_temporary'), (133004, 'meta_temporary'),
    (131026, 'bad_number'), (131021, 'bad_number'), (100, 'other_error'),
    (132000, 'other_error'), (130472, 'other_error'), (131047, 'other_error'), (131051, 'other_error'),
    (368, 'other_error'),
])
async def test_recovery_classifies_attempt_codes_and_legacy_errors(db, code, cause):
    c, r = await seed(db, status='failed')
    r.error = f'(#%s) Original rejection' % code
    assert d.recovery_cause(r, [])[0] == cause
    r.error = f'Meta {code}: Original rejection'
    assert d.recovery_cause(r, [])[0] == cause
    a = await attempt(db, c, r)
    a.state, a.error_code, a.error_detail = 'failed', code, 'Original cause'
    assert d.recovery_cause(r, [a])[0] == cause
    assert str(code) in d.recovery_cause(r, [a])[1]


async def test_recovery_only_queues_selected_causes_and_empty_selection_is_safe(db):
    c, r = await seed(db, status='failed', policy='allow_additional')
    r.error = 'Meta 131000: temporary'
    p = People(full_name='Review parameters', phone_number='5218710001111')
    db.add(p)
    await db.flush()
    other = CampaignRecipient(campaign_id=c.id, person_id=p.id, wa_id=p.phone_number,
                              dedup_key=f'other:{c.id}', status='failed', error='(#100) Invalid parameter')
    db.add(other)
    await db.flush()
    preview = await d.recovery(db, c.id)
    assert {x['key'] for x in preview['causes'] if x['selected_by_default']} == {
        'daily_cap', 'meta_limit', 'meta_temporary', 'bad_number'}
    assert (await d.recovery(db, c.id, execute=True, causes=[]))['eligible'] == 0
    result = await d.recovery(db, c.id, execute=True, causes=['other_error'])
    assert result['eligible'] == 1 and other.status == 'pending' and r.status == 'failed'
    assert (await d.recovery(db, c.id))['recipients'][0]['person_name'] == 'Test member'
    with pytest.raises(ValueError, match='desconocida'):
        await d.recovery(db, c.id, causes=['anything'])


@pytest.mark.parametrize('number', ['', '1234'])
async def test_invalid_snapshot_requires_a_changed_valid_phone_before_dispatch(db, monkeypatch, number):
    c, r = await seed(db, number=number, status='failed', policy='allow_additional')
    r.error = '(#100) Invalid parameter'
    preview = await d.recovery(db, c.id)
    entry = preview['recipients'][0]
    assert entry['cause'] == 'bad_number' and not entry['eligible']
    assert 'Socios' in entry['blocked_reason']
    person = await db.get(People, r.person_id)
    person.phone_number, person.wa_id = '+52 871 222 3333', '5218719990000'
    await db.flush()
    corrected = await d.recovery(db, c.id)
    assert corrected['eligible'] == 1 and r.wa_id == number  # Preview never mutates the snapshot.
    await d.recovery(db, c.id, execute=True, causes=['bad_number'])
    assert r.wa_id == '528712223333' and r.phone_key == '528712223333'
    assert r.phone_e164 == '+52 871 222 3333'
    c.status = 'sending'
    cloud = AsyncMock(return_value={'wa_message_id': f'corrected-{r.id}'})
    monkeypatch.setattr(outbound.cloud, 'send_template', cloud)
    await svc._send_to_recipient(db, c, None, r)
    assert cloud.await_count == 1 and cloud.await_args.kwargs['to'] == '528712223333'
    assert person.wa_id == '5218719990000'


async def test_undeliverable_number_must_change_not_just_be_reformatted(db):
    c, r = await seed(db, status='failed')
    r.error = 'Meta 131026: undeliverable'
    person = await db.get(People, r.person_id)
    person.phone_number = '+52 871 234 5678'
    assert (await d.recovery(db, c.id, causes=['bad_number']))['eligible'] == 0


async def test_converted_recipient_is_excluded_from_recovery_and_worker(db):
    c, r = await seed(db, status='skipped')
    r.converted = True
    result = await d.recovery(db, c.id, execute=True)
    assert result['eligible'] == 0 and 'convirtió' in result['recipients'][0]['blocked_reason']
    await svc._send_to_recipient(db, c, None, r)
    assert r.skip_reason == 'converted'
    outbound.cloud.send_template.assert_not_awaited()


@pytest.mark.parametrize('identity', ['person', 'phone'])
@pytest.mark.parametrize('state', ['awaiting_payment', 'paid'])
async def test_existing_event_buyers_are_excluded_by_person_or_normalized_phone(db, identity, state):
    from app.models.specialEventModel import SpecialEventOrder
    from app.crud.special_events.common import participant_key
    from tests.test_special_event_payments import published_event
    c, r = await seed(db, status='skipped')
    _, event = await published_event(db)
    c.special_event_id = event.id
    order = SpecialEventOrder(event_id=event.id, person_id=r.person_id if identity == 'person' else None,
                              participant_name='Buyer', participant_key=participant_key(r.wa_id) if identity == 'phone' else None,
                              amount=500, status=state, hold_expires_at=d.now() + timedelta(hours=1))
    db.add(order)
    await db.flush()
    result = await d.recovery(db, c.id, execute=True)
    assert result['eligible'] == 0 and 'pedido' in result['recipients'][0]['blocked_reason']
    assert await d.check_recipient(db, c, r) == 'event_buyer'


async def test_unconfirmed_requires_explicit_cause_and_24_hours_then_sends_once(db, monkeypatch):
    c, r = await seed(db, policy='allow_additional', status='sent')
    a = await attempt(db, c, r)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=23))
    assert (await d.recovery(db, c.id, causes=['unconfirmed']))['eligible'] == 0
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=25))
    assert (await d.recovery(db, c.id))['eligible'] == 0
    preview = await d.recovery(db, c.id, causes=['unconfirmed'])
    assert preview['eligible'] == 1
    assert preview['recipients'][0]['cause'] == 'unconfirmed'
    await d.recovery(db, c.id, execute=True, causes=['unconfirmed'])
    assert r.skip_reason == 'recovery_unconfirmed'
    await crud.defer_recipient(db, r, send_after=d.now(), reason='pending_action')
    assert r.skip_reason == 'recovery_unconfirmed'
    cloud = AsyncMock(return_value={'wa_message_id': f'manual-{r.id}'})
    monkeypatch.setattr(outbound.cloud, 'send_template', cloud)
    c.status = 'sending'
    await svc._send_to_recipient(db, c, None, r)
    await svc._send_to_recipient(db, c, None, r)
    assert cloud.await_count == 1
    assert a.state == 'accepted'  # Manual retry keeps the original evidence.


async def test_late_delivery_cancels_manual_authorization_before_send(db, monkeypatch):
    c, r = await seed(db, policy='allow_additional', status='sent')
    a = await attempt(db, c, r)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=25))
    await d.recovery(db, c.id, execute=True, causes=['unconfirmed'])
    await d.apply_status(db, wa_message_id=a.wa_message_id, meta_status='delivered', timestamp=d.now())
    await svc._send_to_recipient(db, c, None, r)
    outbound.cloud.send_template.assert_not_awaited()
    assert (await d.recovery(db, c.id, causes=['unconfirmed']))['eligible'] == 0


async def test_recency_recovery_requires_explicit_selection(db):
    c, r = await seed(db, status='skipped')
    r.skip_reason = 'recency_block'
    assert (await d.recovery(db, c.id))['eligible'] == 0
    assert (await d.recovery(db, c.id, execute=True, causes=['recency_block']))['eligible'] == 1
    assert r.status == 'pending'


async def test_manual_meta_limit_recovery_exceeds_auto_limit_but_waits_24h(db, monkeypatch):
    c, r = await seed(db, auto=False)
    for index in range(3):
        a = await attempt(db, c, r, index)
        await d.failure(db, c, r, a, 131049, 'Limit', AT + timedelta(hours=24 * index), schedule=False)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=49))
    result = await d.recovery(db, c.id, execute=True, causes=['meta_limit'])
    assert result['eligible'] == 1 and r.send_after == AT + timedelta(hours=72)
    assert not c.auto_retry_meta_limits


async def test_graphql_accepts_causes_and_returns_typed_cause_details(db, monkeypatch):
    import strawberry
    import app.graphql.campaigns.mutations as mutations
    import app.graphql.campaigns.queries as queries
    c, r = await seed(db, status='failed')
    r.error = 'Meta 131000: temporary'
    for module in (queries, mutations):
        monkeypatch.setattr(module, 'require_capability', AsyncMock(return_value=None))
    schema = strawberry.Schema(query=queries.CampaignsQuery, mutation=mutations.CampaignsMutation)
    context = SimpleNamespace(db=db, user=SimpleNamespace(id=1))
    result = await schema.execute('''query($id:Int!,$causes:[String!]) {
        previewCampaignRecovery(campaignId:$id,causes:$causes) {
            eligible causes { key label eligible blocked selectedByDefault } recipients error
        }
    }''', variable_values={'id': c.id, 'causes': ['meta_temporary']}, context_value=context)
    assert not result.errors
    assert result.data['previewCampaignRecovery']['eligible'] == 1 and r.status == 'failed'
    result = await schema.execute('''mutation($id:Int!,$causes:[String!]) {
        recoverCampaignRecipients(campaignId:$id,causes:$causes) { success pending error }
    }''', variable_values={'id': c.id, 'causes': []}, context_value=context)
    assert not result.errors and result.data['recoverCampaignRecipients']['pending'] == 0
    assert r.status == 'failed'


async def test_latest_real_error_survives_a_local_omission_and_not_sent_attempt(db):
    c, r = await seed(db, status='skipped')
    r.skip_reason, r.error = 'daily_cap', None
    a = await attempt(db, c, r)
    a.state, a.error_code, a.error_detail = 'failed', 131048, 'Spam limit'
    local = await attempt(db, c, r, 1)
    local.state, local.wa_message_id = 'not_sent', None
    assert d.recovery_cause(r, [a, local])[0] == 'other_error'
    assert (await d.recovery(db, c.id))['eligible'] == 0


async def test_stale_invalid_request_can_recover_to_a_different_valid_number(db):
    c, r = await seed(db, status='sending', number='1234')
    r.updated_at = d.now() - timedelta(hours=25)
    a = await attempt(db, c, r)
    a.state, a.started_at, a.accepted_at, a.wa_message_id = 'started', r.updated_at, None, None
    person = await db.get(People, r.person_id)
    person.phone_number = '5218712223333'
    await db.flush()
    assert (await d.recovery(db, c.id, execute=True, causes=['bad_number']))['eligible'] == 1
    assert r.wa_id == '5218712223333'


async def test_duplicate_owner_does_not_change_when_cause_selection_changes(db, monkeypatch):
    c, first = await seed(db, policy='allow_additional', status='sent')
    await attempt(db, c, first)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=25))
    alias = CampaignRecipient(campaign_id=c.id, person_id=first.person_id, wa_id=first.wa_id,
                              dedup_key=f'alias-recovery:{c.id}', status='skipped', skip_reason='recency_block')
    db.add(alias)
    await db.flush()
    for selected in (None, ['unconfirmed'], ['recency_block']):
        preview = await d.recovery(db, c.id, causes=selected)
        rows = {row['recipient_id']: row for row in preview['recipients']}
        assert rows[first.id]['eligible'] and rows[alias.id]['reason'] == 'duplicates'
    assert (await d.recovery(db, c.id, execute=True, causes=['recency_block']))['eligible'] == 0


async def test_new_live_attempt_is_not_overridden_by_old_manual_authorization(db, monkeypatch):
    c, r = await seed(db, policy='allow_additional', status='sent')
    await attempt(db, c, r)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=25))
    await d.recovery(db, c.id, execute=True, causes=['unconfirmed'])
    live = await attempt(db, c, r, 2)
    live.started_at, live.state = d.now(), 'started'
    await db.flush()
    assert await d.check_recipient(db, c, r) == 'uncertain_previous_send'


async def test_send_guard_refreshes_a_delivery_written_while_models_were_cached(db, monkeypatch):
    from sqlalchemy import update
    c, r = await seed(db, policy='allow_additional', status='sent')
    a = await attempt(db, c, r)
    monkeypatch.setattr(d, 'now', lambda: AT + timedelta(hours=25))
    await d.recovery(db, c.id, execute=True, causes=['unconfirmed'])
    await db.execute(update(Attempt).where(Attempt.id == a.id).values(delivered_at=d.now(), state='delivered')
                     .execution_options(synchronize_session=False))
    assert a.delivered_at is None  # Simulate a webhook processed by a different session.
    assert await d.check_recipient(db, c, r) == 'already_delivered'
    assert a.delivered_at is not None
