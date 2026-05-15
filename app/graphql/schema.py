import logging
# from dataclasses import dataclass
# from fastapi import Request, Response
# from sqlalchemy.ext.asyncio import AsyncSession
# from strawberry.fastapi import BaseContext
import strawberry

# from app.security.jwt import verify_token
# from app.crud.usersCrud import get_user_by_id
from app.graphql.auth.mutations import AuthMutation
from app.graphql.users.mutations import UserMutation
from app.graphql.users.queries import UserQuery
from app.graphql.members.queries import MembersQuery
from app.graphql.members.mutations import MemberMutation
from app.graphql.memberships.queries import MembershipsQuery
from app.graphql.memberships.mutations import MembershipMutation
from app.graphql.leads.queries import LeadsQuery
from app.graphql.leads.mutations import LeadsMutation
from app.graphql.reservations.queries import ReservationQuery
from app.graphql.reservations.mutations import ReservationMutation
from app.graphql.standing_bookings.queries import StandingBookingQuery
from app.graphql.standing_bookings.mutations import StandingBookingMutation
from app.graphql.sessions.queries import SessionQuery
from app.graphql.sessions.mutations import SessionMutation

logger = logging.getLogger(__name__)

# Optional new GraphQL APIs - only if dependencies are available
try:
    from app.graphql.class_sessions.queries import ClassSessionQueries
    from app.graphql.class_sessions.mutations import ClassSessionMutations
    NEW_APIS_AVAILABLE = True
except ImportError as e:
    NEW_APIS_AVAILABLE = False
    logger.warning("New GraphQL APIs not available due to missing dependencies: %s", e)

    # Create empty classes as placeholders
    @strawberry.type
    class ClassSessionQueries:
        pass

    @strawberry.type
    class ClassSessionMutations:
        pass

@strawberry.type
class Query(UserQuery, MembersQuery, MembershipsQuery, LeadsQuery, ReservationQuery, StandingBookingQuery, SessionQuery, ClassSessionQueries):
    @strawberry.field
    def hello(self) -> str:
        return "Hello from GraphQL!"

@strawberry.type
class Mutation(AuthMutation, UserMutation, MemberMutation, MembershipMutation, LeadsMutation, ReservationMutation, StandingBookingMutation, SessionMutation, ClassSessionMutations):
    pass

# @dataclass
# class Context(BaseContext):
#     db: AsyncSession
#     request: Request
#     response: Response
#     user: object | None = None
   
schema = strawberry.Schema(query=Query, mutation=Mutation)
