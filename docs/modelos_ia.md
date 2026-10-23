# Modelos de IA por función

Sonnet 5.5 (`claude-sonnet-5-5`) es el valor predeterminado de todas las funciones
de IA del producto. El identificador y los parámetros se verificaron contra la
[documentación oficial de Anthropic](https://platform.claude.com/docs/en/models/sonnet-5-5/overview).

| Función | Configuración persistida | Control en la UI |
| --- | --- | --- |
| Bot de clientes | `chatbot_config.model` | Chatbot → Bot de clientes |
| Bot de Admin | `owner_agent_config.model` | Agente Admin → Modelo |
| Redactar, corregir y optimizar plantillas | `chatbot_config.template_ai_model` | Chatbot → Asistente de plantillas |
| Optimizar instrucciones del bot | `chatbot_config.prompt_optimizer_model` | Chatbot → Optimizador de instrucciones |

Los modelos se leen en cada solicitud. Guardar uno no modifica los otros, los
prompts, el estado de los bots ni los teléfonos autorizados. La optimización del
prompt también acepta el modelo seleccionado en el editor antes de guardar, sin
persistir ese cambio ni sobrescribir el modelo del chat.

## Contrato backend/frontend

`chatbotConfig` y `saveChatbotConfig` exponen `model`, `templateAiModel` y
`promptOptimizerModel`. Los campos nuevos de entrada son opcionales: los clientes
anteriores pueden seguir guardando sin borrar la configuración independiente.
El bot administrativo conserva `ownerAgentConfig` / `saveOwnerAgentConfig` y su
control existente de permisos.

`aiModelOptions { id label }` devuelve el catálogo compartido para ambos
selectores. La UI muestra nombres legibles, guarda los identificadores reales y
conserva modelos personalizados aunque no aparezcan en el catálogo. El backend
valida formato y longitud; la disponibilidad para una cuenta concreta depende
de Anthropic. La API key sigue en el servidor y no se expone al frontend.

## Sonnet 5.5 y herramientas

Ambos agentes usan `thinking: {type: between_tools}` y `output_config: {effort:
low}` para conservar una respuesta ágil con el presupuesto actual de salida. No
envían temperatura, `top_p`, `top_k` ni selección forzada de herramientas. Los
modelos anteriores conservan sus parámetros habituales. Los auxiliares usan el
mismo constructor compatible con su selección independiente.

## Despliegue

1. Desplegar la migración aditiva `a7d0e3f6b9c2` junto con el backend actualizado,
   después de las migraciones de pases y cobertura de membresías.
2. La migración añade los dos modelos auxiliares con Sonnet 5.5 y actualiza los
   modelos previos `claude-sonnet-4-6` de clientes y Admin a Sonnet 5.5. Conserva
   alternativas seleccionadas expresamente y no cambia prompts ni activación.
3. Distribuir el frontend actualizado. En Chatbot se configuran las tres funciones
   de clientes/contenido y en Agente Admin el modelo administrativo.
4. Comprobar una conversación con herramientas y las dos ayudas de edición con
   la cuenta Anthropic del despliegue. Cambios posteriores desde la UI se aplican
   en la siguiente solicitud, sin despliegue adicional.

La reversión de esquema elimina las columnas auxiliares y restaura el default
anterior, pero no sobrescribe elecciones de modelo posteriores. Para volver a
otro modelo operativo, seleccionarlo desde la UI antes de revertir el backend.

Pruebas: persistencia independiente, guardados parciales, contrato GraphQL,
catálogo autenticado, uso del modelo en los cuatro consumidores, parámetros de
Sonnet 5.5, selección personalizada en Qt y migración de ida/vuelta con prompts
conservados. La prueba local no hace llamadas de generación a Anthropic.
