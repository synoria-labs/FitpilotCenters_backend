"""Tests for the step-up channel policy in the gym backend.

The MVP allows email only (no SMS budget). Verify the channel gate rejects
sms and accepts email based on STEP_UP_ALLOWED_CHANNELS.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.core.verification_config as vcfg
from app.graphql.verification import mutations


def _info(*, account_id: int | None = 1):
    return SimpleNamespace(
        context=SimpleNamespace(
            account_id=account_id,
            db=None,
            session_id=None,
        )
    )


@pytest.mark.asyncio
async def test_email_channel_accepted_by_default(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    monkeypatch.setattr(vcfg.VerificationConfig, "ALLOWED_CHANNELS", ("email",))

    captured = {}

    async def fake_request(channel, destination, purpose, *, client_session_id=None):
        captured["channel"] = channel
        captured["destination"] = destination
        return {"verificationId": "vid-1", "maskedDestination": "ad***@x.com"}

    monkeypatch.setattr("app.graphql.verification.mutations.vc.request_verification", fake_request)

    async def fake_user(db, account_id):
        return SimpleNamespace(
            person=SimpleNamespace(email="admin@example.com", phone_number="+5215555550000")
        )

    monkeypatch.setattr(
        "app.graphql.verification.mutations.get_user_by_account_id", fake_user
    )

    res = await mutations.StepUpMutation().request_step_up_verification(
        _info(), "email"
    )
    assert res.success is True
    assert captured["channel"] == "email"
    assert captured["destination"] == "admin@example.com"


@pytest.mark.asyncio
async def test_sms_channel_rejected_by_default(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    monkeypatch.setattr(vcfg.VerificationConfig, "ALLOWED_CHANNELS", ("email",))

    res = await mutations.StepUpMutation().request_step_up_verification(
        _info(), "sms"
    )
    assert res.success is False
    assert "Canal no disponible" in (res.message or "")
    assert "email" in (res.message or "")


@pytest.mark.asyncio
async def test_unknown_channel_rejected(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    monkeypatch.setattr(vcfg.VerificationConfig, "ALLOWED_CHANNELS", ("email",))

    res = await mutations.StepUpMutation().request_step_up_verification(
        _info(), "carrier-pigeon"
    )
    assert res.success is False
    assert "Canal inválido" in (res.message or "")


@pytest.mark.asyncio
async def test_email_channel_reports_missing_contact(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    monkeypatch.setattr(vcfg.VerificationConfig, "ALLOWED_CHANNELS", ("email",))

    async def fake_user(db, account_id):
        return SimpleNamespace(
            person=SimpleNamespace(email=None, phone_number="+5215555550000")
        )

    monkeypatch.setattr(
        "app.graphql.verification.mutations.get_user_by_account_id", fake_user
    )

    res = await mutations.StepUpMutation().request_step_up_verification(
        _info(), "email"
    )
    assert res.success is False
    assert "correo" in (res.message or "")
