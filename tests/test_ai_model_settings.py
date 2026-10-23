import json
from types import SimpleNamespace

import pytest

from app.core.ai_models import DEFAULT_AI_MODEL, normalize_model_id
from app.crud import chatbotConfigCrud, ownerAgentCrud
from app.graphql.schema import schema


async def test_models_roundtrip_independently_and_partial_saves_preserve_them(db):
    await chatbotConfigCrud.upsert_config(db, model=DEFAULT_AI_MODEL, template_ai_model="claude-haiku-4-5",
                                          prompt_optimizer_model="claude-opus-5-5", commit=False)
    await ownerAgentCrud.upsert_config(db, model="claude-sonnet-4-6", commit=False)
    await chatbotConfigCrud.upsert_config(db, tone="Breve", commit=False)
    config = await chatbotConfigCrud.get_config(db)
    owner = await ownerAgentCrud.get_config(db)
    assert config.model == DEFAULT_AI_MODEL
    assert config.template_ai_model == "claude-haiku-4-5"
    assert config.prompt_optimizer_model == "claude-opus-5-5"
    assert owner.model == "claude-sonnet-4-6"


async def test_graphql_saves_auxiliary_models_and_exposes_catalog(db):
    context = SimpleNamespace(db=db, user=SimpleNamespace(roles=[]), account_id=1)
    result = await schema.execute('''mutation {
        saveChatbotConfig(input: {model: "claude-sonnet-5-5", templateAiModel: "claude-haiku-4-5",
                                 promptOptimizerModel: "claude-opus-5-5"}) {
            success config { model templateAiModel promptOptimizerModel }
        }
    }''', context_value=context)
    assert not result.errors
    assert result.data["saveChatbotConfig"]["success"]
    result = await schema.execute("{ chatbotConfig { model templateAiModel promptOptimizerModel } aiModelOptions { id label } }",
                                  context_value=context)
    assert not result.errors
    assert result.data["chatbotConfig"]["templateAiModel"] == "claude-haiku-4-5"
    assert any(m["id"] == DEFAULT_AI_MODEL for m in result.data["aiModelOptions"])


async def test_catalog_requires_authentication():
    result = await schema.execute("{ aiModelOptions { id } }", context_value=SimpleNamespace(user=None))
    assert result.errors


@pytest.mark.parametrize("value", ["", "bad model", "gpt-5", "claude-" + "x" * 80])
def test_invalid_model_id_rejected(value):
    with pytest.raises(ValueError, match="modelo Claude"):
        normalize_model_id(value)


def test_sonnet_55_payload_uses_supported_options(monkeypatch):
    from app.core.chatbot_env import chatbot_env
    from app.services.chatbot.agent import build_llm
    from app.services.chatbot.prompt_optimizer import _fallback_config

    monkeypatch.setattr(chatbot_env, "ANTHROPIC_API_KEY", "test-key")
    config = _fallback_config()
    config.model = DEFAULT_AI_MODEL
    payload = build_llm(config)._get_request_payload([("human", "Hello")])
    assert payload["model"] == DEFAULT_AI_MODEL
    assert payload["thinking"] == {"type": "between_tools"}
    assert payload["output_config"] == {"effort": "low"}
    assert not any(k in payload for k in ("temperature", "top_p", "top_k", "tool_choice"))
    config.model = "claude-sonnet-4-6"
    payload = build_llm(config)._get_request_payload([("human", "Hello")])
    assert "thinking" not in payload and "output_config" not in payload


@pytest.mark.parametrize("feature", ["templates", "optimizer", "optimizer_preview"])
async def test_auxiliary_ai_uses_its_own_model_without_changing_chatbot(db, monkeypatch, feature):
    from app.services import whatsapp_template_ai_service as templates
    from app.services.chatbot import prompt_optimizer as optimizer

    await chatbotConfigCrud.upsert_config(db, model=DEFAULT_AI_MODEL, template_ai_model="claude-haiku-4-5",
                                          prompt_optimizer_model="claude-sonnet-4-6", commit=False)
    service = templates if feature == "templates" else optimizer
    monkeypatch.setattr(service.chatbot_env.__class__, "ANTHROPIC_API_KEY", "test-key")
    selected = []
    payload = {"bodyText": "Hola", "bodyExamples": [], "suggestedName": "hola", "suggestedCategory": "UTILITY"} if feature == "templates" else {"optimizedPrompt": "Responde breve."}

    class FakeLlm:
        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps(payload))

    def build(config):
        selected.append(config.model)
        return FakeLlm()

    async def context(*args):
        return "FitPilot"

    monkeypatch.setattr(service, "build_llm", build)
    if feature == "templates":
        monkeypatch.setattr(service, "build_template_writer_context", context)
        await templates.assist_whatsapp_template(db, templates.TemplateAiRequestData(action="DRAFT", instruction="Saluda"))
        expected = "claude-haiku-4-5"
    else:
        monkeypatch.setattr(service, "build_business_info", context)
        expected = "claude-opus-5-5" if feature == "optimizer_preview" else "claude-sonnet-4-6"
        await optimizer.optimize_system_prompt(db, optimizer.PromptOptimizeRequestData(
            system_prompt="Responde bien.", model=expected if feature == "optimizer_preview" else None,
        ))
    assert selected == [expected]
    config = await chatbotConfigCrud.get_config(db)
    assert config.model == DEFAULT_AI_MODEL and config.prompt_optimizer_model == "claude-sonnet-4-6"


async def test_admin_agent_uses_its_selected_model_with_sonnet_options(db, monkeypatch):
    import langchain_anthropic
    import langgraph.prebuilt
    from langchain_core.messages import AIMessage
    from app.services.owner_agent import agent as service

    await ownerAgentCrud.upsert_config(db, model=DEFAULT_AI_MODEL, commit=False)
    config = await ownerAgentCrud.get_config(db)
    monkeypatch.setattr(service.owner_agent_env, "ANTHROPIC_API_KEY", "test-key")
    received = {}

    def llm(**kwargs):
        received.update(kwargs)
        return object()

    class Agent:
        async def ainvoke(self, *args, **kwargs):
            return {"messages": [AIMessage(content="OK")]}

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", llm)
    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", lambda *a, **kw: Agent())
    assert await service.run_agent(config=config, tools=[], history=[], user_text="Hola") == "OK"
    assert received["model"] == DEFAULT_AI_MODEL
    assert received["thinking"]["type"] == "between_tools"
