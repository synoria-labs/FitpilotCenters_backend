import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.security.hashing import hash_password, verify_password
from app.crud.usersCrud import (
    create_person,
    create_user_with_account,
    update_user as crud_update_user,
    set_account_active,
    reset_account_password,
    update_own_account,
    get_user_by_account_id,
    username_exists,
)
from app.crud.permissions import MANAGE_USERS
from app.graphql.auth.permissions import IsAuthenticated, require_capability
from app.graphql.users.types import (
    CreatePersonInput, CreatePersonResponse, Person,
    CreateUserInput, UpdateUserInput, UserMutationResponse, AppUser,
    UpdateMyAccountInput,
)

MIN_PASSWORD_LENGTH = 8


# Sentinel so partial updates don't wipe fields that were not provided.
_UNSET = object()


@strawberry.type
class UserMutation:
    @strawberry.mutation
    async def create_person(self, data: CreatePersonInput, info: strawberry.Info) -> CreatePersonResponse:
        """Create a new person in the system"""
        person = await create_person(
            db=info.context.db,
            full_name=data.full_name,
            email=data.email,
            phone_number=data.phone_number
        )

        return CreatePersonResponse(
            person=Person.from_model(person),
            message="Person created successfully"
        )

    # ------------------------------------------------------------------
    # User (login account) management — requires the manage_users capability.
    # ------------------------------------------------------------------
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_user(self, info: Info, input: CreateUserInput) -> UserMutationResponse:
        """Create a login account (person + credentials + roles)."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_USERS)
        if error:
            return UserMutationResponse(success=False, user=None, message=error)

        full_name = (input.full_name or "").strip()
        username = (input.username or "").strip()
        password = input.password or ""

        if not full_name:
            return UserMutationResponse(success=False, user=None, message="El nombre es obligatorio")
        if not username:
            return UserMutationResponse(success=False, user=None, message="El usuario es obligatorio")
        if not password:
            return UserMutationResponse(success=False, user=None, message="La contraseña es obligatoria")
        if not input.role_ids:
            return UserMutationResponse(success=False, user=None, message="Debes asignar al menos un rol")

        try:
            if await username_exists(db, username):
                return UserMutationResponse(
                    success=False, user=None, message="El nombre de usuario ya existe"
                )

            account = await create_user_with_account(
                db=db,
                full_name=full_name,
                username=username,
                password_hash=hash_password(password=password),
                email=(input.email or None),
                phone_number=(input.phone_number or None),
                role_ids=list(input.role_ids),
            )

            return UserMutationResponse(
                success=True,
                user=AppUser.from_account(account) if account else None,
                message="Usuario creado exitosamente",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return UserMutationResponse(
                success=False, user=None, message=f"Error al crear usuario: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_user(self, info: Info, input: UpdateUserInput) -> UserMutationResponse:
        """Update a user's identity, username, active state and/or roles."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_USERS)
        if error:
            return UserMutationResponse(success=False, user=None, message=error)

        def _opt(value):
            return value if value is not None else _UNSET

        try:
            new_username = (input.username or "").strip() if input.username is not None else None
            if new_username:
                if await username_exists(db, new_username, exclude_account_id=input.account_id):
                    return UserMutationResponse(
                        success=False, user=None, message="El nombre de usuario ya existe"
                    )

            account = await crud_update_user(
                db=db,
                account_id=input.account_id,
                full_name=_opt(input.full_name),
                email=input.email if input.email is not None else _UNSET,
                phone_number=input.phone_number if input.phone_number is not None else _UNSET,
                username=_opt(new_username),
                is_active=_opt(input.is_active),
                role_ids=_opt(input.role_ids),
            )

            if account is None:
                return UserMutationResponse(success=False, user=None, message="Usuario no encontrado")

            return UserMutationResponse(
                success=True,
                user=AppUser.from_account(account),
                message="Usuario actualizado exitosamente",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return UserMutationResponse(
                success=False, user=None, message=f"Error al actualizar usuario: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def set_user_active(self, info: Info, account_id: int, is_active: bool) -> UserMutationResponse:
        """Activate or deactivate (soft-delete) a login account."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_USERS)
        if error:
            return UserMutationResponse(success=False, user=None, message=error)

        try:
            account = await set_account_active(db=db, account_id=account_id, is_active=is_active)
            if account is None:
                return UserMutationResponse(success=False, user=None, message="Usuario no encontrado")

            action = "reactivado" if is_active else "desactivado"
            return UserMutationResponse(
                success=True,
                user=AppUser.from_account(account),
                message=f"Usuario {action} exitosamente",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return UserMutationResponse(
                success=False, user=None, message=f"Error al cambiar el estado del usuario: {str(e)}"
            )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def reset_user_password(self, info: Info, account_id: int, password: str) -> UserMutationResponse:
        """Set a new password for a user account."""
        db: AsyncSession = info.context.db

        error = await require_capability(info, MANAGE_USERS)
        if error:
            return UserMutationResponse(success=False, user=None, message=error)

        if not (password or "").strip():
            return UserMutationResponse(success=False, user=None, message="La contraseña es obligatoria")

        try:
            account = await reset_account_password(
                db=db, account_id=account_id, password_hash=hash_password(password=password)
            )
            if account is None:
                return UserMutationResponse(success=False, user=None, message="Usuario no encontrado")

            return UserMutationResponse(
                success=True, user=None, message="Contraseña actualizada exitosamente"
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return UserMutationResponse(
                success=False, user=None, message=f"Error al restablecer la contraseña: {str(e)}"
            )

    # ------------------------------------------------------------------
    # Self-service: the current user updates their OWN account.
    # account_id/session_id come from the authenticated context — never the client.
    # ------------------------------------------------------------------
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_my_account(self, info: Info, input: UpdateMyAccountInput) -> UserMutationResponse:
        """Let the authenticated user edit their own name/email/phone/password."""
        db: AsyncSession = info.context.db

        account_id = getattr(info.context, "account_id", None)
        if account_id is None:
            return UserMutationResponse(success=False, user=None, message="Acceso no autorizado")

        _UNSET = object()
        full_name = _UNSET
        email = _UNSET
        phone_number = _UNSET
        new_password_hash = _UNSET
        revoke_others = False

        # full_name: required-if-sent, trimmed, max 200.
        if input.full_name is not None:
            value = input.full_name.strip()
            if not value:
                return UserMutationResponse(success=False, user=None, message="El nombre no puede estar vacío")
            if len(value) > 200:
                return UserMutationResponse(success=False, user=None, message="El nombre es demasiado largo (máx. 200)")
            full_name = value

        # email: "" clears; non-empty must contain "@"; max 200.
        if input.email is not None:
            value = input.email.strip()
            if value == "":
                email = None
            elif "@" not in value or len(value) > 200:
                return UserMutationResponse(success=False, user=None, message="Correo inválido")
            else:
                email = value

        # phone_number: "" clears; max 32.
        if input.phone_number is not None:
            value = input.phone_number.strip()
            if value == "":
                phone_number = None
            elif len(value) > 32:
                return UserMutationResponse(success=False, user=None, message="Teléfono demasiado largo (máx. 32)")
            else:
                phone_number = value

        # password change: requires current password verification + min length.
        if input.new_password is not None:
            new_password = input.new_password
            if not (input.current_password or ""):
                return UserMutationResponse(success=False, user=None, message="Debes indicar tu contraseña actual")
            if len(new_password) < MIN_PASSWORD_LENGTH:
                return UserMutationResponse(
                    success=False, user=None,
                    message=f"La nueva contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres",
                )

            current_account = await get_user_by_account_id(db, account_id)
            if current_account is None:
                return UserMutationResponse(success=False, user=None, message="Cuenta no encontrada")
            if not verify_password(input.current_password, current_account.password_hash):
                return UserMutationResponse(success=False, user=None, message="La contraseña actual es incorrecta")

            new_password_hash = hash_password(password=new_password)
            revoke_others = True

        if full_name is _UNSET and email is _UNSET and phone_number is _UNSET and new_password_hash is _UNSET:
            return UserMutationResponse(success=False, user=None, message="No hay cambios para aplicar")

        try:
            account = await update_own_account(
                db=db,
                account_id=account_id,
                full_name=full_name,
                email=email,
                phone_number=phone_number,
                new_password_hash=new_password_hash,
                revoke_other_sessions_now=revoke_others,
                keep_session_id=getattr(info.context, "session_id", None),
            )
            if account is None:
                return UserMutationResponse(success=False, user=None, message="Cuenta no encontrada")

            return UserMutationResponse(
                success=True,
                user=AppUser.from_account(account),
                message="Datos actualizados exitosamente",
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return UserMutationResponse(
                success=False, user=None, message=f"Error al actualizar la cuenta: {str(e)}"
            )
