"""Read-only button analytics from exact inbound message evidence, including old attempts."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import unicodedata

from sqlalchemy import cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from app.models import Campaign, CampaignRecipient, CampaignVariant, Contact, Message, Payment, People, WhatsAppTemplate, WebhookLog
from app.models.campaignsModel import CampaignSendAttempt as Attempt
from app.services.campaign_delivery import phone_key
from app.services.whatsapp_template_components import buttons_from_components

COUNT_FIELDS = ("button_clicks", "button_clickers", "text_replies", "text_reply_people", "converted_clickers")


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def key_for(label, kind="QUICK_REPLY"):
    text = " ".join(unicodedata.normalize("NFKC", label).split()).casefold()
    return hashlib.sha256(f"{kind}:{text}".encode()).hexdigest()


def group_for(label, kind, *, historical=False):
    measurable = kind == "QUICK_REPLY"
    return dict(key=key_for(label, kind), label=label, kind=kind, measurable=measurable,
                clicks=0 if measurable else None, unique_people=0 if measurable else None,
                converted_people=0 if measurable else None, historical=historical)


def empty_report():
    return dict.fromkeys(COUNT_FIELDS, 0) | {"buttons": [], "events": [], "updated_at": datetime.now(timezone.utc)}


async def _interactive_evidence(db, rows):
    """A legacy interactive row can also be a list reply; require the actual callback subtype."""
    ids = [row.wa_message_id for row in rows if row.message_type == "interactive" and row.wa_message_id]
    if not ids:
        return {}
    payloads = (await db.scalars(select(WebhookLog.payload).where(func.jsonb_path_exists(
        cast(WebhookLog.payload, JSONB),
        cast('$.entry[*].changes[*].value.messages[*] ? (@.id == $ids[*])', JSONPATH),
        literal({"ids": ids}, type_=JSONB),
    )))).all()
    evidence = {}
    for payload in payloads:
        for entry in (payload or {}).get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                for msg in (change.get("value") or {}).get("messages", []) or []:
                    reply = (msg.get("interactive") or {}).get("button_reply")
                    if msg.get("id") in ids and msg.get("type") == "interactive" and isinstance(reply, dict):
                        if not all(isinstance(value, str) for value in
                                   (reply.get("title"), (msg.get("context") or {}).get("id"), msg.get("from"))):
                            continue
                        value = (reply["title"], msg["context"]["id"], phone_key(msg["from"]))
                        evidence.setdefault(msg["id"], set()).add(value)
    return {mid: next(iter(values)) for mid, values in evidence.items() if len(values) == 1}


async def reports(db, campaign_ids, *, contexts=None):
    ids = list(set(campaign_ids))
    if not ids:
        return {}
    if contexts is None:
        campaigns = list((await db.scalars(select(Campaign).where(Campaign.id.in_(ids)))).all())
        rows = list((await db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id.in_(ids)))).all())
        attempts = list((await db.scalars(select(Attempt).where(Attempt.campaign_id.in_(ids)))).all())
    else:
        campaigns = [contexts[cid][0] for cid in ids if contexts.get(cid) and contexts[cid][0]]
        rows = [r for cid in ids for r in contexts[cid][1]]
        attempts = [a for cid in ids for a in contexts[cid][2]]
    output = {cid: empty_report() for cid in ids}
    if not campaigns:
        return output
    owners = {r.id: r for r in rows}
    refs = {r.wa_message_id for r in rows if r.wa_message_id} | {a.wa_message_id for a in attempts if a.wa_message_id}
    # Detect corrupt/ambiguous cross-campaign associations instead of picking whichever query won.
    if refs:
        attempts += list((await db.scalars(select(Attempt).where(
            Attempt.wa_message_id.in_(refs), Attempt.campaign_id.notin_(ids)))).all())
        other_rows = list((await db.scalars(select(CampaignRecipient).where(
            CampaignRecipient.wa_message_id.in_(refs), CampaignRecipient.campaign_id.notin_(ids)))).all())
    else:
        other_rows = []
    links = defaultdict(list)
    sent_times = {}
    for a in attempts:
        if a.wa_message_id:
            links[a.wa_message_id].append((a.campaign_id, a.recipient_id, phone_key(a.phone_key)))
            sent_times[a.wa_message_id] = utc(a.started_at)
    for r in rows + other_rows:
        if r.wa_message_id and r.wa_message_id not in links:
            links[r.wa_message_id].append((r.campaign_id, r.id, phone_key(r.wa_id or r.phone_e164)))
            sent_times[r.wa_message_id] = utc(r.sent_at)
    # Keep all legacy aliases for conflict detection; an attempt remains the authoritative link.
    attempt_refs = {a.wa_message_id for a in attempts if a.wa_message_id}
    for r in rows + other_rows:
        if r.wa_message_id and r.wa_message_id not in attempt_refs:
            item = (r.campaign_id, r.id, phone_key(r.wa_id or r.phone_e164))
            if item not in links[r.wa_message_id]:
                links[r.wa_message_id].append(item)

    variants = (await db.scalars(select(CampaignVariant).where(CampaignVariant.campaign_id.in_(ids)))).all()
    template_ids = {c.template_id for c in campaigns if c.template_id} | {v.template_id for v in variants if v.template_id}
    templates = {t.id: t for t in (await db.scalars(select(WhatsAppTemplate).where(WhatsAppTemplate.id.in_(template_ids)))).all()}
    template_by_campaign = {c.id: {c.template_id} for c in campaigns}
    for v in variants:
        if v.template_id:
            template_by_campaign[v.campaign_id].add(v.template_id)
    groups = {cid: {} for cid in ids}
    for cid, tids in template_by_campaign.items():
        for tid in sorted(t for t in tids if t):
            for button in buttons_from_components(templates[tid].components) if tid in templates else []:
                label, kind = str(button.get("text") or "Sin texto"), str(button.get("type") or "UNKNOWN").upper()
                group = group_for(label, kind)
                groups[cid].setdefault(group["key"], group)

    incoming = (await db.execute(select(Message, Contact).join(Contact, Contact.id == Message.contact_id).where(
        Message.direction == "inbound", Message.context_message_id.in_(refs),
        Message.message_type.in_(("button", "interactive", "text")), Message.wa_message_id.is_not(None),
    ).order_by(Message.timestamp, Message.id))).all() if refs else []
    raw = await _interactive_evidence(db, [m for m, _ in incoming])
    names = dict((await db.execute(select(People.id, People.full_name).where(
        People.id.in_([r.person_id for r in rows if r.person_id])))).all())
    payments = {p.id: p for p in (await db.scalars(select(Payment).where(Payment.id.in_(
        [r.conversion_payment_id for r in rows if r.converted and r.conversion_payment_id])))).all()}
    conversions = defaultdict(list)
    for r in rows:
        p = payments.get(r.conversion_payment_id)
        if r.converted and r.converted_at and p and p.status == "COMPLETED" and p.person_id == r.person_id:
            conversions[(r.campaign_id, phone_key(r.wa_id or r.phone_e164))].append((utc(r.converted_at), utc(p.paid_at)))
            conversions[(r.campaign_id, f"recipient:{r.id}")].append((utc(r.converted_at), utc(p.paid_at)))
    clickers, writers, converted = defaultdict(set), defaultdict(set), defaultdict(set)
    group_people, group_converted = defaultdict(set), defaultdict(set)
    # One Meta callback ID is one interaction even if old imports left duplicate message rows.
    by_message = defaultdict(list)
    for message, contact in incoming:
        by_message[message.wa_message_id].append((message, contact))
    for mid, copies in by_message.items():
        identities = {(m.context_message_id, m.message_type, m.text_content, phone_key(c.wa_id or c.phone_number)) for m, c in copies}
        if len(identities) != 1:
            continue
        message, contact = copies[0]
        targets = links.get(message.context_message_id, [])
        if len({(cid, key) for cid, _, key in targets}) != 1:
            continue
        cid, rid, sender = min(targets, key=lambda t: t[1])
        if cid not in output or rid not in owners or not sender or sender != phone_key(contact.wa_id or contact.phone_number):
            continue
        stamp = utc(message.timestamp)
        sent_at = sent_times.get(message.context_message_id)
        if not stamp or (sent_at and stamp < sent_at):
            continue
        if message.message_type == "text":
            output[cid]["text_replies"] += 1
            writers[cid].add(sender)
            continue
        label = message.text_content
        if message.message_type == "interactive":
            proof = raw.get(mid)
            if proof is None or proof[1] != message.context_message_id or proof[2] != sender:
                continue
            label = proof[0]
        label = str(label or "Botón sin identificar").strip() or "Botón sin identificar"
        group = group_for(label, "QUICK_REPLY", historical=True)
        key = group["key"]
        groups[cid].setdefault(key, group)
        group = groups[cid][key]
        after = [at for at, paid in conversions[(cid, sender)] + conversions[(cid, f"recipient:{rid}")]
                 if paid and at >= stamp and paid >= stamp]
        did_convert = bool(after)
        owner = owners[rid]
        name = names.get(owner.person_id) or contact.profile_name or contact.name or "Sin nombre"
        output[cid]["events"].append(dict(message_id=message.id, wa_message_id=mid, recipient_id=rid,
            person_name=name, phone=sender, button_key=key, button_label=label, clicked_at=stamp,
            converted=did_convert, converted_at=min(after) if after else None))
        group["clicks"] += 1
        clickers[cid].add(sender)
        group_people[(cid, key)].add(sender)
        if did_convert:
            converted[cid].add(sender)
            group_converted[(cid, key)].add(sender)
    for cid, report in output.items():
        for key, group in groups[cid].items():
            if group["measurable"]:
                group["unique_people"] = len(group_people[(cid, key)])
                group["converted_people"] = len(group_converted[(cid, key)])
        report.update(button_clicks=len(report["events"]), button_clickers=len(clickers[cid]),
                      text_reply_people=len(writers[cid]), converted_clickers=len(converted[cid]),
                      buttons=list(groups[cid].values()))
        report["events"].sort(key=lambda e: (e["clicked_at"], e["message_id"]), reverse=True)
    return output


async def analytics(db, campaign_id, *, button_key=None, limit=50, offset=0):
    if await db.get(Campaign, campaign_id) is None:
        raise ValueError("Campaña no encontrada.")
    report = (await reports(db, [campaign_id]))[campaign_id]
    events = [e for e in report.pop("events") if button_key is None or e["button_key"] == button_key]
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    return dict(report, items=events[offset:offset + limit], total=len(events), limit=limit, offset=offset)
