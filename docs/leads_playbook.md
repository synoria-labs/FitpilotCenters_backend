# Leads & Marketing Capture Playbook (WhatsApp + Landing) — Schema `app`

**Objetivo:** Ingresar y orquestar clientes potenciales provenientes de **WhatsApp (WABA/chatbot)** y **Landing Pages** (Meta/Google), unificar identidad en `people`, manejar estado de *lead* y su **conversión** a *member* con atribución de campañas — **sin romper** el playbook de base ya implementado.

**Contexto del core ya existente (resumen):**
- Identidad y autenticación: `people`, `roles`, `person_roles`, `accounts`.
- Membresías y pagos: `membership_plans` (con reglas), `membership_subscriptions`, `payments`.
- Clases: `class_types`, `class_templates` (recurrencia), `class_sessions` (instancias), `reservations`.
- Seats y mantenimiento: `venues`, `seat_types`, `seats`, `assets` + `asset_events`, `asset_seat_assignments`.
- Reservas fijas por plan: `standing_bookings` + `standing_booking_exceptions`.

---

## 1) Principios de diseño para Leads
- **Una sola identidad** → todo contacto vive en `app.people` (aunque sea lead).  
  Asigna el **rol `lead`** en `app.person_roles` mientras no sea socio.
- **Fuente y estado del lead** → en `app.leads` con `status` y `source` normalizados.
- **Eventos/touchpoints** → en `app.lead_events` (mensajes, formularios, cambios de estado, intentos de pago).
- **Atribución** (UTM/ads) → `app.marketing_campaigns` + `app.lead_attributions` (multi-touch) o graba UTM directo en `form_submissions`.
- **Consentimientos** (WhatsApp/email/SMS) → `app.communications_opt_in`.
- **Conversación WA (opcional, resumen)** → `app.whatsapp_threads` si quieres guardar metadatos mínimos.
- **Conversión** → agregar rol `member`, crear `membership_subscription`, marcar lead `converted` y (si aplica) `standing_bookings`.

---

## 2) DDL idempotente (Extensiones, catálogos y tablas)
> Ejecuta con un usuario con permisos DDL. Todos los nombres en inglés, como en el playbook base.

