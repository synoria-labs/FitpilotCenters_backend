from fastapi import HTTPException, Request, Response
import strawberry
from user_agents import parse
import uuid
import datetime

from app.security.hashing import verify_password
from app.security.jwt import (
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
    verify_token,
    get_cookie_secure_setting,
    get_cookie_samesite_setting,
    get_access_cookie_max_age_seconds,
    get_refresh_cookie_max_age_seconds,
)
from app.crud.authCrud import get_account_by_username
from app.crud.usersCrud import get_person_by_id
from app.crud.permissions import get_capabilities_for_person
from app.crud.sessionCrud import create_session, revoke_session
from app.graphql.auth.types import LoginInput, TokenResponse
from app.models.sessionModel import Session
from app.core.logging_config import get_logger
from app.db.postgresql import async_session_factory

logger = get_logger("graphql.auth.mutations")


@strawberry.type
class AuthMutation:
    @strawberry.mutation
    async def login(self, data: LoginInput, info: strawberry.Info) -> TokenResponse:

        # get information from request
        request: Request = info.context.request
        user_agent = request.headers.get("user-agent")
        response: Response = info.context.response

        ip_address = request.client.host if request.client else None
        ua = parse(user_agent)
        device_name = f"{ua.device.family} - {ua.os.family} {ua.os.version_string}"
        # -----------------------------

        identifier = data.identifier
        password = data.password

        # Use the request-scoped DB session from context
        db = info.context.db
        logger.info("Login attempt for '%s' from %s", identifier, ip_address or "unknown-ip")

        account = await get_account_by_username(db=db, username=identifier)
        logger.info("Account lookup for '%s': %s", identifier, "found" if account else "not-found")

        if not account:
            raise HTTPException(status_code=401, detail="Account not found")

        if not verify_password(password, account.password_hash):
            raise HTTPException(status_code=401, detail="Credentials not valid")

        # Load the person's roles + effective capabilities so the client can
        # gate its UI without an extra round-trip. The backend re-checks
        # capabilities from the DB on every protected mutation, so these claims
        # are a UX convenience, not the security boundary.
        person = await get_person_by_id(db, account.person_id)
        role_codes = sorted({pr.role.code for pr in (person.roles if person else []) if pr.role})
        capabilities = sorted(await get_capabilities_for_person(db, person))

        session_id = f"session-id{uuid.uuid4().hex}"
        token_payload = {
            "person_id": str(account.person_id),
            "username": account.username,
            "session_id": session_id,
            "roles": role_codes,
            "capabilities": capabilities,
        }
        refresh_token = create_refresh_token(token_payload)
        access_token = create_access_token(token_payload)

        payload_refresh = verify_refresh_token(refresh_token)
        exp_timestamp = payload_refresh.get("exp") if payload_refresh else None
        exp = datetime.datetime.fromtimestamp(exp_timestamp) if exp_timestamp else None

        logger.debug("Session %s refresh exp: %s", session_id[:8], exp)

        session = Session(
            refresh_token=refresh_token,
            session=session_id,
            device_name=device_name,
            ip_address=ip_address,
            user_agent=user_agent,
            user_id=account.person_id,
            # Do NOT mark revoked_at at login. That field indicates revocation time, not expiry.
            revoked_at=None,
            last_active_at=datetime.datetime.now()
        )

        # Persist session using the same DB session from context
        insert_session = await create_session(
            db=db,
            sessionEntry=session
        )
        logger.info("Session created for '%s' id=%s", account.username, getattr(insert_session, "id", None))

        # Configurar cookies seguras
        cookie_secure = get_cookie_secure_setting()
        cookie_samesite = get_cookie_samesite_setting()

        # Set refresh token as HTTP-Only cookie
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=cookie_secure,
            samesite=cookie_samesite,
            max_age=get_refresh_cookie_max_age_seconds(),
        )

        # Set access token as HTTP-Only cookie también
        response.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=cookie_secure,
            samesite=cookie_samesite,
            max_age=get_access_cookie_max_age_seconds(),
        )

        # Mantener header para compatibilidad temporal
        response.headers["x-access-token"] = access_token

        logger.info("User '%s' logged in; session %s", account.username, session_id[:8])
        return TokenResponse(access_token=access_token)

    @strawberry.mutation
    async def refresh_token(self, info: strawberry.Info) -> TokenResponse:
        """Manual refresh token mutation for edge cases"""
        request: Request = info.context.request
        response: Response = info.context.response
        db = info.context.db

        refresh_token = request.cookies.get("refresh_token")

        if not refresh_token:
            raise HTTPException(status_code=401, detail="No refresh token found")

        payload = verify_refresh_token(refresh_token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        # Re-derive roles/capabilities from the DB so newly granted/revoked
        # permissions take effect on the next refresh (not only on re-login).
        person_id = payload.get("person_id")
        person = await get_person_by_id(db, person_id) if person_id else None
        role_codes = sorted({pr.role.code for pr in (person.roles if person else []) if pr.role})
        capabilities = sorted(await get_capabilities_for_person(db, person))

        # Create new access token
        new_access_token = create_access_token({
            "person_id": person_id,
            "username": payload.get("username"),
            "session_id": payload.get("session_id"),
            "roles": role_codes,
            "capabilities": capabilities,
        })

        # Configurar cookies seguras
        cookie_secure = get_cookie_secure_setting()
        cookie_samesite = get_cookie_samesite_setting()

        # Set new access token as HTTP-Only cookie
        response.set_cookie(
            key="access_token",
            value=new_access_token,
            httponly=True,
            secure=cookie_secure,
            samesite=cookie_samesite,
            max_age=get_access_cookie_max_age_seconds(),
        )

        # Mantener header para compatibilidad temporal
        response.headers["x-access-token"] = new_access_token

        return TokenResponse(access_token=new_access_token)

    @strawberry.mutation
    async def logout(self, info: strawberry.Info) -> bool:
        """Clear session and cookies, revoke session in database"""
        request: Request = info.context.request
        response: Response = info.context.response
        db = info.context.db

        # Intentar obtener session_id de los tokens para revocar la sesión
        session_id = None

        # Prioridad 1: Obtener de access_token
        access_token = request.cookies.get("access_token")
        if access_token:
            payload = verify_token(access_token)
            if payload:
                session_id = payload.get("session_id")

        # Prioridad 2: Obtener de refresh_token si access_token falló
        if not session_id:
            refresh_token = request.cookies.get("refresh_token")
            if refresh_token:
                payload = verify_refresh_token(refresh_token)
                if payload:
                    session_id = payload.get("session_id")

        # Revocar la sesión en la base de datos si encontramos el session_id
        if session_id:
            try:
                await revoke_session(db, session_id)
                await db.commit()
                logger.info("Session %s revoked during logout", session_id[:8] if len(session_id) >= 8 else session_id)
            except Exception as e:
                logger.error("Failed to revoke session %s: %s", session_id[:8] if len(session_id) >= 8 else session_id, e)
                await db.rollback()

        # Limpiar cookies
        response.delete_cookie("access_token")
        response.delete_cookie("refresh_token")

        logger.info("User logged out successfully")
        return True