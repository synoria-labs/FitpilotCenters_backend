"""Shared model IDs and request options for every Anthropic integration."""
import re

DEFAULT_AI_MODEL = "claude-sonnet-5-5"
AI_MODEL_OPTIONS = (
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
    ("claude-haiku-4-5", "Claude Haiku 4.5"),
)


def normalize_model_id(value):
    model = str(value or "").strip()
    if not re.fullmatch(r"claude-[a-zA-Z0-9._:-]{1,73}", model):
        raise ValueError("Indica un identificador de modelo Claude válido (máximo 80 caracteres).")
    return model


def model_request_options(model):
    # Sonnet 5.5 defaults to adaptive thinking. Use the documented low-latency setting
    # within the existing chat output budget; never send temperature or forced tool choice.
    if model == "claude-sonnet-5-5":
        return {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}
    return {}
