"""Build and parse the Meta template ``components`` array from a simplified editor.

The desktop editor only exposes a BODY (with positional ``{{1}}`` placeholders) and an
optional FOOTER. Meta, however, stores templates as a structured ``components`` array and
requires an ``example`` for any BODY that contains placeholders. These helpers translate
between the two representations so the rest of the backend can work with plain fields.
"""
import copy
import re
from typing import Any, Dict, List, Optional, Tuple

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")

# Header formats that carry a media file (vs TEXT / LOCATION).
_MEDIA_HEADER_FORMATS = {"IMAGE", "VIDEO", "DOCUMENT"}
# Header formats Meta allows inside a CAROUSEL card (no DOCUMENT/TEXT/LOCATION).
_CAROUSEL_HEADER_FORMATS = {"IMAGE", "VIDEO"}
_BUTTON_TYPES = {"QUICK_REPLY", "URL", "PHONE_NUMBER"}
_HEADER_TEXT_MAX = 60
_BUTTON_TEXT_MAX = 25
_BUTTON_URL_MAX = 2000
_MAX_BUTTONS = 10
_MAX_URL_BUTTONS = 2
_MAX_PHONE_BUTTONS = 1
_MAX_CAROUSEL_CARDS = 10


def placeholder_count(body_text: str) -> int:
    """Highest positional placeholder index used in ``body_text`` (0 if none)."""
    indices = [int(m) for m in _PLACEHOLDER_RE.findall(body_text or "")]
    return max(indices) if indices else 0


def _header_component(components: Optional[List[Any]]) -> Optional[dict]:
    for component in components or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "HEADER":
            return component
    return None


def required_header_kind(components: Optional[List[Any]]) -> Optional[str]:
    """Return the top-level header format (IMAGE/VIDEO/DOCUMENT/TEXT/LOCATION) or None."""
    header = _header_component(components)
    if header is None:
        return None
    return str(header.get("format") or "").upper() or None


def required_header_media_format(components: Optional[List[Any]]) -> Optional[str]:
    """Return IMAGE/VIDEO/DOCUMENT when the template requires header media (excludes TEXT/LOCATION)."""
    kind = required_header_kind(components)
    return kind if kind in _MEDIA_HEADER_FORMATS else None


def header_text_var_count(components: Optional[List[Any]]) -> int:
    """Number of placeholders in a TEXT header (0 if no TEXT header)."""
    header = _header_component(components)
    if header is None or str(header.get("format") or "").upper() != "TEXT":
        return 0
    return placeholder_count(str(header.get("text") or ""))


def header_text_value(components: Optional[List[Any]]) -> Optional[str]:
    """The literal text of a TEXT header (may contain ``{{1}}``), or None."""
    header = _header_component(components)
    if header is None or str(header.get("format") or "").upper() != "TEXT":
        return None
    return str(header.get("text") or "")


def header_text_example_value(components: Optional[List[Any]]) -> Optional[str]:
    """Stored example value for a TEXT header variable, if any."""
    header = _header_component(components)
    if header is None or str(header.get("format") or "").upper() != "TEXT":
        return None
    example = header.get("example")
    if isinstance(example, dict):
        values = example.get("header_text")
        if isinstance(values, list) and values:
            return str(values[0])
    return None


def buttons_from_components(components: Optional[List[Any]]) -> List[dict]:
    """Return the top-level BUTTONS array (empty when none)."""
    for component in components or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "BUTTONS":
            buttons = component.get("buttons")
            if isinstance(buttons, list):
                return [b for b in buttons if isinstance(b, dict)]
    return []


def dynamic_url_button_index(buttons: Optional[List[Any]]) -> Optional[int]:
    """Position (in the buttons array) of the URL button with a ``{{1}}`` variable, or None."""
    for index, button in enumerate(buttons or []):
        if not isinstance(button, dict):
            continue
        if str(button.get("type") or "").upper() == "URL" and placeholder_count(str(button.get("url") or "")):
            return index
    return None


def dynamic_url_button_example(buttons: Optional[List[Any]]) -> Optional[str]:
    index = dynamic_url_button_index(buttons)
    if index is None:
        return None
    example = buttons[index].get("example")
    if isinstance(example, list) and example:
        return str(example[0])
    return None


def carousel_cards_from_components(components: Optional[List[Any]]) -> List[dict]:
    """Return the CAROUSEL cards array (each ``{"components": [...]}``); empty when none."""
    for component in components or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "CAROUSEL":
            cards = component.get("cards")
            if isinstance(cards, list):
                return [c for c in cards if isinstance(c, dict)]
    return []


