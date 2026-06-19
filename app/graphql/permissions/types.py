from typing import List, Optional

import strawberry


@strawberry.type
class RoleCapabilities:
    """A role and the capabilities currently granted to it."""

    role_code: str
    role_description: Optional[str]
    capabilities: List[str]
    locked: bool  # True for admin (implicit super-user; not editable)


@strawberry.type
class CapabilityMutationResponse:
    success: bool
    message: str
