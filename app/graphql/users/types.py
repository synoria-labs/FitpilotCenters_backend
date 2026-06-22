import strawberry
from datetime import datetime
from typing import Optional, List
from app.models import People, Role, PersonRole, Account as AccountModel


@strawberry.type
class RoleType:
    id: int
    code: str
    description: Optional[str] = None

    @classmethod
    def from_model(cls, role: Role) -> "RoleType":
        return cls(id=role.id, code=role.code, description=role.description)


@strawberry.type
class PersonRole:
    role: RoleType
    assigned_at: datetime


@strawberry.type
class Account:
    id: int
    username: str
    is_active: bool
    created_at: datetime

    @classmethod
    def from_model(cls, account: "Account") -> "Account":
        return cls(
            id=account.id,
            username=account.username,
            is_active=account.is_active,
            created_at=account.created_at
        )


@strawberry.type
class Person:
    id: int
    full_name: Optional[str]
    email: Optional[str]
    phone_number: Optional[str]
    wa_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    roles: List[PersonRole]

    @classmethod
    def from_model(cls, person: People) -> "Person":
        return cls(
            id=person.id,
            full_name=person.full_name,
            email=person.email,
            phone_number=person.phone_number,
            wa_id=person.wa_id,
            created_at=person.created_at,
            updated_at=person.updated_at,
            roles=[
                PersonRole(
                    role=RoleType.from_model(pr.role),
                    assigned_at=pr.created_at,
                )
                for pr in person.roles
                if pr.role
            ] if person.roles else []
        )


@strawberry.type
class AppUser:
    """A login user: account credentials + identity + assigned roles."""

    account_id: int
    person_id: int
    username: str
    is_active: bool
    full_name: Optional[str]
    email: Optional[str]
    phone_number: Optional[str]
    created_at: datetime
    roles: List[RoleType]

    @classmethod
    def from_account(cls, account: AccountModel) -> "AppUser":
        person = getattr(account, "person", None)
        roles: List[RoleType] = []
        if person is not None and getattr(person, "roles", None):
            roles = [RoleType.from_model(pr.role) for pr in person.roles if pr.role]
        return cls(
            account_id=account.id,
            person_id=account.person_id,
            username=account.username,
            is_active=account.is_active,
            full_name=person.full_name if person else None,
            email=person.email if person else None,
            phone_number=person.phone_number if person else None,
            created_at=account.created_at,
            roles=roles,
        )


@strawberry.input
class CreatePersonInput:
    full_name: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


@strawberry.input
class UpdatePersonInput:
    person_id: int
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    wa_id: Optional[str] = None


@strawberry.type
class CreatePersonResponse:
    person: Person
    message: str


# --- User (login account) management -------------------------------------
@strawberry.input
class CreateUserInput:
    full_name: str
    username: str
    password: str
    email: Optional[str] = None
    phone_number: Optional[str] = None
    role_ids: List[int] = strawberry.field(default_factory=list)


@strawberry.input
class UpdateUserInput:
    account_id: int
    full_name: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    is_active: Optional[bool] = None
    role_ids: Optional[List[int]] = None


@strawberry.type
class UserMutationResponse:
    success: bool
    user: Optional[AppUser]
    message: str


# --- Self-service (current user updates their own account) ----------------
@strawberry.input
class UpdateMyAccountInput:
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone_number: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None
