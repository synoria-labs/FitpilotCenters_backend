"""One-time, idempotent merge of duplicate WhatsApp contacts.

Mexican WhatsApp numbers are stored in two equivalent forms (``52...`` vs ``521...``).
Before the normalized contact resolver landed, sends could create a second contact +
conversation for a number that already existed, so the same person showed up as two
chats. This script consolidates those duplicates:

  - groups contacts by their last-10 normalized digits;
  - for each group with >1 contact, picks a deterministic canonical contact;
  - repoints every message of the group onto the canonical contact and a single
    target conversation;
  - deletes the now-empty extra conversations and the non-canonical contacts.

``message_statuses`` and ``media`` reference ``message_id`` (not contact/conversation),
so repointing messages never orphans them.

Idempotent: with no duplicates it is a no-op. Run ``--dry-run`` first (reports the plan
without writing); the real run commits in a single transaction.

Usage (from the ``backend`` directory):
    python -m scripts.merge_duplicate_whatsapp_contacts --dry-run
    python -m scripts.merge_duplicate_whatsapp_contacts
"""
import argparse
import asyncio
import re
from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.postgresql import async_session_factory
from app.models import Contact, Conversation, Message


def _digits(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def _group_key(contact: Contact) -> Optional[str]:
    """Last-10 digits of the contact's number (shared by the 52/521 forms)."""
    digits = _digits(contact.wa_id) or _digits(contact.phone_number)
    if not digits:
        return None
    return digits[-10:] if len(digits) >= 10 else digits


def _is_authoritative_form(contact: Contact) -> bool:
    """The Mexican inbound ``521...`` (13-digit) form is the canonical wa_id."""
    digits = _digits(contact.wa_id)
    return digits.startswith("521") and len(digits) >= 13


def _pick_canonical(group: List[Contact], msg_counts: Dict[int, int]) -> Contact:
    """Most messages -> authoritative 521 form -> longest wa_id -> lowest id."""
    return sorted(
        group,
        key=lambda c: (
            -msg_counts.get(c.id, 0),
            0 if _is_authoritative_form(c) else 1,
            -len(_digits(c.wa_id)),
            c.id,
        ),
    )[0]


async def _message_counts(db: AsyncSession) -> Dict[int, int]:
    rows = (
        await db.execute(
            select(Message.contact_id, func.count()).group_by(Message.contact_id)
        )
    ).all()
    return {contact_id: count for contact_id, count in rows}


async def _conversations_for(db: AsyncSession, contact_ids: List[int]):
    """Return (conv_id, contact_id, last_message_ts) for the given contacts."""
    rows = (
        await db.execute(
            select(
                Conversation.id,
                Conversation.contact_id,
                func.max(Message.timestamp),
            )
            .select_from(Conversation)
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .where(Conversation.contact_id.in_(contact_ids))
            .group_by(Conversation.id, Conversation.contact_id)
        )
    ).all()
    return rows


async def _merge_group(
    db: AsyncSession,
    group: List[Contact],
    msg_counts: Dict[int, int],
    dry_run: bool,
) -> dict:
    contact_ids = [c.id for c in group]
    canonical = _pick_canonical(group, msg_counts)
    dup_ids = [cid for cid in contact_ids if cid != canonical.id]

    conv_rows = await _conversations_for(db, contact_ids)
    conv_ids = [r[0] for r in conv_rows]

    # Target conversation: the one with the most recent message, else lowest id.
    if conv_rows:
        target_conv_id = sorted(
            conv_rows,
            key=lambda r: (r[2] is not None, r[2], -r[0]),
            reverse=True,
        )[0][0]
    else:
        target_conv_id = None

    # Count messages that will be repointed (by contact or by conversation).
    repoint_filter = Message.contact_id.in_(contact_ids)
    if conv_ids:
        repoint_filter = repoint_filter | Message.conversation_id.in_(conv_ids)
    msg_total = (
        await db.execute(select(func.count()).select_from(Message).where(repoint_filter))
    ).scalar_one()

    summary = {
        "key": _group_key(canonical),
        "canonical_id": canonical.id,
        "canonical_wa_id": canonical.wa_id,
        "removed_contact_ids": dup_ids,
        "removed_conversation_ids": [cid for cid in conv_ids if cid != target_conv_id],
        "repointed_messages": msg_total,
    }

    if dry_run:
        return summary

    # 1) Ensure a single target conversation owned by the canonical contact.
    if target_conv_id is None:
        target_conv = Conversation(contact_id=canonical.id, status="active")
        db.add(target_conv)
        await db.flush()
        target_conv_id = target_conv.id
    else:
        await db.execute(
            update(Conversation)
            .where(Conversation.id == target_conv_id)
            .values(contact_id=canonical.id)
        )

    # 2) Repoint every message of the group onto the canonical contact + target conv.
    await db.execute(
        update(Message)
        .where(repoint_filter)
        .values(contact_id=canonical.id, conversation_id=target_conv_id)
    )

    # 3) Carry over name/profile_name if the canonical lacks them.
    if not canonical.name:
        for c in group:
            if c.name:
                canonical.name = c.name
                break
    if not canonical.profile_name:
        for c in group:
            if c.profile_name:
                canonical.profile_name = c.profile_name
                break
    await db.flush()

    # 4) Delete the now-empty extra conversations, then the non-canonical contacts.
    stale_conv_ids = [cid for cid in conv_ids if cid != target_conv_id]
    if stale_conv_ids:
        await db.execute(delete(Conversation).where(Conversation.id.in_(stale_conv_ids)))
    if dup_ids:
        await db.execute(delete(Contact).where(Contact.id.in_(dup_ids)))

    return summary


async def merge_duplicates(db: AsyncSession, dry_run: bool) -> List[dict]:
    """Core merge over a given session. Commits (or rolls back, if dry-run) at the end.

    Returns one summary dict per duplicate group. Separated from ``run`` so it can be
    driven by tests with their own (rolled-back) session.
    """
    contacts = (await db.execute(select(Contact))).scalars().all()
    groups: Dict[str, List[Contact]] = defaultdict(list)
    for contact in contacts:
        key = _group_key(contact)
        if key:
            groups[key].append(contact)
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    if not dup_groups:
        return []

    msg_counts = await _message_counts(db)
    summaries: List[dict] = []
    for _key, group in sorted(dup_groups.items()):
        summaries.append(await _merge_group(db, group, msg_counts, dry_run))

    if dry_run:
        await db.rollback()
    else:
        await db.commit()
    return summaries


async def run(dry_run: bool) -> None:
    async with async_session_factory() as db:
        total_contacts = (
            await db.execute(select(func.count()).select_from(Contact))
        ).scalar_one()
        summaries = await merge_duplicates(db, dry_run)

        if not summaries:
            print(f"No duplicate contacts found among {total_contacts} contacts. Nothing to do.")
            return

        print(
            f"{'[DRY-RUN] ' if dry_run else ''}Found {len(summaries)} duplicate group(s) "
            f"among {total_contacts} contacts."
        )
        total_removed = 0
        for s in summaries:
            total_removed += len(s["removed_contact_ids"])
            print(
                f"  • key …{s['key']}: keep contact #{s['canonical_id']} "
                f"({s['canonical_wa_id']}); "
                f"remove contacts {s['removed_contact_ids']}; "
                f"drop conversations {s['removed_conversation_ids']}; "
                f"repoint {s['repointed_messages']} message(s)."
            )

        if dry_run:
            print(f"[DRY-RUN] Would remove {total_removed} duplicate contact(s). No changes written.")
        else:
            print(f"Done. Removed {total_removed} duplicate contact(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the merge plan without writing any changes.",
    )
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
