"""Campaign delivery evidence, bounded retries and explicit recovery. No sends here."""
from datetime import datetime, timedelta, timezone
import re

from sqlalchemy import func, select, text, cast, literal
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models import Campaign, CampaignRecipient
from app.models.campaignsModel import CampaignSendAttempt as Attempt
from app.models.whatsappModel import Message, MessageStatus, WebhookLog

POLICIES = ("respect", "defer", "allow_additional")


class DeliveryBlocked(Exception):
    """A policy/receipt changed while the durable request checkpoint was committed."""


def now():
    return datetime.now(timezone.utc)


def phone_key(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 10:
        return "52" + digits
    if len(digits) == 13 and digits.startswith("521"):
        return "52" + digits[3:]
    return digits


def next_day():
    from app.services import whatsapp_outbound as outbound
    _, _, zone = outbound._quiet_bounds()
    midnight = (now().astimezone(zone) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return outbound.next_allowed_send_at(midnight).astimezone(timezone.utc)


async def closed_event(db, campaign):
    if not campaign.special_event_id:
        return False
    from app.models import SpecialEvent
    event = await db.get(SpecialEvent, campaign.special_event_id)
    return event is None or event.status != "published" or event.start_at <= now()


async def beyond_event(db, campaign, due):
    if campaign.special_event_id:
        from app.models import SpecialEvent
        event = await db.get(SpecialEvent, campaign.special_event_id)
        return event is None or due >= event.start_at
    return False


async def phone_lock(db, campaign_id, key):
    from app.services.whatsapp_outbound import _lock_key
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": _lock_key(f"campaign:{campaign_id}:{key}")})


async def attempts_for(db, campaign_id):
    return list((await db.scalars(select(Attempt).where(Attempt.campaign_id == campaign_id)
                                  .order_by(Attempt.id))).all())


async def check_recipient(db, campaign, recipient, *, ignore_attempt_id=None):
    """Under the phone lock, choose one eligible owner and never resend a live request."""
    from app.crud.campaignsCrud import opted_out_person_ids
    key = phone_key(recipient.wa_id or recipient.phone_e164)
    recipient.phone_key = key or None
    if not key:
        return "no_phone"
    if await closed_event(db, campaign):
        return "event_closed"
    if campaign.status == "canceled":
        return "campaign_canceled"
    peers = list((await db.scalars(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign.id).order_by(CampaignRecipient.id))).all())
    peers = [r for r in peers if phone_key(r.wa_id or r.phone_e164) == key]
    history = list((await db.scalars(select(Attempt).where(
        Attempt.campaign_id == campaign.id, Attempt.phone_key == key))).all())
    history = [a for a in history if a.id != ignore_attempt_id]
    if any(a.delivered_at or a.read_at for a in history) or any(r.delivered_at or r.read_at or r.replied_at for r in peers):
        return "already_delivered"
    if any(a.state in ("started", "uncertain") for a in history):
        return "uncertain_previous_send"
    if any(a.state in ("accepted", "sent") for a in history):
        return "awaiting_delivery"
    blocked = await opted_out_person_ids(db, [r.person_id for r in peers if r.person_id])
    eligible = [r for r in peers if r.person_id not in blocked and r.status != "opted_out"]
    if recipient.person_id in blocked:
        return "no_consent"
    if eligible and recipient.id != min(r.id for r in eligible):
        return "duplicate_phone"
    return None


async def failure(db, campaign, recipient, attempt, code, detail, timestamp=None, errors=None, *, schedule=True):
    """A delivery proof wins over a late error; failure never removes its evidence."""
    at = timestamp or now()
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    attempt.failed_at = attempt.failed_at or at
    attempt.error_code = code
    attempt.error_detail = detail
    attempt.errors = errors
    if attempt.delivered_at or attempt.read_at:
        return
    attempt.state = "failed"
    if recipient.replied_at:
        return
    # A callback for an old failed request must not reset a newer active/delivered request.
    newer = await db.scalar(select(Attempt.id).where(
        Attempt.recipient_id == recipient.id, Attempt.id > attempt.id,
        Attempt.state.in_(("started", "uncertain", "accepted", "sent", "delivered", "read"))).limit(1))
    if newer:
        return
    recipient.error = f"Meta {code}: {detail}" if code else detail
    recipient.status = "failed"
    recipient.skip_reason = "meta_rejected"
    recipient.send_after = None
    if code == 131048:
        campaign.status = "paused"
        campaign.updated_at = now()
    if code != 131049 or not schedule or not campaign.auto_retry_meta_limits or campaign.status in ("paused", "canceled"):
        return
    if await closed_event(db, campaign):
        return
    # This attempt may not yet have been flushed by callers using autoflush=False.
    await db.flush()
    failures = await db.scalar(select(func.count(Attempt.id)).where(
        Attempt.campaign_id == campaign.id, Attempt.phone_key == attempt.phone_key, Attempt.error_code == 131049,
        Attempt.state == "failed"))
    if failures <= 2:
        due = at + timedelta(hours=24 if failures == 1 else 48)
        if await beyond_event(db, campaign, due):
            return
        recipient.status = "pending"
        recipient.skip_reason = "meta_retry"
        recipient.send_after = due
        if campaign.status != "sending":
            await db.flush()
            campaign.status = "scheduled"
            campaign.scheduled_at = await db.scalar(select(func.min(func.coalesce(CampaignRecipient.send_after, now()))).where(
                CampaignRecipient.campaign_id == campaign.id, CampaignRecipient.status == "pending"))


async def apply_status(db, *, wa_message_id, meta_status, timestamp, errors=None):
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    a = await db.scalar(select(Attempt).where(Attempt.wa_message_id == wa_message_id).with_for_update())
    if a is None:
        r = await db.scalar(select(CampaignRecipient).where(CampaignRecipient.wa_message_id == wa_message_id))
        if r is None:
            return False
        # Upgrade exact legacy associations lazily; old campaigns keep retries disabled.
        await db.execute(pg_insert(Attempt).values(
            campaign_id=r.campaign_id, recipient_id=r.id, phone_key=phone_key(r.wa_id or r.phone_e164),
            state="accepted", started_at=r.sent_at or now(), accepted_at=r.sent_at or now(),
            wa_message_id=wa_message_id, message_id=r.message_id,
            delivered_at=r.delivered_at, read_at=r.read_at,
        ).on_conflict_do_nothing(index_elements=["wa_message_id"]))
        a = await db.scalar(select(Attempt).where(Attempt.wa_message_id == wa_message_id).with_for_update())
    r = await db.get(CampaignRecipient, a.recipient_id)
    c = await db.get(Campaign, a.campaign_id)
    if meta_status == "failed":
        # A repeated webhook does not reset the retry timer or consume another retry.
        if a.failed_at is not None:
            return True
        error = (errors or [{}])[0]
        await failure(db, c, r, a, error.get("code"), error.get("message") or error.get("title") or "Rechazado",
                      timestamp, errors)
    elif meta_status in ("sent", "delivered", "read"):
        field = meta_status + "_at"
        setattr(a, field, getattr(a, field) or timestamp)
        rank = {"started": 0, "uncertain": 0, "failed": 0, "accepted": 1, "sent": 2, "delivered": 3, "read": 4}
        if rank[meta_status] > rank.get(a.state, 0):
            a.state = meta_status
        if meta_status in ("delivered", "read"):
            setattr(r, field, getattr(r, field) or timestamp)
            r.send_after = None
            if r.status != "replied" and (meta_status == "read" or not r.read_at):
                r.status = meta_status
            r.skip_reason = None
        elif r.wa_message_id == wa_message_id and r.status in ("sending", "sent"):
            r.status = "sent"
    await db.flush()
    return True


async def replay_receipts(db, attempt):
    """Callbacks may precede local message persistence; recover them by exact wamid."""
    payloads = (await db.scalars(select(WebhookLog.payload).where(
        WebhookLog.created_at >= attempt.started_at.replace(tzinfo=None) - timedelta(minutes=5),
        func.jsonb_path_exists(cast(WebhookLog.payload, JSONB),
                              cast('$.entry[*].changes[*].value.statuses[*] ? (@.id == $id)', JSONPATH),
                              literal({"id": attempt.wa_message_id}, type_=JSONB)),
    ))).all()
    for p in payloads:
        for entry in p.get("entry", []):
            for change in entry.get("changes", []):
                for s in change.get("value", {}).get("statuses", []):
                    if s.get("id") == attempt.wa_message_id:
                        ts = datetime.fromtimestamp(int(s["timestamp"]), timezone.utc)
                        await apply_status(db, wa_message_id=attempt.wa_message_id,
                                           meta_status=s.get("status"), timestamp=ts, errors=s.get("errors"))


async def metrics(db, campaign_id, *, context=None):
    """Message counts are independent of mutable recipient statuses and number of retries."""
    if context is None:
        c = await db.get(Campaign, campaign_id)
        rows = list((await db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id))).all())
        history = await attempts_for(db, campaign_id)
    else:
        c, rows, history, _old = context
    proofs = {}
    for a in history:
        if a.wa_message_id:
            proofs[a.wa_message_id] = {
                "accepted": bool(a.accepted_at), "sent": bool(a.sent_at or a.delivered_at or a.read_at),
                "delivered": bool(a.delivered_at or a.read_at), "read": bool(a.read_at),
            }
    # Compatibility before reconciliation: existing messages and statuses are still evidence.
    mids = {r.message_id for r in rows if r.message_id}
    old = _old if context is not None else ((await db.execute(select(Message.wa_message_id, MessageStatus.status).join(
        MessageStatus, MessageStatus.message_id == Message.id).where(
            Message.id.in_(mids), Message.wa_message_id.isnot(None)))).all() if mids else [])
    for r in rows:
        if r.wa_message_id:
            proofs.setdefault(r.wa_message_id, {"accepted": True, "sent": False, "delivered": False, "read": False})
    for wamid, state in old:
        p = proofs.setdefault(wamid, {"accepted": True, "sent": False, "delivered": False, "read": False})
        if state in ("sent", "delivered", "read"):
            p["sent"] = True
        if state in ("delivered", "read"):
            p["delivered"] = True
        if state == "read":
            p["read"] = True
    counts = {s: sum(p[s] for p in proofs.values()) for s in ("accepted", "sent", "delivered", "read")}
    error_counts = {}
    for a in history:
        if a.error_code is not None:
            key = str(a.error_code)
            error_counts[key] = error_counts.get(key, 0) + 1
    return dict(counts, unique_phones=len({phone_key(r.wa_id or r.phone_e164) for r in rows}
                                         - {""}),
                duplicates=sum(r.skip_reason == "duplicate_phone" for r in rows),
                rejected=sum(a.state == "failed" for a in history),
                needs_review=sum(r.status == "needs_review" for r in rows),
                retry_pending=sum(r.status == "pending" and r.send_after is not None for r in rows),
                replied=len({phone_key(r.wa_id or r.phone_e164) for r in rows if r.replied_at}),
                error_counts=error_counts, updated_at=now(), period_start=c.started_at if c else None,
                period_end=now(), template_id=c.template_id if c else None)


