# Casos de borde y regresiones de seguridad, concurrencia y recuperación.
# Se fuerzan entradas malformadas, respuestas LLM truncadas y fallos de servicios.
# Los mocks permiten verificar el comportamiento exacto sin red externa.
# Las pruebas administrativas distinguen autenticación (401), validación de entrada
# y resultados de operación; una credencial inválida no debe causar un error 503.
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from cerro_alto.app import create_app
from cerro_alto.config import Settings
from cerro_alto.models import Classification, Enrichment
from cerro_alto.normalization import normalize, phone
from cerro_alto.routing import route
from cerro_alto.storage import Store
from cerro_alto.worker import Worker
from test_classification import classification, llm_response
from test_worker import configured, drain, fixtures_handler, receive


# Envía un número infinito y un cuerpo excesivo; ambos deben dejar evidencia
# de recepción rechazada. Del cuerpo excesivo solo se conserva el prefijo permitido.
def test_oversize_and_non_finite_json_are_preserved_as_rejections(settings):
    with TestClient(create_app(settings)) as client:
        for data in (b'{"value":1e999}', b"x" * (settings.max_body_bytes + 10)):
            assert client.post("/webhook", content=data).json()["estado"] == "rechazado"
        assert len(client.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()) == 2


# Fuerza metadatos mal tipados pero conserva un correo válido.
# El lead se acepta con advertencias en lugar de descartarse por campos secundarios.
def test_metadata_anomalies_do_not_discard_valid_contact(settings, payloads):
    payload = deepcopy(payloads[0])
    payload.update(event_id=[], created_at=123, source={"platform": []})
    payload["lead"].update(phone=True, email="  Carla.Mendoza@EXAMPLE.COM ", message={"bad": "type"})
    normal, reason = normalize(payload)
    assert reason is None and normal.email == "carla.mendoza@example.com"
    assert normal.telefono_e164 is None
    assert {"event_id_ausente_o_invalido", "created_at_ausente_o_invalido", "source_ausente_o_invalido", "phone_invalido", "message_tipo_invalido"}.issubset(normal.warnings)
    with TestClient(create_app(settings)) as client:
        assert client.post("/webhook", json=payload).json()["estado"] == "pendiente"


# Un nuevo síntoma urgente con los mismos contactos debe crear trabajo propio
# y no quedar oculto por deduplicación comercial.
def test_new_urgent_message_is_not_hidden_by_contact_dedupe(settings, payloads):
    data = deepcopy(payloads[0])
    with TestClient(create_app(settings)) as client:
        first = client.post("/webhook", json=data).json()
        data["event_id"] = "evt_new_clinical_issue"
        data["lead"]["message"] = "Tengo dolor intenso que no cede"
        second = client.post("/webhook", json=data).json()
        assert second["estado"] == "pendiente" and second["lead_id"] != first["lead_id"]


# Reserva cuarenta cupos concurrentes y comprueba que exactamente treinta
# puedan usarse ahora; las diez restantes deben recibir una espera.
def test_concurrent_rate_reservations_never_exceed_limit(settings):
    store = Store(settings.db_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        slots = list(pool.map(lambda _: store.rate_slot("shared", now=1000), range(40)))
    assert slots.count(0) == 30
    assert sum(slot > 0 for slot in slots) == 10


# Agota la cuota local antes de procesar y comprueba pendiente, espera futura
# y contador de intentos intacto: la espera por cupo no es fallo del lead.
async def test_local_rate_limit_defers_without_consuming_attempts(settings, payloads):
    settings = configured(settings)
    store = Store(settings.db_path)
    receive(store, payloads[0])
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([])))
    for _ in range(30):
        assert store.rate_slot(worker.patients.service) == 0
    await worker.process_once()
    with store.connection() as db:
        row = db.execute("SELECT * FROM events").fetchone()
        assert row["attempts"] == 0 and row["estado"] == "pendiente"
        assert row["next_attempt"] > time.time() + 50
    await worker.close()


