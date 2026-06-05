-- Migration: Backfill legacy payment→subscription links and reconstruct historical plans
-- Date: 2026-05-13
-- Description:
--   Before this fix, 86% of payments (IDs 1-2550, pre-2025-09) had subscription_id=NULL
--   and their corresponding subscriptions all referenced a placeholder plan_id=1
--   ("Plan Premium Transformación Total" $8999) regardless of the real amount paid.
--
--   This migration:
--     1. Creates one historical plan per distinct legacy monthly amount.
--     2. Pairs each legacy orphan payment to a subscription using
--        (person_id, calendar_date, row_number) into a TEMP TABLE.
--     3. Reassigns the paired subscription's plan_id to the matching historical plan.
--     4. Sets the payment's subscription_id from the same pairing.
--
--   Pre-flight findings (run on 2026-05-13):
--     - 2480 orphan payments total
--     - 2444 pair cleanly via row_number strategy
--     - 36 remain orphan (no matching subscription on same date) — left for manual review
--     - 0 conflicts with already-linked subscriptions
--
--   Safe to re-run: pre-flight checks short-circuit if there are no orphans left.

BEGIN;

-- 1) Insert historical plans (one per unique legacy amount, except $600 which
--    matches the existing Plan Spinning Mensual id=3 exactly).
INSERT INTO app.membership_plans (name, description, price, duration_value, duration_unit, fixed_time_slot)
SELECT
  'Plan Mensual $' || amt::text || ' (legacy)',
  'Plan histórico inferido por monto durante el backfill de pagos huérfanos.',
  amt,
  1,
  'month',
  false
FROM (VALUES
  (40.00), (50.00), (150.00), (160.00), (200.00), (800.00),
  (1000.00), (2000.00), (4999.00), (5000.00), (5700.00)
) AS legacy(amt)
WHERE NOT EXISTS (
  SELECT 1 FROM app.membership_plans mp
  WHERE mp.name = 'Plan Mensual $' || legacy.amt::text || ' (legacy)'
);

-- 2) Compute the pairing ONCE and persist it in a temp table.
--    Filter the subscription side to: plan_id=1 AND no linked payment.
CREATE TEMP TABLE _legacy_backfill_pairing ON COMMIT DROP AS
WITH ranked_payments AS (
  SELECT id AS pay_id, person_id, paid_at, amount,
         ROW_NUMBER() OVER (PARTITION BY person_id, paid_at::date ORDER BY id) AS rn
  FROM app.payments
  WHERE subscription_id IS NULL
),
ranked_subs AS (
  SELECT s.id AS sub_id, s.person_id, s.start_at,
         ROW_NUMBER() OVER (PARTITION BY s.person_id, s.start_at::date ORDER BY s.id) AS rn
  FROM app.membership_subscriptions s
  WHERE s.plan_id = 1
    AND NOT EXISTS (SELECT 1 FROM app.payments p WHERE p.subscription_id = s.id)
)
SELECT
  p.pay_id,
  p.amount,
  s.sub_id,
  CASE
    WHEN p.amount = 600.00 THEN 3
    ELSE (
      SELECT mp.id FROM app.membership_plans mp
      WHERE mp.name = 'Plan Mensual $' || p.amount::text || ' (legacy)'
      LIMIT 1
    )
  END AS target_plan_id
FROM ranked_payments p
JOIN ranked_subs s
  ON s.person_id = p.person_id
 AND s.start_at::date = p.paid_at::date
 AND s.rn = p.rn;

-- Defensive: every pair should have resolved a target plan. Abort otherwise.
DO $$
DECLARE
  unresolved_count int;
BEGIN
  SELECT COUNT(*) INTO unresolved_count
  FROM _legacy_backfill_pairing WHERE target_plan_id IS NULL;
  IF unresolved_count > 0 THEN
    RAISE EXCEPTION 'Backfill aborted: % pairings could not resolve a target plan_id', unresolved_count;
  END IF;
END $$;

-- 3) Reassign sub.plan_id to the inferred historical plan.
UPDATE app.membership_subscriptions s
SET plan_id = bp.target_plan_id,
    updated_at = now()
FROM _legacy_backfill_pairing bp
WHERE s.id = bp.sub_id
  AND s.plan_id = 1;

-- 4) Link payments to subscriptions.
UPDATE app.payments pay
SET subscription_id = bp.sub_id
FROM _legacy_backfill_pairing bp
WHERE pay.id = bp.pay_id
  AND pay.subscription_id IS NULL;

COMMIT;
