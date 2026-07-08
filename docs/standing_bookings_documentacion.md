# Standing Bookings (“Reservativos”) para Membresías con Horario Fijo
**Versión:** 1.0  
**Ámbito:** Backend de reservas de clases grupales (schema `app`)  
**Audiencia:** Desarrolladores, DevOps, analistas y soporte

---

## 1) Propósito
Los *standing bookings* (también llamados “reservativos”) son reglas permanentes que **aseguran un lugar** a una persona en un **horario recurrente** mientras su **suscripción** esté vigente.  
En la base de datos, esto se implementa con la tabla `app.standing_bookings` y se **materializa** en reservas reales por sesión (`app.reservations`).

**Objetivos principales:**
- Automatizar el “apartado” de lugar en clases recurrentes.
- Permitir **cambios puntuales** o **cambios definitivos** de horario sin romper el historial.
- Respetar **capacidad** y, si aplica, **asientos** (seats) únicos por sesión.

---

## 2) Alcance y antipatrones
**Lo que hacen:**
- Representan **reglas**: *persona + suscripción + horario (template) + seat opcional + ventana de fechas*.
- Generan reservas futuras (`reservations`) en cada `class_session` alineada con el `class_template`.

**Lo que NO hacen:**
- No reemplazan a `reservations`: cada asistencia concreta sigue siendo una fila en `app.reservations`.
- No cobran ni gestionan pagos (eso corresponde a `membership_subscriptions` y `payments`).
- No registran cambios puntuales; para eso está `app.standing_booking_exceptions`.

---

## 3) Entidades y relaciones básicas
- **people**: identidad de la persona (socio/miembro).
- **membership_subscriptions**: vigencia de la membresía (start/end/status).
- **class_templates**: horario recurrente (día de semana, hora local, venue, instructor).
- **class_sessions**: instancias con fecha/hora real, capacidad efectiva.
- **reservations**: reserva específica a una sesión concreta.
- **standing_bookings**: **regla** para “apartado” automático en el template elegido.
- **standing_booking_exceptions**: excepciones por fecha (p. ej., reprogramar un día).

---

## 4) `app.standing_bookings` — Campos clave
- `person_id` → `people.id`  
- `subscription_id` → `membership_subscriptions.id`  
- `template_id` → `class_templates.id`  
- `seat_id` (NULLable) → `seats.id` (solo si existe seating plan, p. ej. spinning)  
- `start_date` / `end_date` → rango de aplicabilidad de la regla  
- `status` → `active | paused | canceled`  
- `created_at` (y opcional `updated_at`)

**Invariante recomendada (único activo por slot):**
```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_sb_unique_active
  ON app.standing_bookings(person_id, subscription_id, template_id)
  WHERE status = 'active';
```

---

## 5) Ciclo de vida típico
1. **Alta/activación de plan fijo**  
   - Crear `membership_subscription` (vigencia).  
   - Insertar `standing_booking` con `template_id` (y `seat_id` si aplica).

2. **Materializacion de reservas** (inmediata al crear/renovar):  
   - Para cada `standing_booking.active`, generar `reservations` para las `class_sessions` correspondientes dentro de la ventana (p. ej., proximas 6-8 semanas, determinada por duration_value y duration_unit de membership_plans), respetando capacidad/seat.
   - Si no existen `class_sessions` para el template/grupo, se generan en ese momento segun la duracion de la suscripcion.

3. **Cambio puntual (un día)**  
   - No se toca `standing_bookings`.  
   - Registrar en `standing_booking_exceptions` con `action='reschedule'` y `new_session_id`.  
   - Cancelar la reserva original de ese día y crear una `reservation` *override* en la nueva sesión.

4. **Cambio definitivo de horario**  
   - Actualizar `template_id` (y `seat_id`) en `standing_bookings`.  
   - Rematerializar reservas futuras (y opcionalmente cancelar las del slot anterior).

5. **Pausa/Cancelación/Vencimiento**  
   - `status='paused'` o `canceled` detiene materialización futura.  
   - Al expirar `end_date` o la suscripción, cesa la generación automática.

6. **Renovación**  
   - Nueva `membership_subscription`.  
   - Clonar o reusar la regla con nuevo rango de fechas y rematerializar.

---

## 6) Algoritmo de materialización (idempotente)
**Objetivo:** Insertar reservas futuras sin duplicar y sin exceder la capacidad.

**Pasos:**
1. Seleccionar `standing_bookings` con `status='active'`.
2. Obtener `class_sessions` por `template_id` en `start_date..end_date` y dentro de la ventana deseada (p. ej., hoy..hoy+8 semanas).
3. Para cada sesión, **insertar** en `app.reservations` si:
   - No existe ya una `reservation` para `(session_id, person_id)` (idempotencia).  
   - Si **sin seat**: reservas actuales `< capacity`.  
   - Si **con seat**: ese `seat_id` no está tomado (índice parcial protege).

