# Entrada HTTP de la solución: crea FastAPI y conecta endpoints con SQLite.
# Recorrido de un lead: POST /webhook -> normalización -> Store.receive -> ACK 202.
# El procesamiento externo ocurre después, en las tareas de Worker; por eso una
# API lenta no mantiene abierta la solicitud del conector durante su procesamiento.
# Los endpoints de estado muestran el contrato público; los de administración
# permiten revisar resultados y originales únicamente con ADMIN_TOKEN.
# Leer después normalization.py, storage.py y worker.py para seguir el recorrido.
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import math
import secrets

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from starlette.background import BackgroundTask

from .config import Settings
from .models import LeadStatus
from .normalization import normalize
from .storage import Store, timestamp

logger = logging.getLogger("cerro_alto")
# Las URLs de pacientes incluyen contactos y un webhook puede incluir un secreto en su ruta.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# Convierte un número JSON y rechaza infinito o NaN.
# Así no se aceptan valores como 1e999 que Python puede interpretar como infinito.
def finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("número no finito")
    return result


# Cuerpo del reprocesamiento manual: identifica el evento o su recepción.
# receipt_id permite recuperar también casos que llegaron sin event_id.
class ReprocessRequest(BaseModel):
    event_id: str | None = None
    receipt_id: str | None = None

    # Exige exactamente un identificador: ambos o ninguno son ambiguos.
    # Pydantic convierte este ValueError en una respuesta de validación 422.
    @model_validator(mode="after")
    def one_identifier(self):
        if bool(self.event_id) == bool(self.receipt_id):
            raise ValueError("Indica event_id o receipt_id")
        return self


