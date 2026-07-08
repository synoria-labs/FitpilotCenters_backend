# Gym App Database Migration Playbook (Markdown)

**Audience:** CLI/automation agent (IaC/DevOps/DBA).  
**DB:** PostgreSQL ≥ 14.  
**Schema target:** `app` (English naming).  
**Goal:** Unify identities, generalize group classes (spinning/yoga/pilates/zumba), support fixed time-slot memberships and seat-based rooms, and add equipment maintenance.

--- 

## 0) Assumptions & Safety
- You have superuser or equivalent privileges for DDL/DML.
- A **full logical backup** exists (e.g., `pg_dump -Fc`), and optional physical backup/snapshot.
- The legacy schema contains tables equivalent to: `members`, `personas`, `users`, `bicicletas`, `classes`, `reservas`, `pagos`, `memberships` (names may vary).
- Cutover will be **one short read-only window**. We’ll pre-create new tables, migrate data, validate, then swap app connections.
- Timezone: store timestamps as `TIMESTAMPTZ`.

### 0.1 Commands (backup & maintenance)
```bash
# Logical backup (custom format)
pg_dump -h $PGHOST -U $PGUSER -d $PGDATABASE -Fc -f ./pre_migration.dump

# Optionally: enable a brief maintenance window (app in read-only)
# Your app-level toggle here (feature flag / env var):
# e.g., APP_READ_ONLY=true
```

---

## 1) Target Schema (Overview)
**Identity & Auth**
- `people`, `roles`, `person_roles`, `accounts`

**Memberships & Payments**
- `membership_plans` (+ rule columns), `membership_subscriptions`, `payments`

**Venues & Seats**
- `venues`, `seat_types`, `seats`

**Equipment & Maintenance**
- `asset_types`, `asset_models`, `assets`, `asset_seat_assignments`, `asset_events`

**Classes**
- `class_types`, `class_templates` (recurrence), `class_sessions` (instances)

**Reservations & Fixed Slot Logic**
- `reservations` (with `source`), `standing_bookings`, `standing_booking_exceptions`

> Rationale: Seats are positions in a venue (e.g., *Bike 7*). Equipment/maintenance belong to *assets* and are attached to seats via assignments with history.

---

## 2) Create/Upgrade DDL (Idempotent)
> Execute in a single transaction if possible; otherwise, run blocks in order. **Adjust FK references** for actual legacy table names if needed.