def card_header_media_format(card: dict) -> Optional[str]:
    for component in (card or {}).get("components") or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "HEADER":
            fmt = str(component.get("format") or "").upper()
            return fmt if fmt in _MEDIA_HEADER_FORMATS else None
    return None


def card_default_asset_id(card: dict) -> Optional[int]:
    for component in (card or {}).get("components") or []:
        if isinstance(component, dict) and str(component.get("type") or "").upper() == "HEADER":
            value = component.get("fitpilot_asset_id")
            return int(value) if value is not None else None
    return None


def _build_body(body_text: str, body_examples: Optional[List[str]]) -> dict:
    """Build a single BODY component, padding/truncating examples to the placeholder count."""
    body_text = (body_text or "").strip()
    body: dict = {"type": "BODY", "text": body_text}
    count = placeholder_count(body_text)
    if count:
        examples = list(body_examples or [])
        if len(examples) < count:
            examples += [f"ejemplo{i + 1}" for i in range(len(examples), count)]
        else:
            examples = examples[:count]
        body["example"] = {"body_text": [examples]}
    return body


def _build_media_header(header_format: str, header_handle: Optional[str]) -> dict:
    handle = (header_handle or "").strip()
    if not handle:
        raise ValueError(f"El header {header_format} requiere un header_handle de Meta.")
    return {
        "type": "HEADER",
        "format": header_format,
        "example": {"header_handle": [handle]},
    }


def _build_text_header(header_text: Optional[str], header_text_example: Optional[str]) -> dict:
    text = (header_text or "").strip()
    if not text:
        raise ValueError("El header de texto requiere texto.")
    if len(text) > _HEADER_TEXT_MAX:
        raise ValueError(f"El header de texto no puede exceder {_HEADER_TEXT_MAX} caracteres.")
    var_count = placeholder_count(text)
    if var_count > 1:
        raise ValueError("El header de texto solo admite una variable ({{1}}).")
    header: dict = {"type": "HEADER", "format": "TEXT", "text": text}
    if var_count == 1:
        example = (header_text_example or "").strip() or "ejemplo"
        header["example"] = {"header_text": [example]}
    return header


def _build_buttons(buttons: List[dict]) -> List[dict]:
    """Build a Meta ``buttons`` array and enforce Meta's structural limits.

    Each input button is ``{type, text, url?, phone_number?, payload?, example?}``. A URL whose
    value ends in ``{{1}}`` is a dynamic URL button and carries an ``example`` list (Meta needs a
    sample suffix). Meta allows at most one dynamic URL variable across the whole template.
    """
    cleaned = [b for b in (buttons or []) if isinstance(b, dict)]
    if not cleaned:
        return []
    if len(cleaned) > _MAX_BUTTONS:
        raise ValueError(f"Máximo {_MAX_BUTTONS} botones por plantilla.")

    result: List[dict] = []
    url_count = phone_count = 0
    dynamic_url_seen = False
    for button in cleaned:
        btype = str(button.get("type") or "").strip().upper()
        text = str(button.get("text") or "").strip()
        if btype not in _BUTTON_TYPES:
            raise ValueError(f"Tipo de botón no soportado: {btype or '(vacío)'}.")
        if not text:
            raise ValueError("Cada botón requiere un texto visible.")
        if len(text) > _BUTTON_TEXT_MAX:
            raise ValueError(f"El texto del botón no puede exceder {_BUTTON_TEXT_MAX} caracteres.")

        if btype == "QUICK_REPLY":
            result.append({"type": "QUICK_REPLY", "text": text})
        elif btype == "PHONE_NUMBER":
            phone_count += 1
            phone = str(button.get("phone_number") or "").strip()
            if not phone:
                raise ValueError("El botón de llamada requiere un número de teléfono.")
            result.append({"type": "PHONE_NUMBER", "text": text, "phone_number": phone})
        else:  # URL
            url_count += 1
            url = str(button.get("url") or "").strip()
            if not url:
                raise ValueError("El botón de URL requiere una URL.")
            if len(url) > _BUTTON_URL_MAX:
                raise ValueError(f"La URL del botón no puede exceder {_BUTTON_URL_MAX} caracteres.")
            comp: dict = {"type": "URL", "text": text, "url": url}
            var_count = placeholder_count(url)
            if var_count:
                if var_count > 1 or not url.rstrip().endswith("}}"):
                    raise ValueError("La URL dinámica solo admite una variable {{1}} al final.")
                if dynamic_url_seen:
                    raise ValueError("Solo se permite una URL dinámica por plantilla.")
                dynamic_url_seen = True
                example_raw = button.get("example")
                if isinstance(example_raw, list):
                    example = str(example_raw[0]).strip() if example_raw else ""
                else:
                    example = str(example_raw or "").strip()
                comp["example"] = [example or "ejemplo"]
            result.append(comp)

    if phone_count > _MAX_PHONE_BUTTONS:
        raise ValueError(f"Solo se permite {_MAX_PHONE_BUTTONS} botón de llamada.")
    if url_count > _MAX_URL_BUTTONS:
        raise ValueError(f"Máximo {_MAX_URL_BUTTONS} botones de URL.")
    return result


