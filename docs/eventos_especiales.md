# Eventos especiales

Clases únicas con precio y cupo propios (por ejemplo, «Halloween · 2.º aniversario de Love
Fitness»). Se venden por WhatsApp con Mercado Pago y se administran desde la pantalla
**Eventos especiales** del escritorio. Para verla y operarla se necesita la capacidad
`manage_special_events`. El admin la tiene; a otros roles se les da desde Configuración → Permisos.

## Qué es y qué no es

- **No es una membresía.** Comprar una entrada no crea, renueva ni consume ninguna membresía. A
  quien no es socio no se le da de alta como socio.
- **No es una clase regular.** El evento tiene sus propias bicicletas, así que una bici ocupada
  en la clase regular se puede vender para el evento. Tampoco se vende con pase diario ni con
  planes.
- **Tiene prioridad sobre el horario regular, pero solo si el personal lo decide.** Si en la
  misma sede y horario hay una clase regular, hay que revisar la vista previa y aplicar la
  sustitución antes de publicar.

## Cómo publicar un evento

1. **Crearlo como borrador.** Escribe nombre, descripción, qué incluye, sede, instructora,
   fecha, hora de inicio y fin, precio, cupo y bicicletas. Las horas son de la sede
   (America/Mexico_City) y el servidor las convierte a UTC. Un borrador no lo ve el asistente.
2. **Horario regular → «Revisar clases afectadas».** La vista previa muestra cada clase que
   se empalma y a sus clientes: bici, membresía y si tienen horario fijo.
3. **Marcar «Revisé la lista…» y luego «Aplicar sustitución».**
   - La clase queda **desplazada**: no se vende ni se reserva y conserva su historial.
   - Sus reservas también quedan **desplazadas**: no cuentan como asistencia ni como falta y
     no consumen clase.
   - Esos clientes pasan a **Clientes desplazados**.
   - No se envía ningún mensaje y no se toca ninguna membresía. El horario de otras fechas no
     cambia.
4. **Publicar.** El botón se activa cuando no queda nada pendiente: duración, cupo, precio
   mayor a 0 y clases sin sustituir.
5. **Vincular la campaña de WhatsApp** al evento, en la sección de campañas de la pestaña
   Evento. Así, cuando un cliente toque «Quiero reservar» o «Más información», el asistente
   sabrá de qué evento se trata y consultará sus datos reales.

## Cómo compra un cliente (WhatsApp)

1. Toca «Quiero reservar». El asistente identifica el evento por la campaña vinculada.
2. Le pide el nombre completo del participante. Si es un socio que compra para sí, ya lo tiene.
3. Le repite el evento, la fecha, el horario y el precio, y le manda el enlace de Mercado Pago.
4. **Apartado:** su bici queda apartada **30 minutos** (configurable por evento). El enlace
   vence a la misma hora y solo acepta tarjeta o saldo de Mercado Pago. No acepta OXXO, cajero
   ni transferencia, que tardan días en acreditarse.
5. Cuando Mercado Pago acredita el pago, el sistema confirma el lugar y le manda por WhatsApp
   la confirmación con bici, fecha y dirección. **El asistente nunca confirma por su cuenta.**

Si Mercado Pago no está configurado en el servidor, el asistente dice que no puede cobrar y no
aparta nada. Nunca convierte la compra en una reserva confirmada por texto.

## Pagos: qué pasa en cada caso

| Situación | Qué hace el sistema |
|---|---|
| Pago aprobado a tiempo | Confirma la entrada y registra el pago en Finanzas. |
| Mercado Pago avisa dos veces del mismo pago | No hace nada la segunda vez. |
| Pago rechazado | El apartado sigue vivo; el cliente puede reintentar con el mismo enlace. |
| Pago que llega después de que venció el apartado | Si aún hay lugar, lo confirma (puede cambiar la bici). Si no, lo reembolsa y avisa al cliente. |
| Pago de una compra cancelada o de un evento cancelado | Lo reembolsa y avisa al cliente. |
| Segundo pago para una entrada ya pagada | Reembolsa el pago extra; la entrada sigue confirmada. |
| El importe o la moneda no coinciden | No confirma; deja la compra «En revisión» y avisa que un asesor la contactará. |
| Falla el reembolso automático | Deja la compra «En revisión» para reembolsarla a mano. |

Las situaciones anómalas quedan registradas en `webhook_logs`, con `event_type` que empieza con
`special_event_` y `processed = 0`, para conciliarlas.

Cada 2 minutos un barrido revisa los apartados vencidos. Antes de liberar uno, le pregunta a
Mercado Pago si ese pago sí se aprobó (por si se perdió el aviso).

## Atender a los clientes desplazados

En **Clientes desplazados** aparece cada cliente con su clase original, su bici y su membresía.

- **Registrar aviso**: guarda la fecha, el canal y quién avisó.
- **Registrar compensación**: reposición, extensión de membresía, acceso al evento, saldo a
  favor, devolución, «no requirió» u otra, con notas. Se puede marcar como resuelto.

El sistema solo registra la decisión. La extensión o la reposición se aplican como siempre, en
Membresías o en Clases.

Si alguien se inscribe a ese horario **después** de la sustitución, aparece solo en esta lista
y no se le reserva una clase que no se va a dar.

## Si el evento se cancela

1. **«Cancelar evento»**, con un motivo.
   - Los apartados se liberan.
   - Las entradas pagadas quedan «Por reembolsar». Reembólsalas desde Participantes: por
     Mercado Pago se devuelven solas; en efectivo, confirmas que ya devolviste el dinero.
   - No se envía ningún mensaje.
2. **Horario regular → «Revisar restitución» → «Restituir horario regular».**
   - La clase regular vuelve a estar activa.
   - Solo se reactivan las reservas que siguen como las dejó la sustitución y que no tienen
     compensación registrada.
   - Las demás se quedan desplazadas, con el motivo, para revisarlas a mano.

## Detalles técnicos

- **Tablas:**
  - `special_events`, `special_event_seats` y `special_event_orders` (apartado, pago y entrada).
  - `special_event_takeovers` (quién sustituyó qué clase y cuándo).
  - `special_event_displacements` (seguimiento manual).
- **Migraciones:**
  - `b2e5f8a1c4d7`: corrige la hora de las clases generadas desde junio de 2026, explicado
    abajo.
  - `c3f6a9b2d5e8`: crea el esquema de eventos.
  - `d4a7b0c3e6f9`: siembra el evento de Halloween como borrador.
- **Integridad:**
  - Dos eventos activos no pueden empalmarse en la misma sede ni con la misma instructora
    (`EXCLUDE USING gist`).
  - Una bici y un participante cuentan una sola vez por evento (índices únicos parciales).
  - El cupo se protege con un candado por evento (`lock_special_event`).
- **Zona horaria:** las clases se generan con hora local explícita (`app/core/timezones.py`).
  Antes se usaba la zona del servidor y, en el contenedor (UTC), las clases quedaban 6 horas
  antes.
