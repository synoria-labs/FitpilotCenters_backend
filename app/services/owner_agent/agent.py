"""LangGraph ReAct runner for the owner/admin WhatsApp agent."""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.crud.ownerAgentCrud import OwnerAgentConfigData
from app.services.chatbot.timefmt import fmt_now_es
from app.services.owner_agent.env import owner_agent_env

logger = logging.getLogger(__name__)

_BASE_RULES = (
    "Eres un agente administrativo de FitPilot. Respondes solo al dueno o a administradores "
    "autorizados por telefono. Usa herramientas para obtener datos reales; no inventes numeros, "
    "ingresos, pagos, socios, horarios, disponibilidad ni estados. Responde en espanol, breve y "
    "con estructura clara. Si una herramienta devuelve ids, puedes citarlos."
)

_ACTION_RULES = (
    "Regla de acciones: cualquier cambio real debe pasar por propuesta y confirmacion. "
    "Para crear/completar/cancelar tareas o ejecutar barridos, llama primero a la herramienta "
    "propose_* correspondiente. Si ya hay una accion pendiente y el usuario confirma con 'si', "
    "llama a confirm_action. Si rechaza, llama a cancel_action. Nunca ejecutes acciones por texto "
    "sin herramienta."
)


def _extract_text(message: Any) -> str:
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


def build_system_prompt(
    config: OwnerAgentConfigData,
    *,
    pending_note: Optional[str] = None,
) -> str:
    parts = [
        _BASE_RULES,
        f"Fecha y hora actual: {fmt_now_es()} (America/Mexico_City). Usa esta fecha para interpretar hoy, manana, esta semana y este mes.",
        _ACTION_RULES,
    ]
    if config.system_prompt:
        parts.append("Instrucciones configuradas:\n" + config.system_prompt.strip())
    if pending_note:
        parts.append(pending_note)
    return "\n\n".join(parts)


async def run_agent(
    *,
    config: OwnerAgentConfigData,
    tools: list,
    history: list[tuple[str, str]],
    user_text: str,
    pending_note: Optional[str] = None,
) -> Optional[str]:
    """Run one owner-agent turn and return the text response."""
    try:
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.prebuilt import create_react_agent
    except Exception as exc:  # noqa: BLE001
        logger.exception("Owner agent LangChain imports failed")
        return (
            "El agente admin no puede iniciar porque faltan dependencias de LangChain/"
            f"LangGraph o Anthropic: {exc}"
        )

    if not owner_agent_env.ANTHROPIC_API_KEY:
        return "Falta ANTHROPIC_API_KEY en el backend para usar el agente admin."

    llm = ChatAnthropic(
        model=config.model or owner_agent_env.DEFAULT_MODEL,
        max_tokens=int(config.max_tokens or 1024),
        api_key=owner_agent_env.ANTHROPIC_API_KEY,
        timeout=60,
    )
    system_prompt = build_system_prompt(config, pending_note=pending_note)
    agent = create_react_agent(llm, tools, prompt=system_prompt)

    messages = []
    for role, text in history:
        clean = (text or "").strip()
        if not clean:
            continue
        if role == "inbound":
            messages.append(HumanMessage(content=clean))
        else:
            messages.append(AIMessage(content=clean))
    messages.append(HumanMessage(content=(user_text or "").strip()))

    result = await agent.ainvoke(
        {"messages": messages},
        config={"recursion_limit": int(owner_agent_env.RECURSION_LIMIT or 12)},
    )
    out_messages = result.get("messages", []) if isinstance(result, dict) else []
    for message in reversed(out_messages):
        if message.__class__.__name__ == "AIMessage":
            text = _extract_text(message)
            if text:
                return text
    return None
