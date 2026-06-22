from datetime import datetime, timezone
from typing import List, Optional
from sqlalchemy import select, update, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.conversions import coerce_int
from app.models import People, Account, PersonRole, Role

async def get_person_by_id(db: AsyncSession, person_id: int) -> Optional[People]:
    """Get person by ID with roles loaded"""
    person_id = coerce_int(person_id)
    if person_id is None:
        return None

    result = await db.execute(
        select(People)
        .options(selectinload(People.roles).selectinload(PersonRole.role))
        .where(People.id == person_id)
        .where(People.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()

async def get_account_by_person_id(db: AsyncSession, person_id: int) -> Optional[Account]:
    """Get account for a specific person"""
    person_id = coerce_int(person_id)
    if person_id is None:
        return None

    result = await db.execute(
        select(Account)
        .where(Account.person_id == person_id)
        .where(Account.is_active == True)
    )
    return result.scalar_one_or_none()

async def list_people(db: AsyncSession, role_code: str = None):
    """List all people, optionally filtered by role"""
    query = select(People).options(
        selectinload(People.roles).selectinload(PersonRole.role)
    ).where(People.deleted_at.is_(None))

    if role_code:
        query = query.join(PersonRole).join(Role).where(Role.code == role_code)

    result = await db.execute(query)
    return result.scalars().all()

async def list_members(db: AsyncSession):
    """List all people with member role"""
    return await list_people(db, role_code='member')

async def update_account_password(db: AsyncSession, username: str, password_hash: str) -> Optional[Account]:
    """Update account password"""
    stmt = (
        update(Account)
        .where(Account.username == username)
        .where(Account.is_active == True)
        .values(password_hash=password_hash)
        .returning(Account.id, Account.username, Account.person_id)
    )

    result = await db.execute(stmt)
    row = result.first()
    await db.commit()
    return row

async def get_person_roles(db: AsyncSession, person_id: int):
    """Get all roles for a person"""
    person_id = coerce_int(person_id)
    if person_id is None:
        return []

    result = await db.execute(
        select(Role)
        .join(PersonRole)
        .where(PersonRole.person_id == person_id)
    )
    return result.scalars().all()

async def create_person(db: AsyncSession, full_name: str, email: str = None, phone_number: str = None) -> People:
    """Create a new person"""
    person = People(
        full_name=full_name,
        email=email,
        phone_number=phone_number
    )
    db.add(person)
    await db.commit()
    await db.refresh(person)
    return person


# ---------------------------------------------------------------------------
# User (login account) management — used by the Admin "Usuarios" CRUD.
# A "user" = People (identity) + Account (login) + roles (PersonRole).
# ---------------------------------------------------------------------------

# Sentinel so callers can distinguish "not provided" from "explicitly None".
_UNSET = object()


async def list_roles(db: AsyncSession) -> List[Role]:
    """All roles ordered by code (to populate role pickers)."""
    result = await db.execute(select(Role).order_by(Role.code))
    return result.scalars().all()


async def list_users(db: AsyncSession, include_inactive: bool = True) -> List[Account]:
    """List login accounts (staff users) with their person + roles loaded."""
    query = (
        select(Account)
        .options(
            selectinload(Account.person)
            .selectinload(People.roles)
            .selectinload(PersonRole.role)
        )
        .join(People, Account.person_id == People.id)
        .where(People.deleted_at.is_(None))
        .order_by(Account.username)
    )
    if not include_inactive:
        query = query.where(Account.is_active.is_(True))

    result = await db.execute(query)
    return result.scalars().all()


async def get_user_by_account_id(db: AsyncSession, account_id: int) -> Optional[Account]:
    """Get a single account (any active state) with its person + roles loaded."""
    account_id = coerce_int(account_id)
    if account_id is None:
        return None

    result = await db.execute(
        select(Account)
        .options(
            selectinload(Account.person)
            .selectinload(People.roles)
            .selectinload(PersonRole.role)
        )
        .where(Account.id == account_id)
    )
    return result.scalar_one_or_none()


async def username_exists(
    db: AsyncSession, username: str, exclude_account_id: Optional[int] = None
) -> bool:
    """Whether an account with this username already exists (optionally ignoring one id)."""
    query = select(Account.id).where(Account.username == username)
    exclude_id = coerce_int(exclude_account_id)
    if exclude_id is not None:
        query = query.where(Account.id != exclude_id)
    result = await db.execute(query)
    return result.first() is not None


async def _set_person_roles(db: AsyncSession, person_id: int, role_ids: List) -> None:
    """Make a person's role assignments match ``role_ids`` (validating they exist).

    Diffs against the current assignments so unchanged roles are left untouched —
    this avoids a delete+reinsert of the same (person_id, role_id) PK in one flush.
    """
    normalized: List[int] = []
    for rid in role_ids or []:
        rid_value = coerce_int(rid)
        if rid_value is not None:
            normalized.append(rid_value)
    target = set(normalized)

    if target:
        found = set(
            (await db.execute(select(Role.id).where(Role.id.in_(target)))).scalars().all()
        )
        missing = target - found
        if missing:
            raise ValueError(f"Roles inexistentes: {sorted(missing)}")

    current = (
        await db.execute(select(PersonRole.role_id).where(PersonRole.person_id == person_id))
    ).scalars().all()
    current_ids = set(current)

    to_remove = current_ids - target
    to_add = target - current_ids

    if to_remove:
        await db.execute(
            delete(PersonRole).where(
                PersonRole.person_id == person_id,
                PersonRole.role_id.in_(to_remove),
            )
        )
    for rid in to_add:
        db.add(PersonRole(person_id=person_id, role_id=rid))


async def create_user_with_account(
    db: AsyncSession,
    full_name: str,
    username: str,
    password_hash: str,
    email: Optional[str] = None,
    phone_number: Optional[str] = None,
    role_ids: Optional[List] = None,
    is_active: bool = True,
) -> Optional[Account]:
    """Create a People + Account (login) and assign roles. Returns the loaded Account."""
    person = People(full_name=full_name, email=email, phone_number=phone_number)
    db.add(person)
    await db.flush()  # obtain person.id without committing

    account = Account(
        person_id=person.id,
        username=username,
        password_hash=password_hash,
        is_active=is_active,
    )
    db.add(account)
    await db.flush()  # obtain account.id

    await _set_person_roles(db, person.id, role_ids or [])

    await db.commit()
    return await get_user_by_account_id(db, account.id)


async def update_user(
    db: AsyncSession,
    account_id: int,
    full_name=_UNSET,
    email=_UNSET,
    phone_number=_UNSET,
    username=_UNSET,
    is_active=_UNSET,
    role_ids=_UNSET,
) -> Optional[Account]:
    """Partial update of a user's person, account and (optionally) roles."""
    account_id_value = coerce_int(account_id)
    if account_id_value is None:
        return None

    account = await get_user_by_account_id(db, account_id_value)
    if account is None:
        return None

    person = account.person

    if full_name is not _UNSET and full_name is not None:
        person.full_name = full_name
    if email is not _UNSET:
        person.email = email
    if phone_number is not _UNSET:
        person.phone_number = phone_number
    if username is not _UNSET and username is not None:
        account.username = username
    if is_active is not _UNSET and is_active is not None:
        account.is_active = bool(is_active)

    person.updated_at = datetime.now(timezone.utc)
    account.updated_at = datetime.now(timezone.utc)

    if role_ids is not _UNSET and role_ids is not None:
        await _set_person_roles(db, person.id, role_ids)

    await db.commit()
    return await get_user_by_account_id(db, account_id_value)


async def set_account_active(
    db: AsyncSession, account_id: int, is_active: bool
) -> Optional[Account]:
    """Activate/deactivate a login account (soft-delete of access)."""
    account_id_value = coerce_int(account_id)
    if account_id_value is None:
        return None

    result = await db.execute(select(Account).where(Account.id == account_id_value))
    account = result.scalar_one_or_none()
    if account is None:
        return None

    account.is_active = bool(is_active)
    account.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await get_user_by_account_id(db, account_id_value)


async def reset_account_password(
    db: AsyncSession, account_id: int, password_hash: str
) -> Optional[Account]:
    """Set a new password hash for an account by id."""
    account_id_value = coerce_int(account_id)
    if account_id_value is None:
        return None

    result = await db.execute(select(Account).where(Account.id == account_id_value))
    account = result.scalar_one_or_none()
    if account is None:
        return None

    account.password_hash = password_hash
    account.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return account
