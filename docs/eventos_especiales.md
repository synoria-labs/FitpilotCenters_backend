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
   - No se envía ningún mensaje y no se toca ninguna membresía (hasta que se les dé la clase
     extra, ver abajo). El horario de otras fechas no cambia.
4. **Publicar.** El botón se activa cuando no queda nada pendiente: duración, cupo, precio
   mayor a 0 y clases sin sustituir.
5. **Vincular la campaña de WhatsApp** al evento, en la sección de campañas de la pestaña
   Evento. Así, cuando un cliente toque «Quiero reservar» o «Más información», el asistente
   sabrá de qué evento se trata y consultará sus datos reales.

## Cómo compra un cliente (WhatsApp)

1. Toca «Quiero reservar». El asistente identifica el evento por la campaña vinculada.
2. Le pide el nombre completo del participante. Si es un socio que compra para sí, ya lo tiene.
3. Le repite el evento, la fecha, el horario y el precio, y le manda el enlace de Mercado Pago.
4. El evento permite elegir entre dos modos en **Evento → Vigencia del enlace**:
   - **Con caducidad** (predeterminado): su bici queda apartada **30 minutos**, configurable de
     5 a 240 minutos. El enlace vence a la misma hora.
   - **Enlace sin caducidad**: el agente verifica disponibilidad antes de generar o reenviar
     el enlace, pero no aparta bicicleta ni reduce el cupo mientras el cliente paga. El cupo se
     verifica otra vez cuando Mercado Pago acredita el pago. Si ya no hay lugar, el pago queda
     registrado para revisión del administrador y reembolso manual; no se confirma una entrada.
   Ambos modos conservan tarjeta o saldo de Mercado Pago; no aceptan OXXO, cajero ni transferencia.
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
| Pago que llega después de que venció un apartado con caducidad | Si aún hay lugar, lo confirma (puede cambiar la bici). Si no, lo reembolsa y avisa al cliente. |
| Pago de enlace sin caducidad cuando ya no hay lugar | Registra el dinero en Finanzas, sin entrada ni bicicleta; queda «En revisión» con reembolso manual requerido. Avisa al cliente que el administrador debe revisarlo. No solicita un reembolso automáticamente. |
| Pago de una compra cancelada o de un evento cancelado | Con caducidad, lo reembolsa; con enlace sin caducidad, requiere revisión y reembolso manual. |
| Segundo pago para una entrada ya pagada | Reembolsa el pago extra; la entrada sigue confirmada. |
| El importe o la moneda no coinciden | No confirma; deja la compra «En revisión» y avisa que un asesor la contactará. |
| Falla el reembolso automático | Deja la compra «En revisión» para reembolsarla a mano. |

Las situaciones anómalas quedan registradas en `webhook_logs`, con `event_type` que empieza con
`special_event_` y `processed = 0`, para conciliarlas.

Cada 2 minutos un barrido revisa los apartados vencidos. Antes de liberar uno, le pregunta a
Mercado Pago si ese pago sí se aprobó (por si se perdió el aviso).
El barrido también concilia los enlaces sin caducidad en rotación y nunca los vence por tiempo.

## Revisar pagos sin cupo y reembolsos manuales

En **Participantes**, «Pago sin cupo · revisar reembolso» muestra el importe, el identificador de
Mercado Pago y el motivo. El encabezado del evento indica cuántos casos siguen pendientes.

1. Seleccionar la compra y pulsar **Revisar reembolso**.
2. Verificar el pago en Mercado Pago y marcar **Revisé el pago y autorizo el reembolso**.
   Se muestran participante, pago, importe y motivo; sin esa confirmación no se
   puede continuar.
3. **Verificar y reembolsar** consulta nuevamente Mercado Pago y valida identificador,
   referencia, importe, moneda y estado antes de solicitar la devolución. Registra la cuenta
   del administrador y la fecha de revisión. Solo esta decisión explícita puede iniciar el
   reembolso de estos casos; el chatbot y los procesos periódicos no lo hacen.
4. Si el administrador ya hizo el reembolso completo directamente en Mercado Pago, se
   comprueba ese estado y se registra sin solicitar otra devolución. El webhook también puede
   registrar esa devolución. Una devolución parcial requiere terminar la revisión en Mercado Pago.

