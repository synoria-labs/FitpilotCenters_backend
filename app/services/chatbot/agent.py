"""LangGraph ReAct agent for the FitPilot WhatsApp chatbot.

Builds a ``ChatAnthropic`` model (latest Sonnet by default) and a prebuilt ReAct agent over
the per-turn tools. The system prompt is assembled each turn from the DB config (persona +
business info) plus the propose-and-confirm rules and the identity note.

The Anthropic API key is read from the environment by ``ChatAnthropic`` — never hardcoded.
"""
import logging
from typing import List, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from app.core.chatbot_env import chatbot_env
from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.services.chatbot.timefmt import fmt_now_es

logger = logging.getLogger(__name__)

# Cap on the ReAct loop so a misbehaving turn can't spin forever.
_RECURSION_LIMIT = 12

_TOOL_RULES = (
    "Usa siempre las herramientas para obtener datos reales y nunca inventes esa información. "
    "En particular: para el horario de clases usa get_weekly_schedule; para la ubicación o "
    "dirección usa get_venues; para los instructores usa list_instructors; para precios "
    "get_membership_plans; para cupo y disponibilidad list_available_classes y "
    "check_class_availability. Prefiere estos datos en vivo sobre cualquier texto fijo."
)

_PURCHASE_RULES = (
    "Para que un cliente asista hay que COMPRAR un plan (no existe reserva gratis):\n"
    "- PAQUETE (plan de horario fijo): usa propose_membership(plan_id, template_id, full_name si es "
    "cliente nuevo). Muestra los planes con get_membership_plans y los horarios con "
    "get_weekly_schedule (cada horario tiene un id=template). Reserva automáticamente todo el periodo. "
    "Si el cliente ya es socio, propose_membership renueva.\n"
    "- PASE DIARIO (1 día): usa propose_day_pass(plan_id, session_id, full_name si es nuevo). Muestra "
    "el plan diario con get_membership_plans y las clases con list_available_classes (cada clase tiene "
    "un id=session). Reserva ese día.\n"
    "El asiento/bici se asigna automáticamente; no le pidas el número al cliente."
)

_CONFIRM_RULES_STRICT = (
    "Flujo de compra: primero llama a la tool propose_* correspondiente, repite el resumen y pide "
    "confirmación. Si la tool devuelve un LINK de pago de MercadoPago, reenvíaselo al cliente y NO "
    "confirmes por texto (se confirma solo al acreditarse el pago). Si NO hay link, cuando el cliente "
    "diga 'sí' llama a confirm_action. Si rechaza, cancel_action. IMPORTANTE: si ya existe una acción "
    "pendiente, NO la vuelvas a proponer; confírmala o cancélala según el cliente."
)

_CONFIRM_RULES_RELAXED = _CONFIRM_RULES_STRICT


def build_llm(config: ChatbotConfigData) -> ChatAnthropic:
    model = config.model or chatbot_env.MODEL
    return ChatAnthropic(
        model=model,
        max_tokens=chatbot_env.MAX_TOKENS,
        api_key=chatbot_env.ANTHROPIC_API_KEY,
        timeout=60,
    )


def build_system_prompt(
    config: ChatbotConfigData,
    business_info: str,
    member_id: Optional[int],
    pending_note: Optional[str] = None,
) -> str:
    parts: List[str] = []
    if config.system_prompt:
        parts.append(config.system_prompt.strip())
    if config.tone:
        parts.append(f"Tono: {config.tone}.")
    parts.append(
        f"Fecha y hora actual: {fmt_now_es()} (hora de México, America/Mexico_City). "
        "Úsala para interpretar 'hoy', 'mañana', 'esta semana' y los días de la semana; no asumas "
        "otra fecha. Las fechas que devuelven las herramientas ya vienen en hora de México con su "
        "día de la semana — repítelas tal cual, no recalcules el día."
    )
    if pending_note:
        parts.append(pending_note)
    parts.append(_TOOL_RULES)
    parts.append(_PURCHASE_RULES)
    parts.append(_CONFIRM_RULES_STRICT if config.require_confirmation else _CONFIRM_RULES_RELAXED)
    if member_id is not None:
        parts.append(
            "El cliente está identificado como socio. get_my_membership, list_my_reservations, "
            "renovación (propose_membership) y pase diario ya operan sobre su cuenta."
        )
    else:
        parts.append(
            "El cliente NO está registrado como socio. Para comprar un plan pídele su nombre "
            "completo y úsalo como full_name en propose_membership / propose_day_pass."
        )
    if business_info:
        parts.append("Información del negocio:\n" + business_info)
    return "\n\n".join(p for p in parts if p)


def _extract_text(message: BaseMessage) -> str:
    """Get plain text out of an AIMessage whose content may be a string or block list."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(t for t in texts if t).strip()
    return str(content).strip()


async def run_agent(
    config: ChatbotConfigData,
    tools: list,
    business_info: str,
    member_id: Optional[int],
    history: List[BaseMessage],
    user_text: str,
    pending_note: Optional[str] = None,
) -> Optional[str]:
    """Run one agent turn and return the reply text (or None if nothing to say)."""
    llm = build_llm(config)
    system_prompt = build_system_prompt(config, business_info, member_id, pending_note)
    agent = create_react_agent(llm, tools, prompt=system_prompt)

    messages: List[BaseMessage] = list(history) + [HumanMessage(content=user_text)]
    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": _RECURSION_LIMIT},
    )
    out_messages = result.get("messages", []) if isinstance(result, dict) else []
    for message in reversed(out_messages):
        if isinstance(message, AIMessage):
            text = _extract_text(message)
            if text:
                return text
    return None
