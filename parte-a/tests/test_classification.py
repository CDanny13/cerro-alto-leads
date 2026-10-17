# Pruebas de clasificación y reglas con respuestas LLM simuladas explícitas.
# MockTransport permite revisar el contrato HTTP sin gastar cuota de un modelo.
# También se ejecutan reglas deterministas directamente, para distinguir su
# comportamiento del de un modelo real. Los asserts comprueban evidencia, presupuesto,
# prioridad clínica y textos preparados; no certifican precisión de un LLM externo.
from copy import deepcopy
from datetime import datetime, timezone
import json

import httpx
import pytest

from cerro_alto.errors import ServiceError
from cerro_alto.llm import LLMClient, consultation
from cerro_alto.models import Classification, Enrichment
from cerro_alto.normalization import normalize
from cerro_alto.routing import clinical_urgent, route, urgent_message


# Fábrica de salida LLM ficticia válida; changes permite alterar una condición
# para aislar qué valida cada test. Se utiliza exclusivamente en pruebas.
def classification(message="info", **changes):
    return {"intencion": "informacion", "urgencia": "baja", "presupuesto_estimado": None,
            "presupuesto_evidencia": None, "siguiente_accion": "seguimiento",
            "interes_alto_valor": False, "es_spam": False, "evidencia": message, **changes}


# Envuelve la salida ficticia en el formato Chat Completions que espera el cliente.
def llm_response(value):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


# Configura un proveedor .test y una clave ficticia para MockTransport.
def llm_settings(settings):
    return settings.model_copy(update={"llm_api_url": "https://llm.test/chat/completions",
        "llm_api_key": settings.admin_token, "llm_model": "test-model"})


# Comprueba selección automática o explícita del parámetro de límite de tokens.
# El handler verifica el cuerpo enviado; no realiza una llamada a OpenAI.
@pytest.mark.parametrize("url,parameter,expected", [
    ("https://api.openai.com/v1/chat/completions", "auto", "max_completion_tokens"),
    ("https://llm.test/chat/completions", "auto", "max_tokens"),
    ("https://llm.test/chat/completions", "max_completion_tokens", "max_completion_tokens"),
    ("https://api.openai.com/v1/chat/completions", "max_tokens", "max_tokens"),
])
async def test_output_limit_parameter_matches_provider(settings, payloads, url, parameter, expected):
    configured = llm_settings(settings).model_copy(update={
        "llm_api_url": url, "llm_model": "gpt-5.4", "llm_token_parameter": parameter,
    })
    # Receptor simulado de esta prueba: inspecciona la solicitud y devuelve
    # la respuesta controlada del escenario. No realiza conexiones externas.
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.4"
        assert body[expected] == 1200
        alternative = "max_tokens" if expected == "max_completion_tokens" else "max_completion_tokens"
        assert alternative not in body
        return httpx.Response(200, json=llm_response(classification()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LLMClient(configured, http).classify(normalize(payloads[8])[0], Enrichment(identity="nuevo"))
        assert result.presupuesto_estimado is None


# Comprueba el contrato HTTP construido y la reducción de datos enviados al LLM.
# El nombre del caso se refiere al contrato real del cliente; la respuesta es mock.
async def test_real_http_contract_and_minimization(settings, payloads):
    lead = normalize(payloads[6])[0]
    # Receptor simulado de esta prueba: inspecciona la solicitud y devuelve
    # la respuesta controlada del escenario. No realiza conexiones externas.
    def handler(request):
        assert request.headers["Authorization"] == "Bearer test-admin"
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        user = json.loads(body["messages"][1]["content"])
        assert "cita para la valoración esta semana" in user["mensaje"]
        assert "full_name" not in user and "email" not in user and "patient_id" not in user
        return httpx.Response(200, json=llm_response(classification("¿Me pueden dar una cita para la valoración esta semana", intencion="agendar", siguiente_accion="coordinar_valoracion")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await LLMClient(llm_settings(settings), http).classify(lead, Enrichment(identity="nuevo"))
        assert result.intencion == "agendar" and result.presupuesto_estimado is None


# Prueba tipos, campos y valores prohibidos: una respuesta con HTTP 200
# también debe rechazarse si no cumple el esquema de clasificación.
@pytest.mark.parametrize("value", ["texto libre", {"intencion": "caliente"}, classification(urgencia="extrema"),
    classification(presupuesto_estimado=-1), classification(es_spam="false"), classification(extra="inventado")])
async def test_invalid_llm_outputs(settings, payloads, value):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=llm_response(value)))) as http:
        with pytest.raises(ServiceError, match="llm_respuesta_invalida"):
            await LLMClient(llm_settings(settings), http).classify(normalize(payloads[8])[0], Enrichment(identity="nuevo"))


# Fuerza importes y evidencias ausentes del mensaje y exige su rechazo.
async def test_invented_budget_and_evidence_rejected(settings, payloads):
    for output in [classification(presupuesto_estimado=25, presupuesto_evidencia="USD 25"),
                   classification(evidencia="Quiero agendar mañana")]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=llm_response(output)))) as http:
            with pytest.raises(ServiceError):
                await LLMClient(llm_settings(settings), http).classify(normalize(payloads[8])[0], Enrichment(identity="nuevo"))


