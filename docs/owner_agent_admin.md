# Agente Administrativo por WhatsApp

## Resumen

El Agente Administrativo es un agente separado del chatbot comercial de clientes.
Su objetivo es que el dueno o administradores autorizados puedan consultar el
negocio y ejecutar tareas operativas desde WhatsApp.

El agente solo responde cuando el mensaje entrante viene de un telefono guardado
en `owner_agent_authorized_phone` y marcado como activo. Los mensajes de clientes
no autorizados siguen entrando al flujo normal del chatbot comercial.

## Activacion

La activacion tiene dos niveles:

1. Kill-switch de servidor en `.env`:
   - `OWNER_AGENT_SERVER_ENABLED=true`
   - `ANTHROPIC_API_KEY=<secret>`
   - Opcionales:
     - `OWNER_AGENT_MODEL=claude-sonnet-4-6`
     - `OWNER_AGENT_RECURSION_LIMIT=12`

2. Configuracion operativa desde el frontend:
   - Menu `Configuracion -> Agente Admin`
   - Activar/desactivar el agente.
   - Administrar telefonos autorizados.
   - Elegir modelo.
   - Configurar si las acciones requieren confirmacion.
   - Editar instrucciones administrativas.

Si `OWNER_AGENT_SERVER_ENABLED` esta apagado, el frontend puede guardar la
configuracion pero el agente no respondera.

## Telefonos autorizados

El telefono inicial sembrado por migracion es:

- `8719708890`, normalizado como `5218719708890`

El backend normaliza formatos mexicanos comunes para que coincidan:

- `8719708890`
- `528719708890`
- `5218719708890`

La tabla `owner_agent_authorized_phone` guarda:

- `label`: etiqueta visible, por ejemplo `Dueno`
- `phone_number`: telefono escrito por el usuario
- `normalized_wa_id`: formato canonico usado para matching
- `enabled`: si puede usar el agente
- `created_by`: cuenta que lo registro

## Permisos

Para editar esta configuracion desde el frontend se requiere la capability:

- `manage_owner_agent`

El rol `admin` la tiene implicitamente. Otros roles pueden recibirla desde:

- `Configuracion -> Permisos`

El backend vuelve a validar la capability en cada mutation; el JWT solo se usa
para mostrar u ocultar opciones de UI.

## Flujo de mensajes

1. Meta WhatsApp Cloud API entrega el webhook en `/webhook/whatsapp`.
2. `whatsapp_ingest_service` persiste contacto, conversacion y mensaje.
3. `whatsapp_hooks.on_inbound_message` revisa si el `wa_id` esta autorizado.
4. Si esta autorizado:
   - agenda `owner_agent.reply_service.schedule_agent_reply`
   - no ejecuta opt-out ni chatbot comercial
5. Si no esta autorizado:
   - mantiene el flujo existente del chatbot de clientes.
6. El agente corre en background con su propia sesion de DB.
7. La respuesta se envia por `whatsapp_outbound` con kind:
   - `owner_agent_reply`

## Seguridad operacional

El agente admin usa estas barreras:

- Allowlist de telefonos autorizados.
- Kill-switch de servidor.
- Toggle operativo en base de datos.
- Confirmacion obligatoria para acciones que cambian estado.
- Auditoria por herramienta/accion.
- Separacion total del estado del chatbot comercial.

Las tablas principales son:

- `owner_agent_config`
- `owner_agent_authorized_phone`
- `owner_agent_pending_action`
- `owner_agent_audit_log`
- `owner_tasks`

## Confirmacion de acciones

Las acciones que modifican estado no se ejecutan directamente. El agente debe:

1. Llamar a una tool `propose_*`.
2. Guardar la accion en `owner_agent_pending_action`.
3. Responder con un resumen y pedir confirmacion.
4. Ejecutar solo si el usuario responde algo como `si`, `ok` o `dale`.
5. Cancelar si responde `no`, `cancelar` o equivalente.

Tools de confirmacion:

- `confirm_action`
- `cancel_action`

## Tareas y consultas disponibles

### Reporte general del negocio

Tool: `get_business_report(period)`

Devuelve KPIs del negocio:

- Ingresos del periodo.
- Reservas del periodo.
- Socios activos.
- Nuevos socios.
- Ocupacion promedio.
- Plan mas vendido del periodo, si existe.

Periodos soportados:

- `today`
- `week`
- `month`
- `last_30_days`
- `year`
- tambien acepta variantes en espanol como `hoy`, `esta semana`, `este mes`.

Ejemplos:

- "Dame el reporte de hoy"
- "Como va el negocio este mes?"
- "Resumen de los ultimos 30 dias"

### Reporte financiero

Tool: `get_payments_report(period)`

Devuelve:

- Total cobrado.
- Numero de pagos.
- Monto completado.
- Ticket promedio.
- Pagos pendientes.
- Pagos fallidos.
- Pagos reembolsados.
- Pagos huerfanos.
- Posibles duplicados.
- Desglose por metodo.

