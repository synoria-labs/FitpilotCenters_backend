from typing import List, Optional
import strawberry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.sessions.types import SessionInfo
from app.models.sessionModel import Session
from app.security.jwt import verify_token, verify_refresh_token
from app.core.logging_config import get_logger

logger = get_logger("graphql.sessions.queries")


@strawberry.type
class SessionQuery:
    @strawberry.field(permission_classes=[IsAuthenticated])
    async def my_sessions(self, info: Info) -> List[SessionInfo]:
        """Obtiene todas las sesiones activas del usuario autenticado."""
        db: AsyncSession = info.context.db
        user = info.context.user

        if not user:
            logger.warning("my_sessions called without authenticated user")
            return []

        # Obtener session_id actual del token
        current_session_id = None
        request = info.context.request
        access_token = request.cookies.get("access_token")
        if access_token:
            payload = verify_token(access_token)
            if payload:
                current_session_id = payload.get("session_id")

        # Buscar sesiones del usuario que NO estén revocadas
        stmt = (
            select(Session)
            .where(Session.user_id == user.id)
            .where(Session.revoked_at.is_(None))
            .where(Session.deleted_at.is_(None))
            .order_by(Session.last_active_at.desc())
        )

        result = await db.execute(stmt)
        sessions = result.scalars().all()

        logger.info("Found %d active sessions for user %d", len(sessions), user.id)

        return [
            SessionInfo.from_model(session, current_session_id)
            for session in sessions
        ]

    @strawberry.field(permission_classes=[IsAuthenticated])
    async def all_sessions(self, info: Info) -> List[SessionInfo]:
        """Obtiene todas las sesiones (solo para administradores)."""
        db: AsyncSession = info.context.db

        # TODO: Agregar verificación de rol admin
        # Por ahora, retornar todas las sesiones activas

        stmt = (
            select(Session)
            .where(Session.revoked_at.is_(None))
            .where(Session.deleted_at.is_(None))
            .order_by(Session.last_active_at.desc())
        )

        result = await db.execute(stmt)
        sessions = result.scalars().all()

        logger.info("Found %d total active sessions", len(sessions))

        return [SessionInfo.from_model(session) for session in sessions]
