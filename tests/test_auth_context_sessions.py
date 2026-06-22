from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.graphql import context as auth_context


class _FakeRequest:
    def __init__(self, *, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}


class _FakeResponse:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def set_cookie(self, **kwargs):
        self.cookies.append(kwargs)


async def _fake_account(_db, username):
    return SimpleNamespace(id=99, username=username)


@pytest.mark.asyncio
async def test_build_context_accepts_active_access_token(monkeypatch):
    user = SimpleNamespace(id=342)

    async def fake_verify_session(_db, session_id):
        assert session_id == "session-1"
        return SimpleNamespace(deleted_at=None, revoked_at=None)

    async def fake_get_person(_db, person_id):
        assert person_id == "342"
        return user

    monkeypatch.setattr(
        auth_context,
        "verify_token",
        lambda token: {
            "person_id": "342",
            "username": "aleramos",
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(auth_context, "verify_session", fake_verify_session)
    monkeypatch.setattr(auth_context, "get_person_by_id", fake_get_person)
    monkeypatch.setattr(auth_context, "get_account_by_username", _fake_account)

    context = await auth_context.build_context(
        _FakeRequest(cookies={"access_token": "access-token"}),
        response=_FakeResponse(),
        db=object(),
    )

    assert context.user is user
    assert context.account_id == 99
    assert context.session_id == "session-1"


@pytest.mark.asyncio
async def test_build_context_rejects_revoked_access_token_session(monkeypatch):
    calls = {"person": 0}

    async def fake_verify_session(_db, _session_id):
        return SimpleNamespace(
            deleted_at=None,
            revoked_at=datetime.now(timezone.utc),
        )

    async def fake_get_person(_db, _person_id):
        calls["person"] += 1
        return SimpleNamespace(id=342)

    monkeypatch.setattr(
        auth_context,
        "verify_token",
        lambda token: {
            "person_id": "342",
            "username": "aleramos",
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(auth_context, "verify_session", fake_verify_session)
    monkeypatch.setattr(auth_context, "get_person_by_id", fake_get_person)
    monkeypatch.setattr(auth_context, "get_account_by_username", _fake_account)

    context = await auth_context.build_context(
        _FakeRequest(cookies={"access_token": "access-token"}),
        response=_FakeResponse(),
        db=object(),
    )

    assert context.user is None
    assert context.account_id is None
    assert calls["person"] == 0


@pytest.mark.asyncio
async def test_build_context_rejects_deleted_access_token_session(monkeypatch):
    async def fake_verify_session(_db, _session_id):
        return SimpleNamespace(
            deleted_at=datetime.now(timezone.utc),
            revoked_at=None,
        )

    monkeypatch.setattr(
        auth_context,
        "verify_token",
        lambda token: {
            "person_id": "342",
            "username": "aleramos",
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(auth_context, "verify_session", fake_verify_session)

    context = await auth_context.build_context(
        _FakeRequest(cookies={"access_token": "access-token"}),
        response=_FakeResponse(),
        db=object(),
    )

    assert context.user is None
    assert context.account_id is None


@pytest.mark.asyncio
async def test_build_context_refreshes_active_refresh_token(monkeypatch):
    user = SimpleNamespace(id=342)
    response = _FakeResponse()
    touched = []

    async def fake_verify_session(_db, session_id):
        assert session_id == "session-1"
        return SimpleNamespace(deleted_at=None, revoked_at=None)

    async def fake_get_person(_db, person_id):
        assert person_id == "342"
        return user

    async def fake_touch(_db, session_id):
        touched.append(session_id)

    async def fake_capabilities(_db, person):
        assert person is user
        return {"manage_owner_agent"}

    access_payloads = []

    def fake_create_access_token(payload):
        access_payloads.append(payload)
        return "new-access"

    monkeypatch.setattr(auth_context, "verify_token", lambda token: None)
    monkeypatch.setattr(
        auth_context,
        "verify_refresh_token",
        lambda token: {
            "person_id": "342",
            "username": "aleramos",
            "session_id": "session-1",
        },
    )
    monkeypatch.setattr(auth_context, "verify_session", fake_verify_session)
    monkeypatch.setattr(auth_context, "get_person_by_id", fake_get_person)
    monkeypatch.setattr(auth_context, "get_account_by_username", _fake_account)
    monkeypatch.setattr(auth_context, "get_capabilities_for_person", fake_capabilities)
    monkeypatch.setattr(auth_context, "create_access_token", fake_create_access_token)
    monkeypatch.setattr(auth_context, "update_last_active_at", fake_touch)

    context = await auth_context.build_context(
        _FakeRequest(cookies={"refresh_token": "refresh-token"}),
        response=response,
        db=object(),
    )

    assert context.user is user
    assert context.account_id == 99
    assert context.session_id == "session-1"
    assert touched == ["session-1"]
    assert response.headers["x-access-token"] == "new-access"
    assert response.cookies[0]["key"] == "access_token"
    assert response.cookies[0]["value"] == "new-access"
    assert access_payloads == [
        {
            "person_id": "342",
            "username": "aleramos",
            "session_id": "session-1",
            "roles": [],
            "capabilities": ["manage_owner_agent"],
        }
    ]
