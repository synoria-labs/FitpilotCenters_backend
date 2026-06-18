"""Anthropic-powered optimizer for the chatbot system prompt.

This service is read-only: it rewrites the configured ``system_prompt`` so it stops
duplicating the context the agent already injects at runtime (business info from the DB +
the live tools + the fixed tool/purchase/confirmation rule blocks). It never persists: the
desktop editor previews the suggestion and the existing ``save_chatbot_config`` mutation is
the only path that writes the prompt.

The goal — "que no interfiera con el contexto que obtiene en la DB" — is enforced by feeding
the model the exact ``build_business_info`` text plus an inventory of what the tools already
provide, and instructing it to strip those facts from the prompt.

The Anthropic API key is read from the environment by ``ChatAnthropic`` — never hardcoded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chatbot_env import chatbot_env
from app.crud import chatbotConfigCrud
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.services.chatbot.business_context import build_business_info

# Inventory of data the live tools already supply each turn (see chatbot/tools.py). The prompt
# must NOT restate any of it — keep this in sync with build_tools() in tools.py.
_TOOL_INVENTORY_ES = (
    "Las herramientas del agente ya entregan en vivo, sin que el prompt lo repita: "
    "precios y planes (get_membership_plans), horario semanal de clases (get_weekly_schedule), "
    "sedes y direccion (get_venues), instructores (list_instructors), cupo y disponibilidad "
    "(list_available_classes, check_class_availability), membresia y reservas del socio "
    "(get_my_membership, list_my_reservations) y el flujo de compra/confirmacion "
    "(propose_membership, propose_day_pass, confirm_action, cancel_action)."
)

# The runtime also injects these as fixed blocks (see build_system_prompt in agent.py); the
# configured prompt must not duplicate them.
_FIXED_BLOCKS_ES = (
    "El sistema tambien inyecta automaticamente, como bloques fijos: la fecha y hora actual, "
    "las reglas de uso de herramientas, el flujo de compra (paquete vs pase diario) y el flujo "
    "de confirmacion/cancelacion, ademas de la identificacion del socio. No los repitas."
)


@dataclass
class PromptOptimizeRequestData:
    system_prompt: str
    tone: str = ""
    instruction: str = ""


@dataclass
class PromptOptimizeSuggestionData:
    optimized_prompt: str
    removed: List[str]
    notes: List[str]
    warnings: List[str]


def _fallback_config() -> ChatbotConfigData:
    return ChatbotConfigData(
        id=None,
        enabled=False,
        require_confirmation=True,
        require_mp_payment=False,
        model=chatbot_env.MODEL,
        system_prompt=None,
        business_name=None,
        address=None,
        operating_hours=None,
        phone=None,
        policies=None,
        tone=None,
        extra_info=None,
        created_at=None,
        updated_at=None,
    )


def build_llm(config: ChatbotConfigData):
    """Lazy wrapper so importing the GraphQL schema does not require LangChain installed."""
    from app.services.chatbot.agent import build_llm as _build_llm

    return _build_llm(config)


def _chat_messages(system_prompt: str, user_prompt: str) -> list:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
    except ModuleNotFoundError:
        return [("system", system_prompt), ("human", user_prompt)]
    return [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()


def _parse_json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("La IA no devolvio JSON valido.") from exc
    if not isinstance(payload, dict):
        raise ValueError("La IA devolvio un formato inesperado.")
    return payload


def _str_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _build_system_prompt() -> str:
    return (
        "Eres un asistente interno que optimiza el *system prompt* de un chatbot de gimnasio "
        "que atiende clientes por WhatsApp. Devuelve SOLO JSON valido, sin Markdown ni texto "
        "fuera del JSON. Escribe en espanol claro y conciso.\n"
        "Objetivo principal: el system prompt NO debe repetir datos que el sistema ya inyecta "
        "en tiempo de ejecucion desde la base de datos y las herramientas: precios, planes, "
        "horarios, clases, sedes, direccion, telefono, politicas, instructores ni disponibilidad; "
        "tampoco las reglas de uso de herramientas, el flujo de compra, el flujo de confirmacion "
        "ni la fecha actual. Elimina esas partes porque duplican o contradicen el contexto en vivo.\n"
        "Conserva la persona/identidad del asistente, la intencion de tono, las reglas de "
        "comportamiento y escalamiento, y cualquier instruccion genuinamente adicional. No "
        "inventes datos nuevos. Se breve.\n"
        "Si el prompt ya esta limpio, devuelvelo casi igual y explica en warnings que no habia "
        "nada que quitar.\n"
        "El JSON debe tener exactamente estas llaves: optimizedPrompt (string con el prompt "
        "optimizado), removed (lista de frases o datos que quitaste), notes (lista de notas "
        "breves), warnings (lista de advertencias). removed, notes y warnings son listas; "
        "pueden ir vacias."
    )


def _build_user_prompt(request: PromptOptimizeRequestData, business_info: str) -> str:
    payload = {
        "currentSystemPrompt": request.system_prompt or "",
        "tone": request.tone or "",
        "instruction": request.instruction or "",
    }
    injected = business_info.strip() or "Sin datos de negocio configurados."
    return (
        "Optimiza el system prompt del chatbot.\n\n"
        f"Datos actuales:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Contexto que el sistema YA inyecta automaticamente y que el prompt NO debe repetir:\n\n"
        f"Informacion del negocio (ya inyectada):\n{injected}\n\n"
        f"{_TOOL_INVENTORY_ES}\n\n"
        f"{_FIXED_BLOCKS_ES}"
    )


def _validate_suggestion(payload: dict) -> PromptOptimizeSuggestionData:
    optimized = str(payload.get("optimizedPrompt") or "").strip()
    if not optimized:
        raise ValueError("La IA devolvio un prompt vacio.")
    return PromptOptimizeSuggestionData(
        optimized_prompt=optimized,
        removed=_str_list(payload.get("removed")),
        notes=_str_list(payload.get("notes")),
        warnings=_str_list(payload.get("warnings")),
    )


async def optimize_system_prompt(
    db: AsyncSession,
    request: PromptOptimizeRequestData,
) -> PromptOptimizeSuggestionData:
    """Rewrite the chatbot system prompt so it stops duplicating DB/tool-injected context."""
    if not chatbot_env.is_configured():
        raise ValueError("Falta ANTHROPIC_API_KEY para optimizar el system prompt.")
    if not (request.system_prompt or "").strip():
        raise ValueError("Escribe un system prompt antes de optimizarlo.")

    config = await chatbotConfigCrud.get_config(db) or _fallback_config()
    # Optimize the live editor text (may be unsaved), not the stored row.
    config.system_prompt = request.system_prompt
    config.tone = request.tone or config.tone

    business_info = await build_business_info(db, config)
    llm = build_llm(config)
    response = await llm.ainvoke(
        _chat_messages(
            _build_system_prompt(),
            _build_user_prompt(request, business_info),
        )
    )
    payload = _parse_json_object(_message_text(response))
    return _validate_suggestion(payload)