# Añade una declaración literal de presupuesto y comprueba que se acepte
# solo con importe y evidencia coincidentes.
async def test_explicit_budget_accepted(settings, payloads):
    lead = normalize(payloads[8])[0]
    lead.message = "Mi presupuesto es de USD 250. Quiero información."
    output = classification("Quiero información", presupuesto_estimado=250, presupuesto_evidencia="Mi presupuesto es de USD 250")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=llm_response(output)))) as http:
        assert (await LLMClient(llm_settings(settings), http).classify(lead, Enrichment(identity="nuevo"))).presupuesto_estimado == 250


# Recorre los ejemplos con clasificación controlada y paciente nuevo.
# Verifica rutas, propuestas, seguimiento y motivos sin depender de variación del LLM.
def test_expected_routes(settings, payloads):
    expected = {0: "caliente", 3: "tibio", 4: "tibio", 5: "tibio", 6: "caliente", 7: "basura", 8: "tibio", 9: "tibio"}
    for index, selected in expected.items():
        lead = normalize(payloads[index])[0]
        changes = {}
        if index in (0, 6):
            changes = {"intencion": "agendar", "siguiente_accion": "coordinar_valoracion"}
        elif index == 7:
            changes = {"intencion": "spam", "es_spam": True, "siguiente_accion": "descartar"}
        output = Classification(**classification(consultation(lead.message)[:100], **changes))
        result = route(lead, Enrichment(identity="nuevo"), output, settings)
        assert result["ruta"] == selected
        assert result["clasificacion"]["presupuesto_estimado"] is None
        if selected == "caliente":
            assert result["propuesta_agendamiento"] and "confirm" in result["propuesta_agendamiento"]
        if selected == "tibio":
            assert result["seguimiento"] and result["primer_mensaje"]
        if selected == "basura":
            assert result["motivo"]


# Verifica que un tratamiento pendiente de identidad confirmada eleve a caliente
# aunque el texto inicial sea breve e informativo.
def test_pending_treatment_overrides_brief_message(settings, payloads):
    lead = normalize(payloads[8])[0]
    result = route(lead, Enrichment(identity="confirmado", has_pending=True), Classification(**classification()), settings)
    assert result["ruta"] == "caliente"
    assert result["motivo_ruta"] == "paciente_con_tratamiento_pendiente"


# Prueba señales clínicas con una clasificación informativa deliberadamente baja.
# Las reglas deben priorizar al lead y preparar orientación de urgencias.
@pytest.mark.parametrize("message", ["dolor intenso que no cede", "Tengo hinchazón en la cara", "sangrado que no se detiene", "golpe en un diente", "fiebre con dolor dental"])
def test_clinical_urgency_does_not_depend_on_llm(settings, payloads, message):
    lead = normalize(payloads[8])[0]
    lead.message = message
    result = route(lead, Enrichment(identity="nuevo"), Classification(**classification()), settings)
    assert result["ruta"] == "caliente" and result["derivar_asesor"]
    assert "911" in result["primer_mensaje"] or "+593 2 000 4410" in result["primer_mensaje"]


# Comprueba negaciones de síntomas y limpieza del bloque de instrucción
# maliciosa conocido, conservando la consulta real sobre horarios.
def test_negation_and_injection(settings, payloads):
    assert not clinical_urgent("No tengo hinchazón, solo pregunto precios")
    assert not clinical_urgent("No tengo dolor intenso")
    cleaned = consultation(payloads[9]["lead"]["message"])
    assert "clasifica" not in cleaned and "sábados" in cleaned


# Fija fechas y calendario para verificar orientación con sede abierta, cerrada
# y en feriado, sin depender del reloj actual de la máquina.
def test_emergency_hours_and_holiday(settings):
    complete = settings.model_copy(update={"holidays_complete": True, "national_holidays": "2026-11-03"})
    assert "ECU 911" in urgent_message(complete, datetime(2026, 11, 3, 17, tzinfo=timezone.utc))
    assert "+593 2 000 4410" in urgent_message(complete, datetime(2026, 11, 4, 17, tzinfo=timezone.utc))
    assert "ECU 911" in urgent_message(complete, datetime(2026, 11, 4, 2, tzinfo=timezone.utc))
