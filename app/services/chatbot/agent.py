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

logger = logging.getLogger(__name__)

# Cap on the ReAct loop so a misbehaving turn can't spin forever.
_RECURSION_LIMIT = 12

_TOOL_RULES = (
    "Usa siempre las herramientas para obtener datos reales (precios, horarios, clases, "
    "disponibilidad, estado de membresía). Nunca inventes esa información."
)

_CONFIRM_RULES_STRICT = (
    "Para reservar una clase, registrar un pago, renovar una membresía o inscribir a alguien: "
    "primero llama a la herramienta propose_* correspondiente, luego repite el resumen al "
    "cliente y pídele que confirme respondiendo 'sí'. SOLO cuando el cliente confirme, llama a "
    "confirm_action. Si el cliente dice que no o cambia de idea, llama a cancel_action. Nunca "
    "confirmes una acción sin que el cliente lo haya pedido explícitamente."
)

_CONFIRM_RULES_RELAXED = (
    "Para reservar, pagar, renovar o inscribir: llama primero a la herramienta propose_* "
    "correspondiente para validar, y luego a confirm_action para ejecutarla. Aun así, deja "
    "claro al cliente qué acción realizaste."
)


def build_llm(config: ChatbotConfigData) -> ChatAnthropic:
    model = config.model or chatbot_env.MODEL
    return ChatAnthropic(
        model=model,
        max_tokens=chatbot_env.MAX_TOKENS,
        api_key=chatbot_env.ANTHROPIC_API_KEY,
        timeout=60,
    )


def build_system_prompt(
    config: ChatbotConfigData, business_info: str, member_id: Optional[int]
) -> str:
    parts: List[str] = []
    if config.system_prompt:
        parts.append(config.system_prompt.strip())
    if config.tone:
        parts.append(f"Tono: {config.tone}.")
    parts.append(_TOOL_RULES)
    parts.append(_CONFIRM_RULES_STRICT if config.require_confirmation else _CONFIRM_RULES_RELAXED)
    if member_id is not None:
        parts.append(
            "El cliente está identificado como socio. Usa get_my_membership y "
            "list_my_reservations para sus datos; las herramientas ya operan sobre su cuenta."
        )
    else:
        parts.append(
            "El cliente NO está registrado como socio. Puedes darle información y, si quiere "
            "inscribirse, pídele su nombre completo y usa propose_enrollment."
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
) -> Optional[str]:
    """Run one agent turn and return the reply text (or None if nothing to say)."""
    llm = build_llm(config)
    system_prompt = build_system_prompt(config, business_info, member_id)
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
