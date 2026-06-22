"""Backend tests for the user (login account) CRUD layer.

Covers:
  - create_user_with_account (person + account + roles) and username_exists
  - update_user role diffing (add keeping existing, then narrow) + scalar fields
  - update_user validation of unknown role ids
  - set_account_active toggling and reset_account_password

Each test runs inside a SAVEPOINT-wrapped session (see conftest.py) so nothing
persists to defaultdb. Roles use unique codes to avoid colliding with real rows.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.crud.usersCrud import (
    create_user_with_account,
    update_user,
    set_account_active,
    reset_account_password,
    username_exists,
)
from app.models import Account, Role


async def _make_role(db, code_prefix: str) -> Role:
    role = Role(code=f"{code_prefix}_{uuid.uuid4().hex[:8]}", description=f"Test {code_prefix}")
    db.add(role)
    await db.flush()
    return role


def _uname() -> str:
    return f"testuser_{uuid.uuid4().hex[:10]}"


async def test_create_user_with_account(db):
    role = await _make_role(db, "rolea")
    username = _uname()

    account = await create_user_with_account(
        db=db,
        full_name="Alice Tester",
        username=username,
        password_hash="hash-1",
        email="alice@example.com",
        phone_number="555",
        role_ids=[role.id],
    )

    assert account is not None
    assert account.username == username
    assert account.is_active is True
    assert account.person is not None
    assert account.person.full_name == "Alice Tester"
    assert {pr.role_id for pr in account.person.roles} == {role.id}

    assert await username_exists(db, username) is True
    assert await username_exists(db, username, exclude_account_id=account.id) is False


async def test_update_user_replaces_roles(db):
    role_a = await _make_role(db, "rolea")
    role_b = await _make_role(db, "roleb")

    account = await create_user_with_account(
        db=db, full_name="Bob", username=_uname(), password_hash="h",
        role_ids=[role_a.id],
    )

    # Add role_b while keeping role_a (diff path: must not collide on the kept PK).
    updated = await update_user(db=db, account_id=account.id, role_ids=[role_a.id, role_b.id])
    assert {pr.role_id for pr in updated.person.roles} == {role_a.id, role_b.id}

    # Narrow down to only role_b.
    updated = await update_user(db=db, account_id=account.id, role_ids=[role_b.id])
    assert {pr.role_id for pr in updated.person.roles} == {role_b.id}

    # Update scalar field without touching roles (username=None => leave unchanged).
    updated = await update_user(
        db=db, account_id=account.id, full_name="Bob Updated", username=None
    )
    assert updated.person.full_name == "Bob Updated"
    assert {pr.role_id for pr in updated.person.roles} == {role_b.id}


async def test_update_user_invalid_role_raises(db):
    role_a = await _make_role(db, "rolea")
    account = await create_user_with_account(
        db=db, full_name="Carol", username=_uname(), password_hash="h",
        role_ids=[role_a.id],
    )
    with pytest.raises(ValueError):
        await update_user(db=db, account_id=account.id, role_ids=[role_a.id, 999_999_999])


async def test_set_account_active_toggle(db):
    role_a = await _make_role(db, "rolea")
    account = await create_user_with_account(
        db=db, full_name="Dan", username=_uname(), password_hash="h",
        role_ids=[role_a.id],
    )

    deactivated = await set_account_active(db=db, account_id=account.id, is_active=False)
    assert deactivated.is_active is False

    reactivated = await set_account_active(db=db, account_id=account.id, is_active=True)
    assert reactivated.is_active is True


async def test_reset_account_password(db):
    role_a = await _make_role(db, "rolea")
    account = await create_user_with_account(
        db=db, full_name="Eve", username=_uname(), password_hash="old-hash",
        role_ids=[role_a.id],
    )

    result = await reset_account_password(db=db, account_id=account.id, password_hash="new-hash")
    assert result is not None

    fresh = (
        await db.execute(select(Account).where(Account.id == account.id))
    ).scalar_one()
    assert fresh.password_hash == "new-hash"