Ejemplos:

- "Cuanto vendimos hoy?"
- "Reporte financiero de esta semana"
- "Hay pagos huerfanos o duplicados?"

### Reporte de socios

Tool: `get_members_report(days_ahead)`

Devuelve:

- Suscripciones activas.
- Suscripciones vencidas que siguen marcadas como activas.
- Socios que vencen proximamente.

`days_ahead` permite revisar proximos vencimientos, por default 14 dias.

Ejemplos:

- "Quien vence en los proximos 7 dias?"
- "Resumen de socios"
- "Cuantos socios activos hay?"

### Reporte de clases

Tool: `get_classes_report(period)`

Devuelve:

- Sesiones del periodo.
- Reservas/check-ins.
- Ocupacion promedio.
- Ocupacion por tipo de clase.

Ejemplos:

- "Como estuvo la ocupacion hoy?"
- "Reporte de clases de esta semana"
- "Que clases tuvieron mas ocupacion?"

### Reporte de leads

Tool: `get_leads_report(period)`

Devuelve:

- Total de leads del periodo.
- Leads convertidos.
- Desglose por estado.

Ejemplos:

- "Como van los leads este mes?"
- "Cuantos leads convertimos?"

### Reporte de campanas

Tool: `get_campaigns_report()`

Devuelve:

- Total de campanas.
- Desglose por estado.

Ejemplos:

- "Estado de campanas"
- "Cuantas campanas hay programadas?"

### Reporte de WhatsApp

Tool: `get_whatsapp_report()`

Devuelve:

- Conversaciones activas.
- Mensajes entrantes no leidos.
- Ultimos mensajes entrantes.

Ejemplos:

- "Hay mensajes pendientes?"
- "Resumen de WhatsApp"

## Acciones disponibles

### Crear tarea

Tool de propuesta: `propose_create_task(title, description)`

Crea una tarea en `owner_tasks` despues de confirmacion.

Ejemplos:

- "Crea una tarea para revisar pagos huerfanos"
- "Agrega tarea: llamar a socios vencidos"

### Completar tarea

Tool de propuesta: `propose_complete_task(task_id)`

Marca una tarea como `done` despues de confirmacion.

Ejemplos:

- "Completa la tarea 12"
- "Marca como terminada la tarea 5"

### Cancelar tarea

Tool de propuesta: `propose_cancel_task(task_id)`

Marca una tarea como `canceled` despues de confirmacion.

Ejemplos:

- "Cancela la tarea 8"

### Listar tareas

Tool: `list_tasks(include_done)`

Lista tareas abiertas por default. Si `include_done=true`, incluye completadas y
canceladas.

Ejemplos:

- "Que tareas tengo pendientes?"
- "Lista mis tareas"

### Ejecutar barrido de notificaciones

Tool de propuesta: `propose_notification_sweep()`

Despues de confirmacion ejecuta:

- `notification_service.run_all_sweeps()`

Uso esperado:

- recordatorios de renovacion
- membresias vencidas

Ejemplo:

- "Ejecuta el barrido de notificaciones"

### Ejecutar barrido de campanas

Tool de propuesta: `propose_campaign_sweep()`

Despues de confirmacion ejecuta:

- `campaign_service.run_campaign_sweep()`

Ejemplo:

- "Ejecuta campanas programadas"

## Auditoria

Cada tool registra un evento en `owner_agent_audit_log` con:

- conversacion
- mensaje origen
- telefono autorizado
- tool o accion
- payload
- resumen del resultado
- estado
- error si fallo

Esto permite revisar que pidio el administrador y que hizo el agente.

## Limitaciones actuales

- El agente no envia mensajes proactivos por si solo; responde a mensajes
  entrantes dentro de la ventana de WhatsApp.
- No administra clientes directamente en v1, salvo reportes y tareas internas.
- Las acciones destructivas o de alto impacto deben agregarse como nuevas
  `propose_*` tools con confirmacion y auditoria.
- La configuracion es single-business. Si FitPilot se vuelve multi-tenant, estas
  tablas deben recibir `tenant_id` o `business_id`.

## Troubleshooting

### No responde

Revisar:

- `OWNER_AGENT_SERVER_ENABLED=true`
- `ANTHROPIC_API_KEY` configurado
- agente activo en `Configuracion -> Agente Admin`
- telefono marcado como activo
- `normalized_wa_id` coincide con el `wa_id` entrante

### Responde el chatbot comercial en lugar del agente admin

El telefono no esta resolviendo contra `owner_agent_authorized_phone`. Revisar
normalizacion y estado activo.

### El frontend no muestra la seccion

El usuario debe ser `admin` o tener la capability `manage_owner_agent`.

### Las tools fallan con datos de reportes

Verificar que la base de datos este disponible y que las migraciones esten
aplicadas:

```bash
cd backend
python -m alembic upgrade head
```
