# Pruebas del recorrido recepción -> cola -> clasificación -> alerta.
# SQLite y el Worker son reales; los tres servicios externos son mocks explícitos.
# Se controlan fallos para comprobar límites de reintento, recuperación tras reinicio,
# entrega de alertas y urgencias. Los dominios .test nunca son servicios de producción.
# Las funciones auxiliares de este archivo también se reutilizan en otras pruebas.
from copy import deepcopy
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from cerro_alto.app import create_app
from cerro_alto.models import Enrichment
from cerro_alto.normalization import normalize
from cerro_alto.process_lock import ProcessLock
from cerro_alto.storage import Store
from cerro_alto.worker import Worker
from test_classification import classification, llm_response
from test_patients import patient


# Configura endpoints y secretos ficticios para este conjunto de tests.
# MockTransport intercepta las solicitudes; estos valores no son credenciales reales.
def configured(settings):
    return settings.model_copy(update={"api_pacientes_url": "https://patients.test",
        "api_pacientes_token": settings.admin_token, "llm_api_url": "https://llm.test/chat/completions",
        "llm_api_key": settings.admin_token, "llm_model": "test-model",
        "alert_webhook_url": settings.admin_token.__class__("https://alerts.test/hook")})


# Crea un servidor externo ficticio configurable para pacientes, LLM y avisos.
# Todo acceso a un destino no previsto falla: no permite una red real accidental.
def fixtures_handler(alerts, *, patient_status=200, llm_status=200, alert_status=200, invalid_llm=False):
    """Respuestas predefinidas EXCLUSIVAMENTE para pruebas, no clasificador alternativo."""
    # Receptor simulado de esta prueba: inspecciona la solicitud y devuelve
    # la respuesta controlada del escenario. No realiza conexiones externas.
    def handler(request):
        if request.url.host == "patients.test":
            if patient_status != 200:
                return httpx.Response(patient_status, json={"error": {"message": "private"}})
            if "/tratamientos" in request.url.path:
                if request.url.params.get("cursor"):
                    return httpx.Response(200, json={"data": [{"id": "trt_pending", "tipo": "endodoncia", "pieza": "36",
                        "estado": "pendiente", "fecha_plan": "2026-01-01"}], "next_cursor": None})
                return httpx.Response(200, json={"data": [], "next_cursor": "page2"})
            matches = [patient(id="pac_jorge", nombre_completo="Jorge Paredes", telefono="+593998877665",
                               email="jorge.paredes@example.com")] if request.url.params.get("email") == "jorge.paredes@example.com" else []
            return httpx.Response(200, json={"data": matches, "next_cursor": None})
        if request.url.host == "llm.test":
            if llm_status != 200:
                return httpx.Response(llm_status)
            if invalid_llm:
                return httpx.Response(200, json=llm_response({"bad": "schema"}))
            message = json.loads(json.loads(request.content)["messages"][1]["content"])["mensaje"]
            if "GANA $500" in message:
                output = classification("GANA $500 DIARIOS DESDE CASA", intencion="spam", es_spam=True, siguiente_accion="descartar")
            elif "quisiera agendar" in message:
                output = classification("quisiera agendar un blanqueamiento esta semana", intencion="agendar", siguiente_accion="coordinar_valoracion")
            elif "decidida a empezar" in message:
                output = classification("¿Me pueden dar una cita para la valoración esta semana", intencion="agendar", siguiente_accion="coordinar_valoracion", interes_alto_valor=True)
            else:
                output = classification(message[:100] or "sin mensaje")
            return httpx.Response(200, json=llm_response(output))
        if request.url.host == "alerts.test":
            assert "Idempotency-Key" in request.headers
            alerts.append(json.loads(request.content)["text"])
            return httpx.Response(alert_status)
        raise AssertionError("Acceso de red inesperado")
    return handler


# Auxiliar de test que normaliza y guarda un payload directamente en Store.
# Permite probar el worker sin necesitar una solicitud HTTP de preparación.
def receive(store, payload):
    lead, reason = normalize(payload)
    return store.receive(json.dumps(payload), payload, lead, reason)


# Ejecuta un paso de cada cola hasta no encontrar trabajo listo.
# El límite evita un test bloqueado si una regresión produce un bucle infinito.
async def drain(worker, limit=60):
    for _ in range(limit):
        job = await worker.process_once()
        notice = await worker.notify_once()
        if not job and not notice:
            return
    raise AssertionError("La cola no se vació")


