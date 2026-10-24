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

Esta migración parte de b8e1f4a7c0d3 (enlaces de eventos sin caducidad), ya integrada en
main. La cadena de Alembic mantiene una sola cabeza.

## Recuperación segmentada por causa (sin migración adicional)

Los dos accesos, «Recuperar no entregados» y «Reintentar fallidos», usan la misma vista
previa. `previewCampaignRecovery(campaignId, causes: [String!])` devuelve las causas,
conteos, notas y una fila por destinatario con nombre, teléfono, causa, detalle, elegibilidad,
bloqueo y fecha prevista. El cliente filtra esa lista localmente al cambiar las casillas.
`recoverCampaignRecipients(campaignId, causes: [String!])` vuelve a comprobar todo y solo
encola las causas elegidas. Una lista vacía no encola nada; una causa desconocida se rechaza.
Las operaciones conservan el permiso SEND_CAMPAIGNS. Guardar la política no recupera ni envía.

Por defecto se seleccionan daily_cap, meta_limit, meta_temporary y bad_number. Recency_block,
unconfirmed, other_error, meta_limit_exhausted y other requieren selección explícita.

El veredicto de Meta manda sobre cualquier clasificación local posterior. Se toma del último
intento del historial; en envíos anteriores al historial, del recibo `failed` de
`message_statuses` y del código guardado en `webhook_logs` (solo lectura, a diferencia de la
reparación); y al final de `recipient.error`. Una solicitud aceptada (con wamid) y sin veredicto
es unconfirmed aunque después se haya omitido por límite diario. Los números inválidos y los
que carecen de teléfono se muestran en bad_number, incluso si quedaron omitidos por otra causa.
Lo que no encaja en ninguna causa (en cola sin intento, aplazado por horario o conversación,
omitido por otra regla) va a other, nunca a daily_cap.

- daily_cap aplica la política guardada (respetar, aplazar o exceptuar); el resto de controles
  siguen vigentes. Recency_block solo se exceptúa al seleccionar esa causa.
- meta_limit respeta el presupuesto automático del teléfono: con un rechazo 131049 espera
  24 h y con dos, 48 h desde el último, igual que los reintentos automáticos. Con tres o más
  pasa a meta_limit_exhausted, que no se preselecciona y, al elegirse, autoriza otro intento
  manual no antes de 24 h desde el último rechazo. La política automática no cambia.
- bad_number exige un teléfono actual de Socios distinto al anterior y de 10 a 15 dígitos.
  Reformatear 52/521 no cuenta como corrección. Solo al ejecutar se refrescan phone_e164,
  wa_id y phone_key con la misma función que construye la audiencia. La ficha no se modifica.
- unconfirmed permite un intento manual tras 24 h con advertencia de posible duplicado.
  La autorización queda en skip_reason=recovery_unconfirmed y sobrevive los aplazamientos.
  No cambia ni borra intentos previos y nunca autoriza saltarse un intento nuevo en curso.
- other_error expone el código para que el administrador revise y corrija la causa antes
  de autorizar. Los errores temporales se separan en meta_temporary. Un rechazo sin código
  (solo el recibo `failed`) también va aquí.
- other no tiene rechazo de Meta. Las filas que siguen en cola salen al reanudar la campaña;
  seleccionarlas solo las reprograma.

Siempre se excluyen entregas/lecturas/respuestas demostradas, duplicados, bajas, conversiones,
compradores del evento (pedido pagado, en revisión con pago o apartado vigente), envíos en curso
y eventos cerrados (o fechas de envío posteriores a su inicio). El bloqueo de compradores se
verifica por persona o participant_key y se repite en el remitente. Cada teléfono tiene un
dueño determinista que no depende de las casillas: la fila que ya está en cola; si no hay, la que
conservó la audiencia (no duplicate_phone); luego la de una causa preseleccionada; por último el
menor ID. Así filtrar casillas no cambia la identidad elegida y nunca se encola una segunda fila
para un número que ya está en cola. El candado de teléfono y la comprobación anterior al envío
siguen activos. La vista previa cuenta aparte (not_selected) a los elegibles de causas no
seleccionadas.

Si la campaña se pausa a mitad de un lote (por ejemplo, por 131048), los destinatarios ya
reclamados vuelven a la cola sin cambios y salen al reanudar. Un evento cerrado o una campaña
cancelada los marca como omitidos. Ninguno de los dos casos se registra como «Error interno.».

Publicar primero backend y luego escritorio. No hay migraciones ni reparación histórica
implícita. La consulta de diagnóstico y las pruebas no encolan recuperaciones en producción.
