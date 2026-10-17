# Comprobación deliberada de servicios reales, independiente del servidor HTTP.
# Usa un ejemplo ficticio de INVERS para probar salud, pacientes y JSON del LLM.
# Puede enviar un aviso real si el operador lo solicita; no crea las diez recepciones
# del webhook ni certifica por sí sola el flujo completo o el SLA de producción.
# La API y el modelo configurados pueden consumir cuota al ejecutar esta función.
import json

import httpx

from .config import ROOT, Settings
from .errors import ServiceError
from .llm import LLMClient
from .normalization import normalize
from .notifications import Notifier
from .patients import PatientsClient
from .storage import Store, dumps


# Valida configuración, consulta health y enriquece/clasifica el evento 09.
# Devuelve indicadores sin contactos. send_alert=True añade un aviso solicitado
# por el operador; si alguna llamada falla no se informa un éxito simulado.
async def check_integrations(settings: Settings, *, send_alert=False) -> dict:
    required = settings.readiness()
    if not required["pacientes"] or not required["llm"] or (send_alert and not required["alertas"]):
        raise ServiceError("configuracion", "integraciones_sin_configurar", retryable=False)
    store = Store(settings.db_path)
    async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=False) as http:
        try:
            health = await http.get(settings.api_pacientes_url.rstrip("/") + "/v1/health")
            if health.status_code != 200 or health.json().get("status") != "ok":
                raise ServiceError("pacientes", "health_no_disponible", retryable=False)
        except (httpx.RequestError, ValueError, AttributeError):
            raise ServiceError("pacientes", "health_no_disponible", retryable=False) from None
        # Solo datos ficticios proporcionados por INVERS. Estas llamadas quedan registradas por la API.
        payload = json.loads((ROOT / "payloads/evento_09.json").read_text(encoding="utf-8"))
        lead, _ = normalize(payload)
        enrichment = await PatientsClient(settings, store, http).enrich(lead)
        classification = await LLMClient(settings, http).classify(lead, enrichment)
        if send_alert:
            from uuid import uuid4
            await Notifier(settings, http).send({"id": str(uuid4()), "payload": dumps({
                "tipo": "prueba_integracion_real", "codigo": "comprobacion_solicitada_por_operador"})})
        return {"api_health": "ok", "busqueda_y_enriquecimiento": "ok", "llm_json_validado": True,
                "presupuesto_estimado": classification.presupuesto_estimado,
                "alerta_real": "enviada" if send_alert else "no_solicitada"}
