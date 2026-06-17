from dataclasses import dataclass
from strawberry.fastapi import BaseContext
from fastapi import Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection, Request
from starlette.websockets import WebSocket

from app.security.jwt import (
    create_access_token,
    verify_refresh_token,
    verify_token,
    get_cookie_secure_setting,
    get_cookie_samesite_setting,
    get_access_cookie_max_age_seconds,
)
from app.crud.sessionCrud import update_last_active_at, verify_session
from app.crud.usersCrud import get_person_by_id
from app.crud.authCrud import get_account_by_username
from app.db.postgresql import get_db
from app.core.logging_config import get_logger

logger = get_logger("graphql.context")


@dataclass
class Context(BaseContext):
    db: AsyncSession
    request: Request | WebSocket
    response: Response | None
    user: object = None
    account_id: int = None


async def _get_active_session(db: AsyncSession, session_id_value):
    if not session_id_value:
        logger.warning("Token without session_id provided; skipping context auth")
        return None

    session_id = str(session_id_value)
    verified_session = await verify_session(db, session_id)
    if verified_session is None:
        logger.warning("Attempted to use unknown session: %s", session_id[:8])
        return None

    logger.debug("Verified session exists: %s", session_id[:8])
    if verified_session.deleted_at is not None or verified_session.revoked_at is not None:
        status = "deleted" if verified_session.deleted_at else "revoked"
        logger.warning("Attempted to use %s session: %s", status, session_id[:8])
        return None

    return verified_session


async def _mint_access_from_refresh(
    db: AsyncSession,
    request: HTTPConnection,
    response: Response | None,
    refresh_token: str,
):
    payload_refresh = verify_refresh_token(refresh_token)
    if payload_refresh is None:
        logger.warning("Invalid refresh token provided; skipping context auth")
        return None, None

    session_id_value = payload_refresh.get("session_id")
    if await _get_active_session(db, session_id_value) is None:
        return None, None
    session_id = str(session_id_value)

    person_id = payload_refresh.get("person_id")
    username = payload_refresh.get("username")

    # Load user/account sequentially
    user = None
    account_id = None
    if person_id:
        user = await get_person_by_id(db, person_id)
    if username:
        account = await get_account_by_username(db, username)
        if account:
            account_id = account.id

    new_access_token = create_access_token({
        "person_id": person_id,
        "username": username,
        "session_id": payload_refresh.get("session_id"),
    })
    logger.info("Refreshed access token for user=%s, session=%s", username, session_id[:8])

    try:
        await update_last_active_at(db, session_id)
    except Exception as e:
        logger.warning("Failed to update last_active_at for session %s: %s", session_id[:8], e)

    if response is not None:
        cookie_secure = get_cookie_secure_setting()
        cookie_samesite = get_cookie_samesite_setting()
        response.set_cookie(
            key="access_token",
            value=new_access_token,
            httponly=True,
            secure=cookie_secure,
            samesite=cookie_samesite,
            max_age=get_access_cookie_max_age_seconds(),
        )
        response.headers["x-access-token"] = new_access_token

    return user, account_id


async def build_context(
    connection: HTTPConnection,
    response: Response = None,
    db: AsyncSession = Depends(get_db),
) -> Context:
    request = connection
    refresh_token = request.cookies.get("refresh_token")

    user = None
    account_id = None

    # Priorizar cookies HTTP-Only, luego headers para compatibilidad
    access_token = request.cookies.get("access_token")
    if not access_token:
        # Fallback a headers para compatibilidad temporal
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            access_token = auth_header.split(" ")[1]
        else:
            access_token = request.headers.get("x-access-token")

    logger.debug(
        "Auth context: access_token=%s, refresh_token=%s",
        "present" if access_token else "none",
        "present" if refresh_token else "none",
    )

    if access_token:
        payload = verify_token(access_token)
        if payload:
            person_id = payload.get("person_id")
            username = payload.get("username")
            session_id = payload.get("session_id")

            if await _get_active_session(db, session_id) is None:
                return Context(db=db, request=request, response=response, user=None, account_id=None)

            # Execute queries sequentially to avoid AsyncPG concurrent operation errors
            if person_id:
                user = await get_person_by_id(db, person_id)
            if username:
                account = await get_account_by_username(db, username)
                if account:
                    account_id = account.id
        else:
            # Access token invalid/expired -> attempt refresh
            if refresh_token:
                user, account_id = await _mint_access_from_refresh(db, request, response, refresh_token)
    elif refresh_token:
        # No access token present but refresh_cookie exists -> proactively mint new access token
        user, account_id = await _mint_access_from_refresh(db, request, response, refresh_token)

    return Context(db=db, request=request, response=response, user=user, account_id=account_id)

