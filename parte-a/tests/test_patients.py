# Pruebas del cliente de pacientes con HTTPX MockTransport explícito.
# Los handlers son servidores simulados exclusivos de tests: inspeccionan la
# solicitud y devuelven páginas o fallos controlados, sin acceso a la API real.
# Se comprueban identidad familiar, paginación, caché, errores y rate limit durable.
# La base temporal sí usa SQLite real para verificar persistencia local.
from copy import deepcopy

import httpx
import pytest

from cerro_alto.errors import ServiceError
from cerro_alto.normalization import normalize
from cerro_alto.patients import PatientsClient
from cerro_alto.storage import Store


# Configura endpoints y secretos ficticios para este conjunto de tests.
# MockTransport intercepta las solicitudes; estos valores no son credenciales reales.
def configured(settings):
    return settings.model_copy(update={"api_pacientes_url": "https://patients.test",
                                       "api_pacientes_token": settings.admin_token})


# Construye un paciente ficticio de contrato válido; permite variar identidad
# y contactos sin modificar los ejemplos originales.
def patient(**changes):
    return {"id": "pac_carla", "nombre_completo": "Carla Mendoza Ruiz", "telefono": "+593991234567",
            "email": "carla.mendoza@example.com", "sede_habitual": "norte", "ultima_visita": "2026-05-14",
            "estado": "activo", **changes}


# Simula dos páginas de pacientes y dos de tratamientos.
# Comprueba Bearer, codificación del teléfono, selección del paciente y caché
# reutilizable desde otra instancia del almacén.
async def test_search_and_treatments_pagination(settings, payloads):
    calls = []
    # Receptor simulado de esta prueba: inspecciona la solicitud y devuelve
    # la respuesta controlada del escenario. No realiza conexiones externas.
    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-admin"
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/pacientes":
            assert request.url.params["telefono"] == "+593991234567"
            assert "%2B593" in str(request.url)
            if not cursor:
                return httpx.Response(200, json={"data": [patient(id="relative", nombre_completo="Pedro Mendoza")], "next_cursor": "p2"})
            return httpx.Response(200, json={"data": [patient()], "next_cursor": None})
        return httpx.Response(200, json={"data": [{"id": "trt_" + (cursor or "1"), "tipo": "endodoncia",
            "pieza": "36", "estado": "pendiente" if cursor else "completado", "fecha_plan": "2026-01-01"}],
            "next_cursor": None if cursor else "t2"})
    store = Store(settings.db_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PatientsClient(configured(settings), store, http)
        result = await client.enrich(normalize(payloads[0])[0])
        assert result.identity == "confirmado" and result.has_pending
        assert result.candidate_count == 2
        assert len(calls) == 4
        # La caché también funciona al crear otro cliente/proceso sobre la misma DB.
        result2 = await PatientsClient(configured(settings), Store(settings.db_path), http).enrich(normalize(payloads[0])[0])
        assert result2 == result and len(calls) == 4


# Parametriza 400/401/404/429/5xx para verificar códigos seguros y reintentos.
# También comprueba que 429 pause futuras consultas sin exponer el cuerpo de error.
@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (404, False), (429, True), (500, True), (503, True)])
async def test_api_errors(settings, payloads, status, retryable):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, headers={"Retry-After": "2"}, json={"error": {"message": "private"}}))) as http:
        client = PatientsClient(configured(settings), Store(settings.db_path), http)
        with pytest.raises(ServiceError) as caught:
            await client.enrich(normalize(payloads[0])[0])
        assert caught.value.retryable is retryable
        assert caught.value.code == f"pacientes_http_{status}"
        assert "private" not in str(caught.value)
        if status == 429:
            assert caught.value.retry_after == 2
            with pytest.raises(ServiceError) as again:
                await client.enrich(normalize(payloads[0])[0])
            assert again.value.deferred


# Un familiar con el mismo contacto debe quedar ambiguo y no disparar
# una consulta a sus tratamientos como si fuera la misma persona.
async def test_ambiguous_family_does_not_fetch_history(settings, payloads):
    calls = []
    # Receptor simulado de esta prueba: inspecciona la solicitud y devuelve
    # la respuesta controlada del escenario. No realiza conexiones externas.
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": [patient(nombre_completo="Pedro Mendoza")], "next_cursor": None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PatientsClient(configured(settings), Store(settings.db_path), http)
        result = await client.enrich(normalize(payloads[0])[0])
        assert result.identity == "ambiguo" and not result.has_pending
        assert calls == ["/v1/pacientes"]


# Hace que el servidor repita cursor indefinidamente; el cliente debe cortar
# la paginación con un error controlado.
async def test_pagination_cycle_and_invalid_response(settings, payloads):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "next_cursor": "same"}))) as http:
        with pytest.raises(ServiceError, match="paginacion_cursor_repetido"):
            await PatientsClient(configured(settings), Store(settings.db_path), http).enrich(normalize(payloads[0])[0])


# Simula una página mal tipada y un timeout con texto sensible.
# Ambos deben producir códigos controlados, sin propagar la excepción externa.
async def test_bad_schema_and_timeout(settings, payloads):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": "wrong"}))) as http:
        with pytest.raises(ServiceError, match="pacientes_respuesta_invalida"):
            await PatientsClient(configured(settings), Store(settings.db_path), http).enrich(normalize(payloads[0])[0])
    # Simula un timeout para comprobar su traducción a un código de error seguro.
    def timeout(request):
        raise httpx.ReadTimeout("sensitive", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(ServiceError, match="pacientes_error_red"):
            await PatientsClient(configured(settings), Store(settings.db_path), http).enrich(normalize(payloads[0])[0])


# Reserva treinta cupos con reloj fijo, recrea Store y verifica que el límite
# persiste hasta vencer la ventana de sesenta segundos.
def test_rate_limit_persists_and_respects_thirty(settings):
    store = Store(settings.db_path)
    for _ in range(30):
        assert store.rate_slot("test", now=1000) == 0
    assert Store(settings.db_path).rate_slot("test", now=1001) > 59
    assert store.rate_slot("test", now=1061) == 0


# Simula cuota restante cero en una respuesta válida y exige aplazar
# la siguiente llamada según la cabecera de restablecimiento.
async def test_remaining_reset_header_blocks_future_requests(settings, payloads):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "45"},
        json={"data": [patient()], "next_cursor": None}))) as http:
        client = PatientsClient(configured(settings), Store(settings.db_path), http)
        with pytest.raises(ServiceError) as caught:
            await client.enrich(normalize(payloads[0])[0])
        assert caught.value.deferred and caught.value.retry_after > 44