# Procesa diez recepciones con APIs ficticias y verifica estados, rutas
# y tres alertas sin contactos sensibles. El paciente pendiente simulado explica
# que una ruta informativa pueda elevarse por enriquecimiento confirmado.
async def test_ten_payloads_end_to_end_with_explicit_mocks(settings, payloads):
    settings = configured(settings)
    app = create_app(settings)
    alerts = []
    with TestClient(app) as client:
        for payload in payloads:
            assert client.post("/webhook", json=payload).status_code == 202
        worker = Worker(app.state.store, settings, transport=httpx.MockTransport(fixtures_handler(alerts)))
        try:
            await drain(worker)
        finally:
            await worker.close()
        states = client.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()
        assert len(states) == 10
        assert [s["estado"] for s in states] == ["procesado", "duplicado", "duplicado"] + ["procesado"] * 7
        assert [s["ruta"] for s in states] == ["caliente", None, None, "tibio", "tibio", "tibio", "caliente", "basura", "caliente", "tibio"]
        assert len(alerts) == 3
        assert not any("carla.mendoza@example.com" in text or "+593991234567" in text for text in alerts)
        for index in [0, 6, 8]:
            detail = client.get("/admin/leads/" + states[index]["lead_id"], headers={"Authorization": "Bearer test-admin"}).json()
            assert detail["resultado"]["propuesta_agendamiento"]
        assert app.state.store.operations()["calientes_fuera_sla"] == 0


# Fuerza fallos de pacientes o LLM hasta agotar intentos; después reprocesa
# con token y servicio recuperado, comprobando que el estado se conserve en SQLite.
@pytest.mark.parametrize("component,status", [("pacientes", 500), ("pacientes", 401), ("llm", 503), ("llm", 200)])
async def test_failures_are_bounded_durable_and_reprocessable(settings, payloads, component, status):
    settings = configured(settings)
    store = Store(settings.db_path)
    receive(store, payloads[0])
    broken = fixtures_handler([], patient_status=status if component == "pacientes" else 200,
        llm_status=status if component == "llm" else 200, invalid_llm=component == "llm" and status == 200)
    worker = Worker(store, settings, transport=httpx.MockTransport(broken))
    await drain(worker)
    await worker.close()
    failed = store.find(payloads[0]["event_id"])
    assert failed["estado"] == "fallido" and failed["ruta"] is None and failed["motivo"]
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM failures WHERE component=?", (component,)).fetchone()[0] == (1 if status == 401 else 3)
    # Reiniciar app no elimina la cola de fallos; reprocesar requiere token.
    with TestClient(create_app(settings)) as client:
        assert client.post("/leads/reprocesar", json={"event_id": payloads[0]["event_id"]}).status_code == 401
        assert client.post("/leads/reprocesar", json={"event_id": payloads[0]["event_id"]}, headers={"Authorization": "Bearer test-admin"}).status_code == 200
        recovered = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([])))
        await drain(recovered)
        await recovered.close()
        assert store.find(payloads[0]["event_id"])["estado"] == "procesado"


# Reserva un evento y simula que muere el procesador. Tras vencer la lease,
# otra instancia debe retomarlo y terminarlo sin perderlo.
async def test_pending_survives_process_crash_and_lease_expiration(settings, payloads):
    settings = configured(settings)
    store = Store(settings.db_path)
    receive(store, payloads[0])
    job = store.claim_event(60)
    assert job
    assert Store(settings.db_path).claim_event(60) is None
    # Simula reloj posterior al vencimiento de la reserva de un proceso muerto.
    with store.connection(write=True) as db:
        db.execute("UPDATE events SET lease_until=?", (time.time()-1,))
    worker = Worker(Store(settings.db_path), settings, transport=httpx.MockTransport(fixtures_handler([])))
    await drain(worker)
    await worker.close()
    assert store.find(payloads[0]["event_id"])["estado"] == "procesado"