async def recovery(db, campaign_id, *, execute=False):
    """Same eligibility calculation powers preview and execution. Never calls Meta."""
    from app.crud.campaignsCrud import opted_out_person_ids
    c = await db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update()) if execute else await db.get(Campaign, campaign_id)
    if c is None or c.status in ("sending", "canceled"):
        raise ValueError("Pausa el envío antes de recuperar; una campaña cancelada no se puede recuperar.")
    if await closed_event(db, c):
        raise ValueError("El evento ya comenzó o no está publicado.")
    rows = list((await db.scalars(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id).order_by(CampaignRecipient.id))).all())
    blocked = await opted_out_person_ids(db, [r.person_id for r in rows if r.person_id])
    history = await attempts_for(db, campaign_id)
    seen = set()
    counts = {"eligible": 0, "duplicates": 0, "delivered": 0, "blocked": 0, "review": 0, "scheduled": 0}
    entries = []
    for r in rows:
        key = phone_key(r.wa_id or r.phone_e164)
        peer_attempts = [a for a in history if a.phone_key == key]
        reason, due = None, now()
        if not key or r.person_id in blocked or r.status == "opted_out":
            reason = "blocked"
        elif any(a.delivered_at or a.read_at for a in peer_attempts) or any(
                (x.delivered_at or x.read_at or x.replied_at) and phone_key(x.wa_id or x.phone_e164) == key for x in rows):
            reason = "delivered"
        elif key in seen:
            reason = "duplicates"
        elif any(a.state in ("started", "uncertain", "accepted", "sent") for a in peer_attempts):
            reason = "review"
        else:
            seen.add(key)
            latest = max(peer_attempts, key=lambda a: a.id, default=None)
            if latest and latest.error_code == 131049:
                n = sum(a.error_code == 131049 for a in peer_attempts)
                if not c.auto_retry_meta_limits or n > 2:
                    reason = "review"
                else:
                    due = max(due, latest.failed_at + timedelta(hours=24 if n == 1 else 48))
            elif latest and latest.error_code:
                reason = "review"
            elif r.status in ("failed", "needs_review") or (r.wa_message_id and not latest):
                reason = "review"
            if reason is None:
                from app.crud.whatsappCrud import find_contact_by_number, count_marketing_sends_today
                from app.core.outbound_config import outbound_config
                contact = await find_contact_by_number(db, key)
                cap = outbound_config.MARKETING_DAILY_CAP
                if c.daily_cap_policy != "allow_additional" and contact and cap > 0:
                    if await count_marketing_sends_today(db, contact.id) >= cap:
                        if c.daily_cap_policy == "defer":
                            due = max(due, next_day())
                        else:
                            reason = "blocked"
        if reason:
            counts[reason] += 1
        else:
            from app.services.whatsapp_outbound import next_allowed_send_at
            due = next_allowed_send_at(due).astimezone(timezone.utc)
            if await beyond_event(db, c, due):
                counts["blocked"] += 1
                entries.append({"recipient_id": r.id, "reason": "event_closed", "send_after": None})
                continue
            counts["eligible"] += 1
            if due > now() + timedelta(seconds=1):
                counts["scheduled"] += 1
            if execute:
                r.status, r.skip_reason, r.send_after = "pending", "recovery", due
                r.phone_key = key
        entries.append({"recipient_id": r.id, "reason": reason or "eligible", "send_after": due if not reason else None})
    if execute and counts["eligible"]:
        c.status = "scheduled"
        c.scheduled_at = min(e["send_after"] for e in entries if e["reason"] == "eligible")
        c.finished_at = None
        await db.commit()
    for entry in entries:
        if entry["send_after"] is not None:
            entry["send_after"] = entry["send_after"].isoformat()
    return dict(counts, recipients=entries)


