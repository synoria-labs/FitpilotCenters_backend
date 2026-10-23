# Pases diarios por sesión y respuestas verificadas

Los planes con `duration_unit=day` y `duration_value=1` se venden para una única
`class_sessions.id`. El pago, socio (si es nuevo), suscripción y reserva se guardan
en una transacción. `membership_subscriptions.session_id` identifica la clase;
`start_at` es la acreditación y `end_at` es su fin programado. No depende del
check-in, del cierre manual ni de un barrido periódico. No extiende ni cancela
otras membresías. Los planes de varios días, semanas y meses siguen su cálculo.

## API y clientes

`CreateSubscriptionInput`, `CreateMemberEnrollmentInput`, `RenewSubscriptionInput`
y `SaleLineInputType` aceptan `sessionId: Int`. Es obligatorio para vender un pase
diario cuando `DAY_PASS_SESSION_REQUIRED=true` (valor predeterminado). Sin él se
rechaza la operación antes de cobrar. Los otros planes pueden omitirlo.
`Subscription.sessionId` es nullable para compatibilidad con el historial.

Los formularios de alta/renovación y caja consultan sesiones reales para la fecha
seleccionada. El pase diario no crea un horario recurrente; la bicicleta disponible
se asigna dentro de la transacción. Mercado Pago conserva su verificación y su
flujo de reembolso si ya no es posible reservar. Repetir un identificador de pago
devuelve los mismos registros de la compra, sin duplicarlos.

## Chatbot

Cada mensaje del historial lleva su fecha local original. Cada turno carga las
reservas vigentes, separadas del estado de la membresía. Todas las respuestas
deben pasar por `respond_to_customer`; si el modelo omite la herramienta, su texto
no se envía. Para confirmar una clase exige fecha, sesión, reserva y socio y los
verifica otra vez antes del envío. La confirmación se redacta en el servidor.
Para un horario ambiguo pide aclaración antes de generar un enlace. Las compras
diarias muestran fecha, hora de clase y vencimiento en lugar de copiar el texto
del modelo. Las entradas de eventos tienen una ruta separada que comprueba el pago.

Los logs `chatbot_tool`, `chatbot_turn` y `chatbot_reply_verification` registran
conversación, herramientas y aceptación/rechazo, sin registrar el contenido del
chat ni credenciales. Revisar `verification=not_called` y `verification=rejected`
tras activar el cambio: indican que se bloqueó un texto no verificado.

## Despliegue progresivo

1. Aplicar y validar la migración `e5b8c1d4f7a0` (solo columna nullable, FK e índice).
   No ejecutar backfill. No recalcula vigencias ni modifica los pases históricos.
2. Para una ventana con clientes antiguos, desplegar el backend con
   `DAY_PASS_SESSION_REQUIRED=false`. Las compras que ya envían sesión y el chatbot
   usan siempre la nueva regla; la excepción solo conserva temporalmente las altas
   de clientes antiguos sin `sessionId`.
3. Distribuir el frontend actualizado y comprobar alta, renovación, caja y pagos
   del chatbot contra pruebas. Activar `DAY_PASS_SESSION_REQUIRED=true` y reiniciar
   los procesos del backend. A partir de ese punto todos los canales exigen clase.
4. Verificar una compra de prueba antes del fin y al alcanzar `end_at`. La lectura
   debe mostrar vencida inmediatamente. El job `day_pass_expiry` sincroniza cada
   minuto solo suscripciones nuevas con `session_id` no nulo; repetirlo es inocuo.
5. Supervisar errores de sesión faltante, fallos del job y respuestas rechazadas.
   Para revertir la exigencia a clientes antiguos usar la variable temporal; no
   eliminar la columna ni borrar vínculos de pases vendidos. El rollback del
   cambio de esquema no forma parte de la reversión operativa.

## Verificación local aislada

Usar una base PostgreSQL de pruebas vacía, nunca la de producción. Aplicar
`alembic upgrade head`, configurar los secretos JWT de prueba y ejecutar `pytest`.
En Windows con psycopg, ejecutar Alembic con `WindowsSelectorEventLoopPolicy`,
igual que la fixture de pruebas. Para el frontend, ejecutar sus pruebas con
`QT_QPA_PLATFORM=offscreen` y PySide6 instalado.

La regresión de Karla conserva la reserva de ayer y una membresía histórica
activa, verifica que ninguna permita confirmar hoy y no actualiza esos registros.
