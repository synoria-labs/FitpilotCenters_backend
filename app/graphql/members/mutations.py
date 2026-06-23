from typing import Optional
from datetime import datetime, timezone
import logging
import time

import strawberry
from strawberry.file_uploads import Upload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update
from strawberry.types import Info

from app.crud.membersCrud import create_member, update_member, delete_member_and_related
from app.graphql.members.types import Member, MemberResponse, DeleteMemberResponse
from app.graphql.auth.permissions import IsAuthenticated, require_step_up_proof
from app.core.verification_config import step_up_enabled
from app.crud.authCrud import get_account_by_id
from app.security.hashing import verify_password
from app.services.image_service import ImageService
from app.models import People
from app.core.conversions import coerce_int

logger = logging.getLogger(__name__)


@strawberry.input
class CreateMemberInput:
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


@strawberry.input
class UpdateMemberInput:
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


async def _build_member_response(
    db: AsyncSession,
    member_id: int,
    *,
    success_message: str,
    missing_message: str,
) -> MemberResponse:
    """Load member data and return a standardized MemberResponse."""
    from app.crud.membersCrud import get_member_by_id

    member_data = await get_member_by_id(db=db, member_id=member_id)
    if not member_data:
        return MemberResponse(
            success=False,
            member=None,
            message=missing_message,
            error_code="MEMBER_NOT_FOUND",
            error_cause="Socio no encontrado",
        )

    return MemberResponse(
        success=True,
        member=Member.from_data(member_data),
        message=success_message,
    )