```sql
BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- 2.1 Identity & Auth
CREATE TABLE IF NOT EXISTS app.people (
  id           BIGSERIAL PRIMARY KEY,
  full_name    VARCHAR(200),
  phone_number VARCHAR(32),
  email        VARCHAR(200),
  wa_id        VARCHAR(100),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  deleted_at   TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app.roles (
  id          BIGSERIAL PRIMARY KEY,
  code        VARCHAR(50) UNIQUE NOT NULL,   -- 'member','instructor','staff','admin'
  description VARCHAR(200),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.person_roles (
  person_id  BIGINT NOT NULL REFERENCES app.people(id),
  role_id    BIGINT NOT NULL REFERENCES app.roles(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (person_id, role_id)
);

CREATE TABLE IF NOT EXISTS app.accounts (
  id            BIGSERIAL PRIMARY KEY,
  person_id     BIGINT NOT NULL REFERENCES app.people(id),
  username      VARCHAR(100) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ
);

-- 2.2 Memberships & Payments (+ rules)
CREATE TABLE IF NOT EXISTS app.membership_plans (
  id               BIGSERIAL PRIMARY KEY,
  name             VARCHAR(120) NOT NULL,
  description      TEXT,
  price            NUMERIC(12,2) NOT NULL,
  duration_value   INT NOT NULL,
  duration_unit    VARCHAR(10) NOT NULL CHECK (duration_unit IN ('day','week','month')),
  class_limit      INT,
  fixed_time_slot  BOOLEAN NOT NULL DEFAULT FALSE,
  max_sessions_per_day  INT,
  max_sessions_per_week INT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app.membership_subscriptions (
  id          BIGSERIAL PRIMARY KEY,
  person_id   BIGINT NOT NULL REFERENCES app.people(id),
  plan_id     BIGINT NOT NULL REFERENCES app.membership_plans(id),
  start_at    TIMESTAMPTZ NOT NULL,
  end_at      TIMESTAMPTZ NOT NULL,
  status      VARCHAR(20) NOT NULL CHECK (status IN ('active','expired','canceled','pending')),
  created_by  BIGINT REFERENCES app.accounts(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_person ON app.membership_subscriptions(person_id, status, end_at DESC);

CREATE TABLE IF NOT EXISTS app.payments (
  id                  BIGSERIAL PRIMARY KEY,
  subscription_id     BIGINT REFERENCES app.membership_subscriptions(id),
  person_id           BIGINT NOT NULL REFERENCES app.people(id),
  amount              NUMERIC(12,2) NOT NULL,
  paid_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  method              VARCHAR(40) NOT NULL, -- 'cash','card','mercadopago'
  provider            VARCHAR(40),
  provider_payment_id VARCHAR(120),
  external_reference  VARCHAR(120),
  status              VARCHAR(20) NOT NULL DEFAULT 'COMPLETED',
  comment             TEXT,
  recorded_by         BIGINT REFERENCES app.accounts(id),
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payments_person_paidat ON app.payments(person_id, paid_at DESC);

-- 2.3 Venues & Seats
CREATE TABLE IF NOT EXISTS app.venues (
  id          BIGSERIAL PRIMARY KEY,
  name        VARCHAR(120) NOT NULL,
  description TEXT,
  capacity    INT NOT NULL CHECK (capacity > 0),
  address     VARCHAR(200),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.seat_types (
  id          BIGSERIAL PRIMARY KEY,
  code        VARCHAR(50) UNIQUE NOT NULL,  -- 'bike','mat','reformer','step'
  name        VARCHAR(100) NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS app.seats (
  id           BIGSERIAL PRIMARY KEY,
  venue_id     BIGINT NOT NULL REFERENCES app.venues(id) ON DELETE CASCADE,
  label        VARCHAR(50) NOT NULL,
  row_number   INT,
  col_number   INT,
  is_active    BOOLEAN NOT NULL DEFAULT TRUE,
  seat_type_id BIGINT REFERENCES app.seat_types(id),
  UNIQUE (venue_id, label)
);

-- 2.4 Equipment & Maintenance
CREATE TABLE IF NOT EXISTS app.asset_types (
  id          BIGSERIAL PRIMARY KEY,
  code        VARCHAR(50) UNIQUE NOT NULL,  -- 'spin_bike','rower','treadmill','reformer'
  name        VARCHAR(100) NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS app.asset_models (
  id                           BIGSERIAL PRIMARY KEY,
  asset_type_id                BIGINT NOT NULL REFERENCES app.asset_types(id),
  brand                        VARCHAR(80),
  model_name                   VARCHAR(120),
  maintenance_interval_days    INT,
  maintenance_interval_classes INT,
  notes                        TEXT
);

CREATE TABLE IF NOT EXISTS app.assets (
  id            BIGSERIAL PRIMARY KEY,
  asset_model_id BIGINT NOT NULL REFERENCES app.asset_models(id),
  serial_number VARCHAR(120) UNIQUE,
  purchase_date DATE,
  status        VARCHAR(20) NOT NULL DEFAULT 'in_service'
                CHECK (status IN ('in_service','maintenance','retired')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  retired_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app.asset_seat_assignments (
  asset_id      BIGINT NOT NULL REFERENCES app.assets(id) ON DELETE CASCADE,
  seat_id       BIGINT NOT NULL REFERENCES app.seats(id)  ON DELETE CASCADE,
  assigned_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  unassigned_at TIMESTAMPTZ,
  PRIMARY KEY (asset_id, assigned_at),
  CHECK (unassigned_at IS NULL OR unassigned_at > assigned_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_active_assignment ON app.asset_seat_assignments(asset_id) WHERE unassigned_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_seat_active_asset      ON app.asset_seat_assignments(seat_id)  WHERE unassigned_at IS NULL;

CREATE TABLE IF NOT EXISTS app.asset_events (
  id           BIGSERIAL PRIMARY KEY,
  asset_id     BIGINT NOT NULL REFERENCES app.assets(id) ON DELETE CASCADE,
  event_type   VARCHAR(20) NOT NULL CHECK (event_type IN ('maintenance','repair','inspection','incident')),
  performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  notes        TEXT,
  cost         NUMERIC(12,2),
  created_by   BIGINT REFERENCES app.accounts(id)
);

-- 2.5 Classes
CREATE TABLE IF NOT EXISTS app.class_types (
  id          BIGSERIAL PRIMARY KEY,
  code        VARCHAR(50) UNIQUE NOT NULL,  -- 'spinning','yoga','pilates','zumba'
  name        VARCHAR(120) NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS app.class_templates (
  id                   BIGSERIAL PRIMARY KEY,
  class_type_id        BIGINT NOT NULL REFERENCES app.class_types(id),
  venue_id             BIGINT NOT NULL REFERENCES app.venues(id),
  default_capacity     INT,
  default_duration_min INT NOT NULL CHECK (default_duration_min BETWEEN 15 AND 240),
  weekday              INT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
  start_time_local     TIME NOT NULL,
  instructor_id        BIGINT REFERENCES app.people(id),
  name                 VARCHAR(120),
  is_active            BOOLEAN NOT NULL DEFAULT TRUE,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.class_sessions (
  id            BIGSERIAL PRIMARY KEY,
  class_type_id BIGINT NOT NULL REFERENCES app.class_types(id),
  venue_id      BIGINT NOT NULL REFERENCES app.venues(id),
  template_id   BIGINT REFERENCES app.class_templates(id),
  instructor_id BIGINT REFERENCES app.people(id),
  name          VARCHAR(120),
  start_at      TIMESTAMPTZ NOT NULL,
  end_at        TIMESTAMPTZ NOT NULL,
  capacity      INT NOT NULL CHECK (capacity > 0),
  status        VARCHAR(20) NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','canceled','completed')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_time       ON app.class_sessions(start_at, venue_id);
CREATE INDEX IF NOT EXISTS idx_sessions_instructor ON app.class_sessions(instructor_id, start_at DESC);

-- 2.6 Reservations & Fixed Slot
CREATE TABLE IF NOT EXISTS app.reservations (
  id           BIGSERIAL PRIMARY KEY,
  session_id   BIGINT NOT NULL REFERENCES app.class_sessions(id) ON DELETE CASCADE,
  person_id    BIGINT NOT NULL REFERENCES app.people(id),
  seat_id      BIGINT REFERENCES app.seats(id),
  status       VARCHAR(20) NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','waitlisted','canceled','checked_in','no_show')),
  reserved_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  checkin_at   TIMESTAMPTZ,
  checkout_at  TIMESTAMPTZ,
  waitlist_position INT,
  idempotency_key   VARCHAR(120),
  source      VARCHAR(20) NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','standing','override')),
  UNIQUE (session_id, person_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_seat_once
  ON app.reservations(session_id, seat_id)
  WHERE seat_id IS NOT NULL AND status IN ('reserved','checked_in');

CREATE TABLE IF NOT EXISTS app.standing_bookings (
  id              BIGSERIAL PRIMARY KEY,
  person_id       BIGINT NOT NULL REFERENCES app.people(id),
  subscription_id BIGINT NOT NULL REFERENCES app.membership_subscriptions(id) ON DELETE CASCADE,
  template_id     BIGINT NOT NULL REFERENCES app.class_templates(id),
  seat_id         BIGINT REFERENCES app.seats(id),
  start_date      DATE NOT NULL,
  end_date        DATE NOT NULL,
  status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','canceled')),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (person_id, subscription_id, template_id, status)
);

CREATE TABLE IF NOT EXISTS app.standing_booking_exceptions (
  id                  BIGSERIAL PRIMARY KEY,
  standing_booking_id BIGINT NOT NULL REFERENCES app.standing_bookings(id) ON DELETE CASCADE,
  session_date        DATE NOT NULL,
  action              VARCHAR(20) NOT NULL CHECK (action IN ('skip','reschedule')),
  new_session_id      BIGINT REFERENCES app.class_sessions(id),
  notes               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (standing_booking_id, session_date)
);

COMMIT;
```