**SQL base (ilustrativo):**
```sql
INSERT INTO app.reservations (session_id, person_id, seat_id, status, reserved_at, source)
SELECT cs.id, sb.person_id, sb.seat_id, 'reserved', NOW(), 'standing'
FROM app.standing_bookings sb
JOIN app.class_templates t ON t.id = sb.template_id
JOIN app.class_sessions cs ON cs.template_id = t.id
LEFT JOIN app.reservations r ON r.session_id = cs.id AND r.person_id = sb.person_id
WHERE sb.status = 'active'
  AND cs.start_at::date BETWEEN sb.start_date AND sb.end_date
  AND cs.start_at::date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '8 weeks')::date
  AND r.id IS NULL
  AND (
       (sb.seat_id IS NULL
        AND (SELECT COUNT(*) FROM app.reservations rr
             WHERE rr.session_id = cs.id
               AND rr.status IN ('reserved','checked_in')) < cs.capacity)
       OR
       (sb.seat_id IS NOT NULL
        AND NOT EXISTS (
          SELECT 1 FROM app.reservations rx
          WHERE rx.session_id = cs.id
            AND rx.seat_id = sb.seat_id
            AND rx.status IN ('reserved','checked_in')
        ))
      );
```

**Buenas prácticas de concurrencia:**
- Usar transacciones con nivel `READ COMMITTED` o `REPEATABLE READ` según carga.  
- Ejecutar por lotes (paginación por `template_id`/fecha).  
- Colocar **índices** en `reservations(session_id, person_id)` y el **índice parcial** en `(session_id, seat_id)` para `status IN ('reserved','checked_in')`.

---

## 7) Reglas de capacidad y asientos
- **Sin seat** (yoga/pilates/zumba): controlar cupo con `cs.capacity` vs. número de reservas.  
- **Con seat** (spinning): requerir `seat_id` en el standing y validar exclusividad del asiento por sesión.

**Índice parcial recomendado:**
```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_seat_once
  ON app.reservations(session_id, seat_id)
  WHERE seat_id IS NOT NULL AND status IN ('reserved','checked_in');
```
Además:
```sql
ALTER TABLE app.reservations
  ADD CONSTRAINT uq_reservation_person_session UNIQUE (session_id, person_id);
```

---

## 8) Relación con suscripciones y pagos
- `standing_bookings.subscription_id` **ancla** la regla a una vigencia concreta.  
- La materialización **no debe** crear reservas fuera de `start_date..end_date` ni cuando `membership_subscriptions.status` no sea compatible (`active`).  
- Los pagos (`payments`) se registran aparte; este módulo **no** cobra, sólo reserva.

---

## 9) API de referencia (sugerencias)
- `POST /standing-bookings` → crea la regla fija (valida plan/plantilla/seat).  
- `PATCH /standing-bookings/{id}` → cambio definitivo de horario/seat o pausa/cancelación.  
- `POST /standing-bookings/{id}/exceptions` → cambio puntual (`reschedule`) o `skip` con fecha concreta.

**Nota:** La materializacion se ejecuta en los flujos de alta/renovacion y crea `class_sessions` si faltan; no hay job programado. Si se requiere reprocesar, usar el disparo manual (GraphQL `materializeStandingBookings`).

**Respuestas incluyen:** ids, ventanas de materialización, conteos de reservas creadas/omitidas, motivos de rechazo (sin cupo, seat ocupado, fuera de vigencia).

---

## 10) Ejemplos frecuentes
**Crear el reservativo al activar plan**
```sql
INSERT INTO app.standing_bookings(person_id, subscription_id, template_id, seat_id, start_date, end_date, status)
VALUES ($person, $subscription, $template, $seat, $start::date, $end::date, 'active');
```

**Cambio puntual (reprogramar un día)**
```sql
INSERT INTO app.standing_booking_exceptions(standing_booking_id, session_date, action, new_session_id)
VALUES ($sb_id, $fecha, 'reschedule', $new_session_id);

-- Cancelar reserva original de ese día (si existe) y crear override
UPDATE app.reservations
SET status='canceled'
WHERE person_id=$person
  AND session_id IN (
     SELECT cs.id FROM app.class_sessions cs
     JOIN app.class_templates t ON t.id=$template_id
     WHERE cs.start_at::date=$fecha
  )
  AND status IN ('reserved','checked_in');

INSERT INTO app.reservations(session_id, person_id, seat_id, status, reserved_at, source)
VALUES ($new_session_id, $person, NULL, 'reserved', NOW(), 'override');
```