async def reconcile(db, campaign_id, *, apply=False):
    """Exact, idempotent historical repair; no queue/consent/provider side effects."""
    c = await db.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update()) if apply else await db.get(Campaign, campaign_id)
    if c is None:
        raise ValueError("Campaña no encontrada.")
    if apply and c.status == "sending":
        raise ValueError("Pausa el envío antes de reparar el historial.")
    rows = list((await db.scalars(select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id))).all())
    history = await attempts_for(db, campaign_id)
    known = {a.wa_message_id for a in history}
    mids = {r.message_id for r in rows if r.message_id}
    messages = {m.id: m for m in (await db.scalars(select(Message).where(Message.id.in_(mids)))).all()}
    receipts = {}
    for event in (await db.scalars(select(MessageStatus).where(MessageStatus.message_id.in_(mids)))).all():
        receipts.setdefault(event.message_id, []).append(event)
    earliest = min((m.timestamp for m in messages.values()), default=now().replace(tzinfo=None))
    payloads = (await db.scalars(select(WebhookLog.payload).where(
        WebhookLog.event_type == "whatsapp_business_account",
        WebhookLog.created_at >= earliest - timedelta(minutes=5)))).all()
    errors = {}
    for payload in payloads:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for st in change.get("value", {}).get("statuses", []):
                    if st.get("errors"):
                        errors[st.get("id")] = st["errors"]
    found, ambiguous = [], 0
    for r in rows:
        if not r.wa_message_id or r.wa_message_id in known:
            continue
        m = messages.get(r.message_id)
        if m is None or m.wa_message_id != r.wa_message_id or m.direction != "outbound":
            ambiguous += 1
            if apply:
                r.status, r.skip_reason, r.send_after = "needs_review", "legacy_link_mismatch", None
            continue
        at = m.timestamp.replace(tzinfo=timezone.utc) if m.timestamp.tzinfo is None else m.timestamp
        a = Attempt(campaign_id=campaign_id, recipient_id=r.id, phone_key=phone_key(r.wa_id or r.phone_e164),
                    state="accepted", started_at=at, accepted_at=at, wa_message_id=r.wa_message_id, message_id=m.id)
        for st in receipts.get(m.id, []):
            ts = st.timestamp.replace(tzinfo=timezone.utc) if st.timestamp.tzinfo is None else st.timestamp
            if st.status in ("sent", "delivered", "read", "failed"):
                setattr(a, st.status + "_at", ts)
        a.errors = errors.get(a.wa_message_id)
        if a.errors:
            a.error_code = a.errors[0].get("code")
            a.error_detail = a.errors[0].get("message") or a.errors[0].get("title")
        a.state = "read" if a.read_at else "delivered" if a.delivered_at else "failed" if a.failed_at else "sent" if a.sent_at else "accepted"
        found.append(a)
        if apply:
            db.add(a)
            r.phone_key = a.phone_key
            if a.read_at or a.delivered_at:
                r.status = "replied" if r.replied_at else a.state
                r.read_at, r.delivered_at = a.read_at, a.delivered_at
                r.skip_reason, r.send_after = None, None
            elif a.state == "failed":
                r.status, r.skip_reason = "failed", "meta_rejected"
                r.error, r.send_after = f"Meta {a.error_code}: {a.error_detail}", None
            elif r.status in ("pending", "sending", "skipped", "failed"):
                r.status, r.skip_reason, r.send_after = "needs_review", "awaiting_delivery", None
    replies = 0
    mapping = {a.wa_message_id: a for a in history + found}
    if mapping:
        inbound = (await db.scalars(select(Message).where(Message.direction == "inbound",
                                   Message.context_message_id.in_(mapping)))).all()
        by_id = {r.id: r for r in rows}
        for msg in inbound:
            a = mapping[msg.context_message_id]
            original = messages.get(a.message_id)
            if original and original.contact_id == msg.contact_id:
                replies += 1
                if apply:
                    r = by_id[a.recipient_id]
                    r.status = "replied"
                    r.replied_at = r.replied_at or msg.timestamp.replace(tzinfo=timezone.utc)
    if apply:
        attempted_phones = {phone_key(r.wa_id or r.phone_e164) for r in rows if r.wa_message_id}
        for r in rows:
            key = phone_key(r.wa_id or r.phone_e164)
            r.phone_key = key or None
            if key in attempted_phones and r.status == "skipped" and not r.wa_message_id:
                r.skip_reason = "duplicate_phone"
        await db.commit()
    return {"matched": len(found), "ambiguous": ambiguous, "replies": replies, "applied": apply}


