"""GraphQL mutation for the chatbot configuration.

``save_chatbot_config`` applies the provided fields to the single config row (partial save:
``None`` fields are left untouched). Editing this from the desktop frontend reconfigures the
WhatsApp agent at runtime — no redeploy needed.
"""
import logging

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types import Info

from app.crud import chatbotConfigCrud as crud
from app.graphql.auth.permissions import IsAuthenticated
from app.graphql.chatbot.types import (
    ChatbotConfigResult,
    ChatbotConfigType,
    OptimizeSystemPromptInput,
    OptimizeSystemPromptResult,
    SaveChatbotConfigInput,
    SystemPromptSuggestion,
)
from app.services.chatbot import prompt_optimizer

logger = logging.getLogger(__name__)


@strawberry.type
class ChatbotConfigMutation:
    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def save_chatbot_config(
        self, info: Info, input: SaveChatbotConfigInput
    ) -> ChatbotConfigResult:
        db: AsyncSession = info.context.db

        model = (input.model or "").strip() or None
        try:
            await crud.upsert_config(
                db,
                enabled=input.enabled,
                require_confirmation=input.require_confirmation,
                require_mp_payment=input.require_mp_payment,
                model=model,
                system_prompt=input.system_prompt,
                business_name=input.business_name,
                address=input.address,
                operating_hours=input.operating_hours,
                phone=input.phone,
                policies=input.policies,
                tone=input.tone,
                extra_info=input.extra_info,
            )
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.exception("Error saving chatbot config")
            return ChatbotConfigResult(success=False, error=str(e))

        data = await crud.get_config(db)
        return ChatbotConfigResult(
            success=True,
            config=ChatbotConfigType.from_data(data) if data else None,
        )

    @strawberry.mutation(permission_classes=[IsAuthenticated])
    async def optimize_system_prompt(
        self, info: Info, input: OptimizeSystemPromptInput
    ) -> OptimizeSystemPromptResult:
        """Optimize the chatbot system prompt with Anthropic.

        Read-only: it returns a suggestion for the editor and persists nothing. The existing
        ``save_chatbot_config`` mutation remains the only path that writes the prompt.
        """
        db: AsyncSession = info.context.db
        try:
            suggestion = await prompt_optimizer.optimize_system_prompt(
                db,
                prompt_optimizer.PromptOptimizeRequestData(
                    system_prompt=input.system_prompt or "",
                    tone=input.tone or "",
                    instruction=input.instruction or "",
                ),
            )
        except ValueError as exc:
            return OptimizeSystemPromptResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error optimizing chatbot system prompt")
            return OptimizeSystemPromptResult(success=False, error=str(exc))
        return OptimizeSystemPromptResult(
            success=True,
            suggestion=SystemPromptSuggestion.from_data(suggestion),
        )
