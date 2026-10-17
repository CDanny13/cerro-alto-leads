# Decisiones y alcance · Parte A

Fecha de trabajo: 15 de octubre de 2026, hora de Ecuador continental. La Ficha manda en reglas clínicas/comerciales; el brief de Parte A precisa aceptación y contrato técnico. No se alteraron los documentos ni payloads originales.

## Recepción y contrato

- ACK 202 solo tras commit SQLite, con una respuesta adicional de recepción que incluye `receipt_id`. El endpoint de estado conserva exclusivamente los ocho campos del contrato. Si falla persistencia se devuelve 503 para permitir reintento del conector.
- Cada recepción se conserva. Por ID se consulta el evento canónico; por `desde`, cada recepción con su estado actual o condición de reenvío. Así el segundo envío de 01 aparece duplicado sin sobrescribir el resultado original. Mismo ID con otro contenido: recepción rechazada, original intacto.
- Un ID ausente, vacío o de tipo incorrecto se representa como null, con identificador interno y receipt_id recuperable. No se deduplican por contenido eventos sin ID: no hay una identidad de entrega fiable.
- `desde` filtra recepción del servidor en UTC. Las fechas futuras de los ejemplos no invalidan leads. Metadatos defectuosos se marcan; la ausencia de contacto válido sí rechaza. Nombre vacío es admisible.
- JSON inválido se registra y rechaza con 202 para evitar tres reenvíos inútiles. Cuerpos de más de 256 KiB conservan un fragmento limitado y motivo explícito; no se promete conservar un cuerpo arbitrariamente grande. Mensajes por encima de 30.000 caracteres se rechazan para revisión; el evento 07 se procesa completo.
- Teléfonos locales se interpretan en Ecuador; se admiten internacionales válidos y enteros sin pérdida decimal. Correos se normalizan sin consultar DNS/buzones; se admite el dominio ficticio `.example`. La validez sintáctica no garantiza contactar a una persona real.

## Duplicación e identidad

- La restricción única de event_id y las transacciones cubren carreras y reintentos. No se vuelve a invocar API/LLM ni avisar por un reenvío exacto.
- Duplicado comercial: nombre normalizado no vacío, ambos contactos válidos e iguales, dentro de 24 h y mismo mensaje o reconocimiento explícito de formulario repetido. Una señal clínica urgente nueva evita ese descarte. Esto identifica 03 con 01 sin fusionar familiares solo por teléfono/correo.
- Cambios de mensaje sin evidencia de reenvío se conservan como otro lead. Quedan posibles duplicados comerciales sin suprimir deliberadamente: se prioriza no esconder necesidades nuevas. Coincidencias parciales o nombres idénticos de familiares requieren revisión humana; no se implementó resolución completa de identidad.
- La búsqueda de pacientes es OR por contacto, pero la selección requiere nombre compatible de al menos dos palabras y una coincidencia de contacto. Todas las páginas se revisan antes de elegir. Varios candidatos compatibles o nombre insuficiente: identidad ambigua, sin consultar/atribuir su historial.

## Integraciones y errores

- HTTPX con timeout, sin redirecciones ni reintentos invisibles. API 400/401/404 son fallos explícitos no reintentables automáticamente; 429 respeta Retry-After y 5xx/red tienen reintentos limitados. Ningún fallo se convierte en "no es paciente".
- Límite local persistente de 30 solicitudes/minuto por token en esta base, compartido por búsquedas y tratamientos. Respeta Remaining=0/Reset. Las esperas por el límite local no consumen intentos de trabajo. No coordina otros programas/bases que usen el token; 429 sigue siendo obligatorio.
- Caché privada de páginas exitosas durante cinco minutos; se excluye fecha de nacimiento y campos no usados. Los cursores repetidos y más de 100 páginas son fallos explícitos. La caché expirada se elimina al acceder de nuevo; no es una política integral de retención de datos.
- Salida del LLM: enums/tipos estrictos, claves adicionales prohibidas, evidencia textual y presupuesto numérico sustentado en declaración de capacidad de gasto. Sin presupuesto declarado debe ser null. No se usan Markdown, respuestas truncadas, refusals ni JSON inválido.
- Todos los proveedores deben exponer el protocolo configurado; no se traduce automáticamente a APIs nativas. La selección/calidad/latencia del modelo requieren una prueba real con la cuenta elegida. No se introducen respuestas simuladas si falta configuración o cuota.
- El procesamiento tiene tres intentos persistidos, con espera exponencial (5 y 10 s antes de los siguientes intentos, salvo Retry-After). Luego queda fallido y se avisa. El operador corrige y reprocesa con token.
- Las reservas vencen tras una caída. No se resetean masivamente al arrancar; un único procesador por archivo está impuesto con bloqueo de sistema operativo. No hay infraestructura distribuida en este alcance.

## Rutas, clínica y avisos