```sql
-- (Opcional) Emails case-insensitive
CREATE EXTENSION IF NOT EXISTS citext;

-- 2.1 Lead sources
CREATE TABLE IF NOT EXISTS app.lead_sources (
  id          BIGSERIAL PRIMARY KEY,
  code        VARCHAR(40) UNIQUE NOT NULL,   -- 'whatsapp','landing','instagram','phone','referral'
  name        VARCHAR(100) NOT NULL
);

-- 2.2 Leads (estado del funnel)
CREATE TABLE IF NOT EXISTS app.leads (
  id                BIGSERIAL PRIMARY KEY,
  person_id         BIGINT NOT NULL REFERENCES app.people(id) ON DELETE CASCADE,
  source_id         BIGINT NOT NULL REFERENCES app.lead_sources(id),
  status            VARCHAR(20) NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','contacted','qualified','converted','lost','disqualified')),
  score             INT,
  owner_account_id  BIGINT REFERENCES app.accounts(id),
  notes             TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  converted_at      TIMESTAMPTZ
);

-- Evita duplicados ACTIVOS por persona + fuente (permite histórico al cerrar estado)
CREATE UNIQUE INDEX IF NOT EXISTS uq_lead_active_per_source
  ON app.leads(person_id, source_id)
  WHERE status IN ('new','contacted','qualified');

-- 2.3 Lead events (touchpoints)
CREATE TABLE IF NOT EXISTS app.lead_events (
  id           BIGSERIAL PRIMARY KEY,
  lead_id      BIGINT NOT NULL REFERENCES app.leads(id) ON DELETE CASCADE,
  event_type   VARCHAR(30) NOT NULL
               CHECK (event_type IN ('message_in','message_out','form_submit','status_change','reservation','payment_attempt','note')),
  event_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  payload      JSONB,
  created_by   BIGINT REFERENCES app.accounts(id)
);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead_at ON app.lead_events(lead_id, event_at DESC);

-- 2.4 Form submissions (landing/forms)
CREATE TABLE IF NOT EXISTS app.form_submissions (
  id              BIGSERIAL PRIMARY KEY,
  person_id       BIGINT NOT NULL REFERENCES app.people(id) ON DELETE CASCADE,
  form_id         VARCHAR(80),
  form_name       VARCHAR(120),
  submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  landing_url     TEXT,
  referrer_url    TEXT,
  utm_source      VARCHAR(80),
  utm_medium      VARCHAR(80),
  utm_campaign    VARCHAR(120),
  utm_term        VARCHAR(120),
  utm_content     VARCHAR(120),
  gclid           VARCHAR(200),
  fbclid          VARCHAR(200),
  payload         JSONB
);
CREATE INDEX IF NOT EXISTS idx_form_submissions_person ON app.form_submissions(person_id, submitted_at DESC);

-- 2.5 Campaigns & attributions (opcional, útil para reporting avanzado)
CREATE TABLE IF NOT EXISTS app.marketing_campaigns (
  id             BIGSERIAL PRIMARY KEY,
  platform       VARCHAR(30),             -- 'meta','google','tiktok','email'
  name           VARCHAR(160) NOT NULL,
  channel        VARCHAR(30),             -- 'ads','email','organic','referral'
  external_id    VARCHAR(120),
  start_at       TIMESTAMPTZ,
  end_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS app.lead_attributions (
  id             BIGSERIAL PRIMARY KEY,
  lead_id        BIGINT NOT NULL REFERENCES app.leads(id) ON DELETE CASCADE,
  campaign_id    BIGINT REFERENCES app.marketing_campaigns(id),
  utm_source     VARCHAR(80),
  utm_medium     VARCHAR(80),
  utm_campaign   VARCHAR(120),
  utm_term       VARCHAR(120),
  utm_content    VARCHAR(120),
  landing_url    TEXT,
  click_at       TIMESTAMPTZ,
  referrer_url   TEXT,
  gclid          VARCHAR(200),
  fbclid         VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS idx_lead_attr_lead ON app.lead_attributions(lead_id);

-- 2.6 Consentimientos por canal
CREATE TABLE IF NOT EXISTS app.communications_opt_in (
  id            BIGSERIAL PRIMARY KEY,
  person_id     BIGINT NOT NULL REFERENCES app.people(id) ON DELETE CASCADE,
  channel       VARCHAR(20) NOT NULL CHECK (channel IN ('whatsapp','email','sms')),
  granted_at    TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,
  source        VARCHAR(80),      -- 'form','whatsapp','manual','import'
  evidence      JSONB             -- captura del checkbox, ip, message_id, etc.
);
CREATE INDEX IF NOT EXISTS idx_optin_person_channel ON app.communications_opt_in(person_id, channel);

-- 2.7 (Opcional) Resumen de conversación WhatsApp por persona
CREATE TABLE IF NOT EXISTS app.whatsapp_threads (
  id                BIGSERIAL PRIMARY KEY,
  person_id         BIGINT NOT NULL REFERENCES app.people(id) ON DELETE CASCADE,
  wa_id             VARCHAR(100),
  phone_e164        VARCHAR(32),
  last_inbound_at   TIMESTAMPTZ,
  last_outbound_at  TIMESTAMPTZ,
  last_message_snippet TEXT,
  is_open           BOOLEAN NOT NULL DEFAULT TRUE,
  provider          VARCHAR(40)  -- 'meta','twilio','360dialog', etc.
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wa_thread_person ON app.whatsapp_threads(person_id);
```

**Notas de identidad (muy importantes):**
- Normaliza teléfonos a **E.164** antes del upsert.
- Para emails, usa `CITEXT` y un índice único parcial: `email IS NOT NULL`.
- Fallback UI: muestra `COALESCE(people.full_name, people.phone_number, people.email)`.

---

## 3) Flujos por canal

### 3.1 WhatsApp (WABA/chatbot con reserva/pago)
- **Upsert de identidad** en `people` por `phone_e164` y/o `wa_id`.  
  Si no hay nombre aún: `full_name = NULL`.  
- **Rol**: inserta `lead` en `person_roles` (no quites luego, sirve para analítica).
- **Lead**: crea/recupera fila en `leads` con `source='whatsapp'` (`status='new'` o `contacted`).
- **Eventos**: graba `lead_events` (`message_in/out`, `reservation`, `payment_attempt`) con `payload` JSONB (ids de mensaje, plantilla, etc.).
- **Consentimiento**: `communications_opt_in(channel='whatsapp', evidence=message_id/timestamp)`.
- **Hilo** (opcional): `whatsapp_threads` para `last_inbound_at`, `last_outbound_at`, snippet.

**Upsert ejemplo (simplificado):**
```sql
-- 1) people (por teléfono WA)
INSERT INTO app.people(full_name, phone_number, wa_id)
VALUES (NULL, $E164, $WA_ID)
ON CONFLICT (phone_number) DO UPDATE
  SET wa_id = EXCLUDED.wa_id,
      updated_at = NOW()
RETURNING id;

-- 2) rol lead
INSERT INTO app.person_roles(person_id, role_id)
SELECT $person_id, r.id FROM app.roles r WHERE r.code='lead'
ON CONFLICT DO NOTHING;

-- 3) lead por fuente 'whatsapp'
INSERT INTO app.leads(person_id, source_id, status)
SELECT $person_id, s.id, 'new' FROM app.lead_sources s WHERE s.code='whatsapp'
ON CONFLICT ON CONSTRAINT uq_lead_active_per_source
DO UPDATE SET updated_at = NOW()
RETURNING id;

-- 4) evento de mensaje entrante
INSERT INTO app.lead_events(lead_id, event_type, payload)
VALUES ($lead_id, 'message_in', $json_payload);
```

