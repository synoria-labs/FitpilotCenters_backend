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


def build_components(
    body_text: str,
    body_examples: Optional[List[str]] = None,
    footer_text: Optional[str] = None,
) -> List[dict]:
    """Assemble a Meta ``components`` array from the simplified editor fields.

    ``body_examples`` are the example values for {{1}}, {{2}}, ... in order; Meta requires
    them whenever the body contains placeholders. Missing examples are padded so the count
    matches the placeholders (Meta rejects a mismatch).
    """
    body_text = (body_text or "").strip()
    components: List[dict] = []

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
