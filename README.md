# Sistema de leads · Clínica Dental Cerro Alto

Implementación de la Parte A de INVERS en Python >=3.11, FastAPI, SQLite, HTTPX y Pydantic. Recibe y conserva eventos antes de responder, enriquece con pacientes, consulta un LLM real y prepara rutas, mensajes y alertas recuperables. La [Ficha del negocio](parte-a/Contexto/ficha-negocio.md) es la fuente de verdad.

Para reproducir la evaluación, sigue este orden: [instalar y completar `.env`](#arranque-desde-cero), [crear el webhook de Slack](#configurar-slack-incoming-webhooks) y [ejecutar las tres comprobaciones reales](#ejecutar-las-comprobaciones-con-servicios-reales). Cada evaluador usa sus propias credenciales y su propio canal.

## Arranque desde cero

Ejecuta desde la raíz del repositorio con Python 3.11 o superior. En Windows comprueba `python --version`; si usas el launcher, puedes sustituir `python` por `py -3`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
```

La copia de `.env` se hace una sola vez. Completa el archivo local antes de procesar leads:

En este equipo ya quedó `.venv` preparada con Python 3.12.10. Si necesitas reconstruirla y los alias `python`/`py` siguen sin reconocer un intérprete, usa `.\.tools\python\python.exe -m venv .venv`. En una máquina limpia instala Python >=3.11 normalmente; `.tools/` no forma parte del repositorio entregable.

| Variable | Valor necesario |
| --- | --- |
| `API_PACIENTES_URL` | URL base entregada por INVERS, sin `/v1` final |
| `API_PACIENTES_TOKEN` | Token personal de pacientes |
| `LLM_API_URL` | URL completa de `/chat/completions` del proveedor |
| `LLM_API_KEY`, `LLM_MODEL` | Credencial y modelo habilitado |
| `ALERT_WEBHOOK_URL` | Webhook real que avise a recepción y reciba JSON `{"text":"..."}` |
| `ADMIN_TOKEN` | Token aleatorio independiente para revisar y recuperar eventos |

Genera tu propio `ADMIN_TOKEN` localmente (no lo entrega INVERS ni el proveedor del LLM):

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copia el resultado en la línea `ADMIN_TOKEN=` de `.env`, guarda el archivo y reinicia la aplicación si estaba encendida. El comando produce un token ASCII aleatorio; cada evaluador puede generar el suyo. No publiques ese valor ni lo reutilices como clave del LLM. Si queda vacío, los endpoints administrativos rechazan todas las solicitudes con 401.

Con Slack Incoming Webhooks, configura `ALERT_WEBHOOK_URL` con la URL privada del webhook y deja `ALERT_WEBHOOK_TOKEN=` vacío: el envío usa la autorización incluida en esa URL. El token opcional de alertas se usa únicamente para receptores que exijan su propio encabezado Bearer; es independiente de `ADMIN_TOKEN`.

Todas las opciones y límites están en [.env.example](.env.example). No se necesitan servicios de pago obligatorios: puedes usar un LLM local real con Ollama y un receptor de avisos gratuito. Ollama local admite la ruta `http://localhost:11434/v1/chat/completions` y un valor de key como `ollama`, que el servidor local ignora; el modelo debe estar instalado y tener capacidad suficiente. [Compatibilidad documentada por Ollama](https://docs.ollama.com/api/openai-compatibility).

Para un proveedor remoto configura su endpoint y modelo. El cliente usa HTTPX, autenticación Bearer y formato Chat Completions, con `response_format=json_object` configurable. Por ejemplo, [Gemini documenta este protocolo](https://ai.google.dev/gemini-api/docs/openai). No se asume que todos los proveedores o modelos admitan los mismos parámetros o tengan cuota gratuita. No hay sustitución por respuestas simuladas.

Para OpenAI puedes mantener el endpoint `https://api.openai.com/v1/chat/completions` y tu modelo configurado. `LLM_TOKEN_PARAMETER=auto` (valor predeterminado incluso si no aparece en tu `.env`) envía `max_completion_tokens` a `api.openai.com`; evita el HTTP 400 de GPT-5.4 al recibir el parámetro antiguo `max_tokens`. En otros proveedores conserva `max_tokens`. Si un proxy u otro servicio exige el parámetro moderno, fija `LLM_TOKEN_PARAMETER=max_completion_tokens`. El límite sigue siendo 1200 tokens y no cambia automáticamente el modelo. [Referencia de Chat Completions de OpenAI](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

```powershell
.\.venv\Scripts\python.exe -m uvicorn cerro_alto.app:create_app --factory --app-dir parte-a --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

En Linux/macOS: `python3 -m venv .venv`, `.venv/bin/python -m pip install -r requirements.lock.txt`, `cp .env.example .env`; usa `.venv/bin/python` en los demás comandos.

`requirements.lock.txt` conserva las versiones verificadas; `requirements.txt` declara dependencias y rangos. No se usa la instalación de Python de `.tools/` para distribuir el proyecto: fue preparación local de este entorno y está ignorada por Git.

## Configurar Slack Incoming Webhooks

Slack recibe los avisos a recepción y los errores del procesamiento. Puedes usar su [plan Free](https://slack.com/help/articles/115003205446-Slack-plans-and-features), que admite integraciones. Este procedimiento usa Incoming Webhooks; no requiere Workflow Builder ni configurar un bot con código.

1. Inicia sesión en [Slack](https://slack.com/) y crea o selecciona un espacio de trabajo para la prueba.
2. Crea un canal llamado, por ejemplo, `recepcion-alertas`.
3. Abre [Slack Apps](https://api.slack.com/apps), selecciona **Create New App → From scratch**, asigna un nombre como `Cerro Alto Leads` y elige ese espacio de trabajo.
4. En la configuración de la aplicación, abre **Incoming Webhooks** y activa **Activate Incoming Webhooks**.
5. Pulsa **Add New Webhook to Workspace**, selecciona `recepcion-alertas` y confirma con **Allow/Autorizar**. Para un canal privado debes pertenecer a él. Si la instalación está restringida, necesitas la aprobación del administrador del espacio.
6. Copia la **Webhook URL** de **Webhook URLs for Your Workspace**. La dirección del canal que muestra el navegador (`app.slack.com/client/...`) no es la URL del webhook.

El procedimiento y los nombres de las opciones se basan en la [guía oficial de Incoming Webhooks](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).

Pega la URL privada generada en el archivo `.env` local:

```dotenv
ALERT_WEBHOOK_URL=REEMPLAZAR_POR_LA_WEBHOOK_URL_GENERADA_EN_SLACK
ALERT_WEBHOOK_TOKEN=
```

Reemplaza completamente el texto de ejemplo por tu URL HTTPS. `ALERT_WEBHOOK_TOKEN` queda vacío con Slack: el programa solo añade Authorization cuando ese token opcional tiene contenido. `ADMIN_TOKEN` es independiente y se configura según las instrucciones anteriores. Guarda `.env` con Ctrl+S y, si el servidor estaba activo, detenlo con Ctrl+C y vuelve a iniciarlo para cargar el cambio.

La URL permite publicar en el canal seleccionado y debe mantenerse privada, al igual que las claves. No la subas a Git ni la incluyas en capturas o logs públicos. El evaluador crea la suya; no necesita acceso al espacio de trabajo del candidato. El envío a Slack es saliente, de modo que la aplicación puede ejecutarse en localhost sin publicar un callback de Slack.

## Ejecutar las comprobaciones con servicios reales

Ejecuta los comandos desde la raíz del repositorio en una terminal PowerShell de VS Code. Antes de empezar, instala las dependencias y completa `.env` con pacientes, LLM, Slack y el token administrativo. Este recorrido utiliza servicios reales, consume su cuota y puede generar avisos; las pruebas offline se explican por separado más abajo.

### 1. Verificar las conexiones y enviar la alerta de prueba

No hace falta iniciar FastAPI para este primer comando:

```powershell
.\.venv\Scripts\python.exe parte-a/scripts/verificar_integraciones.py --alerta
```

Un resultado correcto tiene esta forma:

```json
{"api_health":"ok","busqueda_y_enriquecimiento":"ok","llm_json_validado":true,"presupuesto_estimado":null,"alerta_real":"enviada"}
```

Abre el canal elegido en Slack y comprueba que aparece un aviso **Cerro Alto · prueba_integracion_real**. `alerta_real=enviada` indica que el receptor aceptó el envío HTTP; revisar el canal comprueba que llegó al destino esperado. Este comando consulta pacientes y clasifica el ejemplo ficticio `info`; no crea leads ni envía los diez eventos. Si falla antes de la etapa de alertas, todavía no habrá mensaje de prueba en Slack. Sin `--alerta` comprueba pacientes y LLM, pero no envía ese aviso.

### 2. Iniciar el servidor y dejar abierta la terminal

```powershell
.\.venv\Scripts\python.exe -m uvicorn cerro_alto.app:create_app --factory --app-dir parte-a --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Espera a ver `Application startup complete`. Mantén esta terminal abierta; usa un solo servidor para esa base. Si ya había uno funcionando, detenlo antes de arrancar otro. Para apagarlo al terminar utiliza Ctrl+C.

### 3. En otra terminal, enviar y verificar los diez eventos

Abre una segunda terminal en la misma raíz del repositorio. Primero comprueba el servidor:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Se espera `status=ok`; `/ready` devuelve 503 si encuentra configuración incompleta u otros problemas operativos. Después ejecuta el comando usado en la demostración:

```powershell
.\.venv\Scripts\python.exe parte-a/scripts/verificar_payloads.py
```

La URL predeterminada es `http://127.0.0.1:8000`; también puedes indicarla explícitamente como argumento. El script envía los diez JSON originales, consulta estados hasta terminar (hasta 300 s por defecto) y verifica rutas, duplicados y recepciones. Debe finalizar con:

```text
Verificación HTTP completada. Los mocks de Pytest son una prueba separada.
```

El evento 02 tiene exactamente el mismo contenido e ID que el 01. Ambos se envían y sus recepciones quedan guardadas, pero el reporte actual omite la línea del 02 porque la consulta por ID devuelve el resultado del evento original. La comprobación por `desde` exige al menos dos recepciones de ese ID y una marcada como duplicada. Por eso ver nueve líneas de archivos no significa que se haya perdido el décimo envío. El evento 03 tiene un ID diferente y se muestra como duplicado comercial del 01.

Los eventos 01 y 07 deben ser calientes; 08 debe ser basura; 03 debe ser duplicado. Los informativos pueden ser tibios o pasar a calientes si la API confirma un tratamiento pendiente; el script consulta el detalle administrativo para comprobar ese motivo. Por ejemplo, en la ejecución real revisada, 06 fue caliente por su historial y 09 fue tibio. No se fija artificialmente una ruta para simular la respuesta de pacientes.

`enviar_payloads.py` es el emisor original y se conserva: envía los diez archivos e imprime respuestas de recepción, pero no verifica el resultado final. `verificar_payloads.py` realiza ambos pasos; no necesitas ejecutar los dos para la misma evaluación. Repetir eventos sobre la misma base verifica idempotencia y no debe generar nuevos avisos por leads ya procesados. Para comprobar un procesamiento completo desde cero usa otro archivo `DB_PATH` y reinicia el servidor, conservando las bases anteriores.

### 4. Interpretar los mensajes que aparecen en Slack

| Tipo de aviso | Cuándo aparece | Qué debe revisar el operador |
| --- | --- | --- |
| `prueba_integracion_real` | Al ejecutar `verificar_integraciones.py --alerta` | Que el canal recibe mensajes; no representa un lead nuevo |
| `lead_caliente` | Un lead necesita atención prioritaria | Motivo, identificadores y propuesta de coordinación |
| `error_procesamiento` | Falló una etapa, aunque después pueda recuperarse | Componente, código y estado en el momento del aviso |
| `error_infraestructura` | Falló la infraestructura y se pudo enviar el aviso de último recurso | Disponibilidad del servicio y almacenamiento |

Ejemplo ilustrativo de aviso, sin identificadores ni contactos reales:

```text
Cerro Alto · lead_caliente
Notificación: <id_del_aviso>
event_id: <id_del_evento>
lead_id: <id_del_lead>
motivo: solicitud_de_agendamiento_o_contacto_pronto
propuesta: Proponemos coordinar una valoración con recepción. Indica sede y fecha preferidas; recepción debe verificar disponibilidad y completar los datos obligatorios antes de confirmar.
```

`Notificación` identifica el aviso; `event_id` identifica el evento recibido; `lead_id` permite buscar el detalle mediante el comando administrativo. El aviso es para recepción: no envía un WhatsApp al paciente ni confirma una cita.

Un aviso de error puede tener esta forma:

```text
Cerro Alto · error_procesamiento
Notificación: <id_del_aviso>
event_id: <id_del_evento>
lead_id: <id_del_lead>
componente: pacientes
codigo: pacientes_http_500
estado: pendiente
```

`pendiente` indica que en ese momento el trabajo seguía en proceso o reintento. Los avisos históricos no se editan cuando el evento se recupera: consulta el estado final en la aplicación. Si se agotan los intentos queda `fallido` y se puede reprocesar después de corregir la causa. Los códigos `pacientes_http_429`, `pacientes_error_red` y `pacientes_respuesta_invalida` señalan respectivamente límite de solicitudes, conexión/timeout y respuesta que no cumplió el esquema esperado.

No se manda un aviso comercial por cada tibio, basura o duplicado; sus resultados se guardan. Sí puede aparecer un aviso de error de cualquiera de esos eventos antes de obtener su ruta final. La cantidad y orden de errores externos pueden variar entre ejecuciones; no hace falta provocar errores para que la prueba sea válida.

Para confirmar el estado actual, desde la segunda terminal:

```powershell
.\.venv\Scripts\python.exe parte-a/scripts/operar.py
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --estado fallido
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --estado pendiente
```

Una ejecución terminada correctamente no debe dejar pendientes ni fallidos de esos ejemplos. En una base nueva, sin otros eventos, las dos últimas consultas devolverán listas vacías; la lista de fallos históricos puede seguir conteniendo errores ya recuperados. En el [informe de validación](parte-a/VALIDACION.md) se distinguen los resultados reales de los tests con mocks.

### Problemas habituales al reproducir el recorrido

| Síntoma | Comprobación |
| --- | --- |
| El verificador no conecta a localhost | Iniciar Uvicorn, dejar la primera terminal abierta y comprobar el puerto |
| Falta configuración o `/ready` devuelve 503 | Completar `.env`, revisar la salida y reiniciar el servidor; el token de alertas vacío es válido para Slack |
| LLM devuelve 401 | Revisar la API key y que corresponda al proveedor de la URL, sin mostrarla en logs |
| Slack devuelve 400/403/404 | Revisar que se copió la Webhook URL, que la aplicación siga autorizada y que el canal esté disponible |
| Pacientes devuelve 429 o 5xx | El worker programa reintentos; respetar los límites y revisar los estados; el script de conexiones informa el error y puede repetirse después |
| Hay un error viejo en Slack pero el script terminó correctamente | Consultar el estado final; un aviso `pendiente` no implica que el evento siga pendiente |
| No aparecen nuevos avisos al reenviar los mismos JSON | La idempotencia evita volver a procesar y avisar por los mismos eventos |

## Comprobar la aplicación

Con el servidor activo:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
.\.venv\Scripts\python.exe enviar_payloads.py http://127.0.0.1:8000/webhook
Invoke-RestMethod 'http://127.0.0.1:8000/leads/estado?event_id=evt_01JB7Q3M2X4K8R9T5W6Y'
Invoke-RestMethod 'http://127.0.0.1:8000/leads/estado?desde=2000-01-01T00:00:00Z'
```

`/health` comprueba servicio y acceso a SQLite. `/ready` comprueba configuración, procesador activo, alertas agotadas y métricas del SLA; **no certifica conectividad con terceros**. Si faltan credenciales, la aplicación conserva eventos, registra fallos explícitos y los deja recuperables. No marca como procesado lo que no pudo ejecutar.

La fecha `desde` usa la recepción UTC del servidor, no `created_at` de Meta; los ejemplos tienen fechas de negocio futuras que no se rechazan por ese motivo. Envía el `+` de un offset como `%2B` en una URL o usa parámetros HTTP codificados. La respuesta por evento tiene exactamente los ocho campos exigidos por INVERS. Desconocidos: 404. Debes elegir `event_id` o `desde`, no ambos. Con `desde` se devuelve una lista por recepción, incluidos reenvíos y eventos sin ID.

## Pruebas automatizadas y pruebas reales

```powershell
# Sin credenciales ni solicitudes a servicios externos: usa MockTransport explícito.
.\.venv\Scripts\python.exe -m pytest -m 'not integration' -q

# Servicios REALES: comprueba health, búsqueda/paginación que corresponda y LLM.
.\.venv\Scripts\python.exe parte-a/scripts/verificar_integraciones.py

# Además envía un aviso REAL de prueba al webhook configurado.
.\.venv\Scripts\python.exe parte-a/scripts/verificar_integraciones.py --alerta

# Con la app activa: envía los 10 originales, espera y verifica sus estados por HTTP.
.\.venv\Scripts\python.exe parte-a/scripts/verificar_payloads.py http://127.0.0.1:8000

# Alternativa de integración real mediante Pytest (opcional y explícita).
$env:RUN_REAL_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest -m integration -q
Remove-Item Env:RUN_REAL_INTEGRATION
```

Las pruebas reales usan datos ficticios del repositorio, consumen cuota y quedan registradas por INVERS. El script de payloads falla si faltan integraciones o encuentra estados fallidos; no introduce mocks. En leads informativos admite caliente únicamente si el detalle administrativo demuestra un tratamiento pendiente confirmado. Para repetir la evaluación con datos iniciales utiliza otra `DB_PATH`; no borres una base con eventos que deban recuperarse.

La suite cubre los diez ejemplos, concurrencia, familiares, entradas inválidas, contratos de estado, salida del LLM, presupuesto sin respaldo, inyección, paginación, errores de API, límite persistente, reintentos, avisos, reinicio y reprocesamiento. Consulta [VALIDACION.md](parte-a/VALIDACION.md) para distinguir lo ejecutado de lo pendiente.

## Revisar y recuperar

Las solicitudes administrativas requieren el siguiente encabezado HTTP, sustituyendo el marcador por el valor de tu `.env`:

```http
Authorization: Bearer <ADMIN_TOKEN>
```

El prefijo debe ser exactamente `Bearer ` seguido del token. No se acepta el token como parámetro de URL. Un encabezado ausente, incorrecto o con caracteres no ASCII devuelve 401, sin ejecutar la operación solicitada.

| Endpoint protegido | Operación |
| --- | --- |
| `POST /leads/reprocesar` | Reprocesar un evento fallido por event_id o receipt_id |
| `GET /admin/leads/{lead_id}` | Consultar resultado y mensajes preparados |
| `GET /admin/operacion` | Consultar métricas y fallos |
| `GET /admin/revision` | Listar recepciones para revisión |
| `GET /admin/recepciones/{receipt_id}` | Consultar el contenido original recibido |

Estos comandos leen `ADMIN_TOKEN` de `.env` y requieren el servidor activo:

```powershell
.\.venv\Scripts\python.exe parte-a/scripts/operar.py
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --estado fallido
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --estado rechazado
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --receipt-id RECEIPT_ID
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --lead-id LEAD_ID
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --reprocesar EVENT_ID
.\.venv\Scripts\python.exe parte-a/scripts/operar.py --reprocesar-recepcion RECEIPT_ID
```

`operar.py` añade el encabezado Bearer automáticamente. Copiar el token a `.env` no crea una variable de entorno de PowerShell; estos comandos no necesitan que lo exportes a la terminal.

`--receipt-id` muestra el original para revisión: puede contener datos personales. El resto muestra estados, motivos, advertencias, mensajes y propuestas según la operación. Evita copiar esa salida a logs o servicios públicos.

Los pendientes se retoman automáticamente. Tras un cierre abrupto se espera el vencimiento de su reserva, de hasta `LEASE_SECONDS` (240 s por defecto). Los fallidos agotan un máximo de tres intentos y requieren corregir la causa y reprocesar explícitamente; ese estado sobrevive al reinicio. Una alerta fallida se reenvía sin repetir pacientes ni LLM. Los rechazados se revisan y, si corresponde corregir contactos, se envía un evento nuevo con ID nuevo y referencia al `receipt_id` original en un campo adicional; el original se conserva.

Si el canal de avisos falla también fallan sus propios avisos: se registra el fallo, `/ready` devuelve 503 y `/admin/operacion` expone el problema. Configura un monitor externo del proceso y de `/ready`; una aplicación apagada no puede avisar por sí misma.

## Arquitectura y límites

El webhook solo normaliza y escribe en una transacción corta antes del 202. El procesador consulta pacientes y LLM fuera de esa transacción. La salida validada se enruta con reglas; resultados y avisos se guardan juntos. El envío de avisos tiene su propia tarea, de modo que una espera de pacientes no bloquee recepción.

- `cerro_alto/app.py`: webhook, contrato de estados, administración y salud.
- `storage.py`: recepciones, eventos, resultados, errores, reintentos, caché y límite persistente.
- `normalization.py`, `patients.py`, `llm.py`, `routing.py`: etapas del negocio.
- `worker.py`, `notifications.py`: procesamiento y entrega recuperable.
- `prompts/`, `tests/`, `scripts/`: prompt versionado, verificación y operación.

SQLite debe estar en disco local persistente. Usa un solo proceso de Uvicorn por base; un bloqueo impide arrancar un segundo procesador. Las peticiones concurrentes se protegen con transacciones y unicidad. El límite de pacientes se comparte entre reinicios de esta base, pero no entre máquinas con bases diferentes que usen el mismo token. La caché de páginas dura cinco minutos y evita repetirlas al diferir un trabajo; no se envían identidades ni historiales completos al LLM.

Calientes generan un aviso real y una propuesta pendiente de confirmación, nunca una cita inventada. Su estado final espera la confirmación HTTP del canal. Tibios tienen seguimiento y un primer mensaje preparado; basura conserva motivo y evidencia. **No se envían mensajes reales a pacientes.** Una señal urgente reconocida puede avisarse antes de las integraciones, sin duplicar el aviso final.

Completa `NATIONAL_HOLIDAYS` con los feriados nacionales observados, incluidos traslados, y activa `HOLIDAYS_COMPLETE` solo cuando corresponda. Mientras el calendario esté incompleto, los mensajes urgentes indican qué hacer si una sede está abierta y qué hacer si ambas están cerradas. No se supone disponibilidad de agenda ni se anuncian promociones sin comprobar su vigencia.

La protección frente a inyección combina separación de instrucciones/datos, eliminación de bloques explícitos, evidencia citada, esquema estricto y reglas de negocio. No garantiza inmunidad a todos los ataques ni reemplaza una evaluación real del modelo elegido. Los primeros mensajes son plantillas conservadoras para este flujo de leads, no la implementación completa del bot de la Parte B.

Para publicación, añade HTTPS y control de acceso de red: el contrato público de consulta devuelve contactos por requisito de evaluación y debe quedar accesible solo a evaluadores/operadores autorizados. Por defecto el servidor escucha en localhost. Conserva la base y sus backups en almacenamiento privado; no se implementó cifrado en reposo ni una política automática de retención. No expongas `.env`, SQLite ni endpoints administrativos. Los logs propios contienen únicamente códigos e identificadores internos.

Detalle de supuestos y limitaciones: [DECISIONES.md](parte-a/DECISIONES.md). Uso de IA: [AI_WORKFLOW.md](parte-a/AI_WORKFLOW.md). No se realizó push ni entrega de la prueba.

---

## Notas originales del repositorio base

Repositorio base de la **Parte A** de la prueba técnica de INVERS. Haz un **fork**, trabaja en tu fork y pega su enlace en la página de la prueba.

> Clínica ficticia, creada para la prueba técnica de INVERS.

## Estructura

```
README.md        # reemplázalo por el tuyo: cómo replicar tu sistema paso a paso
.env.example     # las variables que necesita tu sistema, sin valores reales
payloads/        # los 10 eventos de ejemplo del conector
enviar_payloads.py  # los envía a tu webhook: python3 enviar_payloads.py <URL>
parte-a/         # tu sistema: workflows exportados o código, prompts y tests
```

El brief de la Parte A, en la página de la prueba, explica qué construir y cómo lo evaluamos.

## Antes de hacer push

Tu fork es **público**. Nunca subas credenciales (tokens, API keys, contraseñas): usa un archivo `.env`, que ya está en `.gitignore`. Si se te escapa una, revócala de inmediato y anótalo en `DECISIONES`.
