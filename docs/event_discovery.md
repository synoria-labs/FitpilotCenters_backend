# Consultas de eventos sin plantilla previa

El bot puede resolver una consulta como «Estoy interesado en la clase de Halloween» sin
que ese teléfono figure en una campaña. El mensaje se compara con los nombres de eventos
publicados que todavía no comienzan. Una sede/nombre distintivo o una fecha explícita puede
desambiguar eventos con un título compartido; si quedan varias coincidencias se pregunta
al cliente cuál busca. Un anuncio no identificado no se atribuye al único evento disponible.

## Facebook, Instagram y enlaces a WhatsApp

Cuando el webhook contiene `messages[].referral`, se conservan de forma acotada
`source_id`, `source_type`, `source_url`, `headline`, `body` y `ctwa_clid` en
`app.messages.referral`. La migración d0f3b6a9e2c5 añade una columna JSON nullable después de
c9f2a5d8e1b4; los mensajes históricos conservan NULL y no se reparan ni reenvían.

El título y texto de la referencia sirven exclusivamente para identificar candidatos. No se
envían como instrucciones al modelo, no se descargan sus imágenes/URLs y nunca sustituyen
precio, horario, dirección, cupo, vigencia del enlace o estado del pago del sistema. El ID del
anuncio queda como atribución; no implica por sí mismo una asociación con un evento.

Para enlaces o publicaciones sin referencia de anuncio, se puede precargar un mensaje que
incluya el nombre del evento. Si consulta un evento/anuncio pero el mensaje es genérico, el bot pide identificar el evento
entre las opciones publicadas. No requiere recibir primero una campaña de WhatsApp.

Referencia de integración: [Meta, WhatsApp Business Platform — Webhook Payload Reference](https://www.postman.com/meta/whatsapp-business-platform/request/cy6hnq7/received-text-message).

## Continuidad y respuestas verificadas

La consulta directa o la referencia se conserva como contexto durante los siguientes mensajes
entrantes de esa conversación (hasta 24 horas, con un máximo de 12 mensajes). Permite seguir
con el nombre del participante. Una solicitud explícita de membresía/clase regular corta
esa herencia. No se usa la referencia de otra conversación ni un anuncio antiguo.

`respond_to_customer(kind=event_information, event_id=...)` redacta la oferta desde la base,
incluidos los lugares y la política actual del enlace. Para una consulta directa/de anuncio,
information/clarification también se redirigen a esos datos, salvo preguntas transaccionales
como cancelación, pago o error. Si el modelo omite la herramienta, no termina su respuesta o
falla, el fallback del evento se vuelve a consultar antes de enviar; no se pide la fecha de una
clase regular. Una imagen ilegible conserva su respuesta específica.

En contexto de evento se rechazan propuestas de pase diario/membresía y compras de un evento
distinto o ambiguo. La compra válida usa el flujo existente start_event_purchase, con su
verificación de cupo, pago y participante. Informar no crea pedidos, suscripciones ni reservas.
La entrada solo se confirma al acreditarse el pago; los eventos llenos/cancelados/iniciados no
se ofrecen como disponibles. Las comprobaciones de reservas y pagos existentes se conservan.

Se registra message_id, conversation_id, origen de la resolución, evento y candidatos para
diagnóstico. No se registran en ese log el cuerpo del anuncio, el teléfono ni el click ID.

Publicar la migración aditiva y el backend conjuntamente mediante el procedimiento habitual.
El escritorio no requiere campos GraphQL nuevos. Las pruebas simulan el modelo y los
proveedores; no envían mensajes ni crean pagos reales.
