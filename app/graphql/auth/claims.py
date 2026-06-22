from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def build_session_claims(person_id: Any, username: str | None, session_id: Any) -> dict[str, str | None]:
    return {
        "person_id": str(person_id) if person_id is not None else None,
        "username": username,
        "session_id": str(session_id) if session_id is not None else None,
    }


def build_access_claims(
    person_id: Any,
    username: str | None,
    session_id: Any,
    person: Any,
    capabilities: Iterable[str],
) -> dict[str, Any]:
    claims = build_session_claims(person_id, username, session_id)
    role_codes = set()
    for person_role in getattr(person, "roles", None) or []:
        role = getattr(person_role, "role", None)
        if role:
            role_codes.add(role.code)

    claims.update(
        {
            "roles": sorted(role_codes),
            "capabilities": sorted(capabilities),
        }
    )
    return claims
