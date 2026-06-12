import pytest

from app.services.whatsapp_cloud_service import (
    WhatsAppError,
    _template_send_components,
)
from app.services.whatsapp_template_components import (
    render_template_text,
    required_header_media_format,
)


def test_template_payload_includes_media_header_and_body_params():
    components = [
        {
            "type": "HEADER",
            "format": "IMAGE",
            "example": {"header_handle": ["https://example.com/header.png"]},
        },
        {
            "type": "BODY",
            "text": "Hola {{1}}, vence el {{2}}",
            "example": {"body_text": [["Alejandro", "22 de abril"]]},
        },
    ]

    payload = _template_send_components(
        components,
        ["Estefania", "30 de junio"],
        header_media_url="https://cdn.example.com/renewal.png",
    )

    assert payload == [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "image",
                    "image": {"link": "https://cdn.example.com/renewal.png"},
                }
            ],
        },
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Estefania"},
                {"type": "text", "text": "30 de junio"},
            ],
        },
    ]


def test_required_header_media_format_detects_only_media_headers():
    assert required_header_media_format([{"type": "HEADER", "format": "IMAGE"}]) == "IMAGE"
    assert required_header_media_format([{"type": "HEADER", "format": "TEXT"}]) is None
    assert required_header_media_format([{"type": "BODY", "text": "Hola"}]) is None


def test_template_payload_uses_template_examples_as_send_defaults():
    components = [
        {
            "type": "BODY",
            "text": "Hola {{1}}",
            "example": {"body_text": [["Alejandro"]]},
        }
    ]

    assert _template_send_components(components, []) == [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Alejandro"}],
        }
    ]


def test_template_payload_requires_media_header_source():
    components = [{"type": "HEADER", "format": "VIDEO"}, {"type": "BODY", "text": "Hola"}]

    with pytest.raises(WhatsAppError, match="encabezado de media"):
        _template_send_components(components, [])


def test_template_payload_does_not_use_header_handle_as_send_media():
    components = [
        {
            "type": "HEADER",
            "format": "IMAGE",
            "example": {"header_handle": ["4:meta-sample-handle"]},
        },
        {"type": "BODY", "text": "Hola"},
    ]

    with pytest.raises(WhatsAppError, match="encabezado de media"):
        _template_send_components(components, [])


def test_template_payload_rejects_missing_body_params():
    components = [{"type": "BODY", "text": "Hola {{1}} {{2}}"}]

    with pytest.raises(WhatsAppError, match="requiere 2"):
        _template_send_components(components, ["Alejandro"])


def test_render_template_text_uses_sent_params_and_footer():
    components = [
        {
            "type": "BODY",
            "text": "Hola {{1}}, vence el {{2}}",
            "example": {"body_text": [["Alejandro", "22 de abril"]]},
        },
        {"type": "FOOTER", "text": "Love Fitness"},
    ]

    assert (
        render_template_text(components, ["Estefania", "30 de junio"])
        == "Hola Estefania, vence el 30 de junio\n\nLove Fitness"
    )


def test_render_template_text_falls_back_to_examples():
    components = [
        {
            "type": "BODY",
            "text": "Hola {{1}}",
            "example": {"body_text": [["Alejandro"]]},
        }
    ]

    assert render_template_text(components, []) == "Hola Alejandro"
