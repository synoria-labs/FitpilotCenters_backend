"""Backend tests for self-service account update (myAccount / updateMyAccount).

Covers:
  - update_own_account CRUD: field updates, clearing email/phone, password hash
  - revoke_other_sessions CRUD: revokes the person's other sessions, keeps current
  - myAccount query and updateMyAccount mutation via schema execution, including
    password change (current-password verification + revoking other sessions)

Each test runs inside a SAVEPOINT-wrapped session (see conftest.py) so nothing
persists to defaultdb. Roles use unique non-customer codes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.crud.usersCrud import (
    create_user_with_account,
    update_own_account,
    get_user_by_account_id,
)
from app.crud.sessionCrud import revoke_other_sessions
from app.graphql.context import Context
from app.graphql.schema import schema
from app.security.hashing import hash_password, verify_password
from app.models import Role, Session


async def _make_role(db) -> Role:
    role = Role(code=f"team_{uuid.uuid4().hex[:8]}", description="Equipo")
    db.add(role)
    await db.flush()
    return role


def _uname() -> str:
    return f"selfsvc_{uuid.uuid4().hex[:10]}"


def _make_session(db, *, user_id: int, session: str, revoked: bool = False) -> Session:
    s = Session(
        refresh_token="rt",
        session=session,
        user_id=user_id,
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    db.add(s)
    return s


def _ctx(db, *, user, account_id, session_id=None) -> Context:
    return Context(
        db=db,
        request=SimpleNamespace(),
        response=None,
        user=user,
        account_id=account_id,
        session_id=session_id,
    )


_UPDATE_MUTATION = """
mutation Upd($i: UpdateMyAccountInput!) {
  updateMyAccount(input: $i) {
    success
    message
    user { accountId fullName email phoneNumber }
  }
}
"""


# --------------------------------------------------------------------------- #
# CRUD: update_own_account                                                     #
# --------------------------------------------------------------------------- #

async def test_update_own_account_updates_and_clears_fields(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="Keep Me", username=_uname(), password_hash="h",
        email="x@y.com", phone_number="555", role_ids=[role.id],
    )

    # Clear email + phone (explicit None) while leaving full_name untouched (_UNSET).
    updated = await update_own_account(db, acct.id, email=None, phone_number=None)
    assert updated.person.email is None
    assert updated.person.phone_number is None
    assert updated.person.full_name == "Keep Me"

    # Update full_name only.
    updated = await update_own_account(db, acct.id, full_name="New Name")
    assert updated.person.full_name == "New Name"


async def test_update_own_account_sets_password_hash(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="P", username=_uname(), password_hash="old", role_ids=[role.id],
    )
    updated = await update_own_account(db, acct.id, new_password_hash="newhash")
    assert updated.password_hash == "newhash"


# --------------------------------------------------------------------------- #
# CRUD: revoke_other_sessions                                                  #
# --------------------------------------------------------------------------- #

async def test_revoke_other_sessions(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="S", username=_uname(), password_hash="h", role_ids=[role.id],
    )
    pid = acct.person_id
    other_pid = pid + 10_000_000  # a different person

    _make_session(db, user_id=pid, session="s-current")
    _make_session(db, user_id=pid, session="s-other1")
    _make_session(db, user_id=pid, session="s-other2")
    _make_session(db, user_id=pid, session="s-already", revoked=True)
    _make_session(db, user_id=other_pid, session="s-other-user")
    await db.flush()

    count = await revoke_other_sessions(db, pid, "s-current")
    assert count == 2  # only the two active, non-current sessions

    rows = {
        r.session: r
        for r in (
            await db.execute(select(Session).where(Session.user_id == pid))
        ).scalars().all()
    }
    assert rows["s-current"].revoked_at is None
    assert rows["s-other1"].revoked_at is not None
    assert rows["s-other2"].revoked_at is not None
    assert rows["s-already"].revoked_at is not None  # stays revoked

    other = (
        await db.execute(select(Session).where(Session.session == "s-other-user"))
    ).scalar_one()
    assert other.revoked_at is None  # different person untouched


# --------------------------------------------------------------------------- #
# GraphQL: myAccount                                                           #
# --------------------------------------------------------------------------- #

async def test_my_account_returns_current(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="Me", username=_uname(), password_hash=hash_password("pw"),
        email="me@x.com", role_ids=[role.id],
    )
    ctx = _ctx(db, user=acct.person, account_id=acct.id)
    result = await schema.execute(
        "query { myAccount { accountId username fullName email } }", context_value=ctx
    )
    assert result.errors is None
    data = result.data["myAccount"]
    assert data["accountId"] == acct.id
    assert data["username"] == acct.username
    assert data["fullName"] == "Me"


async def test_my_account_none_without_account_id(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="Me", username=_uname(), password_hash=hash_password("pw"),
        role_ids=[role.id],
    )
    ctx = _ctx(db, user=acct.person, account_id=None)
    result = await schema.execute("query { myAccount { accountId } }", context_value=ctx)
    assert result.errors is None
    assert result.data["myAccount"] is None


# --------------------------------------------------------------------------- #
# GraphQL: updateMyAccount                                                     #
# --------------------------------------------------------------------------- #

async def test_update_my_account_updates_fields(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="Old", username=_uname(), password_hash=hash_password("pw"),
        role_ids=[role.id],
    )
    ctx = _ctx(db, user=acct.person, account_id=acct.id, session_id="s1")
    result = await schema.execute(
        _UPDATE_MUTATION,
        variable_values={"i": {"fullName": "New", "email": "new@x.com", "phoneNumber": "123"}},
        context_value=ctx,
    )
    assert result.errors is None
    payload = result.data["updateMyAccount"]
    assert payload["success"] is True
    assert payload["user"]["fullName"] == "New"
    assert payload["user"]["email"] == "new@x.com"
    assert payload["user"]["phoneNumber"] == "123"


async def test_update_my_account_no_changes_fails(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="X", username=_uname(), password_hash=hash_password("pw"),
        role_ids=[role.id],
    )
    ctx = _ctx(db, user=acct.person, account_id=acct.id, session_id="s1")
    result = await schema.execute(
        _UPDATE_MUTATION, variable_values={"i": {}}, context_value=ctx
    )
    assert result.errors is None
    assert result.data["updateMyAccount"]["success"] is False


async def test_update_my_account_wrong_current_password_fails(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="X", username=_uname(),
        password_hash=hash_password("correct-horse"), role_ids=[role.id],
    )
    ctx = _ctx(db, user=acct.person, account_id=acct.id, session_id="s1")
    result = await schema.execute(
        _UPDATE_MUTATION,
        variable_values={"i": {"currentPassword": "wrong", "newPassword": "newpass1234"}},
        context_value=ctx,
    )
    assert result.errors is None
    assert result.data["updateMyAccount"]["success"] is False


async def test_update_my_account_password_change_revokes_other_sessions(db):
    role = await _make_role(db)
    acct = await create_user_with_account(
        db=db, full_name="X", username=_uname(),
        password_hash=hash_password("orig-pass-1"), role_ids=[role.id],
    )
    pid = acct.person_id
    _make_session(db, user_id=pid, session="s-current")
    _make_session(db, user_id=pid, session="s-other")
    await db.flush()

    ctx = _ctx(db, user=acct.person, account_id=acct.id, session_id="s-current")
    result = await schema.execute(
        _UPDATE_MUTATION,
        variable_values={"i": {"currentPassword": "orig-pass-1", "newPassword": "brand-new-pass"}},
        context_value=ctx,
    )
    assert result.errors is None
    assert result.data["updateMyAccount"]["success"] is True

    fresh = await get_user_by_account_id(db, acct.id)
    assert verify_password("brand-new-pass", fresh.password_hash)

    rows = {
        r.session: r
        for r in (
            await db.execute(select(Session).where(Session.user_id == pid))
        ).scalars().all()
    }
    assert rows["s-current"].revoked_at is None
    assert rows["s-other"].revoked_at is not None
