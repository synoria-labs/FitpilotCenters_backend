# Modern FitPilot models - Modular architecture
from app.models.userModel import People, Role, PersonRole, Account
from app.models.membershipsModel import MembershipPlan, MembershipSubscription, Payment
from app.models.venueModel import (
    Venue, Seat, SeatType, Asset, AssetType, AssetModel, AssetSeatAssignment, AssetEvent
)
from app.models.classModel import (
    ClassType, ClassTemplate, ClassSession, Reservation, StandingBooking, StandingBookingException
)
from app.models.sessionModel import Session
from app.models.leadsModel import (
    LeadSource, Lead, LeadEvent, FormSubmission, MarketingCampaign,
    LeadAttribution, CommunicationOptIn, WhatsAppThread
)
from app.models.whatsappModel import (
    Contact, Conversation, Message, MessageStatus, Media, WebhookLog,
    WhatsAppTemplate, WhatsAppMediaAsset
)
from app.models.notificationModel import NotificationSetting, NotificationLog

__all__ = [
    "People", "Role", "PersonRole", "Account",
    "MembershipPlan", "MembershipSubscription", "Payment",
    "Venue", "Seat", "SeatType", "Asset", "AssetType", "AssetModel", "AssetSeatAssignment", "AssetEvent",
    "ClassType", "ClassTemplate", "ClassSession", "Reservation", "StandingBooking", "StandingBookingException",
    "Session",
    "LeadSource", "Lead", "LeadEvent", "FormSubmission", "MarketingCampaign",
    "LeadAttribution", "CommunicationOptIn", "WhatsAppThread",
    "Contact", "Conversation", "Message", "MessageStatus", "Media", "WebhookLog",
    "WhatsAppTemplate", "WhatsAppMediaAsset",
    "NotificationSetting", "NotificationLog"
]
