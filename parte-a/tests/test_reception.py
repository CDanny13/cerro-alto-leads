# Pruebas de recepción, validación local e idempotencia con FastAPI TestClient.
# Ejercitan HTTP dentro del proceso y SQLite temporal, sin servicios externos.
# Cada caso prepara datos, envía una solicitud y usa assert para verificar el
# contrato esperado. Los rechazos deben conservarse, los reenvíos no crear personas
# adicionales y un fallo al guardar no debe devolver un ACK de éxito.
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import time

from fastapi.testclient import TestClient
import pytest

from cerro_alto.app import create_app
from cerro_alto.normalization import normalize


# Recibe los diez originales, comprueba contactos y duplicados y crea una
# nueva app sobre el mismo SQLite para demostrar persistencia tras reinicio.
def test_all_payloads_received_durably(settings, payloads):
    app = create_app(settings)
    with TestClient(app) as client:
        start = time.monotonic()
        responses = [client.post("/webhook", json=p) for p in payloads]
        assert all(r.status_code == 202 for r in responses)
        assert time.monotonic() - start < 10
        assert responses[1].json()["estado"] == "duplicado"
        assert responses[2].json()["duplicado_de"] == payloads[0]["event_id"]
        assert responses[4].json()["estado"] == "pendiente"
        assert responses[5].json()["telefono_e164"] == "+593987650021"
        assert responses[7].json()["email"] == "promo@ganadinero.example"
        states = client.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()
        assert len(states) == 10
        assert states[1]["estado"] == "duplicado"
        canonical = client.get("/leads/estado", params={"event_id": payloads[0]["event_id"]}).json()
        assert canonical["estado"] == "pendiente"
    # Nueva instancia, mismo archivo: no depende de memoria del proceso.
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json() == states


# Envía cuerpos JSON inválidos o de estructura incorrecta; exige guardarlos
# como rechazados y consultables, aunque no tengan event_id.
@pytest.mark.parametrize("body", [b"{", b"null", b"[]", b'{"lead":[]}', b'{"x":NaN}'])
def test_invalid_body_is_audited(settings, body):
    with TestClient(create_app(settings)) as client:
        response = client.post("/webhook", content=body)
        assert response.status_code == 202
        assert response.json()["estado"] == "rechazado"
        states = client.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()
        assert states[0]["event_id"] is None
        assert states[0]["motivo"]


# Elimina ID y contactos válidos para comprobar rechazo auditable.
# También verifica 404 y consultas de estado inválidas.
def test_rejected_contact_and_no_id(settings, payloads):
    data = deepcopy(payloads[0])
    data.pop("event_id")
    data["lead"].update(phone="falso", email="no-es-correo")
    with TestClient(create_app(settings)) as client:
        response = client.post("/webhook", json=data).json()
        assert response["estado"] == "rechazado"
        assert response["motivo"] == "sin_contacto_valido"
        assert response["telefono_e164"] is None
        assert client.get("/leads/estado", params={"event_id": "desconocido"}).status_code == 404
        assert client.get("/leads/estado").status_code == 400
        assert client.get("/leads/estado", params={"desde": "2026-01-01"}).status_code == 400
        assert client.get("/leads/estado", params={"desde": "bad"}).status_code == 400


# Cambia solo el nombre de un familiar y exige un lead separado
# aunque comparta teléfono y correo con el primero.
def test_shared_contacts_are_not_identity(settings, payloads):
    first = deepcopy(payloads[0])
    relative = deepcopy(first)
    relative["event_id"] = "evt_relative"
    relative["lead"]["full_name"] = "Pedro Mendoza"
    with TestClient(create_app(settings)) as client:
        a = client.post("/webhook", json=first).json()
        b = client.post("/webhook", json=relative).json()
        assert b["estado"] == "pendiente"
        assert b["lead_id"] != a["lead_id"]


# Envía simultáneamente el mismo ID: debe haber un único lead y todas
# las recepciones. Reutilizar ese ID con otro cuerpo no altera el original.
def test_concurrent_idempotency_and_conflicting_id(settings, payloads):
    with TestClient(create_app(settings)) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: client.post("/webhook", json=payloads[0]).json(), range(12)))
        assert sum(r["estado"] == "pendiente" for r in results) == 1
        assert len({r["lead_id"] for r in results}) == 1
        assert len(client.get("/leads/estado", params={"desde": "2000-01-01T00:00:00Z"}).json()) == 12
        conflict = deepcopy(payloads[0])
        conflict["lead"]["message"] = "Otro contenido"
        assert client.post("/webhook", json=conflict).json()["estado"] == "rechazado"
        assert client.get("/leads/estado", params={"event_id": conflict["event_id"]}).json()["estado"] == "pendiente"


# Comprueba formatos ecuatorianos e internacionales, nombre vacío, ciudad
# como lista y conservación del mensaje largo sin truncar su intención final.
def test_normalization_and_full_message(payloads):
    normal = [normalize(p)[0] for p in payloads]
    assert normal[0].telefono_e164 == normal[2].telefono_e164 == "+593991234567"
    assert normal[0].email == normal[2].email
    assert normal[3].telefono_e164 == "+13055550142"
    assert normal[4].full_name == "" and normal[4].email
    assert normal[5].city == "Quito"
    assert "city_lista_normalizada" in normal[5].warnings
    assert "cita para la valoración esta semana" in normal[6].message
    assert len(normal[6].message) > 4500
    assert "phone_invalido" in normal[7].warnings


# Sustituye receive por un fallo de escritura y exige HTTP 503.
# No puede haber ACK de éxito cuando no se guardó la recepción.
def test_storage_failure_does_not_ack(settings, payloads, monkeypatch):
    app = create_app(settings)
    # Simula un fallo de persistencia antes del ACK para verificar respuesta 503.
    def fail(*args, **kwargs):
        raise OSError("example-sensitive-value")
    monkeypatch.setattr(app.state.store, "receive", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post("/webhook", json=payloads[0]).status_code == 503
