# Diagnóstico histórico de ventas — 26/10/2026

Consultas de lectura en producción, sin reparaciones ni envíos. Se encontraron 9 pagos completados identificados por el comentario del bot; 7 conservaron una referencia exacta a su propuesta. La revisión de fechas solo puede utilizar esos vínculos disponibles y los mensajes conservados. No constituye una reconstrucción completa de todas las ventas históricas.

| Evidencia | Hallazgo | Disposición |
| --- | --- | --- |
| Conversación 477, pago 3532, suscripción 3468 | Se pidió comenzar el martes 27/10. La suscripción comenzó el domingo 25/10 a las 21:53, con reservas del lunes 26 al viernes 30. La frase también decía que el lunes se complicaba. | Posible afectación confirmada por la discrepancia de fechas. Revisar con el cliente; no ajustar automáticamente. |
| Conversación 474, pago 3528 | Se solicitó iniciar el mismo día y el inicio registrado corresponde a esa fecha. | No se detectó discrepancia de día mediante esta regla; no supone validación de toda su cobertura histórica. |
| Pagos 3464 y 3213 | No conservan una referencia exacta a una propuesta actual. En 3213 no consta proveedor. | Evidencia insuficiente para atribuir una fecha elegida o acreditar un pago externo. Revisión manual; no presumir que fueron cobros ficticios. |
| Plan 2, «Plan Anual con acceso ilimitado a las clases.» | Está configurado `fixed_schedule`, `fixed_time_slot=true`, duración de 12 meses. El nombre anuncia una modalidad distinta. | Aclarar con administración y corregir la configuración comercial mediante una acción explícita. No cambiar suscripciones existentes. |

Los planes semanal y mensual están configurados como horario fijo. El semanal tiene siete días naturales de vigencia y el grupo de Love Fitness ofrece cinco días de clase. Esos conceptos no equivalen a siete clases ni a la capacidad de bicicletas. El valor histórico `class_limit=14` no debe recortar la cobertura del grupo.

No existe un registro histórico suficiente del modelo efectivo por turno para atribuir estos problemas al cambio Sonnet/Haiku. Se reprodujeron causas del servidor: confirmaciones ajenas a la intención, resúmenes técnicos repetidos, fecha de pago utilizada como inicio y lectura equivocada de una frase con dos referencias temporales. El nuevo registro de auditoría permitirá distinguir el comportamiento del modelo de las reglas y herramientas.
