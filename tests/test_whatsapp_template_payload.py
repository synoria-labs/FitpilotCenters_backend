import pytest

from app.services.whatsapp_cloud_service import (
    WhatsAppError,
    _template_send_components,
)
from app.services.whatsapp_template_components import (
    build_components,
    buttons_from_components,
    carousel_cards_from_components,
    dynamic_url_button_index,
    embed_carousel_card_assets,
    header_text_var_count,
    render_template_text,
    required_header_kind,
    required_header_media_format,
    strip_local_metadata,
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


# --- build_components: new components ------------------------------------------------


def test_build_components_text_header_without_variable():
    components = build_components("Cuerpo", header_format="TEXT", header_text="Bienvenido")
    assert components == [
        {"type": "HEADER", "format": "TEXT", "text": "Bienvenido"},
        {"type": "BODY", "text": "Cuerpo"},
    ]


def test_build_components_text_header_with_variable_requires_example():
    components = build_components(
        "Cuerpo",
        header_format="TEXT",
        header_text="Hola {{1}}",
        header_text_example="Ana",
    )
    assert components[0] == {
        "type": "HEADER",
        "format": "TEXT",
        "text": "Hola {{1}}",
        "example": {"header_text": ["Ana"]},
    }


def test_build_components_text_header_rejects_two_variables():
    with pytest.raises(ValueError, match="una variable"):
        build_components("x", header_format="TEXT", header_text="{{1}} {{2}}")


def test_build_components_location_header():
    components = build_components("Visítanos", header_format="LOCATION")
    assert components[0] == {"type": "HEADER", "format": "LOCATION"}


def test_build_components_buttons_mixed_with_dynamic_url():
    buttons = [
        {"type": "QUICK_REPLY", "text": "Sí"},
        {"type": "URL", "text": "Ver", "url": "https://fit.com/{{1}}", "example": "promo"},
        {"type": "PHONE_NUMBER", "text": "Llamar", "phone_number": "+5215555555555"},
    ]
    components = build_components("Cuerpo", buttons=buttons)
    buttons_component = components[-1]
    assert buttons_component == {
        "type": "BUTTONS",
        "buttons": [
            {"type": "QUICK_REPLY", "text": "Sí"},
            {"type": "URL", "text": "Ver", "url": "https://fit.com/{{1}}", "example": ["promo"]},
            {"type": "PHONE_NUMBER", "text": "Llamar", "phone_number": "+5215555555555"},
        ],
    }


def test_build_components_buttons_reject_two_dynamic_urls():
    buttons = [
        {"type": "URL", "text": "A", "url": "https://a.com/{{1}}", "example": "x"},
        {"type": "URL", "text": "B", "url": "https://b.com/{{1}}", "example": "y"},
    ]
    with pytest.raises(ValueError, match="una URL dinámica"):
        build_components("Cuerpo", buttons=buttons)


def test_build_components_copy_code_button():
    components = build_components(
        "Usa este cupón",
        buttons=[
            {
                "type": "COPY_CODE",
                "text": "Copiar código",
                "offer_code": "FIT20",
            }
        ],
    )
    assert components[-1] == {
        "type": "BUTTONS",
        "buttons": [{"type": "COPY_CODE", "example": "FIT20"}],
    }


def test_build_components_copy_code_requires_code():
    with pytest.raises(ValueError, match="código de oferta"):
        build_components(
            "Usa este cupón",
            buttons=[{"type": "COPY_CODE", "text": "Copiar código"}],
        )


def test_build_components_copy_code_rejects_multiple():
    buttons = [
        {"type": "COPY_CODE", "text": "Copiar A", "offer_code": "A"},
        {"type": "COPY_CODE", "text": "Copiar B", "offer_code": "B"},
    ]
    with pytest.raises(ValueError, match="código de oferta"):
        build_components("Cuerpo", buttons=buttons)


def test_build_components_rejects_voice_call_button():
    with pytest.raises(ValueError, match="VOICE_CALL"):
        build_components(
            "Cuerpo",
            buttons=[{"type": "VOICE_CALL", "text": "Llamar en WhatsApp"}],
        )


def test_build_components_carousel_with_per_card_handles():
    cards = [
        {"header_format": "IMAGE", "header_handle": "h1", "body_text": "Tarjeta {{1}}", "body_examples": ["A"]},
        {"header_format": "IMAGE", "header_handle": "h2", "body_text": "Segunda"},
    ]
    components = build_components("Mira", carousel_cards=cards)
    assert components[0] == {"type": "BODY", "text": "Mira"}
    carousel = components[1]
    assert carousel["type"] == "CAROUSEL"
    assert len(carousel["cards"]) == 2
    first_header = carousel["cards"][0]["components"][0]
    assert first_header == {
        "type": "HEADER",
        "format": "IMAGE",
        "example": {"header_handle": ["h1"]},
    }


def test_build_components_carousel_rejects_mixed_formats():
    cards = [
        {"header_format": "IMAGE", "header_handle": "h1", "body_text": "a"},
        {"header_format": "VIDEO", "header_handle": "h2", "body_text": "b"},
    ]
    with pytest.raises(ValueError, match="mismo formato"):
        build_components("x", carousel_cards=cards)


def test_embed_and_strip_carousel_card_assets_roundtrip():
    cards = [
        {"header_format": "IMAGE", "header_handle": "h1", "body_text": "a"},
        {"header_format": "IMAGE", "header_handle": "h2", "body_text": "b"},
    ]
    clean = build_components("x", carousel_cards=cards)
    enriched = embed_carousel_card_assets(clean, [11, 22])
    enriched_cards = carousel_cards_from_components(enriched)
    assert enriched_cards[0]["components"][0]["fitpilot_asset_id"] == 11
    assert enriched_cards[1]["components"][0]["fitpilot_asset_id"] == 22
    # The Meta payload must not carry the FitPilot-only key.
    stripped = strip_local_metadata(enriched)
    assert "fitpilot_asset_id" not in carousel_cards_from_components(stripped)[0]["components"][0]


# --- helpers ------------------------------------------------------------------------


def test_required_header_kind_covers_all_header_types():
    assert required_header_kind([{"type": "HEADER", "format": "IMAGE"}]) == "IMAGE"
    assert required_header_kind([{"type": "HEADER", "format": "TEXT"}]) == "TEXT"
    assert required_header_kind([{"type": "HEADER", "format": "LOCATION"}]) == "LOCATION"
    assert required_header_kind([{"type": "BODY", "text": "x"}]) is None
    # required_header_media_format stays media-only (back-compat).
    assert required_header_media_format([{"type": "HEADER", "format": "TEXT"}]) is None


def test_header_text_var_count_and_dynamic_url_index():
    components = build_components(
        "Cuerpo",
        header_format="TEXT",
        header_text="Hola {{1}}",
        header_text_example="Ana",
        buttons=[
            {"type": "QUICK_REPLY", "text": "No"},
            {"type": "URL", "text": "Ver", "url": "https://x.com/{{1}}", "example": "p"},
        ],
    )
    assert header_text_var_count(components) == 1
    assert dynamic_url_button_index(buttons_from_components(components)) == 1


# --- _template_send_components: new components ---------------------------------------


def test_send_text_header_param_and_example_fallback():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Hola {{1}}", "example": {"header_text": ["Ana"]}},
        {"type": "BODY", "text": "Cuerpo"},
    ]
    assert _template_send_components(components, None, header_text_param="Pedro") == [
        {"type": "header", "parameters": [{"type": "text", "text": "Pedro"}]}
    ]
    # Falls back to the stored example when no runtime value is supplied.
    assert _template_send_components(components, None) == [
        {"type": "header", "parameters": [{"type": "text", "text": "Ana"}]}
    ]


