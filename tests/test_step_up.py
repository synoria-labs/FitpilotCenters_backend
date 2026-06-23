"""Tests for the step-up (2-step) proof gate `require_step_up_proof`.

DB-free: the verification service client and the enable flag are monkeypatched,
so these run without Postgres or the verification microservice.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.graphql.auth import permissions
import app.core.verification_config as vcfg
import app.services.verification_client as vc


def _info(*, user: bool = True, account_id: int = 1, db=None):
    ctx = SimpleNamespace(
        user=(object() if user else None),
        account_id=account_id,
        db=db,
    )
    return SimpleNamespace(context=ctx)


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: False)
    assert await permissions.require_step_up_proof(_info(), None) is None
    assert await permissions.require_step_up_proof(_info(), "whatever") is None


@pytest.mark.asyncio
async def test_enabled_requires_proof(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    err = await permissions.require_step_up_proof(_info(), None)
    assert err and "2 pasos" in err


@pytest.mark.asyncio
async def test_enabled_rejects_invalid_proof(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)

    async def fake_consume(proof, purpose, audience):
        return {"valid": False, "destination": None}

    monkeypatch.setattr(vc, "consume_proof", fake_consume)
    err = await permissions.require_step_up_proof(_info(), "bad-proof")
    assert err is not None


@pytest.mark.asyncio
async def test_enabled_accepts_valid_proof(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)

    captured = {}

    async def fake_consume(proof, purpose, audience):
        captured["args"] = (proof, purpose, audience)
        return {"valid": True, "destination": None}

    monkeypatch.setattr(vc, "consume_proof", fake_consume)
    assert await permissions.require_step_up_proof(_info(), "good-proof") is None
    # consumed with the step_up purpose + gym audience
    assert captured["args"][0] == "good-proof"
    assert captured["args"][1] == vc.PURPOSE_STEP_UP


@pytest.mark.asyncio
async def test_enabled_requires_auth_context(monkeypatch):
    monkeypatch.setattr(vcfg, "step_up_enabled", lambda: True)
    err = await permissions.require_step_up_proof(_info(user=False, account_id=None), "p")
    assert err == "Acceso no autorizado"