async def settle_stale_intents(db, campaign_id):
    stale = (await db.scalars(select(Attempt).where(
        Attempt.campaign_id == campaign_id, Attempt.state.in_(("started", "accepted")),
        Attempt.message_id.is_(None),
        Attempt.started_at < now() - timedelta(minutes=10)))).all()
    for a in stale:
        if a.state == "started":
            a.state = "uncertain"
        r = await db.get(CampaignRecipient, a.recipient_id)
        if not r.delivered_at and not r.read_at:
            r.status, r.skip_reason = "needs_review", "uncertain_send"
    await db.flush()


async def metric_contexts(db, ids):
    """Load a page's evidence in four queries, regardless of campaign count."""
    campaigns = (await db.scalars(select(Campaign).where(Campaign.id.in_(ids)))).all()
    contexts = {cid: [None, [], [], []] for cid in ids}
    for c in campaigns:
        contexts[c.id][0] = c
    rows = (await db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id.in_(ids)))).all()
    owners = {}
    for r in rows:
        contexts[r.campaign_id][1].append(r)
        if r.message_id:
            owners.setdefault(r.message_id, set()).add(r.campaign_id)
    history = (await db.scalars(select(Attempt).where(Attempt.campaign_id.in_(ids)))).all()
    for a in history:
        contexts[a.campaign_id][2].append(a)
    events = (await db.execute(select(Message.id, Message.wa_message_id, MessageStatus.status).join(
        MessageStatus, MessageStatus.message_id == Message.id).where(
            Message.id.in_(owners), Message.wa_message_id.isnot(None)))).all() if owners else []
    for mid, wamid, status in events:
        for cid in owners[mid]:
            contexts[cid][3].append((wamid, status))
    return contexts
