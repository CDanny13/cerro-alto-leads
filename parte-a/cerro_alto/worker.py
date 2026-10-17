# Procesamiento asíncrono separado del webhook y recuperable mediante SQLite.
# Una tarea toma eventos; otra entrega notificaciones, para que sus reintentos
# sean independientes. HTTPX realiza las llamadas reales a pacientes, LLM y avisos.
# Cada fallo se convierte en un código controlado y se registra sin su contenido
# sensible. El número de intentos y la próxima ejecución quedan en la base.
# Al reiniciar no se recrean los leads desde memoria: se vuelve a consultar la cola.
import asyncio
from datetime import datetime
import json
import logging
import time

import httpx

from .config import Settings
from .errors import ServiceError
from .llm import LLMClient
from .models import NormalizedLead
from .notifications import Notifier
from .patients import PatientsClient
from .routing import ECUADOR, clinical_urgent, route, urgent_message
from .storage import Store, dumps

logger = logging.getLogger("cerro_alto")


# Coordina las integraciones reales con las dos colas persistentes de Store.
class Worker:
    # Crea un HTTPX compartido con timeout y sin redirecciones, y los adaptadores.
    # No se siguen redirecciones para no reenviar secretos hacia destinos imprevistos.
    def __init__(self, store: Store, settings: Settings, *, transport=None):
        self.store, self.settings = store, settings
        # HTTPX no hace reintentos automáticos: los controla la cola persistente.
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout),
                                      follow_redirects=False, transport=transport)
        self.patients = PatientsClient(settings, store, self.http)
        self.llm = LLMClient(settings, self.http)
        self.notifier = Notifier(settings, self.http)
        self.last_infra_alert = 0.0

    # Cierra conexiones HTTP al terminar y libera los recursos del cliente compartido.
    async def close(self):
        await self.http.aclose()

    # Calcula espera exponencial o Retry-After, tomando la mayor.
    # Registra un código seguro; los errores permanentes agotan el intento de inmediato.
    def report(self, event, error: ServiceError):
        delay = max(error.retry_after, self.settings.retry_seconds * 2 ** event["attempts"])
        self.store.fail_event(event["id"], error.component, error.code,
            self.settings.max_attempts if error.retryable else 1, delay, deferred=error.deferred)
        logger.warning("processing_failed event_internal=%s component=%s code=%s", event["id"], error.component, error.code)

    # Toma un evento reservado y reconstruye sus datos ya normalizados.
    # Emite prealerta clínica si procede, consulta pacientes, clasifica con LLM y
    # guarda el enrutamiento. Un fallo queda registrado para recuperación durable.
    async def process_once(self):
        event = self.store.claim_event(self.settings.lease_seconds)
        if not event:
            return False
        try:
            lead = NormalizedLead.model_validate_json(event["normalized"])
            if clinical_urgent(lead.message):
                # Un fallo externo no debe retrasar la señal urgente a recepción.
                self.store.urgent_prealert(event, urgent_message(self.settings, datetime.now(ECUADOR)))
            # Orden deliberado: primero se confirma contexto del paciente, luego
            # el modelo interpreta el mensaje y las reglas calculan la ruta definitiva.
            enrichment = await self.patients.enrich(lead)
            classification = await self.llm.classify(lead, enrichment)
            self.store.complete(event["id"], route(lead, enrichment, classification, self.settings))
        except ServiceError as error:
            self.report(event, error)
        except Exception as error:
            self.report(event, ServiceError("procesador", "error_interno_" + type(error).__name__))
        return True

    # Toma un aviso y realiza una entrega; el éxito o fallo queda en SQLite.
    # Tiene intentos propios para no repetir pacientes/LLM cuando falla solo Slack.
    async def notify_once(self):
        notification = self.store.claim_notification(self.settings.lease_seconds)
        if not notification:
            return False
        try:
            await self.notifier.send(notification)
            self.store.notification_sent(notification)
        except ServiceError as error:
            self.store.notification_failed(notification, error.code,
                self.settings.max_attempts if error.retryable else 1,
                max(error.retry_after, self.settings.retry_seconds * 2 ** notification["attempts"]))
            logger.error("notification_failed code=%s notification=%s", error.code, notification["id"])
        except Exception as error:
            self.store.notification_failed(notification, "error_interno_" + type(error).__name__,
                                            self.settings.max_attempts, self.settings.retry_seconds)
            logger.error("notification_internal_failure notification=%s", notification["id"])
        return True

    # Notifica errores que impiden operar normalmente con las colas.
    # Limita estos avisos directos a uno por minuto por instancia; es un último
    # recurso, por lo que no tiene la garantía durable de una alerta encolada.
    async def infrastructure_failure(self, error):
        code = "infra_" + type(error).__name__
        logger.error("infrastructure_failed code=%s", code)
        if time.monotonic() - self.last_infra_alert > 60:
            self.last_infra_alert = time.monotonic()
            # Último recurso si ni siquiera se puede escribir la cola de avisos.
            try:
                await self.notifier.send({"id": f"infra-{int(time.time() // 60)}", "payload": dumps({
                    "tipo": "error_infraestructura", "codigo": code})})
            except Exception:
                logger.error("infrastructure_alert_failed")

    # Bucle de consumo: procesa mientras haya eventos; si no, espera poll_seconds.
    # Contiene fallos de infraestructura para que la tarea pueda seguir intentando.
    async def run_events(self):
        while True:
            try:
                worked = await self.process_once()
                await asyncio.sleep(0 if worked else self.settings.poll_seconds)
            except Exception as error:
                await self.infrastructure_failure(error)
                await asyncio.sleep(self.settings.poll_seconds)

    # Bucle independiente de entrega de avisos, con espera cuando no hay trabajo.
    # La cancelación de la tarea al apagar FastAPI permite terminar el bucle.
    async def run_notifications(self):
        while True:
            try:
                worked = await self.notify_once()
                await asyncio.sleep(0 if worked else self.settings.poll_seconds)
            except Exception as error:
                await self.infrastructure_failure(error)
                await asyncio.sleep(self.settings.poll_seconds)