---

## 3) Seed Data (Roles, Class Types, Seat/Asset Types)
```sql
INSERT INTO app.roles(code, description) VALUES
  ('member','Gym member'),('instructor','Class instructor'),('staff','Front desk'),('admin','Administrator')
ON CONFLICT (code) DO NOTHING;

INSERT INTO app.class_types(code, name) VALUES
  ('spinning','Spinning'),('yoga','Yoga'),('pilates','Pilates'),('zumba','Zumba')
ON CONFLICT (code) DO NOTHING;

INSERT INTO app.seat_types(code, name) VALUES
  ('bike','Bike'),('mat','Mat'),('reformer','Reformer'),('step','Step')
ON CONFLICT (code) DO NOTHING;

INSERT INTO app.asset_types(code, name) VALUES
  ('spin_bike','Spin Bike'),('rower','Rower'),('treadmill','Treadmill'),('reformer','Reformer')
ON CONFLICT (code) DO NOTHING;
```

---

## 4) Mapping Matrix (Legacy → Target)
| Legacy | Target |
|---|---|
| `personas` | `people` (+ dedup via phone/email) |
| `members` | `people` (role=member), `membership_subscriptions`, `payments` |
| `users` | `people` (role=staff/admin/instructor), `accounts` |
| `memberships` | `membership_plans` (plus rule columns) |
| `pagos` | `payments` (link to `person_id` and `subscription_id`) |
| `bicicletas` | `venues` (capacity) + `seats` (labels) + `seat_types` (bike) + **optionally** `assets` |
| `classes` | `class_templates` (recurrence) **and/or** `class_sessions` (instances) |
| `reservas` | `reservations` (session/person/seat) + (derived) `standing_bookings` if weekly fixed |

