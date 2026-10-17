# Adaptador de alertas salientes compatible con Slack Incoming Webhooks.
# Transforma el payload interno en un mensaje de texto con datos de operación.
# La URL de Slack ya contiene su secreto de acceso; ALERT_WEBHOOK_TOKEN es opcional
# y solo añade Bearer si un receptor distinto lo necesita. No es ADMIN_TOKEN.
# Este módulo envía; Worker y Store deciden cuándo reintentar y guardar el resultado.
import json

import httpx

from .config import Settings
from .errors import ServiceError, retry_after


# Envía mensajes de operación a un receptor compatible con JSON {text}.
class Notifier:
    """Webhook JSON compatible con Slack Incoming Webhooks y receptores de {text}."""
    # Recibe configuración y HTTPX compartido; no hace solicitudes al construirse.
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    # Construye un texto solo con campos operativos seleccionados y lo envía.
    # Usa el ID de notificación como clave de idempotencia, pero Slack puede ignorarla:
    # un fallo de red tras recibir el aviso puede producir duplicado al reintentar.
    async def send(self, notification):
        url = self.settings.alert_webhook_url.get_secret_value()
        if not url:
            raise ServiceError("alertas", "alertas_sin_configurar", retryable=False)
        payload = json.loads(notification["payload"])
        # Solo valores generados por el sistema, sin contactos ni historial clínico.
        lines = ["Cerro Alto · " + payload["tipo"], "Notificación: " + notification["id"]]
        for field in ("event_id", "lead_id", "motivo", "propuesta", "componente", "codigo", "estado"):
            if payload.get(field):
                lines.append(f"{field}: {payload[field]}")
        # La clave permanece estable al reintentar el mismo aviso y permite detectar
        # duplicados visualmente. Su aceptación como idempotencia depende del receptor.
        headers = {"Idempotency-Key": notification["id"], "Content-Type": "application/json"}
        token = self.settings.alert_webhook_token.get_secret_value()
        # Slack Incoming Webhooks funciona con este token vacío porque autentica
        # mediante la URL secreta; otros receptores pueden exigir Bearer adicional.
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = await self.client.post(url, json={"text": "\n".join(lines)}, headers=headers)
        except httpx.RequestError:
            raise ServiceError("alertas", "alertas_error_red") from None
        if not 200 <= response.status_code < 300:
            raise ServiceError("alertas", f"alertas_http_{response.status_code}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
                retry_after=retry_after(response.headers) if response.status_code == 429 else 0)
