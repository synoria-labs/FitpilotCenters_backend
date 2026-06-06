"""Merge duplicate WhatsApp contacts.

Revision ID: 9d7b2c3a4e5f
Revises: f6708ea6b1f4
Create Date: 2026-06-05 19:20:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "9d7b2c3a4e5f"
down_revision: Union[str, None] = "f6708ea6b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Consolidate contacts duplicated by Mexican 52/521 number variants."""
    op.execute(
        r"""
        CREATE TEMP TABLE whatsapp_contact_merge_plan ON COMMIT DROP AS
        WITH contact_stats AS (
            SELECT
                c.id AS contact_id,
                c.wa_id,
                c.phone_number,
                c.name,
                c.profile_name,
                regexp_replace(coalesce(c.wa_id, ''), '\D', '', 'g') AS wa_digits,
                regexp_replace(coalesce(c.phone_number, ''), '\D', '', 'g') AS phone_digits,
                coalesce(
                    nullif(regexp_replace(coalesce(c.wa_id, ''), '\D', '', 'g'), ''),
                    nullif(regexp_replace(coalesce(c.phone_number, ''), '\D', '', 'g'), '')
                ) AS identity_digits,
                count(m.id)::bigint AS message_count
            FROM app.contacts AS c
            LEFT JOIN app.messages AS m ON m.contact_id = c.id
            GROUP BY c.id
        ),
        keyed AS (
            SELECT
                *,
                right(identity_digits, 10) AS merge_key,
                CASE
                    WHEN wa_digits LIKE '521%' AND length(wa_digits) >= 13 THEN 0
                    ELSE 1
                END AS authority_rank,
                length(wa_digits) AS wa_length
            FROM contact_stats
            WHERE length(identity_digits) >= 10
        ),
        ranked AS (
            SELECT
                *,
                count(*) OVER (PARTITION BY merge_key) AS group_size,
                first_value(contact_id) OVER (
                    PARTITION BY merge_key
                    ORDER BY message_count DESC, authority_rank ASC, wa_length DESC, contact_id ASC
                ) AS canonical_contact_id
            FROM keyed
        )
        SELECT *
        FROM ranked
        WHERE group_size > 1;
        """
    )

    op.execute(
        r"""
        CREATE TEMP TABLE whatsapp_contact_merge_conversations ON COMMIT DROP AS
        SELECT
            p.merge_key,
            c.id AS conversation_id,
            c.contact_id
        FROM whatsapp_contact_merge_plan AS p
        JOIN app.conversations AS c ON c.contact_id = p.contact_id;
        """
    )

    op.execute(
        r"""
        CREATE TEMP TABLE whatsapp_contact_merge_targets ON COMMIT DROP AS
        WITH groups AS (
            SELECT DISTINCT merge_key, canonical_contact_id
            FROM whatsapp_contact_merge_plan
        )
        SELECT DISTINCT ON (g.merge_key)
            g.merge_key,
            g.canonical_contact_id,
            cm.conversation_id AS target_conversation_id
        FROM groups AS g
        JOIN whatsapp_contact_merge_conversations AS cm ON cm.merge_key = g.merge_key
        LEFT JOIN app.messages AS m ON m.conversation_id = cm.conversation_id
        GROUP BY g.merge_key, g.canonical_contact_id, cm.conversation_id
        ORDER BY g.merge_key, max(m.timestamp) DESC NULLS LAST, cm.conversation_id ASC;
        """
    )

    op.execute(
        r"""
        WITH carry AS (
            SELECT
                merge_key,
                canonical_contact_id,
                min(nullif(name, '')) AS carry_name,
                min(nullif(profile_name, '')) AS carry_profile_name
            FROM whatsapp_contact_merge_plan
            GROUP BY merge_key, canonical_contact_id
        )
        UPDATE app.contacts AS c
        SET
            name = coalesce(nullif(c.name, ''), carry.carry_name),
            profile_name = coalesce(nullif(c.profile_name, ''), carry.carry_profile_name)
        FROM carry
        WHERE c.id = carry.canonical_contact_id;
        """
    )

    op.execute(
        r"""
        UPDATE app.conversations AS c
        SET contact_id = t.canonical_contact_id
        FROM whatsapp_contact_merge_targets AS t
        WHERE c.id = t.target_conversation_id
          AND c.contact_id <> t.canonical_contact_id;
        """
    )

    op.execute(
        r"""
        UPDATE app.messages AS m
        SET
            contact_id = t.canonical_contact_id,
            conversation_id = t.target_conversation_id
        FROM whatsapp_contact_merge_targets AS t
        WHERE t.target_conversation_id IS NOT NULL
          AND (
              m.contact_id IN (
                  SELECT p.contact_id
                  FROM whatsapp_contact_merge_plan AS p
                  WHERE p.merge_key = t.merge_key
              )
              OR m.conversation_id IN (
                  SELECT cm.conversation_id
                  FROM whatsapp_contact_merge_conversations AS cm
                  WHERE cm.merge_key = t.merge_key
              )
          );
        """
    )

    op.execute(
        r"""
        DELETE FROM app.conversations AS c
        USING whatsapp_contact_merge_conversations AS cm
        JOIN whatsapp_contact_merge_targets AS t ON t.merge_key = cm.merge_key
        WHERE c.id = cm.conversation_id
          AND c.id <> t.target_conversation_id;
        """
    )

    op.execute(
        r"""
        DELETE FROM app.contacts AS c
        USING whatsapp_contact_merge_plan AS p
        WHERE c.id = p.contact_id
          AND p.contact_id <> p.canonical_contact_id;
        """
    )


def downgrade() -> None:
    # Data consolidation cannot be safely reversed.
    pass