**Conversión automática (si paga en el flujo):**
1) `UPDATE leads SET status='converted', converted_at=NOW()`  
2) `INSERT person_roles (...) VALUES (person,'member') ON CONFLICT DO NOTHING`  
3) `INSERT membership_subscriptions` + `INSERT payments`  
4) Si `fixed_time_slot=true`, crear `standing_bookings` y **materializar** reservas futuras (ver core).

---

### 3.2 Landing Page (Meta/Google + formularios)
- **Upsert `people`** por `email` (y/o teléfono si viene).  
- **Form capture**: `form_submissions` con **UTMs/gclid/fbclid**, `landing_url`, `referrer_url` y `payload` crudo.  
- **Lead**: `leads` con `source='landing'` (`status='new'`).  
- **Atribución**: (opcional) `marketing_campaigns` + `lead_attributions` embebiendo UTMs para reporte cross-canal.  
- **Consentimiento**: registra `email/sms` cuando aplique (checkbox/IP en `evidence`).

---

## 4) Migración desde `public.leads` (ejemplo orientativo)
> Ajusta nombres reales de columnas (`origen`, `utm_*`, `gclid`, `fbclid`, `form_id`, etc.).

```sql
-- 4.1 Sembrar fuentes
INSERT INTO app.lead_sources(code, name) VALUES
  ('whatsapp','WhatsApp'), ('landing','Landing Page')
ON CONFLICT (code) DO NOTHING;

-- 4.2 Subir personas desde leads (WA prioriza teléfono; Landing prioriza email)
-- WhatsApp
INSERT INTO app.people(full_name, phone_number, wa_id)
SELECT NULL, l.phone_e164, l.wa_id
FROM public.leads l
WHERE l.source = 'whatsapp'
ON CONFLICT (phone_number) DO UPDATE
  SET wa_id = EXCLUDED.wa_id, updated_at = NOW();

-- Landing
INSERT INTO app.people(full_name, email, phone_number)
SELECT COALESCE(l.full_name, NULL), l.email, l.phone_e164
FROM public.leads l
WHERE l.source = 'landing'
ON CONFLICT (email) DO UPDATE
  SET phone_number = COALESCE(app.people.phone_number, EXCLUDED.phone_number),
      updated_at   = NOW();

-- 4.3 Rol lead
INSERT INTO app.person_roles(person_id, role_id)
SELECT p.id, r.id
FROM app.people p
JOIN app.roles r ON r.code='lead'
LEFT JOIN app.person_roles pr ON pr.person_id=p.id AND pr.role_id=r.id
WHERE pr.person_id IS NULL;

-- 4.4 Crear leads por fuente (evita duplicados activos)
INSERT INTO app.leads(person_id, source_id, status, notes)
SELECT p.id,
       (SELECT id FROM app.lead_sources WHERE code=l.source),
       COALESCE(l.status_mapped,'new'),
       l.notes
FROM public.leads l
JOIN app.people p
  ON (l.source='whatsapp' AND p.phone_number=l.phone_e164)
  OR (l.source='landing'  AND p.email=l.email)
ON CONFLICT ON CONSTRAINT uq_lead_active_per_source
DO UPDATE SET updated_at = NOW();

-- 4.5 Formularios (si Landing traía payload y UTMs)
INSERT INTO app.form_submissions(person_id, form_id, form_name, submitted_at,
                                 landing_url, referrer_url,
                                 utm_source, utm_medium, utm_campaign, utm_term, utm_content,
                                 gclid, fbclid, payload)
SELECT p.id, l.form_id, l.form_name, l.created_at,
       l.landing_url, l.referrer_url,
       l.utm_source, l.utm_medium, l.utm_campaign, l.utm_term, l.utm_content,
       l.gclid, l.fbclid, l.payload_json
FROM public.leads l
JOIN app.people p
  ON (l.email IS NOT NULL AND p.email=l.email)
  OR (l.phone_e164 IS NOT NULL AND p.phone_number=l.phone_e164)
WHERE l.source='landing';
```

---

