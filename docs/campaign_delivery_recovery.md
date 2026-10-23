# Entrega y recuperación de campañas

Las tarjetas de aceptados, enviados, entregados y leídos cuentan mensajes únicos por wamid,
usando el historial de intentos y las confirmaciones de Meta. Una solicitud aceptada por la
API no demuestra entrega. La lectura implica entrega; una respuesta no inventa una lectura.
El panel agregado de Meta puede actualizarse después que los webhooks.

La audiencia distingue socios del segmento y teléfonos únicos. Las identidades mexicanas
con 10 dígitos, 52 y 521 se comparan como un único destino. Los registros de socios se
conservan. Los números internacionales conservan su código de país completo.

## Política por campaña

dailyCapPolicy admite respect (omitir), defer (próximo día permitido) y allow_additional
(excepción para esta campaña). La última opción solo exceptúa el límite diario local:
conserva consentimiento, deduplicación, horario permitido y límites de Meta. Los bloqueos
por contacto reciente entre campañas son independientes.

autoRetryMetaLimits activa como máximo dos reintentos de 131049: 24 horas después del
primer rechazo y otras 48 después del segundo. Se cuenta el intento real, no una espera por
horario o límite local. 131048 pausa la campaña y requiere revisión. 130472, 131026 y errores
de parámetros conservan su causa y no se reintentan automáticamente. Un resultado incierto
se revisa antes de volver a enviar. No se programan mensajes que quedarían después del
inicio del evento vinculado.

En campañas nuevas los reintentos de 131049 están activados. La migración los mantiene
desactivados en campañas existentes. Guardar la política no recupera filas: el administrador
debe usar «Recuperar no entregados», revisar la vista previa y confirmar. Las operaciones
requieren send_campaigns. El contenido permanece editable solo en borrador; la política
también puede editarse en campañas programadas, pausadas y completadas.

## Historial y recuperación

campaign_send_attempts conserva cada solicitud, su identidad normalizada, el identificador
de Meta, tiempos, códigos y detalle del rechazo. Antes de la llamada se confirma un intento
duradero. Si se pierde la respuesta o se reinicia el proceso, ese intento impide un reenvío
a ciegas. La aceptación se confirma antes de persistir el mensaje del chat; los webhooks
pueden asociarse al intento aunque esa persistencia falle.

Los callbacks duplicados no reinician el plazo de espera. Los callbacks de intentos
anteriores conservan su evidencia sin restablecer el estado de una solicitud posterior.
Una entrega demostrada impide recuperar ese teléfono en la misma campaña.

previewCampaignRecovery solo consulta; recoverCampaignRecipients vuelve a validar y encola
exclusivamente los elegibles. El worker consulta las políticas vigentes y el cierre del
evento antes de enviar. La acción no anula los códigos de Meta.

## Migración y publicación

1. Ejecutar pruebas con una base PostgreSQL temporal. Aplicar la migración c9f2a5b8d1e4 y
   desplegar el backend antes del cliente.
2. Detener/pausar el envío de la campaña que se va a reparar. Revisar primero:

       python scripts/reconcile_campaign_delivery.py --campaign-id 3

3. Revisar matched, ambiguous y replies. La asociación exige que message_id y wamid
   coincidan. El diagnóstico no modifica datos, encola ni llama a Meta.
4. Aplicar la reparación aprobada, sin activar reintentos o recuperar destinatarios:

       python scripts/reconcile_campaign_delivery.py --campaign-id 3 --apply

5. Publicar el cliente. Elegir y guardar la política, abrir la vista previa y programar
   explícitamente la recuperación. Revisar rechazos por código, pendientes y duplicados.

La reparación es idempotente. Los vínculos ambiguos quedan en revisión; nunca se asigna una
entrega por similitud de fecha o texto. La reversión de la migración se bloquea cuando existe
historial, para impedir su pérdida accidental.

Esta migración parte de a7d0e3f6b9c2. Si antes se integra otra migración desde esa misma
revisión, actualizar la cadena o crear una revisión de unión antes del despliegue; la
publicación debe tener una sola cabeza de Alembic.
