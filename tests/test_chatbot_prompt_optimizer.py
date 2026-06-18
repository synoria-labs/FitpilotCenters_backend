import json
from types import SimpleNamespace

import pytest

from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.graphql.schema import schema
from app.services.chatbot import prompt_optimizer as service


class _FakeLlm:
    def __init__(self, payload):
        self.payload = payload

    async def ainvoke(self, _messages):
        return SimpleNamespace(content=json.dumps(self.payload))


def _config() -> ChatbotConfigData:
    return ChatbotConfigData(
        id=1,
        enabled=False,
        require_confirmation=True,
        require_mp_payment=False,
        model="claude-test",
        system_prompt="Eres el asistente. Los precios son $1000 y abrimos lunes 07:00.",
        business_name="FitPilot",
        address="",
        operating_hours="",
        phone="",
        policies="",
        tone="Breve y profesional",
        extra_info="",
        created_at=None,
        updated_at=None,
    )


@pytest.fixture
def ai_ready(monkeypatch):
    monkeypatch.setattr(service.chatbot_env.__class__, "ANTHROPIC_API_KEY", "test-key")

    async def fake_get_config(_db):
        return _config()

    async def fake_business_info(_db, _config_data):
        return "Nombre: FitPilot\nHorarios: Lunes 07:00"

    monkeypatch.setattr(service.chatbotConfigCrud, "get_config", fake_get_config)
    monkeypatch.setattr(service, "build_business_info", fake_business_info)


@pytest.mark.asyncio
async def test_optimize_returns_valid_suggestion(monkeypatch, ai_ready):
    payload = {
        "optimizedPrompt": "Eres el asistente de FitPilot. Responde breve y profesional.",
        "removed": ["precios $1000", "horario lunes 07:00"],
        "notes": ["Se conservó la persona y el tono."],
        "warnings": [],
    }
    monkeypatch.setattr(service, "build_llm", lambda _config: _FakeLlm(payload))

    result = await service.optimize_system_prompt(
        object(),
        service.PromptOptimizeRequestData(
            system_prompt="Eres el asistente. Los precios son $1000 y abrimos lunes 07:00.",
        ),
    )

    assert result.optimized_prompt == payload["optimizedPrompt"]
    assert result.removed == payload["removed"]
    assert result.notes == ["Se conservó la persona y el tono."]
    assert result.warnings == []


@pytest.mark.asyncio
async def test_empty_optimized_prompt_raises(monkeypatch, ai_ready):
    payload = {"optimizedPrompt": "", "removed": [], "notes": [], "warnings": []}
    monkeypatch.setattr(service, "build_llm", lambda _config: _FakeLlm(payload))

    with pytest.raises(ValueError, match="vacio"):
        await service.optimize_system_prompt(
            object(),
            service.PromptOptimizeRequestData(system_prompt="Algo que optimizar"),
        )


@pytest.mark.asyncio
async def test_empty_input_prompt_raises(ai_ready):
    with pytest.raises(ValueError, match="system prompt"):
        await service.optimize_system_prompt(
            object(),
            service.PromptOptimizeRequestData(system_prompt="   "),
        )


@pytest.mark.asyncio
async def test_missing_anthropic_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(service.chatbot_env.__class__, "ANTHROPIC_API_KEY", "")

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        await service.optimize_system_prompt(
            object(),
            service.PromptOptimizeRequestData(system_prompt="Eres el asistente."),
        )


@pytest.mark.asyncio
async def test_invalid_ai_json_returns_clear_error(monkeypatch, ai_ready):
    class BadLlm:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content="no es json")

    monkeypatch.setattr(service, "build_llm", lambda _config: BadLlm())

    with pytest.raises(ValueError, match="JSON valido"):
        await service.optimize_system_prompt(
            object(),
            service.PromptOptimizeRequestData(system_prompt="Eres el asistente."),
        )


@pytest.mark.asyncio
async def test_graphql_optimize_requires_authentication():
    mutation = """
        mutation {
            optimizeSystemPrompt(input: {systemPrompt: "Eres el asistente."}) {
                success
            }
        }
    """
    result = await schema.execute(
        mutation,
        context_value=SimpleNamespace(db=object(), user=None),
    )

    assert result.errors
    assert "Authentication required" in str(result.errors[0])


@pytest.mark.asyncio
async def test_graphql_optimize_returns_failure_without_writes(monkeypatch):
    async def fake_optimize(_db, _request):
        raise ValueError("Falta ANTHROPIC_API_KEY para optimizar el system prompt.")

    monkeypatch.setattr(service, "optimize_system_prompt", fake_optimize)

    mutation = """
        mutation {
            optimizeSystemPrompt(input: {systemPrompt: "Eres el asistente."}) {
                success
                error
                suggestion { optimizedPrompt }
            }
        }
    """
    result = await schema.execute(
        mutation,
        context_value=SimpleNamespace(db=object(), user=object()),
    )

    assert not result.errors
    payload = result.data["optimizeSystemPrompt"]
    assert payload["success"] is False
    assert "ANTHROPIC_API_KEY" in payload["error"]
    assert payload["suggestion"] is None