## 5) Calidad de datos (unicidad y normalización)
```sql
-- Unicidad por teléfono cuando no null (normalizar a E.164 en la app)
CREATE UNIQUE INDEX IF NOT EXISTS uq_people_phone
  ON app.people((phone_number)) WHERE phone_number IS NOT NULL;

-- Email case-insensitive y único si no null
ALTER TABLE app.people ALTER COLUMN email TYPE citext;
CREATE UNIQUE INDEX IF NOT EXISTS uq_people_email
  ON app.people(email) WHERE email IS NOT NULL;
```

**Recomendaciones:**
- No mezcles personas por homónimos (nombres). Prioriza `phone_number` y `email`.
- Si un lead llega por varias fuentes, **permite múltiples `app.leads`** por persona **pero solo una activa por fuente** (ya protegido por índice parcial).

---

## 6) Conversión Lead → Member (end-to-end)
**Evento de conversión típico**: pago exitoso o alta manual de suscripción.
```sql
-- A) Marcar lead convertido
UPDATE app.leads
SET status='converted', converted_at=NOW(), updated_at=NOW()
WHERE id=$lead_id;

-- B) Añadir rol member (sin quitar 'lead' si te sirve para analítica)
INSERT INTO app.person_roles(person_id, role_id)
SELECT $person_id, r.id FROM app.roles r WHERE r.code='member'
ON CONFLICT DO NOTHING;

-- C) Crear suscripción
INSERT INTO app.membership_subscriptions(person_id, plan_id, start_at, end_at, status)
VALUES ($person_id, $plan_id, $start_at, $end_at, 'active')
RETURNING id;

-- D) (Opcional) Standing booking si el plan es fixed_time_slot
-- Usa el generador de reservas futuras ya definido en el core
```

**Prueba gratis / Clase muestra:**
- Opción 1: plan “trial” (barato/$0) → crea `membership_subscription` corta + reservas.
- Opción 2: reserva “guest” sin suscripción, `reservations.source='override'` y regla en backend que limite frecuencia por `person_id` o `lead_id`.

---

## 7) Reporting listo para usar

**Leads por fuente y estado (últimos 30 días)**
```sql
SELECT s.code AS source, l.status, COUNT(*)
FROM app.leads l
JOIN app.lead_sources s ON s.id=l.source_id
WHERE l.created_at >= NOW() - INTERVAL '30 days'
GROUP BY s.code, l.status
ORDER BY s.code, l.status;
```

**Tasa de conversión por campaña**
```sql
SELECT mc.name AS campaign,
       COUNT(*) FILTER (WHERE l.status='converted')::DECIMAL / NULLIF(COUNT(*),0) AS conversion_rate
FROM app.leads l
JOIN app.lead_attributions la ON la.lead_id = l.id
JOIN app.marketing_campaigns mc ON mc.id = la.campaign_id
GROUP BY mc.name
ORDER BY conversion_rate DESC NULLS LAST;
```

**Ingresos atribuidos (primer toque)**
```sql
SELECT mc.name AS campaign, SUM(p.amount) AS revenue
FROM app.payments p
JOIN app.membership_subscriptions ms ON ms.id = p.subscription_id
JOIN app.leads l ON l.person_id = ms.person_id AND l.status='converted'
JOIN app.lead_attributions la ON la.lead_id = l.id
JOIN app.marketing_campaigns mc ON mc.id = la.campaign_id
GROUP BY mc.name
ORDER BY revenue DESC;
```

---

## 8) Checklist CLI/agent (orden recomendado)
1. Sembrar `lead_sources` + asegurar que existe el rol `lead` en `roles`.
2. Upsert `people` desde canal (WA/Landing).
3. `person_roles` → asignar `lead` (idempotente).
4. Crear/actualizar filas en `leads` + (Landing) `form_submissions`.
5. (Opcional) `marketing_campaigns` + `lead_attributions`.
6. Registrar `lead_events` por cada touchpoint.
7. Registrar `communications_opt_in` por canal con evidencia.
8. Probar **conversiones** end-to-end:
   - pago → `payments` + `membership_subscriptions`
   - si `fixed_time_slot=true` → `standing_bookings` + materialización de reservas.
9. Ejecutar queries de reporting (sección 7).
10. Habilitar tareas programadas (ETL de ads, normalización de teléfonos, etc.).

---

## 9) Apéndice — Reglas de normalización de teléfono (E.164)
- Elimina espacios, guiones y paréntesis.
- Asegura prefijo de país (ej. México `+52`).
- Valida longitud por país y que todos los caracteres sean dígitos (salvo `+`).

**Ejemplo (pseudocódigo):**
```text
raw = \"(871) 970-8890\"
digits = only_digits(raw) -> \"8719708890\"
if not starts_with_country_code(digits): digits = \"52\" + digits
e164 = \"+\" + digits  -- \"+528719708890\"
```

---

**Fin del Playbook de Leads**