**Cambio definitivo de horario**
```sql
UPDATE app.standing_bookings
SET template_id=$new_template_id, seat_id=$new_seat_id, -- opcional
    status='active'  -- por si estaba paused
WHERE id=$sb_id;
-- Re-materializar futuras reservas en la misma operacion o via materializacion manual
```

---

## 11) Pruebas recomendadas (QA)
- **Idempotencia:** correr la materializacion N veces sin duplicar reservas.  
- **Capacidad:** llenar una sesión al máximo y verificar que no se inserten más.  
- **Exclusión de asiento:** simular doble asignación del mismo seat → debe fallar por índice único parcial.  
- **Excepciones:** reprogramar un día específico y confirmar cancelación+override.  
- **Pausa/Cancelación:** detener generación futura y (opcionalmente) cancelar futuras existentes.  
- **Vigencia:** asegurar que no se generan reservas fuera de `start_date..end_date`.

---

## 12) Observabilidad y métricas
- **Logs**: ejecuciones de materializacion (plantillas procesadas, reservas insertadas/omitidas, errores).  
- **Métricas**:  
  - *standing bookings activos* por plantilla/instructor.  
  - *reservas materializadas* por día/semana.  
  - *rechazos por capacidad/seat ocupado*.  
  - *excepciones aplicadas* (reschedules/skips).  
  - *no-shows* vs. reservas standing para evaluar asistencia real.

---

## 13) Preguntas frecuentes (FAQ)
**¿Qué pasa si no hay cupo?**  
No se crea la `reservation`. Se puede registrar un evento/razón de rechazo para trazabilidad.

**¿Se puede forzar un seat distinto por un día?**  
Sí, mediante `standing_booking_exceptions` + `reservation` con `source='override'` en la nueva sesión y seat disponible.

**¿Debo borrar `reservations` al pausar/cancelar?**  
No es obligatorio. Práctica común: **no** borrar; sólo evitar futuras inserciones y, si el negocio lo pide, cancelar futuras pendientes.

---

## 14) Seguridad y privacidad
- No almacenar PII innecesaria en excepciones ni payloads.  
- Asegurar permisos en endpoints de creacion/edicion y en la materializacion manual si aplica.  
- Considerar límites de *rate* y *backoff* en materialización.

---

## 15) Apéndices

### 15.1 Índices recomendados
```sql
-- Unicidad por persona/sesión (evita duplicados)
ALTER TABLE app.reservations
  ADD CONSTRAINT uq_reservation_person_session UNIQUE (session_id, person_id);

-- Exclusividad de seat por sesión
CREATE UNIQUE INDEX IF NOT EXISTS uq_reservations_seat_once
  ON app.reservations(session_id, seat_id)
  WHERE seat_id IS NOT NULL AND status IN ('reserved','checked_in');

-- Standing booking único activo por slot
CREATE UNIQUE INDEX IF NOT EXISTS uq_sb_unique_active
  ON app.standing_bookings(person_id, subscription_id, template_id)
  WHERE status = 'active';

-- Búsqueda eficiente por tiempo/instructor
CREATE INDEX IF NOT EXISTS idx_sessions_time ON app.class_sessions(start_at, venue_id);
CREATE INDEX IF NOT EXISTS idx_sessions_instructor ON app.class_sessions(instructor_id, start_at DESC);
```

### 15.2 Materializacion inmediata (sin job programado)
- Disparador: al crear/renovar una suscripcion fija o al cambiar un standing booking.  
- Ventana: materializar 6-8 semanas hacia adelante (segun duration_value/duration_unit).  
- Reintentos: *retry* con backoff ante bloqueos o timeouts.  
- Idempotencia: garantizada por las restricciones unicas indicadas.

---

**Fin del documento.**

---

## 16) Reschedule batch (nuevo)
**Objetivo:** permitir reprogramar un rango de fechas (dia/semana/mes/anio) dentro de la vigencia, manteniendo el mismo tipo de clase.

**Reglas clave:**
- Solo dentro de `standing_bookings.start_date..end_date`.
- Misma `class_type_id` entre origen y destino.
- Si hay `seat_id` en el standing booking, se intenta conservar; si no esta disponible, se elige otro asiento libre.
- Si existe excepcion para la fecha, se omite.

**Mutaciones (GraphQL):**
- `previewRescheduleStandingBooking(input)` -> lista de fechas con `status` y `reason`.
- `rescheduleStandingBooking(input)` -> aplica el cambio y devuelve el resumen.

**Notas de integridad:**
- Si no se puede crear la reserva de override, no se debe cancelar la original.
- Para overrides con cambio de asiento, guardar `new_seat_id` en `standing_booking_exceptions` (requiere migracion DB).