# Procesa una urgencia con servicios sanos y comprueba que la prealerta
# y la finalización compartan una única notificación.
async def test_urgent_prealert_is_not_sent_twice_after_success(settings, payloads):
    settings = configured(settings)
    store, alerts = Store(settings.db_path), []
    data = deepcopy(payloads[0])
    data["lead"]["message"] = "Tengo hinchazón en la cara"
    receive(store, data)
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler(alerts)))
    await drain(worker)
    await worker.close()
    assert len(alerts) == 1
    assert store.find(data["event_id"])["estado"] == "procesado"


# Simula que el LLM llama urgente a una consulta de horarios; las reglas
# deben conservar tibio al faltar evidencia clínica.
def test_injected_llm_priority_does_not_override_text(settings, payloads):
    lead = normalize(payloads[9])[0]
    wrong = Classification(**classification("solo quería saber hasta qué hora atienden los sábados", intencion="urgencia", urgencia="alta", siguiente_accion="derivar_asesor"))
    assert route(lead, Enrichment(identity="nuevo"), wrong, settings)["ruta"] == "tibio"


# Prueba sobres vacíos, tipos incorrectos, rechazo del proveedor y generación
# truncada: ninguno se acepta como JSON de clasificación utilizable.
@pytest.mark.parametrize("response", [
    {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
    {"choices": [{"message": {"refusal": "no", "content": None}}]}, {"choices": "wrong"},
])
async def test_llm_envelope_and_truncation_rejected(settings, payloads, response):
    from cerro_alto.errors import ServiceError
    from cerro_alto.llm import LLMClient
    from test_classification import llm_settings
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))) as http:
        with pytest.raises(ServiceError, match="llm_respuesta_invalida"):
            await LLMClient(llm_settings(settings), http).classify(normalize(payloads[8])[0], Enrichment(identity="nuevo"))


# Comprueba ocultación de secretos en repr y rechazo de URLs externas
# sin HTTPS o con credenciales incrustadas.
def test_secrets_not_in_configuration_repr_and_urls_must_be_secure(settings):
    assert "test-admin" not in repr(settings)
    for endpoint in ("http://public.example/v1", "https://user:secret@example.com", "https://example.com?token=secret"):
        with pytest.raises(ValueError):
            Settings(llm_api_url=endpoint)


# Captura los logs tras un 401 y exige código auditable sin nombres,
# contactos, tokens ni texto privado del error simulado.
async def test_no_sensitive_external_error_in_logs(settings, payloads, caplog):
    settings = configured(settings)
    store = Store(settings.db_path)
    receive(store, payloads[0])
    worker = Worker(store, settings, transport=httpx.MockTransport(fixtures_handler([], patient_status=401)))
    await drain(worker)
    await worker.close()
    text = caplog.text
    assert "pacientes_http_401" in text
    for secret in ("carla.mendoza@example.com", "Carla Mendoza", "0991234567", "test-admin", "private"):
        assert secret not in text


# Verifica autenticación, readiness degradado sin configuración y diferencias
# entre evento inexistente, estado no recuperable y cuerpo inválido.
def test_admin_contract_and_readiness(settings, payloads):
    with TestClient(create_app(settings)) as client:
        lead_id = client.post("/webhook", json=payloads[0]).json()["lead_id"]
        assert client.get("/admin/leads/" + lead_id).status_code == 401
        assert client.get("/admin/operacion").status_code == 401
        assert client.get("/ready").status_code == 503
        assert client.get("/health").json() == {"status": "ok"}
        headers = {"Authorization": "Bearer test-admin"}
        assert client.post("/leads/reprocesar", json={"event_id": payloads[0]["event_id"]}, headers=headers).status_code == 409
        assert client.post("/leads/reprocesar", json={"event_id": "unknown"}, headers=headers).status_code == 404
        assert client.post("/leads/reprocesar", json={}, headers=headers).status_code == 422
        assert client.get("/leads/estado", params={"event_id": "x", "desde": "2000-01-01T00:00:00Z"}).status_code == 400


