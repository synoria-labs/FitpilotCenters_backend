"""Special events with their own seat inventory, payable over WhatsApp.

Adds the tables described in ``app/models/specialEventModel.py`` and two narrowly scoped
changes to the regular schedule. Nothing existing is deleted or rewritten.

Constraint changes (the only edits to existing tables)
-------------------------------------------------------
* ``class_sessions.status`` and ``reservations.status`` accept a new value, ``displaced``: a
  regular class whose slot a special event took, and the reservations it held. Every existing
  value stays valid; regular booking paths keep accepting only ``scheduled`` sessions, and every
  attendance/occupancy metric already counts only ``reserved``/``checked_in``, so a displaced
  reservation is neither attendance nor an absence nor a consumed class.

  The CHECK constraints carry different names depending on how the database was built
  (production: ``class_sessions_status_check`` / ``reservations_status_check``, created by the
  legacy SQL; a fresh ``alembic upgrade``: ``ck_session_status`` / ``ck_reservation_status``).
  The migration therefore finds them by definition — the CHECK on ``status`` of each table —
  drops them and recreates one under the ORM's name. No other constraint is touched: the
  per-session uniqueness of person and seat stays exactly as it was. Events never needed those
  relaxed, because an event's seats and participants live in their own table.
* ``campaigns.special_event_id`` (nullable FK) links a campaign to the event it promotes.

Integrity of events
-------------------
* Two live (non-canceled) events cannot overlap on the same venue, nor on the same instructor:
  ``EXCLUDE USING gist`` over ``tstzrange(start_at, end_at)``. A draft without ``end_at`` (the
  duration is still unknown) is not checked until it gets one. Requires ``btree_gist``.
* A seat and a participant count once per event while their order holds or owns them (partial
  unique indexes on ``awaiting_payment``/``paid``); a Mercado Pago payment id settles at most one
  order; a class can be taken over by at most one active takeover.

Also seeds ``manage_special_events`` for the admin role (display only — admin is an implicit
super-user). Other roles get it from the permissions matrix.

Revision ID: c3f6a9b2d5e8
Revises: b2e5f8a1c4d7
Create Date: 2026-10-21 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "c3f6a9b2d5e8"
down_revision: Union[str, None] = "b2e5f8a1c4d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SESSION_STATUSES_OLD = "('scheduled','canceled','completed')"
SESSION_STATUSES_NEW = "('scheduled','canceled','completed','displaced')"
RESERVATION_STATUSES_OLD = "('reserved','waitlisted','canceled','checked_in','no_show')"
RESERVATION_STATUSES_NEW = "('reserved','waitlisted','canceled','checked_in','no_show','displaced')"


def _replace_status_check(table: str, new_name: str, allowed: str) -> None:
    """Drop the CHECK on ``status`` of ``app.<table>`` whatever its name, then recreate it."""
    op.execute(
        f"""
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT conname
                  FROM pg_constraint
                 WHERE conrelid = 'app.{table}'::regclass
                   AND contype = 'c'
                   AND pg_get_constraintdef(oid) ~* '\\mstatus\\M'
            LOOP
                EXECUTE format('ALTER TABLE app.{table} DROP CONSTRAINT %I', r.conname);
            END LOOP;
        END $$;
        """
    )
    op.execute(f"ALTER TABLE app.{table} ADD CONSTRAINT {new_name} CHECK (status IN {allowed})")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # --- regular schedule: one new status value each -------------------------------------
    _replace_status_check("class_sessions", "ck_session_status", SESSION_STATUSES_NEW)
    _replace_status_check("reservations", "ck_reservation_status", RESERVATION_STATUSES_NEW)

    # --- events ------------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE app.special_events (
            id BIGSERIAL PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            description TEXT,
            includes TEXT,
            address TEXT,
            venue_id BIGINT NOT NULL REFERENCES app.venues(id),
            class_type_id BIGINT REFERENCES app.class_types(id),
            instructor_id BIGINT REFERENCES app.people(id),
            timezone VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City',
            start_at TIMESTAMPTZ NOT NULL,
            end_at TIMESTAMPTZ,
            price NUMERIC(12,2) NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'MXN',
            capacity INTEGER,
            status VARCHAR(20) NOT NULL DEFAULT 'draft',
            hold_minutes INTEGER NOT NULL DEFAULT 30,
            cancel_reason TEXT,
            created_by BIGINT REFERENCES app.accounts(id),
            updated_by BIGINT REFERENCES app.accounts(id),
            published_by BIGINT REFERENCES app.accounts(id),
            published_at TIMESTAMPTZ,
            canceled_by BIGINT REFERENCES app.accounts(id),
            canceled_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_special_event_status
                CHECK (status IN ('draft','published','canceled','completed')),
            CONSTRAINT ck_special_event_price CHECK (price >= 0),
            CONSTRAINT ck_special_event_capacity CHECK (capacity IS NULL OR capacity > 0),
            CONSTRAINT ck_special_event_period CHECK (end_at IS NULL OR end_at > start_at),
            CONSTRAINT ck_special_event_hold CHECK (hold_minutes BETWEEN 5 AND 240),
            CONSTRAINT ex_special_event_venue_overlap EXCLUDE USING gist (
                venue_id WITH =,
                tstzrange(start_at, end_at, '[)') WITH &&
            ) WHERE (status <> 'canceled' AND end_at IS NOT NULL),
            CONSTRAINT ex_special_event_instructor_overlap EXCLUDE USING gist (
                instructor_id WITH =,
                tstzrange(start_at, end_at, '[)') WITH &&
            ) WHERE (status <> 'canceled' AND end_at IS NOT NULL AND instructor_id IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX idx_special_events_start ON app.special_events (start_at)")

    op.execute(
        """
        CREATE TABLE app.special_event_seats (
            event_id BIGINT NOT NULL REFERENCES app.special_events(id) ON DELETE CASCADE,
            seat_id BIGINT NOT NULL REFERENCES app.seats(id),
            PRIMARY KEY (event_id, seat_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.special_event_orders (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT NOT NULL REFERENCES app.special_events(id),
            person_id BIGINT REFERENCES app.people(id),
            participant_name VARCHAR(200) NOT NULL,
            participant_phone VARCHAR(32),
            participant_key VARCHAR(20),
            seat_id BIGINT REFERENCES app.seats(id),
            channel VARCHAR(20) NOT NULL DEFAULT 'whatsapp',
            conversation_id BIGINT,
            buyer_wa_id VARCHAR(100),
            status VARCHAR(20) NOT NULL DEFAULT 'awaiting_payment',
            amount NUMERIC(12,2) NOT NULL,
            currency VARCHAR(3) NOT NULL DEFAULT 'MXN',
            external_reference VARCHAR(120) UNIQUE,
            mp_preference_id VARCHAR(120),
            mp_init_point TEXT,
            hold_expires_at TIMESTAMPTZ,
            mp_payment_id VARCHAR(120),
            mp_status VARCHAR(40),
            mp_status_detail VARCHAR(120),
            payment_id BIGINT REFERENCES app.payments(id),
            payment_method VARCHAR(40),
            paid_at TIMESTAMPTZ,
            canceled_at TIMESTAMPTZ,
            cancel_reason TEXT,
            refunded_at TIMESTAMPTZ,
            refund_reference VARCHAR(120),
            review_reason TEXT,
            notes TEXT,
            created_by BIGINT REFERENCES app.accounts(id),
            updated_by BIGINT REFERENCES app.accounts(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_special_event_order_status CHECK (status IN (
                'awaiting_payment','paid','expired','canceled','refund_pending','refunded',
                'needs_review')),
            CONSTRAINT ck_special_event_order_channel CHECK (channel IN ('whatsapp','admin')),
            CONSTRAINT ck_special_event_order_amount CHECK (amount >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_special_event_order_seat ON app.special_event_orders
            (event_id, seat_id)
            WHERE seat_id IS NOT NULL AND status IN ('awaiting_payment','paid')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_special_event_order_participant ON app.special_event_orders
            (event_id, participant_key)
            WHERE participant_key IS NOT NULL AND status IN ('awaiting_payment','paid')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_special_event_order_mp_payment ON app.special_event_orders
            (mp_payment_id) WHERE mp_payment_id IS NOT NULL
        """
    )
    op.execute(
        "CREATE INDEX idx_special_event_orders_event_status "
        "ON app.special_event_orders (event_id, status)"
    )
    op.execute(
        "CREATE INDEX idx_special_event_orders_holds ON app.special_event_orders "
        "(hold_expires_at) WHERE status = 'awaiting_payment'"
    )
    op.execute(
        "CREATE INDEX idx_special_event_orders_conversation "
        "ON app.special_event_orders (conversation_id)"
    )

    op.execute(
        """
        CREATE TABLE app.special_event_takeovers (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT NOT NULL REFERENCES app.special_events(id),
            session_id BIGINT NOT NULL REFERENCES app.class_sessions(id),
            previous_status VARCHAR(20) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            reason TEXT,
            applied_by BIGINT REFERENCES app.accounts(id),
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            restored_by BIGINT REFERENCES app.accounts(id),
            restored_at TIMESTAMPTZ,
            CONSTRAINT ck_special_event_takeover_status CHECK (status IN ('active','restored'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_special_event_takeover_active_session
            ON app.special_event_takeovers (session_id) WHERE status = 'active'
        """
    )
    op.execute(
        "CREATE INDEX idx_special_event_takeovers_event ON app.special_event_takeovers (event_id)"
    )

    op.execute(
        """
        CREATE TABLE app.special_event_displacements (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT NOT NULL REFERENCES app.special_events(id),
            takeover_id BIGINT NOT NULL REFERENCES app.special_event_takeovers(id),
            session_id BIGINT NOT NULL REFERENCES app.class_sessions(id),
            reservation_id BIGINT REFERENCES app.reservations(id) ON DELETE SET NULL,
            person_id BIGINT NOT NULL REFERENCES app.people(id),
            seat_id BIGINT REFERENCES app.seats(id),
            previous_reservation_status VARCHAR(20),
            reservation_source VARCHAR(20),
            subscription_id BIGINT
                REFERENCES app.membership_subscriptions(id) ON DELETE SET NULL,
            standing_booking_id BIGINT REFERENCES app.standing_bookings(id) ON DELETE SET NULL,
            reason TEXT NOT NULL,
            follow_up_status VARCHAR(20) NOT NULL DEFAULT 'pending',
            notified_at TIMESTAMPTZ,
            notified_by BIGINT REFERENCES app.accounts(id),
            notification_channel VARCHAR(30),
            notification_notes TEXT,
            compensation_type VARCHAR(30),
            compensation_notes TEXT,
            resolved_at TIMESTAMPTZ,
            resolved_by BIGINT REFERENCES app.accounts(id),
            restoration_status VARCHAR(20),
            restoration_note TEXT,
            restored_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_special_event_displacement_follow_up
                CHECK (follow_up_status IN ('pending','notified','resolved')),
            CONSTRAINT ck_special_event_displacement_compensation CHECK (
                compensation_type IS NULL OR compensation_type IN (
                    'make_up_class','membership_extension','event_access','credit','refund',
                    'not_needed','other')),
            CONSTRAINT ck_special_event_displacement_restoration CHECK (
                restoration_status IS NULL
                OR restoration_status IN ('restored','kept_displaced'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_special_event_displacement_person
            ON app.special_event_displacements (takeover_id, person_id)
        """
    )
    op.execute(
        "CREATE INDEX idx_special_event_displacements_event "
        "ON app.special_event_displacements (event_id, follow_up_status)"
    )

    # --- campaigns: optional link to the promoted event ------------------------------------
    op.execute(
        "ALTER TABLE app.campaigns ADD COLUMN IF NOT EXISTS special_event_id BIGINT "
        "REFERENCES app.special_events(id) ON DELETE SET NULL"
    )

    # --- capability (admin row is display-only; admin always has every capability) -------
    op.execute(
        """
        INSERT INTO app.role_capabilities (role_id, capability, created_at)
        SELECT r.id, 'manage_special_events', now()
          FROM app.roles r
         WHERE r.code = 'admin'
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # Restoring the old CHECKs would fail while displaced rows exist; put those rows back to
    # their pre-takeover status first so the downgrade is lossless for the regular schedule.
    op.execute(
        """
        UPDATE app.reservations r
           SET status = COALESCE(d.previous_reservation_status, 'reserved')
          FROM app.special_event_displacements d
         WHERE d.reservation_id = r.id AND r.status = 'displaced'
        """
    )
    op.execute("UPDATE app.reservations SET status = 'canceled' WHERE status = 'displaced'")
    op.execute(
        """
        UPDATE app.class_sessions s
           SET status = t.previous_status
          FROM app.special_event_takeovers t
         WHERE t.session_id = s.id AND s.status = 'displaced'
        """
    )
    op.execute("UPDATE app.class_sessions SET status = 'scheduled' WHERE status = 'displaced'")

    op.execute("DELETE FROM app.role_capabilities WHERE capability = 'manage_special_events'")
    op.execute("ALTER TABLE app.campaigns DROP COLUMN IF EXISTS special_event_id")
    op.execute("DROP TABLE IF EXISTS app.special_event_displacements")
    op.execute("DROP TABLE IF EXISTS app.special_event_takeovers")
    op.execute("DROP TABLE IF EXISTS app.special_event_orders")
    op.execute("DROP TABLE IF EXISTS app.special_event_seats")
    op.execute("DROP TABLE IF EXISTS app.special_events")

    _replace_status_check("reservations", "ck_reservation_status", RESERVATION_STATUSES_OLD)
    _replace_status_check("class_sessions", "ck_session_status", SESSION_STATUSES_OLD)
