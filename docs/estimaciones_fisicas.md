# Estimaciones físicas en campañas (kcal no quemadas / kg de grasa)
**Versión:** 1.0
**Ámbito:** Backend (`app.fitness_estimation_config`, `app.class_types.met_value`) y pestaña *Configuración → Estimaciones* del frontend
**Audiencia:** Desarrolladores, marketing, soporte

---

## 1) Propósito

Las campañas de reactivación (win-back) pueden decirle a un socio ausente cuántas calorías
dejó de quemar y a cuántos kilos de grasa equivale. Ese número sale a WhatsApp con el nombre
del gimnasio encima, así que tiene que ser **defendible**: el socio debe reconocer su propia
rutina en él.

Este documento explica de dónde sale cada término, qué se deduce solo y qué se configura.

---

## 2) El problema que resuelve

La primera implementación calculaba `días_desde_el_vencimiento × 900 kcal` y dividía entre
7700 para obtener kilos. Medido contra la base de producción el 2026-08-27:

| | Fórmula original | Modelo actual |
|---|---|---|
| Socio vencido mediano (349 días) | 314,100 kcal → **40.8 kg de grasa** | 65,500 kcal → **4.1 kg** |
| Socio más antiguo (714 días) | 642,600 kcal → **83.5 kg** | 133,900 kcal → **6.3 kg** |
| Mensajes con más de 10 kg | **263 de 325 (81%)** | **0** |

Cuatro errores independientes se multiplicaban:

1. **Un día de calendario no es una clase perdida.** Love Fitness abre de lunes a viernes, así
   que dos de cada siete días eran clases que no existían. Y nadie asiste todos los días
   abiertos: el ritmo real es ~2.7 reservas por semana activa, no 7.
2. **900 kcal por hora es aproximadamente el doble.** El *Compendium of Physical Activities*
   (código 02017) asigna 8.5 MET a una clase de spinning: para 70 kg y 60 minutos son 595 kcal
   brutas y 525 netas del metabolismo basal. 900 kcal/h exigiría ~12.9 MET sostenidos.
3. **Sin horizonte.** Un error lineal sin techo aplicado a una mediana de 349 días.
4. **Las kcal y los kg no son la misma cifra dividida entre 7700.** Ver §5.

Además, los valores de muestra del catálogo de variables estaban escritos a mano y la fórmula
no podía producirlos: la vista previa del asistente mostraba 8,700 kcal mientras el envío real
habría dicho 314,100. Hoy las muestras salen del propio motor
(`campaign_service.variable_samples()`), así que no pueden volver a divergir.

---

## 3) Arquitectura: deducir lo deducible, configurar sólo lo demás

### Capa 1 — Horario (cero configuración)

`app.class_templates` ya contiene todo lo necesario:

| Columna | Uso en la estimación |
|---|---|
| `weekday` (0 = domingo) | Días distintos con clase → **días abiertos por semana**, que es el techo de cuántas clases puede perder un socio |
| `default_duration_min` | Duración de la clase, por plantilla y promediada por actividad |
| `is_active` | Sólo cuentan las plantillas vigentes; un horario retirado no infla nada |

**Consecuencia:** un gimnasio futuro que abra sábados y domingos, o que dé clases de 45 o 90
minutos, obtiene el número correcto en cuanto carga su horario semanal. **No hay nada que
configurar y no se toca ningún dato existente.**

### Capa 2 — Intensidad (`app.class_types.met_value`, nullable)

El MET es el único dato que ninguna consulta puede deducir: el horario sabe cuándo y cuánto
dura una clase, nunca qué tan dura es.

- `NULL` (estado normal de un catálogo existente) → hereda el valor por defecto del `code`
  en `fitness_estimation_service.DEFAULT_METS`.
- Un número → lo usa tal cual.

Valores por defecto (Compendium of Physical Activities, Ainsworth et al. 2011):

| `code` | MET | | `code` | MET |
|---|---|---|---|---|
| `spinning`, `cycling` | 8.5 | | `zumba`, `baile` | 6.5 |
| `hiit`, `crossfit` | 8.0 | | `pesas`, `fuerza` | 5.0 |
| `box`, `boxeo` | 7.8 | | `pilates` | 3.8 |
| `funcional`, `step` | 7.0 | | `yoga` | 3.0 |

**`NULL` es un valor con significado**, por eso la columna no se rellena nunca en una
migración: escribir un número rompería la herencia. Vaciar la celda en la pantalla borra el
override y devuelve la actividad a su default.

### Capa 3 — Política (`app.fitness_estimation_config`, fila única)