# Recorre todos los endpoints protegidos con encabezados ausentes, incorrectos
# y no ASCII: siempre 401. Un Bearer válido permite llegar a la operación.
@pytest.mark.parametrize("method,path,body,authorized_status", [
    ("GET", "/admin/operacion", None, 200),
    ("GET", "/admin/revision", None, 200),
    ("GET", "/admin/leads/missing", None, 404),
    ("GET", "/admin/recepciones/missing", None, 404),
    ("POST", "/leads/reprocesar", {"event_id": "evt_missing"}, 404),
])
def test_admin_rejects_malformed_authorization_on_every_route(settings, method, path, body, authorized_status):
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        for value in (None, b"Bearer incorrect", b"test-admin", b"Basic incorrect", b"Bearer \xff"):
            headers = [] if value is None else [(b"authorization", value)]
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 401
        response = client.request(method, path, json=body, headers={"Authorization": "Bearer test-admin"})
        assert response.status_code == authorized_status


# Introduce un token de configuración no ASCII y exige rechazo seguro
# sin excepción interna durante compare_digest.
def test_non_ascii_admin_configuration_fails_closed(settings):
    from pydantic import SecretStr
    settings = settings.model_copy(update={"admin_token": SecretStr("configuracion_no_ascii_\u00f1")})
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/admin/operacion", headers={"Authorization": "Bearer incorrect"})
        assert response.status_code == 401


# Conserva un cuerpo rechazado, exige token para revisar y comprueba que
# el operador autorizado pueda recuperar exactamente el contenido original.
def test_review_rejected_original_requires_admin(settings):
    with TestClient(create_app(settings)) as client:
        response = client.post("/webhook", content=b'{"invalid":true}').json()
        headers = {"Authorization": "Bearer test-admin"}
        assert client.get("/admin/revision").status_code == 401
        assert client.get("/admin/recepciones/" + response["receipt_id"]).status_code == 401
        review = client.get("/admin/revision", params={"estado": "rechazado"}, headers=headers)
        assert review.status_code == 200 and review.json()[0]["receipt_id"] == response["receipt_id"]
        original = client.get("/admin/recepciones/" + response["receipt_id"], headers=headers).json()
        assert original["contenido_original"] == '{"invalid":true}'
        assert client.get("/admin/revision", params={"estado": "x"}, headers=headers).status_code == 400
        assert client.get("/admin/recepciones/unknown", headers=headers).status_code == 404


# Comprueba que la herramienta de integración real falle antes de la red
# si no hay credenciales, sin recurrir a simulaciones.
async def test_real_verifier_refuses_missing_credentials(settings):
    from cerro_alto.errors import ServiceError
    from cerro_alto.live_checks import check_integrations
    with pytest.raises(ServiceError, match="integraciones_sin_configurar"):
        await check_integrations(settings)


# Añade una urgencia a un texto de reenvío y comprueba que la prioridad
# clínica impida descartarlo como duplicado comercial.
def test_repeated_form_with_new_urgency_is_not_deduplicated(settings, payloads):
    store = Store(settings.db_path)
    receive(store, payloads[0])
    data = deepcopy(payloads[2])
    data["lead"]["message"] += " Ahora tengo hinchazón en la cara."
    assert receive(store, data)["estado"] == "pendiente"


# Comprueba derivación a asesor ante solicitud expresa y dudas sobre
# un tratamiento en curso confirmado.
def test_human_request_and_ongoing_clinical_case_are_referred(settings, payloads):
    lead = normalize(payloads[8])[0]
    lead.message = "Quiero hablar con una persona"
    result = route(lead, Enrichment(identity="nuevo"), Classification(**classification()), settings)
    assert result["derivar_asesor"] and "asesor" in result["primer_mensaje"]
    lead.message = "Tengo dudas sobre mi tratamiento"
    result = route(lead, Enrichment(identity="confirmado", has_ongoing=True), Classification(**classification()), settings)
    assert result["derivar_asesor"]


# Prueba infinito, NaN y texto arbitrario: la espera debe volver al valor
# de respaldo de cinco segundos en lugar de bloquear permanentemente la cola.
def test_invalid_retry_after_does_not_block_forever():
    from cerro_alto.errors import retry_after
    for value in ("inf", "NaN", "no es fecha"):
        assert retry_after({"Retry-After": value}) == 5