> If your legacy `classes` already stored real dates, migrate them to `class_sessions`; otherwise, build `class_templates` and generate future `class_sessions` programmatically.

---

## 5) Identity Unification
### 5.1 Build `people`
- Strategy: **deduplicate** using `phone_number` and/or `email`. Prefer `personas` as the base.

```sql
-- Example: create people from personas
INSERT INTO app.people(full_name, phone_number, email, wa_id)
SELECT DISTINCT p.nombre_origen, p.telefono, p.email, p.wa_id
FROM public.personas p
ON CONFLICT DO NOTHING;  -- if you have a unique constraint, add it first
```

> If both `members` and `users` contain additional unique persons not in `personas`, insert those missing rows into `people` too.

### 5.2 Roles
```sql
-- Members → role member
INSERT INTO app.person_roles(person_id, role_id)
SELECT pe.id, r.id
FROM public.members m
JOIN app.people pe ON pe.phone_number = m.telefono OR pe.email = m.email
JOIN app.roles r ON r.code = 'member'
ON CONFLICT DO NOTHING;

-- Users → staff/admin/instructor according to your legacy role mapping
-- Repeat for each role code you use
```

### 5.3 Accounts
```sql
INSERT INTO app.accounts(person_id, username, password_hash, is_active)
SELECT pe.id, u.username, u.password_hash, u.is_active
FROM public.users u
JOIN app.people pe ON pe.email = u.email OR pe.phone_number = u.phone
ON CONFLICT (username) DO NOTHING;
```

> Adjust joins to your real legacy columns. If you don’t store password hashes, migrate as inactive and trigger reset flows.

---

## 6) Membership Plans & Subscriptions
### 6.1 Plans
```sql
INSERT INTO app.membership_plans(name, description, price, duration_value, duration_unit,
                                 class_limit, fixed_time_slot, max_sessions_per_day, max_sessions_per_week)
SELECT name, description, price, duration_value, duration_unit,
       class_limit, fixed_time_slot, max_sessions_per_day, max_sessions_per_week
FROM public.memberships;  -- adapt column names
```

### 6.2 Subscriptions (derive from legacy validity)
```sql
-- Example: create subscriptions from members + their current plan windows
INSERT INTO app.membership_subscriptions(person_id, plan_id, start_at, end_at, status, created_by)
SELECT pe.id, mp.id,
       m.start_at, m.end_at,
       CASE WHEN m.end_at >= NOW() THEN 'active' ELSE 'expired' END,
       NULL
FROM public.members m
JOIN app.people pe ON pe.phone_number = m.telefono OR pe.email = m.email
JOIN app.membership_plans mp ON mp.name = m.plan_name;  -- ensure a reliable key
```