@strawberry.type
class MemberMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_member(self, info: Info, input: CreateMemberInput) -> MemberResponse:
        """Create a new member"""
        db: AsyncSession = info.context.db

        try:
            person = await create_member(
                db=db,
                full_name=input.full_name,
                email=input.email,
                phone_number=input.phone_number,
                wa_id=input.wa_id
            )

            return await _build_member_response(
                db=db,
                member_id=person.id,
                success_message="Miembro creado exitosamente",
                missing_message="Error al obtener datos del miembro creado",
            )

        except Exception as e:
            return MemberResponse(
                success=False,
                member=None,
                message=f"Error al crear miembro: {str(e)}",
                error_code="CREATE_FAILED",
                error_cause="Error al crear socio",
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_member(self, info: Info, member_id: int, input: UpdateMemberInput) -> MemberResponse:
        """Update member information with explicit success/error metadata."""
        db: AsyncSession = info.context.db
        started_at = time.perf_counter()
        response = MemberResponse(
            success=False,
            member=None,
            message="No se guardaron los cambios.",
            error_code="UPDATE_FAILED",
            error_cause="Error al actualizar socio",
        )

        member_id = coerce_int(member_id)
        if member_id is None:
            response = MemberResponse(
                success=False,
                member=None,
                message="No se guardaron los cambios. Causa: ID de socio invalido.",
                error_code="VALIDATION_ERROR",
                error_cause="ID de socio invalido",
            )
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "update_member member_id=%s success=%s error_code=%s duration_ms=%.2f",
                member_id,
                response.success,
                response.error_code,
                duration_ms,
            )
            return response

        try:
            update_data = {}
            if input.full_name is not None:
                update_data["full_name"] = input.full_name
            if input.email is not None:
                update_data["email"] = input.email
            if input.phone_number is not None:
                update_data["phone_number"] = input.phone_number
            if input.wa_id is not None:
                update_data["wa_id"] = input.wa_id

            if not update_data:
                response = MemberResponse(
                    success=False,
                    member=None,
                    message="No se guardaron los cambios. Causa: no se enviaron campos para actualizar.",
                    error_code="VALIDATION_ERROR",
                    error_cause="Sin datos para actualizar",
                )
                return response

            logger.info(
                "update_member requested member_id=%s fields=%s",
                member_id,
                sorted(update_data.keys()),
            )

            person = await update_member(db=db, member_id=member_id, **update_data)

            if not person:
                response = MemberResponse(
                    success=False,
                    member=None,
                    message="No se guardaron los cambios. Causa: socio no encontrado.",
                    error_code="MEMBER_NOT_FOUND",
                    error_cause="Socio no encontrado",
                )
                return response

            response = await _build_member_response(
                db=db,
                member_id=person.id,
                success_message="Miembro actualizado exitosamente",
                missing_message="Error al obtener datos del miembro actualizado",
            )
            return response

        except Exception as e:
            await db.rollback()
            response = MemberResponse(
                success=False,
                member=None,
                message=f"No se guardaron los cambios. Causa: {str(e)}",
                error_code="UPDATE_FAILED",
                error_cause="Error al actualizar socio",
            )
            return response
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "update_member member_id=%s success=%s error_code=%s duration_ms=%.2f",
                member_id,
                response.success,
                response.error_code,
                duration_ms,
            )

    @strawberry.mutation(name="deleteMember", permission_classes=[IsAuthenticated])
    async def delete_member(
        self,
        info: Info,
        member_id: int,
        admin_password: Optional[str] = None,
        step_up_proof: Optional[str] = None,
    ) -> DeleteMemberResponse:
        """Delete a member. Requires admin role + a second factor.

        When step-up verification is enabled, a single-use ``step_up_proof`` is
        required; otherwise the admin re-enters their password (legacy path).
        """
        db: AsyncSession = info.context.db

        member_id = coerce_int(member_id)
        if member_id is None:
            return DeleteMemberResponse(success=False, message="ID de socio invalido")

        account_id = getattr(info.context, "account_id", None)
        if not account_id:
            return DeleteMemberResponse(success=False, message="Acceso no autorizado")

        account = await get_account_by_id(db=db, account_id=account_id)
        if not account or not account.person:
            return DeleteMemberResponse(
                success=False, message="Cuenta de administrador no encontrada"
            )

        roles = {role.role.code for role in account.person.roles if role.role}
        if "admin" not in roles:
            return DeleteMemberResponse(success=False, message="Se requiere rol de administrador")

        # Second factor: step-up proof when enabled, else legacy admin password.
        if step_up_enabled():
            error = await require_step_up_proof(info, step_up_proof)
            if error:
                return DeleteMemberResponse(success=False, message=error)
        else:
            if not admin_password:
                return DeleteMemberResponse(
                    success=False, message="La contrasena de administrador es obligatoria"
                )
            if not verify_password(admin_password, account.password_hash):
                return DeleteMemberResponse(
                    success=False, message="Contrasena de administrador incorrecta"
                )

        success, message = await delete_member_and_related(db=db, member_id=member_id)
        return DeleteMemberResponse(success=success, message=message)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def upload_profile_picture(self, info: Info, member_id: int, file: Upload) -> MemberResponse:
        """Upload or update a member's profile picture"""
        db: AsyncSession = info.context.db
        image_service = ImageService()

        member_id = coerce_int(member_id)
        if member_id is None:
            return MemberResponse(
                success=False,
                member=None,
                message="ID de miembro invalido",
                error_code="VALIDATION_ERROR",
                error_cause="ID de socio invalido",
            )

        try:
            # Read file data
            file_data = await file.read()

            # Validate image
            is_valid, error_message = image_service.validate_image(file_data, file.filename)
            if not is_valid:
                return MemberResponse(
                    success=False,
                    member=None,
                    message=f"Archivo invalido: {error_message}",
                    error_code="VALIDATION_ERROR",
                    error_cause="Archivo invalido",
                )

            # Get current member to check old picture
            from app.crud.membersCrud import get_member_by_id
            member_data = await get_member_by_id(db=db, member_id=member_id)
            if not member_data:
                return MemberResponse(
                    success=False,
                    member=None,
                    message="Miembro no encontrado",
                    error_code="MEMBER_NOT_FOUND",
                    error_cause="Socio no encontrado",
                )

            # Delete old picture if exists
            if member_data.profile_picture_path:
                image_service.delete_old_picture(member_data.profile_picture_path)

            # Process and save new image
            new_path = image_service.process_and_save_image(
                file_data=file_data,
                user_id=member_id,
                original_filename=file.filename
            )

            if not new_path:
                return MemberResponse(
                    success=False,
                    member=None,
                    message="Error al procesar la imagen",
                    error_code="UPLOAD_FAILED",
                    error_cause="Error al procesar la imagen",
                )

            # Update database
            stmt = (
                update(People)
                .where(People.id == member_id)
                .values(
                    profile_picture_path=new_path,
                    profile_picture_uploaded_at=datetime.now(timezone.utc)
                )
            )
            await db.execute(stmt)
            await db.commit()

            return await _build_member_response(
                db=db,
                member_id=member_id,
                success_message="Foto de perfil actualizada exitosamente",
                missing_message="Error al obtener datos actualizados",
            )

        except Exception as e:
            await db.rollback()
            return MemberResponse(
                success=False,
                member=None,
                message=f"Error al cargar foto: {str(e)}",
                error_code="UPLOAD_FAILED",
                error_cause="Error al cargar foto",
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def delete_profile_picture(self, info: Info, member_id: int) -> MemberResponse:
        """Delete a member's profile picture"""
        db: AsyncSession = info.context.db
        image_service = ImageService()

        member_id = coerce_int(member_id)
        if member_id is None:
            return MemberResponse(
                success=False,
                member=None,
                message="ID de miembro invalido",
                error_code="VALIDATION_ERROR",
                error_cause="ID de socio invalido",
            )

        try:
            # Get current member
            from app.crud.membersCrud import get_member_by_id
            member_data = await get_member_by_id(db=db, member_id=member_id)
            if not member_data:
                return MemberResponse(
                    success=False,
                    member=None,
                    message="Miembro no encontrado",
                    error_code="MEMBER_NOT_FOUND",
                    error_cause="Socio no encontrado",
                )

            # Delete picture file if exists
            if member_data.profile_picture_path:
                image_service.delete_old_picture(member_data.profile_picture_path)

            # Update database
            stmt = (
                update(People)
                .where(People.id == member_id)
                .values(
                    profile_picture_path=None,
                    profile_picture_uploaded_at=None
                )
            )
            await db.execute(stmt)
            await db.commit()

            return await _build_member_response(
                db=db,
                member_id=member_id,
                success_message="Foto de perfil eliminada exitosamente",
                missing_message="Error al obtener datos actualizados",
            )

        except Exception as e:
            await db.rollback()
            return MemberResponse(
                success=False,
                member=None,
                message=f"Error al eliminar foto: {str(e)}",
                error_code="DELETE_PICTURE_FAILED",
                error_cause="Error al eliminar foto",
            )

