import json
from types import SimpleNamespace

import pytest

from app.crud.chatbotConfigCrud import ChatbotConfigData
from app.graphql.schema import schema
from app.services import whatsapp_template_ai_service as service


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
        system_prompt="Usa datos reales.",
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

    async def fake_context(_db, _config_data):
        return "Planes: Mensual $1000\nHorario: Lunes 07:00"

    monkeypatch.setattr(service.chatbotConfigCrud, "get_config", fake_get_config)
    monkeypatch.setattr(service, "build_template_writer_context", fake_context)


@pytest.mark.asyncio
async def test_draft_generates_valid_template_suggestion(monkeypatch, ai_ready):
    payload = {
        "bodyText": "Hola {{1}}, tu clase en FitPilot te espera.",
        "bodyExamples": ["Ana"],
        "footerText": "FitPilot",
        "suggestedName": "recordatorio_clase",
        "suggestedCategory": "UTILITY",
        "notes": ["Mensaje breve."],
        "warnings": [],
    }
    monkeypatch.setattr(service, "build_llm", lambda _config: _FakeLlm(payload))

    result = await service.assist_whatsapp_template(
        object(),
        service.TemplateAiRequestData(action="DRAFT", instruction="Recordatorio de clase"),
    )

    assert result.body_text == payload["bodyText"]
    assert result.body_examples == ["Ana"]
    assert result.footer_text == "FitPilot"
    assert result.suggested_category == "UTILITY"


@pytest.mark.asyncio
async def test_optimize_preserves_existing_placeholders(monkeypatch, ai_ready):
    payload = {
        "bodyText": "Hola {{1}}, tu plan {{2}} ya esta activo. Te esperamos.",
        "bodyExamples": ["Ana", "Mensual"],
        "footerText": None,
        "suggestedName": None,
        "suggestedCategory": "UTILITY",
        "notes": [],
        "warnings": [],
    }
    monkeypatch.setattr(service, "build_llm", lambda _config: _FakeLlm(payload))

    result = await service.assist_whatsapp_template(
        object(),
        service.TemplateAiRequestData(
            action="OPTIMIZE",
            body_text="Hola {{1}}, tu plan {{2}} esta activo",
            body_examples=["Ana", "Mensual"],
        ),
    )

    assert result.body_text == payload["bodyText"]
    assert result.body_examples == ["Ana", "Mensual"]


@pytest.mark.asyncio
async def test_correct_rejects_changed_placeholders(monkeypatch, ai_ready):
    payload = {
        "bodyText": "Hola {{1}}, tu plan ya esta activo.",
        "bodyExamples": ["Ana"],
        "footerText": None,
        "suggestedName": None,
        "suggestedCategory": "UTILITY",
        "notes": [],
        "warnings": [],
    }
    monkeypatch.setattr(service, "build_llm", lambda _config: _FakeLlm(payload))

    with pytest.raises(ValueError, match="placeholders"):
        await service.assist_whatsapp_template(
            object(),
            service.TemplateAiRequestData(
                action="CORRECT",
                body_text="Hola {{1}}, tu plan {{2}} esta activo",
                body_examples=["Ana", "Mensual"],
            ),
        )


@pytest.mark.asyncio
async def test_missing_anthropic_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(service.chatbot_env.__class__, "ANTHROPIC_API_KEY", "")

    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        await service.assist_whatsapp_template(
            object(),
            service.TemplateAiRequestData(action="DRAFT", instruction="Bienvenida"),
        )


@pytest.mark.asyncio
async def test_invalid_ai_json_returns_clear_error(monkeypatch, ai_ready):
    class BadLlm:
        async def ainvoke(self, _messages):
            return SimpleNamespace(content="no es json")

    monkeypatch.setattr(service, "build_llm", lambda _config: BadLlm())

    with pytest.raises(ValueError, match="JSON valido"):
        await service.assist_whatsapp_template(
            object(),
            service.TemplateAiRequestData(action="DRAFT", instruction="Bienvenida"),
        )


@pytest.mark.asyncio
async def test_graphql_assist_template_requires_authentication():
    mutation = """
        mutation {
            assistWhatsappTemplate(input: {action: DRAFT, instruction: "Bienvenida"}) {
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
async def test_graphql_assist_template_returns_failure_without_writes(monkeypatch):
    async def fake_assist(_db, _request):
        raise ValueError("Falta ANTHROPIC_API_KEY para usar el asistente de plantillas.")

    monkeypatch.setattr(service, "assist_whatsapp_template", fake_assist)

    mutation = """
        mutation {
            assistWhatsappTemplate(input: {action: DRAFT, instruction: "Bienvenida"}) {
                success
                error
                suggestion { bodyText }
            }
        }
    """
    result = await schema.execute(
        mutation,
        context_value=SimpleNamespace(db=object(), user=object()),
    )

    assert not result.errors
    payload = result.data["assistWhatsappTemplate"]
    assert payload["success"] is False
    assert "ANTHROPIC_API_KEY" in payload["error"]
    assert payload["suggestion"] is None
