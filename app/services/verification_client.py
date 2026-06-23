"""HTTP client for the shared FitPilot verification microservice.

Contract (internal, bearer-authenticated):
- POST /v1/verifications                  -> start a verification (send code)
- POST /v1/verifications/{id}/check       -> check code, return single-use proof
- POST /v1/proofs/consume                 -> validate + consume a proof

All calls go over the internal network (VERIFICATION_SERVICE_URL); no public DNS.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from app.core.verification_config import VerificationConfig
from app.core.logging_config import get_logger

logger = get_logger(__name__)

PURPOSE_STEP_UP = "step_up"


class VerificationServiceError(Exception):
    """Raised when the verification service call fails."""


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {VerificationConfig.SERVICE_TOKEN}",
        "Content-Type": "application/json",
    }


async def request_verification(
    channel: str,
    destination: str,
    purpose: str,
    *,
    client_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Start a verification; the service sends the code (Twilio Verify / Resend)."""
    payload: Dict[str, Any] = {
        "channel": channel,
        "destination": destination,
        "purpose": purpose,
        "audience": VerificationConfig.AUDIENCE,
    }
    if client_session_id:
        payload["clientSessionId"] = client_session_id

    try:
        async with httpx.AsyncClient(timeout=VerificationConfig.TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{VerificationConfig.SERVICE_URL}/v1/verifications",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:  # noqa: BLE001
        logger.error("verification request failed: %s", exc)
        raise VerificationServiceError(str(exc)) from exc


async def check_verification(verification_id: str, code: str) -> Dict[str, Any]:
    """Check a code; on success the service returns a single-use ``proof``."""
    try:
        async with httpx.AsyncClient(timeout=VerificationConfig.TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{VerificationConfig.SERVICE_URL}/v1/verifications/{verification_id}/check",
                json={"code": code},
                headers=_headers(),
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:  # noqa: BLE001
        logger.error("verification check failed: %s", exc)
        raise VerificationServiceError(str(exc)) from exc


async def consume_proof(proof: str, purpose: str, audience: str) -> Dict[str, Any]:
    """Validate and single-use-consume a proof. Fails closed on any error.

    Returns ``{"valid": bool, "destination": Optional[str]}``.
    """
    try:
        async with httpx.AsyncClient(timeout=VerificationConfig.TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{VerificationConfig.SERVICE_URL}/v1/proofs/consume",
                json={"proof": proof, "expectedPurpose": purpose, "expectedAudience": audience},
                headers=_headers(),
            )
            if resp.status_code >= 400:
                logger.warning("proof consume rejected: HTTP %s", resp.status_code)
                return {"valid": False, "destination": None}
            data = resp.json()
            return {"valid": bool(data.get("valid")), "destination": data.get("destination")}
    except httpx.HTTPError as exc:  # noqa: BLE001
        logger.error("proof consume failed: %s", exc)
        return {"valid": False, "destination": None}