Guardar un cambio de modo o de minutos en un evento publicado afecta **compras nuevas**. Cada
compra conserva su modo y vencimiento originales. Los enlaces sin caducidad tienen `expires=false`
sin fechas de vencimiento; se pueden desactivar explícitamente al cancelar la compra o el evento.
Un pago de enlace abierto que llega cuando el evento ya no está a la venta también requiere
revisión y reembolso manual, y no da acceso al evento.

## Atender a los clientes desplazados

En **Clientes desplazados** aparece cada cliente con su clase original, su bici, su membresía
y la clase extra que le tocaría.

- **Registrar aviso**: guarda la fecha, el canal y quién avisó.
- **Registrar compensación**. La opción por defecto es la **clase extra al terminar su
  membresía**, y es la única que el sistema aplica:
  - Se busca el siguiente día con clase a su hora de siempre después de su última clase. Si su
    última clase es un martes, el miércoles siguiente sigue activo y tiene su reserva; si es un
    viernes, el lunes.
  - Su última clase es la última que cubre su membresía o su horario fijo, la que sea más tarde.
  - Se reserva en su bici si está libre; si no, en otra. Si esa clase está llena, se pasa al
    siguiente día.
  - Su membresía queda activa hasta ese día (y si ya renovó, se extiende la renovación).
  - Si su membresía ya terminó, la clase extra es la siguiente a partir de hoy.
  - Si esa clase aún no está generada (las clases se generan hasta donde llega la membresía
    más larga de cada horario), se genera igual que al inscribir a alguien y se reserva.
  - Se registra qué se hizo (día, hora, bici, vigencia anterior y nueva) y ya no se cambia.
  - Sin membresía no hay clase extra: se registra otra compensación.
- Las demás compensaciones (reposición, acceso al evento, saldo a favor, devolución, «no
  requirió» u otra) solo se registran, con notas; se aplican como siempre en Membresías o en
  Clases.
- **«Dar clase extra a los pendientes»** se la da a cada cliente sin compensación, después de
  mostrar la lista de lo que se hará.

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

- **Migración `b8e1f4a7c0d3`**: añade `payment_link_expires` al evento y a cada compra, con
  predeterminado `true`, conservando el comportamiento y los vencimientos anteriores. Añade
  `manual_refund_required`, `refund_reviewed_by` y `refund_reviewed_at` a las compras. Los enlaces
  abiertos pendientes no pueden tener `seat_id` ni `hold_expires_at`; lo exige la base.
- **Contrato GraphQL**: `SpecialEventInput.paymentLinkExpires` es opcional, y omitirlo al
  actualizar conserva la selección. El evento expone `paymentLinkExpires` y
  `manualRefundsPending`. Las compras exponen el modo, la revisión y el reembolso requerido.
  `refundSpecialEventOrder(reviewConfirmed: Boolean = false)` exige confirmación y una cuenta
  autorizada para los casos de revisión manual.
- **Concurrencia**: el bloqueo del evento serializa la comprobación y asignación de cupo al
  pagar. El bloqueo de la compra serializa la generación del enlace y reutiliza la preferencia
  ante reintentos, sin mantener bloqueado el inventario durante la llamada a Mercado Pago.
- **Despliegue**: aplicar primero la migración y desplegar el backend; distribuir después el
  frontend. Los eventos conservan el modo con caducidad hasta que un administrador elija el
  nuevo modo y guarde. No se recalculan enlaces ya emitidos. La reversión se bloquea si quedan
  enlaces abiertos pendientes o reembolsos manuales sin resolver; cerrar y verificar esos
  enlaces en Mercado Pago antes de revertir.

- **Tablas:**
  - `special_events`, `special_event_seats` y `special_event_orders` (apartado, pago y entrada).
  - `special_event_takeovers` (quién sustituyó qué clase y cuándo).
  - `special_event_displacements` (seguimiento manual). La clase extra se registra con
    `compensation_type = 'membership_extension'` y la aplica `app/crud/special_events/bonus.py`.
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