### 6.3 Payments
```sql
INSERT INTO app.payments(subscription_id, person_id, amount, paid_at, method, provider,
                         provider_payment_id, external_reference, status, comment, recorded_by)
SELECT ms.id, pe.id, p.amount, p.paid_at, p.method, p.provider,
       p.provider_payment_id, p.external_reference, COALESCE(p.status,'COMPLETED'), p.comment, acc.id
FROM public.pagos p
JOIN app.people pe ON pe.phone_number = p.telefono OR pe.email = p.email
LEFT JOIN app.accounts acc ON acc.username = p.recorded_by_username
LEFT JOIN app.membership_subscriptions ms ON ms.person_id = pe.id
  AND p.paid_at BETWEEN ms.start_at AND ms.end_at;   -- best-effort link; otherwise leave NULL and backfill
```

> If you can’t reliably attach `subscription_id`, you may leave it `NULL` initially and backfill by date-range logic or reconciliation jobs.

---

## 7) Venues, Seats & (Optional) Assets
### 7.1 Venues & Seats
```sql
-- Example: create a Spin Studio with 14 seats
INSERT INTO app.venues(name, description, capacity) VALUES ('Spin Studio','Indoor cycling room',14)
RETURNING id;

-- Seed seat types (if not already): see Section 3

-- Create seats (Bike 1..14)
WITH v AS (
  SELECT id FROM app.venues WHERE name='Spin Studio'
)
INSERT INTO app.seats(venue_id, label, row_number, col_number, is_active, seat_type_id)
SELECT v.id, 'Bike '||n, NULL, NULL, TRUE,
       (SELECT id FROM app.seat_types WHERE code='bike')
FROM v, generate_series(1,14) AS n;
```

### 7.2 Assets (optional but recommended for maintenance)
```sql
-- Asset model for your spin bikes
INSERT INTO app.asset_models(asset_type_id, brand, model_name, maintenance_interval_days, maintenance_interval_classes)
SELECT at.id, 'Keiser', 'M3', 90, 50 FROM app.asset_types at WHERE at.code='spin_bike'
RETURNING id;

-- Create N assets (serials optional)
WITH m AS (
  SELECT id FROM app.asset_models WHERE model_name='M3'
)
INSERT INTO app.assets(asset_model_id, serial_number)
SELECT m.id, 'SN-'||n FROM m, generate_series(1,14) AS n;

-- Assign each asset to a seat 1:1
WITH s AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY label) AS rn FROM app.seats WHERE seat_type_id=(SELECT id FROM app.seat_types WHERE code='bike')
), a AS (
  SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM app.assets
)
INSERT INTO app.asset_seat_assignments(asset_id, seat_id)
SELECT a.id, s.id FROM a JOIN s USING (rn);
```

---

## 8) Class Templates & Sessions
### 8.1 Templates (recurring schedule)
```sql
-- Example: Spinning Mon-Fri 19:00, 60 min, instructor bound
INSERT INTO app.class_templates(class_type_id, venue_id, default_capacity, default_duration_min, weekday, start_time_local, instructor_id, name)
SELECT ct.id, v.id, v.capacity, 60, wd.dow, '19:00', p.id, 'Evening Spin'
FROM app.class_types ct, app.venues v, app.people p,
LATERAL (VALUES (1),(2),(3),(4),(5)) AS wd(dow)
WHERE ct.code='spinning' AND v.name='Spin Studio' AND p.full_name='Laura Torres'  -- adjust instructor
ON CONFLICT DO NOTHING;
```

### 8.2 Generate Sessions (next 6–8 weeks)
> Use your timezone to build `start_at` and `end_at` from `weekday` + `start_time_local` + `default_duration_min`.

```sql
-- Pseudocode (run from app or SQL function):
-- For each active template t:
--   For each date in next 8 weeks matching t.weekday:
--     start_at = make_timestamptz(date, t.start_time_local)
--     end_at   = start_at + (t.default_duration_min || ' min')::interval
--     INSERT INTO class_sessions (...)
```

---

