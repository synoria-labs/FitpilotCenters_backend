# Pases diarios por sesión y respuestas verificadas

Los planes con `duration_unit=day` y `duration_value=1` se venden para una única
`class_sessions.id`. El pago, socio (si es nuevo), suscripción y reserva se guardan
en una transacción. `membership_subscriptions.session_id` identifica la clase;
`start_at` es la acreditación y `end_at` es su fin programado. No depende del
check-in, del cierre manual ni de un barrido periódico. No extiende ni cancela
otras membresías. Los paquetes de horario fijo y el acceso libre siguen las reglas
de cobertura descritas abajo.

## API y clientes

`CreateSubscriptionInput`, `CreateMemberEnrollmentInput`, `RenewSubscriptionInput`
y `SaleLineInputType` aceptan `sessionId: Int`. Es obligatorio para vender un pase
diario cuando `DAY_PASS_SESSION_REQUIRED=true` (valor predeterminado). Sin él se
rechaza la operación antes de cobrar. Los otros planes pueden omitirlo.
`Subscription.sessionId` es nullable para compatibilidad con el historial.

### Registro en recepción y renovaciones solapadas

Los formularios de recepción pueden seleccionar clases que ya empezaron o
terminaron, tanto programadas como completadas, para registrar pagos posteriores
a la clase. Una clase cancelada o desplazada sigue sin poder venderse. Esta
excepción se habilita únicamente desde las mutaciones autenticadas con permisos
de caja o gestión de suscripciones; el chatbot y la compra pública siguen
exigiendo una clase futura. Un pase histórico se registra desde el inicio de su
clase y conserva el vencimiento real, con estado `expired` si ya terminó.

El selector de sesión aparece solo en planes de un día. Semanales y mensuales
conservan el selector del grupo de horario, incluida la precarga asíncrona del
último plan. Una membresía vencida sugiere hoy como inicio, y recepción puede
cambiarlo para registrar una clase anterior dentro del rango del calendario.

Si las fechas coinciden con una membresía pagada o reserva del mismo socio, el
servidor devuelve `MEMBERSHIP_OVERLAP` antes de confirmar el cobro. Caja y
renovación solicitan contraseña de administrador y reintentan la operación con
`adminPassword`. Se comprueba contra cuentas activas con rol administrador; una
clave errónea devuelve `ADMIN_PASSWORD_INVALID`. Cancelar el diálogo no cobra ni
altera el carrito. Las contraseñas no se guardan en el ticket ni en sus metadatos.

La autorización conserva las demás membresías pagadas. Para una misma clase y
socio se reutiliza la reserva física existente, conservando bicicleta y registro
de asistencia, sin consumir otro lugar. El ticket guarda el identificador de la
cuenta que autorizó el solapamiento en `overlap_authorized_by`. El servidor
serializa estas comprobaciones con la creación de reservas y confirma pago,
suscripción y ticket en una sola transacción.
El chatbot deriva estas compras a recepción antes de generar un enlace de pago;
no solicita contraseñas por WhatsApp. La compra vuelve a comprobar el conflicto
al acreditarse el pago por si apareció otra membresía desde la propuesta.

No requiere migración. Desplegar primero el backend (campos GraphQL aditivos) y
después actualizar y reiniciar el escritorio.

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

1. Aplicar y validar las migraciones `e5b8c1d4f7a0` y `f6c9d2e5a8b1` (columnas
   nullable, FK e índices para vincular pases y reservas con su compra).
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

## Cobertura de los paquetes

El número de bicicletas pertenece al cupo de la sesión y a sus asientos, no al
límite de clases del plan. Cada tipo de membresía tiene una regla diferente:

- **Horario fijo:** todas las ocurrencias del grupo (disciplina, sede, hora e
  instructor) entre `start_at` inclusive y `end_at` exclusive. Un semanal de
  lunes a viernes incluye cinco clases; de lunes a domingo, siete. El mensual
  cuenta los días reales de su calendario; no asume cuatro semanas ni 4–5 clases.
  Las clases canceladas o desplazadas no son clases disponibles. Los cupos y las
  excepciones/reprogramaciones siguen siendo controles independientes.
- **Acceso libre:** sin límite de clases ni horario fijo durante la vigencia.
  Cada reserva requiere disponibilidad; comprarlo no aparta todas las clases.
- **Créditos:** `class_limit` es la cantidad de créditos contratados. Las reservas
  activas, asistencias e inasistencias consumen crédito; las canceladas no. Una
  suscripción se bloquea durante el consumo para impedir gastar el último crédito
  simultáneamente en dos clases. Los límites diarios/semanales opcionales solo
  corresponden a este tipo de plan.

En nuevos semanales, la vigencia es un intervalo completo de siete días desde su
inicio. Así, comprar después de la clase de hoy incluye la correspondiente clase
de la siguiente semana, sin añadir una octava clase. Una renovación comienza al
finalizar la anterior, que conserva su estado y reservas mientras siga vigente.
No se recalculan fechas ni se actualizan suscripciones históricas.

`Subscription.coverage { mode includedClasses schedule }` expone la cobertura
efectiva. Para horario fijo se calcula por sus reservas recurrentes y fechas; el
valor histórico de `membership_plans.class_limit` no recorta ese calendario.
Al guardar planes fijos o de acceso libre se limpia el límite manual: `NULL` en
horario fijo significa **calculado por calendario**, y en acceso libre significa
**sin límite**. El cliente muestra estas reglas por separado.

Las reservas nuevas guardan `reservations.subscription_id` para atribuir el
consumo a su compra sin modificar el historial. En paquetes de créditos antiguos,
las reservas sin vínculo dentro de su vigencia se cuentan de forma conservadora.
Las nuevas reservas regulares validan socio, vigencia, grupo/créditos y cupo antes
de guardarse; las reprogramaciones autorizadas conservan la cobertura original.

El catálogo del chatbot muestra grupos completos con un `template_id` utilizable.
`get_membership_coverage` consulta su cobertura y `membership_information` redacta
la explicación en el servidor; las propuestas antiguas se reconstruyen al leerlas
en vez de reenviar resúmenes con un solo día. Una compra nueva comienza al
acreditarse el pago, no al llegar el día de la plantilla elegida. El acceso libre
y los créditos se compran sin plantilla; `reserve_with_membership` reserva una
clase cubierta sin volver a cobrar un pase. Los logs añaden
`chatbot_membership_verification` para auditar estas respuestas.

La regresión de Ana Luisa conserva un resumen antiguo de miércoles y comprueba
que la respuesta incluya lunes a viernes a las 20:00. Las pruebas también cubren
semanas de cinco/siete clases antes y después de la hora de clase, todo el mes,
renovación anticipada, acceso libre, cupo de bicicletas y consumo concurrente.