- Señales urgentes tienen prioridad; se corroboran con texto clínico. Tratamiento pendiente solo eleva un paciente identificado con seguridad. Ortodoncia del desplegable sin decisión de empezar no basta. Ante duda, tibio.
- Una señal urgente reconocida por las reglas produce aviso temprano aun si pacientes falla. El mismo aviso se reutiliza al terminar, sin generar otra llamada. Las señales detectadas solamente por el modelo llegan después de las integraciones.
- La propuesta siempre solicita coordinación/valoración y confirmación de recepción; no hay API de agenda ni disponibilidad inventada. No se confirma una cita sin requisitos de la Ficha. No se implementa la atención completa de Cami, agendamiento transaccional ni envíos de WhatsApp.
- Presupuesto de la persona y precio del servicio son conceptos separados. Las plantillas usan solo precios/horarios estables de la Ficha y no anuncian promociones que puedan estar vencidas.
- Avisos reales por webhook compatible con `{"text":"..."}` (por ejemplo Slack). Llevan identificadores, motivo y propuesta; el operador obtiene contactos mediante endpoint administrativo. El SLA se mide desde la recepción, no desde la clasificación.
- Solo se marca procesado/caliente después de la entrega HTTP del aviso. Fallo de entrega agotado deja el evento fallido con resultado recuperable; reenviar el aviso no repite el enriquecimiento ni el LLM.
- Se usa Idempotency-Key y el mismo ID al reintentar avisos. Si el receptor no respeta esa clave (p. ej. un webhook que no ofrece deduplicación), una caída después de enviar y antes de confirmar localmente puede duplicar un aviso. No se puede prometer exactly-once frente a un tercero sin ese contrato.
- Calendario nacional no suministrado: configuración explícita de fechas observadas. Sin calendario completo se redactan instrucciones condicionales seguras; la disponibilidad exacta en feriados queda pendiente de configuración.
- Un canal caído no puede notificarse por sí mismo. Hay fallos persistidos, logs sin PII, /ready y métricas; un monitor externo independiente debe vigilar la aplicación. Detección automática de silencio del conector, segundo canal de avisos, cifrado, retención programada y panel gráfico quedaron fuera del alcance de tres horas.

## Autorización administrativa · revisión del 17 de octubre de 2026

- `ADMIN_TOKEN` es un secreto ASCII generado localmente con `secrets.token_urlsafe(32)`, independiente de pacientes, LLM y avisos. Los cinco endpoints administrativos requieren `Authorization: Bearer <ADMIN_TOKEN>`; `operar.py` lee `.env` y lo añade automáticamente.
- Se valida que tanto el token configurado como el encabezado sean ASCII antes de `secrets.compare_digest`. Ausencia, token incorrecto, esquema incorrecto o caracteres no ASCII devuelven 401; antes el último caso causaba TypeError y un 503. Los tokens correctos conservan el comportamiento existente.
- Con Slack Incoming Webhooks, `ALERT_WEBHOOK_TOKEN` permanece vacío y no se envía Authorization. La URL del webhook contiene la autorización; un token separado solo corresponde a receptores que lo exijan.
- Se añadieron pruebas de regresión de los cinco endpoints y de configuración no ASCII. No se consultaron credenciales reales ni se enviaron avisos externos para comprobar esta corrección.

## Compatibilidad de OpenAI · revisión del 17 de octubre de 2026

- Se confirmó una respuesta real HTTP 400 con `param=max_tokens` usando GPT-5.4; el cliente enviaba un parámetro incompatible con ese modelo. Se inspeccionó solo el estado y el nombre del parámetro permitido, sin registrar cuerpos externos ni credenciales.
- `LLM_TOKEN_PARAMETER=auto` elige `max_completion_tokens` para `api.openai.com` y conserva `max_tokens` en otros proveedores. El operador puede seleccionar explícitamente cualquiera de los dos para otros servicios/proxies. No hay reintento oculto, cambio de modelo ni aumento del límite de 1200 tokens.
- Se mantiene la validación del JSON, evidencia y presupuesto. No se modifican secretos ni se requiere regenerar la API key por este error de parámetros.
- Tras recibir HTTP 200, se detectó una evidencia no literal y se precisó el prompt con ejemplos; no se aceptan explicaciones como si fueran citas. La inferencia real aislada posterior pasó la validación. Un 500 independiente en pacientes impidió certificar la comprobación conjunta; queda registrado en VALIDACION.

## Entrega

- README, código, prompt, tests, requisitos reproducibles e instrucciones de IA quedan versionables. Credenciales y SQLite están ignorados por Git.
- Durante la implementación inicial no estaban disponibles las credenciales reales. Posteriormente el candidato configuró pacientes, GPT-6 Luna y Slack y aportó las ejecuciones exitosas; los resultados revisados constan en VALIDACION. No se confunden con los tests offline ni con llamadas nuevas de Codex.
- El README explica cómo cada evaluador crea su canal y aplicación Slack, obtiene su propio Incoming Webhook y ejecuta los tres comandos de evaluación. No se necesita publicar FastAPI para el envío saliente de Slack. El reporte actual omite la línea independiente de 02, pero envía y comprueba sus recepciones como reenvío de 01; se documentó sin cambiar el comportamiento del script.
- Sin push, commit ni entrega desde esta sesión. El usuario revisará y autorizará publicación/entrega.
