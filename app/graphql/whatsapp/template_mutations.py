"""GraphQL mutations for WhatsApp message-template management.

Each write mutation orchestrates Meta first (Business Management API) and then mirrors the
result into the local ``app.whatsapp_templates`` table, so the local row always reflects what
Meta accepted. The test-send reuses the chat CRUD/contact helpers and the Cloud API template
send (which works for any recipient, unlike free-form text).
"""
import logging
import re
from typing import List

import strawberry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import whatsappCrud as chat_crud
from app.crud import whatsappTemplatesCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.whatsapp.template_types import (
    CreateTemplateInput,
    SendTemplateTestInput,
    TemplateResult,
    UpdateTemplateInput,
    WhatsAppTemplate,
)
from app.graphql.whatsapp.types import ChatMessage, SendMessageResult
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_template_service as mgmt
from app.services.whatsapp_template_components import build_components

logger = logging.getLogger(__name__)


def _to_template_type(model) -> WhatsAppTemplate:
    return WhatsAppTemplate.from_data(crud.WhatsAppTemplateData.from_model(model))


@strawberry.type
class WhatsAppTemplateMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def sync_whatsapp_templates(self, info: Info) -> List[WhatsAppTemplate]:
        """Pull every template from Meta and upsert the local mirror; return the result."""
        db: AsyncSession = info.context.db
        namespace = await mgmt.fetch_namespace() or ""
        remote = await mgmt.list_templates()
        for t in remote:
            meta_id = t.get("id")
            await crud.upsert_from_meta(
                db,
                name=t.get("name"),
                language=t.get("language"),
                namespace=namespace,
                status=t.get("status") or "",
                category=t.get("category"),
                components=t.get("components"),
                meta_template_id=str(meta_id) if meta_id is not None else None,
                commit=False,
            )
        await db.commit()
        data = await crud.list_templates(db)
        return [WhatsAppTemplate.from_data(d) for d in data]

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def create_whatsapp_template(
        self, info: Info, input: CreateTemplateInput
    ) -> TemplateResult:
        db: AsyncSession = info.context.db
        name = (input.name or "").strip()
        if not name:
            return TemplateResult(success=False, error="El nombre es obligatorio.")
        if not (input.body_text or "").strip():
            return TemplateResult(success=False, error="El cuerpo de la plantilla es obligatorio.")

        components = build_components(input.body_text, input.body_examples, input.footer_text)

        try:
            namespace = await mgmt.fetch_namespace() or ""
            created = await mgmt.create_template(
                name=name,
                language=input.language,
                category=input.category,
                components=components,
            )
        except cloud.WhatsAppError as e:
            return TemplateResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("Error creating template in Meta")
            return TemplateResult(success=False, error=str(e))

        meta_id = created.get("id")
        try:
            tpl = await crud.create_local(
                db,
                name=name,
                namespace=namespace,
                language=input.language,
                status=created.get("status") or "PENDING",
                category=created.get("category") or input.category,
                components=components,
                meta_template_id=str(meta_id) if meta_id is not None else None,
            )
        except IntegrityError:
            await db.rollback()
            return TemplateResult(
                success=False,
                error="Ya existe una plantilla local con ese nombre e idioma.",
            )
        return TemplateResult(success=True, template=_to_template_type(tpl))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def update_whatsapp_template(
        self, info: Info, input: UpdateTemplateInput
    ) -> TemplateResult:
        db: AsyncSession = info.context.db
        tpl = await crud.get_template_model(db, input.id)
        if tpl is None:
            return TemplateResult(success=False, error="Plantilla no encontrada.")
        if not tpl.meta_template_id:
            return TemplateResult(
                success=False,
                error="La plantilla no tiene id de Meta; sincroniza primero.",
            )

        components = build_components(input.body_text, input.body_examples, input.footer_text)
        try:
            await mgmt.edit_template(tpl.meta_template_id, components)
        except cloud.WhatsAppError as e:
            return TemplateResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("Error editing template in Meta")
            return TemplateResult(success=False, error=str(e))

        # Editing resets Meta review, so the template goes back to PENDING.
        updated = await crud.update_local(
            db, input.id, components=components, status="PENDING"
        )
        return TemplateResult(success=True, template=_to_template_type(updated))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def delete_whatsapp_template(self, info: Info, id: int) -> TemplateResult:
        db: AsyncSession = info.context.db
        tpl = await crud.get_template_model(db, id)
        if tpl is None:
            return TemplateResult(success=False, error="Plantilla no encontrada.")
        try:
            await mgmt.delete_template(tpl.template_name, tpl.meta_template_id)
        except cloud.WhatsAppError as e:
            return TemplateResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("Error deleting template in Meta")
            return TemplateResult(success=False, error=str(e))

        await crud.delete_local(db, id)
        return TemplateResult(success=True)

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def send_template_test(
        self, info: Info, input: SendTemplateTestInput
    ) -> SendMessageResult:
        """Send an approved template to an arbitrary number (works outside the 24h window)."""
        db: AsyncSession = info.context.db
        phone = (input.phone or "").strip()
        if not phone:
            return SendMessageResult(success=False, error="Falta el número de teléfono.")

        tpl = await crud.get_template_model(db, input.template_id)
        if tpl is None:
            return SendMessageResult(success=False, error="Plantilla no encontrada.")
        if (tpl.template_status or "").upper() != "APPROVED":
            return SendMessageResult(
                success=False,
                error="La plantilla no está aprobada por Meta; no se puede enviar.",
            )

        wa_id = re.sub(r"\D", "", phone)  # E.164 digits only
        if not wa_id:
            return SendMessageResult(success=False, error="Número de teléfono inválido.")

        contact = await chat_crud.upsert_contact(db, wa_id=wa_id, phone_number=phone)
        conversation = await chat_crud.get_or_open_conversation(db, contact.id)

        try:
            result = await cloud.send_template(
                to=contact.wa_id,
                template_name=tpl.template_name,
                language_code=tpl.template_language,
                body_params=input.body_params,
            )
        except cloud.WhatsAppError as e:
            await db.rollback()
            return SendMessageResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Unexpected error sending template test")
            return SendMessageResult(success=False, error=str(e))

        message = await chat_crud.insert_outbound_message(
            db,
            conversation_id=conversation.id,
            contact_id=contact.id,
            text=f"[Plantilla] {tpl.template_name}",
            wa_message_id=result.get("wa_message_id"),
            message_type="template",
        )
        await db.commit()

        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(chat_crud.ChatMessageData.from_model(message)),
        )
