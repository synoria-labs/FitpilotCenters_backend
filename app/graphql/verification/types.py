import strawberry
from typing import Optional


@strawberry.type
class StepUpRequestResponse:
    success: bool
    verification_id: Optional[str]
    channel: Optional[str]
    masked_destination: Optional[str]
    next_cooldown_seconds: Optional[int]
    message: str


@strawberry.type
class StepUpVerifyResponse:
    success: bool
    proof: Optional[str]
    message: str
