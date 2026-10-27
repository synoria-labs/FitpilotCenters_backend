# Ventas de membresías con fecha confirmada

## Contrato de la venta

El servidor conserva una orden con precio, modalidad, grupo, fecha de inicio y periodo antes de generar el checkout. La propuesta pide una confirmación humana nueva; una pregunta sobre efectivo o transferencia no la sustituye. Tarjeta y transferencia usan la misma preferencia de Mercado Pago. El bot no registra efectivo ni acredita comprobantes.

Los paquetes nuevos comienzan a medianoche de la fecha elegida en `America/Mexico_City`, guardada en UTC. El final es exclusivo. Un semanal del martes 27/10/2026 termina el martes 03/11/2026 a las 00:00: cubre hasta el lunes 02/11 inclusive. El mensual usa meses calendario. Los créditos y el acceso libre mantienen su modalidad; el pase diario sigue ligado a su sesión. `period_rule_version=1` conserva los registros anteriores; las compras nuevas usan la versión 2.

El pago aprobado se registra de forma duradera antes de asignar cupo. La aplicación de suscripción, reservas y bicicleta es atómica. Los bloqueos de pago, orden, socio y materialización evitan duplicar una venta o vender dos veces la última bicicleta. Un fallo transitorio conserva el pago y puede reintentarse. Los webhooks pendientes no activan membresías.

Si la aprobación ocurrió después del inicio de la primera clase, la orden queda `paid_awaiting_start`. Se conserva el importe y se solicita una fecha nueva sin otro enlace. Un webhook demorado con aprobación oportuna conserva la fecha contratada. La falta de fecha de aprobación verificable, cambios de contrato o importes discordantes requieren revisión. Un pago tardío por sí solo no genera devolución; los fallos de cupo mantienen el procedimiento existente para membresías. Los eventos especiales conservan su política independiente.

## Administración y reversión

La pestaña Chatbot permite configurar `offerInPersonRegistration` y `salesV2Enabled`. El personal gestiona el registro presencial; elegirlo desde WhatsApp no aparta lugares. Antes de cancelar un checkout se consultan pagos aprobados o en proceso.

El panel **Pagos recibidos pendientes de aplicar** muestra órdenes cobradas pendientes de inicio y casos en revisión, con la referencia de Mercado Pago disponible. Requiere `MANAGE_SUBSCRIPTIONS`. Para `paid_awaiting_start`, confirmar la fecha con el cliente, seleccionarla y aplicar el pago existente. Los pases diarios requieren seleccionar una sesión disponible. Los casos `needs_review` no se aplican a ciegas: el equipo debe revisar la causa y el pago en Mercado Pago.

Las membresías prepagadas futuras aparecen **Por iniciar**. Las consultas de actividad requieren `start_at <= ahora < end_at`; los pagos pendientes y las cancelaciones conservan su estado.

Despliegue en orden:

1. Validar migración, pruebas y evaluación con PostgreSQL aislado. Respaldar y registrar la revisión de producción.
2. Aplicar la migración aditiva `e1a4c7d0f3b6`, sin reparar ventas históricas. Sus flags comienzan desactivados.
3. Publicar backend y verificar salud, GraphQL y reconciliación; publicar después el cliente.
4. Para Love Fitness habilitar `offerInPersonRegistration` y `salesV2Enabled`, conservando `claude-haiku-5-5`, la confirmación y el requisito de pago acreditado.
5. Revisar órdenes pagadas pendientes de fecha, errores de asignación, turnos sustituidos y confirmaciones rechazadas. Guardar una configuración no cobra ni envía mensajes.

Para detener nuevas propuestas durante una reversión, desactivar **Activar el chatbot** (`enabled=false`). No borrar órdenes ni degradar la base: el webhook y la conciliación por minuto continúan resolviendo órdenes ya cobradas aun con las nuevas propuestas deshabilitadas. No revertir a un binario anterior al soporte de estas órdenes mientras existan cobros por conciliar.

## Validación

Suite local: PostgreSQL 16 efímero, backend completo y pruebas portables de Qt con `QT_QPA_PLATFORM=offscreen`. Se prueban pagos repetidos o desordenados, interrupción tras registrar dinero, concurrencia en conexiones independientes, ausencia de cupo, fecha y precio reconfirmados, membresías futuras, calendario semanal y mensual, recepción, transferencia, capturas y regresiones de eventos/pases diarios.

`python -m scripts.evaluate_chatbot_sales --repetitions 3 --output docs/chatbot-sales-v2-model-evaluation.json` compara los modelos sobre diez escenarios anónimos. Sobrescribe la URL de base con un destino inaccesible antes de importar la aplicación; sustituye herramientas por implementaciones en memoria y bloquea creación, cancelación y devolución de pagos reales. Usa el agente y el validador final reales. Requiere una credencial Anthropic en el entorno; nunca la escribe en el informe.

Resultado del 26/10/2026:

| Modelo solicitado y efectivo | Pertinencia | Mediana de latencia | Máximo de palabras | Herramientas | Tokens de entrada / salida |
| --- | --- | --- | --- | --- | --- |
| Haiku 5.5 | 30/30, 100 % | 4.118 s | 52 | 68 | 467942 / 17382 |
| Sonnet 5.5 | 29/30, 96.7 % | 5.392 s | 64 | 69 | 450466 / 13159 |

Ningún escenario creó un cobro real. La cobertura financiera se valida adicionalmente con PostgreSQL y proveedores simulados, incluidas dos conexiones independientes compitiendo por una sola bicicleta. La transferencia se comprobó también por lectura de la API de la cuenta: `clabe`/STP activa, sin exclusión en la preferencia consultada y `binary_mode=false`. La disponibilidad que muestre Mercado Pago a cada pagador sigue dependiendo del proveedor.

Sonnet omitió la herramienta final en una de las tres consultas sobre inicio sin fecha. La aplicación descarta ese texto libre y usa una aclaración segura. Haiku no presentó ese fallo en esta muestra. Treinta ejecuciones por modelo no garantizan el mismo porcentaje en todas las conversaciones reales. Se mantiene Haiku y se conserva el informe completo con respuesta, herramientas, validación, tokens y latencia para repetir la comparación.

## Seguimiento

`app.chatbot_turn_audit` registra mensaje y mensajes relacionados, modelo solicitado/efectivo, reglas, herramientas, validación, orden, duración y consumo disponible. Un turno sustituido por entradas nuevas se registra como `superseded` y no envía una respuesta obsoleta. Los mensajes recibidos durante preparación se agrupan; una operación iniciada conserva su ejecución.

Consultas útiles, de solo lectura:

```sql
SELECT status, count(*) FROM app.membership_purchase_orders GROUP BY status;
SELECT validation, reply_kind, count(*) FROM app.chatbot_turn_audit
WHERE created_at >= now() - interval '24 hours' GROUP BY validation, reply_kind;
SELECT requested_model, effective_model, count(*), avg(elapsed_ms)
FROM app.chatbot_turn_audit WHERE created_at >= now() - interval '24 hours'
GROUP BY requested_model, effective_model;
```

El diagnóstico histórico se ejecuta con `python -m scripts.diagnose_chatbot_sales` en una transacción de solo lectura. No modifica pagos, periodos ni reservas, y no encola mensajes. Véase [diagnóstico inicial](chatbot-sales-v2-historical-diagnosis.md).