Editable desde *Configuración → Estimaciones*, sin redeploy. Mismo patrón que
`chatbot_config`. Ver §6 para la tabla completa de knobs.

---

## 4) La fórmula

```
semanas    = min(días_inactivo / 7, horizon_weeks)
cadencia   = reservas / semanas_activas        (historial propio, si ≥ min_bookings)
             ó default_sessions_per_week
cadencia   = min(cadencia, días_abiertos_por_semana)
sesiones   = semanas × cadencia

met_neto   = met − 1                            (si net_of_resting)
kcal/sesión= met_neto × reference_weight_kg × duración_min/60
kcal       = sesiones × kcal/sesión × realization_factor
```

**Cadencia por semana *activa*, no por semana de calendario.** La pregunta que hace el mensaje
es "con qué frecuencia venías cuando venías"; dividir entre toda la ventana metería la caída
previa a la baja dentro del promedio y subestimaría a todos. Un socio por debajo de
`min_bookings_for_history` no entra: se usa el default en vez de extrapolar de una reserva.

**Neto del metabolismo basal.** La frase promete lo que el socio dejó de quemar *de más*; en el
sillón no estaba en cero. Restar el 1 MET que gastaba de todos modos es la diferencia entre una
estimación y una estimación halagadora.

---

## 5) Por qué los kilos no son las kcal entre 7700

Es la parte contraintuitiva y la que más fácil se rompe al modificar el motor.

- **Las kcal son lineales y acumulativas.** No gastar 187 kcal/día durante un año son 65,438
  kcal. Es aritmética; no necesita techo para seguir siendo cierta.
- **Los kilos no lo son.** El cuerpo compensa: baja el apetito, baja el gasto no asociado a
  ejercicio, y un cuerpo más pesado cuesta más de mantener, así que el déficit retirado se
  autocorrige hacia un **nuevo peso de equilibrio**. La regla de Wishnofsky (1958, 7700 kcal/kg)
  sobreestima el cambio de peso a largo plazo aproximadamente al doble.

Con `metabolic_adaptation` activo (por defecto) se usa el modelo de Hall et al.
(*Lancet*, 2011): un cambio permanente de 10 kcal/día mueve el peso ~0.45 kg **finales**, y la
mitad se alcanza en ~1 año.

```
kcal_por_día = cadencia × kcal_por_sesión / 7
peso_equilibrio = kcal_por_día / 100 × kg_per_100_kcal_per_day
kg = peso_equilibrio × (1 − 0.5 ^ (días / kg_half_life_days))
```

Esto **no puede dispararse**: asintota en el peso de equilibrio por construcción, no por un
techo que alguien eligió. Y sigue creciendo con la ausencia, que es justo lo que una campaña de
reactivación necesita.

Apagando `metabolic_adaptation` se vuelve a `kcal / kcal_per_kg_fat`.

### El horizonte es un rail, no el mecanismo

`horizon_weeks` acota **sólo el total de kcal**, para que una ausencia de seis años no cotice
un número de seis cifras. Su default (104 semanas) está deliberadamente **por encima de la
ausencia más larga real** (714 días).

> ⚠️ **Un tope que muerde es el bug original en otra posición.** La versión intermedia usaba 12
> semanas y todos los socios más allá recibían una cifra idéntica: dos años se leía exactamente
> igual que tres meses, en una campaña cuyo propósito entero es ser más urgente cuanto más
> tiempo lleva ausente alguien. Si subes la audiencia a ausencias más largas, sube también el
> horizonte. La pantalla avisa con `horizon_reached` cuando el tope está mordiendo.

---

## 6) Configuración

*Configuración → Estimaciones*. Capacidad requerida: `send_campaigns` (o admin).

| Campo | Default | Rango | Qué hace |
|---|---|---|---|
| `reference_weight_kg` | 70 | 30–200 | El gasto calórico escala linealmente con la masa y el sistema no registra el peso del socio. Una referencia honesta es mejor que un dato inventado. |
| `horizon_weeks` | 104 | 1–260 | Rail sobre el total de kcal. Ver el aviso de §5. |
| `default_sessions_per_week` | 2.5 | 0.5–7 | Ritmo supuesto para quien no tiene historial suficiente (≈30% de la audiencia vencida nunca reservó). |
| `min_bookings_for_history` | 4 | 1–100 | Reservas necesarias para fiarse del ritmo propio. |
| `cadence_lookback_days` | 180 | 7–1095 | Ventana de historial que se mira. |
| `net_of_resting` | sí | — | Descontar el 1 MET basal. |
| `metabolic_adaptation` | sí | — | Modelo saturante vs. regla lineal de 7700. |
| `kg_half_life_days` | 365 | 30–1825 | Vida media hacia el peso de equilibrio. |
| `kg_per_100_kcal_per_day` | 4.5 | 1–10 | Kilos de equilibrio por cada 100 kcal/día. |
| `kcal_per_kg_fat` | 7700 | 5000–12000 | Sólo si `metabolic_adaptation` está apagado. |
| `realization_factor` | 1.0 | 0.1–1.0 | Descuento adicional para ser más conservador. |
| `default_met` / `default_duration_min` / `default_open_days_per_week` | 6.0 / 60 / 5 | — | Respaldos para un gimnasio sin horario cargado. |