## 9) Reservations Migration
### 9.1 Legacy reservations → `reservations`
```sql
INSERT INTO app.reservations(session_id, person_id, seat_id, status, reserved_at, source)
SELECT cs.id, pe.id, s.id,
       CASE r.status WHEN 'cancelada' THEN 'canceled' WHEN 'asistio' THEN 'checked_in' ELSE 'reserved' END,
       COALESCE(r.created_at, NOW()), 'manual'
FROM public.reservas r
JOIN app.people pe ON pe.phone_number = r.telefono OR pe.email = r.email
JOIN app.class_sessions cs ON cs.start_at = r.fecha_inicio AND cs.end_at = r.fecha_fin  -- adjust if you have IDs
LEFT JOIN app.seats s ON s.label = r.bicicleta_label AND s.venue_id = (SELECT id FROM app.venues WHERE name='Spin Studio');
```

> If your legacy `classes` used IDs rather than timestamps, translate via a temporary mapping table.

### 9.2 Standing bookings (fixed slot weekly plans)
```sql
-- Create a standing booking per active weekly subscription + chosen template
INSERT INTO app.standing_bookings(person_id, subscription_id, template_id, seat_id, start_date, end_date)
SELECT ms.person_id, ms.id, t.id, s.id,
       ms.start_at::date, ms.end_at::date
FROM app.membership_subscriptions ms
JOIN app.membership_plans mp ON mp.id = ms.plan_id AND mp.fixed_time_slot = TRUE
JOIN public.members m ON m.plan_name = mp.name AND m.telefono = (SELECT phone_number FROM app.people WHERE id = ms.person_id)
JOIN app.class_templates t ON t.id = m.template_id        -- ensure you have this mapping
LEFT JOIN app.seats s ON s.id = m.seat_id;
```

### 9.3 Auto-generate future reservations for standing bookings
```sql
INSERT INTO app.reservations (session_id, person_id, seat_id, status, reserved_at, source)
SELECT cs.id, sb.person_id, sb.seat_id, 'reserved', NOW(), 'standing'
FROM app.standing_bookings sb
JOIN app.class_templates t   ON t.id = sb.template_id
JOIN app.class_sessions cs   ON cs.template_id = t.id
LEFT JOIN app.reservations r ON r.session_id = cs.id AND r.person_id = sb.person_id
WHERE sb.status = 'active'
  AND cs.start_at::date BETWEEN sb.start_date AND sb.end_date
  AND cs.start_at::date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '8 weeks')::date
  AND r.id IS NULL
  AND (
       (sb.seat_id IS NULL AND (
          SELECT COUNT(*) FROM app.reservations rr
          WHERE rr.session_id = cs.id AND rr.status IN ('reserved','checked_in')
       ) < cs.capacity)
       OR
       (sb.seat_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM app.reservations rx
          WHERE rx.session_id = cs.id AND rx.seat_id = sb.seat_id AND rx.status IN ('reserved','checked_in')
       ))
      );
```

---

## 10) Constraints, Indexes & Housekeeping
- Ensure the unique partial index on `(session_id, seat_id)` for reserved/checked-in.
- Ensure `(session_id, person_id)` unique to avoid duplicates.
- Consider `ON DELETE CASCADE` on `class_sessions → reservations` only if you are comfortable deleting dependent rows.
- Add **triggers** to update `updated_at` columns on `INSERT/UPDATE` if you want audit freshness.

---

## 11) Validation & Integrity Checks
Use these **post-migration checks** before cutover.

```sql
-- 11.1 Counts match (people)
SELECT (SELECT COUNT(*) FROM app.people)    AS new_people,
       (SELECT COUNT(DISTINCT telefono) FROM public.personas) AS legacy_contacts;

-- 11.2 Subscriptions window sanity
SELECT COUNT(*) FROM app.membership_subscriptions WHERE start_at >= end_at; -- should be 0

-- 11.3 Orphans
SELECT COUNT(*) FROM app.reservations r LEFT JOIN app.class_sessions cs ON cs.id=r.session_id WHERE cs.id IS NULL; -- 0
SELECT COUNT(*) FROM app.reservations r LEFT JOIN app.people p ON p.id=r.person_id WHERE p.id IS NULL;           -- 0

-- 11.4 Double-booked seats (should be 0 thanks to unique index)
SELECT session_id, seat_id, COUNT(*) FROM app.reservations
WHERE seat_id IS NOT NULL AND status IN ('reserved','checked_in')
GROUP BY session_id, seat_id HAVING COUNT(*)>1;

-- 11.5 Capacity overflows
SELECT cs.id, COUNT(*) AS reserved, cs.capacity
FROM app.class_sessions cs
JOIN app.reservations r ON r.session_id=cs.id AND r.status IN ('reserved','checked_in')
GROUP BY cs.id, cs.capacity
HAVING COUNT(*) > cs.capacity;
```