# Agota el envío de alerta, reabre el fallo y permite únicamente peticiones
# al receptor de avisos. Así detecta cualquier reclasificación o consulta innecesaria.
async def test_notification_failure_and_retry_without_reclassifying(settings, payloads):
    settings = configured(settings)
    store = Store(settings.db_path)
    receive(store, payloads[0])
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([], alert_status=503)))
    await drain(worker)
    await worker.close()
    assert store.find(payloads[0]["event_id"])["motivo"] == "alerta_caliente_no_entregada"
    assert store.operations()["alertas"]["fallida"] == 1
    assert store.reprocess(event_id=payloads[0]["event_id"])
    # Acepta únicamente entrega de avisos: cualquier nueva consulta a pacientes
    # o LLM hace fallar el test de recuperación sin reclasificación.
    def only_alerts(request):
        assert request.url.host == "alerts.test", "No debe repetir pacientes ni LLM"
        return httpx.Response(200)
    worker = Worker(Store(settings.db_path), settings, transport=httpx.MockTransport(only_alerts))
    await drain(worker)
    await worker.close()
    assert store.find(payloads[0]["event_id"])["estado"] == "procesado"


# Introduce síntomas urgentes y una API de pacientes caída.
# La prealerta debe salir una vez aunque la clasificación no logre completarse.
async def test_urgent_alert_even_when_api_down(settings, payloads):
    settings = configured(settings)
    payload = deepcopy(payloads[0])
    payload["lead"]["message"] = "Tengo hinchazón en la cara y dolor intenso"
    store, alerts = Store(settings.db_path), []
    receive(store, payload)
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler(alerts, patient_status=500)))
    await drain(worker)
    await worker.close()
    assert any("senales_de_urgencia_preclasificacion" in text for text in alerts)
    assert sum("lead_caliente" in text for text in alerts) == 1
    assert store.find(payload["event_id"])["estado"] == "fallido"


# Ejecuta sin servicios configurados y exige fallo explícito en lugar
# de aparentar éxito con un paciente o clasificación inventados.
async def test_missing_configuration_never_uses_fake_results(settings, payloads):
    store = Store(settings.db_path)
    receive(store, payloads[0])
    worker = Worker(store, settings)
    await drain(worker)
    await worker.close()
    assert store.find(payloads[0]["event_id"])["estado"] == "fallido"
    assert store.find(payloads[0]["event_id"])["motivo"] == "pacientes_sin_configurar"
    assert store.operations()["alertas"]["fallida"] == 1


# Activa las tareas reales de lifespan y espera procesamiento automático.
# Los servicios siguen simulados; lo que se verifica es el ciclo de vida del worker.
def test_real_background_lifecycle_with_mock_services(settings, payloads):
    enabled = configured(settings).model_copy(update={"worker_enabled": True, "poll_seconds": 0.01})
    with TestClient(create_app(enabled, transport=httpx.MockTransport(fixtures_handler([])))) as client:
        assert client.get("/ready").status_code == 200
        client.post("/webhook", json=payloads[0])
        for _ in range(200):
            state = client.get("/leads/estado", params={"event_id": payloads[0]["event_id"]}).json()
            if state["estado"] == "procesado":
                break
            time.sleep(0.01)
        assert state["ruta"] == "caliente"


# Intenta tomar dos bloqueos sobre una base y exige rechazar el segundo.
# Tras cerrar el primero, un nuevo procesador sí debe poder arrancar.
def test_single_worker_guard(settings):
    first = ProcessLock(settings.db_path)
    try:
        with pytest.raises(RuntimeError, match="único worker"):
            ProcessLock(settings.db_path)
    finally:
        first.close()
    second = ProcessLock(settings.db_path)
    second.close()


# Procesa un lead válido sin ID, fuerza fallo y lo recupera mediante receipt_id.
# La falta de identificador externo no impide conservar ni recuperar el trabajo.
async def test_valid_no_event_id_and_reprocess_by_receipt(settings, payloads):
    settings = configured(settings)
    payload = deepcopy(payloads[0])
    payload.pop("event_id")
    store = Store(settings.db_path)
    received = receive(store, payload)
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([], patient_status=401)))
    await drain(worker)
    await worker.close()
    assert store.since(0)[0]["event_id"] is None and store.since(0)[0]["estado"] == "fallido"
    assert store.reprocess(receipt_id=received["receipt_id"])
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([])))
    await drain(worker)
    await worker.close()
    assert store.since(0)[0]["estado"] == "procesado"
