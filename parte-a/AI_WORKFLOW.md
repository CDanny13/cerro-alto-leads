# Registro de uso de IA

## Instrucciones del usuario

Se pidió analizar antes de editar: los seis documentos de `Contexto/`, los diez JSON, el emisor original y el resto del repositorio. Después se autorizó implementar por etapas en Python >=3.11, FastAPI, SQLite, HTTPX, Pydantic y Pytest, con LLM real configurable. Se exigieron conservación de originales, pruebas en cada etapa, ausencia de secretos y ninguna publicación sin aprobación.

## Cómo se utilizó

Codex asistió en lectura de requisitos, arquitectura, implementación, redacción del prompt, construcción de pruebas, documentación y corrección de fallos. No se delegó a subagentes. Las herramientas locales editaron código y ejecutaron las pruebas; se consultaron documentos oficiales de FastAPI, SQLite, HTTPX, Pydantic, Python, Gemini, Ollama y Slack para los contratos técnicos.

La configuración relevante de trabajo está versionada en `AGENTS.md`; el prompt operativo en `prompts/clasificar_lead.txt`. Este archivo resume las instrucciones de construcción recibidas en el chat, sin copiar datos personales ni credenciales.

## Etapas verificadas

1. Recepción y persistencia: 11 pruebas inicialmente aprobadas. Cobertura de los diez archivos, idempotencia concurrente, datos inválidos y reinicio.
2. Pacientes: 23 pruebas acumuladas aprobadas. Paginación, familias, tipos de error, caché, rate limit y reinicio del límite.
3. Clasificación y rutas: 41 pruebas acumuladas aprobadas. Esquema, evidencia, presupuesto, mensaje largo, inyección y reglas clínicas/comerciales.
4. Robustez: incorporación de cola de avisos, recuperación, administración y pruebas adicionales. Un test detectó un acceso incorrecto al byte bloqueado en Windows; se corrigió. Otro detectó un sobre de respuesta LLM mal tipado; se añadió validación explícita. La suite final se registra en `VALIDACION.md`.

Resultado final: 73 pruebas aprobadas y una integración real omitida por falta de configuración. Se comprobó el script de los diez payloads sobre Uvicorn y HTTP local con integraciones simuladas explícitas, y un arranque independiente sin credenciales que registró fallos sin simular procesamiento exitoso. También pasaron compilación, compatibilidad de dependencias y comprobación de diferencias de Git.

Se preparó un intérprete Python 3.12.10 local en `.tools/` porque el equipo solo tenía el alias de Microsoft Store; dependencias en `.venv/`. Ambos están ignorados por Git y no son requisitos especiales de la entrega.

## Diferencia entre IA de desarrollo y LLM del sistema

Codex ayudó a construir y revisar la solución. El sistema entregado llama por HTTP al modelo configurado en `.env`; no llama a Codex ni usa las respuestas de desarrollo como clasificación. Las respuestas predefinidas de los tests viven exclusivamente en `tests/` y se inyectan con HTTPX MockTransport.

No se disponía de credenciales de pacientes/LLM/avisos durante la implementación. Las pruebas offline no certifican precisión de un modelo real, acceso a la API personal, latencia ni entrega real de avisos. Los scripts de integración fallan explícitamente si no pueden hacer esas llamadas.

## Corrección de autorización · 17 de octubre de 2026

El usuario autorizó corregir el 503 provocado por encabezados administrativos no ASCII y completar la documentación de ADMIN_TOKEN. Primero se añadieron seis casos de regresión y se comprobó que fallaban reproduciendo el defecto. Después se añadió la validación ASCII previa a la comparación segura y se documentaron generación, configuración, Bearer, endpoints protegidos y el token opcional de Slack. No se mostraron ni cambiaron valores de `.env`, no se enviaron alertas reales y no se publicó el repositorio. La suite offline posterior pasó con 79 pruebas aprobadas y una integración real excluida; el detalle se registra en `VALIDACION.md`.

## Compatibilidad con OpenAI · 17 de octubre de 2026

Ante un HTTP 400 comunicado por el usuario, se revisó la solicitud del cliente y la documentación oficial de OpenAI para su modelo GPT-5.4. Una solicitud real de diagnóstico confirmó `param=max_tokens`; solo se imprimieron estado y parámetro de una lista permitida. Se añadió selección configurable del parámetro de límite y cuatro casos de prueba de contratos, conservando modelo, credenciales, validaciones y límite. Los casos que exigían `max_completion_tokens` reprodujeron el defecto antes de la corrección.

La API pasó a devolver 200; una evidencia no literal fue rechazada correctamente. Se precisó el prompt con ejemplos sin relajar la validación. La comprobación real aislada posterior aprobó la clasificación. Una comprobación conjunta encontró un 500 en pacientes, por lo que no se afirmó éxito del flujo completo ni se enviaron alertas a Slack. La suite offline terminó con 83 aprobadas y una integración excluida. Los resultados se registran en `VALIDACION.md`.

## Documentación de evaluación y Slack · 17 de octubre de 2026

El candidato compartió una comprobación real exitosa con GPT-6 Luna y Slack, el resultado de los diez archivos y capturas del canal. Se contrastó el comportamiento con código y, en la revisión anterior, con estados de SQLite en modo lectura. Se registran los resultados como evidencia del candidato y comprobación local, no como una nueva ejecución externa de Codex.

Se confirmó que los tres comandos de las capturas ya existían en README; se agruparon en un recorrido con terminales, orden, salidas esperadas, idempotencia y diagnóstico. Se añadió la creación de la app/canal/Incoming Webhook siguiendo documentación oficial de Slack, configuración de las variables y explicación de los tipos de aviso. No se copiaron URLs privadas, identificadores del espacio, nombres personales ni secretos de las capturas. No se alteró el código ni se enviaron mensajes reales durante esta actualización documental.

## Revisión humana pendiente

El candidato debe revisar los supuestos de `DECISIONES.md`, completar feriados para uso operativo, revisar los archivos que publicará y poder explicar todas las piezas en entrevista. La ejecución real de los diez ejemplos ya fue aportada y revisada; cada evaluador debe configurar sus propias credenciales y reproducirla. Codex no hizo push ni entregó la prueba.
