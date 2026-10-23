# Imágenes como contexto de los bots

Los bots de clientes y de Admin reciben los píxeles de las imágenes entrantes de
WhatsApp, con la leyenda y la fecha local de envío. Una imagen sin leyenda también
inicia un turno. Los mensajes siguientes, como «me aparece un error», conservan
las imágenes recientes de esa conversación como contexto.

Cada bot usa su modelo configurado en la UI; no hay un modelo visual fijo ni una
segunda llamada de OCR. El modelo seleccionado debe admitir imágenes. Sonnet 5.5
recibe los bloques nativos de imagen de Anthropic. No se modifican las imágenes
originales guardadas ni la visualización de adjuntos en Chats.

## Recepción y contexto

- La ingestión confirma primero el mensaje y su adjunto en la base. Después
  inicia la descarga existente y programa el bot. Las entregas repetidas del
  webhook no programan otro turno ni otra descarga.
- Se agrupan mensajes consecutivos mientras se prepara el contexto, con una
  espera inicial de 0.5 segundos. Una imagen que tarda en descargarse puede
  incorporarse al turno de su mensaje de seguimiento. Al iniciar el modelo y
  sus herramientas, ese turno se completa antes de ejecutar el siguiente.
  Esta coordinación es por conversación y proceso; no es una cola distribuida.
- Se incluyen la imagen actual y hasta dos imágenes anteriores. Una respuesta
  citada puede recuperar una imagen más antigua de la misma conversación,
  ocupando uno de esos dos espacios. Las demás conservan su leyenda y un aviso
  de que no se incluyeron sus píxeles.
- Se espera hasta 15 segundos por la descarga. Si falla, el modelo recibe un
  aviso explícito y debe solicitar el detalle o el reenvío. Si falla la llamada
  visual al proveedor, el servidor responde que no pudo completar la lectura.

## Errores de pago

Ante una captura de Mercado Pago, el bot debe describir lo visible y consultar
`get_my_purchase_status`. Esta herramienta consulta la compra de la conversación,
los pagos registrados y, si está configurado, el estado actual del proveedor por
su referencia interna. No recibe una referencia arbitraria del usuario ni entrega
datos del pagador al modelo.

Una captura de error no demuestra que un cargo fue rechazado, y una captura de
comprobante no acredita un pago. Se conserva la validación de reservas y cobros
contra la base y los procesos existentes de acreditación. La consulta nueva es
de lectura; no acredita pagos ni crea reservas por sí sola.

Si la compra vigente consultada tiene pago aprobado o en proceso, se impide
reemplazarla por otro enlace o volver a pedir el cobro. Los enlaces se envían
mediante la respuesta estructurada `purchase`, que comprueba la compra y su
clase. El bot no puede copiar un enlace de una captura o del historial en una
respuesta informativa. Una compra vencida o de una clase pasada requiere revisar
los cargos y la fecha antes de proponer una nueva compra.

## Límites y datos

- Formatos JPEG, PNG, GIF y WebP; se utiliza el primer fotograma.
- Máximo de entrada: 10 MiB y 25 millones de píxeles. La copia enviada al modelo
  se orienta, elimina metadatos y se reduce proporcionalmente a un lado máximo
  de 2048 píxeles, sin recortar, con un máximo de 4 MiB por imagen.
- Solo se leen adjuntos persistidos de la conversación y el contacto actuales.
  El almacenamiento permitido es `/uploads/whatsapp/` o la ruta
  `whatsapp/chat/` del origen configurado en `MEDIA_PUBLIC_BASE_URL`. Se rechazan
  rutas que salen de esos directorios, otros orígenes y redirecciones HTTP.
- No se abren URLs escritas por el usuario ni visibles en las imágenes.
- Los píxeles se envían al proveedor del modelo como parte del turno y no se
  persiste una copia del análisis. Los nuevos registros de auditoría incluyen
  identificadores y resultados, sin leyendas, URLs, píxeles ni datos del pagador.
- El texto dentro de una imagen se trata como datos no confiables. No sustituye
  las instrucciones del bot ni la autorización textual del administrador para
  ejecutar acciones. Si la imagen llega sin leyenda, el servidor ofrece al modelo
  únicamente herramientas de consulta y respuesta; no permite iniciar compras,
  modificar reservas ni confirmar acciones administrativas en ese turno.

## Validación y despliegue

Este cambio no añade migraciones ni requiere un cambio de frontend. Requiere
desplegar el backend con sus dependencias actuales y la configuración de IA,
WhatsApp y almacenamiento existentes. Las migraciones de membresías y modelos
incluidas previamente en la rama siguen siendo necesarias para esa versión.

Las pruebas locales usan PostgreSQL aislado, imágenes generadas para las pruebas
y respuestas simuladas de Anthropic, Mercado Pago y WhatsApp. Comprueban la
ingestión después del commit, reintentos, ambos bots, seguimiento de una imagen,
citas fuera del historial, aislamiento entre conversaciones, límites, fallos y
el bloqueo de un segundo enlace durante un pago en proceso. No envían mensajes
a clientes ni realizan llamadas de generación o cobro.

Al desplegar, comprobar en una conversación de prueba una imagen sin leyenda,
una captura con «me aparece un error» y un adjunto ilegible. Confirmar también
que un recibo no activa una membresía y que una captura de error no provoca un
segundo cobro. Esas comprobaciones con el proveedor real siguen pendientes.
