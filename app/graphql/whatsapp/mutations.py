"""GraphQL mutations for the WhatsApp chat feature."""
import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import whatsappCrud as crud
from app.graphql.whatsapp.types import (
    SendTextMessageInput,
    SendMessageResult,
    ChatMessage,
)
from app.graphql.auth.permissions import IsAuthenticated
from app.services import whatsapp_cloud_service as cloud

logger = logging.getLogger(__name__)


@strawberry.type
class WhatsAppChatMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def send_text_message(
        self, info: Info, input: SendTextMessageInput
    ) -> SendMessageResult:
        """Send a free-form text message via the Cloud API and persist it."""
        db: AsyncSession = info.context.db

        text = (input.text or "").strip()
        if not text:
            return SendMessageResult(success=False, error="El mensaje está vacío.")

        # Resolve the target contact + conversation.
        contact = None
        conversation = None
        if input.conversation_id:
            conversation = await crud.get_conversation(db, input.conversation_id)
            if conversation is None:
                return SendMessageResult(success=False, error="Conversación no encontrada.")
            contact = conversation.contact
        elif input.wa_id:
            # Resolve by normalized number (52/521 aware) so a send never spawns a
            # duplicate contact/conversation for a number that already exists.
            contact = await crud.upsert_contact(
                db, wa_id=input.wa_id, phone_number=input.wa_id, authoritative=False
            )
            conversation = await crud.get_or_open_conversation(db, contact.id)
        else:
            return SendMessageResult(success=False, error="Falta conversationId o waId.")

        # Send via the Cloud API.
        try:
            result = await cloud.send_text(to=contact.wa_id, text=text)
        except cloud.WhatsAppError as e:
            await db.rollback()
            return SendMessageResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Unexpected error sending WhatsApp message")
            return SendMessageResult(success=False, error=str(e))

        # Persist the outbound message (the DB trigger fans it out to subscribers).
        message = await crud.insert_outbound_message(
            db,
            conversation_id=conversation.id,
            contact_id=contact.id,
            text=text,
            wa_message_id=result.get("wa_message_id"),
        )
        await db.commit()

        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(crud.ChatMessageData.from_model(message)),
        )
