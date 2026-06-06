"""Render stored WhatsApp template message text.

Revision ID: 2c1a4e6f8b90
Revises: 9d7b2c3a4e5f
Create Date: 2026-06-05 20:05:00.000000
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional, Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "2c1a4e6f8b90"
down_revision: Union[str, None] = "9d7b2c3a4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")


def upgrade() -> None:
    bind = op.get_bind()
    template_rows = bind.execute(
        sa.text(
            """
            SELECT id, template_name, components
            FROM app.whatsapp_templates
            WHERE components IS NOT NULL
            """
        )
    ).mappings().all()

    name_counts = Counter(str(row["template_name"] or "") for row in template_rows)
    templates_by_name = {
        str(row["template_name"]): row
        for row in template_rows
        if row["template_name"] and name_counts[str(row["template_name"])] == 1
    }
    if not templates_by_name:
        return

    message_rows = bind.execute(
        sa.text(
            """
            SELECT id, text_content
            FROM app.messages
            WHERE message_type = 'template'
              AND text_content LIKE '[Plantilla] %'
            """
        )
    ).mappings().all()

    for message in message_rows:
        template_name = str(message["text_content"] or "").removeprefix("[Plantilla] ").strip()
        template = templates_by_name.get(template_name)
        if template is None:
            continue
        rendered = _render_template_text(template["components"])
        if not rendered:
            continue
        bind.execute(
            sa.text(
                """
                UPDATE app.messages
                SET text_content = :text_content,
                    template_id = :template_id
                WHERE id = :message_id
                """
            ),
            {
                "text_content": rendered,
                "template_id": template["id"],
                "message_id": message["id"],
            },
        )


def downgrade() -> None:
    # Rendering message text is a presentation data fix and cannot be safely reversed.
    pass


def _render_template_text(components: Optional[list[Any]]) -> str:
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
            body_text = _replace_placeholders(text, component)
        elif ctype == "FOOTER":
            footer_text = text

    return _normalize_text("\n\n".join(part for part in (body_text, footer_text) if part))


def _replace_placeholders(text: str, component: dict[str, Any]) -> str:
    values = _example_values(component)

    def replace(match: re.Match) -> str:
        index = int(match.group(1)) - 1
        if 0 <= index < len(values) and values[index]:
            return values[index]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


def _example_values(component: dict[str, Any]) -> list[str]:
    example = component.get("example")
    if not isinstance(example, dict):
        return []
    rows = example.get("body_text")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        return []
    return [str(value).strip() for value in rows[0]]


def _normalize_text(text: str) -> str:
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
