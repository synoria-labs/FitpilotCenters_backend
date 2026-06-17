"""GraphQL mutations for WhatsApp message-template management.

Each write mutation orchestrates Meta first (Business Management API) and then mirrors the
result into the local ``app.whatsapp_templates`` table, so the local row always reflects what
Meta accepted. The test-send reuses the chat CRUD/contact helpers and the Cloud API template
send (which works for any recipient, unlike free-form text).
"""
import logging
import re
from typing import List, Optional

import strawberry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.file_uploads import Upload
from strawberry.types import Info

from app.crud import whatsappCrud as chat_crud
from app.crud import whatsappMediaAssetsCrud as media_crud
from app.crud import whatsappTemplatesCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.whatsapp.template_types import (
    AssistWhatsappTemplateInput,
    AssistWhatsappTemplateResult,
    CreateTemplateInput,
    SendTemplateTestInput,
    TemplateResult,
    TemplateAiSuggestion,
    UpdateTemplateInput,
    WhatsAppMediaAsset,
    WhatsAppMediaKind,
    WhatsAppTemplate,
)
from app.graphql.whatsapp.types import ChatMessage, SendMessageResult
from app.services import whatsapp_cloud_service as cloud
from app.services import whatsapp_template_service as mgmt
from app.services import whatsapp_media_assets_service as media_service
from app.services import whatsapp_template_ai_service as template_ai
from app.services.whatsapp_template_components import (
    build_components,
    buttons_from_components,
    carousel_cards_from_components,
    embed_carousel_card_assets,
    header_handle_from_components,
    header_text_example_value,
    header_text_value,
    render_template_text,
    required_header_kind,
)
from app.services.whatsapp_template_send_media import (
    resolve_carousel_card_media,
    resolve_template_send_header_media,
)

logger = logging.getLogger(__name__)


def _to_template_type(model) -> WhatsAppTemplate:
    return WhatsAppTemplate.from_data(crud.WhatsAppTemplateData.from_model(model))


async def _ensure_header_asset(
    db: AsyncSession,
    asset_id: Optional[int],
    header_format: Optional[str],
):
    asset = await media_crud.get_asset_model(db, asset_id)
    media_service.assert_asset_matches_header(asset, header_format)
    return asset


async def _ensure_sample_handle(db: AsyncSession, asset) -> str:
    existing = (asset.sample_header_handle or "").strip()
    if existing:
        return existing
    raw = await media_service.fetch_public_asset_bytes(asset)
    handle = await mgmt.upload_template_header_sample(
        filename=asset.original_filename,
        mime_type=asset.mime_type,
        content=raw,
    )
    await media_crud.store_sample_handle(db, asset, handle, commit=False)
    return handle


def _buttons_to_dicts(buttons) -> list:
    """Convert TemplateButtonInput list -> plain dicts for ``build_components``."""
    result = []
    for button in buttons or []:
        result.append(
            {
                "type": button.type,
                "text": button.text,
                "url": button.url,
                "phone_number": button.phone_number,
                "offer_code": button.offer_code,
                "subtype": button.subtype,
                "example": button.example,
            }
        )
    return result


def _location_to_dict(location) -> Optional[dict]:
    if location is None:
        return None
    return {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "name": location.name,
        "address": location.address,
    }


def _card_overrides_to_dicts(overrides) -> list:
    result = []
    for override in overrides or []:
        result.append(
            {
                "media_asset_id": override.media_asset_id,
                "media_url": override.media_url,
                "media_id": override.media_id,
                "body_params": override.body_params,
                "button_url_param": override.button_url_param,
            }
        )
    return result


async def _prepare_carousel_cards(db: AsyncSession, cards) -> tuple:
    """Upload a sample handle per card and return (card defs for build_components, asset ids)."""
    card_defs = []
    card_asset_ids = []
    for card in cards or []:
        card_format = (card.header_format or "").strip().upper()
        asset = await _ensure_header_asset(db, card.header_media_asset_id, card_format)
        handle = await _ensure_sample_handle(db, asset)
        card_defs.append(
            {
                "header_format": card_format,
                "header_handle": handle,
                "body_text": card.body_text,
                "body_examples": card.body_examples,
                "buttons": _buttons_to_dicts(card.buttons),
            }
        )
        card_asset_ids.append(asset.id)
    return card_defs, card_asset_ids