# Construye una instancia con su configuración y almacén.
# transport solo permite inyectar HTTPX MockTransport en las pruebas;
# la ejecución normal usa HTTPX con servicios reales.
def create_app(settings: Settings | None = None, *, transport=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)

    # Administra recursos durante el arranque y el cierre de FastAPI.
    # Arranca las dos tareas del worker bajo un bloqueo de proceso único;
    # al cerrar las cancela, cierra HTTPX y libera reservas de trabajo pendientes.
    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        process_lock = None
        worker = None
        try:
            if settings.worker_enabled:
                from .worker import Worker
                from .process_lock import ProcessLock
                process_lock = ProcessLock(settings.db_path)
                worker = Worker(store, settings, transport=transport)
                app.state.worker = worker
                # Eventos y avisos progresan en tareas distintas dentro del mismo proceso.
                # La cola real está en SQLite, no en estos objetos asyncio.
                tasks = [asyncio.create_task(worker.run_events()), asyncio.create_task(worker.run_notifications())]
            app.state.worker_tasks = tasks
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if worker:
                await worker.close()
                store.release_leases()
            if process_lock:
                process_lock.close()

    app = FastAPI(title="Cerro Alto · Leads", lifespan=lifespan)
    app.state.store = store
    app.state.settings = settings

    # Dependencia de autenticación aplicada a cada endpoint administrativo.
    # Requiere el encabezado Authorization: Bearer seguido del ADMIN_TOKEN;
    # compare_digest compara de forma segura y todo valor inválido termina en 401.
    def admin(authorization: str | None = Header(default=None)):
        token = settings.admin_token.get_secret_value()
        # compare_digest sobre str solo acepta ASCII; un encabezado inválido es 401.
        if (not token or not token.isascii() or not authorization or not authorization.isascii()
                or not secrets.compare_digest(authorization, "Bearer " + token)):
            raise HTTPException(401, "Se requiere un token administrativo válido")

    # Captura errores inesperados que llegaron hasta FastAPI.
    # Devuelve 503 sin detalles sensibles y programa un aviso de infraestructura
    # si existe worker. Un error de persistencia nunca se disfraza como ACK 202.
    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logger.error("request_failed code=%s", type(error).__name__)
        worker = getattr(app.state, "worker", None)
        return JSONResponse(status_code=503, content={"error": "persistencia_o_servicio_no_disponible"},
                            background=BackgroundTask(worker.infrastructure_failure, error) if worker else None)

    # Lee el cuerpo con un límite de tamaño, interpreta JSON y normaliza el lead.
    # Incluso entradas inválidas se guardan para revisión. Store.receive confirma
    # la transacción antes del retorno; aquí no se llama a pacientes, LLM ni Slack.
    @app.post("/webhook", status_code=202)
    @app.post("/webhook/leads", status_code=202)
    async def webhook(request: Request):
        # Se acumula solo el máximo permitido para acotar memoria y conservar
        # un prefijo auditable cuando el cuerpo excede el límite.
        raw = bytearray()
        too_large = False
        async for chunk in request.stream():
            available = settings.max_body_bytes - len(raw)
            raw.extend(chunk[:max(0, available)])
            if len(chunk) > available:
                too_large = True
                break
        # Un cuerpo ilegible sigue el mismo circuito de persistencia, con motivo
        # de rechazo; no se descarta antes de generar su recepción.
        payload = None
        reason = "cuerpo_excede_limite: contenido_truncado_para_revision" if too_large else None
        if not reason:
            try:
                payload = json.loads(bytes(raw), parse_float=finite_float,
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeDecodeError, RecursionError):
                reason = "json_invalido"
        lead, validation_error = normalize(payload, settings.max_message_chars)
        # Esta llamada es local y transaccional. Solo después de su commit
        # se construye la respuesta 202; un fallo de escritura termina en 503.
        receipt = store.receive(raw.decode("utf-8", "replace"), payload, lead,
                                reason or validation_error, settings.dedupe_hours)
        return receipt

    # Implementa GET /leads/estado con una sola modalidad por consulta.
    # event_id devuelve el evento canónico; desde devuelve sus recepciones,
    # incluidas las que no tenían ID. No se aceptan fechas sin zona horaria.
    @app.get("/leads/estado", response_model=LeadStatus | list[LeadStatus])
    def status(event_id: str | None = Query(default=None), desde: str | None = Query(default=None)):
        if (event_id is None) == (desde is None):
            raise HTTPException(400, "Indica exactamente event_id o desde")
        if event_id is not None:
            result = store.find(event_id)
            if result is None:
                raise HTTPException(404, "Evento no recibido")
            return result
        try:
            return store.since(timestamp(desde))
        except (ValueError, OverflowError):
            raise HTTPException(400, "desde debe ser ISO 8601 con zona horaria") from None

    # Solicita recuperación manual de un evento fallido y devuelve pendiente.
    # No recibido -> 404; estado no recuperable -> 409. El worker lo retomará después.
    @app.post("/leads/reprocesar", dependencies=[Depends(admin)])
    def reprocess(body: ReprocessRequest):
        result = store.reprocess(body.event_id, body.receipt_id)
        if result is None:
            raise HTTPException(404, "Evento no recibido")
        if not result:
            raise HTTPException(409, "Solo se reprocesan eventos fallidos")
        return {"estado": "pendiente"}

    # Devuelve datos normalizados y resultado preparado de un lead.
    # Puede contener información personal; por eso requiere la dependencia admin.
    @app.get("/admin/leads/{lead_id}", dependencies=[Depends(admin)])
    def details(lead_id: str):
        result = store.details(lead_id)
        if result is None:
            raise HTTPException(404, "Lead no encontrado")
        return result

    # Expone conteos, fallos recientes y alertas fuera del SLA para el operador.
    @app.get("/admin/operacion", dependencies=[Depends(admin)])
    def operations():
        return store.operations()

    # Lista recepciones para revisión, opcionalmente filtradas por estado.
    # Valida el filtro para no aceptar estados ajenos al contrato.
    @app.get("/admin/revision", dependencies=[Depends(admin)])
    def review(estado: str | None = None):
        if estado and estado not in {"pendiente", "fallido", "rechazado", "duplicado", "procesado"}:
            raise HTTPException(400, "Estado no válido")
        return store.review(estado)

    # Recupera el contenido original de una recepción por su ID interno.
    # Es una operación administrativa porque el cuerpo puede contener datos personales.
    @app.get("/admin/recepciones/{receipt_id}", dependencies=[Depends(admin)])
    def receipt_detail(receipt_id: str):
        result = store.receipt_detail(receipt_id)
        if result is None:
            raise HTTPException(404, "Recepción no encontrada")
        return result

    # Comprueba que la aplicación puede abrir SQLite y ejecutar una consulta.
    # Este resultado no demuestra conexión con pacientes, LLM ni Slack.
    @app.get("/health")
    def health():
        with store.connection() as db:
            db.execute("SELECT 1")
        return {"status": "ok"}

    # Evalúa configuración, tareas activas y señales operativas guardadas.
    # Devuelve 503 si está degradado; no consulta servicios externos en cada chequeo
    # ni demuestra por sí solo que unas credenciales sean aceptadas por el proveedor.
    @app.get("/ready")
    def ready():
        checks = settings.readiness()
        checks["worker"] = settings.worker_enabled
        if settings.worker_enabled:
            checks["worker_activo"] = bool(getattr(app.state, "worker_tasks", [])) and all(
                not task.done() for task in app.state.worker_tasks)
        operational = store.operations()
        checks["sin_alertas_fallidas"] = not operational["alertas"].get("fallida", 0)
        checks["sin_calientes_fuera_sla"] = not operational["calientes_fuera_sla"]
        ok = all(checks.values())
        return JSONResponse(status_code=200 if ok else 503, content={"status": "ok" if ok else "degradado", "checks": checks})

    return app
