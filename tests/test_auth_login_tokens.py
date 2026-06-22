from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.graphql.auth.mutations import AuthMutation


class _FakeRequest:
    def __init__(self):
        self.headers = {"user-agent": "python-httpx/0.28.1"}
        self.client = SimpleNamespace(host="10.0.1.20")


class _FakeResponse:
    def __init__(self):
        self.headers = {}
        self.cookies = []

    def set_cookie(self, **kwargs):
        self.cookies.append(kwargs)


@pytest.mark.asyncio
async def test_login_keeps_refresh_token_claims_minimal(monkeypatch):
    from app.graphql.auth import mutations

    refresh_payloads = []
    access_payloads = []
    created_sessions = []
    role = SimpleNamespace(code="admin")
    person = SimpleNamespace(roles=[SimpleNamespace(role=role)])

    async def fake_get_account_by_username(db, username):
        assert username == "aleramos"
        return SimpleNamespace(person_id=342, username=username, password_hash="hash")

    async def fake_get_person_by_id(db, person_id):
        assert person_id == 342
        return person

    async def fake_get_capabilities_for_person(db, loaded_person):
        assert loaded_person is person
        return {"manage_owner_agent", "manage_membership_plans"}

    async def fake_create_session(db, sessionEntry):
        created_sessions.append(sessionEntry)
        sessionEntry.id = 1
        return sessionEntry

    def fake_create_refresh_token(payload):
        refresh_payloads.append(payload)
        return "refresh-token"

    def fake_create_access_token(payload):
        access_payloads.append(payload)
        return "access-token"

    monkeypatch.setattr(mutations, "get_account_by_username", fake_get_account_by_username)
    monkeypatch.setattr(mutations, "verify_password", lambda password, password_hash: True)
    monkeypatch.setattr(mutations, "get_person_by_id", fake_get_person_by_id)
    monkeypatch.setattr(mutations, "get_capabilities_for_person", fake_get_capabilities_for_person)
    monkeypatch.setattr(mutations, "create_refresh_token", fake_create_refresh_token)
    monkeypatch.setattr(mutations, "create_access_token", fake_create_access_token)
    monkeypatch.setattr(mutations, "verify_refresh_token", lambda token: {"exp": 1782745836})
    monkeypatch.setattr(mutations, "create_session", fake_create_session)
    monkeypatch.setattr(mutations, "get_cookie_secure_setting", lambda: False)
    monkeypatch.setattr(mutations, "get_cookie_samesite_setting", lambda: "lax")
    monkeypatch.setattr(mutations, "get_refresh_cookie_max_age_seconds", lambda: 60)
    monkeypatch.setattr(mutations, "get_access_cookie_max_age_seconds", lambda: 30)

    response = _FakeResponse()
    info = SimpleNamespace(
        context=SimpleNamespace(request=_FakeRequest(), response=response, db=object())
    )

    result = await AuthMutation().login(
        SimpleNamespace(identifier="aleramos", password="secret"),
        info,
    )

    assert result.access_token == "access-token"
    assert refresh_payloads == [
        {
            "person_id": "342",
            "username": "aleramos",
            "session_id": created_sessions[0].session,
        }
    ]
    assert "roles" not in refresh_payloads[0]
    assert "capabilities" not in refresh_payloads[0]
    assert access_payloads == [
        {
            "person_id": "342",
            "username": "aleramos",
            "session_id": created_sessions[0].session,
            "roles": ["admin"],
            "capabilities": ["manage_membership_plans", "manage_owner_agent"],
        }
    ]
    assert created_sessions[0].refresh_token == "refresh-token"
    assert response.cookies[0]["key"] == "refresh_token"
    assert response.cookies[1]["key"] == "access_token"