@strawberry.type
class WhatsAppTemplateMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def assist_whatsapp_template(
        self,
        info: Info,
        input: AssistWhatsappTemplateInput,
    ) -> AssistWhatsappTemplateResult:
        """Generate an AI writing suggestion for the template editor.

        This is read-only: it does not persist local rows and does not call Meta.
        """
        db: AsyncSession = info.context.db
        try:
            suggestion = await template_ai.assist_whatsapp_template(
                db,
                template_ai.TemplateAiRequestData(
                    action=input.action.value,
                    body_text=input.body_text or "",
                    body_examples=input.body_examples or [],
                    footer_text=input.footer_text,
                    template_name=input.template_name,
                    category=input.category,
                    language=input.language,
                    instruction=input.instruction,
                ),
            )
        except ValueError as exc:
            return AssistWhatsappTemplateResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error generating WhatsApp template AI suggestion")
            return AssistWhatsappTemplateResult(success=False, error=str(exc))
        return AssistWhatsappTemplateResult(
            success=True,
            suggestion=TemplateAiSuggestion.from_data(suggestion),
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def upload_whatsapp_media_asset(
        self,
        info: Info,
        file: Upload,
        kind: WhatsAppMediaKind,
        display_name: Optional[str] = None,
    ) -> WhatsAppMediaAsset:
        """Upload a reusable WhatsApp media asset to configured R2/S3 storage."""
        db: AsyncSession = info.context.db
        try:
            asset = await media_service.upload_asset(
                db,
                file=file,
                kind=kind.value,
                display_name=display_name,
            )
        except media_service.MediaAssetError as exc:
            raise ValueError(str(exc)) from exc
        return WhatsAppMediaAsset.from_data(media_crud.WhatsAppMediaAssetData.from_model(asset))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def archive_whatsapp_media_asset(self, info: Info, id: int) -> WhatsAppMediaAsset:
        db: AsyncSession = info.context.db
        asset = await media_crud.archive_asset(db, id)
        if asset is None:
            raise ValueError("Asset no encontrado.")
        return WhatsAppMediaAsset.from_data(media_crud.WhatsAppMediaAssetData.from_model(asset))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def validate_whatsapp_media_asset(self, info: Info, id: int) -> WhatsAppMediaAsset:
        db: AsyncSession = info.context.db
        asset = await media_crud.get_asset_model(db, id)
        if asset is None:
            raise ValueError("Asset no encontrado.")
        try:
            await media_service.validate_asset_url(asset)
        except media_service.MediaAssetError as exc:
            raise ValueError(str(exc)) from exc
        asset = await media_crud.mark_validated(db, asset)
        return WhatsAppMediaAsset.from_data(media_crud.WhatsAppMediaAssetData.from_model(asset))

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def sync_whatsapp_templates(self, info: Info) -> List[WhatsAppTemplate]:
        """Pull every template from Meta and upsert the local mirror; return the result."""
        db: AsyncSession = info.context.db
        namespace = await mgmt.fetch_namespace() or ""
        remote = await mgmt.list_templates()
        remote_keys = set()
        for t in remote:
            meta_id = t.get("id")
            name = t.get("name")
            language = t.get("language")
            if not name or not language:
                continue
            remote_keys.add((name, language))
            await crud.upsert_from_meta(
                db,
                name=name,
                language=language,
                namespace=namespace,
                status=t.get("status") or "",
                category=t.get("category"),
                components=t.get("components"),
                meta_template_id=str(meta_id) if meta_id is not None else None,
                commit=False,
            )
        await crud.mark_not_found_except(db, remote_keys, commit=False)
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

        header_format = (input.header_format or "").strip().upper() or None
        header_handle = None
        header_asset_id = None
        # ``components`` is the clean Meta payload; ``local_components`` may carry FitPilot-only
        # keys (per-card asset ids for carousel sends).
        try:
            if input.carousel_cards:
                card_defs, card_asset_ids = await _prepare_carousel_cards(db, input.carousel_cards)
                components = build_components(
                    input.body_text,
                    input.body_examples,
                    input.footer_text,
                    carousel_cards=card_defs,
                )
                local_components = embed_carousel_card_assets(components, card_asset_ids)
            else:
                if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
                    asset = await _ensure_header_asset(
                        db, input.header_media_asset_id, header_format
                    )
                    header_handle = await _ensure_sample_handle(db, asset)
                    header_asset_id = asset.id
                components = build_components(
                    input.body_text,
                    input.body_examples,
                    input.footer_text,
                    header_format=header_format,
                    header_handle=header_handle,
                    header_text=input.header_text,
                    header_text_example=input.header_text_example,
                    buttons=_buttons_to_dicts(input.buttons),
                )
                local_components = components
        except (media_service.MediaAssetError, cloud.WhatsAppError, ValueError) as exc:
            await db.rollback()
            return TemplateResult(success=False, error=str(getattr(exc, "message", exc)))

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
                components=local_components,
                meta_template_id=str(meta_id) if meta_id is not None else None,
                default_header_media_asset_id=header_asset_id,
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

        if carousel_cards_from_components(tpl.components):
            return TemplateResult(
                success=False,
                error="Las plantillas de carrusel no se editan aquí; elimínala y vuelve a crearla.",
            )

        header_kind = required_header_kind(tpl.components)
        header_handle = header_handle_from_components(tpl.components)
        header_asset_id = input.header_media_asset_id or tpl.default_header_media_asset_id
        header_text = None
        header_text_example = None
        try:
            if header_kind in {"IMAGE", "VIDEO", "DOCUMENT"} and input.header_media_asset_id:
                asset = await _ensure_header_asset(db, input.header_media_asset_id, header_kind)
                header_handle = await _ensure_sample_handle(db, asset)
                header_asset_id = asset.id
            elif header_kind == "TEXT":
                header_text = (
                    input.header_text
                    if input.header_text is not None
                    else header_text_value(tpl.components)
                )
                header_text_example = (
                    input.header_text_example
                    if input.header_text_example is not None
                    else header_text_example_value(tpl.components)
                )
            buttons = (
                _buttons_to_dicts(input.buttons)
                if input.buttons is not None
                else buttons_from_components(tpl.components)
            )
            components = build_components(
                input.body_text,
                input.body_examples,
                input.footer_text,
                header_format=header_kind,
                header_handle=header_handle,
                header_text=header_text,
                header_text_example=header_text_example,
                buttons=buttons,
            )
        except (media_service.MediaAssetError, cloud.WhatsAppError, ValueError) as exc:
            await db.rollback()
            return TemplateResult(success=False, error=str(getattr(exc, "message", exc)))
        try:
            await mgmt.edit_template(tpl.meta_template_id, components)
        except cloud.WhatsAppError as e:
            return TemplateResult(success=False, error=e.message)
        except Exception as e:  # noqa: BLE001
            logger.exception("Error editing template in Meta")
            return TemplateResult(success=False, error=str(e))

        # Editing resets Meta review, so the template goes back to PENDING.
        updated = await crud.update_local(
            db,
            input.id,
            components=components,
            status="PENDING",
            default_header_media_asset_id=header_asset_id,
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

        is_carousel = bool(carousel_cards_from_components(tpl.components))
        try:
            if is_carousel:
                resolved_media = None
                carousel_runtime = await resolve_carousel_card_media(
                    db,
                    template=tpl,
                    card_overrides=_card_overrides_to_dicts(input.carousel_card_overrides),
                )
            else:
                resolved_media = await resolve_template_send_header_media(
                    db,
                    template=tpl,
                    override_media_asset_id=input.header_media_asset_id,
                    legacy_header_media_url=input.header_media_url,
                    header_media_id=input.header_media_id,
                )
                carousel_runtime = None
        except media_service.MediaAssetError as exc:
            return SendMessageResult(success=False, error=str(exc))

        try:
            # authoritative=False: reuse an existing contact (52/521 aware) instead of
            # creating a duplicate, and never overwrite its canonical wa_id with the typed one.
            contact = await chat_crud.upsert_contact(
                db, wa_id=wa_id, phone_number=phone, authoritative=False
            )
            conversation = await chat_crud.get_or_open_conversation(db, contact.id)
            result = await cloud.send_template(
                to=contact.wa_id,
                template_name=tpl.template_name,
                language_code=tpl.template_language,
                body_params=input.body_params,
                components=tpl.components,
                header_media_url=resolved_media.media_url if resolved_media else None,
                header_media_id=resolved_media.media_id if resolved_media else None,
                header_text_param=input.header_text_param,
                location=_location_to_dict(input.location),
                button_url_param=input.button_url_param,
                carousel_cards=carousel_runtime,
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
            text=render_template_text(tpl.components, input.body_params) or tpl.template_name,
            wa_message_id=result.get("wa_message_id"),
            message_type="template",
            template_id=tpl.id,
        )
        # Persist the header media so the chat bubble can render it (same public
        # asset URL sent to Meta). Only when there is a fetchable URL.
        if resolved_media and resolved_media.media_url and resolved_media.media_format:
            await chat_crud.insert_outbound_media(
                db,
                message_id=message.id,
                media_type=resolved_media.media_format.lower(),
                mime_type=None,
                filename=None,
                file_size=None,
                sha256=None,
                media_url=resolved_media.media_url,
                cloud_media_id=resolved_media.media_id,
            )
        await db.commit()

        # Re-fetch with the media relation eager-loaded so the result carries it.
        data = await chat_crud.get_message_by_id(db, message.id)
        return SendMessageResult(
            success=True,
            message=ChatMessage.from_data(data) if data else None,
        )
