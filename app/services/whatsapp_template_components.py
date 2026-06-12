"""Build and parse the Meta template ``components`` array from a simplified editor.

The desktop editor only exposes a BODY (with positional ``{{1}}`` placeholders) and an
optional FOOTER. Meta, however, stores templates as a structured ``components`` array and
requires an ``example`` for any BODY that contains placeholders. These helpers translate
between the two representations so the rest of the backend can work with plain fields.
"""
import re
from typing import Any, List, Optional, Tuple

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def placeholder_count(body_text: str) -> int:
    """Highest positional placeholder index used in ``body_text`` (0 if none)."""
    indices = [int(m) for m in _PLACEHOLDER_RE.findall(body_text or "")]
    return max(indices) if indices else 0


def required_header_media_format(components: Optional[List[Any]]) -> Optional[str]:
    """Return IMAGE/VIDEO/DOCUMENT when the template requires header media."""
    for component in components or []:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").upper() != "HEADER":
            continue
        header_format = str(component.get("format") or "").upper()
        if header_format in {"IMAGE", "VIDEO", "DOCUMENT"}:
            return header_format
    return None


def build_components(
    body_text: str,
    body_examples: Optional[List[str]] = None,
    footer_text: Optional[str] = None,
    header_format: Optional[str] = None,
    header_handle: Optional[str] = None,
) -> List[dict]:
    """Assemble a Meta ``components`` array from the simplified editor fields.

    ``body_examples`` are the example values for {{1}}, {{2}}, ... in order; Meta requires
    them whenever the body contains placeholders. Missing examples are padded so the count
    matches the placeholders (Meta rejects a mismatch).
    """
    body_text = (body_text or "").strip()
    components: List[dict] = []

    header_format = (header_format or "").strip().upper()
    if header_format:
        if header_format not in {"IMAGE", "VIDEO", "DOCUMENT"}:
            raise ValueError("Solo se soportan headers IMAGE, VIDEO o DOCUMENT en este editor.")
        handle = (header_handle or "").strip()
        if not handle:
            raise ValueError(f"El header {header_format} requiere un header_handle de Meta.")
        components.append(
            {
                "type": "HEADER",
                "format": header_format,
                "example": {"header_handle": [handle]},
            }
        )

    body: dict = {"type": "BODY", "text": body_text}
    count = placeholder_count(body_text)
    if count:
        examples = list(body_examples or [])
        if len(examples) < count:
            examples += [f"ejemplo{i + 1}" for i in range(len(examples), count)]
        else:
            examples = examples[:count]
        body["example"] = {"body_text": [examples]}
    components.append(body)

    footer_text = (footer_text or "").strip()
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    return components


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
    body_text = ""
    footer_text = ""

    for component in components or []:
        if not isinstance(component, dict):
            continue
        ctype = str(component.get("type") or "").upper()
        text = component.get("text")
        if not isinstance(text, str):
            continue
        if ctype == "BODY":
            body_text = _replace_placeholders(text, component, body_params)
        elif ctype == "FOOTER":
            footer_text = text

    return _normalize_rendered_text(
        "\n\n".join(part for part in (body_text, footer_text) if part)
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
