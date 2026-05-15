import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.sessions.types import RevokeSessionInput
from app.crud.sessionCrud import revoke_session, verify_session
from app.security.jwt import verify_token
from app.core.logging_config import get_logger

logger = get_logger("graphql.sessions.mutations")


@strawberry.type
class SessionMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def revoke_session(self, info: Info, input: RevokeSessionInput) -> bool:
        """Revoca una sesión específica."""
        db: AsyncSession = info.context.db
        user = info.context.user
        request = info.context.request

        if not user:
            logger.warning("revoke_session called without authenticated user")
            return False

        # Verificar que la sesión existe
        session = await verify_session(db, input.session_id)
        if not session:
            logger.warning("Session %s not found", input.session_id[:8])
            return False

        # Verificar que la sesión pertenece al usuario autenticado
        # (o es admin - TODO: implementar verificación de rol)
        if session.user_id != user.id:
            logger.warning(
                "User %d attempted to revoke session %s belonging to user %d",
                user.id,
                input.session_id[:8],
                session.user_id
            )
            return False

        # No permitir revocar la sesión actual
        current_session_id = None
        access_token = request.cookies.get("access_token")
        if access_token:
            payload = verify_token(access_token)
            if payload:
                current_session_id = payload.get("session_id")

        if current_session_id == input.session_id:
            logger.warning(
                "User %d attempted to revoke their current session %s (use logout instead)",
                user.id,
                input.session_id[:8]
            )
            return False

        # Revocar la sesión
        try:
            await revoke_session(db, input.session_id)
            await db.commit()
            logger.info(
                "Session %s revoked by user %d",
                input.session_id[:8],
                user.id
            )
            return True
        except Exception as e:
            logger.error("Failed to revoke session %s: %s", input.session_id[:8], e)
            await db.rollback()
            return False
