import logging
import os
# from dataclasses import dataclass
# from fastapi import Request, Response
# from sqlalchemy.ext.asyncio import AsyncSession
# from strawberry.fastapi import BaseContext
import strawberry
from strawberry.extensions import AddValidationRules, QueryDepthLimiter
from graphql import NoSchemaIntrospectionCustomRule

from app.core.env import is_production

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
from app.graphql.dashboard.queries import DashboardQuery
from app.graphql.whatsapp.queries import WhatsAppChatQuery
from app.graphql.whatsapp.mutations import WhatsAppChatMutation
from app.graphql.whatsapp.subscriptions import WhatsAppChatSubscription
from app.graphql.whatsapp.template_queries import WhatsAppTemplateQuery
from app.graphql.whatsapp.template_mutations import WhatsAppTemplateMutation
from app.graphql.notifications.queries import NotificationSettingsQuery
from app.graphql.notifications.mutations import NotificationSettingsMutation
from app.graphql.chatbot.queries import ChatbotConfigQuery
from app.graphql.chatbot.mutations import ChatbotConfigMutation
from app.graphql.campaigns.queries import CampaignsQuery
from app.graphql.campaigns.mutations import CampaignsMutation
from app.graphql.permissions.queries import PermissionsQuery
from app.graphql.permissions.mutations import PermissionsMutation
from app.graphql.owner_agent.queries import OwnerAgentQuery
from app.graphql.owner_agent.mutations import OwnerAgentMutation
from app.graphql.verification.mutations import StepUpMutation
from app.graphql.pos.queries import PosQuery
from app.graphql.pos.mutations import PosMutation

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
class Query(UserQuery, MembersQuery, MembershipsQuery, LeadsQuery, ReservationQuery, StandingBookingQuery, SessionQuery, ClassSessionQueries, DashboardQuery, WhatsAppChatQuery, WhatsAppTemplateQuery, NotificationSettingsQuery, ChatbotConfigQuery, CampaignsQuery, PermissionsQuery, OwnerAgentQuery, PosQuery):
    @strawberry.field
    def hello(self) -> str:
        return "Hello from GraphQL!"

@strawberry.type
class Mutation(AuthMutation, UserMutation, MemberMutation, MembershipMutation, LeadsMutation, ReservationMutation, StandingBookingMutation, SessionMutation, ClassSessionMutations, WhatsAppChatMutation, WhatsAppTemplateMutation, NotificationSettingsMutation, ChatbotConfigMutation, CampaignsMutation, PermissionsMutation, OwnerAgentMutation, PosMutation, StepUpMutation):
    pass


# Named "RootSubscription" (not "Subscription") to avoid clashing with the existing
# membership domain type also named "Subscription" (memberships/types.py).
@strawberry.type
class RootSubscription(WhatsAppChatSubscription):
    pass

# @dataclass
# class Context(BaseContext):
#     db: AsyncSession
#     request: Request
#     response: Response
#     user: object | None = None
   
# Query-cost guardrails. Depth limit blunts maliciously nested queries (DoS);
# introspection is disabled in production so the schema/attack surface is not
# self-documenting on the public endpoint (the GraphiQL IDE is also turned off
# in main.py for production).
_MAX_QUERY_DEPTH = int(os.getenv("GRAPHQL_MAX_DEPTH", "15"))
_schema_extensions = [QueryDepthLimiter(max_depth=_MAX_QUERY_DEPTH)]
if is_production():
    _schema_extensions.append(AddValidationRules([NoSchemaIntrospectionCustomRule]))

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=RootSubscription,
    extensions=_schema_extensions,
)