def _build_carousel(carousel_cards: List[dict]) -> dict:
    cards = [c for c in (carousel_cards or []) if isinstance(c, dict)]
    if not cards:
        raise ValueError("El carrusel requiere al menos una tarjeta.")
    if len(cards) > _MAX_CAROUSEL_CARDS:
        raise ValueError(f"Máximo {_MAX_CAROUSEL_CARDS} tarjetas en un carrusel.")

    built_cards: List[dict] = []
    first_format: Optional[str] = None
    for card in cards:
        card_format = str(card.get("header_format") or "").strip().upper()
        if card_format not in _CAROUSEL_HEADER_FORMATS:
            raise ValueError("Cada tarjeta del carrusel requiere header IMAGE o VIDEO.")
        if first_format is None:
            first_format = card_format
        elif card_format != first_format:
            raise ValueError("Todas las tarjetas del carrusel deben usar el mismo formato de header.")

        card_components: List[dict] = [
            _build_media_header(card_format, card.get("header_handle")),
            _build_body(card.get("body_text") or "", card.get("body_examples")),
        ]
        card_buttons = _build_buttons(card.get("buttons") or [])
        if card_buttons:
            card_components.append({"type": "BUTTONS", "buttons": card_buttons})
        built_cards.append({"components": card_components})

    return {"type": "CAROUSEL", "cards": built_cards}


def build_components(
    body_text: str,
    body_examples: Optional[List[str]] = None,
    footer_text: Optional[str] = None,
    header_format: Optional[str] = None,
    header_handle: Optional[str] = None,
    *,
    header_text: Optional[str] = None,
    header_text_example: Optional[str] = None,
    buttons: Optional[List[dict]] = None,
    carousel_cards: Optional[List[dict]] = None,
) -> List[dict]:
    """Assemble a Meta ``components`` array from the simplified editor fields.

    ``body_examples`` are the example values for {{1}}, {{2}}, ... in order; Meta requires
    them whenever the body contains placeholders. Missing examples are padded so the count
    matches the placeholders (Meta rejects a mismatch).

    ``header_format`` accepts IMAGE/VIDEO/DOCUMENT (needs ``header_handle``), TEXT (needs
    ``header_text``) or LOCATION. ``buttons`` is a list of QUICK_REPLY/URL/PHONE_NUMBER button
    dicts. ``carousel_cards`` builds a CAROUSEL template: per Meta, such a template's bubble can
    only carry a BODY (no top-level header/footer/buttons), so those are ignored when cards are
    given and each card supplies its own media header (handle), body and optional buttons.
    """
    components: List[dict] = []
    header_format = (header_format or "").strip().upper()

    if carousel_cards:
        components.append(_build_body(body_text, body_examples))
        components.append(_build_carousel(carousel_cards))
        return components

    if header_format:
        if header_format in _MEDIA_HEADER_FORMATS:
            components.append(_build_media_header(header_format, header_handle))
        elif header_format == "TEXT":
            components.append(_build_text_header(header_text, header_text_example))
        elif header_format == "LOCATION":
            components.append({"type": "HEADER", "format": "LOCATION"})
        else:
            raise ValueError(
                "Header no soportado: usa IMAGE, VIDEO, DOCUMENT, TEXT o LOCATION."
            )

    components.append(_build_body(body_text, body_examples))

    footer_text = (footer_text or "").strip()
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    built_buttons = _build_buttons(buttons or [])
    if built_buttons:
        components.append({"type": "BUTTONS", "buttons": built_buttons})

    return components


