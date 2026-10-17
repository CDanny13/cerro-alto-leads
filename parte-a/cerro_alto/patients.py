# Cliente real de la API de pacientes: búsqueda, identidad y tratamientos.
# Antes de cada petición consulta caché y reserva un cupo persistente de rate limit.
# Recorre cursores de paginación y valida cada página con modelos estrictos.
# Un contacto compartido no confirma a la persona: también se compara el nombre
# y se exige una única coincidencia. La ambigüedad evita consultar un historial
# que podría corresponder a un familiar. Los errores se traducen a ServiceError.
import hashlib
import json
import math
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import Settings
from .errors import ServiceError, retry_after
from .models import Enrichment, NormalizedLead
from .normalization import name_key, phone
from .storage import Store


# Contrato de paciente utilizado por la solución; campos extra del proveedor
# no forman parte del modelo guardado en caché.
class Patient(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=100)
    nombre_completo: str
    telefono: str | None = None
    email: str | None = None
    sede_habitual: Literal["norte", "valle"]
    ultima_visita: str | None = None
    estado: Literal["activo", "inactivo"]


# Contrato de tratamiento necesario para detectar pendientes y casos en curso.
class Treatment(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str = Field(min_length=1, max_length=100)
    tipo: str
    pieza: str | None = None
    estado: Literal["pendiente", "en_curso", "completado", "cancelado"]
    fecha_plan: str


# Página de búsqueda: lista de pacientes y cursor siguiente, o None al terminar.
class PatientPage(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[Patient]
    next_cursor: str | None


# Página de tratamientos con el mismo mecanismo de continuación por cursor.
class TreatmentPage(BaseModel):
    model_config = ConfigDict(strict=True)
    data: list[Treatment]
    next_cursor: str | None


# Exige al menos dos componentes y acepta nombres completos o prefijos
# compatibles. Es una regla conservadora de coincidencia, no verificación documental.
def matching_name(incoming: str, registered: str) -> bool:
    left, right = name_key(incoming).split(), name_key(registered).split()
    # "Carla Mendoza" puede corresponder a "Carla Mendoza Ruiz", pero "Carla" no basta.
    return len(left) >= 2 and len(right) >= 2 and (
        left == right or left == right[:len(left)] or right == left[:len(right)])


# Cliente de pacientes con caché, limitación de tasa y validación de respuestas.
class PatientsClient:
    # Reutiliza el cliente HTTP del worker y agrupa la cuota por huella del token.
    # La clave persistida permite compartir el límite local sin guardar el token literal.
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient):
        self.settings, self.store, self.client = settings, store, client
        self.service = "pacientes:" + hashlib.sha256(
            settings.api_pacientes_token.get_secret_value().encode()).hexdigest()[:16]

    # Obtiene una página validada: configuración -> caché -> cupo -> petición Bearer.
    # 429 bloquea futuros cupos; 5xx/408 pueden reintentarse y errores como 400/401/404
    # se conservan como fallos no reintentables. Nunca se registra el cuerpo externo.
    async def _page(self, path, params, model):
        if not self.settings.api_pacientes_url or not self.settings.api_pacientes_token.get_secret_value():
            raise ServiceError("pacientes", "pacientes_sin_configurar", retryable=False)
        url = self.settings.api_pacientes_url.rstrip("/") + path
        key = hashlib.sha256((self.service + url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
        # Una página ya validada evita consumir otro cupo, incluso al retomar
        # un lead cuya consulta falló en una página posterior.
        cached = self.store.cached(key)
        if cached is not None:
            return model.model_validate(cached)
        # Si no hay cupo se sale sin petición HTTP; Worker guarda la espera
        # y puede atender otros trabajos mientras tanto.
        delay = self.store.rate_slot(self.service)
        if delay:
            raise ServiceError("pacientes", "rate_limit_local", retry_after=delay, deferred=True)
        try:
            response = await self.client.get(url, params=params, headers={
                "Authorization": "Bearer " + self.settings.api_pacientes_token.get_secret_value(),
                "Accept": "application/json",
            })
        except httpx.RequestError:
            raise ServiceError("pacientes", "pacientes_error_red") from None
        if response.status_code == 429:
            delay = retry_after(response.headers)
            self.store.block_rate(self.service, delay)
            raise ServiceError("pacientes", "pacientes_http_429", retry_after=delay)
        if response.headers.get("X-RateLimit-Remaining") == "0":
            try:
                reset = float(response.headers.get("X-RateLimit-Reset", "60"))
                if not math.isfinite(reset):
                    raise ValueError()
                self.store.block_rate(self.service, max(1.0, reset))
            except ValueError:
                self.store.block_rate(self.service, 60)
        if response.status_code != 200:
            raise ServiceError("pacientes", f"pacientes_http_{response.status_code}",
                               retryable=response.status_code >= 500 or response.status_code == 408)
        try:
            page = model.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ServiceError("pacientes", "pacientes_respuesta_invalida") from None
        # Se excluyen campos ajenos al contrato utilizado, incluida la fecha de nacimiento.
        self.store.cache(key, page.model_dump())
        return page

    # Acumula páginas hasta next_cursor=None y elimina IDs repetidos.
    # Detecta cursores vacíos/cíclicos y limita a cien páginas para no entrar en bucles.
    async def _all(self, path, params, model):
        values, seen = [], set()
        query = dict(params)
        for _ in range(100):
            page = await self._page(path, query, model)
            values.extend(page.data)
            if page.next_cursor is None:
                return list({value.id: value for value in values}.values())
            if not page.next_cursor or page.next_cursor in seen:
                raise ServiceError("pacientes", "paginacion_cursor_repetido", retryable=False)
            seen.add(page.next_cursor)
            query["cursor"] = page.next_cursor
        raise ServiceError("pacientes", "paginacion_excede_limite", retryable=False)

    # Busca con los contactos disponibles y cruza contacto con nombre.
    # Sin candidatos es nuevo; sin una coincidencia única es ambiguo. Solo con
    # identidad confirmada consulta tratamientos y devuelve sus indicadores mínimos.
    async def enrich(self, lead: NormalizedLead) -> Enrichment:
        params = {}
        if lead.telefono_e164:
            params["telefono"] = lead.telefono_e164
        if lead.email:
            params["email"] = lead.email
        if not params:
            raise ServiceError("pacientes", "consulta_sin_contacto", retryable=False)
        candidates = await self._all("/v1/pacientes", params, PatientPage)
        if not candidates:
            return Enrichment(identity="nuevo")
        matching = []
        for candidate in candidates:
            shared_phone = bool(lead.telefono_e164 and phone(candidate.telefono) == lead.telefono_e164)
            shared_email = bool(lead.email and candidate.email and candidate.email.strip().casefold() == lead.email)
            if matching_name(lead.full_name, candidate.nombre_completo) and (shared_phone or shared_email):
                matching.append(candidate)
        # Cero o varias coincidencias impiden atribuir tratamientos a esta persona.
        # Se conserva el caso como ambiguo para valoración humana.
        if len(matching) != 1:
            return Enrichment(identity="ambiguo", candidate_count=len(candidates))
        patient = matching[0]
        treatments = await self._all(f"/v1/pacientes/{quote(patient.id, safe='')}/tratamientos", {}, TreatmentPage)
        return Enrichment(identity="confirmado", patient_id=patient.id,
                          sede_habitual=patient.sede_habitual, ultima_visita=patient.ultima_visita,
                          has_pending=any(t.estado == "pendiente" for t in treatments),
                          has_ongoing=any(t.estado == "en_curso" for t in treatments),
                          candidate_count=len(candidates))