def test_send_static_text_header_emits_nothing():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Bienvenido"},
        {"type": "BODY", "text": "Cuerpo"},
    ]
    assert _template_send_components(components, None) == []


def test_send_location_header_param_and_missing_error():
    components = [{"type": "HEADER", "format": "LOCATION"}, {"type": "BODY", "text": "x"}]
    out = _template_send_components(
        components,
        None,
        location={"latitude": "19.4", "longitude": "-99.1", "name": "Gym", "address": "Calle 1"},
    )
    assert out == [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "location",
                    "location": {
                        "latitude": "19.4",
                        "longitude": "-99.1",
                        "name": "Gym",
                        "address": "Calle 1",
                    },
                }
            ],
        }
    ]
    with pytest.raises(WhatsAppError, match="ubicación"):
        _template_send_components(components, None)


def test_send_dynamic_url_button_param_and_fallback():
    components = [
        {"type": "BODY", "text": "x"},
        {
            "type": "BUTTONS",
            "buttons": [
                {"type": "QUICK_REPLY", "text": "No"},
                {"type": "URL", "text": "Ver", "url": "https://x.com/{{1}}", "example": ["base"]},
            ],
        },
    ]
    assert _template_send_components(components, None, button_url_param="promo123") == [
        {"type": "button", "sub_type": "url", "index": "1", "parameters": [{"type": "text", "text": "promo123"}]}
    ]
    # Falls back to the stored example suffix.
    assert _template_send_components(components, None) == [
        {"type": "button", "sub_type": "url", "index": "1", "parameters": [{"type": "text", "text": "base"}]}
    ]


def test_send_static_buttons_emit_nothing():
    components = [
        {"type": "BODY", "text": "x"},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Sí"}]},
    ]
    assert _template_send_components(components, None) == []


def test_send_carousel_per_card_media_and_params():
    components = [
        {"type": "BODY", "text": "x"},
        {
            "type": "CAROUSEL",
            "cards": [
                {
                    "components": [
                        {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["h1"]}, "fitpilot_asset_id": 5},
                        {"type": "BODY", "text": "Tarjeta {{1}}"},
                    ]
                }
            ],
        },
    ]
    runtime = [{"media_url": "https://cdn/a.png", "media_id": None, "body_params": ["X"], "button_url_param": None}]
    out = _template_send_components(components, None, carousel_cards=runtime)
    assert out == [
        {
            "type": "carousel",
            "cards": [
                {
                    "card_index": 0,
                    "components": [
                        {"type": "header", "parameters": [{"type": "image", "image": {"link": "https://cdn/a.png"}}]},
                        {"type": "body", "parameters": [{"type": "text", "text": "X"}]},
                    ],
                }
            ],
        }
    ]


def test_send_carousel_requires_card_media():
    components = [
        {"type": "BODY", "text": "x"},
        {
            "type": "CAROUSEL",
            "cards": [
                {"components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "y"}]}
            ],
        },
    ]
    with pytest.raises(WhatsAppError, match="carrusel requiere media"):
        _template_send_components(components, None, carousel_cards=[{}])


def test_render_template_text_includes_text_header():
    components = [
        {"type": "HEADER", "format": "TEXT", "text": "Promoción"},
        {"type": "BODY", "text": "Hola {{1}}", "example": {"body_text": [["Ana"]]}},
        {"type": "FOOTER", "text": "FitPilot"},
    ]
    assert render_template_text(components, ["Pedro"]) == "Promoción\n\nHola Pedro\n\nFitPilot"