def embed_carousel_card_assets(
    components: List[dict], card_asset_ids: List[Optional[int]]
) -> List[dict]:
    """Return a deep copy of ``components`` with ``fitpilot_asset_id`` set on each carousel card
    header. This local-only key lets sends resolve a per-card default media asset; it is stripped
    before posting to Meta (``strip_local_metadata``)."""
    enriched = copy.deepcopy(components or [])
    for component in enriched:
        if not isinstance(component, dict) or str(component.get("type") or "").upper() != "CAROUSEL":
            continue
        cards = component.get("cards") or []
        for index, card in enumerate(cards):
            if not isinstance(card, dict):
                continue
            asset_id = card_asset_ids[index] if index < len(card_asset_ids) else None
            if asset_id is None:
                continue
            for card_component in card.get("components") or []:
                if (
                    isinstance(card_component, dict)
                    and str(card_component.get("type") or "").upper() == "HEADER"
                ):
                    card_component["fitpilot_asset_id"] = asset_id
                    break
    return enriched


def strip_local_metadata(components: Optional[List[Any]]) -> List[Any]:
    """Return a deep copy of ``components`` with FitPilot-only keys removed (for Meta payloads)."""
    cleaned = copy.deepcopy(components or [])
    for component in cleaned:
        if not isinstance(component, dict) or str(component.get("type") or "").upper() != "CAROUSEL":
            continue
        for card in component.get("cards") or []:
            if not isinstance(card, dict):
                continue
            for card_component in card.get("components") or []:
                if isinstance(card_component, dict):
                    card_component.pop("fitpilot_asset_id", None)
    return cleaned


def header_handle_from_components(components: Optional[List[Any]]) -> Optional[str]:
    """Return the first stored media header sample handle, if present."""
    for component in components or []:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").upper() != "HEADER":
            continue
        example = component.get("example")
        if not isinstance(example, dict):
            continue
        handles = example.get("header_handle")
        if isinstance(handles, list) and handles:
            value = str(handles[0] or "").strip()
            if value:
                return value
    return None


def parse_components(
    components: Optional[List[Any]],
) -> Tuple[str, List[str], Optional[str]]:
    """Inverse of :func:`build_components`: pull (body_text, body_examples, footer_text).

    Tolerant of Meta's uppercase types and of the ``example.body_text`` nesting.
    """
    body_text = ""
    body_examples: List[str] = []
    footer_text: Optional[str] = None

    for component in components or []:
        if not isinstance(component, dict):
            continue
        ctype = str(component.get("type") or "").upper()
        text = component.get("text")
        if ctype == "BODY" and isinstance(text, str):
            body_text = text
            example = component.get("example")
            if isinstance(example, dict):
                rows = example.get("body_text")
                if isinstance(rows, list) and rows and isinstance(rows[0], list):
                    body_examples = [str(v) for v in rows[0]]
        elif ctype == "FOOTER" and isinstance(text, str):
            footer_text = text

    return body_text, body_examples, footer_text


def render_template_text(
    components: Optional[List[Any]],
    body_params: Optional[List[str]] = None,
) -> str:
    """Render BODY + FOOTER for chat storage/presentation.

    ``body_params`` are the actual values sent to Meta. Missing values fall back to the
    template examples because that is also what the send payload builder uses.
    """
    header_text = ""
    body_text = ""
    footer_text = ""

    for component in components or []:
        if not isinstance(component, dict):
            continue
        ctype = str(component.get("type") or "").upper()
        text = component.get("text")
        if not isinstance(text, str):
            continue
        if ctype == "HEADER" and str(component.get("format") or "").upper() == "TEXT":
            # The header runtime value is not in body_params; render with its example.
            example = component.get("example") if isinstance(component.get("example"), dict) else {}
            header_examples = example.get("header_text") if isinstance(example, dict) else None
            header_text = _PLACEHOLDER_RE.sub(
                lambda m: str(header_examples[0]) if isinstance(header_examples, list) and header_examples else m.group(0),
                text,
            )
        elif ctype == "BODY":
            body_text = _replace_placeholders(text, component, body_params)
        elif ctype == "FOOTER":
            footer_text = text

    return _normalize_rendered_text(
        "\n\n".join(part for part in (header_text, body_text, footer_text) if part)
    )


def _replace_placeholders(
    text: str,
    component: dict,
    body_params: Optional[List[str]],
) -> str:
    provided = [str(value).strip() for value in body_params or []]
    examples = _component_example_values(component)

    def replace(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        if 0 <= index < len(provided) and provided[index]:
            return provided[index]
        if 0 <= index < len(examples) and examples[index]:
            return examples[index]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


def _component_example_values(component: dict) -> List[str]:
    example = component.get("example")
    if not isinstance(example, dict):
        return []
    rows = example.get("body_text")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return []
    return [str(value).strip() for value in rows[0]]


def _normalize_rendered_text(text: str) -> str:
    normalized = (
        str(text)
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    lines = [line.rstrip() for line in normalized.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())