Los rangos se validan en `fitnessEstimationCrud._RANGES`: un decimal mal puesto es un
`NUMERIC` perfectamente válido para la base y un mensaje que el gimnasio no puede retirar.

La pantalla muestra además, **sólo de lectura**, el horario deducido (días abiertos, duración
promedio, plantillas activas) y un ejemplo con todos los pasos intermedios. Ambas cosas están
ahí para que quien vea un número raro pueda distinguir *"falta configurar algo"* de *"falta
cargar el horario semanal"*.

---

## 7) Variables de campaña

| Clave | Ejemplo | Notas |
|---|---|---|
| `days_inactive` | `349` | Días desde que venció la membresía. |
| `days_since_last_class` | `185` | **Días desde la última clase asistida.** Es el dato correcto para "hace X días que no te vemos": el socio deja de venir antes de dejar de pagar, y en producción estas dos cifras difieren en meses (mediana 185 vs 349). Vacío si nunca reservó. |
| `kcal_not_burned` | `65,500` | Redondeado a una escala que se lee como estimación. |
| `kg_fat_equivalent` | `4.1` | Equivalencia, no el peso corporal del socio: el sistema no lo registra. |
| `kcal_window_label` | `los últimos 12 meses` | Periodo que cubre el total. Úsalo junto a las kcal para no dar a entender que abarca toda la ausencia si el horizonte la recorta. |

Todas se vacían juntas (`_INACTIVITY_KEYS`): si no sabemos hace cuánto se fue, media frase con
un hueco es peor que ninguna frase.

---

## 8) Rendimiento

Dos agregados por audiencia, ninguno por mensaje:

- **`sessions_per_week`** se congela en `campaign_recipients` durante el build, igual que la
  clase favorita (migración `c2d5e8f3b1a4`). Resolverla en el envío sería un `GROUP BY` por
  mensaje.
- **El perfil** (config + horario deducido) es igual para todo el gimnasio y se memoiza 60 s
  (`load_profile`). Las mutaciones de la pantalla lo invalidan, así que un cambio aplica en el
  siguiente envío y no al minuto.

`days_since_last_class` **no** se congela: cambia cada día, y un destinatario diferido por
horario silencioso no debe mandar una cifra que era cierta cuando se construyó la audiencia.

---

## 9) Limitación conocida: la asistencia es la reserva

`app.reservations.checkin_at` **nunca se escribe en producción** (0 de 6,058 filas históricas);
el `status` siempre queda en `reserved` y `no_show` no lo asigna ningún flujo. Por lo tanto:

- La cadencia se mide sobre reservas, no sobre asistencia confirmada.
- El peso `checked_in = 3` de `segmentation_service` hoy no discrimina nada.

Si algún día se implementa el check-in real, la cadencia mejorará sola: `ATTENDANCE_STATUSES`
ya incluye ambos estados.

---

## 10) Migraciones

| Revisión | Qué hace |
|---|---|
| `e4f7a0b5d3c6` | Añade `class_types.met_value` y `campaign_recipients.sessions_per_week` (ambas nullable), crea `fitness_estimation_config` y siembra su fila única. |
| `f5a8b1c6e4d7` | Añade `metabolic_adaptation`, `kg_half_life_days`, `kg_per_100_kcal_per_day`; sube el default de `horizon_weeks` de 12 a 104. |

Ambas son **aditivas e idempotentes** (`ADD COLUMN IF NOT EXISTS`) y **no modifican ningún dato
de negocio existente**. Verificado con un ciclo `downgrade` → `upgrade` sobre un catálogo
sembrado: mismos ids, mismos nombres, `met_value` en `NULL` en todas las filas. El único
`UPDATE` de `f5a8b1c6e4d7` toca la fila de configuración y sólo si sigue en el default anterior
(`WHERE horizon_weeks = 12`), para no pisar una decisión que alguien haya tomado.
