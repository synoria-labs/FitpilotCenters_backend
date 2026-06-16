"""Anthropic-powered writing assistant for WhatsApp templates.

This service is read-only: it generates copy suggestions for the desktop editor, but it
does not persist templates and never calls Meta. The existing template create/update
mutations remain the only path that writes local state or submits templates for review.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.chatbot_env import chatbot_env
from app.crud import chatbotConfigCrud
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.services.chatbot.business_context import build_template_writer_context
from app.services.whatsapp_template_components import build_components, placeholder_count

ACTION_DRAFT = "DRAFT"
ACTION_OPTIMIZE = "OPTIMIZE"
ACTION_CORRECT = "CORRECT"
_ACTIONS = {ACTION_DRAFT, ACTION_OPTIMIZE, ACTION_CORRECT}
_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


@dataclass
class TemplateAiRequestData:
    action: str
    body_text: str = ""
    body_examples: Optional[List[str]] = None
    footer_text: Optional[str] = None
    template_name: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None
    instruction: Optional[str] = None


@dataclass
class TemplateAiSuggestionData:
    body_text: str
    body_examples: List[str]
    footer_text: Optional[str]
    suggested_name: Optional[str]
    suggested_category: Optional[str]
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
    """Lazy wrapper so importing GraphQL schema does not require LangChain installed."""
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


def _placeholder_sequence(text: str) -> List[int]:
    return [int(match) for match in _PLACEHOLDER_RE.findall(text or "")]


def _normalize_examples(
    *,
    body_text: str,
    suggested_examples: List[str],
    current_examples: Optional[List[str]],
) -> List[str]:
    count = placeholder_count(body_text)
    if count <= 0:
        return []
    current = [str(value).strip() for value in current_examples or []]
    values: List[str] = []
    for index in range(count):
        value = suggested_examples[index] if index < len(suggested_examples) else ""
        if not value and index < len(current):
            value = current[index]
        values.append(value or f"ejemplo{index + 1}")
    return values


def _suggested_category(value: Any, fallback: Optional[str]) -> str:
    candidate = str(value or fallback or "UTILITY").strip().upper()
    return candidate if candidate in _CATEGORIES else "UTILITY"


def _build_system_prompt(action: str, current_placeholders: List[int]) -> str:
    preserve_rule = ""
    if action in {ACTION_OPTIMIZE, ACTION_CORRECT}:
        preserve_rule = (
            "Para OPTIMIZE y CORRECT conserva exactamente la misma secuencia de placeholders "
            f"del BODY actual: {current_placeholders}. No agregues, elimines ni renumeres placeholders."
        )
    return (
        "Eres un asistente interno para redactar plantillas de WhatsApp Business de un gimnasio. "
        "Devuelve SOLO JSON valido, sin Markdown ni explicaciones fuera del JSON. "
        "Escribe en espanol mexicano, con tono breve, claro, natural y profesional. "
        "No inventes precios, promociones, horarios, sedes, instructores ni disponibilidad; usa solo "
        "el contexto proporcionado o redacta de forma generica. "
        "El JSON debe tener estas llaves exactas: bodyText, bodyExamples, footerText, "
        "suggestedName, suggestedCategory, notes, warnings. "
        "bodyExamples debe ser una lista con valores de ejemplo para {{1}}, {{2}}, etc. "
        "footerText, suggestedName y suggestedCategory pueden ser null. notes y warnings son listas. "
        f"{preserve_rule}"
    )


def _build_user_prompt(
    *,
    request: TemplateAiRequestData,
    business_context: str,
) -> str:
    payload = {
        "action": request.action,
        "instruction": request.instruction or "",
        "templateName": request.template_name or "",
        "category": request.category or "UTILITY",
        "language": request.language or "es_MX",
        "bodyText": request.body_text or "",
        "bodyExamples": request.body_examples or [],
        "footerText": request.footer_text or "",
    }
    return (
        "Genera una sugerencia para el editor de plantillas WhatsApp.\n\n"
        f"Datos actuales:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"Contexto de negocio de solo lectura:\n{business_context or 'Sin contexto configurado.'}"
    )


def _validate_suggestion(
    *,
    request: TemplateAiRequestData,
    payload: dict,
) -> TemplateAiSuggestionData:
    body_text = str(payload.get("bodyText") or "").strip()
    if not body_text:
        raise ValueError("La IA devolvio una sugerencia sin cuerpo.")

    action = request.action
    current_sequence = _placeholder_sequence(request.body_text)
    suggested_sequence = _placeholder_sequence(body_text)
    if action in {ACTION_OPTIMIZE, ACTION_CORRECT} and current_sequence != suggested_sequence:
        raise ValueError("La IA cambio los placeholders de la plantilla. Intenta de nuevo.")

    body_examples = _normalize_examples(
        body_text=body_text,
        suggested_examples=_str_list(payload.get("bodyExamples")),
        current_examples=request.body_examples,
    )
    footer_raw = payload.get("footerText")
    footer_text = str(footer_raw).strip() if footer_raw is not None else None
    footer_text = footer_text or None

    # Reuse the canonical component builder so placeholder examples stay Meta-compatible.
    build_components(body_text, body_examples, footer_text)

    return TemplateAiSuggestionData(
        body_text=body_text,
        body_examples=body_examples,
        footer_text=footer_text,
        suggested_name=str(payload.get("suggestedName") or "").strip() or None,
        suggested_category=_suggested_category(payload.get("suggestedCategory"), request.category),
        notes=_str_list(payload.get("notes")),
        warnings=_str_list(payload.get("warnings")),
    )


async def assist_whatsapp_template(
    db: AsyncSession,
    request: TemplateAiRequestData,
) -> TemplateAiSuggestionData:
    """Generate a WhatsApp template writing suggestion with Anthropic."""
    request.action = (request.action or "").strip().upper()
    if request.action not in _ACTIONS:
        raise ValueError("Accion de IA no soportada.")
    if not chatbot_env.is_configured():
        raise ValueError("Falta ANTHROPIC_API_KEY para usar el asistente de plantillas.")
    if request.action in {ACTION_OPTIMIZE, ACTION_CORRECT} and not (request.body_text or "").strip():
        raise ValueError("Escribe un cuerpo de plantilla antes de optimizar o corregir.")
    if request.action == ACTION_DRAFT and not (request.body_text or request.instruction or "").strip():
        raise ValueError("Describe que debe redactar la IA.")

    config = await chatbotConfigCrud.get_config(db) or _fallback_config()
    business_context = await build_template_writer_context(db, config)
    current_placeholders = _placeholder_sequence(request.body_text)
    llm = build_llm(config)
    response = await llm.ainvoke(
        _chat_messages(
            _build_system_prompt(request.action, current_placeholders),
            _build_user_prompt(request=request, business_context=business_context),
        )
    )
    payload = _parse_json_object(_message_text(response))
    return _validate_suggestion(request=request, payload=payload)
