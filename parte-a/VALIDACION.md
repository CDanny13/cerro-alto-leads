# Validación de la implementación

Entorno local: Windows, Python 3.12.10, dependencias de `requirements.lock.txt`.

Última validación offline, 17 de octubre de 2026: **83 aprobadas, 1 excluida**, en 10,87 segundos (`python -B -m pytest -p no:cacheprovider -m "not integration" -q`). Incluye cuatro casos nuevos para selección automática y explícita de `max_tokens`/`max_completion_tokens`, conservando modelo, límite y validación. La prueba de integración real se excluyó de esa suite; las comprobaciones reales separadas se detallan abajo.

Validación previa de autorización: **79 aprobadas, 1 excluida**, en 12,12 segundos con el mismo comando. No se hicieron llamadas a terceros para aquella corrección.

Se añadieron seis casos de regresión para autorización: los cinco endpoints rechazan encabezados ausentes, Bearer incorrecto, token sin Bearer, Basic y bytes no ASCII con 401; el token correcto permite ejecutar la operación. Una configuración de token no ASCII también se rechaza con 401. Los seis casos fallaron antes del cambio por el 503 original y aprobaron después. `git diff --check` pasó.

Validación inicial de la implementación: **73 aprobadas, 1 omitida**, en 9,21 segundos (`python -m pytest -q`). La omitida requiere API y LLM reales. En ambas ejecuciones hay una advertencia de deprecación de Starlette/AnyIO en el cliente de pruebas; no afecta los resultados y queda visible, sin ocultarla.

También pasaron `pip check` (sin dependencias incompatibles), `compileall` del código y scripts y `git diff --check`. No hay diferencias en los payloads ni en `enviar_payloads.py`.

La suite offline usa servicios falsos identificados mediante `httpx.MockTransport`; el procesamiento, SQLite, validación, reglas, endpoints y ciclo de vida de FastAPI son reales. No hay salidas simuladas en la configuración de producción.

Una prueba levanta Uvicorn en un puerto local y ejecuta la función de `verificar_payloads.py` contra HTTP real: diez recepciones, nueve IDs distintos, tres avisos esperados y rutas comprobadas. En esa prueba las tres integraciones siguen siendo mocks explícitos; no se presenta como inferencia real.

Se ejecutó además Uvicorn como proceso independiente con SQLite de smoke aislado y sin credenciales: `/health` respondió 200, `/ready` 503; un lead válido recibió 202 y terminó fallido con `pacientes_sin_configurar`; JSON inválido recibió 202 y quedó rechazado. El proceso de smoke fue detenido. El script de integraciones reales terminó con error explícito `integraciones_sin_configurar`, sin reemplazar servicios ni intentar simular éxito.

Comando reproducible:

```sh
python -m pytest -m "not integration" -q
```

Se comprobaron recepción y persistencia de los diez originales, duplicados por ID y comerciales, concurrencia, identidad familiar, contactos/metadatos inválidos, rechazos recuperables para revisión, JSON malformado, límites de tamaño, paginación de pacientes/tratamientos, rate limit de 30/minuto persistido, Retry-After y errores 400/401/404/429/5xx/red. También esquema/evidencia/presupuesto del LLM, inyección del evento 10, intención al final de 07, rutas, mensajes, aviso urgente previo, entrega/reintentos, fallos persistidos, reinicio, vencimiento de reservas y recuperación mediante token.

La prueba marcada `integration` queda omitida por defecto. Requiere `RUN_REAL_INTEGRATION=1` y configuración real; usa datos ficticios, sin mocks, y falla ante errores del servicio. El script `verificar_payloads.py` prueba por HTTP contra la aplicación levantada y servicios que haya configurado el operador.

## Comprobación real de OpenAI · 17 de octubre de 2026

Con la configuración local del usuario, una llamada inicial a GPT-5.4 devolvió HTTP 400 y señaló `max_tokens` como parámetro rechazado. Se imprimieron únicamente estado y nombre de parámetro de una lista permitida. Tras adaptar la selección del parámetro, la API devolvió HTTP 200. La validación detectó una evidencia que no era cita literal; se precisó el prompt con ejemplos, manteniendo la validación estricta.

La comprobación aislada posterior del LLM devolvió **HTTP 200 y clasificación validada**, usando el mensaje ficticio `info`, servicio Valoración y contexto nuevo. No se registraron cuerpos externos ni credenciales, y no se cambió el modelo o la clave del usuario. Se revisó la documentación oficial de OpenAI sobre GPT-5.4 y Chat Completions.

La comprobación completa mediante `verificar_integraciones.py` encontró **pacientes_http_500** antes de invocar el LLM. Por ello esta revisión no certifica el flujo real completo, tratamientos ni avisos; no se enviaron mensajes a Slack. El worker de la aplicación conserva los reintentos de 5xx; el script de comprobación informa el fallo y debe repetirse cuando el servicio esté disponible.

## Ejecución real posterior aportada por el candidato · 17 de octubre de 2026

El candidato ejecutó `verificar_integraciones.py --alerta` con GPT-6 Luna y compartió una salida correcta: api_health=ok, busqueda_y_enriquecimiento=ok, llm_json_validado=true, presupuesto_estimado=null y alerta_real=enviada. Después ejecutó `verificar_payloads.py` y compartió el mensaje final de verificación completada y los avisos visibles en Slack.

La revisión local de la base en modo lectura confirmó esta ejecución: diez recepciones y nueve event_id distintos, ocho eventos canónicos procesados y uno duplicado comercial. La segunda recepción de 01 (archivo 02) estaba marcada como duplicada, con un único lead original. No había eventos pendientes ni fallidos. Los resultados fueron:

| Archivos | Resultado final |
| --- | --- |
| 01 y 07 | Procesados/calientes por solicitud de agendamiento |
| 02 | Recepción duplicada de 01; comparte su event_id |
| 03 | Duplicado comercial de 01 |
| 04, 05, 09 y 10 | Procesados/tibios |
| 06 | Procesado/caliente por tratamiento pendiente confirmado |
| 08 | Procesado/basura por publicidad ajena a la clínica |

Se comprobaron siete avisos registrados como enviados: tres calientes y cuatro errores de pacientes recuperados (respuesta inválida, HTTP 500, error de red y HTTP 429). Un estado pendiente en esos avisos históricos no era el estado final del evento. El candidato aportó la captura del canal; no se copian sus identificadores, datos personales o Webhook URL al repositorio. No es necesario que otra ejecución reproduzca esos errores externos ni el mismo número de avisos.

Esta evidencia corresponde a la ejecución del candidato y a la inspección local posterior. La actualización documental no vuelve a llamar a pacientes o al modelo ni vuelve a enviar notificaciones. El README reúne los comandos exactos y los pasos para que un evaluador cree su propio webhook y repita el recorrido.

Pendiente: reproducción en el entorno del evaluador y calendario observado de feriados para uso operativo. No se han medido disponibilidad continua ni SLA de terceros. Una ejecución exitosa de los diez ejemplos no garantiza el comportamiento de todo evento desconocido, ni convierte los tests offline en pruebas reales.