---

## 12) Cutover Plan (Minimal Downtime)
1. **Freeze writes** in legacy app (read-only mode).  
2. Run Sections 2–9.  
3. Run Section 11 validation.  
4. Point application to new schema/tables (migrate ORM models/migrations).  
5. Unfreeze writes.  
6. Monitor errors/slow queries.

> Optional: keep legacy tables **read-only archived** for a few cycles, or create **compatibility views**.

### 12.1 Compatibility Views (Optional)
```sql
-- Example: expose a legacy-like view for reservas
CREATE OR REPLACE VIEW public.v_reservas AS
SELECT r.id, cs.start_at AS fecha_inicio, cs.end_at AS fecha_fin,
       p.full_name, p.phone_number, s.label AS bicicleta_label, r.status
FROM app.reservations r
JOIN app.class_sessions cs ON cs.id=r.session_id
JOIN app.people p ON p.id=r.person_id
LEFT JOIN app.seats s ON s.id=r.seat_id;
```

---

## 13) Rollback Plan
- If any critical validation fails, **do not cut over**. Restore from backup:
```bash
pg_restore -h $PGHOST -U $PGUSER -d $PGDATABASE --clean --if-exists ./pre_migration.dump
```
- Or, if running in a single transaction, `ROLLBACK;` will revert uncommitted DDL/DML.

---

## 14) Automation Hooks (CLI/Agent)
- Expose **idempotent** scripts: run-safe multiple times.
- Order:
  1. `00_backup.sh`
  2. `10_create_schema.sql`
  3. `20_seed_catalogs.sql`
  4. `30_migrate_people.sql`
  5. `40_migrate_memberships.sql`
  6. `50_migrate_venues_seats_assets.sql`
  7. `60_migrate_classes_sessions.sql`
  8. `70_migrate_reservations.sql`
  9. `80_generate_standing_reservations.sql`
  10. `90_validate.sql`
  11. `99_cutover.sh`

- Include **dry-run** mode: only `SELECT COUNT(*)` and `EXPLAIN` without mutating data.

---

## 15) Post-Migration Tasks
- Rebuild ORM models (SQLAlchemy/Prisma/etc.) with English names.
- Add background job (daily) to **generate sessions** from templates for the next 6–8 weeks.
- Ensure standing bookings **create reservations immediately** on renewal/enrollment (no scheduled materialization job).
- Create dashboards: attendance, capacity utilization, MRR from `payments`.
- Set up alerts for maintenance thresholds using `asset_events` and model intervals.

---

## Appendix A: Helper — Change Fixed Slot (Definitive)
```sql
-- Update a standing booking to a new template/seat
UPDATE app.standing_bookings
SET template_id = $new_template_id,
    seat_id     = $new_seat_id,
    updated_at  = NOW()
WHERE id = $standing_booking_id;

-- Optionally cancel future reservations from old template and generate new ones (reuse 9.3)
```

## Appendix B: Helper — One-off Reschedule
```sql
-- Cancel the original day’s reservation
UPDATE app.reservations
SET status='canceled'
WHERE person_id = $person_id
  AND session_id IN (
      SELECT cs.id FROM app.class_sessions cs
      JOIN app.class_templates t ON t.id = $template_id
      WHERE cs.start_at::date = $session_date
  )
  AND status IN ('reserved','checked_in');

-- Create the override reservation (if capacity ok)
INSERT INTO app.reservations(session_id, person_id, seat_id, status, reserved_at, source)
VALUES ($new_session_id, $person_id, $new_seat_id, 'reserved', NOW(), 'override');
```

## Appendix C: Sample Seat Availability Query
```sql
SELECT cs.id AS session_id,
       cs.capacity - COALESCE(SUM(CASE WHEN r.status IN ('reserved','checked_in') THEN 1 ELSE 0 END),0) AS spots_left
FROM app.class_sessions cs
LEFT JOIN app.reservations r ON r.session_id = cs.id
WHERE cs.id = $session_id
GROUP BY cs.id, cs.capacity;
```

---

**End of Playbook**
