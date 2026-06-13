"""
CRUD operations for the WhatsApp chat feature.

Read helpers power the desktop chat UI (conversation list + message thread).
Write/upsert helpers are used by the inbound webhook ingest pipeline and the
outbound send mutation. Primary keys are assigned by the database (never set ``id``).
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

import json
import logging

from sqlalchemy import case, func, or_, select, and_, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    Contact,
    Conversation,
    Media,
    MembershipSubscription,
    Message,
    MessageStatus,
    People,
    PersonRole,
    Role,
)

logger = logging.getLogger(__name__)

# Customer-service window for free-form messages (WhatsApp Cloud API rule).
CONVERSATION_WINDOW = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------
@dataclass
class ChatContactData:
    id: int
    wa_id: str
    phone_number: str
    name: Optional[str]
    profile_name: Optional[str]
    member_id: Optional[int] = None
    member_name: Optional[str] = None


@dataclass(frozen=True)
class _MemberCandidate:
    id: int
    full_name: str
    wa_id: Optional[str]
    phone_number: Optional[str]
    active_membership_rank: int
    latest_membership_end: Optional[datetime]


@dataclass
class ChatMediaData:
    """Metadata of a message attachment, exposed to the desktop client."""

    id: int
    media_type: str
    mime_type: Optional[str]
    filename: Optional[str]
    caption: Optional[str]
    file_size: Optional[int]
    media_url: Optional[str]
    downloaded: bool
    download_failed: bool

    @classmethod
    def from_model(cls, m: Media) -> "ChatMediaData":
        return cls(
            id=m.id,
            media_type=m.media_type,
            mime_type=m.mime_type,
            filename=m.filename,
            caption=m.caption,
            file_size=m.file_size,
            media_url=m.media_url,
            downloaded=bool(m.downloaded),
            download_failed=bool(m.download_failed),
        )


@dataclass
class ChatMessageData:
    id: int
    conversation_id: int
    contact_id: int
    direction: str
    message_type: str
    text_content: Optional[str]
    timestamp: datetime
    wa_message_id: Optional[str]
    context_message_id: Optional[str] = None
    media_url: Optional[str] = None
    media: Optional[ChatMediaData] = None

    @classmethod
    def from_model(cls, m: Message) -> "ChatMessageData":
        # WhatsApp delivers one attachment per message; expose the first media
        # row. ``m.media`` is only populated when eager-loaded; guard against
        # lazy access on an async session.
        media = None
        try:
            media_items = m.__dict__.get("media")
            if media_items:
                media = ChatMediaData.from_model(media_items[0])
        except Exception:  # noqa: BLE001
            media = None
        return cls(
            id=m.id,
            conversation_id=m.conversation_id,
            contact_id=m.contact_id,
            direction=m.direction,
            message_type=m.message_type,
            text_content=m.text_content,
            timestamp=m.timestamp,
            wa_message_id=m.wa_message_id,
            context_message_id=m.context_message_id,
            media_url=media.media_url if media else None,
            media=media,
        )


@dataclass
class ConversationData:
    id: int
    status: str
    contact: ChatContactData
    last_message: Optional[ChatMessageData]
    last_activity: Optional[datetime]
    unread_count: int = 0


def _digits_only(value: Optional[str]) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _phone_match_keys(value: Optional[str]) -> set[str]:
    digits = _digits_only(value)
    if not digits:
        return set()

    keys = {digits}
    if len(digits) >= 10:
        local = digits[-10:]
        keys.add(local)
        keys.add(f"52{local}")
        keys.add(f"521{local}")

    if digits.startswith("521") and len(digits) >= 13:
        local = digits[3:]
        keys.update({local, f"52{local}"})
    elif digits.startswith("52") and len(digits) >= 12:
        local = digits[2:]
        keys.update({local, f"521{local}"})

    return {key for key in keys if key}


def _contact_match_keys(contact: Contact) -> set[str]:
    keys: set[str] = set()
    keys.update(_phone_match_keys(contact.wa_id))
    keys.update(_phone_match_keys(contact.phone_number))
    return keys


def _member_contact_match_rank(contact: Contact, member: _MemberCandidate) -> Optional[int]:
    contact_wa = _digits_only(contact.wa_id)
    contact_phone = _digits_only(contact.phone_number)
    contact_exact_values = {value for value in (contact_wa, contact_phone) if value}

    member_wa = _digits_only(member.wa_id)
    if member_wa and contact_wa and member_wa == contact_wa:
        return 0

    member_phone = _digits_only(member.phone_number)
    if member_phone and member_phone in contact_exact_values:
        return 1

    if member_phone and _phone_match_keys(member_phone).intersection(_contact_match_keys(contact)):
        return 2

    return None


def _member_sort_key(match_rank: int, member: _MemberCandidate) -> tuple:
    latest_end = member.latest_membership_end
    latest_end_ts = 0.0
    if latest_end:
        try:
            latest_end_ts = latest_end.timestamp()
        except Exception:  # noqa: BLE001
            latest_end_ts = 0.0

    return (
        match_rank,
        -int(member.active_membership_rank or 0),
        -latest_end_ts,
        member.full_name.lower(),
        member.id,
    )


def _digits_expr(column):
    return func.regexp_replace(func.coalesce(column, ""), r"\D", "", "g")


def _has_membership_subscription():
    return (
        select(MembershipSubscription.id)
        .where(MembershipSubscription.person_id == People.id)
        .exists()
    )


def _member_contact_sql_match_condition() -> object:
    member_wa = _digits_expr(People.wa_id)
    member_phone = _digits_expr(People.phone_number)
    contact_wa = _digits_expr(Contact.wa_id)
    contact_phone = _digits_expr(Contact.phone_number)

    member_phone_matches_wa = and_(
        func.length(member_phone) >= 10,
        func.length(contact_wa) >= 10,
        func.right(member_phone, 10) == func.right(contact_wa, 10),
    )
    member_phone_matches_phone = and_(
        func.length(member_phone) >= 10,
        func.length(contact_phone) >= 10,
        func.right(member_phone, 10) == func.right(contact_phone, 10),
    )

    return or_(
        and_(member_wa != "", contact_wa != "", member_wa == contact_wa),
        and_(member_phone != "", contact_wa != "", member_phone == contact_wa),
        and_(member_phone != "", contact_phone != "", member_phone == contact_phone),
        member_phone_matches_wa,
        member_phone_matches_phone,
    )


def _active_membership_rank_sq():
    now = datetime.now(timezone.utc)
    return (
        select(
            func.max(
                case(
                    (
                        and_(
                            MembershipSubscription.status == "active",
                            MembershipSubscription.end_at.isnot(None),
                            MembershipSubscription.end_at > now,
                        ),
                        1,
                    ),
                    else_=0,
                )
            )
        )
        .where(MembershipSubscription.person_id == People.id)
        .scalar_subquery()
    )


def _latest_membership_end_sq():
    return (
        select(func.max(MembershipSubscription.end_at))
        .where(MembershipSubscription.person_id == People.id)
        .scalar_subquery()
    )


async def _resolve_member_contacts(
    db: AsyncSession, contacts: List[Contact]
) -> Dict[int, _MemberCandidate]:
    if not contacts:
        return {}

    contact_wa_ids = {_digits_only(contact.wa_id) for contact in contacts}
    contact_wa_ids.discard("")

    contact_keys: set[str] = set()
    contact_last10: set[str] = set()
    for contact in contacts:
        contact_keys.update(_contact_match_keys(contact))
        for value in (contact.wa_id, contact.phone_number):
            digits = _digits_only(value)
            if len(digits) >= 10:
                contact_last10.add(digits[-10:])

    member_wa = _digits_expr(People.wa_id)
    member_phone = _digits_expr(People.phone_number)
    candidate_filters = []
    if contact_wa_ids:
        candidate_filters.append(member_wa.in_(contact_wa_ids))
    if contact_keys:
        candidate_filters.append(member_phone.in_(contact_keys))
    if contact_last10:
        candidate_filters.append(
            and_(
                func.length(member_phone) >= 10,
                func.right(member_phone, 10).in_(contact_last10),
            )
        )

    if not candidate_filters:
        return {}

    active_rank = _active_membership_rank_sq()
    latest_end = _latest_membership_end_sq()
    stmt = (
        select(
            People.id,
            People.full_name,
            People.wa_id,
            People.phone_number,
            active_rank.label("active_membership_rank"),
            latest_end.label("latest_membership_end"),
        )
        .join(PersonRole, PersonRole.person_id == People.id)
        .join(Role, Role.id == PersonRole.role_id)
        .where(Role.code == "member")
        .where(People.deleted_at.is_(None))
        .where(_has_membership_subscription())
        .where(or_(*candidate_filters))
    )

    rows = (await db.execute(stmt)).all()
    candidates = [
        _MemberCandidate(
            id=row.id,
            full_name=(row.full_name or "").strip(),
            wa_id=row.wa_id,
            phone_number=row.phone_number,
            active_membership_rank=int(row.active_membership_rank or 0),
            latest_membership_end=row.latest_membership_end,
        )
        for row in rows
        if (row.full_name or "").strip()
    ]
    if not candidates:
        return {}

    matches: Dict[int, _MemberCandidate] = {}
    for contact in contacts:
        best_member = None
        best_key = None
        for member in candidates:
            match_rank = _member_contact_match_rank(contact, member)
            if match_rank is None:
                continue
            sort_key = _member_sort_key(match_rank, member)
            if best_key is None or sort_key < best_key:
                best_key = sort_key
                best_member = member
        if best_member is not None:
            matches[contact.id] = best_member
    return matches


def _contact_data(
    contact: Contact, member: Optional[_MemberCandidate] = None
) -> ChatContactData:
    return ChatContactData(
        id=contact.id,
        wa_id=contact.wa_id,
        phone_number=contact.phone_number,
        name=contact.name,
        profile_name=contact.profile_name,
        member_id=member.id if member else None,
        member_name=member.full_name if member else None,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _last_activity_subquery():
    """Grouped max(timestamp) per conversation.

    Replaces a per-row correlated subquery with a single ``GROUP BY`` scan so the
    list can be ordered and paginated (``OFFSET``/``LIMIT``) efficiently.
    """
    return (
        select(
            Message.conversation_id.label("cid"),
            func.max(Message.timestamp).label("last_activity"),
        )
        .where(Message.message_type != "reaction")  # reactions don't reorder the list
        .group_by(Message.conversation_id)
        .subquery()
    )


async def get_conversations(
    db: AsyncSession,
    limit: Optional[int] = 50,
    offset: int = 0,
    search: Optional[str] = None,
) -> List[ConversationData]:
    """Return conversations ordered by most recent message activity."""
    activity_sq = _last_activity_subquery()

    stmt = (
        select(Conversation, activity_sq.c.last_activity)
        .outerjoin(activity_sq, activity_sq.c.cid == Conversation.id)
        .options(selectinload(Conversation.contact))
    )

    if search:
        like = f"%{search.strip()}%"
        matching_member_name = (
            select(People.id)
            .join(PersonRole, PersonRole.person_id == People.id)
            .join(Role, Role.id == PersonRole.role_id)
            .where(Role.code == "member")
            .where(People.deleted_at.is_(None))
            .where(_has_membership_subscription())
            .where(People.full_name.ilike(like))
            .where(_member_contact_sql_match_condition())
            .exists()
        )
        stmt = stmt.join(Contact, Conversation.contact_id == Contact.id).where(
            or_(
                Contact.name.ilike(like),
                Contact.profile_name.ilike(like),
                Contact.phone_number.ilike(like),
                Contact.wa_id.ilike(like),
                matching_member_name,
            )
        )

    stmt = stmt.order_by(activity_sq.c.last_activity.desc().nullslast()).offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = (await db.execute(stmt)).all()
    if not rows:
        return []

    conv_ids = [conv.id for conv, _ in rows]
    last_messages = await _latest_message_per_conversation(db, conv_ids)
    contacts = [conv.contact for conv, _ in rows]
    member_by_contact_id = await _resolve_member_contacts(db, contacts)

    result: List[ConversationData] = []
    for conv, last_activity in rows:
        last = last_messages.get(conv.id)
        member = member_by_contact_id.get(conv.contact_id)
        result.append(
            ConversationData(
                id=conv.id,
                status=conv.status,
                contact=_contact_data(conv.contact, member),
                last_message=last,
                last_activity=last_activity,
                unread_count=0,  # reserved for a later phase
            )
        )
    return result


async def get_conversation_data(
    db: AsyncSession, conversation_id: int
) -> Optional[ConversationData]:
    """Return a single conversation enriched like ``get_conversations``.

    Used by the desktop client to insert a newly-active conversation incrementally
    (e.g. a realtime message arrives for a conversation outside the loaded pages)
    without reloading the whole list.
    """
    activity_sq = _last_activity_subquery()
    stmt = (
        select(Conversation, activity_sq.c.last_activity)
        .outerjoin(activity_sq, activity_sq.c.cid == Conversation.id)
        .options(selectinload(Conversation.contact))
        .where(Conversation.id == conversation_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None

    conv, last_activity = row
    last_messages = await _latest_message_per_conversation(db, [conv.id])
    member_by_contact_id = await _resolve_member_contacts(db, [conv.contact])
    member = member_by_contact_id.get(conv.contact_id)
    return ConversationData(
        id=conv.id,
        status=conv.status,
        contact=_contact_data(conv.contact, member),
        last_message=last_messages.get(conv.id),
        last_activity=last_activity,
        unread_count=0,  # reserved for a later phase
    )


async def _latest_message_per_conversation(
    db: AsyncSession, conv_ids: List[int]
) -> Dict[int, ChatMessageData]:
    """Fetch the most recent message for each given conversation id.

    Uses ``DISTINCT ON (conversation_id)`` ordered by ``timestamp DESC, id DESC`` so
    exactly one row is returned per conversation even when two messages share the same
    timestamp (deterministic tie-break by id).
    """
    if not conv_ids:
        return {}

    stmt = (
        select(Message)
        .options(selectinload(Message.media))
        .where(Message.conversation_id.in_(conv_ids))
        .where(Message.message_type != "reaction")  # reactions never become the preview
        .distinct(Message.conversation_id)
        .order_by(
            Message.conversation_id,
            Message.timestamp.desc(),
            Message.id.desc(),
        )
    )
    messages = (await db.execute(stmt)).scalars().all()

    latest: Dict[int, ChatMessageData] = {}
    for m in messages:
        latest[m.conversation_id] = ChatMessageData.from_model(m)
    return latest


async def get_conversation_messages(
    db: AsyncSession,
    conversation_id: int,
    limit: int = 50,
    offset: int = 0,
) -> List[ChatMessageData]:
    """Return messages of a conversation in chronological (ascending) order."""
    stmt = (
        select(Message)
        .options(selectinload(Message.media))
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    messages = list((await db.execute(stmt)).scalars().all())
    messages.reverse()  # display oldest -> newest
    return [ChatMessageData.from_model(m) for m in messages]


async def get_message_by_id(db: AsyncSession, message_id: int) -> Optional[ChatMessageData]:
    """Load a single message (used by the realtime subscription)."""
    stmt = (
        select(Message)
        .options(selectinload(Message.media))
        .where(Message.id == message_id)
    )
    m = (await db.execute(stmt)).scalars().first()
    return ChatMessageData.from_model(m) if m else None


# ---------------------------------------------------------------------------
# Writes / upserts (used by the webhook ingest pipeline and send mutation)
# ---------------------------------------------------------------------------
async def get_contact_by_wa_id(db: AsyncSession, wa_id: str) -> Optional[Contact]:
    stmt = select(Contact).where(Contact.wa_id == wa_id)
    return (await db.execute(stmt)).scalars().first()


def _contact_number_match_condition(keys: set[str], last10: set[str]):
    """SQL condition matching a Contact by normalized phone keys (52/521 aware).

    Mirrors ``_member_contact_sql_match_condition`` but targets Contact, comparing the
    digit-only wa_id/phone_number against the candidate key set and the last-10 digits.
    """
    contact_wa = _digits_expr(Contact.wa_id)
    contact_phone = _digits_expr(Contact.phone_number)
    conds = []
    if keys:
        conds.append(contact_wa.in_(keys))
        conds.append(contact_phone.in_(keys))
    if last10:
        conds.append(
            and_(func.length(contact_wa) >= 10, func.right(contact_wa, 10).in_(last10))
        )
        conds.append(
            and_(func.length(contact_phone) >= 10, func.right(contact_phone, 10).in_(last10))
        )
    if not conds:
        return None
    return or_(*conds)


async def find_contact_by_number(db: AsyncSession, raw_number: Optional[str]) -> Optional[Contact]:
    """Find an existing contact whose number matches ``raw_number`` (52/521 aware).

    Returns the lowest-id match (deterministic) or None. Used so a send that types the
    number in a different format (e.g. 52... vs 521...) reuses the existing contact
    instead of creating a duplicate.
    """
    digits = _digits_only(raw_number)
    if not digits:
        return None
    keys = _phone_match_keys(raw_number)
    last10 = {digits[-10:]} if len(digits) >= 10 else set()
    cond = _contact_number_match_condition(keys, last10)
    if cond is None:
        return None
    stmt = select(Contact).where(cond).order_by(Contact.id.asc())
    return (await db.execute(stmt)).scalars().first()


async def get_conversation(db: AsyncSession, conversation_id: int) -> Optional[Conversation]:
    """Load a conversation with its contact eager-loaded (used by send mutation)."""
    stmt = (
        select(Conversation)
        .options(selectinload(Conversation.contact))
        .where(Conversation.id == conversation_id)
    )
    return (await db.execute(stmt)).scalars().first()


async def upsert_contact(
    db: AsyncSession,
    wa_id: str,
    phone_number: Optional[str] = None,
    profile_name: Optional[str] = None,
    *,
    authoritative: bool = True,
) -> Contact:
    """Create or update a contact, matching by normalized number (52/521 aware).

    Lookup order: exact ``wa_id`` (fast path), then a normalized phone match against
    ``wa_id``/``phone_number``. This keeps a single contact per human number across the
    inbound webhook and outbound sends, even when the number is stored/typed in
    different Mexican formats.

    ``authoritative`` should be True for the inbound webhook (Meta's ``from`` is the
    canonical wa_id) and False for sends (the typed number must not overwrite the good
    wa_id of an existing contact). Caller commits.
    """
    contact = await get_contact_by_wa_id(db, wa_id)
    if contact is None:
        contact = await find_contact_by_number(db, wa_id)
    if contact is None and phone_number:
        contact = await find_contact_by_number(db, phone_number)

    now = datetime.utcnow()
    if contact is None:
        contact = Contact(
            wa_id=wa_id,
            phone_number=phone_number or wa_id,
            profile_name=profile_name,
            created_at=now,
            updated_at=now,
            is_saved=0,
        )
        db.add(contact)
        await db.flush()
        return contact

    changed = False
    if profile_name and contact.profile_name != profile_name:
        contact.profile_name = profile_name
        changed = True
    # Only the authoritative inbound source may canonicalize the stored identity;
    # sends never downgrade an existing contact's wa_id/phone_number.
    if authoritative:
        if wa_id and contact.wa_id != wa_id:
            contact.wa_id = wa_id
            changed = True
        if phone_number and contact.phone_number != phone_number:
            contact.phone_number = phone_number
            changed = True
    if changed:
        contact.updated_at = now
        await db.flush()
    return contact


async def get_or_open_conversation(
    db: AsyncSession,
    contact_id: int,
    window_anchor: Optional[datetime] = None,
) -> Conversation:
    """Reuse the active conversation for a contact, or open a new one. Caller commits."""
    anchor = window_anchor or datetime.utcnow()
    stmt = (
        select(Conversation)
        .where(Conversation.contact_id == contact_id, Conversation.status == "active")
        .order_by(Conversation.id.desc())
    )
    conv = (await db.execute(stmt)).scalars().first()
    now = datetime.utcnow()
    if conv is None:
        conv = Conversation(
            contact_id=contact_id,
            status="active",
            expiration_timestamp=anchor + CONVERSATION_WINDOW,
            created_at=now,
            updated_at=now,
        )
        db.add(conv)
        await db.flush()
        return conv

    # Refresh the customer-service window on inbound activity.
    conv.expiration_timestamp = anchor + CONVERSATION_WINDOW
    conv.updated_at = now
    await db.flush()
    return conv


async def _message_exists_by_wa_id(db: AsyncSession, wa_message_id: str) -> Optional[Message]:
    stmt = select(Message).where(Message.wa_message_id == wa_message_id)
    return (await db.execute(stmt)).scalars().first()


async def insert_inbound_message(
    db: AsyncSession,
    conversation_id: int,
    contact_id: int,
    message_type: str,
    timestamp: datetime,
    wa_message_id: Optional[str] = None,
    text_content: Optional[str] = None,
    context_message_id: Optional[str] = None,
) -> Optional[Message]:
    """Insert an inbound message, idempotent by wa_message_id. Caller commits.

    Returns the new Message, or None if it already existed (deduped).
    """
    if wa_message_id:
        existing = await _message_exists_by_wa_id(db, wa_message_id)
        if existing is not None:
            return None

    msg = Message(
        wa_message_id=wa_message_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        direction="inbound",
        message_type=message_type,
        text_content=text_content,
        context_message_id=context_message_id,
        timestamp=timestamp,
        created_at=datetime.utcnow(),
        is_processed=0,
        is_temp=0,
    )
    db.add(msg)
    await db.flush()
    return msg


async def insert_outbound_message(
    db: AsyncSession,
    conversation_id: int,
    contact_id: int,
    text: Optional[str],
    wa_message_id: Optional[str] = None,
    message_type: str = "text",
    template_id: Optional[int] = None,
    context_message_id: Optional[str] = None,
) -> Message:
    """Insert an outbound message after a successful Cloud API send. Caller commits."""
    now = datetime.utcnow()
    msg = Message(
        wa_message_id=wa_message_id,
        conversation_id=conversation_id,
        contact_id=contact_id,
        direction="outbound",
        message_type=message_type,
        text_content=text,
        template_id=template_id,
        context_message_id=context_message_id,
        timestamp=now,
        created_at=now,
        is_processed=1,
        is_temp=0,
    )
    db.add(msg)
    await db.flush()
    return msg


async def insert_message_status(
    db: AsyncSession,
    wa_message_id: str,
    status: str,
    timestamp: datetime,
) -> Optional[MessageStatus]:
    """Record a delivery status for a message identified by its wa_message_id.

    Idempotent on (message_id, status). Caller commits. Returns None if the
    referenced message is unknown or the status was already recorded.
    """
    msg = await _message_exists_by_wa_id(db, wa_message_id)
    if msg is None:
        return None

    dup_stmt = select(MessageStatus).where(
        MessageStatus.message_id == msg.id, MessageStatus.status == status
    )
    if (await db.execute(dup_stmt)).scalars().first() is not None:
        return None

    row = MessageStatus(
        message_id=msg.id,
        status=status,
        timestamp=timestamp,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    await db.flush()
    return row


async def insert_media(
    db: AsyncSession,
    message_id: int,
    media_type: str,
    mime_type: Optional[str] = None,
    caption: Optional[str] = None,
    cloud_media_id: Optional[str] = None,
) -> Media:
    """Insert a media row (pre-download). Caller commits."""
    media = Media(
        message_id=message_id,
        media_type=media_type,
        mime_type=mime_type,
        caption=caption,
        cloud_media_id=cloud_media_id,
        created_at=datetime.utcnow(),
        downloaded=0,
        download_failed=0,
    )
    db.add(media)
    await db.flush()
    return media


async def notify_media_event(db: AsyncSession, media: Media, status: str) -> None:
    """Publish a ``media_updated`` event on the realtime channel.

    Uses ``pg_notify`` from the application (no extra DB trigger) so the event
    reuses the existing ``whatsapp_events`` listener. NOTIFY is transactional:
    it is delivered when the caller commits, i.e. once the media row update is
    visible to other connections.
    """
    conversation_id = (
        await db.execute(
            select(Message.conversation_id).where(Message.id == media.message_id)
        )
    ).scalar_one_or_none()
    payload = json.dumps(
        {
            "type": "media_updated",
            "id": media.message_id,
            "media_id": media.id,
            "conversation_id": conversation_id,
            "status": status,
        }
    )
    await db.execute(
        sql_text("SELECT pg_notify('whatsapp_events', :payload)"),
        {"payload": payload},
    )


async def mark_media_downloaded(
    db: AsyncSession,
    media_id: int,
    *,
    sha256: Optional[str],
    filename: Optional[str],
    file_size: Optional[int],
    media_url: Optional[str],
    mime_type: Optional[str] = None,
) -> None:
    """Update a media row after a successful download. Caller commits."""
    media = (await db.execute(select(Media).where(Media.id == media_id))).scalars().first()
    if media is None:
        return
    media.sha256 = sha256
    media.filename = filename
    media.file_size = file_size
    media.media_url = media_url
    if mime_type:
        media.mime_type = mime_type
    media.downloaded = 1
    media.download_failed = 0
    media.download_time = datetime.utcnow()
    await db.flush()
    await notify_media_event(db, media, "downloaded")


async def mark_media_failed(db: AsyncSession, media_id: int) -> None:
    media = (await db.execute(select(Media).where(Media.id == media_id))).scalars().first()
    if media is None:
        return
    media.download_failed = 1
    media.download_time = datetime.utcnow()
    await db.flush()
    await notify_media_event(db, media, "failed")


async def get_media_for_retry(db: AsyncSession, message_id: int) -> Optional[Media]:
    """Return the media row of a message that can be re-downloaded from Meta."""
    stmt = select(Media).where(Media.message_id == message_id).order_by(Media.id.asc())
    return (await db.execute(stmt)).scalars().first()


async def insert_outbound_media(
    db: AsyncSession,
    message_id: int,
    media_type: str,
    *,
    mime_type: Optional[str],
    filename: Optional[str],
    file_size: Optional[int],
    sha256: Optional[str],
    media_url: Optional[str],
    caption: Optional[str] = None,
    cloud_media_id: Optional[str] = None,
) -> Media:
    """Insert the media row of a sent message (file already stored locally). Caller commits."""
    now = datetime.utcnow()
    media = Media(
        message_id=message_id,
        media_type=media_type,
        mime_type=mime_type,
        filename=filename,
        file_size=file_size,
        sha256=sha256,
        media_url=media_url,
        caption=caption,
        cloud_media_id=cloud_media_id,
        created_at=now,
        downloaded=1,
        download_time=now,
        download_failed=0,
    )
    db.add(media)
    await db.flush()
    return media
