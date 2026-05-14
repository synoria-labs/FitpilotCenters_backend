-- Migration: Add integrity safeguards to payments table
-- Date: 2026-05-13
-- Description:
--   1. Unique partial index on provider_payment_id (idempotency for external payments)
--   2. Composite index on (status, paid_at) to support filter-by-status panel queries
--   3. CHECK constraint enforcing allowed status values
--
-- Safe to re-run: uses IF NOT EXISTS guards and conditional constraint creation.

-- 1) Unique partial index on provider_payment_id
--    Purpose: prevent duplicate ingestion of the same external transaction id
--    while still allowing many rows where the field is NULL (manual payments).
CREATE UNIQUE INDEX IF NOT EXISTS uq_payments_provider_payment_id
ON app.payments(provider_payment_id)
WHERE provider_payment_id IS NOT NULL;

-- 2) Composite index for status + paid_at
--    Purpose: support the new finances panel filters (status + temporal range)
--    and reconciliation queries over PENDING/FAILED states.
CREATE INDEX IF NOT EXISTS idx_payments_status_paidat
ON app.payments(status, paid_at);

-- 2b) Composite index for subscription + paid_at
--     Was declared in the SQLAlchemy model but never applied to the DB.
--     Backfilled here so the model and DB stay in sync.
CREATE INDEX IF NOT EXISTS idx_payments_subscription
ON app.payments(subscription_id, paid_at);

-- 3) CHECK constraint on status
--    Purpose: prevent typos / invalid states at the DB layer.
--    Wrapped in DO block so the migration is idempotent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE c.conname = 'ck_payments_status'
          AND n.nspname = 'app'
          AND t.relname = 'payments'
    ) THEN
        ALTER TABLE app.payments
        ADD CONSTRAINT ck_payments_status
        CHECK (status IN ('COMPLETED', 'PENDING', 'FAILED', 'REFUNDED'));
    END IF;
END
$$;

-- Documentation comments
COMMENT ON INDEX app.uq_payments_provider_payment_id IS
    'Idempotency guard: prevents two payments with the same external provider transaction id.';
COMMENT ON INDEX app.idx_payments_status_paidat IS
    'Supports finances panel filters by (status, date range) and reconciliation queries.';
